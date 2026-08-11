"""Metricas Prometheus del sistema (spec v2.0, secciones 4 y 11).

Si `prometheus_client` no esta instalado, se usan contadores no-op para que el codigo
funcione igual en entornos sin observabilidad (tests, desarrollo offline).
"""
from __future__ import annotations

try:  # pragma: no cover - depende del extra opcional
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False


class _NoOpMetric:
    """Sustituto sin efectos cuando prometheus_client no esta disponible."""

    def labels(self, *args, **kwargs) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1) -> None:  # noqa: D401
        return None

    def set(self, value: float) -> None:
        return None

    def observe(self, value: float) -> None:
        return None


class Metrics:
    """Coleccion central de metricas del sistema."""

    def __init__(self) -> None:
        if _PROM_AVAILABLE:
            # Registro propio por instancia: evita "Duplicated timeseries" en el
            # registro global cuando se crean varios Metrics (p. ej. en tests).
            self.registry = CollectorRegistry()
            self.opportunities_total = Counter(
                "agentic_opportunities_total",
                "Oportunidades detectadas",
                ["agent_source"],
                registry=self.registry,
            )
            self.decisions_total = Counter(
                "agentic_decisions_total",
                "Decisiones emitidas",
                ["decision"],
                registry=self.registry,
            )
            self.orders_total = Counter(
                "agentic_orders_total",
                "Ordenes enviadas",
                ["status"],
                registry=self.registry,
            )
            self.risk_rejections_total = Counter(
                "agentic_risk_rejections_total",
                "Oportunidades rechazadas por risk filter",
                registry=self.registry,
            )
            self.kill_switch_active = Gauge(
                "agentic_kill_switch_active",
                "Kill switch activo (1) o inactivo (0)",
                registry=self.registry,
            )
            self.signal_to_order_seconds = Histogram(
                "agentic_signal_to_order_seconds",
                "Latencia end-to-end senal -> orden",
                registry=self.registry,
            )
            self.pnl_unrealized_pct = Gauge(
                "agentic_pnl_unrealized_pct",
                "P&L no realizado como fraccion del equity (positivo=ganancia)",
                registry=self.registry,
            )
            self.open_positions = Gauge(
                "agentic_open_positions",
                "Numero de posiciones abiertas",
                registry=self.registry,
            )
            self.consecutive_losses = Gauge(
                "agentic_consecutive_losses",
                "Perdidas consecutivas desde la ultima operacion ganadora",
                registry=self.registry,
            )
            self.missing_stop_loss_total = Counter(
                "agentic_missing_stop_loss_total",
                "Posiciones detectadas sin stop-loss",
                registry=self.registry,
            )
            self.monitoring_events_total = Counter(
                "agentic_monitoring_events_total",
                "Eventos de monitorizacion emitidos",
                ["event_type", "severity"],
                registry=self.registry,
            )
        else:
            self.registry = None
            noop = _NoOpMetric()
            self.opportunities_total = noop
            self.decisions_total = noop
            self.orders_total = noop
            self.risk_rejections_total = noop
            self.kill_switch_active = noop
            self.signal_to_order_seconds = noop
            self.pnl_unrealized_pct = noop
            self.open_positions = noop
            self.consecutive_losses = noop
            self.missing_stop_loss_total = noop
            self.monitoring_events_total = noop

    def start_server(self, port: int = 8000, addr: str = "0.0.0.0") -> bool:  # noqa: S104
        """Arranca el endpoint /metrics. Devuelve True si se inicio realmente.

        Usa ``addr="127.0.0.1"`` para restringir el acceso a localhost.
        """
        if _PROM_AVAILABLE:
            start_http_server(port, addr=addr, registry=self.registry)
            return True
        return False


_metrics: Metrics | None = None


def get_metrics() -> Metrics:
    """Singleton de metricas."""
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics
