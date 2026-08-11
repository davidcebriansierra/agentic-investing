"""Evaluador Conservador (spec v2.0, seccion 3.4).

Prioriza preservacion de capital y baja volatilidad: premia R/R alto, stops ajustados
y probabilidad de exito elevada; penaliza tamanos de posicion grandes.
"""
from __future__ import annotations

from src.agents.evaluators.base import BaseEvaluator
from src.schemas.enums import EvaluatorType
from src.schemas.opportunity import Opportunity


class ConservativeEvaluator(BaseEvaluator):
    def __init__(self) -> None:
        super().__init__(EvaluatorType.CONSERVATIVE)

    def _score(self, opp: Opportunity) -> tuple[float, float, str]:
        rr = opp.computed_risk_reward
        rr_score = min(rr / 3.0, 1.0)
        win_score = opp.estimated_win_probability
        size_penalty = max(0.0, (opp.position_size_pct - 0.05) * 4)
        score = 0.5 * rr_score + 0.5 * win_score - size_penalty
        confidence = 0.6 + 0.4 * win_score
        return (
            score,
            confidence,
            f"R/R={rr:.2f}, p_win={win_score:.2f}, size={opp.position_size_pct:.2%}",
        )
