"""Conector de lectura de IBKR (spec v2.0: connectors/ibkr_read_mcp.py).

Expone portfolio, quotes y estado de mercado para alimentar searchers, risk filter,
executor y monitor. En produccion se respalda en un MCP server (`ib-mcp` /
`ibkr-tws-mcp` con persistent pooling). Aqui se define la interfaz `MarketDataClient`
y un `MockMarketDataClient` deterministico para tests y shadow mode sin red.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

from src.connectors.ibkr_contracts import BME_SYMBOL_TO_BASE, make_stock_contract
from src.governance.pre_trade_validator import MarketContext
from src.schemas.enums import Direction, Exchange
from src.schemas.market import OHLCVBar, PremarketSnapshot, Quote
from src.schemas.portfolio import Portfolio, Position

logger = logging.getLogger("agentic.connectors.ibkr")


class _IBWrapperNoiseFilter(logging.Filter):
    """Colapsa el spam de 'Error 162 ... different IP address' de ib_async.wrapper.

    Deja pasar un mensaje por ventana y descarta los repetidos (uno por contrato). El
    resto de logs de ib_async pasa intacto. El cliente ya emite un diagnostico unico y
    omite las peticiones durante la ventana, asi que estos ERROR por contrato son ruido.
    """

    _WINDOW = 90.0

    def __init__(self) -> None:
        super().__init__()
        self._last_emit = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "162" in message and "different IP address" in message:
            now = time.monotonic()
            if now - self._last_emit < self._WINDOW:
                return False
            self._last_emit = now
        return True


_ib_noise_filter_installed = False


def _install_ib_wrapper_noise_filter() -> None:
    """Instala una sola vez el filtro que colapsa el ruido de Error 162 en ib_async."""
    global _ib_noise_filter_installed
    if _ib_noise_filter_installed:
        return
    logging.getLogger("ib_async.wrapper").addFilter(_IBWrapperNoiseFilter())
    _ib_noise_filter_installed = True


class MarketDataClient(Protocol):
    """Contrato de lectura de datos de mercado y cuenta."""

    async def connect(self) -> None: ...

    async def get_portfolio(self) -> Portfolio: ...

    async def get_quote(self, ticker: str) -> Quote: ...

    async def get_ohlcv(
        self, ticker: str, *, bars: int = 60, interval: str = "5 mins"
    ) -> list[OHLCVBar]: ...

    async def get_premarket(
        self, ticker: str, *, phase: str = "preopen"
    ) -> PremarketSnapshot: ...

    async def get_top_movers(self, *, exchange: Exchange, count: int = 20) -> list[str]: ...

    async def is_market_open(self, exchange: Exchange) -> bool: ...

    async def is_tradable_today(self, ticker: str) -> bool: ...


class MockMarketDataClient:
    """Implementacion en memoria. Los datos se inyectan en el constructor."""

    def __init__(
        self,
        portfolio: Portfolio | None = None,
        quotes: dict[str, Quote] | None = None,
        open_exchanges: set[Exchange] | None = None,
        tradable: set[str] | None = None,
        ohlcv: dict[str, list[OHLCVBar]] | None = None,
        premarket: dict[str, PremarketSnapshot] | None = None,
        open_snapshots: dict[str, PremarketSnapshot] | None = None,
        top_movers: dict[Exchange, list[str]] | None = None,
    ) -> None:
        self._portfolio = portfolio or Portfolio(total_equity=100_000, cash=100_000)
        self._quotes = quotes if quotes is not None else {}
        self._open_exchanges = (
            open_exchanges
            if open_exchanges is not None
            else {Exchange.BME, Exchange.NYSE, Exchange.NASDAQ}
        )
        self._tradable = tradable
        self._ohlcv = ohlcv or {}
        self._premarket = premarket or {}
        #: Snapshots de la fase postopen (gap de apertura real). Si falta un ticker, se usa
        #: el de premarket como respaldo para simplificar los tests.
        self._open_snapshots = open_snapshots or {}
        self._top_movers = top_movers or {}
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def get_portfolio(self) -> Portfolio:
        return self._portfolio

    async def get_quote(self, ticker: str) -> Quote:
        if ticker in self._quotes:
            return self._quotes[ticker]
        raise KeyError(f"No hay quote mock para {ticker}")

    async def get_ohlcv(
        self, ticker: str, *, bars: int = 60, interval: str = "5 mins"
    ) -> list[OHLCVBar]:
        data = self._ohlcv.get(ticker)
        if data is None:
            raise KeyError(f"No hay OHLCV mock para {ticker}")
        return data[-bars:]

    async def get_premarket(
        self, ticker: str, *, phase: str = "preopen"
    ) -> PremarketSnapshot:
        store = self._open_snapshots if phase == "postopen" else self._premarket
        if ticker in store:
            return store[ticker]
        if ticker in self._premarket:  # respaldo: postopen sin dato -> usa el de preopen
            return self._premarket[ticker]
        raise KeyError(f"No hay datos de snapshot mock para {ticker} (fase {phase})")

    async def get_top_movers(self, *, exchange: Exchange, count: int = 20) -> list[str]:
        return list(self._top_movers.get(exchange, []))[:count]

    async def is_market_open(self, exchange: Exchange) -> bool:
        return exchange in self._open_exchanges

    async def is_tradable_today(self, ticker: str) -> bool:
        if self._tradable is None:
            return True
        return ticker in self._tradable

    async def build_market_context(self, ticker: str, exchange: Exchange) -> MarketContext:
        """Helper: compone el MarketContext que necesita el Pre-Trade Validator."""
        quote = await self.get_quote(ticker)
        portfolio = await self.get_portfolio()
        return MarketContext(
            current_price=quote.last,
            is_tradable_today=await self.is_tradable_today(ticker),
            market_open=await self.is_market_open(exchange),
            available_capital=portfolio.cash,
            account_equity=portfolio.total_equity,
        )


class IBKRMarketDataClient:
    """Conector real via ib_async a IB Gateway (4002 paper / 4001 live)."""

    def __init__(self, host: str, port: int, client_id: int) -> None:
        try:  # pragma: no cover - depende del extra opcional
            from ib_async import IB, Index, Stock
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "ib_async no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc
        self._Stock = Stock
        self._Index = Index
        self.ib = IB()
        self.host = host
        self.port = port
        self.client_id = client_id
        self._account: str | None = None
        # Cortacircuitos ante Error 162 "different IP address": si otra sesion IBKR
        # esta usando el feed de datos historicos, se omiten las peticiones durante una
        # ventana corta en vez de reintentar ticker a ticker (evita ~40s perdidos por
        # ciclo y el spam de ERROR por contrato). Se rearma con cada 162.
        self._feed_down_cooldown = 90.0
        self._feed_down_until = 0.0
        self._feed_down_last_log = 0.0
        self.ib.errorEvent += self._on_ib_error
        _install_ib_wrapper_noise_filter()

    async def connect(self) -> None:  # pragma: no cover - requiere IB Gateway
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        # managedAccounts() es sincrono: devuelve la lista directamente (sin await)
        accounts = self.ib.managedAccounts()
        if accounts:
            self._account = accounts[0]

    def _on_ib_error(self, reqId, errorCode, errorString, *args) -> None:  # pragma: no cover
        """Handler de ib_async.errorEvent: arma el cortacircuitos ante el 162 de IP.

        Solo reacciona al Error 162 "connected from a different IP address" (feed de
        historicos tomado por otra sesion). Ignora el resto: warnings 2104/2106/2158 y
        el 162 "API scanner subscription cancelled" (cierre normal del scanner).
        """
        if errorCode == 162 and "different IP address" in str(errorString):
            now = time.monotonic()
            self._feed_down_until = now + self._feed_down_cooldown
            if now - self._feed_down_last_log >= self._feed_down_cooldown:
                self._feed_down_last_log = now
                logger.warning(
                    "IBKR: feed de datos historicos no disponible (Error 162: otra "
                    "sesion IBKR conectada desde otra IP esta usando el market data). "
                    "Se omiten las peticiones historicas durante %ss. Cierra la app "
                    "movil/Client Portal u otras sesiones, o usa credenciales dedicadas "
                    "para el Gateway.",
                    int(self._feed_down_cooldown),
                )

    def _ensure_feed_available(self, what: str) -> None:  # pragma: no cover
        """Falla rapido si el feed de historicos esta en cooldown por el Error 162."""
        if time.monotonic() < self._feed_down_until:
            raise ValueError(
                f"IBKR: feed de datos historicos no disponible (otra sesion desde otra "
                f"IP); se omite {what}."
            )

    async def get_portfolio(self) -> Portfolio:  # pragma: no cover
        if not self._account:
            raise RuntimeError("No account ID available. Call connect() first.")
        account_summary = await self.ib.accountSummaryAsync(self._account)
        equity = 0.0
        cash = 0.0
        for item in account_summary:
            if item.tag == "NetLiquidation":
                equity = float(item.value)
            elif item.tag == "AvailableFunds":
                cash = float(item.value)
        try:
            # Puebla el cache con las ordenes de TODOS los client IDs (los stops los coloca
            # el cliente de escritura, con client_id distinto al de lectura).
            await self.ib.reqAllOpenOrdersAsync()
        except Exception as exc:  # noqa: BLE001 - sin ordenes no aborta la lectura de cartera
            logger.debug("IBKR: reqAllOpenOrdersAsync fallo (%s).", exc)
        stops = self._stop_losses_from_trades(self.ib.openTrades())
        positions = self._positions_from_items(self.ib.portfolio(), stops)
        return Portfolio(total_equity=equity, cash=cash, positions=positions)

    def _contract_to_ticker(self, contract) -> str:
        """Reconstruye el ticker de la watchlist desde un contrato IBKR (inverso de _make_contract).

        BME (moneda EUR) -> base + '.MC', deshaciendo los overrides (MT -> MTS). US ->
        symbol con el espacio de multi-clase pasado a punto ('BRK B' -> 'BRK.B').
        """
        symbol = getattr(contract, "symbol", "") or ""
        currency = getattr(contract, "currency", "USD")
        if currency == "EUR":
            return f"{self._BME_SYMBOL_TO_BASE.get(symbol, symbol)}.MC"
        return symbol.replace(" ", ".")

    def _positions_from_items(
        self, items, stops: dict[str, float] | None = None
    ) -> list[Position]:
        """Convierte los PortfolioItem de ib_async en posiciones del dominio.

        Filtra lo que no sea accion, la cantidad nula y los precios no positivos
        (posiciones sin dato de mercado). El signo de la cantidad define la direccion
        (negativo = SHORT). El `stop_loss` se toma del mapa `stops` (ticker -> precio),
        proveniente de las ordenes stop abiertas en IBKR.
        """
        stops = stops or {}
        positions: list[Position] = []
        for item in items:
            contract = getattr(item, "contract", None)
            if contract is None or getattr(contract, "secType", "STK") not in ("STK", ""):
                continue
            quantity = int(getattr(item, "position", 0) or 0)
            avg_price = float(getattr(item, "averageCost", 0.0) or 0.0)
            market_price = float(getattr(item, "marketPrice", 0.0) or 0.0)
            if quantity == 0 or avg_price <= 0 or market_price <= 0:
                continue
            ticker = self._contract_to_ticker(contract)
            positions.append(Position(
                ticker=ticker,
                direction=Direction.LONG if quantity > 0 else Direction.SHORT,
                quantity=abs(quantity),
                avg_price=avg_price,
                market_price=market_price,
                stop_loss=stops.get(ticker),
            ))
        return positions

    # Estados de orden que IBKR considera vivos (con proteccion de stop aun activa).
    _ACTIVE_ORDER_STATUS = frozenset(
        {"PendingSubmit", "PreSubmitted", "Submitted", "ApiPending"}
    )

    def _stop_losses_from_trades(self, trades) -> dict[str, float]:
        """Mapa ticker -> precio de stop a partir de las ordenes stop abiertas en IBKR.

        Reconoce STP y STP LMT (precio en `auxPrice`) y TRAIL (precio en `trailStopPrice`,
        con respaldo en `auxPrice`). Ignora ordenes ya inactivas y precios no positivos.
        Si un ticker tiene varias, conserva la ultima vista.
        """
        stops: dict[str, float] = {}
        for trade in trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            if order is None or contract is None:
                continue
            status = getattr(getattr(trade, "orderStatus", None), "status", "")
            if status and status not in self._ACTIVE_ORDER_STATUS:
                continue
            order_type = str(getattr(order, "orderType", "")).upper()
            if order_type in ("STP", "STP LMT"):
                price = float(getattr(order, "auxPrice", 0.0) or 0.0)
            elif order_type == "TRAIL":
                price = float(
                    getattr(order, "trailStopPrice", 0.0)
                    or getattr(order, "auxPrice", 0.0)
                    or 0.0
                )
            else:
                continue
            if price <= 0:
                continue
            stops[self._contract_to_ticker(contract)] = price
        return stops

    # Inverso de los overrides de contrato BME (symbol IBKR -> base watchlist), usado por
    # _contract_to_ticker al leer posiciones. Fuente unica en connectors.ibkr_contracts.
    _BME_SYMBOL_TO_BASE = BME_SYMBOL_TO_BASE

    def _make_contract(self, ticker: str):
        """Construye el contrato IBKR correcto segun el mercado del ticker.

        Delega en `ibkr_contracts.make_stock_contract` (fuente unica compartida con el
        conector de escritura) para que datos y ordenes usen exactamente el mismo contrato:
        BME (*.MC) -> EUR/primaryExchange 'BM' (salvo overrides), multi-clase 'BRK B', US.
        """
        return make_stock_contract(ticker, self._Stock)

    # Mapa de nombre legible -> (symbol, exchange, currency) para indices IBKR.
    _INDEX_CONTRACTS: dict[str, tuple[str, str, str]] = {
        "SP500":  ("SPX",  "CBOE",    "USD"),
        "DAX":    ("DAX",  "EUREX",   "EUR"),
        "NIKKEI": ("N225", "OSE.JPN", "JPY"),
        "IBEX35": ("IBEX", "MEFFRV",  "EUR"),
    }

    def _make_index_contract(self, name: str):
        """Construye un contrato Index IBKR a partir del nombre legible."""
        symbol, exchange, currency = self._INDEX_CONTRACTS[name]
        return self._Index(symbol, exchange, currency)

    async def get_index_ohlcv(  # pragma: no cover
        self, name: str, *, bars: int = 20
    ) -> list[OHLCVBar]:
        """Devuelve `bars` velas diarias de cierre del indice `name` (SP500/DAX/NIKKEI/IBEX35)."""
        import asyncio
        self._ensure_feed_available(f"indice {name}")
        contract = self._make_index_contract(name)
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                raise ValueError(f"Indice no encontrado en IBKR: {name}")
            raw = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr=_duration_for(bars, "1 day"),
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                ),
                timeout=30,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            raise ValueError(f"Timeout obteniendo OHLCV de indice {name}") from exc
        out: list[OHLCVBar] = []
        for b in raw[-bars:]:
            out.append(
                OHLCVBar(
                    timestamp_utc=_to_utc(b.date),
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=max(0.0, float(b.volume or 0.0)),
                )
            )
        return out

    async def get_quote(self, ticker: str) -> Quote:  # pragma: no cover
        import asyncio
        self._ensure_feed_available(f"quote {ticker}")
        contract = self._make_contract(ticker)
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                raise ValueError(f"Contrato no encontrado en IBKR: {ticker}")
            bars = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 min",
                    whatToShow="MIDPOINT",
                    useRTH=True,
                ),
                timeout=30,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            raise ValueError(f"Timeout obteniendo quote para {ticker}") from exc
        if not bars:
            raise ValueError(f"No hay datos historicos para {ticker}")
        last_bar = bars[-1]
        return Quote(
            ticker=ticker,
            exchange=Exchange.BME if ticker.endswith(".MC") else Exchange.NYSE,
            last=last_bar.close,
            avg_volume_20d=last_bar.volume,
        )

    async def get_ohlcv(  # pragma: no cover - requiere IB Gateway
        self, ticker: str, *, bars: int = 60, interval: str = "5 mins"
    ) -> list[OHLCVBar]:
        import asyncio
        self._ensure_feed_available(f"OHLCV {ticker}")
        contract = self._make_contract(ticker)
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                raise ValueError(f"Contrato no encontrado en IBKR: {ticker}")
            raw = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr=_duration_for(bars, interval),
                    barSizeSetting=interval,
                    whatToShow="TRADES",
                    useRTH=True,
                ),
                timeout=30,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            raise ValueError(f"Timeout obteniendo OHLCV para {ticker}") from exc
        out: list[OHLCVBar] = []
        for b in raw[-bars:]:
            out.append(
                OHLCVBar(
                    timestamp_utc=_to_utc(b.date),
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=max(0.0, float(b.volume or 0.0)),
                )
            )
        return out

    async def get_premarket(  # pragma: no cover - requiere IB Gateway
        self, ticker: str, *, phase: str = "preopen"
    ) -> PremarketSnapshot:
        """Snapshot de apertura con el gap respecto al cierre previo.

        - phase='preopen'  : precio de referencia = subasta indicativa (reqMktData tick 225)
          con fallback a la ultima vela pre-market (useRTH=False).
        - phase='postopen' : precio de referencia = precio actual (ultima vela 1 min RTH),
          i.e. el gap de apertura REAL.
        """
        import asyncio
        self._ensure_feed_available(f"snapshot {ticker} ({phase})")
        contract = self._make_contract(ticker)
        is_bme = ticker.endswith(".MC")
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                raise ValueError(f"Contrato no encontrado en IBKR: {ticker}")
            # Cierre previo: ultima sesion diaria YA COMPLETADA (descarta la vela parcial
            # de hoy, que en fase postopen contaminaria el gap).
            daily = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract, endDateTime="", durationStr="3 D",
                    barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
                ),
                timeout=30,
            )
            if not daily:
                raise ValueError(f"No hay cierre previo para {ticker}")
            previous_close = self._previous_session_close(daily, is_bme)
            if phase == "postopen":
                reference, volume = await self._current_price(contract)
            else:
                reference, volume = await self._preopen_reference(contract)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            raise ValueError(f"Timeout obteniendo snapshot ({phase}) para {ticker}") from exc
        if reference is None or reference <= 0:
            raise ValueError(f"No hay precio de referencia ({phase}) para {ticker}")
        return PremarketSnapshot(
            ticker=ticker,
            exchange=Exchange.BME if is_bme else Exchange.NYSE,
            previous_close=previous_close,
            premarket_price=reference,
            premarket_volume=max(0.0, float(volume or 0.0)),
        )

    def _previous_session_close(self, daily, is_bme: bool) -> float:  # pragma: no cover
        """Cierre de la ultima sesion COMPLETADA (descarta la vela diaria parcial de hoy)."""
        from datetime import date, datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Madrid" if is_bme else "America/New_York")
        today = datetime.now(tz).date()
        completed = []
        for b in daily:
            d = b.date
            if isinstance(d, datetime):
                d = d.date()
            if isinstance(d, date) and d < today:
                completed.append(b)
        chosen = completed[-1] if completed else daily[-1]
        return chosen.close

    async def _current_price(self, contract):  # pragma: no cover
        """Fase postopen: ultima vela de 1 min de sesion regular (precio actual)."""
        import asyncio
        bars = await asyncio.wait_for(
            self.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="1 D",
                barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
            ),
            timeout=30,
        )
        if not bars:
            return None, 0.0
        last = bars[-1]
        return last.close, last.volume

    async def _preopen_reference(self, contract):  # pragma: no cover
        """Fase preopen: precio indicativo de la subasta de apertura, con fallback.

        1) reqMktData con generic tick 225 -> Ticker.auctionPrice (precio de casacion
           indicativo de la subasta). Requiere suscripcion de datos de mercado del mercado.
        2) Si no hay subasta, cae a la ultima vela de 1 min fuera de sesion (useRTH=False):
           en US es el pre-market real; en BME suele quedarse cerca del cierre previo.
        """
        import asyncio
        try:
            tkr = self.ib.reqMktData(contract, "225", False, False)
            await asyncio.sleep(2.0)  # margen para que lleguen los ticks de subasta
            auction = getattr(tkr, "auctionPrice", None)
            self.ib.cancelMktData(contract)
            if auction is not None and auction > 0:
                return float(auction), getattr(tkr, "auctionVolume", 0.0) or 0.0
        except Exception as exc:  # noqa: BLE001 - subasta no disponible -> fallback historico
            logger.debug("ibkr: subasta no disponible (%s); uso pre-market historico.", exc)
        bars = await asyncio.wait_for(
            self.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="1 D",
                barSizeSetting="1 min", whatToShow="TRADES", useRTH=False,
            ),
            timeout=30,
        )
        if not bars:
            return None, 0.0
        last = bars[-1]
        return last.close, last.volume

    # Scanner IBKR por mercado: (instrument, locationCode). Europa exige instrument
    # "STOCK.EU" (no "STK", que solo vale para US) y una ubicacion concreta; BME (Bolsa
    # de Madrid) es STK.EU.BM. El filtrado final al watchlist descarta lo ajeno al mercado.
    _SCANNER_MARKET: dict[str, tuple[str, str]] = {
        "US":  ("STK", "STK.US.MAJOR"),
        "BME": ("STOCK.EU", "STK.EU.BM"),
    }

    def _scanner_market(self, exchange: Exchange) -> tuple[str, str, bool]:
        is_bme = exchange == Exchange.BME
        instrument, location = self._SCANNER_MARKET["BME" if is_bme else "US"]
        return (instrument, location, is_bme)

    def _ibkr_symbol_to_ticker(self, symbol: str, is_bme: bool) -> str:
        """Convierte el symbol del scanner a la convencion de la watchlist.

        El scanner de BME (STK.EU.BM) devuelve el symbol de Bolsa de Madrid, que ya coincide
        con la base del ticker en la watchlist (p.ej. IDR -> IDR.MC, MTS -> MTS.MC); solo hay
        que anadir el sufijo .MC. En US, el symbol multi-clase usa espacio ('BRK B' -> BRK.B).
        """
        if is_bme:
            return f"{symbol}.MC"
        return symbol.replace(" ", ".")  # 'BRK B' -> 'BRK.B'

    async def get_top_movers(  # pragma: no cover - requiere IB Gateway
        self, *, exchange: Exchange, count: int = 20
    ) -> list[str]:
        """Top movers via scanner de IBKR (mayores subidas y bajadas del dia por %).

        Usa `reqScannerDataAsync`, que NO consume el pacing de datos historicos
        (60/10min): es barato para rankear el universo y pedir el detalle pre-market solo
        de los relevantes. Devuelve tickers en la convencion de la watchlist.
        """
        from itertools import zip_longest

        try:
            from ib_async import ScannerSubscription
        except ImportError as exc:
            raise ImportError(
                "ib_async no esta instalado. Instala el extra: pip install -e .[connectors]"
            ) from exc
        instrument, location, is_bme = self._scanner_market(exchange)
        by_code: dict[str, list[str]] = {}
        for scan_code in ("TOP_PERC_GAIN", "TOP_PERC_LOSE"):
            sub = ScannerSubscription(
                instrument=instrument, locationCode=location, scanCode=scan_code, numberOfRows=count
            )
            try:
                rows = await self.ib.reqScannerDataAsync(sub)
            except Exception as exc:  # noqa: BLE001 - un scanCode fallido no aborta el otro
                logger.warning("ibkr: scanner %s fallo (%s).", scan_code, exc)
                rows = []
            by_code[scan_code] = [
                self._ibkr_symbol_to_ticker(r.contractDetails.contract.symbol, is_bme)
                for r in rows
            ]
        # Intercala subidas y bajadas para no sesgar hacia una sola direccion.
        result: list[str] = []
        seen: set[str] = set()
        for gain, lose in zip_longest(by_code.get("TOP_PERC_GAIN", []), by_code.get("TOP_PERC_LOSE", [])):
            for ticker in (gain, lose):
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    result.append(ticker)
        return result[:count]

    async def is_market_open(self, exchange: Exchange) -> bool:  # pragma: no cover
        # IBKR proporciona estado de mercado por exchange
        # Para simplificar, usamos una aproximación basada en hora UTC
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        hour = now.hour
        # BME: 08:00-17:30 CET (07:00-16:30 UTC aprox)
        # NYSE/NASDAQ: 09:30-16:00 EST (14:30-21:00 UTC aprox)
        if exchange == Exchange.BME:
            return 7 <= hour < 17
        else:  # NYSE, NASDAQ
            return 14 <= hour < 21

    async def is_tradable_today(self, ticker: str) -> bool:  # pragma: no cover
        contract = self._make_contract(ticker)
        details = await self.ib.qualifyContractsAsync(contract)
        return bool(details)

    async def build_market_context(self, ticker: str, exchange: Exchange) -> MarketContext:  # pragma: no cover
        """Helper: compone el MarketContext que necesita el Pre-Trade Validator."""
        quote = await self.get_quote(ticker)
        portfolio = await self.get_portfolio()
        return MarketContext(
            current_price=quote.last,
            is_tradable_today=await self.is_tradable_today(ticker),
            market_open=await self.is_market_open(exchange),
            available_capital=portfolio.cash,
            account_equity=portfolio.total_equity,
        )



#: Minutos por barra segun el `barSizeSetting` de IBKR (subset habitual intradia/diario).
_INTERVAL_MINUTES = {
    "1 min": 1, "2 mins": 2, "3 mins": 3, "5 mins": 5, "10 mins": 10,
    "15 mins": 15, "30 mins": 30, "1 hour": 60, "1 day": 390,
}


def _duration_for(bars: int, interval: str) -> str:
    """Traduce (numero de barras, intervalo) al `durationStr` de IBKR en dias.

    Asume ~6.5h de sesion regular (390 min/dia) y anade un dia de colchon para cubrir
    festivos/fines de semana.
    """
    minutes = _INTERVAL_MINUTES.get(interval, 5)
    total_minutes = max(1, bars) * minutes
    days = max(1, -(-total_minutes // 390))  # ceil division
    return f"{days + 1} D"


def _to_utc(value) -> "datetime":  # pragma: no cover - depende de tipos de ib_async
    """Normaliza una fecha/datetime de ib_async a datetime UTC."""
    from datetime import datetime, time, timezone

    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # date -> medianoche UTC
    return datetime.combine(value, time(), tzinfo=timezone.utc)
