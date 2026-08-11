"""Tests del agente Decisor."""
from __future__ import annotations

from src.agents.decisor import Decisor
from src.agents.evaluators import default_evaluators
from src.schemas.enums import DecisionReason, DecisionType, EvaluatorType, Recommendation
from src.schemas.evaluation import Evaluation
from tests.conftest import make_opportunity


def _evals(scores: dict[str, float], confidence: float = 0.8) -> list[Evaluation]:
    return [
        Evaluation(
            opportunity_id="x",
            evaluator=EvaluatorType(ev),
            score=s,
            confidence=confidence,
            recommendation=Recommendation.APPROVE,
        )
        for ev, s in scores.items()
    ]


def test_no_operate_when_risk_filter_fails():
    decisor = Decisor()
    opp = make_opportunity()
    decision = decisor.decide(opp, [], risk_pass=False)
    assert decision.decision == DecisionType.NO_OPERATE
    assert decision.reason == DecisionReason.RISK_FILTER_REJECT


def test_no_operate_low_consensus():
    decisor = Decisor()
    opp = make_opportunity()
    evals = _evals({"conservative": 0.9, "moderate": 0.2, "high_risk": 0.2, "sensationalist": 0.2})
    decision = decisor.decide(opp, evals, risk_pass=True)
    assert decision.decision == DecisionType.NO_OPERATE


def test_operate_when_all_conditions_met():
    decisor = Decisor()
    # R/R = 2.0 exacto, TP 2%, SL 1%.
    opp = make_opportunity(entry=100.0, tp=102.0, sl=99.0, win_prob=0.7)
    evals = _evals(
        {"conservative": 0.85, "moderate": 0.85, "high_risk": 0.8, "sensationalist": 0.7},
        confidence=0.9,
    )
    decision = decisor.decide(opp, evals, risk_pass=True)
    assert decision.decision == DecisionType.OPERATE
    assert decision.reason == DecisionReason.CONSENSUS_REACHED
    assert decision.consensus_count == 4
    assert decision.final_order_spec is not None


def test_default_evaluators_produce_four_evaluations():
    opp = make_opportunity()
    evals = [ev.evaluate(opp) for ev in default_evaluators()]
    assert len(evals) == 4
    assert {e.evaluator for e in evals} == set(EvaluatorType)
