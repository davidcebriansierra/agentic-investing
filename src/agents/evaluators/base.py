"""Clase base para los agentes evaluadores (spec v2.0, seccion 3.4).

Cada evaluador puntua una oportunidad con un perfil de riesgo distinto y devuelve
una Evaluation (score, confidence, recomendacion). El razonamiento real se delega a
un LLM en subclases productivas; esta base define el contrato y una heuristica
deterministica por defecto util para tests y shadow mode.
"""
from __future__ import annotations

import abc

from src.agents._agent_logger import log_entry
from src.schemas.enums import EvaluatorType, Recommendation
from src.schemas.evaluation import Evaluation
from src.schemas.opportunity import Opportunity


class BaseEvaluator(abc.ABC):
    evaluator_type: EvaluatorType

    def __init__(self, evaluator_type: EvaluatorType) -> None:
        self.evaluator_type = evaluator_type

    @abc.abstractmethod
    def _score(self, opp: Opportunity) -> tuple[float, float, str]:
        """Devuelve (score, confidence, justificacion) segun el perfil."""
        raise NotImplementedError

    async def aevaluate(self, opp: Opportunity) -> Evaluation:
        """Interfaz asincrona uniforme.

        Por defecto envuelve la evaluacion sincrona (heuristica). Las subclases con
        I/O real (p. ej. LLMEvaluator) la sobreescriben con su propia logica async.
        """
        return self.evaluate(opp)

    def evaluate(self, opp: Opportunity) -> Evaluation:
        log_entry(f"evaluator_{self.evaluator_type.value}", {
            "event": "evaluate_input",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": opp.ticker,
            "direction": opp.direction.value,
            "entry": opp.entry_price,
            "tp": opp.take_profit,
            "sl": opp.stop_loss,
            "rr": opp.risk_reward_ratio,
            "p_win": opp.estimated_win_probability,
            "justification": opp.justification[:200],
        })
        score, confidence, justification = self._score(opp)
        score = min(1.0, max(0.0, score))
        confidence = min(1.0, max(0.0, confidence))
        if score >= 0.6:
            rec = Recommendation.APPROVE
        elif score <= 0.35:
            rec = Recommendation.REJECT
        else:
            rec = Recommendation.ABSTAIN
        result = Evaluation(
            opportunity_id=opp.opportunity_id,
            evaluator=self.evaluator_type,
            score=round(score, 4),
            confidence=round(confidence, 4),
            recommendation=rec,
            justification=justification,
        )
        log_entry(f"evaluator_{self.evaluator_type.value}", {
            "event": "evaluate_result",
            "opportunity_id": str(opp.opportunity_id),
            "score": result.score,
            "confidence": result.confidence,
            "recommendation": result.recommendation.value,
            "justification": result.justification[:200],
        })
        return result
