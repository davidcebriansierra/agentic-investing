"""SystemState: estado compartido del workflow LangGraph (spec v2.0, seccion 12.1)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.decision import Decision
from src.schemas.enums import RiskDecision
from src.schemas.evaluation import Evaluation
from src.schemas.opportunity import Opportunity
from src.schemas.order import ExecutionResult
from src.schemas.portfolio import Portfolio


class SystemState(BaseModel):
    """Estado que fluye por el grafo: aggregate -> risk_filter -> evaluate -> decide -> hitl -> execute."""

    raw_opportunities: list[Opportunity] = Field(default_factory=list)
    consolidated: list[Opportunity] = Field(default_factory=list)
    portfolio: Portfolio | None = None

    risk_pass: bool = False
    risk_reason: str | None = None
    risk_results: dict[str, RiskDecision] = Field(default_factory=dict)

    evaluations: list[Evaluation] = Field(default_factory=list)
    decision: Decision | None = None

    approved: bool = False
    execution_result: ExecutionResult | None = None
