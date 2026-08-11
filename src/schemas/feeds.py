"""Modelos de items de noticias y redes sociales (entrada de los buscadores)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NewsItem(BaseModel):
    """Noticia normalizada procedente de NewsAPI / Marketaux / Finnhub / Alpha Vantage."""

    item_id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    headline: str
    summary: str = ""
    url: str | None = None
    tickers: list[str] = Field(default_factory=list)
    sentiment: float | None = None  # [-1, 1] si la fuente lo aporta
    published_utc: datetime = Field(default_factory=_utcnow)


class SocialPost(BaseModel):
    """Mensaje de red social (Reddit / StockTwits) normalizado."""

    post_id: str = Field(default_factory=lambda: str(uuid4()))
    platform: str  # reddit | stocktwits
    channel: str  # subreddit o canal
    author: str | None = None
    text: str = ""
    tickers: list[str] = Field(default_factory=list)
    score: int = 0  # upvotes / likes
    sentiment: float | None = None
    created_utc: datetime = Field(default_factory=_utcnow)
