"""Modulo de simulacion de estrategias (reutiliza el motor de backtest).

Envuelve `Backtester` + `BacktestResult` en una capa de servicio que:

- Define **escenarios** (combinaciones de parametros de estrategia) desde config.
- Ejecuta un **barrido de parametros** (`param grid`) sobre un conjunto de tickers
  con barras OHLCV y ordena los resultados por Sharpe.
- Agrega barras intradia (`OHLCVBar` del market data) a barras diarias (`Bar` del
  motor de backtest) para simular con datos reales.
- Mantiene el ultimo resultado en memoria (`latest_summary()`) para que el
  asistente de cartera (advisor) lo incluya en su contexto y pueda explicarlo.

El modulo es declarativo: las estrategias son las de `src.backtest.strategy` y la
configuracion llega desde `config.yaml` (seccion `simulation:`).
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from src.backtest.data import Bar
from src.backtest.engine import BacktestConfig, BacktestReport, Backtester
from src.backtest.metrics import BacktestResult
from src.backtest.strategy import SmaCrossStrategy, Strategy
from src.schemas.enums import Exchange
from src.schemas.market import OHLCVBar

logger = logging.getLogger("agentic.simulation")


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

def aggregate_daily_bars(bars: Iterable[OHLCVBar]) -> list[Bar]:
    """Agrega barras intradia (`OHLCVBar`, timestamp_utc) a barras diarias (`Bar`).

    Las agrupa por fecha UTC: open = primera, high = max, low = min,
    close = ultima, volume = suma. Necesario porque el motor de backtest trabaja
    con barras diarias y el market data entrega barras intradia.
    """
    grouped: dict[date, list[OHLCVBar]] = {}
    for b in bars:
        grouped.setdefault(b.timestamp_utc.date(), []).append(b)
    out: list[Bar] = []
    for day in sorted(grouped):
        chunk = grouped[day]
        out.append(
            Bar(
                day=day,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Escenarios y resultado
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    """Resultado de un escenario de simulacion (un conjunto de parametros)."""

    params: dict
    ticker_results: dict[str, BacktestResult] = field(default_factory=dict)
    n_trades: int = 0
    total_return_pct: float = 0.0
    sharpe_annualized: float = 0.0
    max_drawdown_pct: float = 0.0
    winrate: float = 0.0

    def summary_line(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return (
            f"[{params}] trades={self.n_trades} winrate={self.winrate:.0%} "
            f"sharpe={self.sharpe_annualized:.2f} maxDD={self.max_drawdown_pct:.2f}% "
            f"ret={self.total_return_pct:.2f}%"
        )


def _aggregate_results(ticker_results: dict[str, BacktestResult]) -> tuple[float, float, float, float, int]:
    """Medias simples de los KPIs entre tickers (cada ticker con el mismo peso)."""
    n = len(ticker_results)
    if not n:
        return 0.0, 0.0, 0.0, 0.0, 0
    trades = sum(r.n_trades for r in ticker_results.values())
    return (
        sum(r.total_return_pct for r in ticker_results.values()) / n,
        sum(r.sharpe_annualized for r in ticker_results.values()) / n,
        max(r.max_drawdown_pct for r in ticker_results.values()),
        sum(r.winrate for r in ticker_results.values()) / n,
        trades,
    )


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------

@dataclass
class SimulationService:
    """Ejecuta escenarios de simulacion sobre el motor de backtest.

    `backtest_config` parametriza el motor (equity, comisiones, uso del pipeline
    determinista). El servicio guarda el ultimo barrido en `_latest` para que el
    advisor pueda exponerlo en el informe/chat.
    """

    backtest_config: BacktestConfig = field(default_factory=BacktestConfig)
    _latest: list[ScenarioResult] = field(default_factory=list)

    def run_sweep(
        self,
        tickers: list[str],
        bars_by_ticker: dict[str, list[Bar]],
        exchange: Exchange,
        param_grid: dict[str, list],
        strategy_factory=None,
    ) -> list[ScenarioResult]:
        """Ejecuta todas las combinaciones de `param_grid` y las ordena por Sharpe.

        `strategy_factory(params) -> Strategy` permite inyectar otra estrategia;
        por defecto `SmaCrossStrategy`.
        """
        factory = strategy_factory or (lambda params: SmaCrossStrategy(**params))
        keys = list(param_grid.keys())
        combos = [dict(zip(keys, combo)) for combo in itertools.product(*(param_grid[k] for k in keys))]
        scenarios: list[ScenarioResult] = []
        for params in combos:
            scenario = ScenarioResult(params=params)
            try:
                strategy = factory(params)
            except (TypeError, ValueError) as exc:
                logger.debug("Escenario ignorado (params invalidos %s): %s", params, exc)
                continue
            backtester = Backtester(strategy=strategy, config=self.backtest_config)
            for ticker in tickers:
                bars = bars_by_ticker.get(ticker)
                if not bars:
                    continue
                try:
                    report: BacktestReport = backtester.run(ticker, bars, exchange)
                except Exception as exc:  # noqa: BLE001 - un ticker fallido no aborta el barrido
                    logger.debug("Simulacion fallida %s params=%s: %s", ticker, params, exc)
                    continue
                scenario.ticker_results[ticker] = report.result
            ret, sharpe, max_dd, winrate, n_trades = _aggregate_results(scenario.ticker_results)
            scenario.total_return_pct = ret
            scenario.sharpe_annualized = sharpe
            scenario.max_drawdown_pct = max_dd
            scenario.winrate = winrate
            scenario.n_trades = n_trades
            scenarios.append(scenario)
        scenarios.sort(key=lambda s: s.sharpe_annualized, reverse=True)
        self._latest = scenarios
        logger.info(
            "Simulacion: %d escenarios sobre %d tickers. Mejor: %s",
            len(scenarios), len(tickers),
            scenarios[0].summary_line() if scenarios else "ninguno",
        )
        return scenarios

    def latest_summary(self, top: int = 3) -> str | None:
        """Resumen del ultimo barrido (top N escenarios) para el contexto del advisor."""
        if not self._latest:
            return None
        lines = [f"{i + 1}. {s.summary_line()}" for i, s in enumerate(self._latest[:top])]
        return "Ultimo barrido de simulacion (top escenarios por Sharpe):\n" + "\n".join(lines)
