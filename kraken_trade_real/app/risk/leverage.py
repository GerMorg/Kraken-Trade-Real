from __future__ import annotations

from decimal import Decimal
from typing import Any


D=Decimal


class LeverageEngine:
    IMMUTABLE_MAX_LEVERAGE=D("5")

    def choose(self, instrument: Any, features: dict[str,D], confidence: D,
               portfolio_gross_pct: D, margin_level_pct: D | None,
               configured_max: D, require_margin: bool = False) -> D:
        available=max(instrument.leverage_levels or (D("1"),))
        max_allowed=min(available,configured_max,self.IMMUTABLE_MAX_LEVERAGE)
        if require_margin and max_allowed < D("2"):
            return D("0")
        if max_allowed<=1:
            return D("1")
        if margin_level_pct is not None and margin_level_pct < D("300"):
            return D("0") if require_margin else D("1")
        vol=features.get("volatility",D("999"))
        spread=features.get("spread_bps",D("999"))
        trend=abs(features.get("trend",D("0")))
        score=confidence*D("2") + trend
        if vol > D("10") or spread > D("80") or portfolio_gross_pct > D("60"):
            return D("0") if require_margin else D("1")
        if score < D("1"):
            chosen = min(D("2"),max_allowed) if require_margin else D("1")
        elif score < D("1.6"):
            chosen = min(D("2"),max_allowed)
        elif score < D("2.3"):
            chosen = min(D("3"),max_allowed)
        else:
            chosen = max_allowed
        if require_margin and chosen < D("2"):
            return D("0")
        return chosen
