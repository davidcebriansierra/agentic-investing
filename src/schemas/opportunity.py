"""Contrato Opportunity (spec v2.0, seccion 5.1).

Salida de los agentes buscadores y entrada del aggregator.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.enums import AgentSource, Direction, Exchange, HoldingPeriod


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Opportunity(BaseModel):
    """Oportunidad de inversion detectada por un buscador."""

    model_config = ConfigDict(use_enum_values=False)

    opportunity_id: str = Field(default_factory=lambda: str(uuid4()))
    agent_source: AgentSource
    timestamp_utc: datetime = Field(default_factory=_utcnow)
    ticker: str
    exchange: Exchange
    direction: Direction
    entry_price: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    position_size_pct: float = Field(gt=0, le=1.0)
    expected_holding: HoldingPeriod = HoldingPeriod.INTRADAY
    estimated_win_probability: float = Field(ge=0.0, le=1.0)
    risk_reward_ratio: float = Field(gt=0)
    justification: str = Field(max_length=500)

    supporting_data: dict = Field(default_factory=dict)
    raw_llm_response: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None

    # Campos derivados para convergencia multi-fuente (rellenados por el aggregator).
    sources_count: int = 1
    confidence_bonus: float = 0.0

    @model_validator(mode="after")
    def _validate_bracket_geometry(self) -> "Opportunity":
        """Coherencia de geometria entre entry / TP / SL segun la direccion."""
        if self.direction == Direction.LONG:
            if not (self.stop_loss < self.entry_price < self.take_profit):
                raise ValueError(
                    "LONG requiere stop_loss < entry_price < take_profit"
                )
        else:  # SHORT
            if not (self.take_profit < self.entry_price < self.stop_loss):
                raise ValueError(
                    "SHORT requiere take_profit < entry_price < stop_loss"
                )
        return self

    @property
    def take_profit_pct(self) -> float:
        """Distancia al take-profit en fraccion del precio de entrada (positiva)."""
        return abs(self.take_profit - self.entry_price) / self.entry_price

    @property
    def stop_loss_pct(self) -> float:
        """Distancia al stop-loss en fraccion del precio de entrada (positiva)."""
        return abs(self.entry_price - self.stop_loss) / self.entry_price

    @property
    def computed_risk_reward(self) -> float:
        """R/R recalculado a partir de la geometria (TP_pct / SL_pct)."""
        sl = self.stop_loss_pct
        return self.take_profit_pct / sl if sl > 0 else 0.0

    def dedup_key(self) -> tuple[str, str, str]:
        """Clave de deduplicacion: (ticker, direccion, fecha)."""
        return (
            self.ticker,
            self.direction.value,
            self.timestamp_utc.date().isoformat(),
        )
