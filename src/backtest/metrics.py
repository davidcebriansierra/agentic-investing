"""Calculo de KPIs de backtesting (spec v2.0, seccion 11)."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class BacktestResult:
    n_trades: int
    wins: int
    losses: int
    winrate: float
    expectancy_pct: float          # retorno medio por operacion (%)
    profit_factor: float
    sharpe_annualized: float
    max_drawdown_pct: float
    calmar_ratio: float
    total_return_pct: float
    final_equity: float

    def summary(self) -> str:
        return (
            f"trades={self.n_trades} winrate={self.winrate:.1%} "
            f"expectancy={self.expectancy_pct:.3f}% PF={self.profit_factor:.2f} "
            f"sharpe={self.sharpe_annualized:.2f} maxDD={self.max_drawdown_pct:.2f}% "
            f"calmar={self.calmar_ratio:.2f} ret={self.total_return_pct:.2f}%"
        )


def _max_drawdown_pct(equity_curve: list[float]) -> float:
    peak = -math.inf
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return max_dd * 100


def _sharpe(returns: list[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def compute_metrics(
    trade_returns_pct: list[float],
    equity_curve: list[float],
    initial_equity: float,
    periods_per_year: int = 252,
) -> BacktestResult:
    """Calcula los KPIs a partir de los retornos por operacion y la curva de equity.

    - `trade_returns_pct`: retorno de cada operacion en porcentaje (p. ej. 1.5 = 1,5%).
    - `equity_curve`: valor de la cartera tras cada operacion (sin incluir el inicial).
    """
    n = len(trade_returns_pct)
    wins = sum(1 for r in trade_returns_pct if r > 0)
    losses = sum(1 for r in trade_returns_pct if r < 0)
    winrate = wins / n if n else 0.0
    expectancy = sum(trade_returns_pct) / n if n else 0.0

    gross_profit = sum(r for r in trade_returns_pct if r > 0)
    gross_loss = abs(sum(r for r in trade_returns_pct if r < 0))
    if gross_loss == 0:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss

    full_curve = [initial_equity, *equity_curve]
    equity_returns = [
        (full_curve[i] - full_curve[i - 1]) / full_curve[i - 1]
        for i in range(1, len(full_curve))
        if full_curve[i - 1] > 0
    ]
    sharpe = _sharpe(equity_returns, periods_per_year)
    max_dd = _max_drawdown_pct(full_curve)

    final_equity = equity_curve[-1] if equity_curve else initial_equity
    total_return_pct = (
        (final_equity - initial_equity) / initial_equity * 100 if initial_equity else 0.0
    )

    if max_dd > 0:
        # Calmar aproximado: retorno total anualizado simple / max drawdown.
        years = max(n / periods_per_year, 1e-9)
        annual_return = total_return_pct / years
        calmar = annual_return / max_dd
    else:
        calmar = math.inf if total_return_pct > 0 else 0.0

    return BacktestResult(
        n_trades=n,
        wins=wins,
        losses=losses,
        winrate=winrate,
        expectancy_pct=expectancy,
        profit_factor=profit_factor,
        sharpe_annualized=sharpe,
        max_drawdown_pct=max_dd,
        calmar_ratio=calmar,
        total_return_pct=total_return_pct,
        final_equity=final_equity,
    )
