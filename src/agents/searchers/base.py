"""Clase base para los agentes buscadores de oportunidades (spec v2.0, seccion 3.1).

Un buscador consulta su fuente de datos y emite una lista de Opportunity. La logica
LLM/conectores reales se inyecta en subclases concretas; esta base define el contrato
y un metodo de ayuda para construir oportunidades validadas.
"""
from __future__ import annotations

import abc
import logging

from src.schemas.enums import AgentSource, Direction, Exchange, HoldingPeriod
from src.schemas.opportunity import Opportunity
from src.utils.config import get_risk_limits

logger = logging.getLogger("agentic.searchers.base")


class BaseSearcher(abc.ABC):
    #: Fuente que identifica al agente (se asigna en cada subclase).
    source: AgentSource

    #: Frecuencia de ejecucion en minutos (informativa para el scheduler).
    interval_minutes: int = 15

    def __init__(self, source: AgentSource, interval_minutes: int = 15) -> None:
        self.source = source
        self.interval_minutes = interval_minutes

    @abc.abstractmethod
    async def search(self) -> list[Opportunity]:
        """Consulta la fuente y devuelve oportunidades candidatas."""
        raise NotImplementedError

    def build_opportunity(
        self,
        ticker: str,
        exchange: Exchange,
        direction: Direction,
        entry_price: float,
        take_profit: float,
        stop_loss: float,
        position_size_pct: float,
        estimated_win_probability: float,
        justification: str,
        expected_holding: HoldingPeriod = HoldingPeriod.INTRADAY,
        **extra,
    ) -> Opportunity:
        # El LLM a veces propone un SL fuera del rango permitido (p.ej. 0,41% < 0,5%).
        # Lo acotamos al rango de risk_limits.yaml en vez de perder la oportunidad.
        stop_loss = _clamp_stop_loss(
            entry_price, stop_loss, direction, get_risk_limits()
        )
        rr = (
            abs(take_profit - entry_price) / abs(entry_price - stop_loss)
            if entry_price != stop_loss
            else 0.0
        )
        return Opportunity(
            agent_source=self.source,
            ticker=ticker,
            exchange=exchange,
            direction=direction,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            position_size_pct=position_size_pct,
            expected_holding=expected_holding,
            estimated_win_probability=estimated_win_probability,
            risk_reward_ratio=round(rr, 4),
            justification=justification[:500],
            supporting_data=extra.get("supporting_data", {}),
            model_version=extra.get("model_version"),
            prompt_version=extra.get("prompt_version"),
        )


def _clamp_stop_loss(
    entry_price: float,
    stop_loss: float,
    direction: Direction,
    limits: dict,
) -> float:
    """Acota la distancia del stop-loss al rango [min, max] de risk_limits.yaml.

    El LLM a veces propone un SL demasiado ajustado (p.ej. 0,41% < 0,5%) o demasiado
    amplio, que el PreTradeValidator rechazaria. En lugar de perder la oportunidad,
    movemos el SL al limite mas cercano manteniendo la direccion (LONG=SL por debajo,
    SHORT=SL por encima). No toca entry ni take_profit; el R/R se recalcula en
    build_opportunity a partir del SL resultante.
    """
    if entry_price <= 0:
        return stop_loss
    sl_cfg = limits.get("stop_loss", {})
    min_dist = sl_cfg.get("min_distance_pct", 0.5) / 100
    max_dist = sl_cfg.get("max_distance_pct", 5.0) / 100
    dist = abs(entry_price - stop_loss) / entry_price
    clamped = max(min_dist, min(max_dist, dist))
    if abs(clamped - dist) < 1e-9:
        return stop_loss
    if direction == Direction.LONG:
        adjusted = entry_price * (1 - clamped)
    else:
        adjusted = entry_price * (1 + clamped)
    logger.info(
        "SL ajustado a limites de riesgo: %.4f -> %.4f (distancia %.2f%% -> %.2f%%).",
        stop_loss, adjusted, dist * 100, clamped * 100,
    )
    return round(adjusted, 6)
