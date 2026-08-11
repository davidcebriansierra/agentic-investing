"""Proveedores LLM reales (OpenAI, Anthropic, Bedrock/Azure).

Imports perezosos: cada cliente solo requiere su SDK si se instancia. En Santander
los modelos se sirven via Bedrock o Azure OpenAI (spec v2.0, seccion 4).
"""
from __future__ import annotations

from src.llm.base import LLMResponse, LLMUsage


class OpenAIClient:
    """Cliente OpenAI o Azure OpenAI. Requiere el paquete `openai`.

    - OpenAI estandar: pasa `api_key` (y opcionalmente `base_url` para gateways compatibles).
    - Azure OpenAI: pasa `azure_endpoint` y `api_version`; en Azure, `model` es el nombre
      del *deployment*.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        azure_endpoint: str | None = None,
        api_version: str | None = None,
    ) -> None:
        try:  # pragma: no cover - depende de SDK opcional
            from openai import AsyncAzureOpenAI, AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise ImportError("Instala el SDK: pip install openai") from exc
        self.model = model
        if azure_endpoint:
            self._client = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=azure_endpoint,
                api_version=api_version or "2024-06-01",
            )
        else:
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # Parametros que algunos modelos recientes (familia gpt-5, o1) no admiten o
    # restringen; si el API los rechaza, se eliminan y se reintenta la llamada.
    _DROPPABLE_PARAMS = ("temperature", "max_completion_tokens", "response_format")

    async def complete(  # pragma: no cover - requiere red/credenciales
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse:
        from openai import BadRequestError

        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            # La familia gpt-5/o1 exige `max_completion_tokens` en lugar de `max_tokens`.
            "max_completion_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = None
        for _ in range(len(self._DROPPABLE_PARAMS) + 1):
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                break
            except BadRequestError as exc:
                # Elimina el primer parametro no soportado mencionado en el error y reintenta.
                msg = str(exc)
                dropped = next(
                    (p for p in self._DROPPABLE_PARAMS if p in kwargs and p in msg), None
                )
                if dropped is None:
                    raise
                kwargs.pop(dropped)
        assert resp is not None  # defensivo: el bucle sale por break o por raise
        choice = resp.choices[0].message.content or ""
        usage = resp.usage
        return LLMResponse(
            text=choice,
            model=self.model,
            usage=LLMUsage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0),
                completion_tokens=getattr(usage, "completion_tokens", 0),
                total_tokens=getattr(usage, "total_tokens", 0),
            ),
            raw=choice,
        )


class AnthropicClient:
    """Cliente Anthropic (Claude). Requiere el paquete `anthropic`."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:  # pragma: no cover
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise ImportError("Instala el SDK: pip install anthropic") from exc
        self.model = model
        self._client = AsyncAnthropic(api_key=api_key)

    async def complete(  # pragma: no cover - requiere red/credenciales
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResponse:
        resp = await self._client.messages.create(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = "".join(block.text for block in resp.content if hasattr(block, "text"))
        return LLMResponse(
            text=text,
            model=self.model,
            usage=LLMUsage(
                prompt_tokens=resp.usage.input_tokens,
                completion_tokens=resp.usage.output_tokens,
                total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
            ),
            raw=text,
        )


def build_llm_client(provider: str, model: str, **kwargs):
    """Factory simple por nombre de proveedor."""
    import os
    provider = provider.lower()
    if provider in ("openai", "azure", "azure_openai"):
        return OpenAIClient(model=model, **kwargs)
    if provider == "anthropic":
        return AnthropicClient(model=model, **kwargs)
    if provider == "deepseek":
        return OpenAIClient(
            model=model,
            api_key=kwargs.get("api_key") or os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
        )
    raise ValueError(f"Proveedor LLM no soportado: {provider}")
