from __future__ import annotations

import time
from typing import Iterable

from .models import Instrument


class HistoryBackfill:
    """Persist a bounded warm-up of current market data before live decisions."""

    def __init__(self, gateway, store, per_instrument: int = 1) -> None:
        self.gateway = gateway
        self.store = store
        self.per_instrument = max(1, per_instrument)

    async def run(self, instruments: Iterable[Instrument]) -> int:
        written = 0
        for instrument in instruments:
            for _ in range(self.per_instrument):
                try:
                    snapshot = await self.gateway.market_snapshot(instrument)
                except Exception as exc:  # noqa: BLE001 - isolated data source failure
                    self.store.event(
                        "error_events",
                        {"error_code": "DATA_ERROR", "stage": "HISTORY_BACKFILL", "symbol": instrument.symbol, "error": type(exc).__name__},
                        time.time(),
                    )
                    break
                payload = {
                    "instrument": instrument.symbol,
                    "last": snapshot.last,
                    "bid": snapshot.bid,
                    "ask": snapshot.ask,
                    "volume_24h": snapshot.volume_24h,
                    "funding": snapshot.funding,
                    "open_interest": snapshot.open_interest,
                    "timestamp": snapshot.timestamp,
                    "candles": snapshot.candles,
                    "book": {"bids": snapshot.book.bids, "asks": snapshot.book.asks},
                }
                self.store.save_market_snapshot("BACKFILL", instrument.symbol, payload, snapshot.timestamp)
                written += 1
                break
        return written
