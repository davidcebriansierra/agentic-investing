"""Demo de backtesting: genera una serie sintetica y reporta los KPIs (spec §11).

Uso:
    python scripts/run_backtest.py

Para datos reales, sustituir `synthetic_series` por `load_bars_csv("ruta.csv")`.
"""
from __future__ import annotations

from src.backtest.data import synthetic_series
from src.backtest.engine import BacktestConfig, Backtester
from src.backtest.strategy import SmaCrossStrategy
from src.schemas.enums import Exchange


def main() -> None:
    # Serie con tendencia + oscilacion para generar cruces de medias.
    bars = synthetic_series(
        n=750, start=100.0, trend=0.05, amplitude=4.0, period=30
    )

    strategy = SmaCrossStrategy(short=5, long=20, sl_pct=0.01, rr=2.0)
    config = BacktestConfig(holding_bars=5, use_pipeline=False)
    bt = Backtester(strategy=strategy, config=config)

    report = bt.run("IBE.MC", bars, exchange=Exchange.BME)
    print("=== Backtest IBE.MC (sintetico) ===")
    print(report.result.summary())
    print(f"Operaciones simuladas: {len(report.trades)}")


if __name__ == "__main__":
    main()
