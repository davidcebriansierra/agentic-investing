"""Esquema MonitoringEvent (spec v2.0, secciones 3.8 y 7.3)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MonitoringEventType(str, Enum):
    PNL_REVIEW = "pnl_review"
    MISSING_STOP_LOSS = "missing_stop_loss"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    ANOMALY = "anomaly"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class MonitoringEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: MonitoringEventType
    severity: Severity = Severity.INFO
    ticker: str | None = None
    message: str = ""
    data: dict = Field(default_factory=dict)
    timestamp_utc: datetime = Field(default_factory=_utcnow)
