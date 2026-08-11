"""Tests del Kill Switch."""
from __future__ import annotations

import pytest

from src.governance.kill_switch import KillSwitch, KillSwitchActiveError


def test_default_off():
    ks = KillSwitch()
    assert not ks.active


def test_manual_activation_and_guard():
    ks = KillSwitch()
    ks.activate("manual /kill")
    assert ks.active
    with pytest.raises(KillSwitchActiveError):
        ks.guard()


def test_trigger_on_drawdown():
    ks = KillSwitch()
    reason = ks.evaluate_triggers(intraday_drawdown_pct=3.5, consecutive_losses=0)
    assert reason is not None
    assert ks.active


def test_trigger_on_consecutive_losses():
    ks = KillSwitch()
    reason = ks.evaluate_triggers(intraday_drawdown_pct=0.0, consecutive_losses=5)
    assert reason is not None
    assert ks.active


def test_no_trigger_within_limits():
    ks = KillSwitch()
    reason = ks.evaluate_triggers(intraday_drawdown_pct=1.0, consecutive_losses=2)
    assert reason is None
    assert not ks.active
