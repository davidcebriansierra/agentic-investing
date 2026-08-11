"""Estrategias de generacion de señales para backtesting.

Una estrategia recibe la ventana de barras hasta el indice actual y devuelve una
Opportunity (o None). El backtester se encarga de pasarla por el pipeline y simular.
"""
from __future__ import annotations

import abc

from src.backtest.data import Bar
from src.schemas.enums import AgentSource, Direction, Exchange, HoldingPeriod
from src.schemas.opportunity import Opportunity


class Strategy(abc.ABC):
    @abc.abstractmethod
    def generate(self, ticker: str, exchange: Exchange, bars: list[Bar]) -> Opportunity | None:
        """Devuelve una oportunidad para la ultima barra de `bars`, o None."""
        raise NotImplementedError


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


class SmaCrossStrategy(Strategy):
    """Cruce de medias moviles simples.

    LONG cuando la SMA corta cruza por encima de la larga; SHORT cuando cruza por
    debajo. El take-profit y stop-loss se fijan con R/R >= 2 (tp_pct = 2 * sl_pct).
    """

    def __init__(
        self,
        short: int = 5,
        long: int = 20,
        sl_pct: float = 0.01,
        rr: float = 2.0,
        position_size_pct: float = 0.05,
        win_probability: float = 0.55,
        allow_short: bool = False,
    ) -> None:
        if short >= long:
            raise ValueError("short debe ser menor que long")
        self.short = short
        self.long = long
        self.sl_pct = sl_pct
        self.rr = rr
        self.position_size_pct = position_size_pct
        self.win_probability = win_probability
        self.allow_short = allow_short

    def generate(self, ticker: str, exchange: Exchange, bars: list[Bar]) -> Opportunity | None:
        if len(bars) < self.long + 1:
            return None
        closes = [b.close for b in bars]

        short_now = _sma(closes, self.short)
        long_now = _sma(closes, self.long)
        short_prev = _sma(closes[:-1], self.short)
        long_prev = _sma(closes[:-1], self.long)
        if None in (short_now, long_now, short_prev, long_prev):
            return None

        crossed_up = short_prev <= long_prev and short_now > long_now
        crossed_down = short_prev >= long_prev and short_now < long_now

        if crossed_up:
            direction = Direction.LONG
        elif crossed_down and self.allow_short:
            direction = Direction.SHORT
        else:
            return None

        entry = closes[-1]
        tp_pct = self.sl_pct * self.rr
        if direction == Direction.LONG:
            take_profit = entry * (1 + tp_pct)
            stop_loss = entry * (1 - self.sl_pct)
        else:
            take_profit = entry * (1 - tp_pct)
            stop_loss = entry * (1 + self.sl_pct)

        return Opportunity(
            agent_source=AgentSource.TECHNICAL,
            ticker=ticker,
            exchange=exchange,
            direction=direction,
            entry_price=round(entry, 4),
            take_profit=round(take_profit, 4),
            stop_loss=round(stop_loss, 4),
            position_size_pct=self.position_size_pct,
            expected_holding=HoldingPeriod.INTRADAY,
            estimated_win_probability=self.win_probability,
            risk_reward_ratio=self.rr,
            justification=f"SMA{self.short}x{self.long} cross {direction.value}",
        )
