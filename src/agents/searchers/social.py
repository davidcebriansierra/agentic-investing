"""Agente buscador de Redes Sociales (spec v2.0, seccion 3.1).

Fuentes: Reddit (r/wallstreetbets, r/investing, r/spainfn), StockTwits.

Consume los posts via el `SocialClient` inyectado (real o mock), los formatea como
contexto y delega en el nucleo `LLMSearcher` para detectar cambios de sentimiento
accionables. Sin LLM configurado (o sin posts) no fabrica oportunidades.
"""
from __future__ import annotations

import logging

from src.agents.searchers.context import format_social_context
from src.agents.searchers.llm_base import LLMSearcher, _log_entry
from src.connectors.social_apis import MockSocialClient, SocialClient
from src.llm.base import LLMClient
from src.llm.prompts import PromptLibrary
from src.schemas.enums import AgentSource
from src.schemas.feeds import SocialPost
from src.schemas.opportunity import Opportunity

logger = logging.getLogger("agentic.searchers.social")


class SocialSearcher(LLMSearcher):
    def __init__(
        self,
        client: SocialClient | None = None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 30,
        context_max_posts: int | None = None,
    ) -> None:
        super().__init__(
            source=AgentSource.SOCIAL,
            prompt_name="searchers/social",
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
        )
        self.client: SocialClient = client or MockSocialClient()
        self.watchlist = watchlist
        self.context_max_posts = context_max_posts
        self.last_posts: list[SocialPost] = []

    async def search(self) -> list[Opportunity]:
        _log_entry("social", {
            "event": "api_request",
            "connector": type(self.client).__name__,
            "tickers": self.watchlist,
            "since_minutes": self.interval_minutes,
        })
        self.last_posts = await self.client.fetch(
            tickers=self.watchlist, since_minutes=self.interval_minutes
        )
        _log_entry("social", {
            "event": "api_response",
            "connector": type(self.client).__name__,
            "items_received": len(self.last_posts),
            "sample": [p.text[:120] for p in self.last_posts[:5]],
        })
        logger.debug("SocialSearcher: %d posts recuperados.", len(self.last_posts))
        return await self.reason(
            format_social_context(self.last_posts, limit=self.context_max_posts)
        )
