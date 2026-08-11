"""Modulo de backtesting (spec v2.0, seccion 10).

Motor en Python puro (sin Backtrader/vectorbt) que reproduce histórico OHLCV, evalua
señales con el pipeline determinístico y simula bracket orders para calcular los KPIs
de la seccion 11.
"""
from src.backtest.data import Bar, load_bars_csv, synthetic_series
from src.backtest.engine import BacktestConfig, Backtester, BacktestTrade
from src.backtest.metrics import BacktestResult, compute_metrics
from src.backtest.strategy import SmaCrossStrategy, Strategy

__all__ = [
    "Backtester",
    "BacktestConfig",
    "BacktestResult",
    "BacktestTrade",
    "Bar",
    "SmaCrossStrategy",
    "Strategy",
    "compute_metrics",
    "load_bars_csv",
    "synthetic_series",
]
