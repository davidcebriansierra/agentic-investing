"""LangGraph state machine (spec v2.0, seccion 12.1).

Define el grafo de estados del sistema. LangGraph es una dependencia opcional
(`pip install -e .[agents]`); si no esta instalada, este modulo lanza un error
descriptivo solo al construir el grafo, sin romper el resto del paquete.

Los nodos reutilizan los componentes deterministicos ya implementados, de modo que
la logica de negocio es identica a la del orquestador `DecisionPipeline`.
"""
from __future__ import annotations

import logging
import os

from src.agents.aggregator import Aggregator
from src.agents.decisor import Decisor
from src.agents.evaluators import default_evaluators
from src.agents.executor import Executor, ExecutionRejected
from src.agents.risk_filter import RiskFilter
from src.connectors.ibkr_write import BrokerClient, MockBrokerClient
from src.connectors.telegram_bot import HITLClient, MockHITLClient, build_request
from src.governance.audit_logger import AuditLogger
from src.governance.kill_switch import KillSwitch
from src.governance.pre_trade_validator import MarketContext
from src.messaging.streams import EventPublisher
from src.schemas.enums import DecisionType
from src.schemas.hitl import ApprovalDecision
from src.schemas.state import SystemState

logger = logging.getLogger("agentic.workflow")

try:  # pragma: no cover - depende de extra opcional
    from langgraph.graph import END, StateGraph

    _LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _LANGGRAPH_AVAILABLE = False
    StateGraph = object  # type: ignore[assignment,misc]
    END = "__end__"  # type: ignore[assignment]


_aggregator = Aggregator()
_risk_filter = RiskFilter()
_evaluators = default_evaluators()
_decisor = Decisor()


def _make_aggregator_node(publisher: EventPublisher | None):
    def aggregator_node(state: SystemState) -> SystemState:
        state.consolidated = _aggregator.aggregate(state.raw_opportunities)
        if publisher is not None:
            for opp in state.consolidated:
                publisher.publish_opportunity(opp)
        return state
    return aggregator_node


def risk_filter_node(state: SystemState) -> SystemState:
    if not state.consolidated or state.portfolio is None:
        state.risk_pass = False
        return state
    opp = state.consolidated[0]
    result = _risk_filter.check(opp, state.portfolio)
    state.risk_pass = result.passed
    state.risk_reason = None if result.passed else "; ".join(result.violations)
    return state


def evaluators_parallel_node(state: SystemState) -> SystemState:
    opp = state.consolidated[0]
    state.evaluations = [ev.evaluate(opp) for ev in _evaluators]
    return state


def _make_decisor_node(publisher: EventPublisher | None):
    def decisor_node(state: SystemState) -> SystemState:
        opp = state.consolidated[0]
        state.decision = _decisor.decide(opp, state.evaluations, risk_pass=state.risk_pass)
        if publisher is not None and state.decision is not None:
            publisher.publish_decision(state.decision, opp)
        return state
    return decisor_node


def _make_hitl_node(hitl: HITLClient, kill_switch: KillSwitch | None = None):
    """Fabrica el nodo HITL asincrono con el cliente inyectado.

    Solicita aprobacion humana via Telegram (o Mock). PAUSE_1H activa el kill
    switch inmediatamente. TTL/REJECT -> state.approved = False (no-operar).
    """
    async def hitl_node(state: SystemState) -> SystemState:
        if not state.decision or not state.consolidated:
            state.approved = False
            return state
        opp = state.consolidated[0]
        request = build_request(state.decision, opp)
        try:
            response = await hitl.request_approval(request)
        except Exception as exc:  # noqa: BLE001 - HITL nunca debe tumbar el sistema
            logger.warning("hitl_node: fallo al solicitar aprobacion (%s) -> no operar.", exc)
            state.approved = False
            return state
        state.approved = response.approved
        if response.decision == ApprovalDecision.PAUSE_1H and kill_switch is not None:
            kill_switch.activate("hitl_pause_1h")
            logger.warning(
                "hitl_node: PAUSE_1H -> kill switch activado (por %s).",
                response.responder or "-",
            )
        if response.edited_prices is not None and state.approved:
            ep = response.edited_prices
            state.consolidated[0] = opp.model_copy(update={
                "entry_price": ep.entry_price,
                "stop_loss": ep.stop_loss,
                "take_profit": ep.take_profit,
            })
            logger.info(
                "hitl_node: %s -> precios editados por operador: entrada=%.4f SL=%.4f TP=%.4f",
                opp.ticker, ep.entry_price, ep.stop_loss, ep.take_profit,
            )
        logger.info(
            "hitl_node: %s -> decision=%s aprobado=%s",
            opp.ticker, response.decision.value, state.approved,
        )
        return state
    return hitl_node


def _make_executor_node(executor: Executor, market_data=None, publisher: EventPublisher | None = None):
    """Fabrica el nodo async de ejecucion con el Executor y market_data inyectados."""

    async def executor_node(state: SystemState) -> SystemState:
        if not state.decision or not state.consolidated:
            logger.warning("executor_node: sin decision u oportunidad en el estado.")
            return state

        opp = state.consolidated[0]
        decision = state.decision

        # Construir MarketContext: precio actual + capital desde el portfolio del estado,
        # o consultando IBKR si hay market_data disponible.
        if market_data is not None:
            try:
                exchange = opp.exchange
                ctx = await market_data.build_market_context(opp.ticker, exchange)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "executor_node: no se pudo obtener MarketContext para %s (%s). "
                    "Usando contexto del portfolio.", opp.ticker, exc
                )
                ctx = _ctx_from_portfolio(state, market_data, opp.ticker, opp.exchange)
        else:
            ctx = _ctx_from_portfolio(state, market_data, opp.ticker, opp.exchange)

        result = await executor.execute(decision, opp, ctx)

        if isinstance(result, ExecutionRejected):
            logger.warning(
                "executor_node: orden rechazada para %s — %s", opp.ticker, result.reason
            )
        else:
            state.execution_result = result
            logger.info(
                "executor_node: orden enviada para %s — status=%s ibkr_id=%s",
                opp.ticker, result.status.value, result.ibkr_order_id,
            )
            if publisher is not None:
                publisher.publish_execution(result, decision=decision, opportunity=opp)
        return state

    return executor_node


def _ctx_from_portfolio(state: SystemState, market_data, ticker: str, exchange) -> MarketContext:
    """Construye un MarketContext basico desde el portfolio del estado (fallback sin IBKR)."""
    portfolio = state.portfolio
    equity = portfolio.total_equity if portfolio else 0.0
    cash = portfolio.cash if portfolio else 0.0
    opp = state.consolidated[0] if state.consolidated else None
    return MarketContext(
        current_price=opp.entry_price if opp else 0.0,
        is_tradable_today=True,
        market_open=True,
        available_capital=cash,
        account_equity=equity,
    )


def build_workflow(
    broker: BrokerClient | None = None,
    market_data=None,
    account: str | None = None,
    kill_switch: KillSwitch | None = None,
    audit: AuditLogger | None = None,
    publisher: EventPublisher | None = None,
    hitl: HITLClient | None = None,
):
    """Construye y compila el grafo LangGraph. Requiere el extra `agents`.

    Args:
        broker: cliente de escritura (IBKRBrokerClient o MockBrokerClient).
                Si es None se usa MockBrokerClient.
        market_data: cliente de lectura (IBKRMarketDataClient o Mock) para
                     obtener el MarketContext en tiempo real antes de ejecutar.
        account: numero de cuenta IBKR. Si es None se lee de IBKR_ACCOUNT.
        kill_switch: instancia compartida del kill switch.
        audit: logger de auditoria compartido.
        publisher: publicador de eventos Redis Streams (opcional).
        hitl: cliente de aprobacion humana. Por defecto MockHITLClient (TIMEOUT).
    """
    if not _LANGGRAPH_AVAILABLE:
        raise ImportError(
            "LangGraph no esta instalado. Instala el extra: pip install -e .[agents]"
        )

    _broker = broker or MockBrokerClient()
    _account = account or os.getenv("IBKR_ACCOUNT", "")
    _ks = kill_switch or KillSwitch()
    _audit = audit or AuditLogger()
    _executor = Executor(
        broker=_broker,
        account=_account,
        kill_switch=_ks,
        audit=_audit,
        market_data=market_data,
    )

    g = StateGraph(SystemState)
    g.add_node("aggregate", _make_aggregator_node(publisher))
    g.add_node("risk_filter", risk_filter_node)
    g.add_node("evaluate", evaluators_parallel_node)
    g.add_node("decide", _make_decisor_node(publisher))
    _hitl = hitl or MockHITLClient()
    g.add_node("hitl", _make_hitl_node(_hitl, _ks))
    g.add_node("execute", _make_executor_node(_executor, market_data, publisher))

    g.set_entry_point("aggregate")
    g.add_edge("aggregate", "risk_filter")
    g.add_conditional_edges(
        "risk_filter", lambda s: "evaluate" if s.risk_pass else END
    )
    g.add_edge("evaluate", "decide")
    g.add_conditional_edges(
        "decide",
        lambda s: "hitl" if s.decision and s.decision.decision == DecisionType.OPERATE else END,
    )
    g.add_conditional_edges("hitl", lambda s: "execute" if s.approved else END)
    g.add_edge("execute", END)
    return g.compile()
