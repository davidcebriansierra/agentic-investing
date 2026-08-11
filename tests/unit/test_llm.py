"""Tests de la capa LLM: parseo, prompts y evaluador LLM."""
from __future__ import annotations

import json

import pytest

from src.agents.evaluators.llm_evaluator import LLMEvaluator
from src.llm.mock import MockLLMClient
from src.llm.parsing import extract_json
from src.llm.prompts import PromptLibrary
from src.schemas.enums import EvaluatorType, Recommendation
from tests.conftest import make_opportunity


# ---------- extract_json ----------

def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fence():
    text = 'Aqui tienes:\n```json\n{"score": 0.7, "ok": true}\n```\nfin'
    assert extract_json(text) == {"score": 0.7, "ok": True}


def test_extract_json_embedded_in_text():
    text = 'bla bla {"x": {"y": 2}} mas texto'
    assert extract_json(text) == {"x": {"y": 2}}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        extract_json("no hay json aqui")


# ---------- PromptLibrary ----------

def test_prompt_library_loads_versioned_prompt():
    lib = PromptLibrary()
    tpl = lib.get("evaluators/conservative")
    assert tpl.version == "v1.0"
    assert tpl.metadata.get("evaluator") == "conservative"
    assert len(tpl.hash) == 16


def test_prompt_render_substitutes_variables():
    lib = PromptLibrary()
    tpl = lib.get("evaluators/moderate")
    rendered = tpl.render(
        ticker="IBE.MC",
        direction="LONG",
        entry_price=12.45,
        take_profit=12.70,
        stop_loss=12.32,
        risk_reward=2.0,
        win_probability=0.58,
        position_size_pct=0.05,
        justification="setup",
        supporting_data={},
        multi_source_context="SIN CONVERGENCIA MULTIFUENTE",
    )
    assert "IBE.MC" in rendered
    # Las llaves dobles {{ }} del JSON de ejemplo deben quedar como llaves simples.
    assert '"score": 0.0' in rendered


# ---------- LLMEvaluator ----------

async def test_llm_evaluator_parses_valid_response():
    payload = json.dumps(
        {
            "score": 0.72,
            "confidence": 0.65,
            "recommendation": "APPROVE",
            "adjusted_take_profit": None,
            "adjusted_stop_loss": None,
            "justification": "buena relacion R/R",
        }
    )
    llm = MockLLMClient(canned=f"```json\n{payload}\n```")
    evaluator = LLMEvaluator(EvaluatorType.CONSERVATIVE, llm=llm)
    opp = make_opportunity()
    result = await evaluator.aevaluate(opp)
    assert result.score == 0.72
    assert result.confidence == 0.65
    assert result.recommendation == Recommendation.APPROVE
    assert result.prompt_version == "v1.0"
    assert len(llm.calls) == 1
    assert llm.calls[0]["json_mode"] is True


async def test_llm_evaluator_fallback_on_garbage():
    llm = MockLLMClient(canned="esto no es json valido")
    evaluator = LLMEvaluator(EvaluatorType.HIGH_RISK, llm=llm)
    result = await evaluator.aevaluate(make_opportunity())
    assert result.score == 0.0
    assert result.recommendation == Recommendation.ABSTAIN
    assert "fallback" in result.justification


async def test_llm_evaluator_clamps_out_of_range():
    payload = json.dumps({"score": 1.5, "confidence": -0.2, "recommendation": "APPROVE"})
    llm = MockLLMClient(canned=payload)
    evaluator = LLMEvaluator(EvaluatorType.MODERATE, llm=llm)
    result = await evaluator.aevaluate(make_opportunity())
    assert result.score == 1.0
    assert result.confidence == 0.0
