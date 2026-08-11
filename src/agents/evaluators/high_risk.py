"""Evaluador de Alto Riesgo (spec v2.0, seccion 3.4).

Tolerancia a drawdown, busca alfa: premia recorridos amplios al take-profit aunque
la probabilidad de exito sea moderada.
"""
from __future__ import annotations

from src.agents.evaluators.base import BaseEvaluator
from src.schemas.enums import EvaluatorType
from src.schemas.opportunity import Opportunity


class HighRiskEvaluator(BaseEvaluator):
    def __init__(self) -> None:
        super().__init__(EvaluatorType.HIGH_RISK)

    def _score(self, opp: Opportunity) -> tuple[float, float, str]:
        upside = min(opp.take_profit_pct / 0.03, 1.0)  # normalizado a un TP del 3%
        win_score = opp.estimated_win_probability
        score = 0.6 * upside + 0.4 * win_score
        confidence = 0.5
        return (
            score,
            confidence,
            f"upside={opp.take_profit_pct:.2%}, p_win={win_score:.2f}",
        )
