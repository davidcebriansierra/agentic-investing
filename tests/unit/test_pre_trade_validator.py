"""Tests del Pre-Trade Validator."""
from __future__ import annotations

from src.governance.pre_trade_validator import MarketContext, PreTradeValidator
from src.schemas.enums import OrderAction
from src.schemas.order import Order, OrderLeg


def _order(entry=100.0, tp=102.0, sl=99.0, qty=100) -> Order:
    return Order(
        ticker="IBE.MC",
        action=OrderAction.BUY,
        quantity=qty,
        entry=OrderLeg(type="LMT", price=entry),
        take_profit=OrderLeg(type="LMT", price=tp),
        stop_loss=OrderLeg(type="STP", price=sl),
        account="DU123",
    )


def _ctx(price=100.0) -> MarketContext:
    return MarketContext(
        current_price=price,
        is_tradable_today=True,
        market_open=True,
        available_capital=1_000_000,
        account_equity=1_000_000,
    )


def test_valid_order_passes():
    v = PreTradeValidator()
    result = v.validate(_order(), _ctx(), idempotency_key="opp-1")
    assert result.valid, result.violations


def test_market_closed_rejected():
    v = PreTradeValidator()
    ctx = _ctx()
    ctx.market_open = False
    result = v.validate(_order(), ctx, idempotency_key="opp-1")
    assert not result.valid
    assert "mercado_cerrado" in result.violations


def test_price_out_of_range_rejected():
    v = PreTradeValidator()
    result = v.validate(_order(entry=110.0, tp=112.2, sl=108.9), _ctx(price=100.0), idempotency_key="opp-1")
    assert not result.valid
    assert any("precio_fuera_de_rango" in x for x in result.violations)


def test_low_risk_reward_rejected():
    v = PreTradeValidator()
    # R/R = 1.0 (TP 1%, SL 1%)
    result = v.validate(_order(entry=100.0, tp=101.0, sl=99.0), _ctx(), idempotency_key="opp-1")
    assert not result.valid
    assert any("risk_reward" in x for x in result.violations)


def test_idempotency_blocks_duplicate():
    v = PreTradeValidator()
    first = v.validate(_order(), _ctx(), idempotency_key="opp-1")
    assert first.valid
    second = v.validate(_order(), _ctx(), idempotency_key="opp-1")
    assert not second.valid
    assert "orden_duplicada" in second.violations
