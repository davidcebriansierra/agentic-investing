"""Agente Ejecutor (spec v2.0, seccion 3.7).

Recibe una decision aprobada, construye la bracket order, la valida con el
Pre-Trade Validator, comprueba el Kill Switch y la envia al broker. Devuelve un
ExecutionResult. Es determinístico: el razonamiento LLM ya ha terminado en fases
anteriores.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from src.agents._agent_logger import log_entry
from src.connectors.ibkr_read import MarketDataClient
from src.connectors.ibkr_write import BrokerClient
from src.governance.audit_logger import AuditLogger
from src.governance.kill_switch import KillSwitch
from src.governance.pre_trade_validator import MarketContext, PreTradeValidator
from src.schemas.decision import Decision
from src.schemas.enums import Direction, ExecutionStatus, OrderAction
from src.schemas.opportunity import Opportunity
from src.schemas.order import ExecutionResult, Order, OrderLeg


class ExecutionError(RuntimeError):
    """La orden no pudo enviarse (validacion, kill switch o broker)."""


@dataclass
class ExecutionRejected:
    """Resultado cuando la orden se rechaza antes de llegar al broker."""

    opportunity_id: str
    reason: str
    violations: list[str]


class Executor:
    def __init__(
        self,
        broker: BrokerClient,
        account: str,
        validator: PreTradeValidator | None = None,
        kill_switch: KillSwitch | None = None,
        audit: AuditLogger | None = None,
        market_data: MarketDataClient | None = None,
    ) -> None:
        self.broker = broker
        self.account = account
        self.validator = validator or PreTradeValidator()
        self.kill_switch = kill_switch or KillSwitch()
        self.audit = audit or AuditLogger()
        self.market_data = market_data

    def build_order(
        self,
        opp: Opportunity,
        account_equity: float,
    ) -> Order:
        """Construye la bracket order a partir de la oportunidad y el capital."""
        action = OrderAction.BUY if opp.direction == Direction.LONG else OrderAction.SELL
        notional = account_equity * opp.position_size_pct
        quantity = max(1, int(notional // opp.entry_price))
        return Order(
            ticker=opp.ticker,
            action=action,
            quantity=quantity,
            entry=OrderLeg(type="LMT", price=opp.entry_price),
            take_profit=OrderLeg(type="LMT", price=opp.take_profit),
            stop_loss=OrderLeg(type="STP", price=opp.stop_loss),
            account=self.account,
        )

    async def execute(
        self,
        decision: Decision,
        opp: Opportunity,
        ctx: MarketContext,
    ) -> ExecutionResult | ExecutionRejected:
        """Valida y ejecuta una decision OPERATE. Devuelve el resultado o un rechazo."""
        log_entry("executor", {
            "event": "execute_input",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": opp.ticker,
            "direction": opp.direction.value,
            "final_score": decision.final_score,
            "reason": decision.reason.value,
            "market_open": ctx.market_open,
            "available_capital": ctx.available_capital,
            "account_equity": ctx.account_equity,
        })

        # Si el mercado está cerrado y tenemos market_data, esperar a la apertura
        if not ctx.market_open and self.market_data:
            now = datetime.now(timezone.utc)
            hour = now.hour
            # Calcular hora de apertura según exchange
            # BME: 7:00-17:00 UTC, NYSE/NASDAQ: 14:00-21:00 UTC
            from src.schemas.enums import Exchange
            exchange = opp.exchange if hasattr(opp, "exchange") else Exchange.NYSE
            if exchange == Exchange.BME:
                open_hour = 7
            else:
                open_hour = 14

            # Calcular tiempo hasta apertura
            if hour < open_hour:
                wait_seconds = (open_hour - hour) * 3600 - now.minute * 60 - now.second
                log_entry("executor", {
                    "event": "waiting_market_open",
                    "opportunity_id": str(opp.opportunity_id),
                    "current_hour_utc": hour,
                    "open_hour_utc": open_hour,
                    "wait_seconds": wait_seconds,
                })
                await asyncio.sleep(wait_seconds)

                # Re-construir MarketContext actualizado
                try:
                    ctx = await self.market_data.build_market_context(opp.ticker, exchange)
                    log_entry("executor", {
                        "event": "market_context_refreshed",
                        "opportunity_id": str(opp.opportunity_id),
                        "market_open": ctx.market_open,
                        "current_price": ctx.current_price,
                    })
                except Exception as exc:  # noqa: BLE001
                    log_entry("executor", {
                        "event": "market_context_refresh_failed",
                        "opportunity_id": str(opp.opportunity_id),
                        "error": str(exc),
                    })

        # 1. Kill switch: ninguna orden si esta activo.
        if self.kill_switch.active:
            rejection = ExecutionRejected(
                opp.opportunity_id, "kill_switch_activo", ["kill_switch_activo"]
            )
            log_entry("executor", {"event": "rejected", "reason": "kill_switch_activo",
                                    "opportunity_id": str(opp.opportunity_id)})
            self.audit.log("execution_rejected", rejection.__dict__)
            return rejection

        # 2. Construir orden.
        order = self.build_order(opp, ctx.account_equity)
        log_entry("executor", {
            "event": "order_built",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": order.ticker,
            "action": order.action.value,
            "quantity": order.quantity,
            "entry": order.entry.price,
            "tp": order.take_profit.price,
            "sl": order.stop_loss.price,
        })

        # 3. Pre-Trade Validator (incluye idempotencia por opportunity_id).
        result = self.validator.validate(order, ctx, idempotency_key=opp.opportunity_id)
        if not result.valid:
            rejection = ExecutionRejected(
                opp.opportunity_id, "pre_trade_reject", result.violations
            )
            log_entry("executor", {"event": "rejected", "reason": "pre_trade_reject",
                                    "violations": result.violations,
                                    "opportunity_id": str(opp.opportunity_id)})
            self.audit.log("execution_rejected", rejection.__dict__)
            return rejection

        # 4. Enviar al broker.
        try:
            exec_result = await self.broker.place_bracket_order(order)
        except Exception as exc:  # noqa: BLE001 - se reporta como ejecucion rechazada
            rejection = ExecutionRejected(
                opp.opportunity_id, "broker_error", [str(exc)]
            )
            log_entry("executor", {"event": "rejected", "reason": "broker_error",
                                    "error": str(exc),
                                    "opportunity_id": str(opp.opportunity_id)})
            self.audit.log("execution_rejected", rejection.__dict__)
            return rejection

        log_entry("executor", {
            "event": "executed",
            "opportunity_id": str(opp.opportunity_id),
            "ticker": opp.ticker,
            "status": exec_result.status.value,
            "broker_order_id": getattr(exec_result, "broker_order_id", None),
        })
        self.audit.log("execution", exec_result)
        if exec_result.status == ExecutionStatus.REJECTED:
            self.audit.log(
                "execution_rejected",
                {"opportunity_id": opp.opportunity_id, "reason": "broker_rejected"},
            )
        return exec_result
