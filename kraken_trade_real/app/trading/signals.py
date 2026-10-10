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
        strategy_parameters: dict[str, Any] | None = None,
    ) -> tuple[Signal, Signal]:
        params = strategy_parameters or {}

        def weight(name: str, default: str) -> D:
            try:
                return max(D("0"), D(str(params.get(name, default))))
            except Exception:
                return D(default)

        trend_weight = weight("signal_weight_trend", "18")
        return_5_weight = weight("signal_weight_return_5", "4")
        return_15_weight = weight("signal_weight_return_15", "5")
        return_60_weight = weight("signal_weight_return_60", "6")
        return_240_weight = weight("signal_weight_return_240", "3")
        news_weight = weight("signal_weight_news", "1")
        gemini_weight = weight("signal_weight_gemini", "1")
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
            trend * trend_weight
            + momentum_5 * return_5_weight
            + momentum_15 * return_15_weight
            + momentum_60 * return_60_weight
            + momentum_240 * return_240_weight
        )
        long_raw = directional_raw + news_bps * news_weight + gemini_bps * gemini_weight
        short_raw = -directional_raw - news_bps * news_weight - gemini_bps * gemini_weight
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
                "signal_weight_trend": trend_weight,
                "signal_weight_return_5": return_5_weight,
                "signal_weight_return_15": return_15_weight,
                "signal_weight_return_60": return_60_weight,
                "signal_weight_return_240": return_240_weight,
                "signal_weight_news": news_weight,
                "signal_weight_gemini": gemini_weight,
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
