"""Tests del scheduler simple."""
from __future__ import annotations

import pytest

from src.agents.searchers import TechnicalSearcher
from src.scheduler.scheduler import SimpleScheduler


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
