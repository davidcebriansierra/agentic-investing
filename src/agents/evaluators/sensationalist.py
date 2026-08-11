"""Evaluador Sensacionalista (spec v2.0, seccion 3.4).

Momentum + noticias: premia oportunidades con respaldo de fuentes de noticias/redes
y convergencia multi-fuente (sources_count).
"""
from __future__ import annotations

from src.agents.evaluators.base import BaseEvaluator
from src.schemas.enums import AgentSource, EvaluatorType
from src.schemas.opportunity import Opportunity


class SensationalistEvaluator(BaseEvaluator):
    def __init__(self) -> None:
        super().__init__(EvaluatorType.SENSATIONALIST)

    def _score(self, opp: Opportunity) -> tuple[float, float, str]:
        news_refs = opp.supporting_data.get("news_refs", [])
        momentum = min(len(news_refs) / 3.0, 1.0)
        multi_source = min((opp.sources_count - 1) * 0.25, 0.5)
        from_news = opp.agent_source in (AgentSource.NEWS, AgentSource.SOCIAL)
        base = 0.5 if from_news else 0.3
        score = base + 0.4 * momentum + multi_source
        confidence = 0.4 + 0.2 * momentum
        return (
            score,
            confidence,
            f"momentum_refs={len(news_refs)}, sources={opp.sources_count}",
        )
