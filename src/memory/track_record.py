"""Track-record por evaluador: ajusta dinamicamente los pesos del consenso.

Cada N=50 operaciones se recalibran los pesos de los evaluadores en funcion de su
accuracy historica (spec v2.0, seccion 3.5), manteniendo los pesos base como prior.
"""
from __future__ import annotations

from collections import defaultdict

from src.schemas.enums import EvaluatorType


class TrackRecord:
    def __init__(
        self,
        base_weights: dict[str, float],
        rebalance_every_n: int = 50,
    ) -> None:
        self.base_weights = dict(base_weights)
        self.rebalance_every_n = rebalance_every_n
        self._hits: dict[str, int] = defaultdict(int)
        self._total: dict[str, int] = defaultdict(int)
        self._weights = dict(base_weights)
        self._trades_since_rebalance = 0

    def record_outcome(self, evaluator: EvaluatorType, correct: bool) -> None:
        key = evaluator.value
        self._total[key] += 1
        if correct:
            self._hits[key] += 1

    def register_closed_trade(self) -> None:
        self._trades_since_rebalance += 1
        if self._trades_since_rebalance >= self.rebalance_every_n:
            self._rebalance()
            self._trades_since_rebalance = 0

    def accuracy(self, evaluator: str) -> float | None:
        total = self._total.get(evaluator, 0)
        if total == 0:
            return None
        return self._hits[evaluator] / total

    def weights(self) -> dict[str, float]:
        return dict(self._weights)

    def _rebalance(self) -> None:
        """Pondera el peso base por la accuracy historica y renormaliza."""
        adjusted: dict[str, float] = {}
        for ev, base in self.base_weights.items():
            acc = self.accuracy(ev)
            factor = acc if acc is not None else 0.5
            adjusted[ev] = base * factor
        total = sum(adjusted.values())
        if total > 0:
            self._weights = {k: v / total for k, v in adjusted.items()}
