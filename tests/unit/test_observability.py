"""Tests de la capa de observabilidad y del ensamblado de la app."""
from __future__ import annotations

from src.app.main import Application, build_searchers
from src.observability.metrics import Metrics, get_metrics


def test_metrics_singleton():
    assert get_metrics() is get_metrics()


def test_metrics_counters_are_callable_noop_safe():
    m = Metrics()
    # No debe lanzar aunque prometheus_client no este instalado (no-op).
    m.opportunities_total.labels(agent_source="technical").inc()
    m.decisions_total.labels(decision="OPERATE").inc()
    m.orders_total.labels(status="FILLED").inc()
    m.risk_rejections_total.inc()
    m.kill_switch_active.set(1)
    m.signal_to_order_seconds.observe(0.5)


def test_build_searchers_returns_six():
    searchers = build_searchers()
    assert len(searchers) == 6
    sources = {s.source.value for s in searchers}
    assert sources == {
        "technical",
        "fundamental",
        "news",
        "social",
        "cross_market",
        "premarket",
    }


def test_application_registers_jobs():
    app = Application()
    app._register_jobs()
    assert len(app.scheduler.jobs()) == 6
