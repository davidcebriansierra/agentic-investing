"""Entrypoint del Sistema Agentico de Inversion.

Arranca la observabilidad (endpoint /metrics), registra los buscadores en el scheduler
y mantiene el bucle de ejecucion. En este MVP los buscadores son stubs (devuelven []),
por lo que el sistema opera en modo "no-operar por defecto" hasta cablear los
conectores reales. La ejecucion real requiere IB Gateway, BBDD y claves.
"""
from __future__ import annotations

import asyncio
import logging
import signal

import os
from datetime import datetime, timezone

from src.agents.executor import Executor
from src.governance.audit_logger import build_audit_logger
from src.agents.monitor import Monitor
from src.agents.searchers import (
    CrossMarketSearcher,
    FundamentalSearcher,
    NewsSearcher,
    PremarketSearcher,
    SocialSearcher,
    TechnicalSearcher,
)
from src.connectors.factory import (
    build_broker_client,
    build_fundamentals_client,
    build_hitl_client,
    build_market_data_client,
    build_news_client,
    build_social_client,
)
from src.connectors.telegram_bot import build_request
from src.governance.kill_switch import KillSwitch
from src.graph.factory import build_llm_client, build_pipeline, resolve_engine
from src.messaging.streams import EventPublisher, InMemoryStreamBus, MessageBus, RedisStreamBus
from src.observability.metrics import get_metrics
from src.persistence.postgres import PostgresRepository
from src.persistence.repository import InMemoryRepository, Repository
from src.scheduler.scheduler import SimpleScheduler, within_trading_window
from src.schemas.decision import Decision
from src.schemas.enums import Exchange
from src.schemas.hitl import ApprovalDecision, EditedPrices
from src.schemas.opportunity import Opportunity
from src.schemas.portfolio import Portfolio
from src.utils.config import get_config
from src.utils.env import load_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agentic.app")

# Fuera de la ventana operativa, los jobs se sondean con esta cadencia (segundos) para
# reanudar con prontitud al abrirse la ventana, sin esperar el intervalo completo del job.
_PAUSED_POLL_SECONDS = 60


def build_searchers(
    config: dict | None = None,
    news_client=None,
    social_client=None,
    market_data=None,
    llm=None,
    fundamentals_client=None,
) -> list:
    """Construye los 6 buscadores inyectando LLM, conectores de feeds y market data.

    La `watchlist` (universo vigilado) se toma de `config.watchlist`. El mismo cliente
    LLM se comparte entre todos los buscadores; si es un mock (sin claves), no generan
    oportunidades ("no-operar por defecto"). Los buscadores de mercado reciben el cliente
    de market data (OHLCV/pre-market) y el fundamental recibe el cliente de Finnhub.
    """
    watchlist = (config or {}).get("watchlist") or None
    searchers_cfg = (config or {}).get("searchers", {})
    cross_cfg = searchers_cfg.get("cross_market", {})
    tech_cfg = searchers_cfg.get("technical", {})
    social_cfg = searchers_cfg.get("social", {})
    fund_cfg = searchers_cfg.get("fundamental", {})
    premkt_cfg = searchers_cfg.get("premarket", {})
    markets_cfg = (config or {}).get("markets", [])
    return [
        CrossMarketSearcher(
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            fundamentals_client=fundamentals_client,
            interval_minutes=int(cross_cfg.get("interval_minutes", 15)),
            min_move_pct=float(cross_cfg.get("min_move_pct", 0.0)),
            movers_count=int(cross_cfg.get("movers_count", 25)),
            sector_cache_ttl=int(cross_cfg.get("sector_ttl_hours", 24)) * 3600,
        ),
        NewsSearcher(client=news_client, watchlist=watchlist, llm=llm),
        SocialSearcher(
            client=social_client,
            watchlist=watchlist,
            llm=llm,
            interval_minutes=int(social_cfg.get("interval_minutes", 30)),
            context_max_posts=social_cfg.get("context_max_posts"),
        ),
        PremarketSearcher(
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            interval_minutes=int(premkt_cfg.get("interval_minutes", 5)),
            markets=markets_cfg,
            lead_minutes=int(premkt_cfg.get("lead_minutes", 10)),
            post_open_minutes=int(premkt_cfg.get("post_open_minutes", 10)),
            top_movers=int(premkt_cfg.get("top_movers", 20)),
            gap_batch_size=int(premkt_cfg.get("gap_batch_size", 20)),
            min_gap_pct=float(premkt_cfg.get("min_gap_pct", 1.0)),
        ),
        TechnicalSearcher(
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            interval_minutes=int(tech_cfg.get("interval_minutes", 10)),
            ohlcv_bars=int(tech_cfg.get("ohlcv_bars", 60)),
            ohlcv_interval=str(tech_cfg.get("ohlcv_interval", "5 mins")),
            batch_size=int(tech_cfg.get("batch_size", 50)),
        ),
        FundamentalSearcher(
            market_data=market_data,
            watchlist=watchlist,
            llm=llm,
            fundamentals_client=fundamentals_client,
            interval_minutes=int(fund_cfg.get("interval_minutes", 1440)),
            batch_size=int(fund_cfg.get("batch_size", 40)),
        ),
    ]


def build_repository(config: dict | None = None) -> Repository:
    """PostgresRepository si hay POSTGRES_DSN (y psycopg); InMemoryRepository si no.

    Ante cualquier fallo (SDK ausente, BD no accesible) degrada a memoria con aviso, para
    no impedir el arranque en modo offline/shadow.
    """
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        logger.info("Sin POSTGRES_DSN: usando InMemoryRepository.")
        return InMemoryRepository()
    try:
        repo = PostgresRepository(dsn)
        repo.init_schema()
        logger.info("Persistencia: PostgresRepository (esquema verificado).")
        return repo
    except Exception as exc:  # noqa: BLE001 - degradar sin abortar
        logger.warning("Postgres no disponible (%s): usando InMemoryRepository.", exc)
        return InMemoryRepository()


def build_message_bus(config: dict | None = None) -> MessageBus:
    """RedisStreamBus si hay REDIS_URL (y redis); InMemoryStreamBus si no."""
    url = os.getenv("REDIS_URL")
    if not url:
        logger.info("Sin REDIS_URL: usando InMemoryStreamBus.")
        return InMemoryStreamBus()
    try:
        bus = RedisStreamBus(url)
        logger.info("Mensajeria: RedisStreamBus (%s).", url)
        return bus
    except Exception as exc:  # noqa: BLE001 - degradar sin abortar
        logger.warning("Redis no disponible (%s): usando InMemoryStreamBus.", exc)
        return InMemoryStreamBus()


class Application:
    def __init__(self) -> None:
        self.config = get_config()
        self.metrics = get_metrics()
        self.scheduler = SimpleScheduler()
        self.bus = build_message_bus(self.config)
        self.publisher = EventPublisher(self.bus)
        self.repository = build_repository(self.config)
        self.kill_switch = KillSwitch()
        self.audit = build_audit_logger()
        # Conectores de feeds: reales si hay claves en .env, mock en otro caso.
        self.news_client = build_news_client(self.config)
        self.social_client = build_social_client(self.config)
        self.fundamentals_client = build_fundamentals_client(self.config)
        # Conectores externos: reales (IBKR) o mock segun TRADING_MODE.
        self.market_data = build_market_data_client(self.config)
        self.broker = build_broker_client(self.config)
        # LLM compartido por los buscadores para razonar oportunidades (mock si no hay clave).
        self.searcher_llm = build_llm_client(self.config)
        self.searchers = build_searchers(
            self.config,
            news_client=self.news_client,
            social_client=self.social_client,
            market_data=self.market_data,
            llm=self.searcher_llm,
            fundamentals_client=self.fundamentals_client,
        )
        self.engine = resolve_engine(self.config)
        self.pipeline = build_pipeline(self.config, repository=self.repository)
        self.executor = Executor(
            self.broker,
            account=os.getenv("IBKR_ACCOUNT", ""),
            kill_switch=self.kill_switch,
            audit=self.audit,
            market_data=self.market_data,
        )
        self.monitor = Monitor(kill_switch=self.kill_switch, audit=self.audit, metrics=self.metrics)
        # Canal de aprobacion humana (HITL): Telegram real o mock (TIMEOUT=no operar).
        self.hitl = build_hitl_client(self.config)
        self._stop = asyncio.Event()
        # Estado del monitor: ancla de equity al inicio de sesion (para el drawdown
        # intradia) y contador de perdidas consecutivas (a actualizar cuando se cablee
        # el feedback de resultados de trades).
        self._equity_day_open: float | None = None
        self._equity_day = None
        self._consecutive_losses = 0

    def _portfolio(self) -> Portfolio:
        equity = float(self.config.get("system", {}).get("initial_equity", 100_000.0))
        return Portfolio(total_equity=equity, cash=equity)

    async def _approve(self, decision: Decision, opp: Opportunity) -> bool:
        """Callback HITL: solicita aprobacion humana y devuelve si se puede operar.

        Solo ``APPROVE`` habilita la ejecucion. ``PAUSE_1H`` activa el kill switch durante
        una hora. ``REJECT``/``TIMEOUT`` no operan (principio "no-operar por defecto").
        """
        response = await self.hitl.request_approval(build_request(decision, opp))
        if response.decision == ApprovalDecision.PAUSE_1H:
            self.kill_switch.activate("hitl_pause_1h")
            self._schedule_resume(3600)
            logger.warning("HITL: pausa de 1h solicitada; kill switch activado.")
        if response.approved and response.edited_prices is not None:
            self._apply_edited_prices(opp, response.edited_prices)
        logger.info(
            "HITL %s -> %s (por %s)",
            opp.ticker, response.decision.value, response.responder or "-",
        )
        return response.approved

    @staticmethod
    def _apply_edited_prices(opp: Opportunity, edited: EditedPrices) -> None:
        """Aplica a la oportunidad los precios editados por el operador en el HITL.

        El pipeline (`DecisionPipeline._aprocess_one`) pasa el MISMO objeto `opp` al
        callback de ejecucion, asi que mutarlo aqui hace que la orden se construya con los
        valores editados (entrada/SL/TP). Antes se descartaban y la orden salia con los
        precios originales.
        """
        opp.entry_price = edited.entry_price
        opp.stop_loss = edited.stop_loss
        opp.take_profit = edited.take_profit
        logger.info(
            "HITL: precios editados aplicados a %s (entry=%s SL=%s TP=%s)",
            opp.ticker, edited.entry_price, edited.stop_loss, edited.take_profit,
        )

    def _schedule_resume(self, seconds: int) -> None:
        """Reactiva el trading (desactiva el kill switch) tras `seconds` segundos."""
        async def _resume() -> None:
            await asyncio.sleep(seconds)
            self.kill_switch.deactivate()
            self.monitor.reset_kill_switch_metric()
            logger.info("HITL: pausa finalizada; kill switch desactivado.")

        asyncio.create_task(_resume())

    async def _execute(self, decision: Decision, opp: Opportunity):
        """Callback de ejecucion: construye el contexto de mercado y envia la orden.

        Solo se invoca por el pipeline si la decision ha sido aprobada (HITL).
        """
        exchange = Exchange.BME if opp.ticker.endswith(".MC") else Exchange.NYSE
        ctx = await self.market_data.build_market_context(opp.ticker, exchange)
        return await self.executor.execute(decision, opp, ctx)

    async def _process(self, opportunities: list[Opportunity]) -> None:
        """Pasa las oportunidades por el pipeline asincrono (evaluacion + decision + ejecucion)."""
        if not opportunities:
            return
        if self.kill_switch.active:
            logger.warning("Kill switch activo: se omite el procesamiento.")
            return
        portfolio = self._portfolio()
        # La ejecucion (execute_fn) SOLO se dispara si la decision queda aprobada por el
        # HITL (approval_fn -> state.approved). Con el mock, la aprobacion es TIMEOUT por
        # defecto, respetando el principio "no-operar por defecto".
        states = await self.pipeline.arun(
            opportunities,
            portfolio,
            approval_fn=self._approve,
            execute_fn=self._execute,
        )
        for state in states:
            if state.decision is None:
                continue
            self.publisher.publish_decision(
                state.decision,
                state.consolidated[0] if state.consolidated else None,
            )
            self.metrics.decisions_total.labels(
                decision=state.decision.decision.value
            ).inc()
            if not state.risk_pass:
                self.metrics.risk_rejections_total.inc()

    async def _monitor_tick(self) -> None:
        """Revision periodica de riesgo: P&L no realizado, stop-loss y kill switch.

        Lee la cartera real (fallback al stub si el conector falla), registra el P&L no
        realizado, marca posiciones sin stop-loss y evalua el kill switch con el drawdown
        intradia (equity actual frente al de apertura de la sesion).
        """
        try:
            portfolio = await self.market_data.get_portfolio()
        except Exception as exc:  # noqa: BLE001 - sin cartera no aborta el monitor
            logger.debug("monitor: no se pudo leer la cartera (%s); usando fallback.", exc)
            portfolio = self._portfolio()

        # Ancla el equity de apertura y lo resetea al cambiar de dia (UTC).
        today = datetime.now(timezone.utc).date()
        if self._equity_day != today or self._equity_day_open is None:
            self._equity_day = today
            self._equity_day_open = portfolio.total_equity

        self.monitor.review_pnl(portfolio)
        self.monitor.check_stop_losses(portfolio)

        drawdown = Monitor.intraday_drawdown_pct(self._equity_day_open, portfolio.total_equity)
        event = self.monitor.evaluate_kill_switch(
            intraday_drawdown_pct=drawdown,
            consecutive_losses=self._consecutive_losses,
        )
        if event is not None:
            logger.warning("monitor: kill switch activado (%s).", event.data.get("reason"))

    def _register_jobs(self) -> None:
        for searcher in self.searchers:
            async def runner(s=searcher) -> None:
                opportunities = await s.search()
                for opp in opportunities:
                    self.publisher.publish_opportunity(opp)
                    self.metrics.opportunities_total.labels(
                        agent_source=opp.agent_source.value
                    ).inc()
                await self._process(opportunities)

            self.scheduler.register_searcher(searcher, runner)
            logger.info(
                "Registrado buscador %s cada %s min",
                searcher.source.value,
                searcher.interval_minutes,
            )

        monitor_interval = float(self.config.get("monitor", {}).get("interval_minutes", 15))
        self.scheduler.add_job("monitor", monitor_interval, self._monitor_tick)
        logger.info("Registrado monitor cada %s min", monitor_interval)

    async def _job_loop(self, job) -> None:
        """Bucle periodico para un job.

        Ejecuta el job y luego espera `interval_minutes`. Si hay una ventana operativa
        configurada (`trading_window`) y el instante actual queda fuera, el job se pausa:
        no se ejecuta y el bucle sondea cada `_PAUSED_POLL_SECONDS` para reanudar en cuanto
        se abra la ventana.
        """
        while not self._stop.is_set():
            if within_trading_window(self.config):
                try:
                    logger.debug("Ejecutando job %s", job.job_id)
                    await self.scheduler.run_job(job.job_id)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Error en job %s: %s", job.job_id, exc)
                wait_seconds = job.interval_minutes * 60
            else:
                logger.debug("Fuera de ventana operativa; job %s en pausa.", job.job_id)
                wait_seconds = min(job.interval_minutes * 60, _PAUSED_POLL_SECONDS)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass  # intervalo/sondeo cumplido, siguiente iteracion

    def _start_jobs(self) -> None:
        """Lanza una coroutine asyncio por cada job registrado."""
        for job in self.scheduler.jobs():
            asyncio.create_task(self._job_loop(job), name=job.job_id)

    async def start(self) -> None:
        mode = self.config.get("system", {}).get("trading_mode", "PAPER")
        logger.info(
            "Iniciando Sistema Agentico de Inversion (modo=%s, evaluacion=%s)",
            mode,
            self.engine,
        )

        metrics_cfg = self.config.get("metrics", {})
        metrics_port = int(metrics_cfg.get("port", 8000))
        metrics_addr = metrics_cfg.get("bind_address", "0.0.0.0")  # noqa: S104
        if self.metrics.start_server(port=metrics_port, addr=metrics_addr):
            logger.info("Endpoint de metricas en %s:%s/metrics", metrics_addr, metrics_port)
        else:
            logger.warning("prometheus_client no disponible: metricas deshabilitadas")

        window = self.config.get("trading_window", {})
        if window.get("enabled", False):
            logger.info(
                "Ventana operativa: %s-%s %s (%s). Fuera de ella el sistema se pausa por completo.",
                window.get("start", "08:30"), window.get("end", "22:00"),
                "L-V" if window.get("weekdays_only", True) else "todos los dias",
                window.get("timezone", "Europe/Madrid"),
            )
        else:
            logger.info("Sin ventana operativa configurada: el sistema opera 24/7.")

        await self._connect_clients()
        await self._start_hitl()
        self._register_jobs()
        self._start_jobs()
        logger.info("Sistema listo. Esperando senales (Ctrl+C para salir).")
        try:
            await self._stop.wait()
        finally:
            await self._shutdown()

    async def _start_hitl(self) -> None:
        """Arranca el bot HITL si es el cliente real de Telegram. No aborta si falla."""
        start = getattr(self.hitl, "start", None)
        if start is None:
            return
        try:
            await start()
        except Exception as exc:  # noqa: BLE001 - degradar sin abortar
            logger.warning("No se pudo iniciar el bot HITL (%s): %s", type(self.hitl).__name__, exc)

    async def _shutdown(self) -> None:
        """Cierre ordenado: detiene el bot HITL de Telegram si aplica."""
        stop = getattr(self.hitl, "stop", None)
        if stop is not None:
            try:
                await stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error al detener el bot HITL: %s", exc)

    async def _connect_clients(self) -> None:
        """Conecta los conectores externos (real IBKR o mock). No aborta si falla."""
        for name, client in (("market_data", self.market_data), ("broker", self.broker)):
            try:
                await client.connect()
                logger.info(
                    "Conector %s listo (%s).", name, type(client).__name__
                )
            except Exception as exc:  # noqa: BLE001 - degradar con aviso, no abortar
                logger.warning(
                    "No se pudo conectar el conector %s (%s): %s",
                    name, type(client).__name__, exc,
                )

    def stop(self) -> None:
        self._stop.set()


async def _amain() -> None:
    load_env()  # carga .env antes de construir los factories (LLM, conectores, IBKR).
    app = Application()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await app.start()


def _configure_logging() -> None:
    import logging.handlers
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("agentic").setLevel(logging.DEBUG)
    logging.getLogger("agentic.connectors.http").setLevel(logging.DEBUG)

    log_dir = Path(os.getenv("AGENTS_LOG_DIR", "logs/agents"))
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    _SEARCHER_LOGGERS: list[tuple[str, str]] = [
        ("agentic.searchers.news",         "news"),
        ("agentic.searchers.social",       "social"),
        ("agentic.searchers.technical",    "technical"),
        ("agentic.searchers.premarket",    "premarket"),
        ("agentic.searchers.cross_market", "cross_market"),
        ("agentic.searchers.fundamental",  "fundamental"),
        ("agentic.searchers.llm",          "llm"),
        ("agentic.connectors.http",        "http_console"),
        ("agentic.connectors.social",      "social"),
        ("agentic.connectors",             "connectors"),
    ]
    for logger_name, file_stem in _SEARCHER_LOGGERS:
        log = logging.getLogger(logger_name)
        log.setLevel(logging.DEBUG)
        path = log_dir / f"{file_stem}.log"
        if not any(
            isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(path.resolve())
            for h in log.handlers
        ):
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            log.addHandler(fh)


def main() -> None:
    _configure_logging()
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
