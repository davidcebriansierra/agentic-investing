"""Logging de fichero rotativo compartido para todos los agentes del pipeline.

Genera un fichero JSON-lines por agente en ``AGENTS_LOG_DIR`` (por defecto logs/agents/).
Cada entrada tiene un campo ``ts`` (timestamp UTC ISO-8601) y ``event``.

Se importa desde cualquier agente con::

    from src.agents._agent_logger import log_entry
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path

_AGENTS_LOG_DIR = Path(os.getenv("AGENTS_LOG_DIR", "logs/agents"))


def _get_agent_logger(name: str) -> logging.Logger:
    log = logging.getLogger(f"agentic.agents.file.{name}")
    if log.handlers:
        return log
    _AGENTS_LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        _AGENTS_LOG_DIR / f"{name}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log


def log_entry(agent_name: str, entry: dict) -> None:
    """Escribe una entrada JSON-lines en el fichero del agente indicado."""
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _get_agent_logger(agent_name).debug(
            json.dumps(entry, ensure_ascii=False, default=str)
        )
    except Exception:  # noqa: BLE001 - el logging nunca debe romper el flujo
        pass
