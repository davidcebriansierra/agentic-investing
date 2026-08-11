"""Conectores a sistemas externos (IBKR, noticias, redes, Telegram).

Cada conector define una interfaz (Protocol) y al menos una implementacion mock
ejecutable sin red, mas un stub de la implementacion real.
"""
from src.connectors.fundamentals import (
    FallbackFundamentalsClient,
    YFinanceFundamentalsClient,
)
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
from src.connectors.apify_social_apis import ApifySocialClient
from src.connectors.news_apis import HttpNewsClient, MockNewsClient, NewsClient
from src.connectors.social_apis import (
    HttpSocialClient,
    MockSocialClient,
    SocialClient,
)
from src.connectors.telegram_bot import (
    HITLClient,
    MockHITLClient,
    TelegramHITLClient,
    build_request,
)

__all__ = [
    "ApifySocialClient",
    "BrokerClient",
    "FallbackFundamentalsClient",
    "HITLClient",
    "HttpNewsClient",
    "HttpSocialClient",
    "IBKRBrokerClient",
    "IBKRMarketDataClient",
    "MarketDataClient",
    "MockBrokerClient",
    "MockHITLClient",
    "MockMarketDataClient",
    "MockNewsClient",
    "MockSocialClient",
    "NewsClient",
    "SocialClient",
    "YFinanceFundamentalsClient",
    "TelegramHITLClient",
    "build_request",
]
