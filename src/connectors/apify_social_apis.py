"""Conector de redes sociales via Apify (Twitter).

Fuentes: Apify Actor para Twitter (danek/twitter-scraper).
Normaliza a SocialPost. Requiere API token de Apify.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

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

logger = logging.getLogger("agentic.connectors.apify_social")

# Apify API base URL
_APIFY_API_URL = "https://api.apify.com/v2"

# Apify Actor para Twitter
_TWITTER_ACTOR = "danek~twitter-scraper"


def _parse_twitter_ts(value: str | None) -> datetime:
    """Parsea el ``created_at`` de un tweet.

    Soporta el formato clásico de Twitter ("Wed Oct 10 20:19:24 +0000 2018")
    y el formato ISO-8601. Si falla, devuelve el instante actual (para no
    descartar el post por un problema de formato).
    """
    if not value:
        return datetime.now(timezone.utc)
    # Formato clásico de Twitter.
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        pass
    # Formato ISO-8601.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


# Frases tipicas de spam cripto/airdrop y de engagement-bait que contaminan la senal
# social. Se comparan en minusculas contra el texto del tweet.
_JUNK_PHRASES = (
    "sniper",                         # "sniper alert/bot" (cripto)
    "portal open",                    # airdrop / claim portal
    "portal is open",
    "claim portal",
    "self-custody",
    "self custody",
    "move tokens",
    "move your tokens",
    "redemption plan",
    "airdrop",
    "presale",
    "pre-sale",
    "whitelist",
    "sharing my trading experience",  # engagement-bait / get-rich
    "turning my initial",
)
# Direccion de contrato (0x + hex): marcador inequivoco de scam cripto.
_CONTRACT_ADDR_RE = re.compile(r"0x[0-9a-f]{6,}")
# Cashtag: "$" seguido de letra (ignora importes como "$70" o "$10,000").
_CASHTAG_RE = re.compile(r"\$[A-Z][A-Z0-9]*")
# A partir de este numero de cashtags DISTINTOS el tweet es un volcado de simbolos (spam);
# los tweets legitimos suelen citar 1-2 tickers.
_CASHTAG_FLOOD = 5


def _is_junk(text_upper: str, text_lower: str) -> bool:
    """Pre-filtro heuristico: True si el tweet es spam y debe descartarse.

    Cubre tres familias de ruido que degradan la senal del SocialSearcher:
    frases de scam cripto/airdrop o engagement-bait, direcciones de contrato ``0x...`` y
    volcados de cashtags (listas de simbolos sin contenido). Recibe el texto ya en
    mayusculas y minusculas (el llamador las calcula una vez por tweet).
    """
    if any(phrase in text_lower for phrase in _JUNK_PHRASES):
        return True
    if _CONTRACT_ADDR_RE.search(text_lower):
        return True
    if len(set(_CASHTAG_RE.findall(text_upper))) >= _CASHTAG_FLOOD:
        return True
    return False


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


class ApifySocialClient:
    """Cliente social via Apify Actor (Twitter).

    - **Twitter**: Actor `danek/twitter-scraper` extrae tweets por búsqueda.
      Soporta búsqueda por ticker (ej: "$AAPL", "AAPL stock").

    Requiere ``apify_token`` en config o variable de entorno ``APIFY_TOKEN``.
    """

    def __init__(
        self,
        apify_token: str | None = None,
        timeout: float = 30.0,
        cache: FeedCache | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        batch_size: int = 20,
        max_posts: int = 100,
    ) -> None:
        self.apify_token = apify_token
        self._timeout = timeout
        self._cache = cache
        self._cache_ttl = cache_ttl
        # Nº de tickers por ciclo: se consultan juntos en UN unico run de Apify con una
        # query OR de cashtags ($ACS OR $SAN OR ...). La watchlist se cubre por rotacion
        # a lo largo de varios ciclos, controlando coste/latencia.
        self.batch_size = max(1, batch_size)
        # Nº maximo de tweets que devuelve el run (se reparte entre todos los cashtags).
        self.max_posts = max(1, max_posts)
        self._rotation_offset = 0

    async def fetch(  # pragma: no cover - requiere red/token
        self, tickers: list[str] | None = None, since_minutes: int = 30
    ) -> list[SocialPost]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc

        if not self.apify_token:
            logger.warning("ApifySocialClient: sin API token, omitiendo fuente.")
            return []
        if not tickers:
            return []

        cutoff = datetime.now(timezone.utc).timestamp() - since_minutes * 60
        batch = self._select_batch(tickers)
        logger.info(
            "Apify Twitter: lote de %d/%d tickers este ciclo (proximo offset=%d): %s",
            len(batch), len(tickers), self._rotation_offset, batch,
        )
        posts: list[SocialPost] = []
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                posts.extend(await self._fetch_twitter_apify(http, batch, cutoff))
            except Exception as exc:  # noqa: BLE001 - aislar fallo de la fuente
                logger.warning("Fuente Twitter (Apify) fallo: %s", exc)
        return posts

    def _select_batch(self, tickers: list[str]) -> list[str]:
        """Selecciona el siguiente lote rotando por la watchlist en cada ciclo.

        Devuelve ``batch_size`` tickers (con wrap-around) empezando en el offset actual
        y avanza el offset para el proximo ciclo, de modo que a lo largo de varios ciclos
        se cubre toda la lista sin disparar el coste de Apify.
        """
        n = len(tickers)
        if n == 0:
            return []
        size = min(self.batch_size, n)
        start = self._rotation_offset % n
        batch = [tickers[(start + i) % n] for i in range(size)]
        self._rotation_offset = (start + size) % n
        return batch

    async def _fetch_twitter_apify(
        self,
        http: httpx.AsyncClient,
        tickers: list[str],
        cutoff_ts: float,
    ) -> list[SocialPost]:
        """Extrae tweets de un lote de tickers en UN run (query OR de cashtags)."""
        if not tickers:
            return []
        # Twitter usa cashtags sin sufijo de exchange (ej: ACS.MC -> $ACS).
        # bases: (simbolo_base_mayus, ticker_original) para reasignar menciones despues.
        bases = [(t.split(".")[0].upper(), t) for t in tickers]
        cashtags = [f"${base}" for base, _ in bases]
        search_query = " OR ".join(cashtags)
        logger.info(
            "Apify Twitter: %d cashtags en un run: %s", len(cashtags), search_query,
        )

        actor_input = {
            "query": search_query,
            "search_type": "Latest",
            "max_posts": self.max_posts,
        }
        url = f"{_APIFY_API_URL}/acts/{_TWITTER_ACTOR}/run-sync-get-dataset-items"
        headers = {"Authorization": f"Bearer {self.apify_token}"}

        ckey = cache_key("apify_twitter", search_query)
        data = await cached_json(
            self._cache,
            ckey,
            self._cache_ttl,
            lambda: request_json(http, "POST", url, json_body=actor_input, headers=headers),
        )

        logger.info("Apify Twitter: respuesta recibida, tipo: %s, items: %s", type(data), len(data) if isinstance(data, list) else "N/A")

        out: list[SocialPost] = []
        items = data if isinstance(data, list) else []
        # Patrones por ticker (precompilados una vez por run):
        #  - cashtag con frontera: "$A" NO debe casar dentro de "$AAPL"/"$AMD".
        #  - simbolo suelto (sin $): solo para simbolos de 2+ caracteres; los de 1 letra
        #    (A, J, L, ...) coinciden con articulos/palabras comunes y son puro ruido,
        #    asi que para esos exigimos el cashtag.
        matchers: list[tuple[str, str, re.Pattern[str], re.Pattern[str] | None]] = []
        for base, orig in bases:
            cashtag = f"${base}"
            cashtag_re = re.compile(rf"\${re.escape(base)}(?![A-Z0-9])")
            token_re = (
                re.compile(rf"(?<![A-Z0-9$#]){re.escape(base)}(?![A-Z0-9])")
                if len(base) >= 2
                else None
            )
            matchers.append((orig, cashtag, cashtag_re, token_re))
        dropped_empty = dropped_no_mention = older_than_window = dropped_junk = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("full_text") or ""
            if not text:
                dropped_empty += 1
                continue

            text_upper = text.upper()
            text_lower = text.lower()
            # Pre-filtro de spam (memecoins/airdrops, scam cripto, volcado de cashtags):
            # descartar antes de emparejar tickers para no contaminar la senal social.
            if _is_junk(text_upper, text_lower):
                dropped_junk += 1
                continue

            # ¿Que tickers del lote menciona este tweet? Un tweet puede citar varios;
            # se etiqueta con todos los que coincidan (cashtag con frontera o, para
            # simbolos de 2+ chars, el simbolo suelto como palabra).
            mentioned_tickers: list[str] = []
            matched_cashtags: list[str] = []
            for orig, cashtag, cashtag_re, token_re in matchers:
                if cashtag_re.search(text_upper) or (
                    token_re is not None and token_re.search(text_upper)
                ):
                    mentioned_tickers.append(orig)
                    matched_cashtags.append(cashtag)
            if not mentioned_tickers:
                dropped_no_mention += 1
                continue

            # Parse timestamp (Twitter usa formato propio, no ISO)
            ts = _parse_twitter_ts(item.get("created_at"))

            # El actor devuelve los tweets más recientes ("Latest"); para valores poco
            # líquidos los últimos pueden superar la ventana `since_minutes`. No los
            # descartamos (perderíamos toda la señal); solo lo registramos.
            if ts.timestamp() < cutoff_ts:
                older_than_window += 1

            # Sentimiento simple basado en heurísticas (puede mejorarse con LLM)
            sentiment_score = 0.0
            bullish_words = ["bullish", "buy", "long", "moon", "rocket", "pump", "strong", "up"]
            bearish_words = ["bearish", "sell", "short", "dump", "crash", "weak", "down", "bad"]
            bullish_count = sum(1 for word in bullish_words if word in text_lower)
            bearish_count = sum(1 for word in bearish_words if word in text_lower)
            if bullish_count > bearish_count:
                sentiment_score = 0.5
            elif bearish_count > bullish_count:
                sentiment_score = -0.5

            score = int(item.get("favorites") or 0) + int(item.get("retweets") or 0)
            out.append(
                SocialPost(
                    platform="twitter",
                    channel=" ".join(matched_cashtags),
                    author=item.get("screen_name") or "unknown",
                    text=text[:2000],
                    tickers=mentioned_tickers,
                    score=score,
                    sentiment=sentiment_score,
                    created_utc=ts,
                )
            )
        logger.info(
            "Apify Twitter [%d cashtags]: %d items -> %d posts (vacios=%d, spam=%d, sin_mencion=%d, fuera_de_ventana=%d)",
            len(cashtags), len(items), len(out), dropped_empty, dropped_junk, dropped_no_mention, older_than_window,
        )
        return out


def build_apify_social_client(
    config: dict[str, Any],
    cache: FeedCache | None = None,
) -> SocialClient:
    """Fabrica de SocialClient desde config.

    Busca ``apify_token`` en config o variable de entorno ``APIFY_TOKEN``.
    Si no hay token, devuelve MockSocialClient vacio.
    """
    import os

    token = config.get("apify_token") or os.getenv("APIFY_TOKEN")
    if token:
        social_cfg = config.get("searchers", {}).get("social", {})
        batch_size = int(
            social_cfg.get("apify_batch_size")
            or config.get("apify_twitter_batch_size")
            or os.getenv("APIFY_TWITTER_BATCH_SIZE", 20)
        )
        max_posts = int(
            social_cfg.get("apify_max_posts")
            or config.get("apify_twitter_max_posts")
            or os.getenv("APIFY_TWITTER_MAX_POSTS", 100)
        )
        logger.info(
            "Usando ApifySocialClient (Twitter via Apify, lote=%d, max_posts=%d).",
            batch_size, max_posts,
        )
        return ApifySocialClient(
            apify_token=token, cache=cache, batch_size=batch_size, max_posts=max_posts
        )
    logger.warning("Sin APIFY_TOKEN, usando MockSocialClient (sin datos sociales).")
    return MockSocialClient()
