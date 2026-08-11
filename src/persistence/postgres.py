"""Repositorio PostgreSQL 16 + TimescaleDB (spec v2.0, secciones 4 y 7.3).

Implementacion real append-only sobre psycopg 3. Import perezoso: solo requiere el
extra `infra` si se instancia. `init_schema()` crea el DDL; `insert`/`all`/`count`
operan sobre las 8 tablas del audit trail.
"""
from __future__ import annotations

import logging

from src.persistence.repository import TABLES, RepositoryHelpers

logger = logging.getLogger("agentic.persistence.postgres")

# Columna identificadora por tabla (las que tienen PK propia + payload JSONB).
_ID_COL = {
    "opportunities": "opportunity_id",
    "orders": "order_id_internal",
    "monitoring_events": "event_id",
}


# DDL de referencia para crear el audit trail (tablas inmutables, INSERT-only).
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id UUID PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id UUID PRIMARY KEY,
    opportunity_id UUID NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id UUID PRIMARY KEY,
    opportunity_id UUID NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS orders (
    order_id_internal UUID PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS executions (
    id BIGSERIAL PRIMARY KEY,
    order_id_internal UUID NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS monitoring_events (
    event_id UUID PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prompts_history (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    hash TEXT NOT NULL,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS llm_traces (
    id BIGSERIAL PRIMARY KEY,
    request JSONB NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB       NOT NULL,
    hash        TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_event_type_idx ON audit_log (event_type);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx         ON audit_log (ts DESC);
"""


class PostgresRepository(RepositoryHelpers):
    """Repositorio append-only sobre PostgreSQL/TimescaleDB (psycopg 3).

    Cada `insert` abre una conexion corta y hace commit (las tablas son inmutables y la
    frecuencia de escritura es baja). Hereda los helpers tipados (`save_opportunity`, etc.)
    de `RepositoryHelpers`, por lo que es un reemplazo directo de `InMemoryRepository`.
    """

    def __init__(self, dsn: str, connect_timeout: int = 5) -> None:
        try:  # pragma: no cover - depende del extra opcional
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "psycopg no esta instalado. Instala el extra: pip install -e .[infra]"
            ) from exc
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self._psycopg = psycopg
        self._Jsonb = Jsonb

    def _connect(self):  # pragma: no cover - requiere BD
        """Abre una conexion con timeout para no colgar si la BD no responde."""
        return self._psycopg.connect(self.dsn, connect_timeout=self.connect_timeout)

    def init_schema(self) -> None:  # pragma: no cover - requiere BD
        with self._connect() as conn:
            conn.execute(SCHEMA_DDL)
            conn.commit()

    def insert(self, table: str, record: dict) -> None:  # pragma: no cover - requiere BD
        if table not in TABLES:
            raise KeyError(f"Tabla desconocida: {table}")
        jsonb = self._Jsonb
        with self._connect() as conn:
            if table == "prompts_history":
                conn.execute(
                    "INSERT INTO prompts_history (name, version, hash, model) "
                    "VALUES (%s, %s, %s, %s)",
                    (record["name"], record["version"], record["hash"], record.get("model")),
                )
            elif table == "llm_traces":
                conn.execute(
                    "INSERT INTO llm_traces (request, response) VALUES (%s, %s)",
                    (jsonb(record["request"]), jsonb(record["response"])),
                )
            elif table in ("evaluations", "decisions"):
                id_col = "evaluation_id" if table == "evaluations" else "decision_id"
                conn.execute(
                    f"INSERT INTO {table} ({id_col}, opportunity_id, payload) "
                    "VALUES (%s, %s, %s)",
                    (record[id_col], record["opportunity_id"], jsonb(record)),
                )
            elif table == "executions":
                conn.execute(
                    "INSERT INTO executions (order_id_internal, payload) VALUES (%s, %s)",
                    (record.get("order_id_internal"), jsonb(record)),
                )
            else:  # opportunities, orders, monitoring_events
                id_col = _ID_COL[table]
                conn.execute(
                    f"INSERT INTO {table} ({id_col}, payload) VALUES (%s, %s)",
                    (record.get(id_col), jsonb(record)),
                )
            conn.commit()

    def all(self, table: str) -> list[dict]:  # pragma: no cover - requiere BD
        if table not in TABLES:
            raise KeyError(f"Tabla desconocida: {table}")
        with self._connect() as conn:
            if table == "prompts_history":
                rows = conn.execute(
                    "SELECT name, version, hash, model FROM prompts_history ORDER BY created_at"
                ).fetchall()
                return [
                    {"name": r[0], "version": r[1], "hash": r[2], "model": r[3]} for r in rows
                ]
            if table == "llm_traces":
                rows = conn.execute(
                    "SELECT request, response FROM llm_traces ORDER BY created_at"
                ).fetchall()
                return [{"request": r[0], "response": r[1]} for r in rows]
            rows = conn.execute(
                f"SELECT payload FROM {table} ORDER BY created_at"
            ).fetchall()
            return [r[0] for r in rows]

    def count(self, table: str) -> int:  # pragma: no cover - requiere BD
        if table not in TABLES:
            raise KeyError(f"Tabla desconocida: {table}")
        with self._connect() as conn:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
