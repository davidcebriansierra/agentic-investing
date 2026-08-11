"""Tests del repositorio inmutable, vector store y mensajeria."""
from __future__ import annotations

import pytest

from src.memory.vector_store import InMemoryVectorStore, VectorRecord, cosine_similarity
from src.messaging.streams import InMemoryStreamBus
from src.persistence.repository import InMemoryRepository
from tests.conftest import make_opportunity


# ---------- Repository ----------

def test_repository_insert_and_all():
    repo = InMemoryRepository()
    repo.save_opportunity(make_opportunity())
    assert repo.count("opportunities") == 1
    assert len(repo.all("opportunities")) == 1


def test_repository_unknown_table_raises():
    repo = InMemoryRepository()
    with pytest.raises(KeyError):
        repo.insert("tabla_inexistente", {})


def test_repository_prompt_history_and_traces():
    repo = InMemoryRepository()
    repo.save_prompt_history("evaluators/conservative", "v1.0", "abc123", "gpt-5.1")
    repo.save_llm_trace({"system": "s"}, {"text": "t"})
    assert repo.count("prompts_history") == 1
    assert repo.count("llm_traces") == 1


# ---------- VectorStore ----------

def test_cosine_similarity_identical():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_vector_store_search_orders_by_similarity():
    store = InMemoryVectorStore()
    store.upsert(VectorRecord(id="a", vector=[1.0, 0.0], payload={"t": "a"}))
    store.upsert(VectorRecord(id="b", vector=[0.0, 1.0], payload={"t": "b"}))
    store.upsert(VectorRecord(id="c", vector=[0.9, 0.1], payload={"t": "c"}))
    hits = store.search([1.0, 0.0], top_k=2)
    assert hits[0].id == "a"
    assert hits[1].id == "c"
    assert len(store) == 3


def test_vector_store_text_with_embedder():
    store = InMemoryVectorStore(embedder=lambda s: [float(len(s)), float(s.count("a"))])
    store.upsert_text("doc1", "banana", {"k": 1})
    hits = store.search_text("banana", top_k=1)
    assert hits[0].id == "doc1"


# ---------- MessageBus ----------

def test_stream_bus_publish_and_read():
    bus = InMemoryStreamBus()
    bus.publish("opportunities", {"ticker": "IBE.MC"})
    bus.publish("opportunities", {"ticker": "SAN.MC"})
    msgs = bus.read("opportunities", group="aggregator", count=10)
    assert len(msgs) == 2
    assert bus.pending("opportunities", "aggregator") == 2


def test_stream_bus_offsets_and_ack():
    bus = InMemoryStreamBus()
    bus.publish("s", {"n": 1})
    first = bus.read("s", "g", count=10)
    assert len(first) == 1
    # Segunda lectura no devuelve nada (offset avanzado).
    assert bus.read("s", "g", count=10) == []
    bus.ack("s", "g", first[0].id)
    assert bus.pending("s", "g") == 0


def test_stream_bus_independent_groups():
    bus = InMemoryStreamBus()
    bus.publish("s", {"n": 1})
    assert len(bus.read("s", "g1", count=10)) == 1
    # Otro grupo lee desde el principio.
    assert len(bus.read("s", "g2", count=10)) == 1
