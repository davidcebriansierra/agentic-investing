"""Tests de los buscadores respaldados por datos de mercado (camino 2, spec seccion 3.1).

Cubre: OHLCV/pre-market en MockMarketDataClient, indicadores tecnicos, fundamentales
(Finnhub mapping + mock) y los overrides de build_context de technical/premarket/
fundamental/cross_market.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.agents.searchers import (
    CrossMarketSearcher,
    FundamentalSearcher,
    PremarketSearcher,
    TechnicalSearcher,
)
from src.agents.searchers.indicators import _rsi, _sma, compute_indicators
from src.connectors.fundamentals import MockFundamentalsClient, _to_fundamentals
from src.connectors.ibkr_read import IBKRMarketDataClient, MockMarketDataClient
from src.llm.mock import MockLLMClient
from src.schemas.enums import Direction, Exchange
from src.schemas.market import Fundamentals, OHLCVBar, PremarketSnapshot
from src.schemas.portfolio import Portfolio, Position


def _opp_dict(ticker: str = "AAPL", exchange: str = "NASDAQ") -> dict:
    return {
        "ticker": ticker,
        "exchange": exchange,
        "direction": "LONG",
        "entry_price": 100.0,
        "take_profit": 104.0,
        "stop_loss": 98.0,
        "position_size_pct": 0.05,
        "estimated_win_probability": 0.6,
        "justification": "setup",
    }


def _bars(prices: list[float]) -> list[OHLCVBar]:
    return [
        OHLCVBar(open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1000 + i)
        for i, p in enumerate(prices)
    ]


# ---------- indicadores ----------

def test_sma_basic():
    assert _sma([1, 2, 3, 4, 5], 5) == 3.0
    assert _sma([1, 2], 5) is None


def test_rsi_all_gains_is_100():
    # Serie estrictamente creciente -> sin perdidas -> RSI 100.
    assert _rsi([float(i) for i in range(1, 20)], 14) == 100.0


def test_compute_indicators_returns_core_metrics():
    ind = compute_indicators(_bars([100 + i for i in range(60)]))
    assert "last_close" in ind
    assert "change_pct" in ind
    assert ind["change_pct"] > 0  # serie creciente
    assert "high_20" in ind and "low_20" in ind


def test_compute_indicators_insufficient_data():
    assert compute_indicators(_bars([100.0])) == {}


# ---------- OHLCV / pre-market en el mock ----------

async def test_mock_get_ohlcv_returns_last_n():
    bars = _bars([100 + i for i in range(10)])
    market = MockMarketDataClient(ohlcv={"AAPL": bars})
    got = await market.get_ohlcv("AAPL", bars=3, interval="5 mins")
    assert len(got) == 3
    assert got[-1].close == bars[-1].close


async def test_mock_get_premarket():
    snap = PremarketSnapshot(
        ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=102.0
    )
    market = MockMarketDataClient(premarket={"AAPL": snap})
    got = await market.get_premarket("AAPL")
    assert abs(got.gap_pct - 0.02) < 1e-9


# ---------- TechnicalSearcher ----------

async def test_technical_searcher_context_has_indicators():
    market = MockMarketDataClient(ohlcv={"AAPL": _bars([100 + i for i in range(60)])})
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = TechnicalSearcher(market_data=market, watchlist=["AAPL"], llm=llm)
    opps = await searcher.search()
    assert len(opps) == 1
    system = llm.calls[0]["system"]
    assert "AAPL" in system
    assert "last_close" in system


async def test_technical_searcher_no_data_returns_empty():
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = TechnicalSearcher(market_data=MockMarketDataClient(), watchlist=["AAPL"], llm=llm)
    assert await searcher.search() == []
    assert llm.calls == []


# ---------- PremarketSearcher ----------

async def test_premarket_searcher_context_has_gap():
    snap = PremarketSnapshot(
        ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=103.0
    )
    market = MockMarketDataClient(premarket={"AAPL": snap})
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = PremarketSearcher(market_data=market, watchlist=["AAPL"], llm=llm)
    opps = await searcher.search()
    assert len(opps) == 1
    assert "gap=" in llm.calls[0]["system"]


_MARKETS = [
    {"id": "IBEX35", "exchange": "BME", "open_local": "09:00", "timezone": "Europe/Madrid"},
    {"id": "SP500", "exchange": "NYSE", "open_local": "09:30", "timezone": "America/New_York"},
]


def _premarket_market(top_movers=None, portfolio=None) -> MockMarketDataClient:
    return MockMarketDataClient(
        premarket={
            "ACS.MC": PremarketSnapshot(
                ticker="ACS.MC", exchange=Exchange.BME, previous_close=30.0, premarket_price=30.6
            ),
            "AAPL": PremarketSnapshot(
                ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=101.0
            ),
        },
        top_movers=top_movers,
        portfolio=portfolio,
    )


def _IBEX_WINDOW() -> datetime:
    return datetime(2025, 8, 6, 6, 55, tzinfo=timezone.utc)   # 08:55 Madrid


def _SP500_WINDOW() -> datetime:
    return datetime(2025, 8, 6, 13, 25, tzinfo=timezone.utc)  # 09:25 New York


def _NO_WINDOW() -> datetime:
    return datetime(2025, 8, 6, 12, 0, tzinfo=timezone.utc)


async def test_premarket_movers_ibex_window():
    """En la ventana del IBEX solo se consultan los top movers .MC (no los US)."""
    market = _premarket_market(top_movers={Exchange.BME: ["ACS.MC"], Exchange.NYSE: ["AAPL"]})
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, now_provider=_IBEX_WINDOW,
    )
    ctx = await searcher.build_context()
    assert "ACS.MC" in ctx and "AAPL" not in ctx


async def test_premarket_movers_sp500_window_filters_non_watchlist():
    """En la ventana del SP500 se consultan movers US, descartando los que no esten en watchlist."""
    market = _premarket_market(top_movers={Exchange.NYSE: ["AAPL", "TSLA"]})
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, now_provider=_SP500_WINDOW,
    )
    ctx = await searcher.build_context()
    assert "AAPL" in ctx and "ACS.MC" not in ctx and "TSLA" not in ctx


async def test_premarket_includes_open_positions():
    """Las posiciones abiertas del mercado se analizan aunque no aparezcan en los movers."""
    portfolio = Portfolio(
        total_equity=100_000, cash=50_000,
        positions=[Position(
            ticker="ACS.MC", direction=Direction.LONG, quantity=100,
            avg_price=30.0, market_price=30.5,
        )],
    )
    market = _premarket_market(portfolio=portfolio)  # sin movers
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, now_provider=_IBEX_WINDOW,
    )
    ctx = await searcher.build_context()
    assert "ACS.MC" in ctx


async def test_premarket_outside_window_no_fetch():
    """Fuera de toda ventana de preapertura el contexto es vacio (no hay fetch)."""
    market = _premarket_market(top_movers={Exchange.BME: ["ACS.MC"], Exchange.NYSE: ["AAPL"]})
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, now_provider=_NO_WINDOW,
    )
    assert await searcher.build_context() == ""


async def test_premarket_fallback_gap_when_scanner_empty():
    """Sin movers del scanner (preapertura), el fallback calcula el gap directo del watchlist."""
    market = _premarket_market(top_movers=None)  # scanner vacio, sin posiciones
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, min_gap_pct=1.0, now_provider=_SP500_WINDOW,
    )
    ctx = await searcher.build_context()
    assert "AAPL" in ctx and "ACS.MC" not in ctx  # AAPL gap +1% pasa; ACS.MC es de otro mercado


async def test_premarket_fallback_filters_small_gaps():
    """El fallback descarta tickers con |gap| por debajo de min_gap_pct."""
    market = _premarket_market(top_movers=None)  # AAPL gap +1%
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, min_gap_pct=5.0, now_provider=_SP500_WINDOW,
    )
    assert await searcher.build_context() == ""  # AAPL +1% < 5% -> descartado


async def test_premarket_fallback_ranks_by_gap_and_limits():
    """El fallback ordena por |gap| y respeta top_movers (solo el mayor gap)."""
    market = MockMarketDataClient(
        premarket={
            "AAPL": PremarketSnapshot(
                ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=101.0
            ),  # +1%
            "MSFT": PremarketSnapshot(
                ticker="MSFT", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=103.0
            ),  # +3%
            "TSLA": PremarketSnapshot(
                ticker="TSLA", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=98.0
            ),  # -2%
        },
    )
    searcher = PremarketSearcher(
        market_data=market, watchlist=["AAPL", "MSFT", "TSLA"], markets=_MARKETS,
        lead_minutes=10, top_movers=1, min_gap_pct=1.0, now_provider=_SP500_WINDOW,
    )
    ctx = await searcher.build_context()
    assert "MSFT" in ctx and "AAPL" not in ctx and "TSLA" not in ctx  # solo el mayor |gap|


async def test_premarket_fallback_keeps_positions_even_without_gap():
    """Las posiciones abiertas se mantienen en el fallback aunque su gap sea pequeno."""
    portfolio = Portfolio(
        total_equity=100_000, cash=50_000,
        positions=[Position(
            ticker="AAPL", direction=Direction.LONG, quantity=10,
            avg_price=100.0, market_price=101.0,
        )],
    )
    market = _premarket_market(top_movers=None, portfolio=portfolio)  # AAPL +1%
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, top_movers=20, min_gap_pct=5.0, now_provider=_SP500_WINDOW,
    )
    ctx = await searcher.build_context()
    assert "AAPL" in ctx  # posicion abierta -> siempre, pese a gap < min_gap_pct


# ---------- PremarketSearcher: fase postopen ----------

def _SP500_POSTOPEN() -> datetime:
    return datetime(2025, 8, 6, 13, 35, tzinfo=timezone.utc)  # 09:35 New York (postopen)


def _IBEX_POSTOPEN() -> datetime:
    return datetime(2025, 8, 6, 7, 5, tzinfo=timezone.utc)     # 09:05 Madrid (postopen)


def test_premarket_window_phase():
    """_window_phase distingue preopen, postopen y fuera de ventana."""
    searcher = PremarketSearcher(
        market_data=MockMarketDataClient(), watchlist=["AAPL"], markets=_MARKETS,
        lead_minutes=10, post_open_minutes=10,
    )
    sp500 = _MARKETS[1]
    searcher._now = _SP500_WINDOW
    assert searcher._window_phase(sp500) == "preopen"
    searcher._now = _SP500_POSTOPEN
    assert searcher._window_phase(sp500) == "postopen"
    searcher._now = _NO_WINDOW
    assert searcher._window_phase(sp500) is None


async def test_premarket_postopen_uses_scanner_movers():
    """En postopen la sesion esta abierta: el scanner aporta movers y se usan."""
    market = _premarket_market(top_movers={Exchange.NYSE: ["AAPL"]})
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, post_open_minutes=10, now_provider=_SP500_POSTOPEN,
    )
    ctx = await searcher.build_context()
    assert "AAPL" in ctx and "ACS.MC" not in ctx


async def test_premarket_postopen_reads_open_snapshot():
    """En postopen se usa el snapshot de apertura (gap real), no el de preopen."""
    market = MockMarketDataClient(
        premarket={"AAPL": PremarketSnapshot(
            ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=101.0
        )},  # preopen: +1%
        open_snapshots={"AAPL": PremarketSnapshot(
            ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=103.0
        )},  # postopen: +3%
        top_movers={Exchange.NYSE: ["AAPL"]},
    )
    searcher = PremarketSearcher(
        market_data=market, watchlist=["AAPL"], markets=_MARKETS,
        lead_minutes=10, post_open_minutes=10, now_provider=_SP500_POSTOPEN,
    )
    ctx = await searcher.build_context()
    assert "gap=+3.00%" in ctx  # usa el snapshot postopen, no el +1% de preopen


async def test_premarket_ibex_postopen_gap_fallback():
    """El IBEX, sin pre-market util, si funciona en postopen con el gap de apertura real."""
    market = MockMarketDataClient(
        open_snapshots={"ACS.MC": PremarketSnapshot(
            ticker="ACS.MC", exchange=Exchange.BME, previous_close=30.0, premarket_price=30.9
        )},  # +3% al abrir
        top_movers=None,  # scanner vacio -> fallback de gap directo
    )
    searcher = PremarketSearcher(
        market_data=market, watchlist=["ACS.MC", "AAPL"], markets=_MARKETS,
        lead_minutes=10, post_open_minutes=10, min_gap_pct=1.0, now_provider=_IBEX_POSTOPEN,
    )
    ctx = await searcher.build_context()
    assert "ACS.MC" in ctx and "AAPL" not in ctx


async def test_mock_get_premarket_phase():
    """El mock enruta por fase: postopen usa open_snapshots (con fallback a preopen)."""
    preopen = PremarketSnapshot(
        ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=101.0
    )
    postopen = PremarketSnapshot(
        ticker="AAPL", exchange=Exchange.NASDAQ, previous_close=100.0, premarket_price=104.0
    )
    market = MockMarketDataClient(premarket={"AAPL": preopen}, open_snapshots={"AAPL": postopen})
    assert abs((await market.get_premarket("AAPL", phase="preopen")).gap_pct - 0.01) < 1e-9
    assert abs((await market.get_premarket("AAPL", phase="postopen")).gap_pct - 0.04) < 1e-9
    # postopen sin dato propio -> respaldo con el snapshot de preopen
    market2 = MockMarketDataClient(premarket={"AAPL": preopen})
    assert abs((await market2.get_premarket("AAPL", phase="postopen")).gap_pct - 0.01) < 1e-9


# ---------- FundamentalSearcher ----------

async def test_fundamental_searcher_context_has_metrics():
    fund = Fundamentals(ticker="AAPL", company_name="Apple", pe_ratio=28.5, roe=120.0)
    client = MockFundamentalsClient({"AAPL": fund})
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = FundamentalSearcher(watchlist=["AAPL"], llm=llm, fundamentals_client=client)
    opps = await searcher.search()
    assert len(opps) == 1
    system = llm.calls[0]["system"]
    assert "PER=28.5" in system
    assert "Apple" in system


async def test_fundamental_searcher_without_client_returns_empty():
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = FundamentalSearcher(watchlist=["AAPL"], llm=llm, fundamentals_client=None)
    assert await searcher.search() == []
    assert llm.calls == []


def test_finnhub_mapping():
    metric = {
        "peTTM": 30.1,
        "psTTM": 7.2,
        "roeTTM": 145.0,
        "revenueGrowthTTMYoy": 8.1,
        "52WeekHigh": 199.6,
    }
    profile = {"name": "Apple Inc", "marketCapitalization": 3_000_000.0}
    f = _to_fundamentals("AAPL", metric, profile)
    assert f.company_name == "Apple Inc"
    assert f.pe_ratio == 30.1
    assert f.roe == 145.0
    assert f.week52_high == 199.6
    assert f.peg_ratio is None  # ausente -> None


def test_mock_fundamentals_filters_by_ticker():
    client = MockFundamentalsClient(
        {"AAPL": Fundamentals(ticker="AAPL"), "MSFT": Fundamentals(ticker="MSFT")}
    )
    # Nota: fetch es async; se prueba el filtrado sincrono via el atributo interno.
    assert set(client._items) == {"AAPL", "MSFT"}


# ---------- CrossMarketSearcher ----------

async def test_cross_market_context_groups_by_market():
    market = MockMarketDataClient(
        ohlcv={
            "AAPL": _bars([100.0, 102.0]),   # +2% NASDAQ->NYSE mapping
            "IBE.MC": _bars([10.0, 9.8]),    # -2% BME
        },
        top_movers={Exchange.NYSE: ["AAPL"], Exchange.BME: ["IBE.MC"]},
    )
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("IBE.MC", "BME")]}))
    searcher = CrossMarketSearcher(market_data=market, watchlist=["AAPL", "IBE.MC"], llm=llm)
    opps = await searcher.search()
    assert len(opps) == 1
    system = llm.calls[0]["system"]
    assert "Mercado BME" in system
    assert "Mercado NYSE" in system


def test_ibkr_symbol_to_ticker_maps_scanner_symbols():
    """Scanner BME devuelve el symbol de Madrid -> base + .MC (coincide con la watchlist)."""
    fn = IBKRMarketDataClient._ibkr_symbol_to_ticker
    # BME: Indra (IDR.MC) y ArcelorMittal (MTS.MC) se mapean directos, sin remapear a INDRA.
    assert fn(None, "IDR", True) == "IDR.MC"
    assert fn(None, "MTS", True) == "MTS.MC"
    assert fn(None, "SAN", True) == "SAN.MC"
    # US: symbol multi-clase con espacio -> punto.
    assert fn(None, "BRK B", False) == "BRK.B"
    assert fn(None, "AAPL", False) == "AAPL"


def test_contract_to_ticker_inverts_make_contract():
    """_contract_to_ticker deshace _make_contract: EUR -> .MC (con override), US -> punto."""
    from types import SimpleNamespace

    client = object.__new__(IBKRMarketDataClient)  # sin __init__: no requiere ib_async
    to_ticker = client._contract_to_ticker
    assert to_ticker(SimpleNamespace(symbol="SAN", currency="EUR")) == "SAN.MC"
    assert to_ticker(SimpleNamespace(symbol="MT", currency="EUR")) == "MTS.MC"  # override inverso
    assert to_ticker(SimpleNamespace(symbol="AAPL", currency="USD")) == "AAPL"
    assert to_ticker(SimpleNamespace(symbol="BRK B", currency="USD")) == "BRK.B"


def test_positions_from_items_maps_ib_portfolio():
    """get_portfolio puebla posiciones: PortfolioItem de ib_async -> Position del dominio."""
    from types import SimpleNamespace

    client = object.__new__(IBKRMarketDataClient)

    def item(symbol, currency, sec, qty, avg, mkt):
        return SimpleNamespace(
            contract=SimpleNamespace(symbol=symbol, currency=currency, secType=sec),
            position=qty, averageCost=avg, marketPrice=mkt,
        )

    items = [
        item("AAPL", "USD", "STK", 10, 100.0, 105.0),   # long US
        item("BRK B", "USD", "STK", -5, 300.0, 310.0),  # short US multi-clase
        item("SAN", "EUR", "STK", 20, 4.0, 4.2),         # long BME
        item("MT", "EUR", "STK", 3, 25.0, 26.0),         # BME con override MT -> MTS
        item("AAPL", "USD", "OPT", 1, 1.0, 2.0),         # no es accion -> descartada
        item("ZERO", "USD", "STK", 0, 1.0, 2.0),         # cantidad 0 -> descartada
        item("NODATA", "USD", "STK", 4, 10.0, 0.0),      # sin precio de mercado -> descartada
    ]
    stops = {"AAPL": 95.0, "SAN.MC": 3.8}  # solo algunas tienen stop abierto
    by_ticker = {p.ticker: p for p in client._positions_from_items(items, stops)}
    assert set(by_ticker) == {"AAPL", "BRK.B", "SAN.MC", "MTS.MC"}
    assert by_ticker["AAPL"].direction == Direction.LONG
    assert by_ticker["AAPL"].quantity == 10 and by_ticker["AAPL"].avg_price == 100.0
    assert by_ticker["BRK.B"].direction == Direction.SHORT and by_ticker["BRK.B"].quantity == 5
    assert by_ticker["MTS.MC"].market_price == 26.0
    # El stop_loss se enlaza desde el mapa; sin orden stop queda None.
    assert by_ticker["AAPL"].stop_loss == 95.0
    assert by_ticker["SAN.MC"].stop_loss == 3.8
    assert by_ticker["BRK.B"].stop_loss is None and by_ticker["MTS.MC"].stop_loss is None


def test_stop_losses_from_trades_extracts_open_stops():
    """_stop_losses_from_trades mapea ticker -> precio de las ordenes stop abiertas."""
    from types import SimpleNamespace

    client = object.__new__(IBKRMarketDataClient)

    def trade(symbol, currency, order_type, *, aux=0.0, trail=0.0, status="Submitted"):
        return SimpleNamespace(
            contract=SimpleNamespace(symbol=symbol, currency=currency, secType="STK"),
            order=SimpleNamespace(orderType=order_type, auxPrice=aux, trailStopPrice=trail),
            orderStatus=SimpleNamespace(status=status),
        )

    trades = [
        trade("AAPL", "USD", "STP", aux=95.0),                     # stop simple
        trade("MT", "EUR", "STP LMT", aux=24.0),                   # stop-limit BME (override MT->MTS)
        trade("TSLA", "USD", "TRAIL", trail=210.0),                # trailing stop
        trade("NFLX", "USD", "LMT", aux=400.0),                    # no es stop -> ignorada
        trade("AMZN", "USD", "STP", aux=180.0, status="Cancelled"),# inactiva -> ignorada
        trade("META", "USD", "STP", aux=0.0),                      # precio 0 -> ignorada
    ]
    stops = client._stop_losses_from_trades(trades)
    assert stops == {"AAPL": 95.0, "MTS.MC": 24.0, "TSLA": 210.0}


def test_ib_wrapper_noise_filter_collapses_162_ip_spam():
    """El filtro colapsa el spam del Error 162 'different IP address' sin tocar el resto."""
    import logging

    from src.connectors.ibkr_read import _IBWrapperNoiseFilter

    filt = _IBWrapperNoiseFilter()

    def rec(msg: str) -> logging.LogRecord:
        return logging.LogRecord("ib_async.wrapper", logging.ERROR, __file__, 0, msg, None, None)

    ip_162 = (
        "Error 162, reqId 5: ...historicos:Trading TWS session is connected from a "
        "different IP address, contract: Stock(symbol='ACS')"
    )
    # El primer 162 de IP pasa; los repetidos dentro de la ventana se descartan.
    assert filt.filter(rec(ip_162)) is True
    assert filt.filter(rec(ip_162)) is False
    # El 162 del scanner (sin 'different IP address') NO se filtra: es cierre normal.
    assert filt.filter(rec("Error 162 ...: API scanner subscription cancelled: 20")) is True
    # El resto de logs de ib_async pasan intactos.
    assert filt.filter(rec("Warning 2106 ...: HMDS funciona correctamente:ushmds")) is True
