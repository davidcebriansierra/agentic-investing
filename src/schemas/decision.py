"""Contrato Decision (spec v2.0, seccion 5.3). Salida del agente decisor."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from src.schemas.enums import DecisionReason, DecisionType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Decision(BaseModel):
    """Decision final agregada y calibrada por el decisor."""

    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    final_score: float = Field(ge=0.0, le=1.0)
    expectancy_pct: float
    risk_reward_ratio: float = Field(ge=0)
    evaluator_breakdown: dict[str, float] = Field(default_factory=dict)
    consensus_count: int = 0
    decision: DecisionType
    reason: DecisionReason
    final_order_spec: dict | None = None
    timestamp_utc: datetime = Field(default_factory=_utcnow)
