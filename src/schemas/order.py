"""Contratos Order y ExecutionResult (spec v2.0, secciones 5.4 y 5.5)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from src.schemas.enums import (
    ExecutionStatus,
    OrderAction,
    OrderType,
    TimeInForce,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderLeg(BaseModel):
    """Pata de una bracket order (entrada, take-profit o stop-loss)."""

    type: str  # LMT | STP | MKT
    price: float = Field(gt=0)


class Order(BaseModel):
    """Orden a enviar a IBKR (bracket order atomica)."""

    order_id_internal: str = Field(default_factory=lambda: str(uuid4()))
    ticker: str
    action: OrderAction
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.BRACKET
    entry: OrderLeg
    take_profit: OrderLeg
    stop_loss: OrderLeg
    tif: TimeInForce = TimeInForce.DAY
    account: str


class ExecutionResult(BaseModel):
    """Resultado de la ejecucion de una orden en IBKR."""

    order_id_internal: str
    ibkr_order_id: int | None = None
    status: ExecutionStatus
    fill_price: float | None = None
    fill_quantity: int | None = None
    commission: float | None = None
    timestamp_utc: datetime = Field(default_factory=_utcnow)
    raw_ibkr_response: str | None = None


class Fill(BaseModel):
    """Ejecucion (fill) reportada por el broker.

    A diferencia de `ExecutionResult` (resultado de UNA orden enviada por el sistema),
    un `Fill` es el registro de ejecucion que IBKR reporta por cuenta: sirve para
    reconciliar el P&L realizado aunque el fill corresponda a una orden que el sistema
    no conoce (p. ej. un stop-loss disparado en otra sesion).
    """

    ticker: str
    side: str                    # "BOT" (compra) | "SLD" (venta)
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    commission: float = 0.0
    timestamp_utc: datetime = Field(default_factory=_utcnow)
    order_id_internal: str | None = None  # si el fill casa con una orden del sistema
