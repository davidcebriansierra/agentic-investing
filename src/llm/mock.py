"""Cliente LLM simulado para tests, shadow mode y desarrollo offline."""
from __future__ import annotations

from collections.abc import Callable

from src.llm.base import LLMResponse, LLMUsage


class MockLLMClient:
    """LLM determinístico.

    - `responder`: callable (system, user) -> str para respuestas dinamicas.
    - `canned`: texto fijo a devolver si no hay responder.
    Registra todas las llamadas en `self.calls` para aserciones en tests.
    """

    def __init__(
        self,
        canned: str = "{}",
        responder: Callable[[str, str], str] | None = None,
        model: str = "mock-llm",
    ) -> None:
        self.canned = canned
        self.responder = responder
        self.model = model
        self.calls: list[dict] = []

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
            }
        )
        text = self.responder(system, user) if self.responder else self.canned
        return LLMResponse(
            text=text,
            model=self.model,
            usage=LLMUsage(
                prompt_tokens=len(system) + len(user),
                completion_tokens=len(text),
                total_tokens=len(system) + len(user) + len(text),
            ),
            raw=text,
        )
