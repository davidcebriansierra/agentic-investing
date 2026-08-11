"""Construccion del pipeline segun la configuracion (config.llm.evaluation_engine).

- `heuristic` (por defecto): evaluadores deterministicos, pipeline sincrono (`run`).
- `llm`: evaluadores respaldados por LLM, pipeline asincrono (`arun`).

El cliente LLM se construye desde el proveedor configurado; si el SDK o las claves no
estan disponibles, se hace fallback a `MockLLMClient` con un aviso (modo offline).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from src.agents.evaluators import default_evaluators, llm_evaluators
from src.agents.evaluators.base import BaseEvaluator
from src.graph.pipeline import DecisionPipeline
from src.llm.base import LLMClient
from src.llm.mock import MockLLMClient
from src.persistence.repository import InMemoryRepository

logger = logging.getLogger("agentic.factory")

_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "azure_openai": "OPENAI_API_KEY",
    "azure": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


def resolve_engine(config: dict[str, Any]) -> str:
    """Devuelve 'llm' o 'heuristic' a partir de config.llm.evaluation_engine."""
    engine = config.get("llm", {}).get("evaluation_engine", "heuristic")
    engine = str(engine).lower()
    return "llm" if engine == "llm" else "heuristic"


def build_llm_client(config: dict[str, Any]) -> LLMClient:
    """Crea el cliente LLM real; cae a MockLLMClient si falta SDK o clave."""
    llm_cfg = config.get("llm", {})
    provider = str(llm_cfg.get("provider", "openai")).lower()
    model = llm_cfg.get("reasoning_model", "gpt-5.1")

    is_azure = provider in ("azure", "azure_openai")
    is_deepseek = provider == "deepseek"
    # En Azure la clave puede venir en AZURE_OPENAI_API_KEY u OPENAI_API_KEY.
    api_key = os.getenv(_API_KEY_ENV.get(provider, "OPENAI_API_KEY"))
    if is_azure:
        api_key = os.getenv("AZURE_OPENAI_API_KEY") or api_key
    if not api_key:
        logger.warning(
            "Sin API key para el proveedor '%s': usando MockLLMClient (modo offline).",
            provider,
        )
        return MockLLMClient()

    kwargs: dict[str, Any] = {"api_key": api_key}
    if is_azure:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        if not endpoint:
            logger.warning(
                "Proveedor azure sin AZURE_OPENAI_ENDPOINT: usando MockLLMClient."
            )
            return MockLLMClient()
        kwargs["azure_endpoint"] = endpoint
        kwargs["api_version"] = os.getenv("OPENAI_API_VERSION", "2024-06-01")
        # En Azure, el "modelo" es el nombre del deployment.
        model = os.getenv("AZURE_OPENAI_DEPLOYMENT", model)
    elif is_deepseek:
        # DeepSeek expone una API compatible con OpenAI en https://api.deepseek.com
        kwargs["base_url"] = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    elif provider == "openai":
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url

    try:
        from src.llm.providers import build_llm_client as _build_real

        return _build_real(provider=provider, model=model, **kwargs)
    except ImportError as exc:
        logger.warning("SDK LLM no disponible (%s): usando MockLLMClient.", exc)
        return MockLLMClient()


def build_evaluators(
    config: dict[str, Any], llm: LLMClient | None = None
) -> list[BaseEvaluator]:
    """Selecciona los evaluadores segun el motor configurado."""
    if resolve_engine(config) == "llm":
        client = llm or build_llm_client(config)
        temperature = float(config.get("llm", {}).get("temperature", 0.2))
        return llm_evaluators(client, temperature=temperature)
    return default_evaluators()


def build_pipeline(
    config: dict[str, Any],
    repository: InMemoryRepository | None = None,
    llm: LLMClient | None = None,
) -> DecisionPipeline:
    """Construye el DecisionPipeline con los evaluadores adecuados."""
    evaluators = build_evaluators(config, llm=llm)
    return DecisionPipeline(evaluators=evaluators, repository=repository)
