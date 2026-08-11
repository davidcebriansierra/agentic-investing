"""Tests del factory de pipeline segun config.llm.evaluation_engine."""
from __future__ import annotations

from src.agents.evaluators import LLMEvaluator
from src.agents.evaluators.base import BaseEvaluator
from src.graph.factory import (
    build_evaluators,
    build_llm_client,
    build_pipeline,
    resolve_engine,
)
from src.llm.mock import MockLLMClient


def test_resolve_engine_defaults_to_heuristic():
    assert resolve_engine({}) == "heuristic"
    assert resolve_engine({"llm": {"evaluation_engine": "heuristic"}}) == "heuristic"


def test_resolve_engine_llm():
    assert resolve_engine({"llm": {"evaluation_engine": "llm"}}) == "llm"
    assert resolve_engine({"llm": {"evaluation_engine": "LLM"}}) == "llm"


def test_build_evaluators_heuristic():
    evs = build_evaluators({"llm": {"evaluation_engine": "heuristic"}})
    assert len(evs) == 4
    assert all(isinstance(e, BaseEvaluator) for e in evs)
    assert not any(isinstance(e, LLMEvaluator) for e in evs)


def test_build_evaluators_llm_uses_injected_client():
    evs = build_evaluators(
        {"llm": {"evaluation_engine": "llm"}}, llm=MockLLMClient()
    )
    assert len(evs) == 4
    assert all(isinstance(e, LLMEvaluator) for e in evs)


def test_build_llm_client_falls_back_to_mock_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = build_llm_client({"llm": {"provider": "openai"}})
    assert isinstance(client, MockLLMClient)


def test_build_pipeline_heuristic():
    pipe = build_pipeline({"llm": {"evaluation_engine": "heuristic"}})
    assert len(pipe.evaluators) == 4
    assert not any(isinstance(e, LLMEvaluator) for e in pipe.evaluators)


def test_build_pipeline_llm():
    pipe = build_pipeline(
        {"llm": {"evaluation_engine": "llm"}}, llm=MockLLMClient()
    )
    assert all(isinstance(e, LLMEvaluator) for e in pipe.evaluators)
