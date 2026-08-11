"""Tests de contrato de los esquemas Pydantic."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.enums import Direction
from tests.conftest import make_opportunity


def test_long_geometry_valid():
    opp = make_opportunity(direction=Direction.LONG, entry=12.45, tp=12.70, sl=12.32)
    assert opp.computed_risk_reward > 0
    assert opp.take_profit_pct > 0
    assert opp.stop_loss_pct > 0


def test_long_geometry_invalid_raises():
    with pytest.raises(ValidationError):
        make_opportunity(direction=Direction.LONG, entry=12.45, tp=12.30, sl=12.32)


def test_short_geometry_valid():
    opp = make_opportunity(direction=Direction.SHORT, entry=12.45, tp=12.20, sl=12.60)
    assert opp.direction == Direction.SHORT
    assert opp.computed_risk_reward > 0


def test_short_geometry_invalid_raises():
    with pytest.raises(ValidationError):
        make_opportunity(direction=Direction.SHORT, entry=12.45, tp=12.60, sl=12.20)


def test_position_size_bounds():
    with pytest.raises(ValidationError):
        make_opportunity(size=1.5)


def test_dedup_key_components():
    opp = make_opportunity()
    key = opp.dedup_key()
    assert key[0] == "IBE.MC"
    assert key[1] == "LONG"
