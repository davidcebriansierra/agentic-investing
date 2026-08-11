"""Nucleo de razonamiento LLM para los buscadores (spec v2.0, seccion 3.1).

`LLMSearcher` extiende `BaseSearcher` con el patron comun a todos los buscadores que
razonan con un LLM:

1. Cada subclase construye un *contexto* de texto a partir de su fuente (noticias,
   posts, quotes, ...) e invoca `reason(context)`.
2. `reason` renderiza el prompt versionado (config/prompts/searchers/<nombre>.md) con el
   placeholder `{market_context}`, llama al LLM en modo JSON y parsea una lista de
   objetos candidatos.
3. Cada objeto se convierte en una `Opportunity` validada (`build_opportunity`), y los
   invalidos se descartan.

Ante cualquier fallo (sin LLM, respuesta ilegible, campos invalidos) el resultado es
`[]`, respetando el principio "no-operar por defecto".
"""
from __future__ import annotations

import logging

from src.agents.searchers.base import BaseSearcher
from src.agents.searchers.context import format_quote_line
from src.llm.base import LLMClient, LLMResponse
from src.llm.parsing import extract_json_array
from src.llm.prompts import PromptLibrary, PromptTemplate
from src.schemas.enums import AgentSource, Direction, Exchange, HoldingPeriod
from src.schemas.opportunity import Opportunity

from src.agents._agent_logger import log_entry as _log_entry

logger = logging.getLogger("agentic.searchers.llm")

#: Instruccion de usuario comun: el "que" va en el prompt de sistema (versionado).
_USER_INSTRUCTION = (
    "Analiza el contexto proporcionado y responde SOLO con el JSON solicitado "
    '(un objeto {"opportunities": [...]}). Si no hay oportunidades claras, '
    'devuelve {"opportunities": []}.'
)


class LLMSearcher(BaseSearcher):
    """Base para buscadores que generan oportunidades razonando con un LLM."""

    def __init__(
        self,
        source: AgentSource,
        prompt_name: str,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 15,
        temperature: float = 0.3,
        max_tokens: int = 1500,
    ) -> None:
        super().__init__(source, interval_minutes)
        self.llm = llm
        self.prompt_name = prompt_name
        self.prompts = prompts or PromptLibrary()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._prompt: PromptTemplate | None = None

    @property
    def prompt(self) -> PromptTemplate:
        if self._prompt is None:
            self._prompt = self.prompts.get(self.prompt_name)
        return self._prompt

    async def reason(
        self, market_context: str, extra: dict | None = None
    ) -> list[Opportunity]:
        """Convierte un contexto de texto en oportunidades validadas via LLM.

        ``extra`` permite pasar variables adicionales al render del prompt
        (p.ej. ``watchlist`` para el NewsSearcher).
        """
        agent = self.source.value
        if self.llm is None:
            logger.debug("%s: sin LLM configurado; no se generan oportunidades.", agent)
            _log_entry(agent, {"event": "skip", "reason": "sin LLM configurado"})
            return []
        if not market_context.strip():
            logger.debug("%s: contexto vacio; no se invoca el LLM.", agent)
            _log_entry(agent, {"event": "skip", "reason": "contexto vacio"})
            return []
        try:
            system = self.prompt.render(market_context=market_context, **(extra or {}))
        except KeyError as exc:  # placeholder faltante en el prompt
            logger.warning("%s: prompt '%s' requiere variable %s.", agent, self.prompt_name, exc)
            _log_entry(agent, {"event": "error", "reason": f"prompt variable faltante: {exc}"})
            return []

        _log_entry(agent, {
            "event": "llm_request",
            "prompt_name": self.prompt_name,
            "prompt_version": self.prompt.version,
            "model": getattr(self.llm, "model", "unknown"),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "context_chars": len(market_context),
            "context_snippet": market_context[:500],
            "system_prompt_chars": len(system),
        })

        try:
            response = await self.llm.complete(
                system=system,
                user=_USER_INSTRUCTION,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                json_mode=True,
            )
        except Exception as exc:  # noqa: BLE001 - fallo del proveedor -> no operar
            logger.warning("%s: fallo de LLM (%s); no se generan oportunidades.", agent, exc)
            _log_entry(agent, {"event": "llm_error", "error": str(exc)})
            return []

        _log_entry(agent, {
            "event": "llm_response",
            "model": response.model,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
            "raw_response": response.text,
        })

        items = extract_json_array(response.text)
        opportunities: list[Opportunity] = []
        for item in items:
            opp = self._build_from_dict(item, response)
            if opp is not None:
                opportunities.append(opp)
        logger.info(
            "%s: %d/%d oportunidades validas del LLM.",
            agent, len(opportunities), len(items),
        )
        _log_entry(agent, {
            "event": "opportunities",
            "valid": len(opportunities),
            "total_parsed": len(items),
            "opportunities": [
                {
                    "ticker": o.ticker,
                    "direction": o.direction.value,
                    "entry": o.entry_price,
                    "tp": o.take_profit,
                    "sl": o.stop_loss,
                    "rr": o.risk_reward_ratio,
                    "p_win": o.estimated_win_probability,
                    "justification": o.justification,
                }
                for o in opportunities
            ],
        })
        return opportunities

    def _build_from_dict(self, data: dict, response: LLMResponse) -> Opportunity | None:
        """Construye una Opportunity validada desde un dict del LLM; None si es invalido."""
        try:
            exchange = Exchange(str(data["exchange"]).strip().upper())
            direction = Direction(str(data["direction"]).strip().upper())
            holding = HoldingPeriod(
                str(data.get("expected_holding", "INTRADAY")).strip().upper()
            )
            return self.build_opportunity(
                ticker=str(data["ticker"]).strip(),
                exchange=exchange,
                direction=direction,
                entry_price=float(data["entry_price"]),
                take_profit=float(data["take_profit"]),
                stop_loss=float(data["stop_loss"]),
                position_size_pct=_normalize_pct(data.get("position_size_pct", 0.02)),
                estimated_win_probability=_clamp01(
                    float(data.get("estimated_win_probability", 0.5))
                ),
                justification=str(data.get("justification", "")),
                expected_holding=holding,
                supporting_data=dict(data.get("supporting_data", {})),
                model_version=response.model,
                prompt_version=self.prompt.version,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "%s: oportunidad descartada por datos invalidos (%s): %s",
                self.source.value, exc, data,
            )
            return None


class MarketDataLLMSearcher(LLMSearcher):
    """Buscador LLM que compone el contexto a partir de quotes de una watchlist.

    Base comun de los buscadores respaldados por datos de mercado (technical, premarket,
    fundamental, cross_market). Hoy el `MarketDataClient` solo expone `get_quote`
    (ultimo precio y volumen medio), por lo que el contexto es limitado. Cuando el
    conector exponga OHLCV/fundamentales/series multi-mercado, cada subclase podra
    enriquecer `build_context()` sin tocar el flujo de razonamiento.
    """

    def __init__(
        self,
        source: AgentSource,
        prompt_name: str,
        market_data=None,
        watchlist: list[str] | None = None,
        llm: LLMClient | None = None,
        prompts: PromptLibrary | None = None,
        interval_minutes: int = 15,
        temperature: float = 0.3,
    ) -> None:
        super().__init__(
            source=source,
            prompt_name=prompt_name,
            llm=llm,
            prompts=prompts,
            interval_minutes=interval_minutes,
            temperature=temperature,
        )
        self.market_data = market_data
        self.watchlist = watchlist or []

    async def build_context(self) -> str:
        """Contexto por defecto: quotes de la watchlist. Subclases pueden enriquecerlo."""
        agent = self.source.value
        if self.market_data is None or not self.watchlist:
            return ""
        _log_entry(agent, {
            "event": "api_request",
            "connector": type(self.market_data).__name__,
            "tickers": self.watchlist,
        })
        lines: list[str] = []
        errors: list[str] = []
        for ticker in self.watchlist:
            try:
                quote = await self.market_data.get_quote(ticker)
                lines.append(format_quote_line(quote))
            except Exception as exc:  # noqa: BLE001 - un ticker sin datos no aborta el resto
                logger.debug("%s: sin quote para %s (%s).", agent, ticker, exc)
                errors.append(f"{ticker}: {exc}")
        _log_entry(agent, {
            "event": "api_response",
            "connector": type(self.market_data).__name__,
            "tickers_ok": len(lines),
            "tickers_error": len(errors),
            "errors_sample": errors[:5],
        })
        return "\n".join(lines)

    async def search(self) -> list[Opportunity]:
        context = await self.build_context()
        return await self.reason(context)


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def _normalize_pct(value: float | int | str) -> float:
    """Normaliza el tamano de posicion a fraccion (0, 1].

    Acepta tanto fracciones (0.02) como porcentajes (2.0 -> 0.02); los valores > 1 se
    interpretan como porcentaje. Se acota a un maximo prudente del 20%.
    """
    pct = float(value)
    if pct > 1.0:
        pct = pct / 100.0
    return min(0.2, max(1e-6, pct))
