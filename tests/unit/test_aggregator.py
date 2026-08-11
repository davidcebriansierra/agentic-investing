"""Tests del Aggregator / Deduplicator."""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.agents.aggregator import Aggregator
from src.schemas.enums import AgentSource
from tests.conftest import make_opportunity


def test_single_opportunity_passthrough():
    agg = Aggregator()
    opp = make_opportunity()
    result = agg.aggregate([opp])
    assert len(result) == 1
    assert result[0].sources_count == 1


def test_multi_source_convergence_merges_and_adds_bonus():
    agg = Aggregator(confidence_bonus_per_source=0.05, max_confidence_bonus=0.15)
    base_ts = make_opportunity().timestamp_utc
    opps = [
        make_opportunity(source=AgentSource.TECHNICAL, win_prob=0.55, timestamp=base_ts),
        make_opportunity(source=AgentSource.NEWS, win_prob=0.60, timestamp=base_ts + timedelta(seconds=60)),
        make_opportunity(source=AgentSource.SOCIAL, win_prob=0.58, timestamp=base_ts + timedelta(seconds=120)),
    ]
    result = agg.aggregate(opps)
    assert len(result) == 1
    merged = result[0]
    assert merged.sources_count == 3
    assert merged.agent_source == AgentSource.AGGREGATOR
    assert merged.confidence_bonus == 0.10
    assert merged.estimated_win_probability == 0.70  # 0.60 + 0.10


def test_outside_window_not_merged():
    agg = Aggregator(window_seconds=300)
    base_ts = make_opportunity().timestamp_utc
    opps = [
        make_opportunity(source=AgentSource.TECHNICAL, timestamp=base_ts),
        make_opportunity(source=AgentSource.NEWS, timestamp=base_ts + timedelta(seconds=600)),
    ]
    result = agg.aggregate(opps)
    assert len(result) == 2


def test_different_keys_not_merged():
    agg = Aggregator()
    result = agg.aggregate(
        [make_opportunity(ticker="IBE.MC"), make_opportunity(ticker="SAN.MC")]
    )
    assert len(result) == 2


def test_cross_run_convergence_bonus_on_later_source():
    """Fuentes distintas en ejecuciones separadas dentro de la ventana rodante: bonus."""
    agg = Aggregator(
        confidence_bonus_per_source=0.05,
        max_confidence_bonus=0.15,
        cross_source_window_seconds=3600,
    )
    base_ts = make_opportunity().timestamp_utc

    r1 = agg.aggregate(
        [make_opportunity(source=AgentSource.NEWS, win_prob=0.60, timestamp=base_ts)]
    )
    assert r1[0].sources_count == 1
    assert r1[0].confidence_bonus == 0.0

    r2 = agg.aggregate(
        [
            make_opportunity(
                source=AgentSource.TECHNICAL,
                win_prob=0.55,
                timestamp=base_ts + timedelta(minutes=25),
            )
        ]
    )
    assert r2[0].sources_count == 2
    assert r2[0].confidence_bonus == 0.05
    assert r2[0].estimated_win_probability == pytest.approx(0.60)  # 0.55 + 0.05
    # Se conserva la fuente original (no es un merge fisico).
    assert r2[0].agent_source == AgentSource.TECHNICAL
    assert r2[0].supporting_data["converging_sources"] == ["news", "technical"]


def test_cross_run_outside_window_no_bonus():
    """Si la segunda fuente llega fuera de la ventana rodante, no hay convergencia."""
    agg = Aggregator(cross_source_window_seconds=3600)
    base_ts = make_opportunity().timestamp_utc

    agg.aggregate([make_opportunity(source=AgentSource.NEWS, timestamp=base_ts)])
    r2 = agg.aggregate(
        [
            make_opportunity(
                source=AgentSource.TECHNICAL,
                timestamp=base_ts + timedelta(minutes=90),
            )
        ]
    )
    assert r2[0].sources_count == 1
    assert r2[0].confidence_bonus == 0.0


def test_cross_run_same_source_repeated_no_bonus():
    """La misma fuente repitiendo la senal no cuenta como convergencia."""
    agg = Aggregator(cross_source_window_seconds=3600)
    base_ts = make_opportunity().timestamp_utc

    agg.aggregate([make_opportunity(source=AgentSource.NEWS, timestamp=base_ts)])
    r2 = agg.aggregate(
        [
            make_opportunity(
                source=AgentSource.NEWS,
                timestamp=base_ts + timedelta(minutes=10),
            )
        ]
    )
    assert r2[0].sources_count == 1
    assert r2[0].confidence_bonus == 0.0
