"""Agente asesor de cartera (TFM: asistente LLM sobre resultados del sistema).

Genera el resumen periodico de estado de la cartera y responde preguntas en lenguaje
natural (chat via Telegram). Puede PROponer cambios de parametros de configuracion,
pero nunca los aplica: la aplicacion requiere confirmacion humana (boton en Telegram),
coherente con el principio "no-operar por defecto" y el canal HITL.

El advisor no ejecuta ordenes ni muta el pipeline: solo lee estado (portfolio,
audit trail) y produce texto/recomendaciones auditables.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.llm.base import LLMClient
from src.llm.parsing import extract_json
from src.llm.prompts import PromptLibrary
from src.schemas.portfolio import Portfolio

logger = logging.getLogger("agentic.advisor")

#: Prompt versionado del asesor (system prompt).
ADVISOR_PROMPT = "advisor/portfolio"

#: Rutas de configuracion que el asesor puede proponer modificar (whitelist).
#: Clave: fichero YAML relativo a config/; valores: prefijos de ruta permitidos.
ALLOWED_PARAM_PREFIXES: dict[str, tuple[str, ...]] = {
    "decisor_weights.yaml": ("thresholds.", "weights.", "track_record."),
    "config.yaml": ("searchers.", "aggregator.", "monitor.", "advisor."),
}

#: Numero maximo de turnos de historial por usuario en el chat.
_CHAT_HISTORY_LEN = 10


@dataclass
class ParamProposal:
    """Propuesta de cambio de parametro pendiente de confirmacion humana."""

    proposal_id: str
    file: str           # nombre del YAML dentro de config/
    path: str           # ruta punteada, p.ej. "thresholds.min_final_score"
    value: Any
    rationale: str = ""


@dataclass
class DailyStats:
    """Resumen de actividad del dia extraido del audit trail (repository)."""

    opportunities: int = 0
    decisions_operate: int = 0
    decisions_no_operate: int = 0
    risk_rejections: int = 0
    orders: int = 0
    executions_filled: int = 0
    executions_rejected: int = 0
    consecutive_losses: int = 0
    intraday_drawdown_pct: float = 0.0
    realized_pnl: float = 0.0      # P&L realizado del dia (reconciliado con fills IBKR)
    realized_trades: int = 0       # lotes cerrados en el dia


def daily_stats_from_repository(repository, consecutive_losses: int = 0,
                                intraday_drawdown_pct: float = 0.0) -> DailyStats:
    """Agrega los contadores del dia a partir del repositorio (memoria o Postgres).

    Tolera repositorios sin registros o con esquema incompleto: cualquier fallo
    deja el contador a cero en lugar de abortar el resumen.
    """
    stats = DailyStats(
        consecutive_losses=consecutive_losses,
        intraday_drawdown_pct=intraday_drawdown_pct,
    )
    if repository is None:
        return stats
    today = datetime.now(timezone.utc).date().isoformat()

    def _records_today(table: str) -> list[dict]:
        try:
            rows = repository.all(table)
        except Exception:  # noqa: BLE001 - tabla ausente o repo caido
            return []
        out = []
        for r in rows:
            ts = r.get("timestamp_utc") or r.get("created_at") or ""
            if str(ts)[:10] == today:
                out.append(r)
        return out

    stats.opportunities = len(_records_today("opportunities"))
    stats.orders = len(_records_today("orders"))
    decisions = _records_today("decisions")
    for d in decisions:
        decision = (d.get("decision") or "").upper()
        if decision == "OPERATE":
            stats.decisions_operate += 1
        else:
            stats.decisions_no_operate += 1
        if d.get("risk_pass") is False:
            stats.risk_rejections += 1
    for ex in _records_today("executions"):
        status = (ex.get("status") or "").upper()
        if status in ("FILLED", "PARTIALLY_FILLED"):
            stats.executions_filled += 1
        elif status in ("REJECTED", "ERROR", "CANCELLED"):
            stats.executions_rejected += 1
    return stats


def format_portfolio_context(
    portfolio: Portfolio,
    stats: DailyStats,
    equity_day_open: float | None = None,
    extra_context: str | None = None,
) -> str:
    """Serializa el estado de la cartera y la actividad del dia para el prompt."""
    unrealized = sum(p.unrealized_pnl for p in portfolio.positions)
    lines = [
        f"Equity total: {portfolio.total_equity:.2f}",
        f"Cash: {portfolio.cash:.2f}",
    ]
    if equity_day_open:
        day_pnl = portfolio.total_equity - equity_day_open
        lines.append(f"Equity apertura dia: {equity_day_open:.2f} (variacion dia: {day_pnl:+.2f})")
    lines.append(f"P&L realizado hoy: {stats.realized_pnl:+.2f} ({stats.realized_trades} cierres)")
    lines.append(f"P&L no realizado: {unrealized:+.2f}")
    lines.append(f"Capital total en riesgo: {portfolio.total_at_risk_pct():.1%}")
    lines.append("")
    if portfolio.positions:
        lines.append("Posiciones abiertas:")
        for p in portfolio.positions:
            sl = f"{p.stop_loss}" if p.stop_loss is not None else "SIN STOP-LOSS"
            lines.append(
                f"  - {p.ticker} {p.direction.value} x{p.quantity} "
                f"avg={p.avg_price} mkt={p.market_price} "
                f"pnl={p.unrealized_pnl:+.2f} sl={sl} sector={p.sector or '-'}"
            )
    else:
        lines.append("Sin posiciones abiertas.")
    lines.append("")
    lines.append(
        "Actividad hoy: "
        f"oportunidades={stats.opportunities}, decisiones OPERATE={stats.decisions_operate}, "
        f"NO_OPERATE={stats.decisions_no_operate}, rechazos riesgo={stats.risk_rejections}, "
        f"ordenes={stats.orders}, ejecuciones filled={stats.executions_filled}, "
        f"rechazadas={stats.executions_rejected}"
    )
    lines.append(
        f"Riesgo: drawdown intradia={stats.intraday_drawdown_pct:.2f}%, "
        f"perdidas consecutivas={stats.consecutive_losses}"
    )
    if extra_context:
        lines.append("")
        lines.append(extra_context)
    return "\n".join(lines)


@dataclass
class PortfolioAdvisor:
    """Asistente LLM de cartera: resumen periodico + chat + propuestas de parametros."""

    llm: LLMClient | None = None
    prompts: PromptLibrary | None = None
    temperature: float = 0.3
    max_tokens: int = 1500
    #: Callable opcional -> str|None con contexto extra (p. ej. ultimo barrido de
    #: simulacion) que se anade al prompt del informe y del chat.
    extra_context_fn: Any = None
    _chat_history: dict[int, deque] = field(default_factory=dict)

    def _system_prompt(self) -> str:
        if self.prompts is None:
            return ""
        try:
            return self.prompts.get(ADVISOR_PROMPT).body
        except FileNotFoundError:
            logger.warning("Prompt %s no encontrado; usando prompt vacio.", ADVISOR_PROMPT)
            return ""

    async def summarize(
        self,
        portfolio: Portfolio,
        stats: DailyStats,
        equity_day_open: float | None = None,
    ) -> tuple[str, list[ParamProposal]]:
        """Genera el resumen periodico de cartera y propuestas de parametros.

        Devuelve (texto_resumen, propuestas). Ante fallo del LLM devuelve un
        resumen determinista de emergencia (el texto nunca depende del LLM para
        los datos, solo para la redaccion y las recomendaciones).
        """
        extra = self.extra_context_fn() if self.extra_context_fn else None
        context = format_portfolio_context(portfolio, stats, equity_day_open, extra_context=extra)
        fallback = "📊 Resumen de cartera\n" + context
        if self.llm is None:
            return fallback, []
        try:
            resp = await self.llm.complete(
                system=self._system_prompt(),
                user=(
                    "Genera el resumen periodico de cartera con recomendaciones.\n\n"
                    f"ESTADO ACTUAL:\n{context}\n\n"
                    'Responde SOLO con JSON: {"summary": "...", "param_changes": '
                    '[{"file": "decisor_weights.yaml", "path": "thresholds.min_final_score", '
                    '"value": 0.55, "rationale": "..."}]}. Si no propones cambios, '
                    '"param_changes": [].'
                ),
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                json_mode=True,
            )
            payload = extract_json(resp.text) or {}
            summary = str(payload.get("summary") or "").strip()
            proposals = self._parse_proposals(payload.get("param_changes"))
            return (summary or fallback), proposals
        except Exception as exc:  # noqa: BLE001 - fallo LLM -> resumen basico
            logger.warning("advisor: fallo al generar resumen (%s); enviando fallback.", exc)
            return fallback, []

    async def answer(
        self,
        user_id: int,
        question: str,
        portfolio: Portfolio,
        stats: DailyStats,
        equity_day_open: float | None = None,
    ) -> str:
        """Responde una pregunta libre del usuario con el contexto actual de cartera."""
        if self.llm is None:
            return "Asistente no disponible (LLM sin configurar)."
        extra = self.extra_context_fn() if self.extra_context_fn else None
        context = format_portfolio_context(portfolio, stats, equity_day_open, extra_context=extra)
        history = self._chat_history.setdefault(user_id, deque(maxlen=_CHAT_HISTORY_LEN))
        history_text = "".join(
            f"Usuario: {h['q']}\nAsistente: {h['a']}\n" for h in history
        )
        try:
            resp = await self.llm.complete(
                system=self._system_prompt(),
                user=(
                    f"ESTADO ACTUAL:\n{context}\n\n"
                    f"CONVERSACION PREVIA:\n{history_text or '(sin historial)'}\n\n"
                    f"PREGUNTA DEL USUARIO: {question}"
                ),
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            answer = resp.text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisor: fallo al responder (%s).", exc)
            return "No he podido generar respuesta ahora mismo. Intentalo de nuevo."
        history.append({"q": question, "a": answer})
        return answer

    @staticmethod
    def _parse_proposals(raw: Any) -> list[ParamProposal]:
        """Valida y normaliza las propuestas de cambio del LLM contra la whitelist."""
        proposals: list[ParamProposal] = []
        if not isinstance(raw, list):
            return proposals
        for item in raw:
            if not isinstance(item, dict):
                continue
            file = str(item.get("file") or "")
            path = str(item.get("path") or "")
            value = item.get("value")
            if file not in ALLOWED_PARAM_PREFIXES:
                logger.warning("advisor: propuesta sobre fichero no permitido (%s) ignorada.", file)
                continue
            if not path.startswith(ALLOWED_PARAM_PREFIXES[file]):
                logger.warning("advisor: ruta no permitida (%s.%s) ignorada.", file, path)
                continue
            proposals.append(
                ParamProposal(
                    proposal_id=f"{file}:{path}",
                    file=file,
                    path=path,
                    value=value,
                    rationale=str(item.get("rationale") or ""),
                )
            )
        return proposals
