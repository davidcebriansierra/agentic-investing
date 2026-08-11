"""Enumeraciones compartidas por los contratos de datos (spec v2.0, seccion 5)."""
from __future__ import annotations

from enum import Enum


class AgentSource(str, Enum):
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    NEWS = "news"
    SOCIAL = "social"
    CROSS_MARKET = "cross_market"
    PREMARKET = "premarket"
    AGGREGATOR = "aggregator"


class Exchange(str, Enum):
    BME = "BME"
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class HoldingPeriod(str, Enum):
    INTRADAY = "INTRADAY"
    MULTIDAY = "MULTIDAY"


class EvaluatorType(str, Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    HIGH_RISK = "high_risk"
    SENSATIONALIST = "sensationalist"


class Recommendation(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"


class DecisionType(str, Enum):
    OPERATE = "OPERATE"
    NO_OPERATE = "NO_OPERATE"


class DecisionReason(str, Enum):
    CONSENSUS_REACHED = "consensus_reached"
    LOW_EXPECTANCY = "low_expectancy"
    RISK_FILTER_REJECT = "risk_filter_reject"
    LOW_CONFIDENCE = "low_confidence"
    LOW_CONSENSUS = "low_consensus"
    LOW_RISK_REWARD = "low_risk_reward"


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    BRACKET = "BRACKET"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"


class ExecutionStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    SUBMITTED = "SUBMITTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RiskDecision(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"
