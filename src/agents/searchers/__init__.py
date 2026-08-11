"""Agentes buscadores de oportunidades (spec v2.0, seccion 3.1)."""
from src.agents.searchers.base import BaseSearcher
from src.agents.searchers.cross_market import CrossMarketSearcher
from src.agents.searchers.fundamental import FundamentalSearcher
from src.agents.searchers.llm_base import LLMSearcher, MarketDataLLMSearcher
from src.agents.searchers.news import NewsSearcher
from src.agents.searchers.premarket import PremarketSearcher
from src.agents.searchers.social import SocialSearcher
from src.agents.searchers.technical import TechnicalSearcher

__all__ = [
    "BaseSearcher",
    "LLMSearcher",
    "MarketDataLLMSearcher",
    "CrossMarketSearcher",
    "FundamentalSearcher",
    "NewsSearcher",
    "PremarketSearcher",
    "SocialSearcher",
    "TechnicalSearcher",
]
