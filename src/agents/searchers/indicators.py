"""Calculo de indicadores tecnicos a partir de velas OHLCV (spec v2.0, seccion 3.1).

Usa ``pandas-ta`` si esta disponible; si su import falla (p. ej. incompatibilidad con
numpy 2.x) degrada a una implementacion pura en Python. En ambos casos devuelve el
**ultimo** valor de cada indicador en un dict plano listo para inyectar en el prompt.

Indicadores: last_close, change_pct (variacion en la ventana), SMA(20/50), EMA(12/26),
RSI(14), MACD (linea/senal/histograma), ATR(14) y maximos/minimos de 20 barras.
"""
from __future__ import annotations

import logging

from src.schemas.market import OHLCVBar

logger = logging.getLogger("agentic.searchers.indicators")


def compute_indicators(bars: list[OHLCVBar]) -> dict[str, float]:
    """Devuelve el ultimo valor de cada indicador; {} si no hay datos suficientes."""
    if len(bars) < 2:
        return {}
    try:
        return _compute_with_pandas_ta(bars)
    except Exception as exc:  # noqa: BLE001 - import/runtime de pandas_ta -> fallback
        logger.debug("pandas-ta no disponible/uso fallido (%s); uso fallback puro Python.", exc)
        return _compute_fallback(bars)


def _round_clean(values: dict[str, float]) -> dict[str, float]:
    return {k: round(float(v), 4) for k, v in values.items() if v is not None and _is_finite(v)}


def _is_finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


# --------------------------------------------------------------------------------------
# pandas-ta
# --------------------------------------------------------------------------------------
def _compute_with_pandas_ta(bars: list[OHLCVBar]) -> dict[str, float]:
    import pandas as pd
    import pandas_ta as ta  # noqa: F401 - se usa via accessor df.ta / funciones

    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )
    close = df["close"]
    out: dict[str, float] = {
        "last_close": float(close.iloc[-1]),
        "change_pct": float((close.iloc[-1] - close.iloc[0]) / close.iloc[0]),
        "high_20": float(df["high"].tail(20).max()),
        "low_20": float(df["low"].tail(20).min()),
    }

    def last(series) -> float | None:
        if series is None or len(series) == 0:
            return None
        val = series.iloc[-1]
        return None if val != val else float(val)  # descarta NaN

    out["sma_20"] = last(ta.sma(close, length=20))
    out["sma_50"] = last(ta.sma(close, length=50))
    out["ema_12"] = last(ta.ema(close, length=12))
    out["ema_26"] = last(ta.ema(close, length=26))
    out["rsi_14"] = last(ta.rsi(close, length=14))
    out["atr_14"] = last(ta.atr(df["high"], df["low"], df["close"], length=14))

    macd = ta.macd(close)
    if macd is not None and not macd.empty:
        cols = list(macd.columns)
        macd_col = next((c for c in cols if c.startswith("MACD_")), None)
        signal_col = next((c for c in cols if c.startswith("MACDs_")), None)
        hist_col = next((c for c in cols if c.startswith("MACDh_")), None)
        if macd_col:
            out["macd"] = last(macd[macd_col])
        if signal_col:
            out["macd_signal"] = last(macd[signal_col])
        if hist_col:
            out["macd_hist"] = last(macd[hist_col])
    return _round_clean(out)


# --------------------------------------------------------------------------------------
# Fallback puro Python
# --------------------------------------------------------------------------------------
def _compute_fallback(bars: list[OHLCVBar]) -> dict[str, float]:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    out: dict[str, float] = {
        "last_close": closes[-1],
        "change_pct": (closes[-1] - closes[0]) / closes[0],
        "high_20": max(highs[-20:]),
        "low_20": min(lows[-20:]),
    }
    out["sma_20"] = _sma(closes, 20)
    out["sma_50"] = _sma(closes, 50)
    out["ema_12"] = _ema(closes, 12)
    out["ema_26"] = _ema(closes, 26)
    out["rsi_14"] = _rsi(closes, 14)
    out["atr_14"] = _atr(highs, lows, closes, 14)
    macd, signal, hist = _macd(closes)
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = hist
    return _round_clean(out)


def _sma(values: list[float], length: int) -> float | None:
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def _ema(values: list[float], length: int) -> float | None:
    if len(values) < length:
        return None
    k = 2 / (length + 1)
    ema = sum(values[:length]) / length  # semilla: SMA inicial
    for price in values[length:]:
        ema = price * k + ema * (1 - k)
    return ema


def _ema_series(values: list[float], length: int) -> list[float] | None:
    if len(values) < length:
        return None
    k = 2 / (length + 1)
    ema = sum(values[:length]) / length
    series = [ema]
    for price in values[length:]:
        ema = price * k + ema * (1 - k)
        series.append(ema)
    return series


def _rsi(values: list[float], length: int) -> float | None:
    if len(values) <= length:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / length
    avg_loss = losses / length
    for i in range(length + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], length: int) -> float | None:
    if len(closes) <= length:
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < length:
        return None
    atr = sum(trs[:length]) / length
    for tr in trs[length:]:
        atr = (atr * (length - 1) + tr) / length
    return atr


def _macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    fast_series = _ema_series(values, fast)
    slow_series = _ema_series(values, slow)
    if fast_series is None or slow_series is None:
        return None, None, None
    # Alinea por el final (el EMA lento empieza mas tarde).
    n = min(len(fast_series), len(slow_series))
    macd_line = [fast_series[-n + i] - slow_series[-n + i] for i in range(n)]
    if len(macd_line) < signal:
        return (macd_line[-1] if macd_line else None), None, None
    signal_series = _ema_series(macd_line, signal)
    if signal_series is None:
        return macd_line[-1], None, None
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return macd_val, signal_val, macd_val - signal_val
