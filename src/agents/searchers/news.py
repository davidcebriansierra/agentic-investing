"""Agente buscador de Noticias (spec v2.0, seccion 3.1).

Event-driven (websocket) + polling de 30 min. Fuentes: NewsAPI, Marketaux,
Finnhub, Alpha Vantage.

Consume el feed de noticias via el `NewsClient` inyectado (real o mock), lo formatea
como contexto y delega en el nucleo `LLMSearcher` para extraer setups accionables. Sin
LLM configurado (o sin noticias) no fabrica oportunidades, respetando "no-operar por
defecto".
"""
from __future__ import annotations

import logging

from src.agents.searchers.context import format_news_context
from src.agents.searchers.llm_base import LLMSearcher, _log_entry
from src.connectors.news_apis import MockNewsClient, NewsClient
from src.llm.base import LLMClient
from src.llm.prompts import PromptLibrary
from src.schemas.enums import AgentSource
from src.schemas.feeds import NewsItem
from src.schemas.opportunity import Opportunity

logger = logging.getLogger("agentic.searchers.news")


class NewsSearcher(LLMSearcher):
    def __init__(
        self,
        client: NewsClient | None = None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 30,
    ) -> None:
        super().__init__(
            source=AgentSource.NEWS,
            prompt_name="searchers/news",
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
        )
        self.client: NewsClient = client or MockNewsClient()
        self.watchlist = watchlist
        self.last_items: list[NewsItem] = []

    async def search(self) -> list[Opportunity]:
        _log_entry("news", {
            "event": "api_request",
            "connector": type(self.client).__name__,
            "tickers": self.watchlist,
            "since_minutes": self.interval_minutes,
        })
        self.last_items = await self.client.fetch(
            tickers=self.watchlist, since_minutes=self.interval_minutes
        )
        _log_entry("news", {
            "event": "api_response",
            "connector": type(self.client).__name__,
            "items_received": len(self.last_items),
            "headlines": [i.headline[:120] for i in self.last_items[:10]],
        })
        logger.debug("NewsSearcher: %d noticias recuperadas.", len(self.last_items))
        market_context = format_news_context(self.last_items)
        watchlist_str = ", ".join(str(t) for t in (self.watchlist or []))
        return await self.reason(market_context, extra={"watchlist": watchlist_str})
