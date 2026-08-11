"""Datos OHLCV para backtesting."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    """Barra OHLCV diaria."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def load_bars_csv(path: str | Path) -> list[Bar]:
    """Carga barras desde un CSV con columnas: date,open,high,low,close[,volume].

    La fecha debe estar en formato ISO (YYYY-MM-DD).
    """
    bars: list[Bar] = []
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append(
                Bar(
                    day=date.fromisoformat(row["date"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0) or 0),
                )
            )
    return bars


def synthetic_series(
    n: int,
    start: float = 100.0,
    trend: float = 0.0,
    amplitude: float = 0.0,
    period: int = 20,
    start_day: date | None = None,
    daily_range_pct: float = 0.01,
) -> list[Bar]:
    """Genera una serie OHLCV determinística (tendencia + componente sinusoidal).

    Util para tests reproducibles sin datos externos.
    """
    day0 = start_day or date(2024, 1, 1)
    bars: list[Bar] = []
    for i in range(n):
        close = start + trend * i + amplitude * math.sin(2 * math.pi * i / period)
        open_ = close - trend * 0.5
        high = max(open_, close) * (1 + daily_range_pct)
        low = min(open_, close) * (1 - daily_range_pct)
        bars.append(
            Bar(
                day=day0 + timedelta(days=i),
                open=round(open_, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=1_000_000,
            )
        )
    return bars
