from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from app.domain.models import Instrument, MarketSnapshot


D=Decimal


class MarketScanner:
    def __init__(self, min_liquidity_eur: float, max_spread_bps: float, freshness_seconds: int) -> None:
        self.min_liquidity = D(str(min_liquidity_eur))
        self.max_spread = D(str(max_spread_bps))
        self.freshness_seconds = freshness_seconds

    def fast_filter(
        self,
        instruments: Iterable[Instrument],
        snapshots: dict[str, MarketSnapshot],
        *,
        require_history: bool = True,
    ) -> list[Instrument]:
        candidates=[]
        for instrument in instruments:
            snap=snapshots.get(instrument.symbol)
            if not snap or not instrument.tradeable:
                continue
            if snap.age_seconds > self.freshness_seconds:
                continue
            if snap.spread_bps > self.max_spread:
                continue
            if snap.volume_24h < self.min_liquidity and instrument.product_type.value != "DERIVATIVE":
                continue
            if require_history and len(snap.closes) < 30:
                continue
            candidates.append(instrument)
        return sorted(candidates, key=lambda i: snapshots[i.symbol].volume_24h, reverse=True)

    def rank(self, candidates: Iterable[Instrument], snapshots: dict[str, MarketSnapshot],
             features: dict[str, dict[str, Decimal]]) -> list[Instrument]:
        def score(inst: Instrument) -> Decimal:
            f=features[inst.symbol]
            trend=abs(f.get("trend", D("0")))
            momentum=abs(f.get("return_5", D("0")))
            liquidity=(f.get("liquidity", D("0"))+D("1")).ln()
            spread=max(D("0.1"), f.get("spread_bps", D("9999")))
            return trend*D("5")+momentum*D("2")+liquidity-spread/D("100")
        return sorted(candidates, key=score, reverse=True)
