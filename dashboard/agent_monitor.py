"""Monitor visual de agentes del Sistema Agentico de Inversion.

Lee los ficheros JSON-lines de logs/agents/ y muestra en tiempo real:
- Vista general: mapa de todos los agentes con estado (activo/inactivo/error)
- Vista detalle por agente: timeline de eventos, llamadas a API, respuestas LLM,
  oportunidades generadas, evaluaciones y decisiones.

Uso:
    streamlit run dashboard/agent_monitor.py
    streamlit run dashboard/agent_monitor.py -- --logs-dir /ruta/custom

Variable de entorno:
    AGENTS_LOG_DIR  (def. logs/agents)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    _HAS_AUTOREFRESH = True
except Exception:  # noqa: BLE001
    st_autorefresh = None
    _HAS_AUTOREFRESH = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LOGS_DIR = Path(os.getenv("AGENTS_LOG_DIR", "logs/agents"))
REFRESH_MS = 5_000  # 5 segundos

_AGENT_META: dict[str, dict] = {
    # Searchers
    "news":         {"label": "News",         "icon": "📰", "group": "Buscadores"},
    "social":       {"label": "Social",       "icon": "💬", "group": "Buscadores"},
    "technical":    {"label": "Technical",    "icon": "📈", "group": "Buscadores"},
    "premarket":    {"label": "Premarket",    "icon": "🌅", "group": "Buscadores"},
    "fundamental":  {"label": "Fundamental",  "icon": "📊", "group": "Buscadores"},
    "cross_market": {"label": "CrossMarket",  "icon": "🌍", "group": "Buscadores"},
    # Evaluators
    "evaluator_conservative":   {"label": "Conservative",   "icon": "🛡️",  "group": "Evaluadores"},
    "evaluator_moderate":       {"label": "Moderate",       "icon": "⚖️",  "group": "Evaluadores"},
    "evaluator_high_risk":      {"label": "High Risk",      "icon": "🔥",  "group": "Evaluadores"},
    "evaluator_sensationalist": {"label": "Sensationalist", "icon": "📢",  "group": "Evaluadores"},
    # Pipeline
    "decisor":  {"label": "Decisor",  "icon": "🧠", "group": "Pipeline"},
    "executor": {"label": "Executor", "icon": "⚡", "group": "Pipeline"},
    "monitor":  {"label": "Monitor",  "icon": "👁️", "group": "Pipeline"},
}

_EVENT_COLORS: dict[str, str] = {
    "api_request":    "#3B82F6",
    "api_response":   "#22C55E",
    "api_error":      "#EF4444",
    "llm_request":    "#8B5CF6",
    "llm_response":   "#A78BFA",
    "llm_error":      "#EF4444",
    "llm_fallback":   "#F59E0B",
    "opportunities":  "#10B981",
    "evaluate_input": "#6366F1",
    "evaluate_result":"#818CF8",
    "decide_input":   "#0EA5E9",
    "decide_result":  "#38BDF8",
    "execute_input":  "#F97316",
    "order_built":    "#FB923C",
    "executed":       "#22C55E",
    "rejected":       "#EF4444",
    "pnl_review":     "#06B6D4",
    "kill_switch_check":     "#94A3B8",
    "kill_switch_triggered": "#DC2626",
    "missing_stop_loss":     "#F87171",
    "skip":           "#94A3B8",
    "error":          "#EF4444",
}

# ---------------------------------------------------------------------------
# Utilidades de lectura de logs
# ---------------------------------------------------------------------------

@st.cache_data(ttl=4)
def read_log(agent: str, max_lines: int = 2000) -> list[dict]:
    path = LOGS_DIR / f"{agent}.log"
    if not path.exists():
        return []
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-max_lines:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return entries


def last_event(entries: list[dict]) -> dict | None:
    return entries[-1] if entries else None


def seconds_since(ts_iso: str | None) -> float | None:
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _to_local(ts_iso: str | None) -> datetime | None:
    """Parsea un ts ISO (el logger lo escribe en UTC) y lo pasa a la zona local del sistema."""
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:  # sin offset: asumimos UTC como escribe _agent_logger
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()  # zona horaria local del sistema (maneja DST)


def fmt_local_time(ts_iso: str | None) -> str:
    """HH:MM:SS en hora local. Fallback al substring crudo si no parsea."""
    dt = _to_local(ts_iso)
    return dt.strftime("%H:%M:%S") if dt else (ts_iso or "")[11:19]


def fmt_local_datetime(ts_iso: str | None) -> str:
    """YYYY-MM-DD HH:MM:SS en hora local. Fallback al substring crudo si no parsea."""
    dt = _to_local(ts_iso)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else (ts_iso or "")[:19]


def agent_status(entries: list[dict]) -> tuple[str, str]:
    """Devuelve (estado_texto, color_hex)."""
    if not entries:
        return "Sin datos", "#64748B"
    last = entries[-1]
    ago = seconds_since(last.get("ts"))
    event = last.get("event", "")
    if event in ("llm_error", "api_error", "rejected", "kill_switch_triggered", "missing_stop_loss"):
        return "Error", "#EF4444"
    if ago is not None and ago < 120:
        return "Activo", "#22C55E"
    if ago is not None and ago < 600:
        return "En espera", "#F59E0B"
    return "Inactivo", "#64748B"


def count_opportunities(entries: list[dict]) -> int:
    total = 0
    for e in entries:
        if e.get("event") == "opportunities":
            total += e.get("valid", 0)
    return total


def count_decisions(entries: list[dict], decision_value: str) -> int:
    return sum(
        1 for e in entries
        if e.get("event") == "decide_result" and e.get("decision") == decision_value
    )


def last_n_events(entries: list[dict], n: int = 50) -> list[dict]:
    return entries[-n:]


# ---------------------------------------------------------------------------
# Componentes UI
# ---------------------------------------------------------------------------

def render_agent_card(agent_key: str, meta: dict, entries: list[dict]) -> bool:
    """Renderiza una tarjeta de agente. Devuelve True si fue clickada."""
    status, color = agent_status(entries)
    n_opps = count_opportunities(entries) if meta["group"] == "Buscadores" else None
    last = last_event(entries)
    ago = seconds_since(last.get("ts")) if last else None
    ago_str = f"{int(ago)}s ago" if ago is not None and ago < 3600 else (
        f"{int(ago // 60)}m ago" if ago is not None else "—"
    )

    with st.container():
        clicked = st.button(
            f"{meta['icon']} **{meta['label']}**",
            key=f"btn_{agent_key}",
            use_container_width=True,
        )
        cols = st.columns([1, 1])
        with cols[0]:
            st.markdown(
                f"<span style='color:{color};font-size:13px;font-weight:600'>"
                f"● {status}</span>",
                unsafe_allow_html=True,
            )
        with cols[1]:
            st.caption(ago_str)
        if n_opps is not None:
            st.caption(f"🎯 {n_opps} oportunidades")
        if entries:
            last_evt = last.get("event", "?") if last else "?"
            evt_color = _EVENT_COLORS.get(last_evt, "#94A3B8")
            st.markdown(
                f"<span style='background:{evt_color}22;color:{evt_color};"
                f"padding:1px 6px;border-radius:4px;font-size:11px'>{last_evt}</span>",
                unsafe_allow_html=True,
            )
    return clicked


def render_event_badge(event: str) -> str:
    color = _EVENT_COLORS.get(event, "#94A3B8")
    return (
        f"<span style='background:{color}22;color:{color};"
        f"padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600'>{event}</span>"
    )


def render_timeline(entries: list[dict], max_rows: int = 100) -> None:
    if not entries:
        st.info("Sin eventos registrados todavía.")
        return
    rows = last_n_events(entries, max_rows)[::-1]  # más reciente primero
    for entry in rows:
        ts = entry.get("ts", "")
        event = entry.get("event", "unknown")
        color = _EVENT_COLORS.get(event, "#94A3B8")
        ts_short = fmt_local_time(ts)

        with st.container():
            col_ts, col_badge, col_detail = st.columns([1.2, 1.8, 6])
            with col_ts:
                st.caption(ts_short)
            with col_badge:
                st.markdown(render_event_badge(event), unsafe_allow_html=True)
            with col_detail:
                _render_event_detail(event, entry)
        st.divider()


def _render_event_detail(event: str, e: dict) -> None:
    if event in ("api_request",):
        connector = e.get("connector", "?")
        tickers = e.get("tickers") or []
        n = len(tickers) if tickers else "?"
        st.markdown(f"**{connector}** — {n} tickers")

    elif event == "api_response":
        ok = e.get("items_received", e.get("tickers_ok", "?"))
        err = e.get("tickers_error", 0)
        connector = e.get("connector", "")
        st.markdown(f"**{connector}** → `{ok}` items recibidos" + (f", `{err}` errores" if err else ""))
        sample = e.get("headlines") or e.get("sample") or e.get("tickers_ok") or []
        if sample:
            with st.expander("Ver muestra"):
                for item in sample[:5]:
                    st.caption(str(item)[:150])

    elif event == "llm_request":
        model = e.get("model", "?")
        ctx = e.get("context_chars", "?")
        prompt = e.get("prompt_name", e.get("prompt_version", ""))
        st.markdown(f"**{model}** · `{ctx}` chars de contexto · prompt `{prompt}`")
        snippet = e.get("context_snippet", "")
        if snippet:
            with st.expander("Ver contexto (primeros 500 chars)"):
                st.code(snippet, language=None)

    elif event == "llm_response":
        model = e.get("model", "?")
        ptok = e.get("prompt_tokens", "?")
        ctok = e.get("completion_tokens", "?")
        st.markdown(f"**{model}** · {ptok}+{ctok} tokens")
        raw = e.get("raw_response", "")
        if raw:
            with st.expander("Ver respuesta bruta"):
                st.code(raw[:2000], language="json")

    elif event == "opportunities":
        valid = e.get("valid", 0)
        total = e.get("total_parsed", 0)
        st.markdown(f"**{valid}/{total}** oportunidades válidas")
        opps = e.get("opportunities", [])
        if opps:
            df = pd.DataFrame(opps)
            st.dataframe(df, use_container_width=True, hide_index=True)

    elif event in ("evaluate_input", "evaluate_result"):
        ticker = e.get("ticker", e.get("opportunity_id", "?"))
        if event == "evaluate_result":
            score = e.get("score", "?")
            rec = e.get("recommendation", "?")
            conf = e.get("confidence", "?")
            st.markdown(f"**{ticker}** → score `{score}` · conf `{conf}` · **{rec}**")
            j = e.get("justification", "")
            if j:
                st.caption(j[:200])
        else:
            direction = e.get("direction", "?")
            rr = e.get("rr", "?")
            st.markdown(f"**{ticker}** {direction} · R/R `{rr}`")

    elif event == "llm_request" and "opportunity_id" in e:
        st.markdown(f"Opp `{e['opportunity_id'][:8]}…` · model `{e.get('model','?')}`")

    elif event == "decide_input":
        ticker = e.get("ticker", "?")
        evs = e.get("evaluations", [])
        st.markdown(f"**{ticker}** · {len(evs)} evaluaciones recibidas")
        if evs:
            df = pd.DataFrame(evs)
            st.dataframe(df, use_container_width=True, hide_index=True)

    elif event == "decide_result":
        ticker = e.get("ticker", "?")
        decision = e.get("decision", "?")
        reason = e.get("reason", "?")
        score = e.get("final_score", "?")
        exp = e.get("expectancy_pct", "?")
        color = "#22C55E" if decision == "OPERATE" else "#EF4444"
        st.markdown(
            f"**{ticker}** — "
            f"<span style='color:{color};font-weight:700'>{decision}</span> "
            f"· score `{score}` · exp `{exp}%` · motivo: `{reason}`",
            unsafe_allow_html=True,
        )
        spec = e.get("order_spec")
        if spec:
            with st.expander("Ver order spec"):
                st.json(spec)

    elif event in ("execute_input", "order_built"):
        ticker = e.get("ticker", "?")
        action = e.get("action", e.get("direction", "?"))
        qty = e.get("quantity", "")
        st.markdown(f"**{ticker}** {action}" + (f" · qty `{qty}`" if qty else ""))

    elif event == "executed":
        ticker = e.get("ticker", "?")
        status = e.get("status", "?")
        oid = e.get("broker_order_id", "")
        color = "#22C55E" if status == "FILLED" else "#F59E0B"
        st.markdown(
            f"**{ticker}** → <span style='color:{color}'>{status}</span>"
            + (f" · broker_id `{oid}`" if oid else ""),
            unsafe_allow_html=True,
        )

    elif event == "rejected":
        reason = e.get("reason", "?")
        viol = e.get("violations", [])
        ticker = e.get("opportunity_id", "?")
        st.markdown(f"❌ `{reason}` — opp `{str(ticker)[:8]}…`")
        if viol:
            st.caption(", ".join(str(v) for v in viol))

    elif event == "pnl_review":
        pct = e.get("unrealized_pct", 0)
        pnl = e.get("unrealized_pnl", 0)
        pos = e.get("open_positions", 0)
        color = "#22C55E" if pct >= 0 else "#EF4444"
        st.markdown(
            f"P&L no realizado: <span style='color:{color};font-weight:700'>"
            f"{pct:.2%} ({pnl:.2f})</span> · {pos} posiciones abiertas",
            unsafe_allow_html=True,
        )

    elif event in ("kill_switch_check",):
        dd = e.get("intraday_drawdown_pct", 0)
        losses = e.get("consecutive_losses", 0)
        anomaly = e.get("anomaly_detected", False)
        st.markdown(f"DD intraday `{dd:.2f}%` · pérdidas consec. `{losses}` · anomalía `{anomaly}`")

    elif event == "kill_switch_triggered":
        reason = e.get("reason", "?")
        st.markdown(f"🚨 **Kill switch activado**: `{reason}`")

    elif event == "skip":
        st.caption(f"Saltado: {e.get('reason', '?')}")

    else:
        # Fallback genérico
        extra = {k: v for k, v in e.items() if k not in ("ts", "event")}
        if extra:
            st.caption(str(extra)[:200])


def render_stats_sidebar(all_data: dict[str, list[dict]]) -> None:
    st.sidebar.markdown("## 📊 Resumen global")
    total_opps = sum(count_opportunities(v) for v in all_data.values())
    total_operate = count_decisions(all_data.get("decisor", []), "OPERATE")
    total_no_op = count_decisions(all_data.get("decisor", []), "NO_OPERATE")
    total_exec = sum(
        1 for e in all_data.get("executor", []) if e.get("event") == "executed"
    )
    total_rejected_exec = sum(
        1 for e in all_data.get("executor", []) if e.get("event") == "rejected"
    )
    active_count = sum(
        1 for k, v in all_data.items()
        if agent_status(v)[0] == "Activo"
    )
    st.sidebar.metric("Agentes activos", active_count, delta=None)
    st.sidebar.metric("Oportunidades detectadas", total_opps)
    st.sidebar.metric("Decisiones OPERATE", total_operate)
    st.sidebar.metric("Decisiones NO_OPERATE", total_no_op)
    st.sidebar.metric("Órdenes enviadas", total_exec)
    st.sidebar.metric("Órdenes rechazadas", total_rejected_exec)
    st.sidebar.divider()
    st.sidebar.caption(f"Directorio de logs: `{LOGS_DIR}`")
    st.sidebar.caption(f"Última actualización: {datetime.now().strftime('%H:%M:%S')}")


def render_llm_cost_summary(all_data: dict[str, list[dict]]) -> None:
    rows = []
    for agent, entries in all_data.items():
        for e in entries:
            if e.get("event") == "llm_response":
                rows.append({
                    "agente": agent,
                    "modelo": e.get("model", "?"),
                    "prompt_tokens": e.get("prompt_tokens", 0),
                    "completion_tokens": e.get("completion_tokens", 0),
                    "total_tokens": e.get("total_tokens", 0) or (
                        e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
                    ),
                    "ts": e.get("ts", ""),
                })
    if not rows:
        st.info("Sin llamadas LLM registradas todavía.")
        return
    df = pd.DataFrame(rows)
    summary = df.groupby(["agente", "modelo"]).agg(
        llamadas=("total_tokens", "count"),
        total_tokens=("total_tokens", "sum"),
    ).reset_index().sort_values("total_tokens", ascending=False)
    st.dataframe(summary, use_container_width=True, hide_index=True)
    total = df["total_tokens"].sum()
    st.caption(f"**Total tokens consumidos:** {total:,}")


# ---------------------------------------------------------------------------
# App principal
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Agent Monitor",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    if _HAS_AUTOREFRESH:
        st_autorefresh(interval=REFRESH_MS, key="agent_monitor_refresh")

    st.title("🤖 Agent Monitor — Sistema Agéntico de Inversión")

    # Leer todos los logs
    all_data: dict[str, list[dict]] = {
        agent: read_log(agent) for agent in _AGENT_META
    }

    # Sidebar con estadísticas globales
    render_stats_sidebar(all_data)

    # ---- Selector de vista ----
    selected_agent: str | None = st.session_state.get("selected_agent")

    # Botón volver si hay agente seleccionado
    if selected_agent:
        if st.button("← Volver al panel general"):
            st.session_state["selected_agent"] = None
            st.rerun()

    # ================================================================
    # VISTA GENERAL: grid de tarjetas por grupo
    # ================================================================
    if not selected_agent:
        groups = {}
        for key, meta in _AGENT_META.items():
            groups.setdefault(meta["group"], []).append(key)

        for group_name, agents in groups.items():
            st.markdown(f"### {group_name}")
            cols = st.columns(len(agents))
            for i, agent_key in enumerate(agents):
                with cols[i]:
                    with st.container(border=True):
                        clicked = render_agent_card(
                            agent_key, _AGENT_META[agent_key], all_data[agent_key]
                        )
                        if clicked:
                            st.session_state["selected_agent"] = agent_key
                            st.rerun()
            st.divider()

        # Resumen global de tokens LLM
        with st.expander("📉 Consumo de tokens LLM (acumulado)"):
            render_llm_cost_summary(all_data)

    # ================================================================
    # VISTA DETALLE de un agente
    # ================================================================
    else:
        meta = _AGENT_META[selected_agent]
        entries = all_data[selected_agent]
        status, color = agent_status(entries)

        # Header
        col_icon, col_title, col_status = st.columns([0.5, 5, 2])
        with col_icon:
            st.markdown(f"<h1 style='margin:0'>{meta['icon']}</h1>", unsafe_allow_html=True)
        with col_title:
            st.markdown(f"## {meta['label']}")
            st.caption(f"Log: `{LOGS_DIR / selected_agent}.log` · {len(entries)} eventos")
        with col_status:
            st.markdown(
                f"<div style='background:{color}22;border:1px solid {color};"
                f"border-radius:8px;padding:8px 16px;text-align:center;"
                f"color:{color};font-weight:700;font-size:18px;margin-top:8px'>"
                f"● {status}</div>",
                unsafe_allow_html=True,
            )

        st.divider()

        # Métricas rápidas del agente
        n_llm_calls = sum(1 for e in entries if e.get("event") == "llm_request")
        n_api_calls = sum(1 for e in entries if e.get("event") == "api_request")
        n_errors = sum(1 for e in entries if e.get("event") in ("llm_error", "api_error", "rejected"))
        n_opps = count_opportunities(entries)

        m_cols = st.columns(5)
        m_cols[0].metric("Eventos totales", len(entries))
        m_cols[1].metric("Llamadas API", n_api_calls)
        m_cols[2].metric("Llamadas LLM", n_llm_calls)
        m_cols[3].metric("Errores", n_errors)
        if meta["group"] == "Buscadores":
            m_cols[4].metric("Oportunidades", n_opps)
        elif selected_agent == "decisor":
            m_cols[4].metric("OPERATE", count_decisions(entries, "OPERATE"))
        elif selected_agent == "executor":
            m_cols[4].metric("Ejecutadas", sum(1 for e in entries if e.get("event") == "executed"))
        else:
            m_cols[4].metric("—", "—")

        st.divider()

        # Tabs: Timeline / Oportunidades / Tokens
        tabs = st.tabs(["🕐 Timeline de eventos", "🎯 Oportunidades / Decisiones", "📉 Tokens LLM"])

        with tabs[0]:
            n_show = st.slider("Eventos a mostrar", 10, 500, 50, step=10, key="timeline_n")
            render_timeline(entries, max_rows=n_show)

        with tabs[1]:
            if meta["group"] == "Buscadores":
                all_opps = []
                for e in entries:
                    if e.get("event") == "opportunities":
                        for opp in e.get("opportunities", []):
                            opp["ts"] = fmt_local_datetime(e.get("ts", ""))
                            all_opps.append(opp)
                if all_opps:
                    df_opps = pd.DataFrame(all_opps)
                    st.dataframe(df_opps, use_container_width=True, hide_index=True)
                else:
                    st.info("Sin oportunidades generadas aún.")
            elif selected_agent == "decisor":
                decisions = [
                    {
                        "ts": fmt_local_datetime(e.get("ts", "")),
                        "ticker": e.get("ticker", ""),
                        "decision": e.get("decision", ""),
                        "reason": e.get("reason", ""),
                        "final_score": e.get("final_score", ""),
                        "expectancy_pct": e.get("expectancy_pct", ""),
                        "rr": e.get("rr", ""),
                        "consensus_count": e.get("consensus_count", ""),
                    }
                    for e in entries if e.get("event") == "decide_result"
                ]
                if decisions:
                    df_dec = pd.DataFrame(decisions)
                    st.dataframe(df_dec, use_container_width=True, hide_index=True)
                else:
                    st.info("Sin decisiones registradas aún.")
            elif selected_agent == "executor":
                execs = [
                    {
                        "ts": fmt_local_datetime(e.get("ts", "")),
                        "ticker": e.get("ticker", ""),
                        "status": e.get("status", ""),
                        "broker_order_id": e.get("broker_order_id", ""),
                    }
                    for e in entries if e.get("event") == "executed"
                ]
                if execs:
                    st.dataframe(pd.DataFrame(execs), use_container_width=True, hide_index=True)
                else:
                    st.info("Sin ejecuciones registradas aún.")
            else:
                st.info("No aplica para este agente.")

        with tabs[2]:
            llm_rows = [
                {
                    "ts": fmt_local_datetime(e.get("ts", "")),
                    "modelo": e.get("model", "?"),
                    "prompt_tokens": e.get("prompt_tokens", 0),
                    "completion_tokens": e.get("completion_tokens", 0),
                    "ticker": e.get("ticker", e.get("opportunity_id", "")),
                }
                for e in entries if e.get("event") == "llm_response"
            ]
            if llm_rows:
                df_llm = pd.DataFrame(llm_rows)
                total_tok = df_llm["prompt_tokens"].sum() + df_llm["completion_tokens"].sum()
                st.metric("Total tokens", f"{total_tok:,}")
                st.dataframe(df_llm, use_container_width=True, hide_index=True)
            else:
                st.info("Sin llamadas LLM registradas para este agente.")


if __name__ == "__main__":
    main()
