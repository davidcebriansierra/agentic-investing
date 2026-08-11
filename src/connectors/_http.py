"""Utilidades HTTP compartidas por los conectores de feeds (noticias/redes).

Aporta dos mecanismos, ambos con **degradacion elegante** (funcionan aunque falte la
dependencia o el servicio):

- ``request_json``: peticion GET/POST con **reintentos y backoff exponencial + jitter**
  ante errores transitorios (429 rate-limit, 5xx, timeouts, errores de transporte).
- ``FeedCache`` + ``cached_json``: **cache opcional en Redis** con TTL corto para deduplicar
  llamadas y respetar los limites de las APIs. Si no hay ``REDIS_URL`` o el paquete
  ``redis`` no esta instalado, la cache se deshabilita silenciosamente.

Parametros configurables por entorno:
- ``FEEDS_HTTP_MAX_ATTEMPTS`` (def. 3), ``FEEDS_HTTP_BASE_DELAY`` (def. 1.0s),
  ``FEEDS_HTTP_MAX_DELAY`` (def. 10.0s).
- ``FEEDS_CACHE_TTL`` (def. 120s), ``REDIS_URL``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - solo para tipado
    import httpx

logger = logging.getLogger("agentic.connectors.http")

_MAX_RESPONSE_LOG_BYTES = int(os.getenv("FEEDS_LOG_RESPONSE_BYTES", str(10 * 1024)))

_MAX_ATTEMPTS = int(os.getenv("FEEDS_HTTP_MAX_ATTEMPTS", "3"))
_BASE_DELAY = float(os.getenv("FEEDS_HTTP_BASE_DELAY", "1.0"))
_MAX_DELAY = float(os.getenv("FEEDS_HTTP_MAX_DELAY", "10.0"))
DEFAULT_CACHE_TTL = int(os.getenv("FEEDS_CACHE_TTL", "120"))

_SENSITIVE_PARAM_KEYS = frozenset({
    "token", "apikey", "api_key", "key", "secret",
    "x-rapidapi-key", "authorization",
})


def _sanitize(mapping: dict[str, Any] | None) -> dict[str, Any]:
    """Devuelve una copia del mapping con los valores sensibles enmascarados."""
    if not mapping:
        return {}
    out = {}
    for k, v in mapping.items():
        out[k] = "***" if k.lower() in _SENSITIVE_PARAM_KEYS else v
    return out


def _is_transient(exc: Exception, httpx_mod: Any) -> bool:
    """True si el error merece reintento (rate-limit, 5xx, timeout, transporte)."""
    if isinstance(exc, (httpx_mod.TransportError, httpx_mod.TimeoutException)):
        return True
    if isinstance(exc, httpx_mod.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


async def request_json(
    http: "httpx.AsyncClient",
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any | None = None,
    auth: tuple[str, str] | None = None,
) -> Any:
    """Ejecuta la peticion y devuelve el JSON, reintentando errores transitorios.

    El backoff es exponencial (``base * 2**(intento-1)``, acotado a ``MAX_DELAY``) con
    jitter aleatorio para evitar sincronizacion de reintentos. Los errores no transitorios
    (4xx distintos de 429) se propagan de inmediato.

    - ``data``: cuerpo **form-urlencoded** (``application/x-www-form-urlencoded``).
    - ``json_body``: cuerpo **JSON** (``application/json``); usar este para APIs que
      esperan JSON (p.ej. Apify run-sync).
    """
    import httpx

    from src.agents._agent_logger import log_entry as _log_entry

    sanitized_params = _sanitize(params)
    sanitized_headers = _sanitize(dict(headers) if headers else {})
    logger.debug(
        "API call: %s %s | params=%s | headers=%s",
        method,
        url,
        sanitized_params,
        sanitized_headers,
    )
    _log_entry("http", {
        "event": "http_request",
        "method": method,
        "url": url,
        "params": sanitized_params,
        "headers": sanitized_headers,
    })
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            resp = await http.request(
                method, url, params=params, headers=headers,
                data=data, json=json_body, auth=auth,
            )
            elapsed_ms = round((time.monotonic() - t0) * 1000)
            resp.raise_for_status()
            body = resp.json()
            body_raw = resp.text
            _log_entry("http", {
                "event": "http_response",
                "method": method,
                "url": url,
                "status": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "attempt": attempt,
                "response_body": body_raw[:_MAX_RESPONSE_LOG_BYTES],
                "truncated": len(body_raw) > _MAX_RESPONSE_LOG_BYTES,
            })
            return body
        except Exception as exc:  # noqa: BLE001 - se filtra por _is_transient
            elapsed_ms = round((time.monotonic() - t0) * 1000)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            _log_entry("http", {
                "event": "http_error",
                "method": method,
                "url": url,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "attempt": attempt,
                "error": str(exc),
            })
            if not _is_transient(exc, httpx) or attempt == _MAX_ATTEMPTS:
                raise
            delay = min(_MAX_DELAY, _BASE_DELAY * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)  # jitter
            logger.warning(
                "HTTP %s %s fallo (intento %d/%d): %s. Reintento en %.1fs",
                method, url, attempt, _MAX_ATTEMPTS, exc, delay,
            )
            last_exc = exc
            await asyncio.sleep(delay)
    assert last_exc is not None  # pragma: no cover - defensivo
    raise last_exc


def cache_key(*parts: Any) -> str:
    """Clave de cache estable a partir de las partes dadas (fuente, tickers, etc.)."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return "feeds:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class FeedCache:
    """Cache asincrona sobre Redis. Todas las operaciones degradan a no-op ante error."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    @classmethod
    def from_env(cls) -> "FeedCache | None":
        """Construye la cache desde ``REDIS_URL``; None si no hay Redis disponible."""
        url = os.getenv("REDIS_URL")
        if not url:
            logger.info("Sin REDIS_URL: cache de feeds deshabilitada.")
            return None
        # En Windows con ProactorEventLoop, "localhost" puede resolver a ::1 (IPv6)
        # y causar TimeoutError. Forzar 127.0.0.1 evita el problema.
        url = url.replace("localhost", "127.0.0.1")
        try:
            from redis.asyncio import from_url
        except ImportError:
            logger.info("redis no instalado: cache de feeds deshabilitada.")
            return None
        try:
            client = from_url(url, decode_responses=True, socket_timeout=2.0, socket_connect_timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo crear cliente Redis (%s): cache deshabilitada.", exc)
            return None
        logger.info("Cache de feeds activa (Redis en %s).", url)
        return cls(client)

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            _is_transient = "Timeout" in type(exc).__name__ or "Connection" in type(exc).__name__
            log = logger.debug if _is_transient else logger.warning
            log("Cache get fallo (%s): %s", key, exc)
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self._redis.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            _is_transient = "Timeout" in type(exc).__name__ or "Connection" in type(exc).__name__
            log = logger.debug if _is_transient else logger.warning
            log("Cache set fallo (%s): %s", key, exc)


async def cached_json(
    cache: FeedCache | None,
    key: str,
    ttl: int,
    factory: Callable[[], Awaitable[Any]],
) -> Any:
    """Devuelve el valor cacheado si existe; si no, ejecuta ``factory`` y lo cachea."""
    if cache is not None:
        hit = await cache.get(key)
        if hit is not None:
            logger.debug("Cache HIT %s", key)
            return hit
    value = await factory()
    if cache is not None and value is not None:
        await cache.set(key, value, ttl)
    return value
