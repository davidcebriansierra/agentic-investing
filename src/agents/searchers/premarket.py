"""Agente buscador de Apertura (spec v2.0, seccion 3.1).

Se activa en dos fases alrededor de la apertura de cada mercado, y unicamente para los
tickers de ESE mercado (BME=.MC, resto=US):

- `preopen`  [apertura - lead_minutes, apertura): snapshot con el precio de referencia
  previo a la apertura (subasta/pre-market segun mercado) via `get_premarket(phase="preopen")`.
- `postopen` [apertura, apertura + post_open_minutes): gap de apertura REAL (precio actual
  vs cierre previo) via `get_premarket(phase="postopen")`. En esta fase el scanner
  intradia ya funciona (sesion abierta), asi que aporta top movers.

En ambos casos se razona el setup de apertura con el LLM.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.agents.searchers.context import format_premarket_context
from src.agents.searchers.llm_base import MarketDataLLMSearcher
from src.llm.base import LLMClient
from src.llm.prompts import PromptLibrary
from src.schemas.enums import AgentSource, Exchange
from src.schemas.market import PremarketSnapshot

logger = logging.getLogger("agentic.searchers.premarket")


class PremarketSearcher(MarketDataLLMSearcher):
    def __init__(
        self,
        market_data=None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 5,
        markets: list[dict] | None = None,
        lead_minutes: int = 10,
        post_open_minutes: int = 10,
        top_movers: int = 20,
        gap_batch_size: int = 20,
        min_gap_pct: float = 1.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            source=AgentSource.PREMARKET,
            prompt_name="searchers/premarket",
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
        )
        #: Config de mercados (id, exchange, open_local, timezone) para la ventana de preapertura.
        self.markets = markets or []
        #: Minutos antes de la apertura en los que se activa la fase preopen de cada mercado.
        self.lead_minutes = lead_minutes
        #: Minutos despues de la apertura en los que se activa la fase postopen (gap real).
        self.post_open_minutes = post_open_minutes
        #: Numero de top movers a pedir al scanner por mercado en cada ventana.
        self.top_movers = top_movers
        #: Fallback: nº de tickers del watchlist a escanear por |gap| cuando el scanner
        #: intradia no da movers (en preapertura la sesion aun no ha abierto). Cada
        #: get_premarket consume 2 req historicas de IBKR, luego gap_batch_size*2 debe caber
        #: en el pacing (60/10min); se recorre el universo por lotes rotatorios en sucesivas
        #: ventanas.
        self.gap_batch_size = gap_batch_size
        #: Gap minimo en % (1.0 = 1%) para que un ticker del fallback sea candidato.
        self.min_gap_pct = min_gap_pct
        #: Indice de inicio del proximo lote rotatorio del fallback dentro del watchlist.
        self._offset = 0
        #: Proveedor de "ahora" (UTC-aware) inyectable para tests.
        self._now = now_provider or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        return [x for x in items if not (x in seen or seen.add(x))]

    def _next_batch(self, tickers: list[str]) -> list[str]:
        """Siguiente lote rotatorio sobre `tickers`, avanzando el offset.

        En ventanas sucesivas recorre todo el universo por bloques de `gap_batch_size`,
        acotando las peticiones a IBKR por ejecucion y respetando el pacing de historicos.
        """
        n = len(tickers)
        if n == 0:
            return []
        size = min(self.gap_batch_size, n)
        start = self._offset % n
        batch = [tickers[(start + i) % n] for i in range(size)]
        self._offset = (start + size) % n
        return batch

    def _market_tickers(self, market: dict) -> list[str]:
        """Tickers de la watchlist que pertenecen a `market` (BME=.MC, resto=US)."""
        is_bme = str(market.get("exchange", "")).upper() == "BME"
        return [t for t in self.watchlist if t.endswith(".MC") == is_bme]

    def _window_phase(self, market: dict) -> str | None:
        """Fase activa de `market` segun `ahora`, o None si esta fuera de ventana.

        - 'preopen'  si ahora esta en [apertura - lead_minutes, apertura).
        - 'postopen' si ahora esta en [apertura, apertura + post_open_minutes).
        """
        tzname = market.get("timezone")
        open_local = market.get("open_local")
        if not tzname or not open_local:
            return None
        try:
            tz = ZoneInfo(str(tzname))
            open_h, open_m = (int(x) for x in str(open_local).split(":"))
        except Exception as exc:  # noqa: BLE001 - config invalida -> mercado inactivo
            logger.warning("premarket: config de mercado invalida (%s): %s", market.get("id"), exc)
            return None
        now_local = self._now().astimezone(tz)
        open_dt = now_local.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        if open_dt - timedelta(minutes=self.lead_minutes) <= now_local < open_dt:
            return "preopen"
        if open_dt <= now_local < open_dt + timedelta(minutes=self.post_open_minutes):
            return "postopen"
        return None

    def _exchange_for_market(self, market: dict) -> Exchange:
        return Exchange.BME if str(market.get("exchange", "")).upper() == "BME" else Exchange.NYSE

    async def _open_positions(self, markets: list[dict]) -> list[str]:
        """Tickers con posicion abierta que pertenecen a alguno de `markets`.

        Las posiciones se gestionan siempre en la apertura, aparezcan o no en los movers.
        """
        market_set: set[str] = set()
        for m in markets:
            market_set.update(self._market_tickers(m))
        try:
            portfolio = await self.market_data.get_portfolio()
        except Exception as exc:  # noqa: BLE001 - sin cartera no aborta el resto
            logger.debug("premarket: no se pudo leer la cartera (%s).", exc)
            return []
        return [p.ticker for p in portfolio.positions if p.ticker in market_set]

    async def _scanner_movers(self, market: dict) -> list[str]:
        """Top movers del scanner IBKR filtrados al watchlist del mercado.

        NOTA: el scanner (TOP_PERC_GAIN/LOSE) rankea por variacion de la sesion regular,
        que en la ventana de preapertura aun no ha empezado; por eso suele venir vacio y el
        fallback de gap directo toma el relevo.
        """
        market_set = set(self._market_tickers(market))
        try:
            movers = await self.market_data.get_top_movers(
                exchange=self._exchange_for_market(market), count=self.top_movers
            )
        except Exception as exc:  # noqa: BLE001 - scanner no disponible -> fallback
            logger.warning("premarket: scanner de movers no disponible (%s).", exc)
            return []
        return [t for t in movers if t in market_set]

    async def _snapshots_for(
        self, tickers: list[str], *, phase: str
    ) -> list[PremarketSnapshot]:
        """Snapshot de cada ticker para la fase dada; ignora los que no tienen datos."""
        snapshots: list[PremarketSnapshot] = []
        for ticker in tickers:
            try:
                snapshots.append(await self.market_data.get_premarket(ticker, phase=phase))
            except Exception as exc:  # noqa: BLE001 - un ticker sin datos no aborta el resto
                logger.debug("premarket: sin datos para %s (%s).", ticker, exc)
        return snapshots

    @staticmethod
    def _dedupe_snapshots(snapshots: list[PremarketSnapshot]) -> list[PremarketSnapshot]:
        seen: set[str] = set()
        return [s for s in snapshots if not (s.ticker in seen or seen.add(s.ticker))]

    async def _market_snapshots(self, market: dict, phase: str) -> list[PremarketSnapshot]:
        """Snapshots de `market` en la fase dada: posiciones + movers, o fallback de gap.

        - Posiciones abiertas: siempre.
        - Movers del scanner: en `postopen` la sesion ya esta abierta y el scanner funciona;
          en `preopen` suele venir vacio -> se usa el fallback de gap directo por lote.
        """
        positions = self._dedupe(await self._open_positions([market]))
        movers = self._dedupe(await self._scanner_movers(market))
        if movers:
            return await self._snapshots_for(self._dedupe(positions + movers), phase=phase)

        # Fallback: sin movers del scanner -> gap directo sobre un lote rotatorio del watchlist.
        market_tickers = self._market_tickers(market)
        batch = self._next_batch(market_tickers)
        logger.info(
            "premarket: %s sin movers de scanner (%s); fallback gap sobre lote de %d/%d tickers.",
            market.get("id"), phase, len(batch), len(market_tickers),
        )
        snaps = await self._snapshots_for(self._dedupe(positions + batch), phase=phase)
        pos_set = set(positions)
        pos_snaps = [s for s in snaps if s.ticker in pos_set]  # posiciones: siempre
        gap_snaps = sorted(
            (
                s for s in snaps
                if s.ticker not in pos_set and abs(s.gap_pct) * 100.0 >= self.min_gap_pct
            ),
            key=lambda s: abs(s.gap_pct),
            reverse=True,
        )[: self.top_movers]
        return pos_snaps + gap_snaps

    async def build_context(self) -> str:
        if self.market_data is None or not self.watchlist:
            return ""
        if not self.markets:  # sin horario configurado -> sin gating (compat/tests)
            snapshots = await self._snapshots_for(list(self.watchlist), phase="preopen")
            return format_premarket_context(snapshots)

        active = [(m, self._window_phase(m)) for m in self.markets]
        active = [(m, phase) for m, phase in active if phase is not None]
        if not active:
            logger.debug(
                "premarket: fuera de ventana (preopen lead=%dmin / postopen post=%dmin). "
                "Mercados configurados: %s.",
                self.lead_minutes, self.post_open_minutes,
                [m.get("id") for m in self.markets],
            )
            return ""  # fuera de toda ventana de apertura -> no fetch

        snapshots: list[PremarketSnapshot] = []
        for market, phase in active:
            snapshots.extend(await self._market_snapshots(market, phase))
        snapshots = self._dedupe_snapshots(snapshots)

        phases = [(m.get("id"), phase) for m, phase in active]
        if not snapshots:
            logger.warning(
                "premarket: ventana activa %s pero sin snapshots relevantes. "
                "Revisa conexion IBKR y datos de mercado.",
                phases,
            )
            return ""
        logger.info("premarket: %d snapshots para el LLM (fases: %s).", len(snapshots), phases)
        return format_premarket_context(snapshots)
