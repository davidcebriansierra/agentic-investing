"""Panel de monitorizacion (Streamlit) del Sistema Agentico de Inversion.

Lee el audit trail persistido en PostgreSQL (tablas append-only con payload JSONB) y lo
muestra en vivo: actividad por buscador, oportunidades, desglose por evaluador,
decisiones y ejecuciones/monitor. Corre en local contra el Postgres del docker-compose.

Uso:
    pip install -e ".[dashboard,infra]"
    streamlit run dashboard/app.py

Variables de entorno:
    POSTGRES_DSN  (def. postgresql://postgres:postgres@localhost:5432/agentic)
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

try:  # Auto-refresh no bloqueante (recomendado). Fallback abajo si no esta instalado.
    from streamlit_autorefresh import st_autorefresh

    _HAS_AUTOREFRESH = True
except Exception:  # noqa: BLE001
    st_autorefresh = None  # type: ignore[assignment]
    _HAS_AUTOREFRESH = False

# Carga .env si es posible (para POSTGRES_DSN), sin romper si no esta el paquete.
try:  # pragma: no cover - conveniencia local
    from src.utils.env import load_env

    load_env()
except Exception:  # noqa: BLE001
    pass


# Mapa estatico: agente -> fuentes de datos y variables de entorno que las activan
_AGENT_SOURCES: dict[str, dict] = {
    "news": {
        "desc": "Noticias financieras",
        "fuentes": ["NewsAPI", "Marketaux", "Finnhub", "AlphaVantage"],
        "env_keys": ["NEWSAPI_KEY", "MARKETAUX_KEY", "FINNHUB_KEY", "ALPHAVANTAGE_KEY"],
        "connector_env": "NEWSAPI_KEY",
    },
    "social": {
        "desc": "StockTwits (público) + Reddit (opcional)",
        "fuentes": ["StockTwits (sin clave)", "Reddit (con REDDIT_CLIENT_ID+SECRET)"],
        "env_keys": ["STOCKTWITS_TOKEN", "REDDIT_CLIENT_ID", "REDDIT_SECRET"],
        "connector_env": "SOCIAL",  # rama especial: StockTwits siempre activo
    },
    "technical": {
        "desc": "OHLCV + indicadores técnicos",
        "fuentes": ["IBKR MarketData (OHLCV)"],
        "env_keys": ["IBKR_HOST", "IBKR_PORT"],
        "connector_env": None,
    },
    "premarket": {
        "desc": "Gaps de preapertura",
        "fuentes": ["IBKR MarketData (pre-market snapshot)"],
        "env_keys": ["IBKR_HOST", "IBKR_PORT"],
        "connector_env": None,
    },
    "fundamental": {
        "desc": "Ratios y fundamentales",
        "fuentes": ["Finnhub"],
        "env_keys": ["FINNHUB_KEY"],
        "connector_env": "FINNHUB_KEY",
    },
    "cross_market": {
        "desc": "Correlaciones cross-market",
        "fuentes": ["IBKR MarketData (OHLCV diario)"],
        "env_keys": ["IBKR_HOST", "IBKR_PORT"],
        "connector_env": None,
    },
}

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/agentic"
DSN = os.getenv("POSTGRES_DSN", DEFAULT_DSN)

st.set_page_config(
    page_title="Agentic Investing — Monitor",
    page_icon="📈",
    layout="wide",
)


# --------------------------------------------------------------------------------------
# Acceso a datos
# --------------------------------------------------------------------------------------
# Tablas del audit trail que el dashboard consulta (subconjunto de las 8 de la spec).
_TABLES = ("opportunities", "evaluations", "decisions", "executions", "monitoring_events")

# DDL minimo para que el panel funcione contra una BD recien creada (sin datos aun).
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id UUID PRIMARY KEY, payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id UUID PRIMARY KEY, opportunity_id UUID NOT NULL, payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS decisions (
    decision_id UUID PRIMARY KEY, opportunity_id UUID NOT NULL, payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS executions (
    id BIGSERIAL PRIMARY KEY, order_id_internal UUID NOT NULL, payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS monitoring_events (
    event_id UUID PRIMARY KEY, payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL,
    hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_log_event_type_idx ON audit_log (event_type);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts DESC);
"""


def _connect():
    import psycopg

    # autocommit=True evita estados de transaccion abortada entre consultas.
    # statement_timeout garantiza que ninguna query se cuelgue en silencio (p.ej. por
    # un lock retenido por una conexion antigua). Si se bloquea, falla y se muestra.
    return psycopg.connect(
        DSN,
        connect_timeout=5,
        autocommit=True,
        options="-c statement_timeout=8000",
    )


@st.cache_resource(show_spinner=False)
def _healthcheck() -> tuple[bool, str]:
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True, "conectado"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@st.cache_resource(show_spinner=False)
def _ensure_schema() -> tuple[bool, str]:
    """Crea las tablas si faltan. Comprueba existencia primero (sin lock pesado) para no
    quedarse bloqueado por locks retenidos por conexiones antiguas."""
    try:
        with _connect() as conn:
            row = conn.execute("SELECT to_regclass('public.opportunities')").fetchone()
            if row and row[0] is not None:
                return True, "esquema existente"
            conn.execute(_SCHEMA_DDL)
        return True, "esquema creado"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def load_payload_table(table: str, limit: int = 500) -> pd.DataFrame:
    """Carga una tabla payload+created_at y normaliza el JSON a columnas."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT payload, created_at FROM {table} ORDER BY created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Error consultando {table}: {exc}")
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.json_normalize([r[0] for r in rows])
    df.insert(0, "created_at", [r[1] for r in rows])
    return df


def table_count(table: str) -> int:
    try:
        with _connect() as conn:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------------------
# Sidebar: conexion y auto-refresh
# --------------------------------------------------------------------------------------
st.sidebar.title("⚙️ Monitor")
st.sidebar.caption("Panel v4 · vista por agente")
masked = DSN.split("@")[-1] if "@" in DSN else DSN
st.sidebar.caption(f"Postgres: `{masked}`")

ok, msg = _healthcheck()
if ok:
    st.sidebar.success("Postgres conectado")
else:
    st.sidebar.error(f"Sin conexión: {msg}")
    st.sidebar.info("Levanta la BD: `docker compose up -d postgres`")

auto = st.sidebar.checkbox("Auto-refresh", value=True)
interval = st.sidebar.slider("Intervalo (s)", 2, 60, 10)
limit = st.sidebar.slider("Filas máx. por tabla", 50, 2000, 500, step=50)
if st.sidebar.button("🔄 Refrescar ahora"):
    _healthcheck.clear()
    st.rerun()

# Auto-refresh no bloqueante (rerun del script, sin recargar el navegador). Evita el
# bucle de recarga que dejaba la pantalla en blanco al interrumpir el websocket.
if auto and _HAS_AUTOREFRESH:
    st_autorefresh(interval=interval * 1000, key="agentic-monitor-refresh")

st.title("📈 Sistema Agéntico de Inversión — Monitor")

if not ok:
    st.stop()

# Asegura que las tablas existen (BD recién creada sin datos aún).
schema_ok, schema_msg = _ensure_schema()
if not schema_ok:
    st.error(f"No se pudo crear/verificar el esquema: {schema_msg}")
    st.stop()


# --------------------------------------------------------------------------------------
# Carga de datos
# --------------------------------------------------------------------------------------
try:
    opps = load_payload_table("opportunities", limit)
    evals = load_payload_table("evaluations", limit)
    decisions = load_payload_table("decisions", limit)
    executions = load_payload_table("executions", limit)
    monitoring = load_payload_table("monitoring_events", limit)
except Exception as exc:  # noqa: BLE001
    st.exception(exc)
    st.stop()


def _drilldown(opportunity_id: str) -> None:
    """Muestra la oportunidad, sus evaluaciones y su decisión."""
    st.write("**Evaluaciones de los 4 evaluadores**")
    ev = evals[evals["opportunity_id"] == opportunity_id] if not evals.empty else pd.DataFrame()
    if ev.empty:
        st.info("Sin evaluaciones para esta oportunidad.")
    else:
        ev_cols = [
            c for c in [
                "evaluator", "score", "confidence", "recommendation",
                "adjusted_take_profit", "adjusted_stop_loss", "justification",
            ] if c in ev.columns
        ]
        st.dataframe(ev[ev_cols], use_container_width=True, hide_index=True)

    st.write("**Decisión**")
    dec = decisions[decisions["opportunity_id"] == opportunity_id] if not decisions.empty else pd.DataFrame()
    if dec.empty:
        st.info("Sin decisión registrada.")
    else:
        d = dec.iloc[0].to_dict()
        dc1, dc2, dc3, dc4 = st.columns(4)
        dc1.metric("Decisión", str(d.get("decision", "-")))
        dc2.metric("Score final", round(float(d.get("final_score", 0) or 0), 3))
        dc3.metric("Expectancy %", round(float(d.get("expectancy_pct", 0) or 0), 3))
        dc4.metric("Consenso", int(d.get("consensus_count", 0) or 0))
        st.caption(f"Motivo: {d.get('reason', '-')}")
        if d.get("evaluator_breakdown"):
            st.json(d["evaluator_breakdown"], expanded=False)


# --------------------------------------------------------------------------------------
# KPIs
# --------------------------------------------------------------------------------------
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Oportunidades", table_count("opportunities"))
c2.metric("Evaluaciones", table_count("evaluations"))
c3.metric("Decisiones", table_count("decisions"))
operate = 0
if not decisions.empty and "decision" in decisions:
    operate = int((decisions["decision"] == "OPERATE").sum())
c4.metric("Decisiones OPERATE", operate)
c5.metric("Ejecuciones", table_count("executions"))

st.divider()

tab_res, tab_agents, tab_opps, tab_eval, tab_dec, tab_exec, tab_audit = st.tabs(
    ["Resumen", "🤖 Agentes", "Oportunidades", "Evaluadores", "Decisiones", "Ejecuciones / Monitor", "🔒 Audit Trail"]
)


# --------------------------------------------------------------------------------------
# Resumen
# --------------------------------------------------------------------------------------
with tab_res:
    left, right = st.columns(2)
    with left:
        st.subheader("Oportunidades por buscador")
        if not opps.empty and "agent_source" in opps:
            by_agent = opps["agent_source"].value_counts()
            st.bar_chart(by_agent)
        else:
            st.info("Sin oportunidades todavía.")
    with right:
        st.subheader("Decisiones por tipo")
        if not decisions.empty and "decision" in decisions:
            st.bar_chart(decisions["decision"].value_counts())
        else:
            st.info("Sin decisiones todavía.")

    st.subheader("Motivos de decisión")
    if not decisions.empty and "reason" in decisions:
        st.bar_chart(decisions["reason"].value_counts())
    else:
        st.info("Sin decisiones todavía.")


# --------------------------------------------------------------------------------------
# Agentes
# --------------------------------------------------------------------------------------
with tab_agents:
    st.subheader("Actividad por agente buscador")

    # --- Panel de estado de conectores ---
    st.markdown("### 🔌 Estado de conectores y fuentes de datos")
    trading_mode = os.getenv("TRADING_MODE", "PAPER")
    ibkr_host = os.getenv("IBKR_HOST", "127.0.0.1")
    ibkr_port = os.getenv("IBKR_PORT", "4002")

    conn_rows = []
    for agent_key, meta in _AGENT_SOURCES.items():
        env_key = meta["connector_env"]
        if env_key == "SOCIAL":
            # StockTwits siempre activo (API pública sin clave). Reddit es adicional.
            has_reddit = bool(os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_SECRET"))
            has_token = bool(os.getenv("STOCKTWITS_TOKEN"))
            fuentes_activas = ["StockTwits " + ("(token)" if has_token else "(público)")]
            if has_reddit:
                fuentes_activas.append("Reddit")
            estado = "🟢 Real"
            detalle = " + ".join(fuentes_activas)
        elif env_key is None:
            # Conector de mercado: PAPER/LIVE indica intención, pero requiere IB Gateway.
            # Sin IBKR_HOST configurado explícitamente se asume mock (datos sintéticos).
            ibkr_configured = bool(os.getenv("IBKR_HOST", ""))
            is_real = trading_mode.upper() in ("PAPER", "LIVE") and ibkr_configured
            if is_real:
                estado = f"🟢 Real ({trading_mode})"
                detalle = f"IBKR {ibkr_host}:{ibkr_port}"
            else:
                estado = "🟡 Mock (datos sintéticos)"
                detalle = f"MockMarketDataClient · TRADING_MODE={trading_mode}"
        else:
            val = os.getenv(env_key, "")
            is_real = bool(val)
            estado = "🟢 Real" if is_real else "🔴 Mock (sin clave)"
            detalle = f"{env_key}={'***' if is_real else 'vacío'}"
        n_opps_agent = 0
        if not opps.empty and "agent_source" in opps.columns:
            n_opps_agent = int((opps["agent_source"] == agent_key).sum())
        conn_rows.append({
            "Agente": agent_key,
            "Descripción": meta["desc"],
            "Fuentes": ", ".join(meta["fuentes"]),
            "Conector": estado,
            "Detalle": detalle,
            "Oportunidades generadas": n_opps_agent,
        })

    import pandas as _pd
    conn_df = _pd.DataFrame(conn_rows)
    st.dataframe(conn_df, use_container_width=True, hide_index=True)

    st.caption(
        "🟢 Real = datos reales (clave API configurada o IB Gateway accesible) · "
        "🟡 Mock = datos sintéticos (IB Gateway no configurado — oportunidades sin valor real) · "
        "🔴 Mock = sin clave API (el agente no genera oportunidades)"
    )

    # Claves de APIs externas disponibles
    with st.expander("🔑 Variables de entorno de APIs externas"):
        api_vars = [
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "NEWSAPI_KEY", "MARKETAUX_KEY", "FINNHUB_KEY", "ALPHAVANTAGE_KEY",
            "REDDIT_CLIENT_ID", "REDDIT_SECRET", "STOCKTWITS_TOKEN",
            "TELEGRAM_BOT_TOKEN",
        ]
        for var in api_vars:
            val = os.getenv(var, "")
            icon = "✅" if val else "❌"
            st.write(f"{icon} `{var}` {'= ***' if val else '= (no configurada)'}")

    st.divider()

    # --- Resumen global de los 6 agentes ---
    st.markdown("### 📊 Resumen de oportunidades por agente")
    all_agents = list(_AGENT_SOURCES.keys())
    summary_rows = []
    for ag in all_agents:
        ag_opps = opps[opps["agent_source"] == ag] if not opps.empty and "agent_source" in opps.columns else _pd.DataFrame()
        ids_ag = set(ag_opps["opportunity_id"].tolist()) if not ag_opps.empty and "opportunity_id" in ag_opps.columns else set()
        dec_ag = decisions[decisions["opportunity_id"].isin(ids_ag)] if not decisions.empty and "opportunity_id" in decisions.columns else _pd.DataFrame()
        summary_rows.append({
            "Agente": ag,
            "Oportunidades": len(ag_opps),
            "OPERATE": int((dec_ag["decision"] == "OPERATE").sum()) if not dec_ag.empty and "decision" in dec_ag.columns else 0,
            "HOLD": int((dec_ag["decision"] == "HOLD").sum()) if not dec_ag.empty and "decision" in dec_ag.columns else 0,
            "REJECT": int((dec_ag["decision"] == "REJECT").sum()) if not dec_ag.empty and "decision" in dec_ag.columns else 0,
        })
    st.dataframe(_pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
    st.caption("Ejecuta `python -m scripts.verify_searchers` para generar datos de los 6 agentes.")

    st.divider()

    if opps.empty:
        st.info("Sin oportunidades. Ejecuta los buscadores o `scripts/verify_searchers.py`.")
    else:
        agentes_list = sorted(opps["agent_source"].dropna().unique().tolist()) if "agent_source" in opps.columns else []
        sel_agente = st.selectbox("Selecciona un agente", agentes_list, key="sel_agente_tab")

        opp_agente = opps[opps["agent_source"] == sel_agente] if sel_agente else opps

        # --- KPIs del agente ---
        n_opps = len(opp_agente)
        ids_agente = set(opp_agente["opportunity_id"].tolist()) if "opportunity_id" in opp_agente.columns else set()

        evals_agente = evals[evals["opportunity_id"].isin(ids_agente)] if not evals.empty and "opportunity_id" in evals.columns else pd.DataFrame()
        dec_agente = decisions[decisions["opportunity_id"].isin(ids_agente)] if not decisions.empty and "opportunity_id" in decisions.columns else pd.DataFrame()

        n_operate = int((dec_agente["decision"] == "OPERATE").sum()) if not dec_agente.empty and "decision" in dec_agente.columns else 0
        n_hold = int((dec_agente["decision"] == "HOLD").sum()) if not dec_agente.empty and "decision" in dec_agente.columns else 0
        n_reject = int((dec_agente["decision"] == "REJECT").sum()) if not dec_agente.empty and "decision" in dec_agente.columns else 0

        ka1, ka2, ka3, ka4, ka5 = st.columns(5)
        ka1.metric("Oportunidades generadas", n_opps)
        ka2.metric("Evaluaciones recibidas", len(evals_agente))
        ka3.metric("OPERATE", n_operate)
        ka4.metric("HOLD", n_hold)
        ka5.metric("REJECT", n_reject)

        st.divider()

        # --- Señales enviadas (lo que generó el agente) ---
        st.markdown("### 📤 Señales generadas por el agente")
        opp_cols = [c for c in [
            "created_at", "ticker", "exchange", "direction",
            "entry_price", "take_profit", "stop_loss", "risk_reward_ratio",
            "estimated_win_probability", "expected_holding", "justification",
        ] if c in opp_agente.columns]
        st.dataframe(opp_agente[opp_cols], use_container_width=True, hide_index=True)

        st.divider()

        # --- Drill-down por oportunidad del agente ---
        st.markdown("### 🔎 Traza completa por oportunidad")
        if "opportunity_id" in opp_agente.columns and not opp_agente.empty:

            def _agent_label(oid: str) -> str:
                row = opp_agente[opp_agente["opportunity_id"] == oid].iloc[0]
                ts = str(row.get("created_at", ""))[:16]
                return f"{row.get('ticker', '?')} · {row.get('direction', '?')} · {ts}"

            sel_opp = st.selectbox(
                "Oportunidad", opp_agente["opportunity_id"].tolist(),
                format_func=_agent_label, key="sel_opp_agent"
            )

            row_opp = opp_agente[opp_agente["opportunity_id"] == sel_opp].iloc[0].to_dict()

            col_l, col_r = st.columns(2)
            with col_l:
                st.markdown("**📤 Lo que envió el agente**")
                st.json({
                    k: v for k, v in row_opp.items()
                    if k not in ("created_at",) and v not in (None, "", [], {})
                }, expanded=True)

            with col_r:
                st.markdown("**📥 Lo que recibieron los evaluadores**")
                ev_sel = evals_agente[evals_agente["opportunity_id"] == sel_opp] if not evals_agente.empty else pd.DataFrame()
                if ev_sel.empty:
                    st.info("Sin evaluaciones para esta oportunidad.")
                else:
                    for _, ev_row in ev_sel.iterrows():
                        evaluator = ev_row.get("evaluator", "?")
                        score = ev_row.get("score", "-")
                        conf = ev_row.get("confidence", "-")
                        rec = ev_row.get("recommendation", "-")
                        just = ev_row.get("justification", "")
                        with st.expander(f"**{evaluator}** → {rec} (score={score}, conf={conf})"):
                            st.caption(just)
                            extra = {k: v for k, v in ev_row.items()
                                     if k not in ("evaluator", "score", "confidence",
                                                  "recommendation", "justification",
                                                  "opportunity_id", "evaluation_id", "created_at")
                                     and v not in (None, "", [], {})}
                            if extra:
                                st.json(extra, expanded=False)

            st.markdown("**⚖️ Decisión del pipeline**")
            dec_sel = dec_agente[dec_agente["opportunity_id"] == sel_opp] if not dec_agente.empty else pd.DataFrame()
            if dec_sel.empty:
                st.info("Sin decisión registrada para esta oportunidad.")
            else:
                d = dec_sel.iloc[0].to_dict()
                dc1, dc2, dc3, dc4 = st.columns(4)
                dc1.metric("Decisión", str(d.get("decision", "-")))
                dc2.metric("Score final", round(float(d.get("final_score", 0) or 0), 3))
                dc3.metric("Expectancy %", round(float(d.get("expectancy_pct", 0) or 0), 3))
                dc4.metric("Consenso", int(d.get("consensus_count", 0) or 0))
                st.caption(f"Motivo: {d.get('reason', '-')}")
                if d.get("evaluator_breakdown"):
                    st.json(d["evaluator_breakdown"], expanded=False)

        st.divider()

        # --- Gráfico temporal de señales del agente ---
        st.markdown("### 📈 Señales en el tiempo")
        if "created_at" in opp_agente.columns and not opp_agente.empty:
            ts_df = opp_agente[["created_at", "ticker"]].copy()
            ts_df["created_at"] = pd.to_datetime(ts_df["created_at"], utc=True)
            ts_df = ts_df.set_index("created_at").sort_index()
            ts_df["count"] = 1
            st.area_chart(ts_df.resample("1min")["count"].sum().fillna(0), use_container_width=True)
        else:
            st.info("Sin datos temporales.")


# --------------------------------------------------------------------------------------
# Oportunidades + drill-down
# --------------------------------------------------------------------------------------
with tab_opps:
    st.subheader("Oportunidades detectadas")
    if opps.empty:
        st.info("Sin oportunidades. Ejecuta los buscadores (o `scripts/verify_searchers.py`).")
    else:
        agentes = ["(todos)"] + sorted(opps["agent_source"].dropna().unique().tolist())
        sel_agent = st.selectbox("Filtrar por buscador", agentes)
        view = opps if sel_agent == "(todos)" else opps[opps["agent_source"] == sel_agent]

        cols = [
            c for c in [
                "created_at", "agent_source", "ticker", "exchange", "direction",
                "entry_price", "take_profit", "stop_loss", "risk_reward_ratio",
                "estimated_win_probability", "expected_holding", "justification",
            ] if c in view.columns
        ]
        st.dataframe(view[cols], use_container_width=True, hide_index=True)

        st.markdown("#### 🔎 Drill-down por oportunidad")
        if "opportunity_id" in view.columns and not view.empty:
            options = view["opportunity_id"].tolist()

            def _label(oid: str) -> str:
                row = view[view["opportunity_id"] == oid].iloc[0]
                return f"{row.get('ticker', '?')} · {row.get('direction', '?')} · {oid[:8]}"

            sel = st.selectbox("Oportunidad", options, format_func=_label)
            _drilldown(sel)


# --------------------------------------------------------------------------------------
# Evaluadores
# --------------------------------------------------------------------------------------
with tab_eval:
    st.subheader("Actividad de los evaluadores")
    if evals.empty:
        st.info("Sin evaluaciones todavía.")
    else:
        if "evaluator" in evals and "score" in evals:
            agg = evals.groupby("evaluator").agg(
                n=("score", "size"),
                score_medio=("score", "mean"),
                confianza_media=("confidence", "mean"),
            ).round(3)
            st.dataframe(agg, use_container_width=True)

        if "evaluator" in evals and "recommendation" in evals:
            st.markdown("#### Recomendaciones por evaluador")
            pivot = (
                evals.groupby(["evaluator", "recommendation"]).size().unstack(fill_value=0)
            )
            st.bar_chart(pivot)

        st.markdown("#### Últimas evaluaciones")
        ev_cols = [
            c for c in [
                "created_at", "evaluator", "opportunity_id", "score", "confidence",
                "recommendation", "justification",
            ] if c in evals.columns
        ]
        st.dataframe(evals[ev_cols], use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------------------
# Decisiones
# --------------------------------------------------------------------------------------
with tab_dec:
    st.subheader("Decisiones")
    if decisions.empty:
        st.info("Sin decisiones todavía.")
    else:
        dcols = [
            c for c in [
                "created_at", "opportunity_id", "decision", "reason", "final_score",
                "expectancy_pct", "risk_reward_ratio", "consensus_count",
            ] if c in decisions.columns
        ]
        st.dataframe(decisions[dcols], use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------------------
# Ejecuciones / Monitor
# --------------------------------------------------------------------------------------
with tab_exec:
    st.subheader("Ejecuciones")
    if executions.empty:
        st.info("Sin ejecuciones (requiere HITL APPROVE + broker).")
    else:
        st.dataframe(executions, use_container_width=True, hide_index=True)

    st.subheader("Eventos de monitorización")
    if monitoring.empty:
        st.info("Sin eventos de monitorización.")
    else:
        st.dataframe(monitoring, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------------------
# Audit Trail
# --------------------------------------------------------------------------------------
def load_audit_log(limit: int = 500, event_type_filter: str | None = None) -> pd.DataFrame:
    """Carga eventos de audit_log con filtro opcional por event_type."""
    try:
        with _connect() as conn:
            if event_type_filter and event_type_filter != "(todos)":
                rows = conn.execute(
                    "SELECT event_type, ts, payload, hash FROM audit_log "
                    "WHERE event_type = %s ORDER BY ts DESC LIMIT %s",
                    (event_type_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event_type, ts, payload, hash FROM audit_log "
                    "ORDER BY ts DESC LIMIT %s",
                    (limit,),
                ).fetchall()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Error consultando audit_log: {exc}")
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    records = []
    for r in rows:
        flat = {"event_type": r[0], "ts": r[1], "hash": r[3]}
        payload = r[2] or {}
        if isinstance(payload, dict):
            for k, v in payload.items():
                flat[f"payload.{k}"] = v
        records.append(flat)
    return pd.DataFrame(records)


with tab_audit:
    st.subheader("🔒 Audit Trail — Registro inmutable de eventos")
    st.caption(
        "Cada fila es un evento INSERT-only con hash de integridad SHA-256. "
        "Los campos sensibles aparecen como `***REDACTED***`."
    )

    # KPIs
    try:
        with _connect() as conn:
            total_audit = conn.execute("SELECT count(*) FROM audit_log").fetchone()
            types_rows = conn.execute(
                "SELECT event_type, count(*) as n FROM audit_log GROUP BY event_type ORDER BY n DESC"
            ).fetchall()
        total_audit = int(total_audit[0]) if total_audit else 0
        types_dict = {r[0]: int(r[1]) for r in (types_rows or [])}
    except Exception:  # noqa: BLE001
        total_audit = 0
        types_dict = {}

    ak1, ak2, ak3, ak4 = st.columns(4)
    ak1.metric("Total eventos", total_audit)
    ak2.metric("Tipos distintos", len(types_dict))
    executions_audit = types_dict.get("execution", 0)
    rejections_audit = types_dict.get("execution_rejected", 0)
    ak3.metric("Ejecuciones", executions_audit)
    ak4.metric("Rechazos", rejections_audit)

    st.divider()

    # Filtros
    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        type_options = ["(todos)"] + list(types_dict.keys())
        sel_type = st.selectbox("Filtrar por tipo de evento", type_options, key="audit_type_filter")
    with col_f2:
        audit_limit = st.number_input("Max. filas", min_value=10, max_value=5000, value=200, step=50, key="audit_limit")

    audit_df = load_audit_log(limit=int(audit_limit), event_type_filter=sel_type)

    if audit_df.empty:
        st.info("Sin eventos en audit_log todavía. Los eventos se registran cuando el sistema ejecuta decisiones.")
    else:
        # Gráfico de eventos en el tiempo
        st.markdown("#### Eventos por tipo en el tiempo")
        if "ts" in audit_df.columns and "event_type" in audit_df.columns:
            ts_audit = audit_df[["ts", "event_type"]].copy()
            ts_audit["ts"] = pd.to_datetime(ts_audit["ts"], utc=True)
            ts_audit["count"] = 1
            pivot_audit = (
                ts_audit.set_index("ts")
                .groupby([pd.Grouper(freq="1min"), "event_type"])["count"]
                .sum()
                .unstack(fill_value=0)
            )
            st.area_chart(pivot_audit, use_container_width=True)

        st.divider()

        # Distribución por tipo
        st.markdown("#### Distribución de eventos")
        if types_dict:
            dist_df = pd.DataFrame(
                [{"event_type": k, "count": v} for k, v in types_dict.items()]
            ).sort_values("count", ascending=False)
            st.bar_chart(dist_df.set_index("event_type")["count"], use_container_width=True)

        st.divider()

        # Tabla principal
        st.markdown("#### Eventos recientes")
        display_cols = ["ts", "event_type"] + [
            c for c in audit_df.columns
            if c not in ("ts", "event_type", "hash")
            and not str(audit_df[c].iloc[0] if not audit_df.empty else "").startswith("{")
        ]
        display_cols = [c for c in display_cols if c in audit_df.columns][:12]
        st.dataframe(audit_df[display_cols], use_container_width=True, hide_index=True)

        # Drill-down de un evento concreto
        st.markdown("#### 🔎 Payload completo de un evento")
        if "ts" in audit_df.columns:
            event_labels = [
                f"{str(row['ts'])[:19]} · {row['event_type']}"
                for _, row in audit_df.head(100).iterrows()
            ]
            sel_event_idx = st.selectbox(
                "Selecciona un evento", range(len(event_labels)),
                format_func=lambda i: event_labels[i], key="audit_event_sel"
            )
            sel_row = audit_df.iloc[sel_event_idx].to_dict()
            col_ev1, col_ev2 = st.columns([2, 1])
            with col_ev1:
                payload_data = {k.replace("payload.", ""): v for k, v in sel_row.items()
                                if k.startswith("payload.") and v not in (None, "", [], {})}
                st.json(payload_data, expanded=True)
            with col_ev2:
                st.markdown("**Metadatos**")
                st.write(f"- **Tipo**: `{sel_row.get('event_type', '-')}`")
                st.write(f"- **Timestamp**: `{str(sel_row.get('ts', '-'))[:23]}`")
                st.write(f"- **Hash SHA-256**: `{str(sel_row.get('hash', '-'))[:16]}...`")
                st.caption("El hash garantiza inmutabilidad: cualquier modificación posterior lo invalida.")


# --------------------------------------------------------------------------------------
# Fallback de auto-refresh si no esta instalado `streamlit-autorefresh`.
# Se coloca al final para que la pagina renderice antes de dormir y volver a ejecutarse.
# --------------------------------------------------------------------------------------
if auto and not _HAS_AUTOREFRESH:
    import time

    st.sidebar.caption("💡 Instala `streamlit-autorefresh` para refresco más fluido.")
    time.sleep(interval)
    st.rerun()
