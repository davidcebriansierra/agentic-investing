"""Tests del motor de backtesting y la estrategia SMA."""
from __future__ import annotations

from datetime import date, timedelta

from src.backtest.data import Bar
from src.backtest.engine import BacktestConfig, Backtester
from src.backtest.strategy import SmaCrossStrategy, Strategy
from src.schemas.enums import AgentSource, Direction, Exchange
from src.schemas.opportunity import Opportunity


def _bar(i: int, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(day=date(2024, 1, 1) + timedelta(days=i), open=o, high=h, low=low, close=c)


class _FixedStrategy(Strategy):
    """Devuelve una unica oportunidad LONG en el indice indicado."""

    def __init__(self, at_index: int) -> None:
        self.at_index = at_index

    def generate(self, ticker, exchange, bars):
        if len(bars) - 1 != self.at_index:
            return None
        entry = bars[-1].close
        return Opportunity(
            agent_source=AgentSource.TECHNICAL,
            ticker=ticker,
            exchange=exchange,
            direction=Direction.LONG,
            entry_price=entry,
            take_profit=entry * 1.02,
            stop_loss=entry * 0.99,
            position_size_pct=0.05,
            estimated_win_probability=0.6,
            risk_reward_ratio=2.0,
            justification="fixed",
        )


def test_engine_simulates_winning_trade():
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # señal: entry 100, tp 102, sl 99
        _bar(2, 100, 103, 100, 102.5),    # toca tp 102
        _bar(3, 102, 104, 101, 103),
    ]
    bt = Backtester(
        strategy=_FixedStrategy(at_index=1),
        config=BacktestConfig(holding_bars=2, use_pipeline=False, commission_per_trade=1.0),
    )
    report = bt.run("IBE.MC", bars, exchange=Exchange.BME)
    assert report.result.n_trades == 1
    trade = report.trades[0]
    assert trade.outcome == "win"
    assert trade.exit_price == 102.0  # take-profit
    # qty = floor(100000*0.05/100)=50 ; pnl=(102-100)*50-1=99
    assert trade.quantity == 50
    assert trade.pnl == 99.0
    assert report.result.final_equity == 100_099.0


def test_engine_stop_loss_first_on_ambiguous_bar():
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # señal: entry 100, tp 102, sl 99
        _bar(2, 100, 103, 98, 100),       # toca SL(99) y TP(102) -> peor caso: SL
    ]
    bt = Backtester(
        strategy=_FixedStrategy(at_index=1),
        config=BacktestConfig(holding_bars=1, use_pipeline=False, commission_per_trade=0.0),
    )
    report = bt.run("IBE.MC", bars)
    trade = report.trades[0]
    assert trade.exit_price == 99.0
    assert trade.outcome == "loss"


def test_sma_strategy_generates_long_on_cross_up():
    strat = SmaCrossStrategy(short=3, long=6, sl_pct=0.01, rr=2.0)
    closes = [100, 100, 100, 100, 100, 100, 100, 101, 102, 103, 104, 105]
    bars = [_bar(i, c, c + 0.5, c - 0.5, float(c)) for i, c in enumerate(closes)]
    signals = [
        strat.generate("IBE.MC", Exchange.BME, bars[: i + 1]) for i in range(len(bars))
    ]
    longs = [s for s in signals if s is not None and s.direction == Direction.LONG]
    assert len(longs) >= 1
    opp = longs[0]
    assert opp.computed_risk_reward >= 2.0 - 1e-9
