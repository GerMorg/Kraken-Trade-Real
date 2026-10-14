from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Instrument, MarketSnapshot, Signal
from app.domain.states import Direction


D = Decimal


def gate_unvalidated_external_effects(
    config: Any, news_bps: Any, gemini_bps: Any
) -> dict[str, Any]:
    """Prevent unvalidated News/LLM impacts from justifying live entries.

    During active live execution, third-party text-derived effects are retained
    as raw diagnostics but return zero signal contribution until an independent,
    cost-adjusted out-of-sample evaluation explicitly supports enabling them.
    In non-live/shadow mode, valid effects remain available for research.
    """
    parsed: dict[str, D] = {}
    invalid: list[str] = []
    for name, value in (("news_bps", news_bps), ("gemini_bps", gemini_bps)):
        try:
            candidate = D(str(value))
        except (ArithmeticError, TypeError, ValueError):
            candidate = D("NaN")
        if not candidate.is_finite():
            invalid.append(name)
            candidate = D("0")
        parsed[name] = candidate

    live_execution_active = (
        bool(getattr(config, "live_enabled", False))
        and not bool(getattr(config, "kill_switch", True))
    )
    if live_execution_active:
        return {
            "news_bps": D("0"),
            "gemini_bps": D("0"),
            "shadow_only": True,
            "reason": "LIVE_EXTERNAL_EFFECTS_NOT_OUT_OF_SAMPLE_VALIDATED",
            "invalid_inputs": invalid,
        }
    return {
        "news_bps": parsed["news_bps"],
        "gemini_bps": parsed["gemini_bps"],
        "shadow_only": False,
        "reason": "SHADOW_OR_DISABLED_LIVE",
        "invalid_inputs": invalid,
    }


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
        # Core volatility is expressed in percentage points over a fixed 15-bar
        # horizon. Keep the historical model coefficient explicit as a bps surcharge
        # per percentage point; do not silently mix it with a changing sample horizon.
        volatility_cost_bps = vol * D("1.5")
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
            + volatility_cost_bps
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
                "volatility_cost_bps": volatility_cost_bps,
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
