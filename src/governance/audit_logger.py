"""Audit trail (spec v2.0, seccion 7.3).

Registro inmutable (INSERT-only) de todo evento del ciclo de vida de una decision.
Dos implementaciones:

- ``AuditLogger``: memoria + append a fichero JSONL. Usada en tests / shadow mode.
- ``PostgresAuditLogger``: INSERT-only sobre la tabla ``audit_log`` en PostgreSQL.
  Import perezoso de psycopg 3; si no esta disponible degrada a ``AuditLogger``.

Usar ``build_audit_logger()`` para obtener la implementacion adecuada segun entorno.
Los campos sensibles se redactan en ambas implementaciones.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger("agentic.governance.audit")

_SENSITIVE_KEYS = {"api_key", "token", "secret", "password", "raw_llm_response"}

# DDL de la tabla de audit trail generica (INSERT-only, sin UPDATE/DELETE).
AUDIT_LOG_DDL = """
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


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _SENSITIVE_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _build_record(event_type: str, data: Any) -> dict:
    """Construye el registro canonico con hash de integridad."""
    record: dict = {
        "event_type": event_type,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "payload": data,
    }
    record["hash"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return record


class AuditLogger:
    """Implementacion local: memoria + append a fichero JSONL."""

    def __init__(self, sink_path: str | Path | None = None) -> None:
        self.sink_path = Path(sink_path) if sink_path else None
        self._events: list[dict] = []

    def log(self, event_type: str, payload: Any) -> dict:
        """Registra un evento inmutable. Devuelve el registro creado."""
        if isinstance(payload, BaseModel):
            data = payload.model_dump(mode="json")
        else:
            data = payload
        data = _redact(data)
        record = _build_record(event_type, data)

        self._events.append(record)
        if self.sink_path is not None:
            self.sink_path.parent.mkdir(parents=True, exist_ok=True)
            with self.sink_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        return record

    @property
    def events(self) -> list[dict]:
        return list(self._events)


class PostgresAuditLogger:
    """Implementacion de produccion: INSERT-only sobre PostgreSQL (psycopg 3).

    Cada llamada a ``log()`` abre una conexion corta y hace commit. Ademas
    mantiene una copia en memoria para consultas rapidas en la misma sesion.
    """

    def __init__(self, dsn: str, connect_timeout: int = 5) -> None:
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise ImportError(
                "psycopg no esta instalado. Instala el extra: pip install -e .[infra]"
            ) from exc
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self._psycopg = psycopg
        self._Jsonb = Jsonb
        self._events: list[dict] = []

    def init_schema(self) -> None:  # pragma: no cover - requiere BD
        """Crea la tabla audit_log si no existe. Llamar una vez al arrancar."""
        with self._psycopg.connect(self.dsn, connect_timeout=self.connect_timeout) as conn:
            conn.execute(AUDIT_LOG_DDL)
            conn.commit()
        logger.info("audit_log: schema verificado en PostgreSQL.")

    def log(self, event_type: str, payload: Any) -> dict:  # pragma: no cover - requiere BD
        """INSERT-only: escribe en PostgreSQL y mantiene copia en memoria."""
        if isinstance(payload, BaseModel):
            data = payload.model_dump(mode="json")
        else:
            data = payload
        data = _redact(data)
        record = _build_record(event_type, data)

        try:
            with self._psycopg.connect(
                self.dsn, connect_timeout=self.connect_timeout
            ) as conn:
                conn.execute(
                    "INSERT INTO audit_log (event_type, ts, payload, hash) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        record["event_type"],
                        record["timestamp_utc"],
                        self._Jsonb(record["payload"]),
                        record["hash"],
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - audit nunca debe tumbar el sistema
            logger.error("PostgresAuditLogger: fallo al escribir evento '%s': %s", event_type, exc)

        self._events.append(record)
        return record

    def query(
        self,
        event_type: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:  # pragma: no cover - requiere BD
        """Consulta eventos del audit trail. Solo para observabilidad/backoffice."""
        filters = []
        params: list[Any] = []
        if event_type:
            filters.append("event_type = %s")
            params.append(event_type)
        if since:
            filters.append("ts >= %s")
            params.append(since.isoformat())
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        params.append(limit)
        with self._psycopg.connect(self.dsn, connect_timeout=self.connect_timeout) as conn:
            rows = conn.execute(
                f"SELECT event_type, ts, payload, hash FROM audit_log "
                f"{where} ORDER BY ts DESC LIMIT %s",
                params,
            ).fetchall()
        return [
            {"event_type": r[0], "timestamp_utc": r[1].isoformat(), "payload": r[2], "hash": r[3]}
            for r in rows
        ]

    @property
    def events(self) -> list[dict]:
        return list(self._events)


def build_audit_logger(
    dsn: str | None = None,
    sink_path: str | Path | None = None,
) -> AuditLogger | PostgresAuditLogger:
    """Devuelve ``PostgresAuditLogger`` si hay DSN disponible; ``AuditLogger`` si no.

    El DSN se toma del parametro o de la variable de entorno ``POSTGRES_DSN``.
    Si psycopg no esta instalado, degrada a ``AuditLogger`` con aviso.
    """
    _dsn = dsn or os.getenv("POSTGRES_DSN", "")
    if _dsn:
        try:
            pg = PostgresAuditLogger(_dsn)
            pg.init_schema()
            logger.info("AuditLogger: usando PostgresAuditLogger (%s).", _dsn[:30] + "...")
            return pg
        except ImportError as exc:
            logger.warning(
                "psycopg no disponible (%s): usando AuditLogger local (JSONL).", exc
            )
        except Exception as exc:  # noqa: BLE001 - BD no accesible al arrancar
            logger.warning(
                "PostgreSQL no accesible (%s): usando AuditLogger local (JSONL).", exc
            )
    _sink = sink_path or Path("logs") / "audit.jsonl"
    logger.info("AuditLogger: usando implementacion local → %s", _sink)
    return AuditLogger(sink_path=_sink)
