"""Tests del asistente de cartera (advisor) y la aplicacion de parametros."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import yaml

from src.agents.advisor import (
    ALLOWED_PARAM_PREFIXES,
    DailyStats,
    ParamProposal,
    PortfolioAdvisor,
    daily_stats_from_repository,
    format_portfolio_context,
)
from src.governance.param_change import ParamChangeError, apply_param_change
from src.llm.mock import MockLLMClient
from src.persistence.repository import InMemoryRepository
from src.schemas.enums import Direction
from src.schemas.portfolio import Portfolio, Position


def _portfolio() -> Portfolio:
    return Portfolio(
        total_equity=100_000.0,
        cash=60_000.0,
        positions=[
            Position(
                ticker="SAN.MC", direction=Direction.LONG, quantity=100,
                avg_price=4.0, market_price=4.2, stop_loss=3.9, sector="Banca",
            )
        ],
    )


def _stats() -> DailyStats:
    return DailyStats(
        opportunities=5, decisions_operate=2, decisions_no_operate=3,
        orders=1, executions_filled=1, consecutive_losses=2,
        intraday_drawdown_pct=1.5,
    )


# --- format_portfolio_context --------------------------------------------------

def test_format_context_incluye_posiciones_y_stats():
    ctx = format_portfolio_context(_portfolio(), _stats(), equity_day_open=99_000.0)
    assert "SAN.MC" in ctx
    assert "pnl=+20.00" in ctx  # 100 * (4.2 - 4.0)
    assert "oportunidades=5" in ctx
    assert "drawdown intradia=1.50%" in ctx
    assert "variacion dia: +1000.00" in ctx


def test_format_context_sin_posiciones():
    ctx = format_portfolio_context(Portfolio(total_equity=10_000, cash=10_000), DailyStats())
    assert "Sin posiciones abiertas" in ctx


# --- daily_stats_from_repository ----------------------------------------------

def test_daily_stats_filtra_por_fecha():
    repo = InMemoryRepository()
    today = datetime.now(timezone.utc).date().isoformat()
    repo.insert("decisions", {"decision": "OPERATE", "timestamp_utc": today})
    repo.insert("decisions", {"decision": "NO_OPERATE", "timestamp_utc": today})
    repo.insert("decisions", {"decision": "OPERATE", "timestamp_utc": "2020-01-01"})
    repo.insert("opportunities", {"ticker": "AAPL", "timestamp_utc": today})
    stats = daily_stats_from_repository(repo)
    assert stats.opportunities == 1
    assert stats.decisions_operate == 1
    assert stats.decisions_no_operate == 1


def test_daily_stats_repo_none_devuelve_ceros():
    stats = daily_stats_from_repository(None, consecutive_losses=3)
    assert stats.opportunities == 0
    assert stats.consecutive_losses == 3


# --- PortfolioAdvisor -----------------------------------------------------------

async def test_summarize_sin_llm_devuelve_fallback():
    advisor = PortfolioAdvisor(llm=None)
    text, proposals = await advisor.summarize(_portfolio(), _stats())
    assert "Resumen de cartera" in text
    assert "SAN.MC" in text
    assert proposals == []


async def test_summarize_parsea_propuestas_validas():
    canned = json.dumps({
        "summary": "Todo en orden.",
        "param_changes": [
            {"file": "decisor_weights.yaml", "path": "thresholds.min_final_score",
             "value": 0.55, "rationale": "muchas falsas señales"},
        ],
    })
    advisor = PortfolioAdvisor(llm=MockLLMClient(canned=canned))
    text, proposals = await advisor.summarize(_portfolio(), _stats())
    assert text == "Todo en orden."
    assert proposals == [
        ParamProposal(
            proposal_id="decisor_weights.yaml:thresholds.min_final_score",
            file="decisor_weights.yaml",
            path="thresholds.min_final_score",
            value=0.55,
            rationale="muchas falsas señales",
        )
    ]


async def test_summarize_filtra_propuestas_fuera_de_whitelist():
    canned = json.dumps({
        "summary": "x",
        "param_changes": [
            {"file": "secrets.yaml", "path": "api.key", "value": 1},          # fichero no permitido
            {"file": "decisor_weights.yaml", "path": "danger.deep", "value": 1},  # ruta no permitida
        ],
    })
    advisor = PortfolioAdvisor(llm=MockLLMClient(canned=canned))
    _, proposals = await advisor.summarize(_portfolio(), _stats())
    assert proposals == []


async def test_summarize_llm_falla_devuelve_fallback():
    class FailingLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("LLM down")

    advisor = PortfolioAdvisor(llm=FailingLLM())
    text, proposals = await advisor.summarize(_portfolio(), _stats())
    assert "Resumen de cartera" in text
    assert proposals == []


async def test_answer_usa_llm_y_guarda_historial():
    advisor = PortfolioAdvisor(llm=MockLLMClient(canned="Porque venció el TTL."))
    reply = await advisor.answer(42, "¿por qué no operamos SAN.MC?", _portfolio(), _stats())
    assert reply == "Porque venció el TTL."
    assert len(advisor._chat_history[42]) == 1


async def test_answer_sin_llm():
    advisor = PortfolioAdvisor(llm=None)
    reply = await advisor.answer(1, "hola", _portfolio(), _stats())
    assert "no disponible" in reply.lower()


# --- apply_param_change ----------------------------------------------------------

def _write_config(tmp_path, name: str, data: dict) -> None:
    (tmp_path / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_apply_param_change_escribe_y_devuelve_anterior(tmp_path):
    _write_config(tmp_path, "decisor_weights.yaml",
                  {"thresholds": {"min_final_score": 0.49}})
    old = apply_param_change(
        tmp_path, "decisor_weights.yaml", "thresholds.min_final_score", 0.55,
        ALLOWED_PARAM_PREFIXES,
    )
    assert old == 0.49
    data = yaml.safe_load((tmp_path / "decisor_weights.yaml").read_text())
    assert data["thresholds"]["min_final_score"] == 0.55


def test_apply_param_change_convierte_string_numerico(tmp_path):
    _write_config(tmp_path, "decisor_weights.yaml", {"thresholds": {"min_final_score": 0.49}})
    apply_param_change(
        tmp_path, "decisor_weights.yaml", "thresholds.min_final_score", "0.60",
        ALLOWED_PARAM_PREFIXES,
    )
    data = yaml.safe_load((tmp_path / "decisor_weights.yaml").read_text())
    assert data["thresholds"]["min_final_score"] == 0.60  # float, no string


def test_apply_param_change_rechaza_fichero_no_permitido(tmp_path):
    _write_config(tmp_path, "secrets.yaml", {"api": {"key": "x"}})
    with pytest.raises(ParamChangeError):
        apply_param_change(tmp_path, "secrets.yaml", "api.key", "y", ALLOWED_PARAM_PREFIXES)


def test_apply_param_change_rechaza_ruta_no_permitida(tmp_path):
    _write_config(tmp_path, "decisor_weights.yaml", {"system": {"kill": True}})
    with pytest.raises(ParamChangeError):
        apply_param_change(tmp_path, "decisor_weights.yaml", "system.kill", False,
                           ALLOWED_PARAM_PREFIXES)


def test_apply_param_change_ruta_atraviesa_no_dict(tmp_path):
    _write_config(tmp_path, "config.yaml", {"searchers": "noesdict"})
    with pytest.raises(ParamChangeError):
        apply_param_change(tmp_path, "config.yaml", "searchers.technical.interval_minutes",
                           20, ALLOWED_PARAM_PREFIXES)
