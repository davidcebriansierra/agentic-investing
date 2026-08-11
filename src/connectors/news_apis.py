"""Conector de noticias (spec v2.0, seccion 3.1).

Fuentes: NewsAPI, Marketaux, Finnhub, Alpha Vantage. Normaliza a NewsItem.
Interfaz `NewsClient` + `MockNewsClient` (offline) + `HttpNewsClient` (real via httpx).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from src.connectors._http import (
    DEFAULT_CACHE_TTL,
    FeedCache,
    cache_key,
    cached_json,
    request_json,
)
from src.schemas.feeds import NewsItem

if TYPE_CHECKING:  # pragma: no cover - solo para tipado
    import httpx

logger = logging.getLogger("agentic.connectors.news")


class NewsClient(Protocol):
    async def fetch(self, tickers: list[str] | None = None, since_minutes: int = 30) -> list[NewsItem]: ...


class MockNewsClient:
    """Devuelve noticias predefinidas, opcionalmente filtradas por ticker."""

    def __init__(self, items: list[NewsItem] | None = None) -> None:
        self._items = items or []

    def add(self, item: NewsItem) -> None:
        self._items.append(item)

    async def fetch(
        self, tickers: list[str] | None = None, since_minutes: int = 30
    ) -> list[NewsItem]:
        if tickers is None:
            return list(self._items)
        wanted = set(tickers)
        return [i for i in self._items if wanted.intersection(i.tickers)]


class HttpNewsClient:
    """Cliente real sobre httpx para NewsAPI, Marketaux, Finnhub y Alpha Vantage.

    ``api_keys`` es un dict con las claves opcionales: ``newsapi``, ``marketaux``,
    ``finnhub``, ``alphavantage``. Solo se consultan las fuentes con clave presente.
    El fallo de una fuente se registra y no interrumpe al resto.
    """

    def __init__(
        self,
        api_keys: dict[str, str],
        timeout: float = 10.0,
        cache: FeedCache | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
    ) -> None:
        self.api_keys = api_keys
        self._timeout = timeout
        self._cache = cache
        self._cache_ttl = cache_ttl

    async def fetch(  # pragma: no cover - requiere red/keys
        self, tickers: list[str] | None = None, since_minutes: int = 30
    ) -> list[NewsItem]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc

        # Usar inicio del día anterior para tener más rango de noticias
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        items: list[NewsItem] = []
        sources = (
            self._fetch_newsapi,
            self._fetch_marketaux,
            self._fetch_finnhub,
            self._fetch_alphavantage,
        )
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for source in sources:
                try:
                    items.extend(await source(http, tickers, since))
                except Exception as exc:  # noqa: BLE001 - aislar fallo por fuente
                    logger.warning("Fuente de noticias %s fallo: %s", source.__name__, exc)
        return items

    # ------------------------------------------------------------------
    # Fuentes concretas (cada una devuelve [] si no hay clave configurada)
    # ------------------------------------------------------------------
    async def _fetch_newsapi(
        self, http: httpx.AsyncClient, tickers: list[str] | None, since: datetime
    ) -> list[NewsItem]:
        key = self.api_keys.get("newsapi")
        if not key:
            return []
        # Estrategia: buscar noticias financieras generales del dia.
        # El LLM del NewsSearcher identificara que tickers del universo se ven afectados,
        # en lugar de filtrar aqui por ticker (lo que generaba queries enormes y perdia
        # noticias macro que afectan a varios valores).
        # NewsAPI plan gratuito indexa con ~24h de retraso: usar ventana minima de 24h
        # para evitar siempre devolver 0 articulos con ventanas de 30 min.
        _NEWSAPI_MIN_WINDOW = timedelta(hours=24)
        now = datetime.now(timezone.utc)
        effective_since = min(since, now - _NEWSAPI_MIN_WINDOW)
        # Usar inicio del día anterior para tener más rango de noticias
        day_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        _NEWSAPI_QUERY = (
            "noticias OR news"
        )
        params = {
            "q": _NEWSAPI_QUERY,
            "domains": "elpais.com,expansion.com,eleconomista.es,elconfidencial.com,bloomberg.com,reuters.com, wsj.com,ft.com,cnbc.com",
            "from": day_start.isoformat(),
            "sortBy": "publishedAt",
            "pageSize": 100,
            "apiKey": key,
        }
        ckey = cache_key("newsapi", day_start.strftime("%Y-%m-%d"))
        data = await cached_json(
            self._cache,
            ckey,
            self._cache_ttl,
            lambda: request_json(
                http, "GET", "https://newsapi.org/v2/everything", params=params
            ),
        )
        out: list[NewsItem] = []
        for a in data.get("articles", []):
            if not isinstance(a, dict):
                continue
            out.append(
                NewsItem(
                    source="newsapi",
                    headline=str(a.get("title") or ""),
                    summary=str(a.get("description") or ""),
                    url=str(a.get("url") or "") or None,
                    tickers=[],
                    published_utc=_parse_dt(a.get("publishedAt")),
                )
            )
        return out

    async def _fetch_marketaux(
        self, http: httpx.AsyncClient, tickers: list[str] | None, since: datetime
    ) -> list[NewsItem]:
        key = self.api_keys.get("marketaux")
        if not key:
            return []
        params: dict[str, Any] = {
            "published_after": since.strftime("%Y-%m-%dT%H:%M"),
            "api_token": key,
        }
        if tickers:
            # Marketaux plan gratuito tiene límites; limitar a 20 tickers
            limited_tickers = tickers[:20]
            params["symbols"] = ",".join(_symbol(t) for t in limited_tickers)
        ckey = cache_key("marketaux", sorted(tickers) if tickers else None)
        data = await cached_json(
            self._cache,
            ckey,
            self._cache_ttl,
            lambda: request_json(
                http, "GET", "https://api.marketaux.com/v1/news/all", params=params
            ),
        )
        out: list[NewsItem] = []
        for a in data.get("data", []):
            if not isinstance(a, dict):
                continue
            entities = a.get("entities") or []
            found = [e.get("symbol") for e in entities if isinstance(e, dict) and e.get("symbol")]
            sentiments = [
                e.get("sentiment_score")
                for e in entities
                if isinstance(e, dict) and e.get("sentiment_score") is not None
            ]
            out.append(
                NewsItem(
                    source="marketaux",
                    headline=str(a.get("title") or ""),
                    summary=str(a.get("description") or ""),
                    url=str(a.get("url") or "") or None,
                    tickers=found or list(tickers or []),
                    sentiment=(sum(sentiments) / len(sentiments)) if sentiments else None,
                    published_utc=_parse_dt(a.get("published_at")),
                )
            )
        return out

    async def _fetch_finnhub(
        self, http: httpx.AsyncClient, tickers: list[str] | None, since: datetime
    ) -> list[NewsItem]:
        key = self.api_keys.get("finnhub")
        if not key:
            return []
        # Estrategia: una sola llamada de noticias generales de mercado en lugar de
        # una llamada por ticker (evita rate limit 429 con watchlists grandes).
        # El LLM del NewsSearcher identificara que tickers se ven afectados.
        params = {"category": "general", "minId": 0, "token": key}
        ckey = cache_key("finnhub_general", since.strftime("%Y-%m-%dT%H"))
        articles = await cached_json(
            self._cache,
            ckey,
            self._cache_ttl,
            lambda: request_json(
                http, "GET", "https://finnhub.io/api/v1/news", params=params
            ),
        )
        cutoff_ts = since.timestamp()
        out: list[NewsItem] = []
        for a in (articles if isinstance(articles, list) else []):
            if not isinstance(a, dict):
                continue
            ts = a.get("datetime")
            if isinstance(ts, (int, float)) and ts < cutoff_ts:
                continue
            published = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if isinstance(ts, (int, float)) and ts
                else datetime.now(timezone.utc)
            )
            out.append(
                NewsItem(
                    source="finnhub",
                    headline=str(a.get("headline") or ""),
                    summary=str(a.get("summary") or ""),
                    url=a.get("url"),
                    tickers=[],
                    published_utc=published,
                )
            )
        return out

    async def _fetch_alphavantage(
        self, http: httpx.AsyncClient, tickers: list[str] | None, since: datetime
    ) -> list[NewsItem]:
        key = self.api_keys.get("alphavantage")
        if not key:
            return []
        # Estrategia: sin filtro de tickers — Alpha Vantage devuelve ticker_sentiment[]
        # por articulo con el impacto por empresa. El LLM lo usara directamente.
        # Evita URLs largas y el limite de 25 req/dia del plan gratuito.
        params: dict[str, Any] = {
            "function": "NEWS_SENTIMENT",
            "apikey": key,
            "time_from": since.strftime("%Y%m%dT%H%M"),
            "sort": "LATEST",
            "limit": 50,
        }
        ckey = cache_key("alphavantage_general", since.strftime("%Y-%m-%dT%H"))
        data = await cached_json(
            self._cache,
            ckey,
            self._cache_ttl,
            lambda: request_json(
                http, "GET", "https://www.alphavantage.co/query", params=params
            ),
        )
        out: list[NewsItem] = []
        for a in data.get("feed", []):
            if not isinstance(a, dict):
                continue
            score = a.get("overall_sentiment_score")
            try:
                sentiment = float(score) if score is not None else None
            except (TypeError, ValueError):
                sentiment = None
            # Extraer tickers mencionados en el articulo segun Alpha Vantage
            ticker_sentiments = a.get("ticker_sentiment") or []
            mentioned = [
                ts.get("ticker") for ts in ticker_sentiments
                if isinstance(ts, dict) and ts.get("ticker")
            ]
            out.append(
                NewsItem(
                    source="alphavantage",
                    headline=str(a.get("title") or ""),
                    summary=str(a.get("summary") or ""),
                    url=str(a.get("url") or "") or None,
                    tickers=mentioned,
                    sentiment=sentiment,
                    published_utc=_parse_dt(a.get("time_published")),
                )
            )
        return out


def _symbol(ticker) -> str:
    """Extrae el simbolo base (quita sufijo de exchange, p. ej. 'SAN.MC' -> 'SAN')."""
    return str(ticker).split(".")[0]


def _parse_dt(value: str | None) -> datetime:
    """Parsea fechas de las distintas fuentes; usa 'ahora' si no se puede."""
    if not value:
        return datetime.now(timezone.utc)
    # Alpha Vantage: 'YYYYMMDDTHHMMSS'
    try:
        if "T" in value and "-" not in value and ":" not in value:
            return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # ISO 8601 (NewsAPI/Marketaux), admite sufijo 'Z'
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
