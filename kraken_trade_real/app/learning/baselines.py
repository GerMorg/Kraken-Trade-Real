from __future__ import annotations

import math
from typing import Any, Callable


def _prices(history: list[dict[str, Any]]) -> list[float] | None:
    prices: list[float] = []
    for row in history:
        try:
            value = float(row["price"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        prices.append(value)
    return prices


def cash_signal(history: list[dict[str, Any]]) -> int:
    """Predefined cash/flat benchmark."""
    return 0


def buy_and_hold_signal(history: list[dict[str, Any]]) -> int:
    """Predefined always-long benchmark; starts at the first executable quote."""
    return 1 if history else 0


def momentum_signal(
    history: list[dict[str, Any]], *, lookback_observations: int = 12,
    threshold_bps: float = 0.0,
) -> int:
    """Long/flat momentum baseline using only archived quotes at or before decision time."""
    if (
        lookback_observations < 1
        or not math.isfinite(float(threshold_bps))
        or threshold_bps < 0
    ):
        raise ValueError("invalid momentum baseline parameters")
    prices = _prices(history)
    if prices is None or len(prices) <= lookback_observations:
        return 0
    start, current = prices[-lookback_observations - 1], prices[-1]
    return 1 if (current / start - 1.0) * 10_000.0 > threshold_bps else 0


def trend_signal(
    history: list[dict[str, Any]], *, fast_observations: int = 5,
    slow_observations: int = 20,
) -> int:
    """Long/flat fast-vs-slow mean baseline on archived quotes only."""
    if fast_observations < 1 or slow_observations <= fast_observations:
        raise ValueError("trend baseline requires 0 < fast < slow")
    prices = _prices(history)
    if prices is None or len(prices) < slow_observations:
        return 0
    fast_mean = sum(prices[-fast_observations:]) / fast_observations
    slow_mean = sum(prices[-slow_observations:]) / slow_observations
    return 1 if fast_mean > slow_mean else 0


def standard_baselines() -> dict[str, Callable[[list[dict[str, Any]]], int]]:
    """Return fixed, long/flat baselines for predeclared comparisons.

    These are reference strategies, not tuned winners. Parameter selection should
    occur on training/validation data only, never on the final test window.
    """
    return {
        "CASH": cash_signal,
        "BUY_AND_HOLD": buy_and_hold_signal,
        "MOMENTUM_12_OBS_25_BPS": lambda rows: momentum_signal(
            rows, lookback_observations=12, threshold_bps=25.0
        ),
        "TREND_FAST5_SLOW20": lambda rows: trend_signal(
            rows, fast_observations=5, slow_observations=20
        ),
    }
