"""Agente de Monitorizacion (spec v2.0, seccion 3.8).

Responsabilidades:
- Revisar P&L de posiciones abiertas (realizado/no realizado).
- Verificar que TODAS las posiciones abiertas tienen stop-loss asociado.
- Vigilar condiciones de kill switch (drawdown intradia, perdidas consecutivas, anomalias).

Genera MonitoringEvent auditables y emite metricas Prometheus via Metrics.
"""
from __future__ import annotations

from src.agents._agent_logger import log_entry
from src.governance.audit_logger import AuditLogger
from src.governance.kill_switch import KillSwitch
from src.observability.metrics import Metrics, get_metrics
from src.schemas.monitoring import MonitoringEvent, MonitoringEventType, Severity
from src.schemas.portfolio import Portfolio


class Monitor:
    def __init__(
        self,
        kill_switch: KillSwitch | None = None,
        audit: AuditLogger | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.kill_switch = kill_switch or KillSwitch()
        self.audit = audit or AuditLogger()
        self.metrics = metrics or get_metrics()

    def review_pnl(self, portfolio: Portfolio) -> MonitoringEvent:
        """Resumen de P&L no realizado de la cartera (revision horaria)."""
        total_unrealized = sum(p.unrealized_pnl for p in portfolio.positions)
        pct = (
            total_unrealized / portfolio.total_equity
            if portfolio.total_equity
            else 0.0
        )
        event = MonitoringEvent(
            event_type=MonitoringEventType.PNL_REVIEW,
            severity=Severity.WARNING if pct < 0 else Severity.INFO,
            message=f"P&L no realizado {pct:.2%} ({total_unrealized:.2f})",
            data={
                "unrealized_pnl": round(total_unrealized, 2),
                "unrealized_pct": round(pct, 4),
                "open_positions": len(portfolio.positions),
            },
        )
        log_entry("monitor", {
            "event": "pnl_review",
            "unrealized_pnl": round(total_unrealized, 2),
            "unrealized_pct": round(pct, 4),
            "open_positions": len(portfolio.positions),
            "severity": event.severity.value,
        })
        self.audit.log("monitoring", event)
        self.metrics.pnl_unrealized_pct.set(pct)
        self.metrics.open_positions.set(len(portfolio.positions))
        self.metrics.monitoring_events_total.labels(
            event_type=event.event_type.value, severity=event.severity.value
        ).inc()
        return event

    def check_stop_losses(self, portfolio: Portfolio) -> list[MonitoringEvent]:
        """Verifica que cada posicion abierta tiene stop-loss (chequeo cada 15 min)."""
        events: list[MonitoringEvent] = []
        for pos in portfolio.positions:
            if pos.stop_loss is None:
                event = MonitoringEvent(
                    event_type=MonitoringEventType.MISSING_STOP_LOSS,
                    severity=Severity.CRITICAL,
                    ticker=pos.ticker,
                    message=f"Posicion {pos.ticker} sin stop-loss asociado",
                )
                log_entry("monitor", {
                    "event": "missing_stop_loss",
                    "ticker": pos.ticker,
                })
                self.audit.log("monitoring", event)
                self.metrics.missing_stop_loss_total.inc()
                self.metrics.monitoring_events_total.labels(
                    event_type=event.event_type.value, severity=event.severity.value
                ).inc()
                events.append(event)
        return events

    def evaluate_kill_switch(
        self,
        intraday_drawdown_pct: float,
        consecutive_losses: int,
        anomaly_detected: bool = False,
    ) -> MonitoringEvent | None:
        """Evalua disparadores del kill switch y lo activa si procede."""
        log_entry("monitor", {
            "event": "kill_switch_check",
            "intraday_drawdown_pct": intraday_drawdown_pct,
            "consecutive_losses": consecutive_losses,
            "anomaly_detected": anomaly_detected,
        })
        reason = self.kill_switch.evaluate_triggers(
            intraday_drawdown_pct=intraday_drawdown_pct,
            consecutive_losses=consecutive_losses,
            anomaly_detected=anomaly_detected,
        )
        if reason is None:
            return None
        event = MonitoringEvent(
            event_type=MonitoringEventType.KILL_SWITCH_TRIGGERED,
            severity=Severity.CRITICAL,
            message=f"Kill switch activado: {reason}",
            data={"reason": reason},
        )
        log_entry("monitor", {
            "event": "kill_switch_triggered",
            "reason": reason,
        })
        self.audit.log("monitoring", event)
        self.metrics.kill_switch_active.set(1)
        self.metrics.consecutive_losses.set(consecutive_losses)
        self.metrics.monitoring_events_total.labels(
            event_type=event.event_type.value, severity=event.severity.value
        ).inc()
        return event

    def reset_kill_switch_metric(self) -> None:
        """Actualiza la gauge kill_switch_active a 0 tras desactivar el kill switch."""
        self.metrics.kill_switch_active.set(0)

    def record_consecutive_losses(self, count: int) -> None:
        """Actualiza la gauge consecutive_losses sin disparar un MonitoringEvent."""
        self.metrics.consecutive_losses.set(count)

    @staticmethod
    def intraday_drawdown_pct(equity_open: float, equity_now: float) -> float:
        """Drawdown intradia en porcentaje positivo (0 si no hay perdida)."""
        if equity_open <= 0:
            return 0.0
        dd = (equity_open - equity_now) / equity_open * 100
        return max(0.0, dd)
