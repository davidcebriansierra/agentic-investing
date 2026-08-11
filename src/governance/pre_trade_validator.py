"""Pre-Trade Validator deterministico (spec v2.0, seccion 7.2).

Se ejecuta antes de TODA orden. Devuelve la lista de violaciones; si esta vacia la
orden es valida. Incluye control de idempotencia por opportunity_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.schemas.enums import Direction, OrderAction
from src.schemas.order import Order
from src.utils.config import get_risk_limits


@dataclass
class ValidationResult:
    valid: bool
    violations: list[str] = field(default_factory=list)


@dataclass
class MarketContext:
    """Contexto de mercado necesario para validar la orden."""

    current_price: float
    is_tradable_today: bool
    market_open: bool
    available_capital: float
    account_equity: float


class PreTradeValidator:
    def __init__(self, limits: dict | None = None) -> None:
        self.limits = limits or get_risk_limits()
        # Registro de idempotencia: opportunity_id ya enviados.
        self._sent_keys: set[str] = set()

    def reset_idempotency(self) -> None:
        self._sent_keys.clear()

    def validate(
        self,
        order: Order,
        ctx: MarketContext,
        idempotency_key: str,
    ) -> ValidationResult:
        v: list[str] = []
        sl_cfg = self.limits.get("stop_loss", {})
        rr_cfg = self.limits.get("risk_reward", {})
        sizing = self.limits.get("position_sizing", {})
        exec_cfg = self.limits.get("execution", {})
        max_price_dev = exec_cfg.get("max_price_deviation_pct", 2.0) / 100

        entry = order.entry.price
        tp = order.take_profit.price
        sl = order.stop_loss.price

        # 1. Ticker operable hoy.
        if not ctx.is_tradable_today:
            v.append("ticker_no_operable_hoy")

        # 6. Mercado abierto.
        if not ctx.market_open:
            v.append("mercado_cerrado")

        # 2. Precio sugerido dentro de +-max_price_deviation_pct del precio actual.
        if ctx.current_price > 0:
            deviation = abs(entry - ctx.current_price) / ctx.current_price
            if deviation > max_price_dev:
                v.append(f"precio_fuera_de_rango {deviation:.2%} > {max_price_dev:.0%}")

        # Geometria coherente con la direccion (LONG=BUY, SHORT=SELL).
        is_long = order.action == OrderAction.BUY
        sl_distance = abs(entry - sl) / entry if entry else 0.0
        tp_distance = abs(tp - entry) / entry if entry else 0.0

        if is_long and not (sl < entry < tp):
            v.append("geometria_invalida_long")
        if not is_long and not (tp < entry < sl):
            v.append("geometria_invalida_short")

        # 3. Stop-loss presente y a distancia entre min y max.
        min_sl = sl_cfg.get("min_distance_pct", 0.5) / 100
        max_sl = sl_cfg.get("max_distance_pct", 5.0) / 100
        if not (min_sl <= sl_distance <= max_sl):
            v.append(
                f"stop_loss_distancia {sl_distance:.2%} fuera [{min_sl:.2%},{max_sl:.2%}]"
            )

        # 4. Take-profit presente y R/R >= min.
        min_rr = rr_cfg.get("min_ratio", 2.0)
        rr = tp_distance / sl_distance if sl_distance > 0 else 0.0
        if rr < min_rr:
            v.append(f"risk_reward {rr:.2f} < {min_rr}")

        # 5. Tamano respeta limites de la cuenta.
        max_pos = sizing.get("max_pct", 10) / 100
        notional = order.quantity * entry
        if ctx.account_equity > 0:
            pos_pct = notional / ctx.account_equity
            if pos_pct > max_pos:
                v.append(f"tamano {pos_pct:.2%} > {max_pos:.0%}")

        # 8. Capital disponible suficiente.
        if notional > ctx.available_capital:
            v.append("capital_insuficiente")

        # 7. Idempotencia.
        if idempotency_key in self._sent_keys:
            v.append("orden_duplicada")

        valid = len(v) == 0
        if valid:
            self._sent_keys.add(idempotency_key)
        return ValidationResult(valid=valid, violations=v)
