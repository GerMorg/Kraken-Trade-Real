from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from custom_components.kraken_ai_trader.core.authority import CentralTradingAuthority
from custom_components.kraken_ai_trader.core.models import Instrument, MarketSnapshot, OrderBook, PortfolioSnapshot, ProductType, SafetyLimits
from custom_components.kraken_ai_trader.core.news import NewsEngine
from custom_components.kraken_ai_trader.core.storage import Store


class FakeGateway:
    futures_key = ""
    def __init__(self):
        self.instruments = {}
        self.submitted = []
    async def start(self): pass
    async def close(self): pass
    async def system_status(self): return "online"
    async def authenticate(self): return {"query-funds", "query-open-trades", "modify-trades", "close-trades"}
    async def discover(self):
        i = Instrument("fake", ProductType.SPOT_MARGIN, "BTC/USD", "XXBTZUSD", "XBTUSD", "BTC", "USD", None, "online", True, True, True, (Decimal("1"), Decimal("2")), Decimal("2"), Decimal("0.0001"), Decimal("0.5"), 8, 1, Decimal("0.1"))
        self.instruments = {i.instrument_id: i}
        return [i]
    async def portfolio(self):
        return PortfolioSnapshot(Decimal("1000"), Decimal("1000"), Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), (), 0, 0, time.time())
    async def market_snapshot(self, i):
        book = OrderBook(((Decimal("100"), Decimal("10000")),), ((Decimal("100.01"), Decimal("10000")),), time.time())
        candles = {15: tuple(Decimal(str(100 + i * 0.5)) for i in range(60))}
        return MarketSnapshot(i, Decimal("100.01"), Decimal("100"), Decimal("100.01"), Decimal("1000000"), book, candles, None, None, time.time())
    async def submit(self, intent):
        self.submitted.append(intent)
        return "FAKE-ORDER", {"status": "placed"}


class FakeNews(NewsEngine):
    def __init__(self): super().__init__([]); self.last_fetch_ok = True
    async def fetch(self): return []


class FakeGemini:
    api_key = ""
    healthy = True


def test_end_to_end_paper_cycle(tmp_path):
    async def run():
        gateway = FakeGateway(); store = Store(tmp_path / "trader.db")
        limits = SafetyLimits(Decimal("0.02"), Decimal("1"), Decimal("1"), Decimal("0.25"), Decimal("3"), 5, Decimal("0.05"), Decimal("0.10"), Decimal("10"), Decimal("0.0025"), 20)
        authority = CentralTradingAuthority(gateway, FakeNews(), FakeGemini(), store, limits, {"enabled": True, "live_enabled": False, "news_enabled": False, "minimum_liquidity": 1000, "max_spread": 0.01, "minimum_expected_edge": 0.001})
        await authority.startup()
        state = await authority.cycle()
        assert state.system_ready
        assert state.last_action in {"PAPER_INTENT", "NO_TRADE"}
        await authority.close()
    asyncio.run(run())
