"""Prueba manual del cliente LLM real (OpenAI / Azure OpenAI / Anthropic / DeepSeek).

Construye el cliente segun `config/config.yaml` (`llm.provider`, `llm.reasoning_model`) y
las claves de `.env`, hace una llamada de prueba en modo JSON e imprime la respuesta y el
consumo de tokens. Si no hay clave/SDK, el factory cae a `MockLLMClient` y el script lo
avisa.

Requisitos:
- `pip install -e .[llm]` (paquete `openai`/`anthropic`) y `python-dotenv`.
- En `.env`: la clave del proveedor elegido:
    OPENAI_API_KEY        para openai
    ANTHROPIC_API_KEY     para anthropic
    AZURE_OPENAI_*        para azure_openai
    DEEPSEEK_API_KEY      para deepseek

Configuracion en config/config.yaml para DeepSeek:
    llm:
      provider: deepseek
      reasoning_model: deepseek-reasoner   # o deepseek-chat

Uso:
    python scripts/test_llm.py
    python scripts/test_llm.py "Resume en JSON {\"resumen\": \"...\"} el estado del IBEX35"
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Permite ejecutar el script directamente (anade la raiz del proyecto al path).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.graph.factory import build_llm_client  # noqa: E402
from src.llm.mock import MockLLMClient  # noqa: E402
from src.utils.config import get_config  # noqa: E402
from src.utils.env import load_env  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("test_llm")

_DEFAULT_USER = 'Responde solo con este JSON exacto: {"ok": true, "proveedor": "<tu proveedor>"}'


async def main() -> int:
    load_env()

    config = get_config()
    llm_cfg = config.get("llm", {})
    logger.info(
        "Config LLM: provider=%s, model=%s, engine=%s",
        llm_cfg.get("provider"),
        llm_cfg.get("reasoning_model"),
        llm_cfg.get("evaluation_engine"),
    )

    client = build_llm_client(config)
    if isinstance(client, MockLLMClient):
        logger.error(
            "Se ha construido MockLLMClient: falta la API key, el SDK o (en Azure) el "
            "endpoint. Revisa .env y config/config.yaml (llm.provider)."
        )
        return 1

    logger.info("Cliente real: %s (model=%s)", type(client).__name__, client.model)

    user = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_USER
    system = "Eres un asistente de prueba. Responde siempre con JSON valido."

    logger.info("Enviando prompt de prueba ...")
    response = await client.complete(system=system, user=user, json_mode=True, max_tokens=256)

    print("\n--- Respuesta ---")
    print(response.text)
    print("--- Uso ---")
    print(
        f"model={response.model} | prompt={response.usage.prompt_tokens} "
        f"completion={response.usage.completion_tokens} total={response.usage.total_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
