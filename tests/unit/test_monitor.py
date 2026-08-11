"""Tests del agente de Monitorizacion."""
from __future__ import annotations

from src.agents.monitor import Monitor
from src.governance.kill_switch import KillSwitch
from src.schemas.enums import Direction
from src.schemas.monitoring import MonitoringEventType, Severity
from src.schemas.portfolio import Portfolio, Position


def _portfolio(with_stop=True) -> Portfolio:
    return Portfolio(
        total_equity=100_000,
        cash=50_000,
        positions=[
            Position(
                ticker="IBE.MC",
                direction=Direction.LONG,
                quantity=1000,
                avg_price=12.0,
                market_price=11.5,
                stop_loss=11.0 if with_stop else None,
            )
        ],
    )


def test_review_pnl_negative_is_warning():
    monitor = Monitor()
    event = monitor.review_pnl(_portfolio())
    assert event.event_type == MonitoringEventType.PNL_REVIEW
    assert event.severity == Severity.WARNING  # market_price < avg_price
    assert event.data["unrealized_pnl"] < 0


def test_check_stop_losses_flags_missing():
    monitor = Monitor()
    events = monitor.check_stop_losses(_portfolio(with_stop=False))
    assert len(events) == 1
    assert events[0].event_type == MonitoringEventType.MISSING_STOP_LOSS
    assert events[0].severity == Severity.CRITICAL


def test_check_stop_losses_ok_when_present():
    monitor = Monitor()
    events = monitor.check_stop_losses(_portfolio(with_stop=True))
    assert events == []


def test_evaluate_kill_switch_triggers_on_drawdown():
    ks = KillSwitch()
    monitor = Monitor(kill_switch=ks)
    event = monitor.evaluate_kill_switch(intraday_drawdown_pct=4.0, consecutive_losses=0)
    assert event is not None
    assert event.event_type == MonitoringEventType.KILL_SWITCH_TRIGGERED
    assert ks.active


def test_evaluate_kill_switch_none_within_limits():
    monitor = Monitor()
    event = monitor.evaluate_kill_switch(intraday_drawdown_pct=1.0, consecutive_losses=1)
    assert event is None


def test_intraday_drawdown_calculation():
    assert Monitor.intraday_drawdown_pct(100_000, 97_000) == 3.0
    assert Monitor.intraday_drawdown_pct(100_000, 101_000) == 0.0
