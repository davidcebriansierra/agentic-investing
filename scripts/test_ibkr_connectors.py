"""Script de prueba para los conectores IBKR (lectura y escritura).

Requiere:
- IB Gateway o TWS corriendo y logueado (paper: puerto 4002, live: 4001)
- Variables de entorno: IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID_READ, IBKR_CLIENT_ID_WRITE, IBKR_ACCOUNT
- ib_async instalado: pip install -e .[connectors]

Uso:
    python scripts/test_ibkr_connectors.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Añadir la raíz del proyecto al path para poder importar `src`
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    """Carga el fichero .env de la raíz (usa python-dotenv si está; si no, parser simple)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        print(f"[aviso] No existe {env_path}; se usaran solo variables de la shell.")
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except ImportError:
        # Parser mínimo si no está instalado python-dotenv
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

from src.connectors.ibkr_read import IBKRMarketDataClient  # noqa: E402
from src.connectors.ibkr_write import IBKRBrokerClient  # noqa: E402
from src.schemas.enums import Exchange, OrderAction  # noqa: E402
from src.schemas.order import Order, OrderLeg  # noqa: E402


def get_env() -> dict[str, str]:
    """Lee variables de entorno para IBKR."""
    required = [
        "IBKR_HOST",
        "IBKR_PORT",
        "IBKR_CLIENT_ID_READ",
        "IBKR_CLIENT_ID_WRITE",
        "IBKR_ACCOUNT",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"Faltan variables de entorno: {', '.join(missing)}\n"
            "Definelas en el fichero .env de la raiz del proyecto o exportalas en la shell."
        )
    return {k: os.getenv(k) for k in required}


async def test_read():
    """Prueba de lectura: portfolio, quote, estado de mercado."""
    env = get_env()
    host = env["IBKR_HOST"]
    port = int(env["IBKR_PORT"])
    client_id = int(env["IBKR_CLIENT_ID_READ"])

    print("=== PRUEBA DE LECTURA (IBKRMarketDataClient) ===")
    client = IBKRMarketDataClient(host=host, port=port, client_id=client_id)

    try:
        print(f"Conectando a IB Gateway en {host}:{port} (client_id={client_id})...")
        await client.connect()
        print("✅ Conectado")

        print("\n--- Portfolio ---")
        portfolio = await client.get_portfolio()
        print(f"Total Equity: {portfolio.total_equity:,.2f}")
        print(f"Available Cash: {portfolio.cash:,.2f}")

        print("\n--- Quote (AAPL) ---")
        quote = await client.get_quote("AAPL")
        print(f"Ticker: {quote.ticker}")
        print(f"Exchange: {quote.exchange}")
        print(f"Last: {quote.last}")
        print(f"Volume: {quote.avg_volume_20d}")

        print("\n--- Estado de mercado ---")
        for ex in [Exchange.BME, Exchange.NYSE, Exchange.NASDAQ]:
            is_open = await client.is_market_open(ex)
            print(f"{ex.value}: {'ABIERTO' if is_open else 'CERRADO'}")

        print("\n--- Negociabilidad (AAPL) ---")
        tradable = await client.is_tradable_today("AAPL")
        print(f"AAPL es negociable hoy: {tradable}")

    except Exception as e:
        print(f"❌ Error en lectura: {e}")
        raise


async def test_write():
    """Prueba de escritura: orden bracket (SOLO EN PAPER)."""
    env = get_env()
    host = env["IBKR_HOST"]
    port = int(env["IBKR_PORT"])
    client_id = int(env["IBKR_CLIENT_ID_WRITE"])
    account = env["IBKR_ACCOUNT"]

    print("\n=== PRUEBA DE ESCRITURA (IBKRBrokerClient) ===")
    print("⚠️  AVISO: Esto enviara una orden REAL a IBKR.")
    print("   Asegurate de estar en cuenta PAPER (puerto 4002).")
    confirm = input("   Continuar? (s/N): ").strip().lower()
    if confirm != "s":
        print("Prueba cancelada.")
        return

    client = IBKRBrokerClient(host=host, port=port, client_id=client_id, account=account)

    try:
        print(f"Conectando a IB Gateway en {host}:{port} (client_id={client_id})...")
        await client.connect()
        print("✅ Conectado")

        print("\n--- Orden bracket (AAPL, LONG, 1 share) ---")
        order = Order(
            ticker="AAPL",
            action=OrderAction.BUY,
            quantity=1,
            entry=OrderLeg(type="LMT", price=150.0),  # Ajustar precio actual
            take_profit=OrderLeg(type="LMT", price=155.0),
            stop_loss=OrderLeg(type="STP", price=148.0),
            account=account,
        )

        result = await client.place_bracket_order(order)
        print(f"Order ID interno: {result.order_id_internal}")
        print(f"IBKR Order ID: {result.ibkr_order_id}")
        print(f"Estado: {result.status}")
        print(f"Fill price: {result.fill_price}")
        print(f"Fill quantity: {result.fill_quantity}")
        print(f"Comision: {result.commission}")
        print(f"Raw response: {result.raw_ibkr_response}")

    except Exception as e:
        print(f"❌ Error en escritura: {e}")
        raise


async def main():
    """Ejecuta pruebas de lectura y escritura."""
    print("### Test de conectores IBKR ###\n")
    try:
        await test_read()
        await test_write()
    except Exception as e:
        import traceback

        print("\n❌ La prueba fallo:")
        print(f"   {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
    print("\n✅ Pruebas finalizadas.")


if __name__ == "__main__":
    asyncio.run(main())
