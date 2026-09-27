"""Modulo de simulacion de estrategias (reutiliza el motor de backtest)."""
from src.simulation.service import (
    ScenarioResult,
    SimulationService,
    aggregate_daily_bars,
)

__all__ = ["ScenarioResult", "SimulationService", "aggregate_daily_bars"]
