"""Bus de mensajeria sobre streams (spec v2.0, seccion 4: Redis Streams).

Los buscadores publican oportunidades en un stream y el aggregator las consume en
ventanas. Se define `MessageBus`, una implementacion `InMemoryStreamBus` (con offsets
por grupo de consumidor) y `RedisStreamBus` (cliente real, requiere el extra `infra`).
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("agentic.messaging")


@dataclass
class StreamMessage:
    id: str
    data: dict = field(default_factory=dict)


class MessageBus(Protocol):
    def publish(self, stream: str, data: dict) -> str: ...

    def read(self, stream: str, group: str, count: int = 10) -> list[StreamMessage]: ...

    def ack(self, stream: str, group: str, message_id: str) -> None: ...


class InMemoryStreamBus:
    """Implementacion en memoria con semantica tipo Redis Streams.

    Cada (stream, group) mantiene su offset de lectura. `read` entrega los mensajes no
    leidos y los marca como pendientes hasta su `ack`.
    """

    def __init__(self) -> None:
        self._streams: dict[str, list[StreamMessage]] = defaultdict(list)
        self._seq: dict[str, int] = defaultdict(int)
        # offset de lectura por (stream, group)
        self._offsets: dict[tuple[str, str], int] = defaultdict(int)
        # mensajes entregados pendientes de ack: (stream, group) -> set(ids)
        self._pending: dict[tuple[str, str], set[str]] = defaultdict(set)

    def publish(self, stream: str, data: dict) -> str:
        self._seq[stream] += 1
        msg_id = f"{stream}-{self._seq[stream]}"
        self._streams[stream].append(StreamMessage(id=msg_id, data=data))
        return msg_id

    def read(self, stream: str, group: str, count: int = 10) -> list[StreamMessage]:
        key = (stream, group)
        start = self._offsets[key]
        msgs = self._streams[stream][start : start + count]
        self._offsets[key] = start + len(msgs)
        for m in msgs:
            self._pending[key].add(m.id)
        return msgs

    def ack(self, stream: str, group: str, message_id: str) -> None:
        self._pending[(stream, group)].discard(message_id)

    def pending(self, stream: str, group: str) -> int:
        return len(self._pending[(stream, group)])

    def length(self, stream: str) -> int:
        return len(self._streams[stream])


class RedisStreamBus:
    """Bus sobre Redis Streams. Requiere el extra `infra`.

    El `data` (dict anidado) se serializa a un unico campo `json` en el stream. `read`
    usa grupos de consumidor (XREADGROUP), creando el grupo con MKSTREAM si no existe, y
    entrega los mensajes pendientes hasta su `ack` (XACK).
    """

    _FIELD = "json"

    def __init__(self, url: str, consumer: str = "consumer-1") -> None:
        try:  # pragma: no cover - depende del extra opcional
            import redis
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "redis no esta instalado. Instala el extra: pip install -e .[infra]"
            ) from exc
        # decode_responses=True -> ids y campos como str (evita manejar bytes).
        # timeouts para no colgar indefinidamente si Redis no responde.
        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        # Verifica conectividad al construir: si Redis no responde, el factory degrada.
        self.client.ping()
        self.consumer = consumer
        self._groups_ready: set[tuple[str, str]] = set()

    def publish(self, stream: str, data: dict) -> str:  # pragma: no cover - requiere Redis
        return self.client.xadd(stream, {self._FIELD: json.dumps(data, default=str)})

    def _ensure_group(self, stream: str, group: str) -> None:  # pragma: no cover
        key = (stream, group)
        if key in self._groups_ready:
            return
        try:
            self.client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP si ya existe
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups_ready.add(key)

    def read(  # pragma: no cover - requiere Redis
        self, stream: str, group: str, count: int = 10
    ) -> list[StreamMessage]:
        self._ensure_group(stream, group)
        resp = self.client.xreadgroup(group, self.consumer, {stream: ">"}, count=count)
        if not resp:
            return []
        messages: list[StreamMessage] = []
        for _stream_name, entries in resp:
            for msg_id, fields in entries:
                raw = fields.get(self._FIELD)
                try:
                    data = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    logger.warning("Mensaje %s no parseable en %s; se ignora.", msg_id, stream)
                    data = {}
                messages.append(StreamMessage(id=msg_id, data=data))
        return messages

    def ack(self, stream: str, group: str, message_id: str) -> None:  # pragma: no cover
        self.client.xack(stream, group, message_id)


# ---------------------------------------------------------------------------
# Nombres canonicos de streams (F3/F5)
# ---------------------------------------------------------------------------
STREAM_OPPORTUNITIES = "agentic:opportunities"
STREAM_DECISIONS = "agentic:decisions"
STREAM_EXECUTIONS = "agentic:executions"

# Grupos de consumidor por defecto
GROUP_AGGREGATOR = "aggregator"
GROUP_MONITOR = "monitor"


class EventPublisher:
    """Publicador tipado de eventos sobre un MessageBus.

    Envuelve el bus con metodos de alto nivel para publicar oportunidades,
    decisiones y resultados de ejecucion. Los datos se serializan con
    `model_dump(mode='json')` de Pydantic para garantizar tipos JSON-safe.

    Si `bus` es None (modo offline) todas las operaciones son no-op silenciosas.
    """

    def __init__(self, bus: MessageBus | None = None) -> None:
        self._bus = bus

    @property
    def enabled(self) -> bool:
        return self._bus is not None

    def publish_opportunity(self, opportunity) -> str | None:
        """Publica una Opportunity en STREAM_OPPORTUNITIES. Retorna el message_id."""
        if self._bus is None:
            return None
        try:
            data = opportunity.model_dump(mode="json")
            return self._bus.publish(STREAM_OPPORTUNITIES, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventPublisher: fallo al publicar oportunidad (%s).", exc)
            return None

    def publish_decision(self, decision, opportunity=None) -> str | None:
        """Publica una Decision en STREAM_DECISIONS, opcionalmente con la oportunidad asociada."""
        if self._bus is None:
            return None
        try:
            data = decision.model_dump(mode="json")
            if opportunity is not None:
                data["_opportunity"] = opportunity.model_dump(mode="json")
            return self._bus.publish(STREAM_DECISIONS, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventPublisher: fallo al publicar decision (%s).", exc)
            return None

    def publish_execution(self, execution_result, decision=None, opportunity=None) -> str | None:
        """Publica un ExecutionResult en STREAM_EXECUTIONS."""
        if self._bus is None:
            return None
        try:
            data = (
                execution_result.model_dump(mode="json")
                if hasattr(execution_result, "model_dump")
                else {"result": str(execution_result)}
            )
            if decision is not None:
                data["_decision_id"] = str(decision.decision_id)
            if opportunity is not None:
                data["_opportunity_id"] = str(opportunity.opportunity_id)
                data["_ticker"] = opportunity.ticker
            return self._bus.publish(STREAM_EXECUTIONS, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventPublisher: fallo al publicar ejecucion (%s).", exc)
            return None

    def read_opportunities(
        self, group: str = GROUP_AGGREGATOR, count: int = 50
    ) -> list[StreamMessage]:
        """Lee mensajes pendientes del stream de oportunidades para un grupo de consumidor."""
        if self._bus is None:
            return []
        try:
            return self._bus.read(STREAM_OPPORTUNITIES, group, count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventPublisher: fallo al leer oportunidades (%s).", exc)
            return []

    def read_decisions(
        self, group: str = GROUP_MONITOR, count: int = 50
    ) -> list[StreamMessage]:
        """Lee mensajes pendientes del stream de decisiones para un grupo de consumidor."""
        if self._bus is None:
            return []
        try:
            return self._bus.read(STREAM_DECISIONS, group, count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventPublisher: fallo al leer decisiones (%s).", exc)
            return []

    def ack_opportunity(self, message_id: str, group: str = GROUP_AGGREGATOR) -> None:
        if self._bus is not None:
            try:
                self._bus.ack(STREAM_OPPORTUNITIES, group, message_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("EventPublisher: fallo al ack oportunidad %s (%s).", message_id, exc)

    def ack_decision(self, message_id: str, group: str = GROUP_MONITOR) -> None:
        if self._bus is not None:
            try:
                self._bus.ack(STREAM_DECISIONS, group, message_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("EventPublisher: fallo al ack decision %s (%s).", message_id, exc)
