from __future__ import annotations

from decimal import Decimal
from typing import Any


D = Decimal


class LeverageEngine:
    IMMUTABLE_MAX_LEVERAGE = D("5")
    IMMUTABLE_MIN_MARGIN_LEVEL = D("200")

    def choose(
        self,
        instrument: Any,
        features: dict[str, D],
        confidence: D,
        portfolio_gross_pct: D,
        margin_level_pct: D | None,
        configured_max: D,
        require_margin: bool = False,
    ) -> D:
        available = max(instrument.leverage_levels or (D("1"),))
        max_allowed = min(
            available,
            configured_max,
            self.IMMUTABLE_MAX_LEVERAGE,
        )
        if not require_margin:
            if max_allowed <= 1:
                return D("1")
            if margin_level_pct is not None and margin_level_pct < D("200"):
                return D("1")
            vol = features.get("volatility", D("999"))
            spread = features.get("spread_bps", D("999"))
            score = confidence * D("2") + abs(features.get("trend", D("0")))
            if vol > D("10") or spread > D("80") or portfolio_gross_pct > D("60"):
                return D("1")
            if score < D("1.0"):
                return D("1")
            if score < D("1.6"):
                return min(D("2"), max_allowed)
            if score < D("2.3"):
                return min(D("3"), max_allowed)
            return max_allowed

        # A new Spot Margin short is a real leveraged position. The old
        # implementation returned zero for ordinary volatility >10 bps, which
        # made supported shorts practically impossible. Apply the actual hard
        # margin/market guards here, then select the lowest sufficient leverage.
        if max_allowed < D("2"):
            return D("0")
        if margin_level_pct is not None and margin_level_pct < self.IMMUTABLE_MIN_MARGIN_LEVEL:
            return D("0")

        volatility = features.get("volatility", D("999"))
        spread = features.get("spread_bps", D("999"))
        if volatility > D("30") or spread > D("60") or portfolio_gross_pct > D("60"):
            return D("0")

        confidence_score = confidence * D("2") + abs(features.get("trend", D("0")))
        if confidence_score < D("1.5"):
            return D("2") if max_allowed >= D("2") else D("0")
        if confidence_score < D("2.2"):
            return min(D("3"), max_allowed)
        return max_allowed
