"""Prueba manual del bot HITL de Telegram (spec v2.0, seccion 3.6).

Arranca el `TelegramHITLClient` real, envia una peticion de aprobacion de ejemplo a los
usuarios autorizados y espera la respuesta (Aprobar / Rechazar / Pausar 1h) con TTL.
Imprime la decision recibida y cierra el bot ordenadamente.

Requisitos:
- `pip install -e .[connectors]` (python-telegram-bot) y `python-dotenv`.
- En `.env`: `TELEGRAM_BOT_TOKEN` y `TELEGRAM_AUTHORIZED_USERS` (ids separados por comas).

Uso:
    python scripts/test_telegram_bot.py                 # ticker AAPL, TTL 120s (interactivo)
    python scripts/test_telegram_bot.py MSFT 60         # ticker y TTL personalizados
    python scripts/test_telegram_bot.py --timeout-test  # flujo TTL->no-operar (sin red)
    python scripts/test_telegram_bot.py --approve-test  # flujo APPROVE via Mock
    python scripts/test_telegram_bot.py --reject-test   # flujo REJECT via Mock
    python scripts/test_telegram_bot.py --pause-test    # flujo PAUSE_1H via Mock
    python scripts/test_telegram_bot.py --all-mock      # todos los flujos sin red
    python scripts/test_telegram_bot.py --edit-test     # flujo APPROVE con precios editados via Mock
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

from src.connectors.factory import build_hitl_client  # noqa: E402
from src.connectors.telegram_bot import MockHITLClient, TelegramHITLClient  # noqa: E402
from src.schemas.hitl import ApprovalDecision, ApprovalRequest, EditedPrices  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("test_telegram_bot")


def _load_env() -> None:
    """Carga variables de .env si python-dotenv esta disponible."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv no instalado: usando variables de entorno del sistema.")
        return
    env_path = _PROJECT_ROOT / ".env"
    load_dotenv(env_path)
    logger.info("Variables cargadas desde %s", env_path)


def _sample_request(ticker: str, ttl_seconds: int) -> ApprovalRequest:
    return ApprovalRequest(
        opportunity_id="test-opp-001",
        ticker=ticker,
        summary="Prueba manual del bot HITL: setup de ejemplo, sin operacion real.",
        final_score=0.82,
        expectancy_pct=1.35,
        risk_reward_ratio=2.4,
        entry_price=185.00,
        stop_loss=180.50,
        take_profit=195.00,
        estimated_win_probability=0.65,
        position_size_pct=0.05,
        ttl_seconds=ttl_seconds,
    )


async def _run_mock_test(decision: ApprovalDecision, label: str) -> int:
    """Valida un flujo concreto con MockHITLClient (sin red)."""
    client = MockHITLClient(default_decision=decision, responder="test_script")
    request = _sample_request("AAPL", ttl_seconds=5)
    response = await client.request_approval(request)
    ok = response.decision == decision
    expected_approved = decision == ApprovalDecision.APPROVE
    ok = ok and (response.approved == expected_approved)
    status = "OK" if ok else "FAIL"
    logger.info(
        "[%s] %s: decision=%s | aprobado=%s | por=%s",
        status, label, response.decision.value, response.approved, response.responder or "-",
    )
    return 0 if ok else 1


async def _run_telegram_interactive(ticker: str, ttl: int) -> int:
    """Prueba interactiva real con TelegramHITLClient."""
    client = build_hitl_client()
    if not isinstance(client, TelegramHITLClient):
        logger.error(
            "El cliente HITL no es de Telegram (es %s). Revisa TELEGRAM_BOT_TOKEN y "
            "TELEGRAM_AUTHORIZED_USERS en .env.",
            type(client).__name__,
        )
        return 1
    logger.info("Arrancando el bot de Telegram ...")
    await client.start()
    try:
        request = _sample_request(ticker, ttl)
        logger.info(
            "Enviando aprobacion de prueba para %s (TTL %ds). Responde en Telegram ...",
            ticker, ttl,
        )
        response = await client.request_approval(request)
        logger.info(
            "Respuesta: decision=%s | aprobado=%s | por=%s",
            response.decision.value, response.approved, response.responder or "-",
        )
    finally:
        logger.info("Deteniendo el bot ...")
        await client.stop()
    return 0


async def main() -> int:
    args = sys.argv[1:]

    if "--timeout-test" in args:
        logger.info("=== Test flujo TTL -> TIMEOUT -> no-operar ===")
        rc = await _run_mock_test(ApprovalDecision.TIMEOUT, "TIMEOUT -> no-operar")
        if rc == 0:
            logger.info("Flujo TTL validado: TIMEOUT -> approved=False -> no se opera.")
        return rc

    if "--approve-test" in args:
        return await _run_mock_test(ApprovalDecision.APPROVE, "APPROVE -> operar")

    if "--reject-test" in args:
        return await _run_mock_test(ApprovalDecision.REJECT, "REJECT -> no-operar")

    if "--pause-test" in args:
        return await _run_mock_test(ApprovalDecision.PAUSE_1H, "PAUSE_1H -> no-operar")

    if "--edit-test" in args:
        logger.info("=== Test flujo APPROVE con precios editados (sin red) ===")
        prices = EditedPrices(entry_price=186.00, stop_loss=181.50, take_profit=196.00)
        client = MockHITLClient(
            default_decision=ApprovalDecision.APPROVE,
            edited_prices=prices,
            responder="test_script",
        )
        request = _sample_request("AAPL", ttl_seconds=5)
        response = await client.request_approval(request)
        ok = (
            response.approved is True
            and response.edited_prices is not None
            and response.edited_prices.entry_price == 186.00
            and response.edited_prices.stop_loss == 181.50
            and response.edited_prices.take_profit == 196.00
        )
        status = "OK" if ok else "FAIL"
        logger.info(
            "[%s] APPROVE+edit: aprobado=%s | entrada=%.2f | SL=%.2f | TP=%.2f",
            status, response.approved,
            response.edited_prices.entry_price if response.edited_prices else 0,
            response.edited_prices.stop_loss if response.edited_prices else 0,
            response.edited_prices.take_profit if response.edited_prices else 0,
        )
        return 0 if ok else 1

    if "--all-mock" in args:
        logger.info("=== Validacion completa de todos los flujos HITL (sin red) ===")
        results = [
            await _run_mock_test(ApprovalDecision.TIMEOUT,  "TIMEOUT  -> no-operar"),
            await _run_mock_test(ApprovalDecision.APPROVE,  "APPROVE  -> operar"),
            await _run_mock_test(ApprovalDecision.REJECT,   "REJECT   -> no-operar"),
            await _run_mock_test(ApprovalDecision.PAUSE_1H, "PAUSE_1H -> no-operar"),
        ]
        passed = sum(1 for r in results if r == 0)
        logger.info("Resultado: %d/%d flujos OK", passed, len(results))
        return 0 if all(r == 0 for r in results) else 1

    _load_env()
    ticker = args[0] if args else "AAPL"
    ttl = int(args[1]) if len(args) > 1 else 120
    return await _run_telegram_interactive(ticker, ttl)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
