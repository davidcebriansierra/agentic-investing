"""Formateadores de contexto para los buscadores LLM.

Convierten las estructuras de datos de cada fuente (noticias, posts sociales, quotes de
mercado) en texto compacto y legible que se inyecta en el placeholder `{market_context}`
del prompt correspondiente.
"""
from __future__ import annotations

from src.schemas.feeds import NewsItem, SocialPost
from src.schemas.market import Fundamentals, PremarketSnapshot, Quote

#: Limite de elementos para no exceder el contexto del modelo.
_MAX_ITEMS = 40


def format_news_context(items: list[NewsItem]) -> str:
    """Formatea noticias como lineas '- [fuente] titular (tickers, sentimiento)'."""
    lines: list[str] = []
    for it in items[:_MAX_ITEMS]:
        tickers = ", ".join(it.tickers) if it.tickers else "-"
        sentiment = f"{it.sentiment:+.2f}" if it.sentiment is not None else "n/d"
        summary = (it.summary or "").strip().replace("\n", " ")
        if len(summary) > 200:
            summary = summary[:200] + "..."
        line = f"- [{it.source}] {it.headline.strip()} (tickers: {tickers}; sentimiento: {sentiment})"
        if summary:
            line += f"\n  {summary}"
        lines.append(line)
    return "\n".join(lines)


def format_social_context(posts: list[SocialPost], limit: int | None = None) -> str:
    """Formatea posts como lineas '- [plataforma/canal] texto (tickers, score, sentimiento)'.

    Con ``limit=None`` se incluyen todos los posts (el conector social ya acota el
    volumen via ``max_posts``); pasa un entero para recortar explicitamente.
    """
    items = posts[:limit] if limit else posts
    lines: list[str] = []
    for p in items:
        tickers = ", ".join(p.tickers) if p.tickers else "-"
        sentiment = f"{p.sentiment:+.2f}" if p.sentiment is not None else "n/d"
        text = (p.text or "").strip().replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        lines.append(
            f"- [{p.platform}/{p.channel}] {text} "
            f"(tickers: {tickers}; score: {p.score}; sentimiento: {sentiment})"
        )
    return "\n".join(lines)


def format_quote_line(q: Quote) -> str:
    """Formatea un quote como una linea compacta con los campos disponibles."""
    parts = [f"last={q.last}"]
    if q.bid is not None:
        parts.append(f"bid={q.bid}")
    if q.ask is not None:
        parts.append(f"ask={q.ask}")
    if q.volume is not None:
        parts.append(f"vol={q.volume}")
    if q.avg_volume_20d is not None:
        parts.append(f"avg_vol_20d={q.avg_volume_20d}")
    return f"- {q.ticker} ({q.exchange.value}): " + ", ".join(parts)


def format_indicators_context(indicators_by_ticker: dict[str, dict[str, float]]) -> str:
    """Formatea indicadores tecnicos por ticker como bloques legibles."""
    blocks: list[str] = []
    for ticker, ind in indicators_by_ticker.items():
        if not ind:
            continue
        metrics = ", ".join(f"{k}={v}" for k, v in ind.items())
        blocks.append(f"- {ticker}: {metrics}")
    return "\n".join(blocks)


def format_premarket_context(snapshots: list[PremarketSnapshot]) -> str:
    """Formatea snapshots pre-market destacando el gap respecto al cierre previo."""
    lines: list[str] = []
    for s in snapshots[:_MAX_ITEMS]:
        lines.append(
            f"- {s.ticker} ({s.exchange.value}): prev_close={s.previous_close}, "
            f"premarket={s.premarket_price}, gap={s.gap_pct * 100:+.2f}%, "
            f"vol={s.premarket_volume}"
        )
    return "\n".join(lines)


def format_cross_market_context(
    ticker_rows: list[dict],
    index_last_change: dict[str, float],
    sector_summary: dict[str, float] | None = None,
    sector_counts: dict[str, int] | None = None,
    limit: int | None = None,
) -> str:
    """Formatea variaciones diarias de tickers y correlaciones Pearson con indices.

    Cada fila incluye: mercado, variacion diaria y correlacion con cada indice disponible.
    Encabezado muestra la variacion del dia de cada indice de referencia.
    """
    lines: list[str] = []
    if index_last_change:
        idx_summary = "  ".join(
            f"{name}: {chg * 100:+.2f}%" for name, chg in index_last_change.items()
        )
        lines.append(f"Indices de referencia (variacion hoy): {idx_summary}")
        lines.append("")

    by_market: dict[str, list[str]] = {}
    cap = limit if limit is not None else _MAX_ITEMS
    for row in ticker_rows[:cap]:
        ticker = row["ticker"]
        exchange = row["exchange"]
        change = row["change_1d"]
        close = row["close"]
        correlations: dict[str, float] = row.get("correlations", {})
        corr_str = ""
        if correlations:
            corr_str = "  corr[" + ", ".join(
                f"{idx}={v:+.2f}" for idx, v in correlations.items()
            ) + "]"
        sector = row.get("sector")
        sector_str = f"  [{sector}]" if sector else ""
        by_market.setdefault(exchange, []).append(
            f"{ticker}{sector_str}: {change * 100:+.2f}% (close={close}){corr_str}"
        )

    for market, rows in by_market.items():
        lines.append(f"Mercado {market}:")
        lines.extend(f"  {r}" for r in rows)

    if sector_summary:
        lines.append("")
        lines.append("Variacion media por sector (hoy):")
        for sec, avg in sorted(sector_summary.items(), key=lambda x: -abs(x[1])):
            n = (
                sector_counts.get(sec, 0)
                if sector_counts is not None
                else sum(1 for r in ticker_rows if r.get("sector") == sec)
            )
            lines.append(f"  {sec}: {avg * 100:+.2f}% ({n} tickers)")

    return "\n".join(lines)


def format_fundamentals_context(items: list[Fundamentals], limit: int | None = None) -> str:
    """Formatea fundamentales por ticker, omitiendo metricas no disponibles."""
    fields = (
        ("mkt_cap(M)", "market_cap"),
        ("PER", "pe_ratio"),
        ("PEG", "peg_ratio"),
        ("P/S", "ps_ratio"),
        ("P/B", "pb_ratio"),
        ("div_yield%", "dividend_yield"),
        ("ROE%", "roe"),
        ("net_margin%", "net_margin"),
        ("rev_growth%", "revenue_growth_yoy"),
        ("eps_growth%", "eps_growth_yoy"),
        ("debt/eq", "debt_to_equity"),
        ("beta", "beta"),
        ("52w_high", "week52_high"),
        ("52w_low", "week52_low"),
    )
    blocks: list[str] = []
    cap = limit if limit is not None else _MAX_ITEMS
    for f in items[:cap]:
        metrics = [f"{label}={getattr(f, attr)}" for label, attr in fields if getattr(f, attr) is not None]
        name = f" {f.company_name}" if f.company_name else ""
        blocks.append(f"- {f.ticker}{name}: " + (", ".join(metrics) if metrics else "sin datos"))
    return "\n".join(blocks)
