"""Memoria semantica sobre vector DB (spec v2.0, seccion 4: Qdrant).

Interfaz `VectorStore` + `InMemoryVectorStore` (similitud coseno, sin dependencias) +
`QdrantVectorStore` (cliente real, requiere el extra `infra`). La funcion de embedding
se inyecta para no acoplar a un proveedor concreto.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class VectorRecord:
    id: str
    vector: list[float]
    payload: dict = field(default_factory=dict)


@dataclass
class SearchHit:
    id: str
    score: float
    payload: dict


class VectorStore(Protocol):
    def upsert(self, record: VectorRecord) -> None: ...

    def search(self, vector: list[float], top_k: int = 5) -> list[SearchHit]: ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Dimensiones incompatibles")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class InMemoryVectorStore:
    """Vector store en memoria con busqueda por similitud coseno."""

    def __init__(self, embedder: Callable[[str], list[float]] | None = None) -> None:
        self.embedder = embedder
        self._records: dict[str, VectorRecord] = {}

    def upsert(self, record: VectorRecord) -> None:
        self._records[record.id] = record

    def upsert_text(self, id: str, text: str, payload: dict | None = None) -> None:
        if self.embedder is None:
            raise RuntimeError("No hay embedder configurado para indexar texto.")
        self.upsert(VectorRecord(id=id, vector=self.embedder(text), payload=payload or {}))

    def search(self, vector: list[float], top_k: int = 5) -> list[SearchHit]:
        hits = [
            SearchHit(id=r.id, score=cosine_similarity(vector, r.vector), payload=r.payload)
            for r in self._records.values()
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def search_text(self, text: str, top_k: int = 5) -> list[SearchHit]:
        if self.embedder is None:
            raise RuntimeError("No hay embedder configurado para buscar texto.")
        return self.search(self.embedder(text), top_k=top_k)

    def __len__(self) -> int:
        return len(self._records)


class QdrantVectorStore:
    """Vector store sobre Qdrant. Requiere el extra `infra`.

    Crea la coleccion (distancia coseno) si no existe. Como los ids de `VectorRecord` son
    cadenas arbitrarias y Qdrant exige entero o UUID, se derivan a un UUID determinista y
    se conserva el id original en el payload (`original_id`).
    """

    def __init__(
        self,
        url: str,
        collection: str,
        embedder: Callable[[str], list[float]] | None = None,
        dim: int = 1536,
    ) -> None:
        try:  # pragma: no cover - depende del extra opcional
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "qdrant-client no esta instalado. Instala: pip install -e .[infra]"
            ) from exc
        # check_compatibility=False evita el warning cuando cliente y servidor difieren
        # de version menor (p.ej. cliente 1.18 contra servidor 1.9).
        self.client = QdrantClient(url=url, timeout=5, check_compatibility=False)
        self.collection = collection
        self.embedder = embedder
        self.dim = dim
        self._ensure_collection()

    def _ensure_collection(self) -> None:  # pragma: no cover - requiere Qdrant
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    @staticmethod
    def _point_id(raw: str) -> str:
        """Devuelve un UUID valido para Qdrant a partir de un id arbitrario."""
        try:
            return str(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw)))

    def upsert(self, record: VectorRecord) -> None:  # pragma: no cover - requiere Qdrant
        from qdrant_client.models import PointStruct

        payload = dict(record.payload)
        payload.setdefault("original_id", record.id)
        self.client.upsert(
            self.collection,
            points=[
                PointStruct(id=self._point_id(record.id), vector=record.vector, payload=payload)
            ],
        )

    def upsert_text(self, id: str, text: str, payload: dict | None = None) -> None:  # pragma: no cover
        if self.embedder is None:
            raise RuntimeError("No hay embedder configurado para indexar texto.")
        self.upsert(VectorRecord(id=id, vector=self.embedder(text), payload=payload or {}))

    def search(self, vector: list[float], top_k: int = 5) -> list[SearchHit]:  # pragma: no cover
        # `query_points` es la API actual (qdrant-client >= 1.10 / servidor >= 1.10).
        # `search` fue eliminado en qdrant-client >= 1.12.
        result = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
        )
        hits: list[SearchHit] = []
        for point in result.points:
            payload = point.payload or {}
            hits.append(
                SearchHit(
                    id=str(payload.get("original_id", point.id)),
                    score=point.score,
                    payload=payload,
                )
            )
        return hits

    def search_text(self, text: str, top_k: int = 5) -> list[SearchHit]:  # pragma: no cover
        if self.embedder is None:
            raise RuntimeError("No hay embedder configurado para buscar texto.")
        return self.search(self.embedder(text), top_k=top_k)
