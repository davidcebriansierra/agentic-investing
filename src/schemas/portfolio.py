"""Modelos de portfolio y posiciones (soporte para Risk Filter y Monitor)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.enums import Direction


class Position(BaseModel):
    """Posicion abierta en cartera."""

    ticker: str
    sector: str | None = None
    direction: Direction
    quantity: int
    avg_price: float = Field(gt=0)
    market_price: float = Field(gt=0)
    stop_loss: float | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        sign = 1 if self.direction == Direction.LONG else -1
        return sign * (self.market_price - self.avg_price) * self.quantity


class Portfolio(BaseModel):
    """Estado de cartera usado por los controles deterministicos."""

    total_equity: float = Field(gt=0)
    cash: float = Field(ge=0)
    positions: list[Position] = Field(default_factory=list)
    # Matriz de correlacion opcional: {(ticker_a, ticker_b): rho}
    correlations: dict[str, float] = Field(default_factory=dict)
    # Volumen medio 20d por ticker (para chequeo de liquidez).
    avg_volume_20d: dict[str, float] = Field(default_factory=dict)

    def exposure_pct(self, ticker: str) -> float:
        value = sum(p.market_value for p in self.positions if p.ticker == ticker)
        return value / self.total_equity if self.total_equity else 0.0

    def sector_exposure_pct(self, sector: str) -> float:
        value = sum(
            p.market_value for p in self.positions if p.sector == sector
        )
        return value / self.total_equity if self.total_equity else 0.0

    def total_at_risk_pct(self) -> float:
        """Capital total en riesgo = suma de distancias a stop-loss de cada posicion."""
        at_risk = 0.0
        for p in self.positions:
            if p.stop_loss is None:
                at_risk += p.market_value
            else:
                at_risk += abs(p.market_price - p.stop_loss) * p.quantity
        return at_risk / self.total_equity if self.total_equity else 0.0
