"""Agentes evaluadores con perfiles de riesgo (spec v2.0, seccion 3.4)."""
from src.agents.evaluators.base import BaseEvaluator
from src.agents.evaluators.conservative import ConservativeEvaluator
from src.agents.evaluators.high_risk import HighRiskEvaluator
from src.agents.evaluators.llm_evaluator import LLMEvaluator
from src.agents.evaluators.moderate import ModerateEvaluator
from src.agents.evaluators.sensationalist import SensationalistEvaluator
from src.schemas.enums import EvaluatorType


def default_evaluators() -> list[BaseEvaluator]:
    """Conjunto estandar de 4 evaluadores heuristicos (sin LLM)."""
    return [
        ConservativeEvaluator(),
        ModerateEvaluator(),
        HighRiskEvaluator(),
        SensationalistEvaluator(),
    ]


def llm_evaluators(llm, prompts=None, temperature: float = 0.2) -> list[LLMEvaluator]:
    """Conjunto de 4 evaluadores respaldados por LLM (evaluacion asincrona)."""
    return [
        LLMEvaluator(et, llm=llm, prompts=prompts, temperature=temperature)
        for et in (
            EvaluatorType.CONSERVATIVE,
            EvaluatorType.MODERATE,
            EvaluatorType.HIGH_RISK,
            EvaluatorType.SENSATIONALIST,
        )
    ]


__all__ = [
    "BaseEvaluator",
    "ConservativeEvaluator",
    "HighRiskEvaluator",
    "LLMEvaluator",
    "ModerateEvaluator",
    "SensationalistEvaluator",
    "default_evaluators",
    "llm_evaluators",
]
