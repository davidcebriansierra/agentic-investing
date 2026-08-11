"""Contrato Evaluation (spec v2.0, seccion 5.2). Salida de cada agente evaluador."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from src.schemas.enums import EvaluatorType, Recommendation


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Evaluation(BaseModel):
    """Puntuacion de una oportunidad por un evaluador con perfil concreto."""

    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    evaluator: EvaluatorType
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    recommendation: Recommendation
    adjusted_take_profit: float | None = Field(default=None, gt=0)
    adjusted_stop_loss: float | None = Field(default=None, gt=0)
    justification: str = ""
    timestamp_utc: datetime = Field(default_factory=_utcnow)
    model_version: str | None = None
    prompt_version: str | None = None
    # Campos de la oportunidad evaluada (echo para trazabilidad completa).
    ticker: str | None = None
    direction: str | None = None
    entry_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    risk_reward: float | None = None
    win_probability: float | None = None
    position_size_pct: float | None = None
