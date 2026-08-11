"""Aggregator / Deduplicator deterministico (spec v2.0, seccion 3.2).

No es un LLM. Agrupa las oportunidades por la clave (ticker, direccion, fecha).
Dentro de un mismo lote colapsa duplicados casi simultaneos en una ventana corta
(`window_seconds`). Ademas mantiene una memoria rodante (`cross_source_window_seconds`,
por defecto 1h) de que fuentes han senalado cada clave: cuando una fuente distinta
confirma la misma oportunidad -aunque llegue en una ejecucion posterior- aplica un
bonus de confianza acotado a la probabilidad de exito y anota la convergencia
multi-fuente en supporting_data. Respeta el principio de "no-operar por defecto".

Nota: al procesar en streaming (una fuente por ejecucion), solo las llegadas
posteriores reciben el bonus; la primera fuente ya paso por el pipeline en solitario.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from src.schemas.enums import AgentSource
from src.schemas.opportunity import Opportunity
from src.utils.config import get_config


class Aggregator:
    def __init__(
        self,
        window_seconds: int | None = None,
        confidence_bonus_per_source: float | None = None,
        max_confidence_bonus: float | None = None,
        cross_source_window_seconds: int | None = None,
    ) -> None:
        cfg = get_config().get("aggregator", {})
        self.window = timedelta(
            seconds=window_seconds
            if window_seconds is not None
            else cfg.get("window_seconds", 300)
        )
        self.bonus_per_source = (
            confidence_bonus_per_source
            if confidence_bonus_per_source is not None
            else cfg.get("multi_source_confidence_bonus", 0.05)
        )
        self.max_bonus = (
            max_confidence_bonus
            if max_confidence_bonus is not None
            else cfg.get("max_confidence_bonus", 0.15)
        )
        self.cross_source_window = timedelta(
            seconds=cross_source_window_seconds
            if cross_source_window_seconds is not None
            else cfg.get("cross_source_window_seconds", 3600)
        )
        # Memoria rodante: dedup_key -> {source_value: ultimo_timestamp_visto}.
        self._memory: dict[tuple[str, str, str], dict[str, datetime]] = defaultdict(dict)

    def aggregate(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        """Consolida las oportunidades y aplica la convergencia multi-fuente rodante."""
        if not opportunities:
            return []

        now = max(o.timestamp_utc for o in opportunities)

        groups: dict[tuple[str, str, str], list[Opportunity]] = defaultdict(list)
        for opp in opportunities:
            groups[opp.dedup_key()].append(opp)

        consolidated: list[Opportunity] = []
        for key, group in groups.items():
            for bucket in self._bucketize(group):
                rep, batch_sources = self._merge(bucket)
                self._apply_convergence(key, rep, batch_sources, bucket[-1].timestamp_utc)
                consolidated.append(rep)

        self._prune_memory(now)
        return consolidated

    def _bucketize(self, group: list[Opportunity]) -> list[list[Opportunity]]:
        """Parte un grupo con la misma clave en buckets segun la ventana intra-lote."""
        group = sorted(group, key=lambda o: o.timestamp_utc)
        buckets: list[list[Opportunity]] = []
        bucket: list[Opportunity] = []
        window_start = group[0].timestamp_utc

        for opp in group:
            if opp.timestamp_utc - window_start <= self.window:
                bucket.append(opp)
            else:
                buckets.append(bucket)
                bucket = [opp]
                window_start = opp.timestamp_utc
        if bucket:
            buckets.append(bucket)
        return buckets

    def _merge(self, bucket: list[Opportunity]) -> tuple[Opportunity, set[AgentSource]]:
        """Colapsa duplicados de un bucket en un representante; NO aplica el bonus.

        Devuelve (representante, fuentes_distintas_del_bucket). Si el bucket reune mas
        de una fuente, el representante se reetiqueta como AGGREGATOR (merge fisico de
        oportunidades casi simultaneas). El bonus y la anotacion de convergencia se
        aplican despues en `_apply_convergence`, sobre la ventana rodante.
        """
        distinct_sources = {o.agent_source for o in bucket}

        # Representante: la de mayor probabilidad de exito estimada.
        base = max(bucket, key=lambda o: o.estimated_win_probability)

        if len(distinct_sources) <= 1:
            return base, distinct_sources

        merged = base.model_copy(deep=True)
        merged.agent_source = AgentSource.AGGREGATOR
        merged.supporting_data = {
            **base.supporting_data,
            "merged_sources": sorted(s.value for s in distinct_sources),
            "merged_opportunity_ids": [o.opportunity_id for o in bucket],
        }
        return merged, distinct_sources

    def _apply_convergence(
        self,
        key: tuple[str, str, str],
        opp: Opportunity,
        batch_sources: set[AgentSource],
        ts: datetime,
    ) -> None:
        """Registra las fuentes en la memoria rodante y aplica el bonus multi-fuente.

        Cuenta las fuentes DISTINTAS que han senalado esta clave dentro de la ventana
        rodante (incluidas las del lote actual). Si son mas de una, aplica un bonus de
        confianza acotado a la probabilidad de exito y anota la convergencia. La fuente
        original del representante se conserva (salvo que ya fuera un merge fisico).
        """
        mem = self._memory[key]
        cutoff = ts - self.cross_source_window
        mem = {s: t for s, t in mem.items() if t >= cutoff}
        for source in batch_sources:
            mem[source.value] = ts
        self._memory[key] = mem

        all_sources = sorted(mem.keys())
        sources_count = len(all_sources)
        opp.sources_count = sources_count
        if sources_count <= 1:
            return

        bonus = min(self.bonus_per_source * (sources_count - 1), self.max_bonus)
        opp.confidence_bonus = bonus
        opp.estimated_win_probability = min(1.0, opp.estimated_win_probability + bonus)
        opp.supporting_data = {
            **opp.supporting_data,
            "converging_sources": all_sources,
            "sources_count": sources_count,
        }

    def _prune_memory(self, now: datetime) -> None:
        """Housekeeping: descarta timestamps y claves cuya ventana rodante ha expirado."""
        cutoff = now - self.cross_source_window
        stale_keys: list[tuple[str, str, str]] = []
        for key, mem in self._memory.items():
            fresh = {s: t for s, t in mem.items() if t >= cutoff}
            if fresh:
                self._memory[key] = fresh
            else:
                stale_keys.append(key)
        for key in stale_keys:
            del self._memory[key]
