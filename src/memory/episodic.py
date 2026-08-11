"""Memoria episodica: historico de operaciones para calibrar P(exito).

Implementacion in-memory (MVP). En produccion se respalda en PostgreSQL/Qdrant.
La probabilidad de exito de una oportunidad se calibra con el winrate historico
de operaciones similares (mismo ticker y direccion); si no hay suficiente muestra
se mezcla con un prior (la estimacion del propio buscador) via suavizado de Laplace.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.schemas.opportunity import Opportunity


@dataclass
class TradeOutcome:
    ticker: str
    direction: str
    won: bool
    return_pct: float


class EpisodicMemory:
    def __init__(self, min_samples: int = 10, prior_strength: float = 5.0) -> None:
        self._trades: list[TradeOutcome] = []
        self.min_samples = min_samples
        self.prior_strength = prior_strength

    def record(self, outcome: TradeOutcome) -> None:
        self._trades.append(outcome)

    def _similar(self, opp: Opportunity) -> list[TradeOutcome]:
        return [
            t
            for t in self._trades
            if t.ticker == opp.ticker and t.direction == opp.direction.value
        ]

    def calibrated_win_prob(self, opp: Opportunity) -> float:
        """Probabilidad de exito calibrada con suavizado hacia el prior del buscador."""
        prior = opp.estimated_win_probability
        similar = self._similar(opp)
        wins = sum(1 for t in similar if t.won)
        n = len(similar)
        # Suavizado: (wins + prior * k) / (n + k). Con n=0 devuelve el prior.
        k = self.prior_strength
        return (wins + prior * k) / (n + k)

    def avg_win_return(self, opp: Opportunity) -> float | None:
        wins = [t.return_pct for t in self._similar(opp) if t.won]
        return sum(wins) / len(wins) if wins else None

    def avg_loss_return(self, opp: Opportunity) -> float | None:
        losses = [abs(t.return_pct) for t in self._similar(opp) if not t.won]
        return sum(losses) / len(losses) if losses else None
