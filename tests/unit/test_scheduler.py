"""Tests del scheduler simple."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.agents.searchers import TechnicalSearcher
from src.scheduler.scheduler import SimpleScheduler, within_trading_window

# Ventana L-V 08:30-22:00 (mismos valores que config.yaml).
_WINDOW_CFG = {
    "trading_window": {
        "enabled": True,
        "timezone": "Europe/Madrid",
        "start": "08:30",
        "end": "22:00",
        "weekdays_only": True,
    }
}

# Fechas ancla con dia de la semana conocido (2024-01-01 fue lunes).
_MONDAY = datetime(2024, 1, 1)     # weekday 0
_TUESDAY = datetime(2024, 1, 2)    # weekday 1
_FRIDAY = datetime(2024, 1, 5)     # weekday 4
_SATURDAY = datetime(2024, 1, 6)   # weekday 5
_SUNDAY = datetime(2024, 1, 7)     # weekday 6


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute)


def test_add_and_list_jobs():
    sched = SimpleScheduler()
    sched.add_job("j1", 5, lambda: None)
    sched.add_job("j2", 15, lambda: None)
    assert {j.job_id for j in sched.jobs()} == {"j1", "j2"}
    assert sched.get("j1").interval_minutes == 5


def test_duplicate_job_raises():
    sched = SimpleScheduler()
    sched.add_job("j1", 5, lambda: None)
    with pytest.raises(ValueError):
        sched.add_job("j1", 10, lambda: None)


async def test_run_sync_job():
    sched = SimpleScheduler()
    calls = []
    sched.add_job("j1", 5, lambda: calls.append(1))
    await sched.run_job("j1")
    assert calls == [1]


async def test_run_async_job():
    sched = SimpleScheduler()
    calls = []

    async def work():
        calls.append("async")

    sched.add_job("j1", 5, work)
    await sched.run_job("j1")
    assert calls == ["async"]


def test_register_searcher_uses_interval():
    sched = SimpleScheduler()
    searcher = TechnicalSearcher(interval_minutes=10)
    job = sched.register_searcher(searcher, lambda: None)
    assert job.job_id == "searcher:technical"
    assert job.interval_minutes == 10


# ---------- Ventana operativa (within_trading_window) ----------

def test_window_disabled_is_always_open():
    cfg = {"trading_window": {"enabled": False}}
    # Incluso sabado de madrugada esta "abierto" si la ventana esta desactivada.
    assert within_trading_window(cfg, now=_at(_SATURDAY, 3)) is True


def test_window_missing_config_is_always_open():
    assert within_trading_window({}, now=_at(_SATURDAY, 3)) is True
    assert within_trading_window(None, now=_at(_SATURDAY, 3)) is True


def test_window_weekday_within_hours_is_open():
    assert within_trading_window(_WINDOW_CFG, now=_at(_TUESDAY, 12)) is True


def test_window_start_is_inclusive():
    assert within_trading_window(_WINDOW_CFG, now=_at(_MONDAY, 8, 30)) is True


def test_window_before_start_is_closed():
    assert within_trading_window(_WINDOW_CFG, now=_at(_MONDAY, 8, 29)) is False


def test_window_end_is_exclusive():
    # 22:00 exacto ya esta fuera; 21:59 dentro.
    assert within_trading_window(_WINDOW_CFG, now=_at(_FRIDAY, 22, 0)) is False
    assert within_trading_window(_WINDOW_CFG, now=_at(_FRIDAY, 21, 59)) is True


def test_window_weekend_is_always_closed():
    # Fin de semana en pausa aunque la hora caiga dentro del rango diario.
    assert within_trading_window(_WINDOW_CFG, now=_at(_SATURDAY, 12)) is False
    assert within_trading_window(_WINDOW_CFG, now=_at(_SUNDAY, 12)) is False


def test_window_weekend_pause_boundaries():
    # De viernes 22:00 a lunes 08:30 todo en pausa.
    assert within_trading_window(_WINDOW_CFG, now=_at(_FRIDAY, 23)) is False
    assert within_trading_window(_WINDOW_CFG, now=_at(_MONDAY, 8, 29)) is False
    assert within_trading_window(_WINDOW_CFG, now=_at(_MONDAY, 8, 30)) is True


def test_window_overnight_wraps_midnight():
    cfg = {
        "trading_window": {
            "enabled": True,
            "start": "22:00",
            "end": "06:00",
            "weekdays_only": False,
        }
    }
    assert within_trading_window(cfg, now=_at(_TUESDAY, 23)) is True
    assert within_trading_window(cfg, now=_at(_TUESDAY, 5)) is True
    assert within_trading_window(cfg, now=_at(_TUESDAY, 12)) is False
