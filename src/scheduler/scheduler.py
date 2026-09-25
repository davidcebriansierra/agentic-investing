"""Scheduler de agentes (spec v2.0, seccion 4).

`SimpleScheduler` registra trabajos con su intervalo (en minutos) y permite dispararlos
manualmente (util en tests y shadow mode). `build_apscheduler()` crea un
`AsyncIOScheduler` real cuando el extra `infra` esta disponible.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time as dtime

logger = logging.getLogger("agentic.scheduler")

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


def _parse_hhmm(value: str) -> dtime:
    """Convierte 'HH:MM' en un datetime.time."""
    hours, minutes = value.strip().split(":")
    return dtime(int(hours), int(minutes))


def _now_in_timezone(tz_name: str) -> datetime:
    """Hora actual en la zona indicada.

    Usa `zoneinfo` (stdlib). En Windows la base de datos IANA la aporta el paquete
    `tzdata`; si no esta disponible, degrada a la hora local del sistema (naive) con un
    aviso. Como la maquina ya suele estar en la zona deseada, el fallback es razonable.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception as exc:  # noqa: BLE001 - ZoneInfoNotFoundError u otros
        logger.warning(
            "No se pudo cargar la zona horaria '%s' (%s); usando la hora local del "
            "sistema. Instala 'tzdata' para un manejo correcto de DST en Windows.",
            tz_name, exc,
        )
        return datetime.now()


def within_trading_window(config: dict | None, now: datetime | None = None) -> bool:
    """Indica si el instante actual cae dentro de la ventana operativa configurada.

    Config esperada bajo la clave `trading_window`:
        enabled: bool          # si False -> siempre True (sin restriccion horaria)
        timezone: str          # p.ej. "Europe/Madrid"
        start: "HH:MM"         # inicio de la ventana (inclusive)
        end: "HH:MM"           # fin de la ventana (exclusivo)
        weekdays_only: bool    # si True, solo de lunes a viernes

    `now` permite inyectar el instante (para tests); si es None se calcula en la zona
    configurada. Soporta ventanas que cruzan medianoche (start > end).
    """
    window = (config or {}).get("trading_window") or {}
    if not window.get("enabled", False):
        return True

    if now is None:
        now = _now_in_timezone(str(window.get("timezone", "Europe/Madrid")))

    if window.get("weekdays_only", True) and now.weekday() >= 5:  # 5=sabado, 6=domingo
        return False

    start = _parse_hhmm(str(window.get("start", "08:30")))
    end = _parse_hhmm(str(window.get("end", "22:00")))
    current = now.time()
    if start <= end:
        return start <= current < end
    # Ventana que cruza medianoche (p.ej. 22:00 -> 06:00).
    return current >= start or current < end
