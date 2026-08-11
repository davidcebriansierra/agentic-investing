"""Tests de la rama asincrona del orquestador (arun)."""
from __future__ import annotations

import json

from src.agents.evaluators import default_evaluators, llm_evaluators
from src.graph.pipeline import DecisionPipeline
from src.llm.mock import MockLLMClient
from src.schemas.enums import DecisionType
from src.schemas.portfolio import Portfolio
from tests.conftest import make_opportunity


def _portfolio() -> Portfolio:
    return Portfolio(total_equity=100_000.0, cash=100_000.0)


def _operable_opp():
    # R/R = 0.50 / 0.25 = 2.0 (cumple el minimo del decisor).
    return make_opportunity(entry=12.45, tp=12.95, sl=12.20)


async def test_arun_parity_with_run_for_heuristics():
    """Con evaluadores heuristicos, arun debe dar las mismas decisiones que run."""
    opp = make_opportunity()
    sync_pipe = DecisionPipeline(evaluators=default_evaluators())
    async_pipe = DecisionPipeline(evaluators=default_evaluators())

    sync_states = sync_pipe.run([opp], _portfolio())
    async_states = await async_pipe.arun([opp], _portfolio())

    assert len(async_states) == len(sync_states) == 1
    assert async_states[0].decision.decision == sync_states[0].decision.decision
    assert len(async_states[0].evaluations) == 4


async def test_arun_with_llm_evaluators_reaches_operate():
    payload = json.dumps(
        {"score": 0.85, "confidence": 0.8, "recommendation": "APPROVE",
         "justification": "fuerte"}
    )
    llm = MockLLMClient(canned=payload)
    pipe = DecisionPipeline(evaluators=llm_evaluators(llm))
    states = await pipe.arun([_operable_opp()], _portfolio())
    state = states[0]
    assert len(state.evaluations) == 4
    assert all(e.model_version == "mock-llm" for e in state.evaluations)
    assert state.decision.decision == DecisionType.OPERATE
    # 4 evaluadores -> 4 llamadas al LLM.
    assert len(llm.calls) == 4


async def test_arun_supports_async_callbacks():
    payload = json.dumps({"score": 0.85, "confidence": 0.8, "recommendation": "APPROVE"})
    pipe = DecisionPipeline(evaluators=llm_evaluators(MockLLMClient(canned=payload)))

    executed: list[str] = []

    async def approval_fn(decision, opp):
        return True

    async def execute_fn(decision, opp):
        executed.append(opp.ticker)
        return {"status": "FILLED"}

    states = await pipe.arun(
        [_operable_opp()], _portfolio(), approval_fn=approval_fn, execute_fn=execute_fn
    )
    state = states[0]
    assert state.approved is True
    assert executed == ["IBE.MC"]
    assert state.execution_result == {"status": "FILLED"}


async def test_arun_sync_callbacks_still_work():
    payload = json.dumps({"score": 0.85, "confidence": 0.8, "recommendation": "APPROVE"})
    pipe = DecisionPipeline(evaluators=llm_evaluators(MockLLMClient(canned=payload)))

    states = await pipe.arun(
        [_operable_opp()],
        _portfolio(),
        approval_fn=lambda d, o: True,
        execute_fn=lambda d, o: {"status": "FILLED"},
    )
    assert states[0].execution_result == {"status": "FILLED"}
