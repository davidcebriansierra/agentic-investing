"""Tests de la capa de observabilidad y del ensamblado de la app."""
from __future__ import annotations

from src.app.main import Application, build_searchers
from src.observability.metrics import Metrics, get_metrics


def test_metrics_singleton():
    assert get_metrics() is get_metrics()


def test_metrics_counters_are_callable_noop_safe():
    m = Metrics()
    # No debe lanzar aunque prometheus_client no este instalado (no-op).
    m.opportunities_total.labels(agent_source="technical").inc()
    m.decisions_total.labels(decision="OPERATE").inc()
    m.orders_total.labels(status="FILLED").inc()
    m.risk_rejections_total.inc()
    m.kill_switch_active.set(1)
    m.signal_to_order_seconds.observe(0.5)


def test_build_searchers_returns_six():
    searchers = build_searchers()
    assert len(searchers) == 6
    sources = {s.source.value for s in searchers}
    assert sources == {
        "technical",
        "fundamental",
        "news",
        "social",
        "cross_market",
        "premarket",
    }


def test_application_registers_jobs():
    app = Application()
    app._register_jobs()
    jobs = app.scheduler.jobs()
    assert len(jobs) == 7  # 6 buscadores + monitor
    assert any(j.job_id == "monitor" for j in jobs)


async def test_approve_applies_edited_prices():
    """Regresion: _approve aplica los precios editados en el HITL al MISMO opp que ejecuta
    el pipeline; antes se descartaban y la orden salia con los valores originales."""
    from src.connectors.telegram_bot import MockHITLClient
    from src.schemas.decision import Decision
    from src.schemas.enums import DecisionReason, DecisionType
    from src.schemas.hitl import ApprovalDecision, EditedPrices
    from tests.conftest import make_opportunity

    app = Application()
    app.hitl = MockHITLClient(
        default_decision=ApprovalDecision.APPROVE,
        edited_prices=EditedPrices(entry_price=13.00, stop_loss=12.60, take_profit=13.60),
    )
    opp = make_opportunity(entry=12.45, tp=12.95, sl=12.20)
    decision = Decision(
        opportunity_id=opp.opportunity_id, final_score=0.8, expectancy_pct=1.2,
        risk_reward_ratio=2.0, decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    approved = await app._approve(decision, opp)
    assert approved is True
    assert opp.entry_price == 13.00 and opp.stop_loss == 12.60 and opp.take_profit == 13.60


async def test_approve_keeps_original_prices_without_edits():
    """Sin edicion, _approve no toca los precios de la oportunidad."""
    from src.connectors.telegram_bot import MockHITLClient
    from src.schemas.decision import Decision
    from src.schemas.enums import DecisionReason, DecisionType
    from src.schemas.hitl import ApprovalDecision
    from tests.conftest import make_opportunity

    app = Application()
    app.hitl = MockHITLClient(default_decision=ApprovalDecision.APPROVE)
    opp = make_opportunity(entry=12.45, tp=12.95, sl=12.20)
    decision = Decision(
        opportunity_id=opp.opportunity_id, final_score=0.8, expectancy_pct=1.2,
        risk_reward_ratio=2.0, decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    assert await app._approve(decision, opp) is True
    assert opp.entry_price == 12.45 and opp.stop_loss == 12.20 and opp.take_profit == 12.95
