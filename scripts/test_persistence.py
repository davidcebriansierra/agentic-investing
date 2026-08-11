"""Prueba manual de la capa de persistencia/mensajeria (spec v2.0, secciones 4 y 7.3).

Comprueba, segun lo que este configurado en `.env`:
- **PostgreSQL**: crea el esquema, inserta un registro de ejemplo y lo lee de vuelta.
- **Redis Streams**: publica, lee con grupo de consumidor y hace ack.
- **Qdrant**: crea la coleccion, hace upsert de un vector y una busqueda.

Cada bloque se salta con aviso si falta la URL/DSN o el SDK correspondiente.

Uso:
    python scripts/test_persistence.py
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path

# Permite ejecutar el script directamente (anade la raiz del proyecto al path).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.memory.vector_store import QdrantVectorStore, VectorRecord  # noqa: E402
from src.messaging.streams import RedisStreamBus  # noqa: E402
from src.persistence.postgres import PostgresRepository  # noqa: E402
from src.schemas.opportunity import Opportunity  # noqa: E402
from src.utils.env import load_env  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("test_persistence")


def _sample_opportunity() -> Opportunity:
    from src.schemas.enums import AgentSource, Direction, Exchange

    return Opportunity(
        ticker="AAPL",
        exchange=Exchange.NYSE,
        direction=Direction.LONG,
        entry_price=180.0,
        take_profit=195.0,
        stop_loss=175.0,
        estimated_win_probability=0.6,
        position_size_pct=0.02,
        risk_reward_ratio=3.0,
        agent_source=AgentSource.TECHNICAL,
        justification="prueba de persistencia",
    )


def test_postgres() -> None:
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        logger.warning("[postgres] Sin POSTGRES_DSN: omitido.")
        return
    try:
        repo = PostgresRepository(dsn)
        repo.init_schema()
        opp = _sample_opportunity()
        repo.save_opportunity(opp)
        rows = repo.all("opportunities")
        logger.info("[postgres] OK: %d filas en opportunities.", len(rows))
    except Exception as exc:  # noqa: BLE001
        logger.error("[postgres] FALLO: %s", exc)


def test_redis() -> None:
    url = os.getenv("REDIS_URL")
    if not url:
        logger.warning("[redis] Sin REDIS_URL: omitido.")
        return
    try:
        bus = RedisStreamBus(url)
        stream, group = f"test:{uuid.uuid4().hex[:8]}", "g1"
        msg_id = bus.publish(stream, {"hello": "world", "n": 1})
        msgs = bus.read(stream, group, count=10)
        for m in msgs:
            bus.ack(stream, group, m.id)
        logger.info(
            "[redis] OK: publicado %s, leidos %d, ack %d.", msg_id, len(msgs), len(msgs)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[redis] FALLO: %s", exc)


def test_qdrant() -> None:
    url = os.getenv("QDRANT_URL")
    if not url:
        logger.warning("[qdrant] Sin QDRANT_URL: omitido.")
        return
    try:
        dim = 8
        store = QdrantVectorStore(url=url, collection="test_smoke", dim=dim)
        store.upsert(VectorRecord(id="doc-1", vector=[0.1] * dim, payload={"t": "demo"}))
        hits = store.search([0.1] * dim, top_k=3)
        logger.info("[qdrant] OK: %d vecinos (top score=%.3f).",
                    len(hits), hits[0].score if hits else 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.error("[qdrant] FALLO: %s", exc)


def main() -> int:
    load_env()
    test_postgres()
    test_redis()
    test_qdrant()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
