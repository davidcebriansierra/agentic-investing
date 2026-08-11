"""Agente buscador de Analisis Tecnico (spec v2.0, seccion 3.1).

Obtiene OHLCV intradia de la watchlist via `MarketDataClient.get_ohlcv`, calcula
indicadores (RSI, MACD, SMA/EMA, ATR, maximos/minimos) y razona setups INTRADIA con el LLM.
"""
from __future__ import annotations

import logging

from src.agents.searchers.context import format_indicators_context
from src.agents.searchers.indicators import compute_indicators
from src.agents.searchers.llm_base import MarketDataLLMSearcher
from src.llm.base import LLMClient
from src.llm.prompts import PromptLibrary
from src.schemas.enums import AgentSource

logger = logging.getLogger("agentic.searchers.technical")


class TechnicalSearcher(MarketDataLLMSearcher):
    def __init__(
        self,
        market_data=None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 10,
        ohlcv_bars: int = 60,
        ohlcv_interval: str = "5 mins",
        batch_size: int = 50,
    ) -> None:
        super().__init__(
            source=AgentSource.TECHNICAL,
            prompt_name="searchers/technical",
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
        )
        self.ohlcv_bars = ohlcv_bars
        self.ohlcv_interval = ohlcv_interval
        #: Tickers analizados por ciclo (<=60 para respetar el pacing de IBKR: 60 req/10min).
        self.batch_size = batch_size
        #: Indice de inicio del proximo lote rotatorio dentro de la watchlist.
        self._offset = 0

    def _next_batch(self) -> list[str]:
        """Devuelve el siguiente lote rotatorio de la watchlist y avanza el offset.

        Limita el fetch a IBKR a `batch_size` peticiones/ciclo (el pacing de datos
        historicos de IBKR es de 60 en 10 min) y recorre todo el universo por bloques a
        lo largo de sucesivos ciclos, manteniendo la senal intradia razonablemente fresca.
        """
        n = len(self.watchlist)
        size = min(self.batch_size, n)
        start = self._offset % n
        batch = [self.watchlist[(start + i) % n] for i in range(size)]
        self._offset = (start + size) % n
        return batch

    async def build_context(self) -> str:
        if self.market_data is None or not self.watchlist:
            return ""
        batch = self._next_batch()
        logger.info(
            "technical: lote rotatorio de %d tickers (%s..%s) sobre %d en total.",
            len(batch), batch[0], batch[-1], len(self.watchlist),
        )
        indicators_by_ticker: dict[str, dict[str, float]] = {}
        for ticker in batch:
            try:
                bars = await self.market_data.get_ohlcv(
                    ticker, bars=self.ohlcv_bars, interval=self.ohlcv_interval
                )
            except Exception as exc:  # noqa: BLE001 - un ticker sin datos no aborta el resto
                logger.debug("technical: sin OHLCV para %s (%s).", ticker, exc)
                continue
            if not bars:
                continue
            indicators_by_ticker[ticker] = compute_indicators(bars)
        return format_indicators_context(indicators_by_ticker)
