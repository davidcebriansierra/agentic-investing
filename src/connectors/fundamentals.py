"""Conector de datos fundamentales (spec v2.0, seccion 3.1).

Fuentes:
- Finnhub (endpoints ``/stock/metric`` y ``/stock/profile2``)
- yfinance (Yahoo Finance, fallback cuando Finnhub falla)

Normaliza a ``Fundamentals``. Define la interfaz ``FundamentalsClient`` + ``MockFundamentalsClient``
(offline/tests) + ``HttpFundamentalsClient`` (real via httpx) + ``YFinanceFundamentalsClient``
(fallback via yfinance), reutilizando la infra HTTP compartida (reintentos + cache opcional en Redis).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from src.connectors._http import (
    DEFAULT_CACHE_TTL,
    FeedCache,
    cache_key,
    cached_json,
    request_json,
)
from src.schemas.market import Fundamentals

if TYPE_CHECKING:  # pragma: no cover - solo para tipado
    import httpx

logger = logging.getLogger("agentic.connectors.fundamentals")

#: TTL largo por defecto: los fundamentales cambian poco (1h).
_FUND_CACHE_TTL = 3600


# Sufijos de exchange a eliminar (no son parte del simbolo en Finnhub)
_EXCHANGE_SUFFIXES = {"MC", "L", "PA", "DE", "AS", "MI", "SW", "HK", "TO", "AX"}


def _symbol(ticker: str) -> str:
    """Convierte 'IBE.MC' -> 'IBE.MC' para Finnhub (requiere sufijo .MC para IBEX 35).
    Preserva 'BRK.B' -> 'BRK.B' y 'BF.B' -> 'BF.B' (sufijos de clase de acción).

    Para otros exchanges (L, PA, DE, etc.), elimina el sufijo como antes.
    """
    parts = ticker.split(".")
    if len(parts) == 2:
        suffix = parts[1].upper()
        # Finnhub requiere .MC para acciones del IBEX 35
        if suffix == "MC":
            return ticker  # Preservar .MC
        # Para otros exchanges conocidos, eliminar el sufijo
        if suffix in _EXCHANGE_SUFFIXES:
            return parts[0]
    return ticker


#: Sufijos de exchange que Finnhub free tier NO soporta (devuelve 403).
#: Estos tickers se enrutan directamente a yfinance.
_FINNHUB_UNSUPPORTED_SUFFIXES = {
    "MC", "L", "PA", "DE", "AS", "MI", "SW", "HK", "TO", "AX", "BR", "LS", "VI",
}


def _has_unsupported_suffix(ticker: str) -> bool:
    """True si el ticker tiene un sufijo de exchange no soportado por Finnhub free."""
    parts = ticker.rsplit(".", 1)
    return len(parts) == 2 and parts[1].upper() in _FINNHUB_UNSUPPORTED_SUFFIXES


async def _empty() -> list[Fundamentals]:
    """Coroutine auxiliar que devuelve una lista vacia (para asyncio.gather)."""
    return []


def _first(data: dict, *keys: str) -> float | None:
    """Devuelve el primer valor numerico presente entre `keys`, o None."""
    for key in keys:
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


class FundamentalsClient(Protocol):
    async def fetch(self, tickers: list[str]) -> list[Fundamentals]: ...


class MockFundamentalsClient:
    """Devuelve fundamentales predefinidos, filtrados por ticker."""

    def __init__(self, items: dict[str, Fundamentals] | None = None) -> None:
        self._items = items or {}

    def add(self, item: Fundamentals) -> None:
        self._items[item.ticker] = item

    async def fetch(self, tickers: list[str]) -> list[Fundamentals]:
        if not tickers:
            return list(self._items.values())
        return [self._items[t] for t in tickers if t in self._items]


class HttpFundamentalsClient:
    """Cliente real sobre httpx para Finnhub. Requiere ``FINNHUB_KEY``."""

    #: Delay entre peticiones para respetar el rate-limit de Finnhub free (30 req/min -> 2s/req)
    _REQUEST_DELAY = 2.1

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        cache: FeedCache | None = None,
        cache_ttl: int = _FUND_CACHE_TTL,
        request_delay: float = _REQUEST_DELAY,
    ) -> None:
        self.api_key = api_key
        self._timeout = timeout
        self._cache = cache
        self._cache_ttl = cache_ttl
        self._request_delay = request_delay
        self._semaphore = asyncio.Semaphore(1)  # una peticion a la vez

    async def fetch(self, tickers: list[str]) -> list[Fundamentals]:  # pragma: no cover - red/keys
        if not tickers:
            return []
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc

        out: list[Fundamentals] = []
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for ticker in tickers:
                try:
                    async with self._semaphore:
                        result = await self._fetch_one(http, ticker)
                        out.append(result)
                        await asyncio.sleep(self._request_delay)
                except Exception as exc:  # noqa: BLE001 - un ticker fallido no aborta el resto
                    logger.warning("Finnhub fundamentals fallo para %s: %s", ticker, exc)
        return out

    async def _fetch_one(self, http: "httpx.AsyncClient", ticker: str) -> Fundamentals:  # pragma: no cover
        symbol = _symbol(ticker)
        metric_params = {"symbol": symbol, "metric": "all", "token": self.api_key}
        profile_params = {"symbol": symbol, "token": self.api_key}

        metric_raw = await cached_json(
            self._cache,
            cache_key("finnhub_metric", symbol),
            self._cache_ttl,
            lambda: request_json(
                http, "GET", "https://finnhub.io/api/v1/stock/metric", params=metric_params
            ),
        )
        profile = await cached_json(
            self._cache,
            cache_key("finnhub_profile", symbol),
            self._cache_ttl,
            lambda: request_json(
                http, "GET", "https://finnhub.io/api/v1/stock/profile2", params=profile_params
            ),
        )
        metric = (metric_raw or {}).get("metric", {}) or {}
        profile = profile or {}
        return _to_fundamentals(ticker, metric, profile)


def _to_fundamentals(ticker: str, metric: dict[str, Any], profile: dict[str, Any]) -> Fundamentals:
    """Mapea la respuesta de Finnhub al contrato `Fundamentals` (tolerante a claves ausentes)."""
    return Fundamentals(
        ticker=ticker,
        company_name=profile.get("name"),
        sector=profile.get("finnhubIndustry") or profile.get("gsector"),
        market_cap=_first(profile, "marketCapitalization") or _first(metric, "marketCapitalization"),
        pe_ratio=_first(metric, "peTTM", "peBasicExclExtraTTM", "peNormalizedAnnual"),
        peg_ratio=_first(metric, "pegTTM", "pegRatio"),
        ps_ratio=_first(metric, "psTTM", "psAnnual"),
        pb_ratio=_first(metric, "pbQuarterly", "pbAnnual"),
        dividend_yield=_first(metric, "dividendYieldIndicatedAnnual", "currentDividendYieldTTM"),
        roe=_first(metric, "roeTTM", "roeRfy"),
        net_margin=_first(metric, "netProfitMarginTTM", "netProfitMarginAnnual"),
        revenue_growth_yoy=_first(metric, "revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"),
        eps_growth_yoy=_first(metric, "epsGrowthTTMYoy", "epsGrowthQuarterlyYoy"),
        debt_to_equity=_first(
            metric, "totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual",
            "longTermDebt/equityQuarterly", "longTermDebt/equityAnnual",
        ),
        beta=_first(metric, "beta"),
        week52_high=_first(metric, "52WeekHigh"),
        week52_low=_first(metric, "52WeekLow"),
    )


class YFinanceFundamentalsClient:
    """Cliente de fundamentales via yfinance (fallback cuando Finnhub falla).

    yfinance es una librería Python que obtiene datos de Yahoo Finance sin API key.
    Útil como fallback cuando Finnhub devuelve 403 o falla.
    """

    def __init__(
        self,
        cache: FeedCache | None = None,
        cache_ttl: int = _FUND_CACHE_TTL,
    ) -> None:
        self._cache = cache
        self._cache_ttl = cache_ttl

    async def fetch(self, tickers: list[str]) -> list[Fundamentals]:  # pragma: no cover
        """Obtiene fundamentales via yfinance."""
        try:
            import yfinance as yf
        except ImportError as exc:
            logger.warning("yfinance no esta instalado. Instala con: pip install yfinance")
            return []

        out: list[Fundamentals] = []
        for ticker in tickers:
            try:
                # yfinance usa el mismo formato que el ticker (ej: ACS.MC, AAPL).
                yf_symbol = _symbol(ticker)

                # Obtener datos en un thread pool para no bloquear el event loop
                loop = asyncio.get_event_loop()
                ticker_obj = await loop.run_in_executor(None, yf.Ticker, yf_symbol)

                # Obtener info fundamental
                info = await loop.run_in_executor(None, lambda: ticker_obj.info)
                if not info:
                    logger.warning("yfinance no devolvio info para %s", ticker)
                    continue

                out.append(_yf_to_fundamentals(ticker, info))
            except Exception as exc:  # noqa: BLE001 - un ticker fallido no aborta el resto
                logger.warning("yfinance fallo para %s: %s", ticker, exc)
        return out


def _yf_to_fundamentals(ticker: str, info: dict[str, Any]) -> Fundamentals:
    """Mapea la respuesta de yfinance al contrato `Fundamentals`."""
    return Fundamentals(
        ticker=ticker,
        company_name=info.get("longName") or info.get("shortName"),
        sector=info.get("sector"),
        market_cap=info.get("marketCap"),
        pe_ratio=info.get("trailingPE") or info.get("forwardPE"),
        peg_ratio=info.get("pegRatio"),
        ps_ratio=info.get("priceToSalesTrailing12Months"),
        pb_ratio=info.get("priceToBook"),
        dividend_yield=info.get("dividendYield"),
        roe=info.get("returnOnEquity"),
        net_margin=info.get("profitMargins"),
        revenue_growth_yoy=info.get("revenueGrowth"),
        eps_growth_yoy=info.get("earningsGrowth"),
        debt_to_equity=info.get("debtToEquity"),
        beta=info.get("beta"),
        week52_high=info.get("fiftyTwoWeekHigh"),
        week52_low=info.get("fiftyTwoWeekLow"),
    )


class FallbackFundamentalsClient:
    """Cliente compuesto que usa Finnhub primero y yfinance como fallback."""

    def __init__(
        self,
        finnhub_client: HttpFundamentalsClient | None = None,
        yfinance_client: YFinanceFundamentalsClient | None = None,
    ) -> None:
        self.finnhub = finnhub_client
        self.yfinance = yfinance_client or YFinanceFundamentalsClient()

    async def fetch(self, tickers: list[str]) -> list[Fundamentals]:
        """Obtiene fundamentales combinando Finnhub y yfinance.

        Estrategia:
        - Los tickers con sufijo de exchange no soportado por Finnhub free tier
          (p.ej. ``.MC`` del IBEX) se envían **directamente a yfinance**, evitando
          cientos de respuestas 403 lentas.
        - El resto (US, sin sufijo) va a Finnhub; los que fallen o vengan sin datos
          significativos se reintentan con yfinance.
        - Finnhub y la rama directa de yfinance se ejecutan **en paralelo**.
        """
        if not tickers:
            return []

        # Sin Finnhub: usar solo yfinance para todo.
        if not self.finnhub:
            logger.info("FallbackFundamentals: sin Finnhub, usando yfinance para %d tickers", len(tickers))
            return await self.yfinance.fetch(tickers)

        # Separar tickers que Finnhub free no soporta (con sufijo de exchange).
        finnhub_tickers = [t for t in tickers if not _has_unsupported_suffix(t)]
        yf_direct_tickers = [t for t in tickers if _has_unsupported_suffix(t)]
        logger.info(
            "FallbackFundamentals: %d tickers -> Finnhub, %d tickers -> yfinance directo",
            len(finnhub_tickers), len(yf_direct_tickers),
        )

        # Ejecutar Finnhub y yfinance-directo en paralelo.
        finnhub_task = self.finnhub.fetch(finnhub_tickers) if finnhub_tickers else _empty()
        yf_direct_task = self.yfinance.fetch(yf_direct_tickers) if yf_direct_tickers else _empty()
        finnhub_results, yf_direct_results = await asyncio.gather(finnhub_task, yf_direct_task)

        # Detectar tickers de Finnhub sin datos significativos -> fallback yfinance.
        successful = {
            r.ticker for r in finnhub_results
            if r and (r.market_cap or r.pe_ratio or r.company_name)
        }
        finnhub_failed = [t for t in finnhub_tickers if t not in successful]
        logger.info(
            "Finnhub: %d ok, %d fallidos (fallback yfinance)",
            len(successful), len(finnhub_failed),
        )

        yf_fallback_results: list[Fundamentals] = []
        if finnhub_failed:
            yf_fallback_results = await self.yfinance.fetch(finnhub_failed)

        # Combinar: solo Finnhub exitosos + yfinance (directo + fallback).
        finnhub_ok = [r for r in finnhub_results if r.ticker in successful]
        return finnhub_ok + yf_direct_results + yf_fallback_results
