"""Tests de los buscadores LLM (spec v2.0, seccion 3.1) y del parseo de listas JSON."""
from __future__ import annotations

import json

from src.agents.searchers import (
    NewsSearcher,
    SocialSearcher,
    TechnicalSearcher,
)
from src.agents.searchers.cross_market import CrossMarketSearcher
from src.agents.searchers.fundamental import FundamentalSearcher
from src.agents.searchers.context import (
    format_news_context,
    format_social_context,
)
from src.connectors.ibkr_read import MockMarketDataClient
from src.connectors.news_apis import MockNewsClient
from src.connectors.social_apis import MockSocialClient
from src.llm.mock import MockLLMClient
from src.llm.parsing import extract_json_array
from src.schemas.enums import AgentSource, Exchange
from src.schemas.feeds import NewsItem, SocialPost
from src.schemas.market import OHLCVBar


# ---------- extract_json_array ----------

def _opp_dict(ticker: str = "AAPL") -> dict:
    return {
        "ticker": ticker,
        "exchange": "NASDAQ",
        "direction": "LONG",
        "entry_price": 100.0,
        "take_profit": 104.0,
        "stop_loss": 98.0,
        "position_size_pct": 0.05,
        "expected_holding": "INTRADAY",
        "estimated_win_probability": 0.6,
        "justification": "setup",
    }


def test_extract_array_top_level_list():
    text = json.dumps([_opp_dict("A"), _opp_dict("B")])
    assert len(extract_json_array(text)) == 2


def test_extract_array_wrapped_object():
    text = json.dumps({"opportunities": [_opp_dict("A")]})
    result = extract_json_array(text)
    assert len(result) == 1
    assert result[0]["ticker"] == "A"


def test_extract_array_single_object_as_one():
    text = json.dumps(_opp_dict("A"))
    assert len(extract_json_array(text)) == 1


def test_extract_array_fenced_block():
    text = "aqui tienes:\n```json\n" + json.dumps({"opportunities": [_opp_dict()]}) + "\n```"
    assert len(extract_json_array(text)) == 1


def test_extract_array_empty_object_yields_empty():
    assert extract_json_array("{}") == []


def test_extract_array_garbage_yields_empty():
    assert extract_json_array("esto no es json") == []


# ---------- LLMSearcher core via NewsSearcher ----------

def _news_item(ticker: str = "AAPL") -> NewsItem:
    return NewsItem(source="newsapi", headline="Apple bate resultados", tickers=[ticker], sentiment=0.6)


async def test_news_searcher_builds_opportunities_from_llm():
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = NewsSearcher(client=MockNewsClient([_news_item()]), llm=llm)
    opps = await searcher.search()
    assert len(opps) == 1
    assert opps[0].ticker == "AAPL"
    assert opps[0].agent_source == AgentSource.NEWS
    assert opps[0].exchange == Exchange.NASDAQ
    # El R/R se recomputa desde la geometria (4.0 / 2.0 = 2.0).
    assert opps[0].risk_reward_ratio == 2.0
    assert llm.calls[0]["json_mode"] is True


async def test_news_searcher_without_llm_returns_empty():
    searcher = NewsSearcher(client=MockNewsClient([_news_item()]), llm=None)
    assert await searcher.search() == []


async def test_news_searcher_empty_feed_skips_llm():
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict()]}))
    searcher = NewsSearcher(client=MockNewsClient([]), llm=llm)
    assert await searcher.search() == []
    assert llm.calls == []  # sin contexto no se llama al LLM


async def test_searcher_discards_invalid_geometry():
    bad = _opp_dict("AAPL")
    bad["take_profit"] = 90.0  # LONG con TP < entry -> geometria invalida
    llm = MockLLMClient(canned=json.dumps({"opportunities": [bad, _opp_dict("MSFT")]}))
    searcher = NewsSearcher(client=MockNewsClient([_news_item()]), llm=llm)
    opps = await searcher.search()
    assert [o.ticker for o in opps] == ["MSFT"]


async def test_searcher_normalizes_percentage_position_size():
    data = _opp_dict("AAPL")
    data["position_size_pct"] = 5.0  # 5% expresado como porcentaje -> 0.05
    llm = MockLLMClient(canned=json.dumps({"opportunities": [data]}))
    searcher = NewsSearcher(client=MockNewsClient([_news_item()]), llm=llm)
    opps = await searcher.search()
    assert abs(opps[0].position_size_pct - 0.05) < 1e-9


async def test_social_searcher_builds_opportunities():
    post = SocialPost(platform="reddit", channel="wallstreetbets", text="AAPL to the moon", tickers=["AAPL"], score=120)
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = SocialSearcher(client=MockSocialClient([post]), llm=llm)
    opps = await searcher.search()
    assert len(opps) == 1
    assert opps[0].agent_source == AgentSource.SOCIAL


# ---------- MarketDataLLMSearcher via TechnicalSearcher ----------

async def test_technical_searcher_uses_ohlcv_context():
    bars = [
        OHLCVBar(open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1000)
        for p in [100 + i for i in range(30)]
    ]
    market = MockMarketDataClient(ohlcv={"AAPL": bars})
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = TechnicalSearcher(market_data=market, watchlist=["AAPL"], llm=llm)
    opps = await searcher.search()
    assert len(opps) == 1
    # El contexto enviado al LLM contiene los indicadores calculados sobre el OHLCV.
    assert "AAPL" in llm.calls[0]["system"]
    assert "last_close" in llm.calls[0]["system"]


async def test_technical_searcher_no_watchlist_returns_empty():
    llm = MockLLMClient(canned=json.dumps({"opportunities": [_opp_dict("AAPL")]}))
    searcher = TechnicalSearcher(market_data=MockMarketDataClient(), watchlist=[], llm=llm)
    assert await searcher.search() == []
    assert llm.calls == []


# ---------- context formatters ----------

def test_format_news_context_includes_headline_and_tickers():
    ctx = format_news_context([_news_item("AAPL")])
    assert "Apple bate resultados" in ctx
    assert "AAPL" in ctx


def test_format_social_context_includes_text_and_score():
    post = SocialPost(platform="stocktwits", channel="AAPL", text="bullish", tickers=["AAPL"], score=42)
    ctx = format_social_context([post])
    assert "bullish" in ctx
    assert "42" in ctx


# ---------- cross-market movers + umbral ----------

async def test_cross_market_sorts_by_movers_and_filters_flat():
    """Ordena por |variacion diaria| desc y descarta los planos (< min_move_pct)."""
    def _daily(closes: list[float]) -> list[OHLCVBar]:
        return [OHLCVBar(open=c, high=c, low=c, close=c, volume=1000) for c in closes]

    market = MockMarketDataClient(
        ohlcv={
            "BIGUP": _daily([100.0, 100.0, 105.0]),   # +5.0%
            "BIGDN": _daily([100.0, 100.0, 96.0]),    # -4.0%
            "MID":   _daily([100.0, 100.0, 102.0]),   # +2.0%
            "FLAT":  _daily([100.0, 100.0, 100.2]),   # +0.2% -> filtrado
        },
        top_movers={Exchange.NYSE: ["BIGUP", "BIGDN", "MID", "FLAT"]},
    )
    # El orden de la watchlist es distinto del orden por movimiento a proposito.
    searcher = CrossMarketSearcher(
        market_data=market,
        watchlist=["FLAT", "MID", "BIGDN", "BIGUP"],
        min_move_pct=1.0,
    )
    ctx = await searcher.build_context()

    assert "FLAT" not in ctx  # plano (<1%) descartado del detalle
    assert "BIGUP" in ctx and "BIGDN" in ctx and "MID" in ctx
    # Ordenados por mayor movimiento (no por orden de watchlist).
    assert ctx.index("BIGUP") < ctx.index("BIGDN") < ctx.index("MID")


async def test_cross_market_only_analyzes_scanner_movers():
    """Solo analiza los movers del scanner, no toda la watchlist (aunque otro se mueva mas)."""
    def _daily(closes: list[float]) -> list[OHLCVBar]:
        return [OHLCVBar(open=c, high=c, low=c, close=c, volume=1000) for c in closes]

    market = MockMarketDataClient(
        ohlcv={
            "MOVER": _daily([100.0, 100.0, 105.0]),   # +5% -> mover del scanner
            "OTHER": _daily([100.0, 100.0, 110.0]),   # +10% pero NO lo devuelve el scanner
        },
        top_movers={Exchange.NYSE: ["MOVER"]},
    )
    searcher = CrossMarketSearcher(
        market_data=market, watchlist=["MOVER", "OTHER"], min_move_pct=1.0,
    )
    ctx = await searcher.build_context()
    assert "MOVER" in ctx and "OTHER" not in ctx


# ---------- fundamental: rotacion por lotes ----------

def test_fundamental_batch_rotation_covers_universe():
    """Ejecuciones sucesivas recorren todo el universo por bloques (con wraparound)."""
    watchlist = [f"T{i}" for i in range(5)]
    searcher = FundamentalSearcher(watchlist=watchlist, batch_size=2)
    assert searcher._next_batch() == ["T0", "T1"]
    assert searcher._next_batch() == ["T2", "T3"]
    assert searcher._next_batch() == ["T4", "T0"]  # wraparound
    assert searcher._next_batch() == ["T1", "T2"]


# ---------- technical: rotacion por lotes ----------

async def test_technical_batch_rotation_limits_fetch():
    """Cada ciclo analiza solo su lote rotatorio (limita el fetch a IBKR)."""
    bars = [
        OHLCVBar(open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1000)
        for p in [100 + i for i in range(30)]
    ]
    watchlist = [f"T{i}" for i in range(5)]
    market = MockMarketDataClient(ohlcv={t: bars for t in watchlist})
    searcher = TechnicalSearcher(market_data=market, watchlist=watchlist, batch_size=2)

    ctx1 = await searcher.build_context()
    assert "T0:" in ctx1 and "T1:" in ctx1
    assert "T2:" not in ctx1 and "T3:" not in ctx1 and "T4:" not in ctx1

    ctx2 = await searcher.build_context()  # rota al siguiente lote
    assert "T2:" in ctx2 and "T3:" in ctx2
    assert "T0:" not in ctx2 and "T1:" not in ctx2
