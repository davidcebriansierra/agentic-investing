"""Kill Switch global (spec v2.0, seccion 7.1).

Mecanismo de parada de emergencia. Estado persistido en Redis en produccion
(`system:kill_switch`); aqui se abstrae mediante un backend intercambiable para
poder testear sin infraestructura.
"""
from __future__ import annotations

from typing import Protocol

from src.utils.config import get_risk_limits


class StateBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...


class InMemoryBackend:
    """Backend en memoria para tests y MVP."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value


KILL_KEY = "system:kill_switch"


class KillSwitch:
    def __init__(
        self,
        backend: StateBackend | None = None,
        limits: dict | None = None,
    ) -> None:
        self.backend = backend or InMemoryBackend()
        ks = (limits or get_risk_limits()).get("kill_switch", {})
        self.max_drawdown_pct = ks.get("max_intraday_drawdown_pct", 3.0)
        self.max_consecutive_losses = ks.get("max_consecutive_losses", 5)
        if self.backend.get(KILL_KEY) is None:
            self.backend.set(KILL_KEY, "OFF")

    @property
    def active(self) -> bool:
        return self.backend.get(KILL_KEY) == "ON"

    def activate(self, reason: str = "manual") -> str:
        """Activa el kill switch. Bloquea nuevas ordenes; no cierra posiciones."""
        self.backend.set(KILL_KEY, "ON")
        self.backend.set(f"{KILL_KEY}:reason", reason)
        return reason

    def deactivate(self) -> None:
        self.backend.set(KILL_KEY, "OFF")

    def evaluate_triggers(
        self,
        intraday_drawdown_pct: float,
        consecutive_losses: int,
        anomaly_detected: bool = False,
    ) -> str | None:
        """Evalua disparadores automaticos. Devuelve el motivo si se activa."""
        reason: str | None = None
        if intraday_drawdown_pct > self.max_drawdown_pct:
            reason = f"drawdown_intradia {intraday_drawdown_pct:.2f}% > {self.max_drawdown_pct}%"
        elif consecutive_losses >= self.max_consecutive_losses:
            reason = f"perdidas_consecutivas {consecutive_losses} >= {self.max_consecutive_losses}"
        elif anomaly_detected:
            reason = "anomalia_en_monitorizacion"

        if reason is not None:
            self.activate(reason)
        return reason

    def guard(self) -> None:
        """Levanta una excepcion si el kill switch esta activo (usar antes de ejecutar)."""
        if self.active:
            raise KillSwitchActiveError(
                self.backend.get(f"{KILL_KEY}:reason") or "kill_switch_activo"
            )


class KillSwitchActiveError(RuntimeError):
    """Se ha intentado operar con el kill switch activo."""
