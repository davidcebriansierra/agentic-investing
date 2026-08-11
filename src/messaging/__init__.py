"""Mensajeria interna entre agentes (spec v2.0, seccion 4: Redis Streams)."""
from src.messaging.streams import (
    EventPublisher,
    GROUP_AGGREGATOR,
    GROUP_MONITOR,
    InMemoryStreamBus,
    MessageBus,
    RedisStreamBus,
    STREAM_DECISIONS,
    STREAM_EXECUTIONS,
    STREAM_OPPORTUNITIES,
    StreamMessage,
)

__all__ = [
    "EventPublisher",
    "GROUP_AGGREGATOR",
    "GROUP_MONITOR",
    "InMemoryStreamBus",
    "MessageBus",
    "RedisStreamBus",
    "STREAM_DECISIONS",
    "STREAM_EXECUTIONS",
    "STREAM_OPPORTUNITIES",
    "StreamMessage",
]
