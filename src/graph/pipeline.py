"""Orquestador deterministico del flujo de decision (sin dependencias externas).

Implementa el mismo recorrido que el grafo LangGraph de la spec (seccion 12.1)
pero en Python puro, de modo que pueda ejecutarse y testearse sin LLMs ni infra:

    aggregate -> risk_filter -> evaluate -> decide -> (hitl) -> (execute)

La aprobacion HITL y la ejecucion se modelan mediante callbacks inyectables.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

from src.agents.aggregator import Aggregator
from src.agents.decisor import Decisor
from src.agents.evaluators import default_evaluators
from src.agents.evaluators.base import BaseEvaluator
from src.agents.risk_filter import RiskFilter
from src.governance.audit_logger import AuditLogger
from src.persistence.repository import InMemoryRepository
from src.schemas.decision import Decision
from src.schemas.enums import DecisionType, RiskDecision
from src.schemas.opportunity import Opportunity
from src.schemas.portfolio import Portfolio
from src.schemas.state import SystemState

ApprovalFn = Callable[[Decision, Opportunity], bool]
ExecuteFn = Callable[[Decision, Opportunity], object]


class DecisionPipeline:
    def __init__(
        self,
        aggregator: Aggregator | None = None,
        risk_filter: RiskFilter | None = None,
        evaluators: list[BaseEvaluator] | None = None,
        decisor: Decisor | None = None,
        audit: AuditLogger | None = None,
        repository: InMemoryRepository | None = None,
    ) -> None:
        self.aggregator = aggregator or Aggregator()
        self.risk_filter = risk_filter or RiskFilter()
        self.evaluators = evaluators or default_evaluators()
        self.decisor = decisor or Decisor()
        self.audit = audit or AuditLogger()
        self.repository = repository

    def run(
        self,
        opportunities: list[Opportunity],
        portfolio: Portfolio,
        approval_fn: ApprovalFn | None = None,
        execute_fn: ExecuteFn | None = None,
    ) -> list[SystemState]:
        """Procesa un lote de oportunidades y devuelve un estado por oportunidad consolidada."""
        consolidated = self.aggregator.aggregate(opportunities)
        for opp in consolidated:
            self.audit.log("opportunity", opp)
            if self.repository is not None:
                self.repository.save_opportunity(opp)

        states: list[SystemState] = []
        for opp in consolidated:
            states.append(
                self._process_one(opp, portfolio, approval_fn, execute_fn)
            )
        return states

    def _process_one(
        self,
        opp: Opportunity,
        portfolio: Portfolio,
        approval_fn: ApprovalFn | None,
        execute_fn: ExecuteFn | None,
    ) -> SystemState:
        state = SystemState(consolidated=[opp], portfolio=portfolio)

        # 1. Risk filter (gate deterministico).
        risk = self.risk_filter.check(opp, portfolio)
        state.risk_pass = risk.passed
        state.risk_reason = None if risk.passed else "; ".join(risk.violations)
        state.risk_results = {opp.ticker: risk.decision}
        if not risk.passed:
            decision = self.decisor.decide(opp, [], risk_pass=False)
            state.decision = decision
            self.audit.log("decision", decision)
            if self.repository is not None:
                self.repository.save_decision(decision)
            return state

        # 2. Evaluacion (4 evaluadores).
        evaluations = [ev.evaluate(opp) for ev in self.evaluators]
        state.evaluations = evaluations
        for e in evaluations:
            self.audit.log("evaluation", e)
            if self.repository is not None:
                self.repository.save_evaluation(e)

        # 3. Decision.
        decision = self.decisor.decide(opp, evaluations, risk_pass=True)
        state.decision = decision
        self.audit.log("decision", decision)
        if self.repository is not None:
            self.repository.save_decision(decision)
        if decision.decision != DecisionType.OPERATE:
            return state

        # 4. HITL (aprobacion humana).
        if approval_fn is not None:
            state.approved = approval_fn(decision, opp)
            self.audit.log("hitl", {"opportunity_id": opp.opportunity_id, "approved": state.approved})
            if not state.approved:
                return state

        # 5. Ejecucion.
        if execute_fn is not None and state.approved:
            result = execute_fn(decision, opp)
            state.execution_result = result  # type: ignore[assignment]
            self.audit.log("execution", result)

        return state

    # ------------------------------------------------------------------
    # Rama asincrona (para evaluadores LLM y callbacks HITL/ejecucion async)
    # ------------------------------------------------------------------
    async def arun(
        self,
        opportunities: list[Opportunity],
        portfolio: Portfolio,
        approval_fn: ApprovalFn | None = None,
        execute_fn: ExecuteFn | None = None,
    ) -> list[SystemState]:
        """Version asincrona de `run`.

        Usa `aevaluate()` en los evaluadores (concurrente via asyncio.gather), lo que
        permite emplear `LLMEvaluator` u otros con I/O. Los callbacks `approval_fn` y
        `execute_fn` pueden ser sincronos o corrutinas.
        """
        consolidated = self.aggregator.aggregate(opportunities)
        for opp in consolidated:
            self.audit.log("opportunity", opp)
            if self.repository is not None:
                self.repository.save_opportunity(opp)

        states: list[SystemState] = []
        for opp in consolidated:
            states.append(
                await self._aprocess_one(opp, portfolio, approval_fn, execute_fn)
            )
        return states

    async def _aprocess_one(
        self,
        opp: Opportunity,
        portfolio: Portfolio,
        approval_fn: ApprovalFn | None,
        execute_fn: ExecuteFn | None,
    ) -> SystemState:
        state = SystemState(consolidated=[opp], portfolio=portfolio)

        # 1. Risk filter (gate deterministico).
        risk = self.risk_filter.check(opp, portfolio)
        state.risk_pass = risk.passed
        state.risk_reason = None if risk.passed else "; ".join(risk.violations)
        state.risk_results = {opp.ticker: risk.decision}
        if not risk.passed:
            decision = self.decisor.decide(opp, [], risk_pass=False)
            state.decision = decision
            self.audit.log("decision", decision)
            if self.repository is not None:
                self.repository.save_decision(decision)
            return state

        # 2. Evaluacion concurrente (4 evaluadores, posiblemente LLM).
        evaluations = await asyncio.gather(
            *(ev.aevaluate(opp) for ev in self.evaluators)
        )
        evaluations = list(evaluations)
        state.evaluations = evaluations
        for e in evaluations:
            self.audit.log("evaluation", e)
            if self.repository is not None:
                self.repository.save_evaluation(e)

        # 3. Decision.
        decision = self.decisor.decide(opp, evaluations, risk_pass=True)
        state.decision = decision
        self.audit.log("decision", decision)
        if self.repository is not None:
            self.repository.save_decision(decision)
        if decision.decision != DecisionType.OPERATE:
            return state

        # 4. HITL (aprobacion humana; callback sync o async).
        if approval_fn is not None:
            state.approved = await self._maybe_await(approval_fn(decision, opp))
            self.audit.log(
                "hitl", {"opportunity_id": opp.opportunity_id, "approved": state.approved}
            )
            if not state.approved:
                return state

        # 5. Ejecucion (callback sync o async).
        if execute_fn is not None and state.approved:
            result = await self._maybe_await(execute_fn(decision, opp))
            state.execution_result = result  # type: ignore[assignment]
            self.audit.log("execution", result)

        return state

    @staticmethod
    async def _maybe_await(value):
        """Await del valor si es una corrutina/awaitable; en otro caso lo devuelve."""
        if inspect.isawaitable(value):
            return await value
        return value
