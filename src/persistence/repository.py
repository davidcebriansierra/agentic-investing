"""Repositorio de persistencia inmutable (spec v2.0, seccion 7.3).

Todas las tablas son INSERT-only: opportunities, evaluations, decisions, orders,
executions, monitoring_events, prompts_history, llm_traces.

Se define la interfaz `Repository` y una implementacion `InMemoryRepository` para
tests / shadow mode. La implementacion PostgreSQL/TimescaleDB vive en `postgres.py`.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from src.schemas.decision import Decision
from src.schemas.evaluation import Evaluation
from src.schemas.monitoring import MonitoringEvent
from src.schemas.opportunity import Opportunity
from src.schemas.order import ExecutionResult, Order

# Nombres canonicos de tablas (audit trail).
TABLES = (
    "opportunities",
    "evaluations",
    "decisions",
    "orders",
    "executions",
    "monitoring_events",
    "prompts_history",
    "llm_traces",
)


class Repository(Protocol):
    def insert(self, table: str, record: dict) -> None: ...

    def all(self, table: str) -> list[dict]: ...


class RepositoryHelpers:
    """Helpers tipados compartidos por todas las implementaciones de repositorio.

    Solo dependen de `insert(table, record)`, por lo que sirven tanto para la version en
    memoria como para la de PostgreSQL sin duplicar logica.
    """

    def insert(self, table: str, record: dict) -> None:  # implementado por la subclase
        raise NotImplementedError

    def save_opportunity(self, opp: Opportunity) -> None:
        self._save("opportunities", opp)

    def save_evaluation(self, ev: Evaluation) -> None:
        self._save("evaluations", ev)

    def save_decision(self, dec: Decision) -> None:
        self._save("decisions", dec)

    def save_order(self, order: Order) -> None:
        self._save("orders", order)

    def save_execution(self, result: ExecutionResult) -> None:
        self._save("executions", result)

    def save_monitoring_event(self, event: MonitoringEvent) -> None:
        self._save("monitoring_events", event)

    def save_prompt_history(self, name: str, version: str, prompt_hash: str, model: str) -> None:
        self.insert(
            "prompts_history",
            {"name": name, "version": version, "hash": prompt_hash, "model": model},
        )

    def save_llm_trace(self, request: dict, response: dict) -> None:
        self.insert("llm_traces", {"request": request, "response": response})

    def _save(self, table: str, model: BaseModel) -> None:
        self.insert(table, model.model_dump(mode="json"))


class InMemoryRepository(RepositoryHelpers):
    """Almacen append-only en memoria. Util para tests y shadow mode."""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict]] = {t: [] for t in TABLES}

    # --- API generica ---
    def insert(self, table: str, record: dict) -> None:
        if table not in self._tables:
            raise KeyError(f"Tabla desconocida: {table}")
        self._tables[table].append(record)

    def all(self, table: str) -> list[dict]:
        return list(self._tables[table])

    def count(self, table: str) -> int:
        return len(self._tables[table])
