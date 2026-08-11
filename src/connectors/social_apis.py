"""Conector de redes sociales (spec v2.0, seccion 3.1).

Fuentes: Reddit (r/wallstreetbets, r/investing, r/spainfn), StockTwits. Normaliza a
SocialPost. Interfaz `SocialClient` + `MockSocialClient` + `HttpSocialClient` (real).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from src.connectors._http import (
    DEFAULT_CACHE_TTL,
    FeedCache,
    cache_key,
    cached_json,
    request_json,
)
from src.schemas.feeds import SocialPost

if TYPE_CHECKING:  # pragma: no cover - solo para tipado
    import httpx

logger = logging.getLogger("agentic.connectors.social")

_USER_AGENT = "agentic-investing/2.0 (news+social searcher)"
_DEFAULT_SUBREDDITS = ("wallstreetbets", "investing", "stocks")


class SocialClient(Protocol):
    async def fetch(self, tickers: list[str] | None = None, since_minutes: int = 30) -> list[SocialPost]: ...


class MockSocialClient:
    """Devuelve posts predefinidos, opcionalmente filtrados por ticker."""

    def __init__(self, posts: list[SocialPost] | None = None) -> None:
        self._posts = posts or []

    def add(self, post: SocialPost) -> None:
        self._posts.append(post)

    async def fetch(
        self, tickers: list[str] | None = None, since_minutes: int = 30
    ) -> list[SocialPost]:
        if tickers is None:
            return list(self._posts)
        wanted = set(tickers)
        return [p for p in self._posts if wanted.intersection(p.tickers)]


class HttpSocialClient:
    """Cliente real (Reddit + StockTwits) sobre httpx.

    - **Reddit**: requiere ``reddit_client_id``/``reddit_secret`` (OAuth
      client-credentials). Sin credenciales, se omite Reddit.
    - **StockTwits**: API por simbolo; funciona sin clave (limitada por IP) o con
      ``stocktwits_token`` (se envia como query param ``access_token``). Solo se consulta
      si se pasan tickers.

    El fallo de una fuente se registra y no interrumpe al resto.
    """

    def __init__(
        self,
        reddit_client_id: str | None = None,
        reddit_secret: str | None = None,
        stocktwits_token: str | None = None,
        subreddits: tuple[str, ...] = _DEFAULT_SUBREDDITS,
        timeout: float = 10.0,
        cache: FeedCache | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
    ) -> None:
        self.reddit_client_id = reddit_client_id
        self.reddit_secret = reddit_secret
        self.stocktwits_token = stocktwits_token
        self.subreddits = subreddits
        self._timeout = timeout
        self._cache = cache
        self._cache_ttl = cache_ttl

    async def fetch(  # pragma: no cover - requiere red/credenciales
        self, tickers: list[str] | None = None, since_minutes: int = 30
    ) -> list[SocialPost]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc

        cutoff = datetime.now(timezone.utc).timestamp() - since_minutes * 60
        posts: list[SocialPost] = []
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                posts.extend(await self._fetch_reddit(http, tickers, cutoff))
            except Exception as exc:  # noqa: BLE001 - aislar fallo por fuente
                logger.warning("Fuente Reddit fallo: %s", exc)
            try:
                posts.extend(await self._fetch_stocktwits(http, tickers))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fuente StockTwits fallo: %s", exc)
        return posts

    async def _reddit_token(self, http: httpx.AsyncClient) -> str | None:
        if not (self.reddit_client_id and self.reddit_secret):
            return None
        data = await request_json(
            http,
            "POST",
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(self.reddit_client_id, self.reddit_secret),
            headers={"User-Agent": _USER_AGENT},
        )
        return data.get("access_token")

    async def _fetch_reddit(
        self, http: httpx.AsyncClient, tickers: list[str] | None, cutoff: float
    ) -> list[SocialPost]:
        token = await self._reddit_token(http)
        if not token:
            return []
        headers = {"Authorization": f"bearer {token}", "User-Agent": _USER_AGENT}
        wanted = {_symbol(t).upper() for t in tickers} if tickers else None
        out: list[SocialPost] = []
        for sub in self.subreddits:
            url = f"https://oauth.reddit.com/r/{sub}/new"
            ckey = cache_key("reddit", sub)
            payload = await cached_json(
                self._cache,
                ckey,
                self._cache_ttl,
                lambda u=url: request_json(
                    http, "GET", u, params={"limit": 50}, headers=headers
                ),
            )
            for child in payload.get("data", {}).get("children", []):
                d = child.get("data", {})
                created = float(d.get("created_utc") or 0)
                if created < cutoff:
                    continue
                text = f"{d.get('title', '')}\n{d.get('selftext', '')}".strip()
                found = _extract_tickers(text)
                if wanted is not None and not wanted.intersection(found):
                    continue
                out.append(
                    SocialPost(
                        platform="reddit",
                        channel=sub,
                        author=d.get("author"),
                        text=text[:2000],
                        tickers=sorted(found),
                        score=int(d.get("ups") or 0),
                        created_utc=datetime.fromtimestamp(created, tz=timezone.utc),
                    )
                )
        return out

    async def _fetch_stocktwits(
        self, http: httpx.AsyncClient, tickers: list[str] | None
    ) -> list[SocialPost]:
        if not tickers:
            return []
        params = {"access_token": self.stocktwits_token} if self.stocktwits_token else None
        out: list[SocialPost] = []
        safe_tickers = [t for t in tickers if t is not None and t is not True and t is not False]
        _consecutive_errors = 0
        _EARLY_ABORT = 3  # sin token: abortar tras N errores consecutivos al inicio
        for tk in safe_tickers:
            url = f"https://api.stocktwits.com/api/2/streams/symbol/{_symbol(tk)}.json"
            ckey = cache_key("stocktwits", _symbol(tk))
            try:
                payload = await cached_json(
                    self._cache,
                    ckey,
                    self._cache_ttl,
                    lambda u=url: request_json(http, "GET", u, params=params),
                )
            except Exception as exc:  # noqa: BLE001 - 403 para tickers no soportados (p.ej. BME)
                logger.debug("StockTwits skip %s: %s", tk, exc)
                if not self.stocktwits_token:
                    _consecutive_errors += 1
                    if _consecutive_errors >= _EARLY_ABORT:
                        logger.warning(
                            "StockTwits: %d errores consecutivos sin token — omitiendo fuente. "
                            "Añade STOCKTWITS_TOKEN en .env para habilitar.",
                            _consecutive_errors,
                        )
                        return out
                continue
            _consecutive_errors = 0
            for m in payload.get("messages", []):
                sentiment = None
                entities = m.get("entities") or {}
                basic = (entities.get("sentiment") or {}).get("basic")
                if basic == "Bullish":
                    sentiment = 1.0
                elif basic == "Bearish":
                    sentiment = -1.0
                symbols = [s.get("symbol") for s in (m.get("symbols") or []) if s.get("symbol")]
                out.append(
                    SocialPost(
                        platform="stocktwits",
                        channel=_symbol(tk),
                        author=(m.get("user") or {}).get("username"),
                        text=(m.get("body") or "")[:2000],
                        tickers=symbols or [tk],
                        score=int(m.get("likes", {}).get("total", 0)) if isinstance(m.get("likes"), dict) else 0,
                        sentiment=sentiment,
                        created_utc=_parse_iso(m.get("created_at")),
                    )
                )
        return out


class RapidApiSocialClient:
    """Cliente social via RapidAPI.

    - **Socialgrep** (``rapidapi_key`` requerida): busqueda en Reddit por ticker.
      Host: ``socialgrep.p.rapidapi.com``
    - **Finance Social Sentiment** (``rapidapi_key`` requerida): sentimiento de
      StockTwits y Twitter por simbolo.
      Host: ``finance-social-sentiment-for-twitter-and-stocktwits.p.rapidapi.com``

    Ambas fuentes se consultan en paralelo. El fallo de una no interrumpe la otra.
    """

    _SOCIALGREP_HOST = "socialgrep.p.rapidapi.com"
    _SENTIMENT_HOST = (
        "finance-social-sentiment-for-twitter-and-stocktwits.p.rapidapi.com"
    )

    def __init__(
        self,
        rapidapi_key: str,
        subreddits: tuple[str, ...] = _DEFAULT_SUBREDDITS,
        timeout: float = 10.0,
        cache: FeedCache | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
    ) -> None:
        self._key = rapidapi_key
        self._subreddits = subreddits
        self._timeout = timeout
        self._cache = cache
        self._cache_ttl = cache_ttl

    def _headers(self, host: str) -> dict[str, str]:
        return {
            "X-RapidAPI-Key": self._key,
            "X-RapidAPI-Host": host,
        }

    async def fetch(  # pragma: no cover - requiere red/credenciales
        self, tickers: list[str] | None = None, since_minutes: int = 30
    ) -> list[SocialPost]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc

        posts: list[SocialPost] = []
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                posts.extend(await self._fetch_socialgrep(http, tickers, since_minutes))
            except Exception as exc:  # noqa: BLE001
                logger.error("Fuente Socialgrep (Reddit/RapidAPI) fallo: %s", exc, exc_info=True)
            try:
                posts.extend(await self._fetch_sentiment(http, tickers))
            except Exception as exc:  # noqa: BLE001
                logger.error("Fuente Finance Sentiment (RapidAPI) fallo: %s", exc, exc_info=True)
        return posts

    async def _fetch_socialgrep(
        self,
        http: httpx.AsyncClient,
        tickers: list[str] | None,
        since_minutes: int,
    ) -> list[SocialPost]:
        """Busca posts en Reddit via Socialgrep API."""
        out: list[SocialPost] = []
        queries: list[str] = []
        if tickers:
            queries = [f"${_symbol(t)}" for t in tickers]
        else:
            queries = [f"site:reddit.com/r/{sub}" for sub in self._subreddits]

        for query in queries[:5]:  # max 5 queries por llamada para respetar rate-limit
            ckey = cache_key("socialgrep", query)
            url = "https://socialgrep.p.rapidapi.com/api/v1/search/posts"
            try:
                payload = await cached_json(
                    self._cache,
                    ckey,
                    self._cache_ttl,
                    lambda u=url, q=query: request_json(
                        http,
                        "GET",
                        u,
                        params={"query": q, "after": f"{since_minutes}m"},
                        headers=self._headers(self._SOCIALGREP_HOST),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                if _is_plan_blocked(exc):
                    logger.warning("Socialgrep: acceso denegado (403/plan). Omitiendo fuente.")
                    return out
                raise
            for item in payload.get("data", []):
                text = f"{item.get('title', '')} {item.get('selftext', '')}".strip()
                found = _extract_tickers(text)
                out.append(
                    SocialPost(
                        platform="reddit",
                        channel=item.get("subreddit", "unknown"),
                        author=item.get("author"),
                        text=text[:2000],
                        tickers=sorted(found),
                        score=int(item.get("score") or 0),
                        created_utc=datetime.fromtimestamp(
                            float(item.get("created_utc") or 0), tz=timezone.utc
                        ),
                    )
                )
        return out

    async def _fetch_sentiment(
        self,
        http: httpx.AsyncClient,
        tickers: list[str] | None,
    ) -> list[SocialPost]:
        """Obtiene datos sociales via Finance Social Sentiment API.

        Endpoint: GET /get-social-timestamps/{interval}
        Params: social=(twitter|stocktwits), tickers=A,B,C, timestamp=24h
        La API acepta multiples tickers en una sola peticion.
        """
        if not tickers:
            return []
        out: list[SocialPost] = []
        _EXCHANGE_SUFFIXES = {"MC", "L", "PA", "DE", "AS", "MI", "SW", "HK", "TO", "AX"}
        us_tickers = [
            tk for tk in tickers
            if not (len(tk.split(".")) == 2 and tk.split(".")[1].upper() in _EXCHANGE_SUFFIXES)
        ]
        if not us_tickers:
            return []
        syms = [_symbol(tk) for tk in us_tickers[:2]]
        tickers_param = ",".join(syms)

        for social in ("twitter", "stocktwits"):
            url = f"https://{self._SENTIMENT_HOST}/get-social-timestamps/15m"
            ckey = cache_key("rapidapi_sentiment", social, tickers_param)
            try:
                payload = await cached_json(
                    self._cache,
                    ckey,
                    self._cache_ttl,
                    lambda u=url, sp=social, tp=tickers_param: request_json(
                        http,
                        "GET",
                        u,
                        params={"social": sp, "tickers": tp, "timestamp": "24h"},
                        headers=self._headers(self._SENTIMENT_HOST),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                if _is_plan_blocked(exc):
                    logger.warning(
                        "Finance Sentiment (%s): acceso denegado (403/plan). Omitiendo fuente.",
                        social,
                    )
                    return out
                logger.debug("Finance Sentiment skip %s: %s", social, exc)
                continue

            for ticker_key, items in (payload if isinstance(payload, dict) else {}).items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    ts = item.get("timestamp") or item.get("created_at")
                    out.append(
                        SocialPost(
                            platform=social,
                            channel=ticker_key,
                            author=None,
                            text=str(item.get("posts") or item.get("count") or ""),
                            tickers=[ticker_key],
                            score=int(item.get("posts") or item.get("count") or 0),
                            created_utc=_parse_iso(ts),
                        )
                    )
        return out


def _is_plan_blocked(exc: BaseException) -> bool:
    """Devuelve True si el error es un 403 Forbidden (acceso denegado por plan)."""
    return "403" in str(exc) or "Forbidden" in str(exc)


def _symbol(ticker) -> str:
    """Extrae el simbolo base (quita sufijo de exchange, p. ej. 'SAN.MC' -> 'SAN')."""
    return str(ticker).split(".")[0]


def _extract_tickers(text: str) -> set[str]:
    """Extrae cashtags ($AAPL) del texto y devuelve el conjunto de simbolos en mayusculas."""
    found: set[str] = set()
    for token in text.split():
        if token.startswith("$") and 2 <= len(token) <= 6 and token[1:].isalpha():
            found.add(token[1:].upper())
    return found


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
