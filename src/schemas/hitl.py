"""Modelos de aprobacion humana (HITL) – Telegram bot (spec v2.0, seccion 3.6)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    PAUSE_1H = "pause_1h"
    TIMEOUT = "timeout"  # sin respuesta dentro del TTL -> no operar


class ApprovalRequest(BaseModel):
    """Mensaje estructurado enviado al canal de aprobacion."""

    request_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    ticker: str
    summary: str
    final_score: float
    expectancy_pct: float
    risk_reward_ratio: float
    entry_price: float
    stop_loss: float
    take_profit: float
    estimated_win_probability: float
    position_size_pct: float
    evaluator_breakdown: dict[str, float] = Field(default_factory=dict)
    ttl_seconds: int = 300
    created_utc: datetime = Field(default_factory=_utcnow)


class EditedPrices(BaseModel):
    """Precios modificados por el operador en el flujo HITL."""

    entry_price: float
    stop_loss: float
    take_profit: float


class ApprovalResponse(BaseModel):
    """Respuesta del aprobador humano."""

    request_id: str
    opportunity_id: str
    decision: ApprovalDecision
    responder: str | None = None
    responded_utc: datetime = Field(default_factory=_utcnow)
    edited_prices: EditedPrices | None = None

    @property
    def approved(self) -> bool:
        return self.decision == ApprovalDecision.APPROVE
