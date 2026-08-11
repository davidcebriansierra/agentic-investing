"""Tests del agente Ejecutor."""
from __future__ import annotations

import pytest

from src.agents.executor import Executor, ExecutionRejected
from src.connectors.ibkr_write import MockBrokerClient
from src.governance.kill_switch import KillSwitch
from src.governance.pre_trade_validator import MarketContext
from src.schemas.decision import Decision
from src.schemas.enums import DecisionReason, DecisionType, ExecutionStatus
from tests.conftest import make_opportunity


def _decision(opp) -> Decision:
    return Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )


def _ctx(price=100.0) -> MarketContext:
    return MarketContext(
        current_price=price,
        is_tradable_today=True,
        market_open=True,
        available_capital=1_000_000,
        account_equity=1_000_000,
    )


async def test_executor_places_order_on_valid_decision():
    broker = MockBrokerClient()
    executor = Executor(broker=broker, account="DU123")
    opp = make_opportunity(entry=100.0, tp=102.0, sl=99.0, size=0.03)
    result = await executor.execute(_decision(opp), opp, _ctx())
    assert not isinstance(result, ExecutionRejected)
    assert result.status == ExecutionStatus.FILLED
    assert len(broker.placed_orders) == 1
    # quantity = floor(1_000_000 * 0.03 / 100) = 300
    assert broker.placed_orders[0].quantity == 300


async def test_executor_blocks_when_kill_switch_active():
    broker = MockBrokerClient()
    ks = KillSwitch()
    ks.activate("test")
    executor = Executor(broker=broker, account="DU123", kill_switch=ks)
    opp = make_opportunity(entry=100.0, tp=102.0, sl=99.0)
    result = await executor.execute(_decision(opp), opp, _ctx())
    assert isinstance(result, ExecutionRejected)
    assert result.reason == "kill_switch_activo"
    assert broker.placed_orders == []


async def test_executor_rejects_on_pre_trade_violation():
    broker = MockBrokerClient()
    executor = Executor(broker=broker, account="DU123")
    opp = make_opportunity(entry=100.0, tp=102.0, sl=99.0)
    ctx = _ctx()
    ctx.market_open = False
    result = await executor.execute(_decision(opp), opp, ctx)
    assert isinstance(result, ExecutionRejected)
    assert result.reason == "pre_trade_reject"
    assert "mercado_cerrado" in result.violations


async def test_executor_idempotency_blocks_second_send():
    broker = MockBrokerClient()
    executor = Executor(broker=broker, account="DU123")
    opp = make_opportunity(entry=100.0, tp=102.0, sl=99.0, size=0.03)
    first = await executor.execute(_decision(opp), opp, _ctx())
    assert not isinstance(first, ExecutionRejected)
    second = await executor.execute(_decision(opp), opp, _ctx())
    assert isinstance(second, ExecutionRejected)
    assert "orden_duplicada" in second.violations
