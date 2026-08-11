"""Evaluador respaldado por LLM (spec v2.0, seccion 3.4).

Usa un prompt versionado (config/prompts/evaluators/<perfil>.md) y un LLMClient para
puntuar la oportunidad. El parseo de la respuesta es determinístico. Si el LLM falla o
devuelve algo no parseable, aplica un fallback conservador (ABSTAIN, score 0) acorde al
principio de "no-operar por defecto".
"""
from __future__ import annotations

from src.agents._agent_logger import log_entry
from src.agents.evaluators.base import BaseEvaluator
from src.llm.base import LLMClient
from src.llm.parsing import extract_json
from src.llm.prompts import PromptLibrary, PromptTemplate
from src.schemas.enums import EvaluatorType, Recommendation
from src.schemas.evaluation import Evaluation
from src.schemas.opportunity import Opportunity


def _multi_source_context(opp: Opportunity) -> str:
    """Frase para el prompt que resume la convergencia multi-fuente de la oportunidad."""
    sources = (
        opp.supporting_data.get("converging_sources")
        or opp.supporting_data.get("merged_sources")
        or []
    )
    if opp.sources_count > 1 and sources:
        return (
            f"CONVERGENCIA MULTIFUENTE: {opp.sources_count} fuentes independientes han "
            f"senalado esta misma oportunidad en la ultima hora ({', '.join(sources)}). "
            f"Esto refuerza la senal; se ha aplicado un bonus de +{opp.confidence_bonus:.2f} "
            f"a la probabilidad de exito estimada."
        )
    return (
        "CONVERGENCIA MULTIFUENTE: no aplicable por ahora (una sola fuente ha senalado "
        "esta oportunidad). Su ausencia es NEUTRA: NO penalices el score por ello, ya "
        "que muchas senales validas provienen de una unica fuente. La convergencia, "
        "cuando existe, es un plus; su ausencia no es un defecto."
    )


class LLMEvaluator(BaseEvaluator):
    def __init__(
        self,
        evaluator_type: EvaluatorType,
        llm: LLMClient,
        prompts: PromptLibrary | None = None,
        temperature: float = 0.2,
    ) -> None:
        super().__init__(evaluator_type)
        self.llm = llm
        self.prompts = prompts or PromptLibrary()
        self.temperature = temperature
        self._prompt: PromptTemplate | None = None

    @property
    def prompt(self) -> PromptTemplate:
        if self._prompt is None:
            self._prompt = self.prompts.get(f"evaluators/{self.evaluator_type.value}")
        return self._prompt

    def _score(self, opp: Opportunity) -> tuple[float, float, str]:
        # No usado: LLMEvaluator sobreescribe evaluate() directamente (es async).
        raise NotImplementedError("LLMEvaluator usa aevaluate()")

    async def aevaluate(self, opp: Opportunity) -> Evaluation:
        """Evalua de forma asincrona usando el LLM."""
        agent_name = f"evaluator_{self.evaluator_type.value}"
        system = self.prompt.render(
            ticker=opp.ticker,
            direction=opp.direction.value,
            entry_price=opp.entry_price,
            take_profit=opp.take_profit,
            stop_loss=opp.stop_loss,
            risk_reward=round(opp.computed_risk_reward, 3),
            win_probability=opp.estimated_win_probability,
            position_size_pct=opp.position_size_pct,
            justification=opp.justification,
            supporting_data=opp.supporting_data,
            multi_source_context=_multi_source_context(opp),
        )
        user = "Evalua la oportunidad y responde solo con el JSON solicitado."
        log_entry(agent_name, {
            "event": "llm_request",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": opp.ticker,
            "model": getattr(self.llm, "model", "unknown"),
            "prompt_name": f"evaluators/{self.evaluator_type.value}",
            "prompt_version": self.prompt.version,
            "temperature": self.temperature,
            "context_chars": len(system),
            "context_snippet": system[:500],
            "system_prompt_chars": len(system),
        })
        try:
            response = await self.llm.complete(
                system=system,
                user=user,
                temperature=self.temperature,
                json_mode=True,
            )
            log_entry(agent_name, {
                "event": "llm_response",
                "opportunity_id": str(opp.opportunity_id),
                "model": response.model,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "raw_response": response.text,
            })
            data = extract_json(response.text)
            result = self._to_evaluation(opp, data, response)
            log_entry(agent_name, {
                "event": "evaluate_result",
                "opportunity_id": str(opp.opportunity_id),
                "score": result.score,
                "confidence": result.confidence,
                "recommendation": result.recommendation.value,
                "justification": result.justification[:200],
            })
            return result
        except Exception:  # noqa: BLE001 - fallback conservador ante cualquier fallo
            log_entry(agent_name, {
                "event": "llm_fallback",
                "opportunity_id": str(opp.opportunity_id),
            })
            return self._fallback(opp)

    def _to_evaluation(self, opp: Opportunity, data: dict, response) -> Evaluation:
        score = self._clamp(float(data.get("score", 0.0)))
        confidence = self._clamp(float(data.get("confidence", 0.0)))
        rec_raw = str(data.get("recommendation", "ABSTAIN")).upper()
        try:
            rec = Recommendation(rec_raw)
        except ValueError:
            rec = Recommendation.ABSTAIN
        return Evaluation(
            opportunity_id=opp.opportunity_id,
            evaluator=self.evaluator_type,
            score=round(score, 4),
            confidence=round(confidence, 4),
            recommendation=rec,
            adjusted_take_profit=data.get("adjusted_take_profit"),
            adjusted_stop_loss=data.get("adjusted_stop_loss"),
            justification=str(data.get("justification", ""))[:500],
            model_version=response.model,
            prompt_version=self.prompt.version,
            ticker=data.get("ticker", opp.ticker),
            direction=data.get("direction", opp.direction.value),
            entry_price=data.get("entry_price", opp.entry_price),
            take_profit=data.get("take_profit", opp.take_profit),
            stop_loss=data.get("stop_loss", opp.stop_loss),
            risk_reward=data.get("risk_reward", round(opp.computed_risk_reward, 3)),
            win_probability=data.get("win_probability", opp.estimated_win_probability),
            position_size_pct=data.get("position_size_pct", opp.position_size_pct),
        )

    def _fallback(self, opp: Opportunity) -> Evaluation:
        return Evaluation(
            opportunity_id=opp.opportunity_id,
            evaluator=self.evaluator_type,
            score=0.0,
            confidence=0.0,
            recommendation=Recommendation.ABSTAIN,
            justification="fallback: respuesta LLM no parseable (no-operar por defecto)",
            prompt_version=self.prompt.version,
            ticker=opp.ticker,
            direction=opp.direction.value,
            entry_price=opp.entry_price,
            take_profit=opp.take_profit,
            stop_loss=opp.stop_loss,
            risk_reward=round(opp.computed_risk_reward, 3),
            win_probability=opp.estimated_win_probability,
            position_size_pct=opp.position_size_pct,
        )

    @staticmethod
    def _clamp(x: float) -> float:
        return min(1.0, max(0.0, x))
