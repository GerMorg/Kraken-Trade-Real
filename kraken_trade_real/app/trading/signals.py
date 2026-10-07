from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Instrument, MarketSnapshot, Signal
from app.domain.states import Direction


D = Decimal


class SignalEngine:
    """Cost-aware core signal model using multiple market horizons."""

    def __init__(self, config: Any | None = None) -> None:
        self.config = config

    def _fee_cost_bps(self) -> D:
        entry = D(str(getattr(self.config, "strategy_entry_fee_bps", 40.0)))
        exit = D(str(getattr(self.config, "strategy_exit_fee_bps", 80.0)))
        overhead = D(str(getattr(self.config, "strategy_execution_overhead_bps", 8.0)))
        return max(D("0"), entry) + max(D("0"), exit) + max(D("0"), overhead)

    def evaluate(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        features: dict[str, D],
        regime: str,
        news_bps: D = D("0"),
        gemini_bps: D = D("0"),
    ) -> tuple[Signal, Signal]:
        trend = features.get("trend", D("0"))
        momentum_5 = features.get("return_5", D("0"))
        momentum_15 = features.get("return_15", D("0"))
        momentum_60 = features.get("return_60", D("0"))
        momentum_240 = features.get("return_240", D("0"))
        vol = max(D("1"), features.get("volatility", D("999")))
        liquidity = max(D("1"), features.get("liquidity", D("1")))
        spread = features.get("spread_bps", D("999"))
        quality = (
            max(D("0"), D("1") - spread / D("200"))
            * min(D("1"), liquidity / D("1000"))
        )

        directional_raw = (
            trend * D("18")
            + momentum_5 * D("4")
            + momentum_15 * D("5")
            + momentum_60 * D("6")
            + momentum_240 * D("3")
        )
        long_raw = directional_raw + news_bps + gemini_bps
        short_raw = -directional_raw - news_bps - gemini_bps
        long_return = max(D("0"), long_raw) * quality
        short_return = max(D("0"), short_raw) * quality

        cost = (
            spread
            + vol * D("1.5")
            + self._fee_cost_bps()
        )
        long_conf = max(
            D("0"),
            min(
                D("1"),
                D("0.5")
                + long_raw / D("45")
                - vol / D("120"),
            ),
        )
        short_conf = max(
            D("0"),
            min(
                D("1"),
                D("0.5")
                + short_raw / D("45")
                - vol / D("120"),
            ),
        )
        shared_features = dict(features)
        shared_features.update(
            {
                "return_15": momentum_15,
                "return_60": momentum_60,
                "return_240": momentum_240,
            }
        )
        return (
            Signal(
                instrument.symbol,
                Direction.LONG,
                long_return,
                cost,
                long_conf,
                regime,
                news_bps,
                gemini_bps,
                shared_features,
            ),
            Signal(
                instrument.symbol,
                Direction.SHORT,
                short_return,
                cost,
                short_conf,
                regime,
                -news_bps,
                -gemini_bps,
                shared_features,
            ),
        )
