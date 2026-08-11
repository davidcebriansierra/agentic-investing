"""Fixtures compartidas para los tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.schemas.enums import AgentSource, Direction, Exchange
from src.schemas.opportunity import Opportunity
from src.schemas.portfolio import Portfolio


def make_opportunity(
    ticker: str = "IBE.MC",
    direction: Direction = Direction.LONG,
    entry: float = 12.45,
    tp: float = 12.70,
    sl: float = 12.32,
    size: float = 0.05,
    win_prob: float = 0.58,
    source: AgentSource = AgentSource.TECHNICAL,
    timestamp: datetime | None = None,
    **supporting,
) -> Opportunity:
    return Opportunity(
        agent_source=source,
        timestamp_utc=timestamp or datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc),
        ticker=ticker,
        exchange=Exchange.BME,
        direction=direction,
        entry_price=entry,
        take_profit=tp,
        stop_loss=sl,
        position_size_pct=size,
        estimated_win_probability=win_prob,
        risk_reward_ratio=abs(tp - entry) / abs(entry - sl),
        justification="setup de prueba",
        supporting_data=supporting,
    )


@pytest.fixture
def opportunity() -> Opportunity:
    return make_opportunity()


@pytest.fixture
def empty_portfolio() -> Portfolio:
    return Portfolio(total_equity=100_000.0, cash=100_000.0)
