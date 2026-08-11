"""Scheduler de agentes (spec v2.0, seccion 4).

`SimpleScheduler` registra trabajos con su intervalo (en minutos) y permite dispararlos
manualmente (util en tests y shadow mode). `build_apscheduler()` crea un
`AsyncIOScheduler` real cuando el extra `infra` esta disponible.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

JobFn = Callable[[], Awaitable[None]] | Callable[[], None]


@dataclass
class Job:
    job_id: str
    interval_minutes: float
    func: JobFn


class SimpleScheduler:
    """Registro de trabajos periodicos sin dependencias externas."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def add_job(self, job_id: str, interval_minutes: float, func: JobFn) -> Job:
        if job_id in self._jobs:
            raise ValueError(f"Job duplicado: {job_id}")
        job = Job(job_id=job_id, interval_minutes=interval_minutes, func=func)
        self._jobs[job_id] = job
        return job

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job:
        return self._jobs[job_id]

    async def run_job(self, job_id: str) -> None:
        """Ejecuta un trabajo una vez (soporta funciones sync y async)."""
        result = self._jobs[job_id].func()
        if hasattr(result, "__await__"):
            await result  # type: ignore[func-returns-value]

    def register_searcher(self, searcher, runner: JobFn) -> Job:
        """Atajo: registra un buscador usando su intervalo declarado."""
        return self.add_job(
            job_id=f"searcher:{searcher.source.value}",
            interval_minutes=searcher.interval_minutes,
            func=runner,
        )


def build_apscheduler():  # pragma: no cover - requiere el extra infra
    """Crea un AsyncIOScheduler real (APScheduler)."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:
        raise ImportError(
            "APScheduler no esta instalado. Instala el extra: pip install -e .[infra]"
        ) from exc
    return AsyncIOScheduler()
