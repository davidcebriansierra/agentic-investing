"""Evaluador Moderado (spec v2.0, seccion 3.4).

Balance riesgo/retorno con preferencia por R/R >= 2.
"""
from __future__ import annotations

from src.agents.evaluators.base import BaseEvaluator
from src.schemas.enums import EvaluatorType
from src.schemas.opportunity import Opportunity


class ModerateEvaluator(BaseEvaluator):
    def __init__(self) -> None:
        super().__init__(EvaluatorType.MODERATE)

    def _score(self, opp: Opportunity) -> tuple[float, float, str]:
        rr = opp.computed_risk_reward
        rr_score = 1.0 if rr >= 2.0 else rr / 2.0
        win_score = opp.estimated_win_probability
        score = 0.45 * rr_score + 0.45 * win_score + 0.10
        confidence = 0.65
        return score, confidence, f"balance R/R={rr:.2f}, p_win={win_score:.2f}"
