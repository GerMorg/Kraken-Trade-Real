from __future__ import annotations

import json
import time
from app.kraken.ws import WebSocketSupervisor


class Audit:
    def emit(self, *args, **kwargs):
        return None


class Recovery:
    def issue(self, *args, **kwargs):
        return None


def test_websocket_stream_builds_book_ticker_and_trade_metrics():
    ws = WebSocketSupervisor(Audit(), Recovery())
    now = time.time()
    ticker = {
        "channel": "ticker",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "bid": "100.0",
            "bid_qty": "10",
            "ask": "100.1",
            "ask_qty": "5",
            "last": "100.05",
            "timestamp": now,
        }],
    }
    ws.on_message(json.dumps(ticker))
    book = {
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "bids": [{"price": "100.0", "qty": "10"}],
            "asks": [{"price": "100.1", "qty": "2"}],
        }],
    }
    ws.on_message(json.dumps(book))
    for i in range(40):
        trade = {
            "channel": "trade",
            "type": "update",
            "data": [{
                "symbol": "BTC/USD",
                "side": "buy",
                "price": "100.05",
                "qty": "0.01",
                "timestamp": now - 1 + i * 0.01,
            }],
        }
        ws.on_message(json.dumps(trade))

    snapshot = ws.market_snapshot("BTC/USD")
    assert snapshot is not None
    assert snapshot["bid"] > 0
    assert snapshot["ask"] > snapshot["bid"]
    assert snapshot["depths_bid"]
    assert snapshot["depths_ask"]
    assert "price_points" in snapshot

    metrics = ws.trade_metrics("BTC/USD")
    assert metrics["trade_count"] == 40
    assert metrics["recent_quote"] >= 0
    assert metrics["volume_ratio"] >= 0


def test_websocket_symbol_subscriptions_are_deterministic():
    ws = WebSocketSupervisor(Audit(), Recovery())
    ws.set_symbols(["ETH/USD", "BTC/USD", "BTC/USD"])
    assert ws.symbols() == ("BTC/USD", "ETH/USD")
