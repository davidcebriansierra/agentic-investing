"""Modelos de datos de mercado (quotes, OHLCV, pre-market, fundamentales)."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from src.schemas.enums import Exchange


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Quote(BaseModel):
    """Snapshot de precio de un instrumento."""

    ticker: str
    exchange: Exchange
    last: float = Field(gt=0)
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    avg_volume_20d: float | None = None
    timestamp_utc: datetime = Field(default_factory=_utcnow)


class OHLCVBar(BaseModel):
    """Vela OHLCV para una barra temporal (intradia o diaria)."""

    timestamp_utc: datetime = Field(default_factory=_utcnow)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = 0.0


class PremarketSnapshot(BaseModel):
    """Estado pre-apertura de un instrumento: precio y gap respecto al cierre previo."""

    ticker: str
    exchange: Exchange
    previous_close: float = Field(gt=0)
    premarket_price: float = Field(gt=0)
    premarket_volume: float = 0.0
    timestamp_utc: datetime = Field(default_factory=_utcnow)

    @property
    def gap_pct(self) -> float:
        """Variacion porcentual pre-market respecto al cierre previo (0.02 = +2%)."""
        return (self.premarket_price - self.previous_close) / self.previous_close


class Fundamentals(BaseModel):
    """Metricas fundamentales normalizadas (fuente: Finnhub u otra)."""

    ticker: str
    company_name: str | None = None
    market_cap: float | None = None  # en millones
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    ps_ratio: float | None = None
    pb_ratio: float | None = None
    dividend_yield: float | None = None  # en %
    roe: float | None = None  # en %
    net_margin: float | None = None  # en %
    revenue_growth_yoy: float | None = None  # en %
    eps_growth_yoy: float | None = None  # en %
    debt_to_equity: float | None = None
    beta: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    current_price: float | None = None
    sector: str | None = None
