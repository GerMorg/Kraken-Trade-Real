from __future__ import annotations

from app.learning.backtest import BacktestEngine


def observation(i, signal, *, bid=99.9, ask=100.1, bid_depth=100.0, ask_depth=100.0):
    return {
        "decision_timestamp": float(i * 60),
        "execution_timestamp": float(i * 60 + 1),
        "signal": signal,
        "bid": bid,
        "ask": ask,
        "bid_depth_base": bid_depth,
        "ask_depth_base": ask_depth,
        "min_order_qty": 0.001,
        "min_order_cost_quote": 1.0,
    }


def test_backtest_applies_fees_spread_slippage_and_financing():
    rows = [observation(0, 1), observation(1, 0)]
    result = BacktestEngine().run(
        rows, initial_equity_quote=1000, target_notional_quote=100,
        taker_fee_bps=8, slippage_bps=3, impact_coefficient_bps=15,
        financing_bps_per_bar=2,
    )
    assert result["status"] == "OK"
    assert len(result["fills"]) == 2
    assert result["metrics"]["fees_quote"] > 0
    assert result["metrics"]["slippage_and_impact_cost_quote"] > 0
    assert result["metrics"]["financing_quote"] > 0
    assert result["metrics"]["final_equity_quote"] < 1000
    assert result["assumptions"]["automatic_promotion"] is False


def test_backtest_caps_fill_to_visible_depth_and_retries_remaining_delta_later():
    rows = [
        observation(0, 1, ask_depth=2.0),
        observation(1, 1, ask_depth=2.0),
        observation(2, 0, bid_depth=100.0, ask_depth=100.0),
    ]
    result = BacktestEngine().run(
        rows, initial_equity_quote=1000, target_notional_quote=500,
        max_depth_fraction=0.10,
    )
    assert result["status"] == "OK"
    assert result["fills"][0]["quantity"] <= 0.2
    assert result["fills"][0]["partial"] is True
    assert len(result["fills"]) >= 2


def test_backtest_skips_stale_quote_and_minimum_order_and_reports_reason():
    rows = [
        {**observation(0, 1), "execution_timestamp": 5000.0},
        {**observation(1, 1), "min_order_qty": 1_000_000},
    ]
    result = BacktestEngine().run(
        rows, initial_equity_quote=1000, target_notional_quote=100,
        max_quote_age_seconds=30,
    )
    assert result["status"] == "NO_EXECUTABLE_FILLS"
    assert result["skipped"]["STALE_EXECUTION_QUOTE"] == 1
    assert result["skipped"]["BELOW_EXCHANGE_MINIMUM"] == 1


def test_backtest_rejects_noncausal_execution_timestamp():
    rows = [observation(1, 1)]
    rows[0]["execution_timestamp"] = rows[0]["decision_timestamp"]
    result = BacktestEngine().run(rows)
    assert result["status"] == "LOOKAHEAD_OR_NONCAUSAL_QUOTE"


def test_backtest_rejects_out_of_order_execution_observations():
    rows = [observation(1, 1), observation(0, 0)]
    result = BacktestEngine().run(rows)
    assert result["status"] == "NON_CHRONOLOGICAL_EXECUTION_DATA"


def test_backtest_rejects_nonfinite_parameters():
    result = BacktestEngine().run([observation(0, 1)], taker_fee_bps=float("nan"))
    assert result["status"] == "INVALID_PARAMETERS"
