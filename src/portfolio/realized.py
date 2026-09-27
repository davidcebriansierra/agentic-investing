"""Reconciliacion del P&L realizado a partir de fills del broker (FIFO).

El sistema no observa directamente los cierres de posicion: las patas SL/TP de las
bracket orders las ejecuta IBKR y solo aparecen como fills en la cuenta. Este modulo
empareja compras y ventas por ticker en orden FIFO para derivar el P&L realizado,
incluyendo comisiones reportadas por el broker.

Solo cierra P&L los fills con side opuesta al inventario acumulado (cola de lotes);
una venta sin compras previas abre posicion corta (el sistema puede operar shorts).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from src.schemas.order import Fill


@dataclass
class RealizedSummary:
    """P&L realizado de un conjunto de fills (p. ej. los del dia)."""

    realized_pnl: float = 0.0          # P&L neto de comisiones
    gross_pnl: float = 0.0
    commissions: float = 0.0
    closed_trades: int = 0             # lotes cerrados completamente
    unmatched_sells: int = 0           # ventas sin lote abierto previo (shorts)


def _fill_day(fill: Fill) -> date | None:
    ts = fill.timestamp_utc
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.date()


def realized_pnl(
    fills: Iterable[Fill],
    on_date: date | None = None,
) -> RealizedSummary:
    """Empareja fills FIFO por ticker y devuelve el P&L realizado.

    `on_date` filtra los fills por dia (UTC); `None` procesa todo el historial.
    Las comisiones de TODOS los fills del periodo se restan del P&L (una compra que
    sigue abierta ya pago su comision en el dia).
    """
    lots: dict[str, deque[Fill]] = {}
    summary = RealizedSummary()

    ordered = sorted(
        (f for f in fills if on_date is None or _fill_day(f) == on_date),
        key=lambda f: f.timestamp_utc or datetime.min.replace(tzinfo=timezone.utc),
    )
    for fill in ordered:
        summary.commissions += fill.commission
        queue = lots.setdefault(fill.ticker, deque())
        side = fill.side.upper()
        remaining = fill.quantity

        while remaining > 0 and queue and queue[0].side.upper() != side:
            open_lot = queue[0]
            qty = min(remaining, open_lot.quantity)
            # Long cerrado por venta: (vende - compra) * qty.
            # Short cerrado por compra: (vende - compra) * qty con signo invertido.
            if open_lot.side.upper() == "BOT":
                summary.gross_pnl += (fill.price - open_lot.price) * qty
            else:
                summary.gross_pnl += (open_lot.price - fill.price) * qty
            remaining -= qty
            if qty >= open_lot.quantity:
                queue.popleft()
                summary.closed_trades += 1
            else:
                open_lot = open_lot.model_copy(update={"quantity": open_lot.quantity - qty})
                queue[0] = open_lot

        if remaining > 0:
            if not queue or queue[0].side.upper() == side:
                queue.append(fill.model_copy(update={"quantity": remaining}))
            else:
                summary.unmatched_sells += 1  # defensivo: no deberia ocurrir

    summary.realized_pnl = round(summary.gross_pnl - summary.commissions, 2)
    summary.gross_pnl = round(summary.gross_pnl, 2)
    summary.commissions = round(summary.commissions, 2)
    return summary
