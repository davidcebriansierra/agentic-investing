"""Tests de los KPIs de backtesting."""
from __future__ import annotations

import math

import pytest

from src.backtest.metrics import compute_metrics


def test_metrics_basic_counts_and_expectancy():
    returns = [2.0, -1.0, 2.0]
    equity = [102_000, 100_980, 102_999.6]
    res = compute_metrics(returns, equity, initial_equity=100_000)
    assert res.n_trades == 3
    assert res.wins == 2
    assert res.losses == 1
    assert res.winrate == pytest.approx(2 / 3)
    assert res.expectancy_pct == pytest.approx(1.0)
    assert res.profit_factor == pytest.approx(4.0)


def test_metrics_empty():
    res = compute_metrics([], [], initial_equity=100_000)
    assert res.n_trades == 0
    assert res.winrate == 0.0
    assert res.final_equity == 100_000


def test_metrics_profit_factor_infinite_when_no_losses():
    res = compute_metrics([1.0, 2.0], [101_000, 103_000], initial_equity=100_000)
    assert math.isinf(res.profit_factor)


def test_max_drawdown_detected():
    # Sube a 110k, cae a 99k -> drawdown desde pico 110k.
    res = compute_metrics(
        [10.0, -10.0], equity_curve=[110_000, 99_000], initial_equity=100_000
    )
    assert res.max_drawdown_pct == pytest.approx(10.0, abs=0.001)
