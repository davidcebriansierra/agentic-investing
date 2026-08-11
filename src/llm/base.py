"""Contrato del cliente LLM y modelos de respuesta.

El razonamiento de los agentes se delega a un LLM a traves de la interfaz `LLMClient`.
La ejecucion de ordenes NUNCA depende del LLM (spec v2.0: determinismo en ejecucion).
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResponse(BaseModel):
    """Respuesta normalizada de cualquier proveedor LLM."""

    text: str
    model: str
    usage: LLMUsage = Field(default_factory=LLMUsage)
    raw: str | None = None  # respuesta cruda para auditoria (§7.3 llm_traces)


class LLMClient(Protocol):
    """Interfaz comun para OpenAI, Anthropic, Bedrock, Azure OpenAI o mocks."""

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse: ...
