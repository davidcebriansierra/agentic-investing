"""Agente Revisor de Bolsas / cross-market (spec v2.0, seccion 3.1).

Arranca del scanner de IBKR para quedarse con los mayores movers del dia (US + BME) en
vez de recorrer todo el universo; de esos obtiene series historicas de 20 dias y de los
indices de referencia (SP500, DAX, Nikkei, IBEX35), calcula correlaciones Pearson de
retornos diarios y razona senales de arrastre entre bolsas con el LLM.
"""
from __future__ import annotations

import logging
import math
import time

from src.agents.searchers.context import format_cross_market_context
from src.agents.searchers.llm_base import MarketDataLLMSearcher
from src.connectors.fundamentals import FundamentalsClient
from src.llm.base import LLMClient
from src.llm.prompts import PromptLibrary
from src.schemas.enums import AgentSource, Exchange
from src.schemas.market import OHLCVBar

logger = logging.getLogger("agentic.searchers.cross_market")

#: Indices de referencia que se obtienen siempre.
_REFERENCE_INDICES = ("SP500", "DAX", "NIKKEI", "IBEX35")
#: Numero de dias de historia para el calculo de correlacion.
_HISTORY_BARS = 20
#: Minimo de solapamiento (dias en comun) para reportar correlacion.
_MIN_OVERLAP = 10


def _returns(bars: list[OHLCVBar]) -> list[float]:
    """Retornos diarios log-simples: (close_t - close_{t-1}) / close_{t-1}."""
    return [
        (bars[i].close - bars[i - 1].close) / bars[i - 1].close
        for i in range(1, len(bars))
    ]


def _pearson(x: list[float], y: list[float]) -> float | None:
    """Correlacion de Pearson entre dos series de igual longitud. None si longitud < 2."""
    n = min(len(x), len(y))
    if n < 2:
        return None
    x, y = x[:n], y[:n]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    dy = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


class CrossMarketSearcher(MarketDataLLMSearcher):
    def __init__(
        self,
        market_data=None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 15,
        fundamentals_client: FundamentalsClient | None = None,
        min_move_pct: float = 0.0,
        movers_count: int = 25,
        sector_cache_ttl: int = 86_400,
    ) -> None:
        super().__init__(
            source=AgentSource.CROSS_MARKET,
            prompt_name="searchers/cross_market",
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
        )
        self.fundamentals_client = fundamentals_client
        #: Umbral minimo de variacion diaria (en %) para incluir un ticker en el detalle.
        self.min_move_pct = min_move_pct
        #: Nº de movers a pedir al scanner por mercado (US + BME).
        self.movers_count = movers_count
        #: TTL (s) de la cache de sectores; el sector es estatico -> TTL largo.
        self.sector_cache_ttl = sector_cache_ttl
        self._sector_cache: dict[str, str] = {}
        self._sector_cache_at: dict[str, float] = {}

    async def _candidate_tickers(self) -> list[str]:
        """Tickers a analizar: top movers del scanner (US + BME) ∩ watchlist.

        Arranca del scanner de IBKR (barato, no consume el pacing de historicos) en vez de
        recorrer las 500+; asi solo se piden OHLCV/sector de los que realmente se mueven.
        Si el cliente no expone scanner, cae a la watchlist completa (compat/tests). Una
        lista vacia (mercado plano) es un resultado valido: ese ciclo solo reporta indices.
        """
        get_fn = getattr(self.market_data, "get_top_movers", None)
        if get_fn is None:
            return list(self.watchlist)
        wl = set(self.watchlist)
        movers: list[str] = []
        for exchange in (Exchange.NYSE, Exchange.BME):
            try:
                res = await get_fn(exchange=exchange, count=self.movers_count)
            except Exception as exc:  # noqa: BLE001 - un mercado sin scanner no aborta el otro
                logger.warning("cross_market: scanner %s no disponible (%s).", exchange.value, exc)
                res = []
            movers.extend(t for t in res if t in wl)
        seen: set[str] = set()
        return [t for t in movers if not (t in seen or seen.add(t))]

    async def _sectors_for(self, tickers: list[str]) -> dict[str, str]:
        """Sectores de `tickers` con cache propia de TTL largo (el sector es estatico).

        Solo consulta al FundamentalsClient los tickers ausentes o caducados; el resto sale
        de la cache en memoria. Evita el fetch de fundamentales de todo el universo cada ciclo.
        """
        if self.fundamentals_client is None:
            return {t: self._sector_cache[t] for t in tickers if t in self._sector_cache}
        now = time.monotonic()
        stale = [
            t for t in tickers
            if now - self._sector_cache_at.get(t, -math.inf) >= self.sector_cache_ttl
        ]
        if stale:
            try:
                items = await self.fundamentals_client.fetch(stale)
                for item in items:
                    if item.sector:
                        self._sector_cache[item.ticker] = item.sector
                for t in stale:  # marca refrescados aunque vengan sin sector
                    self._sector_cache_at[t] = now
            except Exception as exc:  # noqa: BLE001
                logger.debug("cross_market: no se pudo obtener sectores (%s).", exc)
        return {t: self._sector_cache[t] for t in tickers if t in self._sector_cache}

    async def build_context(self) -> str:
        if self.market_data is None or not self.watchlist:
            return ""

        # --- Series historicas de indices de referencia ---
        index_returns: dict[str, list[float]] = {}
        index_last_change: dict[str, float] = {}
        for idx_name in _REFERENCE_INDICES:
            try:
                get_fn = getattr(self.market_data, "get_index_ohlcv", None)
                if get_fn is None:
                    raise AttributeError("get_index_ohlcv no disponible en este cliente")
                bars = await get_fn(idx_name, bars=_HISTORY_BARS + 1)
                if len(bars) >= 2:
                    rets = _returns(bars)
                    index_returns[idx_name] = rets
                    index_last_change[idx_name] = rets[-1] if rets else 0.0
            except Exception as exc:  # noqa: BLE001
                logger.debug("cross_market: sin datos para indice %s (%s).", idx_name, exc)

        # --- Candidatos: top movers del scanner (US + BME) ∩ watchlist ---
        candidates = await self._candidate_tickers()
        if not candidates:
            logger.info("cross_market: el scanner no devolvio movers; solo se reportan indices.")
            if not index_last_change:
                return ""
            return format_cross_market_context([], index_last_change)

        # --- Sectores de los movers (cache TTL largo, solo refresca lo caducado) ---
        ticker_sector = await self._sectors_for(candidates)

        # --- OHLCV + correlaciones, solo de los movers ---
        ticker_rows: list[dict] = []
        for ticker in candidates:
            try:
                bars = await self.market_data.get_ohlcv(
                    ticker, bars=_HISTORY_BARS + 1, interval="1 day"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("cross_market: sin OHLCV diario para %s (%s).", ticker, exc)
                continue
            if len(bars) < 2:
                continue
            rets = _returns(bars)
            change_1d = rets[-1] if rets else 0.0
            exchange = Exchange.BME if ticker.endswith(".MC") else Exchange.NYSE

            correlations: dict[str, float] = {}
            for idx_name, idx_rets in index_returns.items():
                # Alinear por los ultimos N dias en comun
                n = min(len(rets), len(idx_rets))
                if n >= _MIN_OVERLAP:
                    corr = _pearson(rets[-n:], idx_rets[-n:])
                    if corr is not None:
                        correlations[idx_name] = round(corr, 3)

            ticker_rows.append({
                "ticker": ticker,
                "exchange": exchange.value,
                "change_1d": change_1d,
                "close": bars[-1].close,
                "correlations": correlations,
                "sector": ticker_sector.get(ticker),
            })

        if not ticker_rows and not index_last_change:
            return ""

        # Filtrar planos (< umbral) y ordenar por mayor movimiento.
        threshold = self.min_move_pct / 100.0
        display_rows = [
            r for r in ticker_rows if abs(r.get("change_1d") or 0.0) >= threshold
        ]
        display_rows.sort(key=lambda r: abs(r.get("change_1d") or 0.0), reverse=True)

        # Resumen por sector limitado a los movers mostrados.
        sector_returns: dict[str, list[float]] = {}
        for r in display_rows:
            sec, chg = r.get("sector"), r.get("change_1d")
            if sec and chg is not None:
                sector_returns.setdefault(sec, []).append(chg)
        sector_summary = {sec: sum(v) / len(v) for sec, v in sector_returns.items() if v}
        sector_counts = {sec: len(v) for sec, v in sector_returns.items() if v}

        logger.info(
            "cross_market: %d movers del scanner, %d superan umbral %.2f%%.",
            len(candidates), len(display_rows), self.min_move_pct,
        )

        return format_cross_market_context(
            display_rows, index_last_change, sector_summary, sector_counts=sector_counts
        )
