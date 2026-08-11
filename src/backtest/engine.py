"""Motor de backtesting (spec v2.0, seccion 10).

Reproduce barra a barra una serie OHLCV, genera señales con una estrategia, las pasa
(opcionalmente) por el pipeline determinístico de decision y simula bracket orders
intradía/multidía sobre las barras siguientes. Calcula los KPIs de la seccion 11.

Reglas de simulacion conservadoras:
- Entrada al cierre de la barra de la señal.
- En cada barra de la ventana de holding, si la barra toca tanto SL como TP, se asume
  que se ejecuta PRIMERO el stop-loss (peor caso).
- Si no se toca ningun nivel dentro de la ventana, se cierra al cierre de la ultima
  barra de la ventana.
- Una sola posicion abierta a la vez (sin solapamiento).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.backtest.data import Bar
from src.backtest.metrics import BacktestResult, compute_metrics
from src.backtest.strategy import Strategy
from src.graph.pipeline import DecisionPipeline
from src.schemas.enums import Direction, DecisionType, Exchange
from src.schemas.opportunity import Opportunity
from src.schemas.portfolio import Portfolio


@dataclass
class BacktestTrade:
    ticker: str
    direction: Direction
    entry_day: date
    exit_day: date
    entry_price: float
    exit_price: float
    quantity: int
    return_pct: float
    pnl: float
    outcome: str  # "win" | "loss" | "flat"


@dataclass
class BacktestConfig:
    initial_equity: float = 100_000.0
    holding_bars: int = 1          # ventana de holding (1 = intradía siguiente barra)
    commission_per_trade: float = 1.0
    use_pipeline: bool = True       # gatear señales con el pipeline determinístico
    periods_per_year: int = 252


@dataclass
class BacktestReport:
    result: BacktestResult
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


class Backtester:
    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig | None = None,
        pipeline: DecisionPipeline | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.pipeline = pipeline or DecisionPipeline()

    def run(
        self,
        ticker: str,
        bars: list[Bar],
        exchange: Exchange = Exchange.BME,
    ) -> BacktestReport:
        equity = self.config.initial_equity
        trades: list[BacktestTrade] = []
        equity_curve: list[float] = []
        i = 0
        n = len(bars)

        while i < n:
            window = bars[: i + 1]
            opp = self.strategy.generate(ticker, exchange, window)
            if opp is not None and self._accepts(opp, equity):
                trade = self._simulate_trade(opp, bars, i, equity)
                if trade is not None:
                    equity += trade.pnl
                    trades.append(trade)
                    equity_curve.append(equity)
                    # Avanzar mas alla de la ventana de holding (sin solapar).
                    i += self.config.holding_bars + 1
                    continue
            i += 1

        returns = [t.return_pct for t in trades]
        result = compute_metrics(
            trade_returns_pct=returns,
            equity_curve=equity_curve,
            initial_equity=self.config.initial_equity,
            periods_per_year=self.config.periods_per_year,
        )
        return BacktestReport(result=result, trades=trades, equity_curve=equity_curve)

    def _accepts(self, opp: Opportunity, equity: float) -> bool:
        if not self.config.use_pipeline:
            return True
        portfolio = Portfolio(total_equity=equity, cash=equity)
        states = self.pipeline.run([opp], portfolio)
        decision = states[0].decision
        return decision is not None and decision.decision == DecisionType.OPERATE

    def _simulate_trade(
        self,
        opp: Opportunity,
        bars: list[Bar],
        signal_index: int,
        equity: float,
    ) -> BacktestTrade | None:
        entry = opp.entry_price
        notional = equity * opp.position_size_pct
        quantity = int(notional // entry)
        if quantity <= 0:
            return None

        is_long = opp.direction == Direction.LONG
        window = bars[signal_index + 1 : signal_index + 1 + self.config.holding_bars]
        if not window:
            return None

        exit_price = window[-1].close
        exit_day = window[-1].day
        for bar in window:
            hit_sl = bar.low <= opp.stop_loss if is_long else bar.high >= opp.stop_loss
            hit_tp = bar.high >= opp.take_profit if is_long else bar.low <= opp.take_profit
            if hit_sl:  # peor caso: stop primero
                exit_price = opp.stop_loss
                exit_day = bar.day
                break
            if hit_tp:
                exit_price = opp.take_profit
                exit_day = bar.day
                break

        sign = 1 if is_long else -1
        gross = sign * (exit_price - entry) * quantity
        pnl = gross - self.config.commission_per_trade
        return_pct = pnl / notional * 100 if notional else 0.0
        outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"

        return BacktestTrade(
            ticker=opp.ticker,
            direction=opp.direction,
            entry_day=bars[signal_index].day,
            exit_day=exit_day,
            entry_price=entry,
            exit_price=round(exit_price, 4),
            quantity=quantity,
            return_pct=round(return_pct, 4),
            pnl=round(pnl, 2),
            outcome=outcome,
        )
