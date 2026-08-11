"""Fabrica de conectores externos segun el modo de operacion (TRADING_MODE).

Selecciona la implementacion real o el mock para lectura (market data) y escritura
(broker) de IBKR:

- ``TRADING_MODE`` = ``PAPER`` | ``LIVE`` con IB Gateway disponible -> conectores reales
  (``IBKRMarketDataClient`` / ``IBKRBrokerClient`` via ``ib_async``).
- Cualquier otro modo, o si ``ib_async`` no esta instalado -> conectores mock.

El modo se toma de la variable de entorno ``TRADING_MODE`` y, en su defecto, de
``config.system.trading_mode``. Los parametros de conexion se leen de ``.env``:
``IBKR_HOST``, ``IBKR_PORT``, ``IBKR_CLIENT_ID_READ``, ``IBKR_CLIENT_ID_WRITE``,
``IBKR_ACCOUNT``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from src.connectors.ibkr_read import (
    IBKRMarketDataClient,
    MarketDataClient,
    MockMarketDataClient,
)
from src.connectors.ibkr_write import (
    BrokerClient,
    IBKRBrokerClient,
    MockBrokerClient,
)
from src.connectors._http import FeedCache
from src.connectors.fundamentals import (
    FallbackFundamentalsClient,
    FundamentalsClient,
    HttpFundamentalsClient,
    MockFundamentalsClient,
    YFinanceFundamentalsClient,
)
from src.connectors.news_apis import HttpNewsClient, MockNewsClient, NewsClient
from src.connectors.apify_social_apis import (
    ApifySocialClient,
    build_apify_social_client,
)
from src.connectors.social_apis import (
    HttpSocialClient,
    MockSocialClient,
    RapidApiSocialClient,
    SocialClient,
)
from src.connectors.telegram_bot import (
    HITLClient,
    MockHITLClient,
    TelegramHITLClient,
)

logger = logging.getLogger("agentic.connectors")

# Puerto por defecto del IB Gateway segun el modo.
_DEFAULT_PORTS = {"PAPER": 4002, "LIVE": 4001}
_REAL_MODES = {"PAPER", "LIVE"}


def resolve_trading_mode(config: dict[str, Any]) -> str:
    """Modo de operacion: env TRADING_MODE tiene prioridad sobre config.system."""
    mode = os.getenv("TRADING_MODE") or config.get("system", {}).get(
        "trading_mode", "PAPER"
    )
    return str(mode).strip().upper()


def use_real_connectors(config: dict[str, Any]) -> bool:
    """True si el modo exige conectores reales (PAPER/LIVE)."""
    return resolve_trading_mode(config) in _REAL_MODES


def _ibkr_common(config: dict[str, Any]) -> dict[str, Any]:
    mode = resolve_trading_mode(config)
    port = int(os.getenv("IBKR_PORT", str(_DEFAULT_PORTS.get(mode, 4002))))
    return {"host": os.getenv("IBKR_HOST", "127.0.0.1"), "port": port, "mode": mode}


def build_market_data_client(config: dict[str, Any]) -> MarketDataClient:
    """Cliente de lectura: real (IBKR) en PAPER/LIVE, mock en otro caso o sin ib_async."""
    if not use_real_connectors(config):
        logger.info(
            "TRADING_MODE=%s: usando MockMarketDataClient.",
            resolve_trading_mode(config),
        )
        return MockMarketDataClient()
    common = _ibkr_common(config)
    client_id = int(os.getenv("IBKR_CLIENT_ID_READ", "10"))
    try:
        client = IBKRMarketDataClient(
            host=common["host"], port=common["port"], client_id=client_id
        )
        logger.info(
            "TRADING_MODE=%s: usando IBKRMarketDataClient (%s:%s, client_id=%s).",
            common["mode"], common["host"], common["port"], client_id,
        )
        return client
    except ImportError as exc:
        logger.warning(
            "ib_async no disponible (%s): fallback a MockMarketDataClient.", exc
        )
        return MockMarketDataClient()


def build_broker_client(config: dict[str, Any]) -> BrokerClient:
    """Cliente de escritura: real (IBKR) en PAPER/LIVE, mock en otro caso o sin ib_async."""
    if not use_real_connectors(config):
        logger.info(
            "TRADING_MODE=%s: usando MockBrokerClient.",
            resolve_trading_mode(config),
        )
        return MockBrokerClient()
    common = _ibkr_common(config)
    client_id = int(os.getenv("IBKR_CLIENT_ID_WRITE", "11"))
    account = os.getenv("IBKR_ACCOUNT", "")
    try:
        client = IBKRBrokerClient(
            host=common["host"],
            port=common["port"],
            client_id=client_id,
            account=account,
        )
        logger.info(
            "TRADING_MODE=%s: usando IBKRBrokerClient (%s:%s, client_id=%s, account=%s).",
            common["mode"], common["host"], common["port"], client_id, account or "<sin cuenta>",
        )
        return client
    except ImportError as exc:
        logger.warning(
            "ib_async no disponible (%s): fallback a MockBrokerClient.", exc
        )
        return MockBrokerClient()


def build_news_client(config: dict[str, Any] | None = None) -> NewsClient:
    """Cliente de noticias real si hay alguna API key en .env; mock en otro caso."""
    api_keys = {
        "newsapi": os.getenv("NEWSAPI_KEY", ""),
        "marketaux": os.getenv("MARKETAUX_KEY", ""),
        "finnhub": os.getenv("FINNHUB_KEY", ""),
        "alphavantage": os.getenv("ALPHAVANTAGE_KEY", ""),
    }
    active = {k: v for k, v in api_keys.items() if v}
    if not active:
        logger.info("Sin API keys de noticias: usando MockNewsClient.")
        return MockNewsClient()
    logger.info("Usando HttpNewsClient con fuentes: %s.", ", ".join(sorted(active)))
    return HttpNewsClient(api_keys=active, cache=FeedCache.from_env())


def build_fundamentals_client(config: dict[str, Any] | None = None) -> FundamentalsClient:
    """Cliente de fundamentales real (Finnhub + yfinance fallback) si hay ``FINNHUB_KEY``; solo yfinance si no; mock en otro caso."""
    key = os.getenv("FINNHUB_KEY", "")
    cache = FeedCache.from_env()

    if key:
        logger.info("Usando FallbackFundamentalsClient (Finnhub + yfinance fallback).")
        finnhub = HttpFundamentalsClient(api_key=key, cache=cache)
        yfinance = YFinanceFundamentalsClient(cache=cache)
        return FallbackFundamentalsClient(finnhub_client=finnhub, yfinance_client=yfinance)

    # Sin Finnhub, intentar usar solo yfinance
    logger.info("Sin FINNHUB_KEY: usando YFinanceFundamentalsClient (Yahoo Finance).")
    return YFinanceFundamentalsClient(cache=cache)


def build_social_client(config: dict[str, Any] | None = None) -> SocialClient:
    """Cliente social real.

    Prioridad de seleccion:
    1. ``APIFY_TOKEN`` presente -> ``ApifySocialClient`` (Reddit + Stocktwits via Apify).
    2. ``RAPIDAPI_KEY`` presente -> ``RapidApiSocialClient`` (Socialgrep + Finance
       Social Sentiment).
    3. ``REDDIT_CLIENT_ID`` + ``REDDIT_SECRET`` y/o ``STOCKTWITS_TOKEN`` ->
       ``HttpSocialClient`` (APIs directas).
    4. Sin credenciales -> ``MockSocialClient``.
    """
    apify_token = os.getenv("APIFY_TOKEN", "")
    if apify_token:
        logger.info("Usando ApifySocialClient (Reddit + Stocktwits via Apify).")
        return build_apify_social_client(config or {}, cache=FeedCache.from_env())

    rapidapi_key = os.getenv("RAPIDAPI_KEY", "")
    if rapidapi_key:
        logger.info("Usando RapidApiSocialClient (Socialgrep + Finance Sentiment via RapidAPI).")
        return RapidApiSocialClient(
            rapidapi_key=rapidapi_key,
            cache=FeedCache.from_env(),
        )

    client_id = os.getenv("REDDIT_CLIENT_ID", "")
    secret = os.getenv("REDDIT_SECRET", "")
    stocktwits_token = os.getenv("STOCKTWITS_TOKEN", "")
    has_reddit = bool(client_id and secret)
    fuentes = ["StockTwits (publico)"]
    if stocktwits_token:
        fuentes = ["StockTwits (token)"]
    if has_reddit:
        fuentes.append("Reddit")
    logger.info("Usando HttpSocialClient (%s).", " + ".join(fuentes))
    return HttpSocialClient(
        reddit_client_id=client_id or None,
        reddit_secret=secret or None,
        stocktwits_token=stocktwits_token or None,
        cache=FeedCache.from_env(),
    )


def _parse_authorized_users(raw: str) -> list[int]:
    """Convierte '12345,67890' en [12345, 67890], ignorando valores no numericos."""
    users: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            users.append(int(part))
        except ValueError:
            logger.warning("TELEGRAM_AUTHORIZED_USERS: '%s' no es un id valido, se ignora.", part)
    return users


def build_hitl_client(config: dict[str, Any] | None = None) -> HITLClient:
    """Cliente HITL de Telegram si hay token y usuarios autorizados; mock en otro caso.

    El mock devuelve TIMEOUT por defecto (no operar), respetando "no-operar por defecto".
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    users = _parse_authorized_users(os.getenv("TELEGRAM_AUTHORIZED_USERS", ""))
    if not (token and users):
        logger.info("Sin TELEGRAM_BOT_TOKEN/usuarios: usando MockHITLClient (TIMEOUT).")
        return MockHITLClient()
    try:
        client = TelegramHITLClient(bot_token=token, authorized_users=users)
    except ImportError as exc:
        logger.warning(
            "python-telegram-bot no disponible (%s): fallback a MockHITLClient.", exc
        )
        return MockHITLClient()
    logger.info("Usando TelegramHITLClient (%d usuarios autorizados).", len(users))
    return client
