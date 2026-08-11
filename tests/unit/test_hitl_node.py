"""Tests del nodo HITL del grafo LangGraph (_make_hitl_node)."""
from __future__ import annotations

from src.connectors.telegram_bot import MockHITLClient, build_request
from src.governance.kill_switch import KillSwitch
from src.graph.workflow import _make_hitl_node
from src.schemas.decision import Decision
from src.schemas.enums import DecisionReason, DecisionType
from src.schemas.hitl import ApprovalDecision, EditedPrices
from src.schemas.state import SystemState
from tests.conftest import make_opportunity


def _make_state(decision_type: DecisionType = DecisionType.OPERATE) -> SystemState:
    opp = make_opportunity(entry=12.45, tp=12.95, sl=12.20)
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.8,
        expectancy_pct=1.2,
        risk_reward_ratio=2.0,
        decision=decision_type,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    return SystemState(
        raw_opportunities=[opp],
        consolidated=[opp],
        decision=decision,
    )


async def test_hitl_node_approve_sets_approved_true():
    hitl = MockHITLClient(default_decision=ApprovalDecision.APPROVE)
    node = _make_hitl_node(hitl)
    state = _make_state()
    result = await node(state)
    assert result.approved is True


async def test_hitl_node_timeout_sets_approved_false():
    hitl = MockHITLClient(default_decision=ApprovalDecision.TIMEOUT)
    node = _make_hitl_node(hitl)
    state = _make_state()
    result = await node(state)
    assert result.approved is False


async def test_hitl_node_reject_sets_approved_false():
    hitl = MockHITLClient(default_decision=ApprovalDecision.REJECT)
    node = _make_hitl_node(hitl)
    state = _make_state()
    result = await node(state)
    assert result.approved is False


async def test_hitl_node_pause_activates_kill_switch():
    hitl = MockHITLClient(default_decision=ApprovalDecision.PAUSE_1H)
    ks = KillSwitch()
    assert not ks.active
    node = _make_hitl_node(hitl, kill_switch=ks)
    state = _make_state()
    result = await node(state)
    assert result.approved is False
    assert ks.active


async def test_hitl_node_pause_without_kill_switch_does_not_raise():
    hitl = MockHITLClient(default_decision=ApprovalDecision.PAUSE_1H)
    node = _make_hitl_node(hitl, kill_switch=None)
    state = _make_state()
    result = await node(state)
    assert result.approved is False


async def test_hitl_node_empty_state_returns_not_approved():
    hitl = MockHITLClient(default_decision=ApprovalDecision.APPROVE)
    node = _make_hitl_node(hitl)
    state = SystemState()  # sin consolidated ni decision
    result = await node(state)
    assert result.approved is False


async def test_hitl_node_exception_in_client_returns_not_approved():
    class FailingHITL:
        async def request_approval(self, request):
            raise RuntimeError("red caida")

    node = _make_hitl_node(FailingHITL())
    state = _make_state()
    result = await node(state)
    assert result.approved is False


async def test_hitl_node_edited_prices_update_opportunity():
    """APPROVE con edited_prices -> state.consolidated[0] refleja los nuevos precios."""
    prices = EditedPrices(entry_price=13.00, stop_loss=12.60, take_profit=13.60)
    hitl = MockHITLClient(default_decision=ApprovalDecision.APPROVE, edited_prices=prices)
    node = _make_hitl_node(hitl)
    state = _make_state()  # entry=12.45, tp=12.95, sl=12.20
    result = await node(state)
    assert result.approved is True
    opp = result.consolidated[0]
    assert opp.entry_price == 13.00
    assert opp.stop_loss == 12.60
    assert opp.take_profit == 13.60


async def test_hitl_node_edited_prices_not_applied_on_reject():
    """REJECT con edited_prices -> el opportunity NO se modifica."""
    prices = EditedPrices(entry_price=13.00, stop_loss=12.60, take_profit=13.60)
    hitl = MockHITLClient(default_decision=ApprovalDecision.REJECT, edited_prices=prices)
    node = _make_hitl_node(hitl)
    state = _make_state()
    original_entry = state.consolidated[0].entry_price
    result = await node(state)
    assert result.approved is False
    assert result.consolidated[0].entry_price == original_entry


async def test_hitl_node_all_decisions_invariant():
    """Solo APPROVE produce approved=True; el resto no opera."""
    for dec in ApprovalDecision:
        hitl = MockHITLClient(default_decision=dec)
        ks = KillSwitch()
        node = _make_hitl_node(hitl, kill_switch=ks)
        state = _make_state()
        result = await node(state)
        expected = dec == ApprovalDecision.APPROVE
        assert result.approved == expected, (
            f"decision={dec.value}: esperado approved={expected}, obtenido {result.approved}"
        )
