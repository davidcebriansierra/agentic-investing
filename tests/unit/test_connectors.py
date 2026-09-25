"""Tests de los conectores mock."""
from __future__ import annotations

import pytest

from src.connectors.ibkr_read import MockMarketDataClient
from src.connectors.ibkr_write import MockBrokerClient
from src.connectors.news_apis import MockNewsClient
from src.connectors.social_apis import MockSocialClient
from src.connectors.telegram_bot import MockHITLClient, build_request
from src.schemas.decision import Decision
from src.schemas.enums import DecisionReason, DecisionType, Exchange
from src.schemas.feeds import NewsItem, SocialPost
from src.schemas.hitl import ApprovalDecision, EditedPrices
from src.schemas.market import Quote
from src.schemas.portfolio import Portfolio
from tests.conftest import make_opportunity


async def test_market_data_client_quote_and_context():
    quote = Quote(ticker="IBE.MC", exchange=Exchange.BME, last=12.5, avg_volume_20d=500000)
    client = MockMarketDataClient(
        portfolio=Portfolio(total_equity=100_000, cash=80_000),
        quotes={"IBE.MC": quote},
    )
    await client.connect()
    assert client.connected
    assert (await client.get_quote("IBE.MC")).last == 12.5
    assert await client.is_market_open(Exchange.BME) is True
    ctx = await client.build_market_context("IBE.MC", Exchange.BME)
    assert ctx.current_price == 12.5
    assert ctx.account_equity == 100_000
    assert ctx.available_capital == 80_000


async def test_market_data_market_closed():
    client = MockMarketDataClient(open_exchanges=set())
    assert await client.is_market_open(Exchange.NYSE) is False


async def test_broker_places_order():
    broker = MockBrokerClient()
    await broker.connect()
    from src.schemas.enums import OrderAction
    from src.schemas.order import Order, OrderLeg

    order = Order(
        ticker="IBE.MC",
        action=OrderAction.BUY,
        quantity=100,
        entry=OrderLeg(type="LMT", price=12.5),
        take_profit=OrderLeg(type="LMT", price=12.8),
        stop_loss=OrderLeg(type="STP", price=12.3),
        account="DU123",
    )
    result = await broker.place_bracket_order(order)
    assert result.fill_price == 12.5
    assert result.fill_quantity == 100
    assert len(broker.placed_orders) == 1


async def test_news_client_filters_by_ticker():
    client = MockNewsClient(
        [
            NewsItem(source="newsapi", headline="IBE sube", tickers=["IBE.MC"]),
            NewsItem(source="finnhub", headline="AAPL earnings", tickers=["AAPL"]),
        ]
    )
    res = await client.fetch(tickers=["IBE.MC"])
    assert len(res) == 1
    assert res[0].tickers == ["IBE.MC"]
    assert len(await client.fetch()) == 2


async def test_social_client_filters_by_ticker():
    client = MockSocialClient(
        [
            SocialPost(platform="reddit", channel="wallstreetbets", text="AAPL moon", tickers=["AAPL"]),
        ]
    )
    assert await client.fetch(tickers=["IBE.MC"]) == []
    assert len(await client.fetch(tickers=["AAPL"])) == 1


async def test_hitl_default_timeout_is_not_approved():
    client = MockHITLClient()  # default TIMEOUT
    opp = make_opportunity()
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    request = build_request(decision, opp)
    response = await client.request_approval(request)
    assert response.decision == ApprovalDecision.TIMEOUT
    assert response.approved is False


async def test_hitl_approve():
    client = MockHITLClient(default_decision=ApprovalDecision.APPROVE)
    opp = make_opportunity()
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    response = await client.request_approval(build_request(decision, opp))
    assert response.approved is True
    assert len(client.requests) == 1


async def test_hitl_reject_is_not_approved():
    client = MockHITLClient(default_decision=ApprovalDecision.REJECT)
    opp = make_opportunity()
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    response = await client.request_approval(build_request(decision, opp))
    assert response.decision == ApprovalDecision.REJECT
    assert response.approved is False


async def test_hitl_pause_is_not_approved():
    client = MockHITLClient(default_decision=ApprovalDecision.PAUSE_1H)
    opp = make_opportunity()
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    response = await client.request_approval(build_request(decision, opp))
    assert response.decision == ApprovalDecision.PAUSE_1H
    assert response.approved is False


async def test_hitl_timeout_responder_is_none():
    client = MockHITLClient(default_decision=ApprovalDecision.TIMEOUT)
    opp = make_opportunity()
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    response = await client.request_approval(build_request(decision, opp))
    assert response.decision == ApprovalDecision.TIMEOUT
    assert response.approved is False
    assert response.responder == "mock_user"  # MockHITLClient siempre rellena responder


async def test_hitl_decision_fn_overrides_default():
    """decision_fn permite comportamiento dinamico segun el contenido del request."""
    def fn(req):
        return ApprovalDecision.APPROVE if req.final_score >= 0.8 else ApprovalDecision.REJECT

    client = MockHITLClient(decision_fn=fn)
    opp = make_opportunity()

    high_score = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.9,
        expectancy_pct=1.0,
        risk_reward_ratio=2.5,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    resp_high = await client.request_approval(build_request(high_score, opp))
    assert resp_high.approved is True

    low_score = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.5,
        expectancy_pct=0.3,
        risk_reward_ratio=1.5,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    resp_low = await client.request_approval(build_request(low_score, opp))
    assert resp_low.approved is False
    assert resp_low.decision == ApprovalDecision.REJECT


async def test_hitl_all_decisions_are_not_approved_except_approve():
    """Invariante: solo APPROVE produce approved=True; el resto no opera."""
    opp = make_opportunity()
    base_decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.7,
        expectancy_pct=0.5,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    request = build_request(base_decision, opp)
    for dec in ApprovalDecision:
        client = MockHITLClient(default_decision=dec)
        response = await client.request_approval(request)
        expected = dec == ApprovalDecision.APPROVE
        assert response.approved == expected, (
            f"decision={dec.value}: esperado approved={expected}, obtenido {response.approved}"
        )


async def test_hitl_edited_prices_propagated_on_approve():
    """APPROVE con edited_prices inyectado -> response.edited_prices != None."""
    prices = EditedPrices(entry_price=196.0, stop_loss=191.5, take_profit=202.0)
    client = MockHITLClient(default_decision=ApprovalDecision.APPROVE, edited_prices=prices)
    opp = make_opportunity()
    decision = Decision(
        opportunity_id=opp.opportunity_id,
        final_score=0.75,
        expectancy_pct=1.0,
        risk_reward_ratio=2.0,
        decision=DecisionType.OPERATE,
        reason=DecisionReason.CONSENSUS_REACHED,
    )
    response = await client.request_approval(build_request(decision, opp))
    assert response.approved is True
    assert response.edited_prices is not None
    assert response.edited_prices.entry_price == 196.0
    assert response.edited_prices.stop_loss == 191.5
    assert response.edited_prices.take_profit == 202.0


async def test_hitl_edited_prices_not_propagated_on_reject():
    """edited_prices inyectado NO se propaga si la decision no es APPROVE."""
    prices = EditedPrices(entry_price=196.0, stop_loss=191.5, take_profit=202.0)
    for dec in (ApprovalDecision.REJECT, ApprovalDecision.TIMEOUT, ApprovalDecision.PAUSE_1H):
        client = MockHITLClient(default_decision=dec, edited_prices=prices)
        opp = make_opportunity()
        decision = Decision(
            opportunity_id=opp.opportunity_id,
            final_score=0.75,
            expectancy_pct=1.0,
            risk_reward_ratio=2.0,
            decision=DecisionType.OPERATE,
            reason=DecisionReason.CONSENSUS_REACHED,
        )
        response = await client.request_approval(build_request(decision, opp))
        assert response.edited_prices is None, f"decision={dec.value} no debe propagar edited_prices"


def test_apify_social_prefilter_drops_junk():
    """Regresion: el pre-filtro descarta memecoins/airdrops y spam de engagement,
    conservando tweets legitimos de la watchlist."""
    from src.connectors.apify_social_apis import _is_junk

    junk = [
        "$BAC SNIPER ALERT CA: 0x8564e2cf5b6f72ff4705e702cbed54f0c3587777",
        "$ELE portal open, might be the easiest $70 today",
        "does not affect the redemption plan. move tokens to self-custody wallets",
        "Sharing my trading experience! $ALL $ECL $MTRN turning my initial $10,000",
        "$CAT holders, claim portal is open",
        "buy $NVDA $AAPL $GOOGL $AMZN $COIN $SPCX",
    ]
    legit = [
        "$BBVA combina resultados solidos con una valoracion exigente",
        "$AMGN Amgen raises FY2026 guidance again",
        "$ADM Admiral profit falls 18% as UK motor market more challenging",
        "Super keen on $AMD at current prices",
        "$AMAT looking for a dip towards the $510 area",
    ]
    for t in junk:
        assert _is_junk(t.upper(), t.lower()) is True, t
    for t in legit:
        assert _is_junk(t.upper(), t.lower()) is False, t


def test_ibkr_bracket_links_children_to_parent():
    """Regresion: TP y SL deben enlazar con el orderId del parent (no 0); si no, IBKR
    transmitiria solo el SL y quedaria un stop huerfano sin compra ni take-profit."""
    pytest.importorskip("ib_async")
    from src.connectors.ibkr_write import IBKRBrokerClient
    from src.schemas.enums import OrderAction
    from src.schemas.order import Order, OrderLeg

    client = object.__new__(IBKRBrokerClient)  # sin __init__: no conecta a IB Gateway
    client.account = "DU123"
    order = Order(
        ticker="AAPL", action=OrderAction.BUY, quantity=10,
        entry=OrderLeg(type="LMT", price=100.0),
        take_profit=OrderLeg(type="LMT", price=110.0),
        stop_loss=OrderLeg(type="STP", price=95.0),
        account="DU123",
    )
    parent, tp, sl = client._build_bracket_orders(order, parent_id=42)
    # El parent lleva el orderId reservado y los hijos lo referencian.
    assert parent.orderId == 42
    assert tp.parentId == 42 and sl.parentId == 42
    # transmit: solo el SL transmite el bracket completo.
    assert parent.transmit is False and tp.transmit is False and sl.transmit is True
    # Acciones y precios correctos (compra -> cierres en venta).
    assert parent.action == "BUY" and tp.action == "SELL" and sl.action == "SELL"
    assert parent.lmtPrice == 100.0 and tp.lmtPrice == 110.0 and sl.auxPrice == 95.0


def test_make_stock_contract_bme_us_and_multiclass():
    """El constructor compartido resuelve BME en EUR/BM (con override), US en USD y las
    clases de accion con espacio. Lo usan tanto lectura como escritura (mismo contrato)."""
    from types import SimpleNamespace

    from src.connectors.ibkr_contracts import make_stock_contract

    def FakeStock(symbol, exchange, currency, primaryExchange=None):
        return SimpleNamespace(
            symbol=symbol, exchange=exchange, currency=currency,
            primaryExchange=primaryExchange,
        )

    san = make_stock_contract("SAN.MC", FakeStock)
    assert (san.symbol, san.currency, san.primaryExchange) == ("SAN", "EUR", "BM")
    mts = make_stock_contract("MTS.MC", FakeStock)  # override ArcelorMittal
    assert (mts.symbol, mts.currency, mts.primaryExchange) == ("MT", "EUR", "AEB")
    aapl = make_stock_contract("AAPL", FakeStock)
    assert (aapl.symbol, aapl.currency) == ("AAPL", "USD")
    brk = make_stock_contract("BRK.B", FakeStock)
    assert (brk.symbol, brk.currency) == ("BRK B", "USD")


def test_geometry_error_long_and_short():
    """Regresion: la validacion de geometria de precios editados replica la de Opportunity."""
    from src.connectors.telegram_bot import _geometry_error

    # LONG: SL < entrada < TP.
    assert _geometry_error(100.0, 95.0, 110.0, is_long=True) is None
    assert _geometry_error(100.0, 105.0, 110.0, is_long=True) is not None  # SL sobre entrada
    assert _geometry_error(100.0, 95.0, 99.0, is_long=True) is not None    # TP bajo entrada
    # SHORT: TP < entrada < SL.
    assert _geometry_error(100.0, 105.0, 90.0, is_long=False) is None
    assert _geometry_error(100.0, 95.0, 90.0, is_long=False) is not None   # SL bajo entrada
