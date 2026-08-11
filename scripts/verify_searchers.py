"""Verificacion rapida: los buscadores news/fundamental generan oportunidades, pasan por
el pipeline (evaluacion + decision) y se persisten.

Ejecuta una sola pasada de los buscadores de noticias y fundamentales (sin esperar al
scheduler), imprime las oportunidades, las procesa por el `DecisionPipeline` (que guarda
``opportunities``, ``evaluations`` y ``decisions``) y muestra el recuento por tabla.
Persiste en Postgres si hay ``POSTGRES_DSN``; en caso contrario usa un repositorio en
memoria para poder mostrar igualmente los recuentos.

Requisitos:
- ``OPENAI_API_KEY`` (o ANTHROPIC/Azure): sin clave, el LLM es un mock y no genera nada.
- ``NEWSAPI_KEY`` y/o ``FINNHUB_KEY``: sin ellas, se usan feeds mock (posiblemente vacios).
- ``POSTGRES_DSN`` (opcional): si esta, persiste en la BD real; si no, repositorio memoria.
- ``config.llm.evaluation_engine``: ``heuristic`` (evaluadores deterministicos) o ``llm``.

Uso:
    python -m scripts.verify_searchers
"""
from __future__ import annotations

import asyncio
import logging
import os

from src.agents.searchers import (
    CrossMarketSearcher,
    FundamentalSearcher,
    NewsSearcher,
    PremarketSearcher,
    SocialSearcher,
    TechnicalSearcher,
)
from src.connectors.factory import (
    build_fundamentals_client,
    build_market_data_client,
    build_news_client,
    build_social_client,
)
from src.graph.factory import build_llm_client, build_pipeline, resolve_engine
from src.persistence.postgres import PostgresRepository
from src.persistence.repository import InMemoryRepository
from src.schemas.portfolio import Portfolio
from src.utils.config import get_config
from src.utils.env import load_env

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("verify")


def _build_repository():
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        logger.info("Sin POSTGRES_DSN: usando InMemoryRepository (recuentos en memoria).")
        return InMemoryRepository()
    repo = PostgresRepository(dsn)
    repo.init_schema()
    logger.info("Postgres conectado (esquema verificado).")
    return repo


async def main() -> None:
    load_env()
    cfg = get_config()
    watchlist = cfg.get("watchlist") or None
    llm = build_llm_client(cfg)
    logger.info("LLM: %s | evaluation_engine=%s", type(llm).__name__, resolve_engine(cfg))
    if type(llm).__name__ == "MockLLMClient":
        logger.warning("Sin clave LLM real: no se generaran oportunidades (mock).")

    news_client = build_news_client(cfg)
    social_client = build_social_client(cfg)
    os.environ["IBKR_CLIENT_ID_READ"] = "20"  # evitar colision con app (client_id=10)
    market_data = build_market_data_client(cfg)
    fundamentals_client = build_fundamentals_client(cfg)

    print("\n=== Conectores activos ===")
    print(f"  LLM:           {type(llm).__name__}")
    print(f"  News:          {type(news_client).__name__}")
    print(f"  Social:        {type(social_client).__name__}")
    print(f"  MarketData:    {type(market_data).__name__}")
    print(f"  Fundamentals:  {type(fundamentals_client).__name__}")

    searchers = [
        NewsSearcher(client=news_client, watchlist=watchlist, llm=llm),
        SocialSearcher(client=social_client, watchlist=watchlist, llm=llm),
        TechnicalSearcher(market_data=market_data, watchlist=watchlist, llm=llm),
        PremarketSearcher(market_data=market_data, watchlist=watchlist, llm=llm),
        FundamentalSearcher(
            watchlist=watchlist,
            llm=llm,
            fundamentals_client=fundamentals_client,
        ),
        CrossMarketSearcher(market_data=market_data, watchlist=watchlist, llm=llm),
    ]

    repo = _build_repository()
    pipeline = build_pipeline(cfg, repository=repo, llm=llm)
    equity = float(cfg.get("system", {}).get("initial_equity", 100_000.0))
    portfolio = Portfolio(total_equity=equity, cash=equity)

    # Conecta market_data antes de usarlo (necesario para IBKRMarketDataClient)
    try:
        await market_data.connect()
        logger.info("MarketData conectado: %s", type(market_data).__name__)
    except Exception as exc:
        logger.warning("MarketData no se pudo conectar (%s): %s", type(market_data).__name__, exc)

    all_opps = []
    for s in searchers:
        print(f"\n=== {s.source.value} ({type(s).__name__}) ===")
        # Mostrar contexto que construye el agente antes de pasar al LLM
        build_context = getattr(s, "build_context", None)
        if build_context is not None:
            try:
                ctx = await build_context()
                print(f"  Contexto ({len(ctx)} chars): {ctx[:300]!r}" if ctx else "  Contexto: VACIO (sin datos de la fuente)")
            except Exception as exc:
                print(f"  Error al construir contexto: {exc}")
        else:
            # NewsSearcher / SocialSearcher: muestra items del cliente
            client = getattr(s, "client", None)
            if client is not None:
                print(f"  Cliente: {type(client).__name__}")

        opps = await s.search()
        all_opps.extend(opps)
        print(f"  → {len(opps)} oportunidades generadas")
        for o in opps:
            print(
                f"     {o.ticker} {o.direction.value} entry={o.entry_price} "
                f"TP={o.take_profit} SL={o.stop_loss} R/R={o.risk_reward_ratio} "
                f"p_win={o.estimated_win_probability}"
            )
            print(f"     just: {o.justification[:120]}")

    # Pasa por el pipeline: guarda opportunities + evaluations + decisions. Sin approval_fn
    # ni execute_fn el flujo se detiene antes del HITL/ejecucion ("no-operar por defecto").
    print(f"\nProcesando {len(all_opps)} oportunidades por el pipeline...")
    states = await pipeline.arun(all_opps, portfolio)
    operate = sum(
        1 for st in states if st.decision is not None
        and st.decision.decision.value == "OPERATE"
    )

    print("\n--- Recuentos persistidos ---")
    for table in ("opportunities", "evaluations", "decisions"):
        print(f"  {table}: {repo.count(table)}")
    print(f"  decisiones OPERATE: {operate} / {len(states)}")


if __name__ == "__main__":
    asyncio.run(main())
