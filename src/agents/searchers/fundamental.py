"""Agente buscador de Analisis Fundamental (spec v2.0, seccion 3.1).

Obtiene fundamentales reales (ratios, crecimiento, margenes) de la watchlist via el
`FundamentalsClient` inyectado (Finnhub) y razona tesis MULTIDAY con el LLM. Sin cliente
de fundamentales (o sin datos) no fabrica oportunidades.
"""
from __future__ import annotations

import logging

from src.agents.searchers.context import format_fundamentals_context
from src.agents.searchers.llm_base import MarketDataLLMSearcher, _log_entry
from src.connectors.fundamentals import FundamentalsClient
from src.llm.base import LLMClient
from src.llm.prompts import PromptLibrary
from src.schemas.enums import AgentSource

logger = logging.getLogger("agentic.searchers.fundamental")


class FundamentalSearcher(MarketDataLLMSearcher):
    def __init__(
        self,
        market_data=None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 1440,
        fundamentals_client: FundamentalsClient | None = None,
        batch_size: int = 40,
    ) -> None:
        super().__init__(
            source=AgentSource.FUNDAMENTAL,
            prompt_name="searchers/fundamental",
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
        )
        self.fundamentals_client = fundamentals_client
        #: Tamano del lote rotatorio analizado por ejecucion (cubre el universo por bloques).
        self.batch_size = batch_size
        #: Indice de inicio del proximo lote rotatorio dentro de la watchlist.
        self._offset = 0

    def _next_batch(self) -> list[str]:
        """Devuelve el siguiente lote rotatorio de la watchlist y avanza el offset.

        En ejecuciones sucesivas recorre TODO el universo por bloques de `batch_size`,
        de modo que en varios dias todos los tickers reciben analisis fundamental y solo
        se hace fetch del lote (no de los 506), recortando coste de API y latencia.
        """
        n = len(self.watchlist)
        size = min(self.batch_size, n)
        start = self._offset % n
        batch = [self.watchlist[(start + i) % n] for i in range(size)]
        self._offset = (start + size) % n
        return batch

    async def build_context(self) -> str:
        if self.fundamentals_client is None or not self.watchlist:
            return ""
        batch = self._next_batch()
        logger.info(
            "fundamental: lote rotatorio de %d tickers (%s..%s) sobre %d en total.",
            len(batch), batch[0], batch[-1], len(self.watchlist),
        )
        _log_entry("fundamental", {
            "event": "api_request",
            "connector": type(self.fundamentals_client).__name__,
            "tickers": batch,
        })
        try:
            items = await self.fundamentals_client.fetch(batch)
        except Exception as exc:  # noqa: BLE001 - fallo del proveedor -> sin contexto
            logger.warning("fundamental: fallo al obtener fundamentales (%s).", exc)
            _log_entry("fundamental", {"event": "api_error", "error": str(exc)})
            return ""
        _log_entry("fundamental", {
            "event": "api_response",
            "connector": type(self.fundamentals_client).__name__,
            "items_received": len(items),
            "tickers_ok": [i.ticker for i in items],
        })
        # Enviar todos los items del lote al LLM (el lote ya limita el tamano del contexto).
        return format_fundamentals_context(items, limit=None)
