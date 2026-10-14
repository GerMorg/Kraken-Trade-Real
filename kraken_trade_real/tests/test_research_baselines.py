from __future__ import annotations

import pytest

from app.learning.baselines import (
    buy_and_hold_signal,
    cash_signal,
    momentum_signal,
    trend_signal,
)


def history(prices):
    return [
        {"captured_at": float(i), "price": float(price)}
        for i, price in enumerate(prices)
    ]


def test_cash_and_buy_and_hold_baselines_are_explicit():
    rows = history([100, 101, 102])
    assert cash_signal(rows) == 0
    assert buy_and_hold_signal([]) == 0
    assert buy_and_hold_signal(rows) == 1


def test_momentum_baseline_uses_only_past_quote_observations():
    rows = history([100, 100, 100, 101, 102])
    assert momentum_signal(rows[:3], lookback_observations=2) == 0
    assert momentum_signal(rows, lookback_observations=2, threshold_bps=50) == 1
    assert momentum_signal(rows, lookback_observations=2, threshold_bps=300) == 0


def test_trend_baseline_requires_sufficient_chronological_history():
    rows = history([100, 98, 97, 99, 103, 106])
    assert trend_signal(rows[:3], fast_observations=2, slow_observations=3) == 0
    assert trend_signal(rows, fast_observations=2, slow_observations=4) == 1


def test_baselines_fail_flat_on_invalid_price_and_validate_parameters():
    rows = history([100, 101])
    rows[-1]["price"] = float("nan")
    assert momentum_signal(rows, lookback_observations=1) == 0
    with pytest.raises(ValueError):
        momentum_signal(history([100, 101]), lookback_observations=0)
    with pytest.raises(ValueError):
        trend_signal(history([100, 101]), fast_observations=3, slow_observations=2)
