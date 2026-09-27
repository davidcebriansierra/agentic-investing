"""Tests de reconciliacion de fills (P&L realizado) y del modulo de simulacion."""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import pytest

from src.backtest.data import synthetic_series
from src.portfolio.realized import realized_pnl
from src.schemas.enums import Exchange
from src.schemas.order import Fill
from src.simulation import SimulationService, aggregate_daily_bars
from src.schemas.market import OHLCVBar

TODAY = datetime.now(timezone.utc).date()


def _fill(ticker="SAN.MC", side="BOT", qty=10, price=4.0, commission=0.0, day=None):
    ts = datetime.combine(day or TODAY, datetime.min.time(), tzinfo=timezone.utc)
    return Fill(
        ticker=ticker, side=side, quantity=qty, price=price,
        commission=commission, timestamp_utc=ts,
    )


# --- realized_pnl FIFO ---------------------------------------------------------

def test_realized_pnl_compra_y_venta_ganadora():
    fills = [_fill(side="BOT", price=4.0), _fill(side="SLD", price=4.2)]
    s = realized_pnl(fills, on_date=TODAY)
    assert s.gross_pnl == 2.0  # (4.2-4.0)*10
    assert s.closed_trades == 1
    assert s.realized_pnl == 2.0


def test_realized_pnl_descuenta_comisiones():
    fills = [
        _fill(side="BOT", price=4.0, commission=1.0),
        _fill(side="SLD", price=4.2, commission=1.0),
    ]
    s = realized_pnl(fills, on_date=TODAY)
    assert s.commissions == 2.0
    assert s.realized_pnl == 0.0  # 2.0 bruto - 2.0 comisiones


def test_realized_pnl_parcial_fifo():
    fills = [
        _fill(side="BOT", qty=10, price=4.0),
        _fill(side="BOT", qty=10, price=4.4),
        _fill(side="SLD", qty=15, price=4.5),
    ]
    s = realized_pnl(fills, on_date=TODAY)
    # FIFO: 10 lotes a 4.0 (+0.5*10=5) + 5 lotes a 4.4 (+0.1*5=0.5) = 5.5
    assert s.gross_pnl == 5.5


def test_realized_pnl_solo_fecha_pedida():
    other_day = TODAY - timedelta(days=1)
    fills = [
        _fill(side="BOT", price=4.0, day=other_day),
        _fill(side="SLD", price=4.2, day=other_day),
        _fill(side="BOT", price=4.0),
        _fill(side="SLD", price=4.1),
    ]
    s = realized_pnl(fills, on_date=TODAY)
    assert s.gross_pnl == 1.0  # solo los fills de hoy


def test_realized_pnl_compra_sin_cierre_no_realiza():
    fills = [_fill(side="BOT", price=4.0)]
    s = realized_pnl(fills, on_date=TODAY)
    assert s.gross_pnl == 0.0
    assert s.closed_trades == 0


# --- aggregate_daily_bars -------------------------------------------------------

def test_aggregate_daily_bars_agrupa_por_dia():
    base = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    intraday = [
        OHLCVBar(timestamp_utc=base, open=10, high=12, low=9, close=11, volume=100),
        OHLCVBar(timestamp_utc=base + timedelta(hours=2), open=11, high=13, low=10, close=12, volume=50),
        OHLCVBar(timestamp_utc=base + timedelta(days=1), open=12, high=12, low=11, close=11, volume=80),
    ]
    daily = aggregate_daily_bars(intraday)
    assert len(daily) == 2
    d0 = daily[0]
    assert d0.open == 10 and d0.high == 13 and d0.low == 9 and d0.close == 12
    assert d0.volume == 150


# --- SimulationService -----------------------------------------------------------

def test_run_sweep_ordena_por_sharpe_y_guarda_latest():
    bars = synthetic_series(120, trend=0.3, amplitude=1.0)
    service = SimulationService()
    grid = {"short": [5, 10], "long": [20], "sl_pct": [0.01], "rr": [2.0]}
    results = service.run_sweep(
        ["SAN.MC"], {"SAN.MC": bars}, Exchange.BME, grid,
    )
    assert len(results) == 2
    assert all(r.n_trades >= 0 for r in results)
    sharpes = [r.sharpe_annualized for r in results]
    assert sharpes == sorted(sharpes, reverse=True)
    assert service.latest_summary() is not None
    assert "sharpe" in service.latest_summary()


def test_run_sweep_sin_barras_devuelve_vacio_de_tickers():
    service = SimulationService()
    grid = {"short": [5], "long": [20], "sl_pct": [0.01]}
    results = service.run_sweep(["SAN.MC"], {}, Exchange.BME, grid)
    assert len(results) == 1  # un escenario, cero resultados por ticker
    assert results[0].n_trades == 0


def test_run_sweep_ignora_params_invalidos():
    bars = synthetic_series(60)
    service = SimulationService()
    # short=30 > long=20 -> SmaCrossStrategy lanza ValueError -> escenario ignorado
    grid = {"short": [30, 5], "long": [20], "sl_pct": [0.01]}
    results = service.run_sweep(["A"], {"A": bars}, Exchange.BME, grid)
    assert len(results) == 1
    assert results[0].params["short"] == 5
