"""Agente Decisor deterministico (spec v2.0, seccion 3.5).

Aplica la regla de consenso ponderado, calcula la expectancy calibrada con la
memoria episodica y emite una Decision OPERATE / NO_OPERATE con el motivo.
"""
from __future__ import annotations

from src.agents._agent_logger import log_entry
from src.memory.episodic import EpisodicMemory
from src.schemas.decision import Decision
from src.schemas.enums import DecisionReason, DecisionType, RiskDecision
from src.schemas.evaluation import Evaluation
from src.schemas.opportunity import Opportunity
from src.utils.config import get_decisor_config, get_risk_limits


class Decisor:
    def __init__(
        self,
        memory: EpisodicMemory | None = None,
        weights: dict[str, float] | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        cfg = get_decisor_config()
        self.weights = weights or cfg.get("weights", {})
        self.t = thresholds or cfg.get("thresholds", {})
        # Ratio riesgo/beneficio minimo: fuente unica en risk_limits.yaml
        # (misma clave que usa PreTradeValidator).
        self.min_risk_reward = get_risk_limits().get("risk_reward", {}).get("min_ratio", 2.0)
        self.memory = memory or EpisodicMemory()

    def decide(
        self,
        opp: Opportunity,
        evaluations: list[Evaluation],
        risk_pass: bool = True,
    ) -> Decision:
        log_entry("decisor", {
            "event": "decide_input",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": opp.ticker,
            "direction": opp.direction.value,
            "risk_pass": risk_pass,
            "evaluations": [
                {"evaluator": e.evaluator.value, "score": e.score,
                 "confidence": e.confidence, "recommendation": e.recommendation.value}
                for e in evaluations
            ],
        })
        breakdown = {e.evaluator.value: e.score for e in evaluations}

        # score_final = Σ (peso_i × score_i × confidence_i)
        final_score = sum(
            self.weights.get(e.evaluator.value, 0.0) * e.score * e.confidence
            for e in evaluations
        )
        final_score = min(1.0, max(0.0, final_score))

        consensus_count = sum(
            1
            for e in evaluations
            if e.score >= self.t.get("min_evaluator_score", 0.5)
        )

        # Expectancy calibrada con memoria episodica.
        p_win = self.memory.calibrated_win_prob(opp)
        avg_win = self.memory.avg_win_return(opp) or opp.take_profit_pct
        avg_loss = self.memory.avg_loss_return(opp) or opp.stop_loss_pct
        expectancy = p_win * avg_win - (1 - p_win) * avg_loss
        expectancy_pct = expectancy * 100

        rr = opp.computed_risk_reward

        # Determinar decision y razon (primer fallo encontrado).
        reason = self._evaluate_rules(
            final_score, consensus_count, expectancy_pct, rr, risk_pass
        )
        operate = reason == DecisionReason.CONSENSUS_REACHED

        decision = Decision(
            opportunity_id=opp.opportunity_id,
            final_score=round(final_score, 4),
            expectancy_pct=round(expectancy_pct, 4),
            risk_reward_ratio=round(rr, 4),
            evaluator_breakdown=breakdown,
            consensus_count=consensus_count,
            decision=DecisionType.OPERATE if operate else DecisionType.NO_OPERATE,
            reason=reason,
            final_order_spec=self._order_spec(opp) if operate else None,
        )
        log_entry("decisor", {
            "event": "decide_result",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": opp.ticker,
            "final_score": decision.final_score,
            "expectancy_pct": decision.expectancy_pct,
            "rr": decision.risk_reward_ratio,
            "consensus_count": consensus_count,
            "decision": decision.decision.value,
            "reason": decision.reason.value,
            "order_spec": decision.final_order_spec,
        })
        return decision

    def _evaluate_rules(
        self,
        final_score: float,
        consensus_count: int,
        expectancy_pct: float,
        rr: float,
        risk_pass: bool,
    ) -> DecisionReason:
        if not risk_pass:
            return DecisionReason.RISK_FILTER_REJECT
        if final_score < self.t.get("min_final_score", 0.60):
            return DecisionReason.LOW_CONFIDENCE
        if consensus_count < self.t.get("min_consensus_count", 3):
            return DecisionReason.LOW_CONSENSUS
        if expectancy_pct <= self.t.get("min_expectancy_pct", 0.30):
            return DecisionReason.LOW_EXPECTANCY
        if rr < self.min_risk_reward:
            return DecisionReason.LOW_RISK_REWARD
        return DecisionReason.CONSENSUS_REACHED

    @staticmethod
    def _order_spec(opp: Opportunity) -> dict:
        return {
            "ticker": opp.ticker,
            "direction": opp.direction.value,
            "entry_price": opp.entry_price,
            "take_profit": opp.take_profit,
            "stop_loss": opp.stop_loss,
            "position_size_pct": opp.position_size_pct,
        }
