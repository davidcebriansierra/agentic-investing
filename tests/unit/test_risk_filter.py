"""Tests del Risk Filter deterministico."""
from __future__ import annotations

from src.agents.risk_filter import RiskFilter
from src.schemas.enums import Direction
from src.schemas.portfolio import Portfolio, Position
from tests.conftest import make_opportunity


def test_pass_with_empty_portfolio():
    rf = RiskFilter()
    opp = make_opportunity(size=0.05)
    portfolio = Portfolio(total_equity=100_000, cash=100_000)
    result = rf.check(opp, portfolio)
    assert result.passed
    assert result.violations == []


def test_reject_on_asset_exposure():
    rf = RiskFilter()
    opp = make_opportunity(ticker="IBE.MC", size=0.06)
    portfolio = Portfolio(
        total_equity=100_000,
        cash=50_000,
        positions=[
            Position(
                ticker="IBE.MC",
                direction=Direction.LONG,
                quantity=1000,
                avg_price=12.0,
                market_price=12.0,  # 12.000 -> 12% del equity
            )
        ],
    )
    result = rf.check(opp, portfolio)
    assert not result.passed
    assert any("exposicion_activo" in v for v in result.violations)


def test_reject_on_high_correlation():
    rf = RiskFilter()
    opp = make_opportunity(ticker="IBE.MC", size=0.02)
    portfolio = Portfolio(
        total_equity=100_000,
        cash=90_000,
        positions=[
            Position(
                ticker="SAN.MC",
                direction=Direction.LONG,
                quantity=100,
                avg_price=4.0,
                market_price=4.0,
                stop_loss=3.9,
            )
        ],
        correlations={"IBE.MC|SAN.MC": 0.85},
    )
    result = rf.check(opp, portfolio)
    assert not result.passed
    assert any("correlacion" in v for v in result.violations)


def test_reject_on_low_liquidity():
    rf = RiskFilter()
    opp = make_opportunity(ticker="IBE.MC", size=0.02)
    portfolio = Portfolio(
        total_equity=100_000,
        cash=90_000,
        avg_volume_20d={"IBE.MC": 1000},  # < min 100000
    )
    result = rf.check(opp, portfolio)
    assert not result.passed
    assert any("liquidez" in v for v in result.violations)
