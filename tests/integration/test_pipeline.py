"""Test de integracion del orquestador completo (aggregate -> ... -> execute)."""
from __future__ import annotations

from src.graph.pipeline import DecisionPipeline
from src.schemas.enums import DecisionType
from tests.conftest import make_opportunity
from src.schemas.portfolio import Portfolio


def test_pipeline_rejects_on_risk_filter():
    pipeline = DecisionPipeline()
    # Liquidez insuficiente -> risk filter REJECT.
    opp = make_opportunity(ticker="IBE.MC", size=0.02)
    portfolio = Portfolio(
        total_equity=100_000, cash=90_000, avg_volume_20d={"IBE.MC": 100}
    )
    states = pipeline.run([opp], portfolio)
    assert len(states) == 1
    assert states[0].risk_pass is False
    assert states[0].decision.decision == DecisionType.NO_OPERATE


def test_pipeline_full_flow_with_approval_and_execution():
    pipeline = DecisionPipeline()
    opp = make_opportunity(entry=100.0, tp=102.0, sl=99.0, win_prob=0.7, size=0.03)
    portfolio = Portfolio(total_equity=100_000, cash=100_000)

    executed = {}

    def approve(decision, opportunity):
        return True

    def execute(decision, opportunity):
        executed["ticker"] = opportunity.ticker
        return {"status": "FILLED", "ticker": opportunity.ticker}

    states = pipeline.run([opp], portfolio, approval_fn=approve, execute_fn=execute)
    state = states[0]
    assert state.risk_pass is True
    assert len(state.evaluations) == 4
    # Con evaluadores heuristicos por defecto, comprobamos coherencia del flujo.
    if state.decision.decision == DecisionType.OPERATE:
        assert state.approved is True
        assert executed.get("ticker") == "IBE.MC"


def test_audit_logger_records_events():
    pipeline = DecisionPipeline()
    opp = make_opportunity()
    portfolio = Portfolio(total_equity=100_000, cash=100_000)
    pipeline.run([opp], portfolio)
    event_types = {e["event_type"] for e in pipeline.audit.events}
    assert "opportunity" in event_types
    assert "decision" in event_types
