from __future__ import annotations

from app.config.adaptive import TACTICAL_DEFAULTS
from app.learning.optimizer import AdaptiveParameterOptimizer
from app.learning.registry import ModelRegistry


def _trade(direction: str, path: list[dict], close: float = 1_040.0) -> dict:
    return {
        "symbol": "BTC/USD",
        "opened_at": 1_000.0,
        "closed_at": close,
        "entry_price": 100.0,
        "direction": direction,
        "position_context": {"venue": "futures", "leverage": "1"},
        "cost_snapshot": {
            "entry_fee_bps": 0,
            "exit_fee_bps": 0,
            "max_spread_bps": 0,
            "expected_slippage_bps": 0,
            "safety_buffer_bps": 0,
            "margin_open_fee_bps": 0,
            "rollover_fee_bps": 0,
        },
        "path": path,
    }


def _point(
    t: float,
    price: float,
    *,
    peak: float | None = None,
    trough: float | None = None,
    state: str = "OPEN",
    signal_direction: str = "",
    signal_net_edge_bps: float = 0.0,
) -> dict:
    return {
        "observed_at": t,
        "price": str(price),
        "peak_price": str(peak if peak is not None else price),
        "trough_price": str(trough if trough is not None else price),
        "state": state,
        "signal_direction": signal_direction,
        "signal_net_edge_bps": str(signal_net_edge_bps),
    }


def _policy(**overrides) -> dict:
    result = {
        "tactical_stop_loss_pct": 1.0,
        "tactical_take_profit_pct": 4.0,
        "tactical_trailing_trigger_bps": 500.0,
        "tactical_trailing_stop_pct": 0.7,
        "tactical_max_hold_seconds": 300,
        "tactical_reversal_exit_bps": 120.0,
    }
    result.update(overrides)
    return result


def test_exit_replay_detects_long_stop_loss_directionally():
    trade = _trade("LONG", [
        _point(1_000, 100.0),
        _point(1_010, 100.5, peak=100.5, trough=100.0),
        _point(1_020, 98.8, peak=100.5, trough=98.8),
    ], close=1_020)
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(trade, _policy())
    assert result is not None
    assert result["reason"] == "STOP_LOSS"
    assert result["exit_at"] == 1_020
    assert result["net_bps"] < 0


def test_exit_replay_detects_short_stop_loss_directionally():
    trade = _trade("SHORT", [
        _point(1_000, 100.0),
        _point(1_010, 99.5, peak=100.0, trough=99.5),
        _point(1_020, 101.2, peak=101.2, trough=99.5),
    ], close=1_020)
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(trade, _policy())
    assert result is not None
    assert result["reason"] == "STOP_LOSS"
    assert result["exit_at"] == 1_020
    assert result["net_bps"] < 0


def test_exit_replay_uses_maximum_hold_for_non_trailing_positions():
    trade = _trade("LONG", [
        _point(1_000, 100.0),
        _point(1_020, 100.1, peak=100.1, trough=100.0),
        _point(1_040, 100.2, peak=100.2, trough=100.0),
    ])
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(
        trade, _policy(tactical_max_hold_seconds=30)
    )
    assert result is not None
    assert result["reason"] == "TIME_STOP"
    assert result["exit_at"] == 1_040


def test_exit_replay_restores_a_trailing_position_baseline():
    trade = _trade("LONG", [
        _point(1_000, 101.0, peak=103.0, trough=100.5, state="TRAILING"),
        _point(1_010, 101.0, peak=103.0, trough=100.5, state="TRAILING"),
    ], close=1_010)
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(
        trade,
        _policy(
            tactical_stop_loss_pct=2.0,
            tactical_take_profit_pct=4.0,
            tactical_trailing_trigger_bps=50.0,
            tactical_trailing_stop_pct=0.7,
        ),
    )
    assert result is not None
    assert result["reason"] == "TRAILING_STOP"


def test_exit_replay_triggers_on_opposite_signal_at_the_recorded_edge():
    trade = _trade("LONG", [
        _point(1_000, 100.0, signal_direction="LONG", signal_net_edge_bps=900),
        _point(1_010, 100.5, peak=100.5, signal_direction="SHORT", signal_net_edge_bps=150),
        _point(1_020, 100.6, peak=100.6, signal_direction="SHORT", signal_net_edge_bps=200),
    ], close=1_040)
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(trade, _policy())
    assert result is not None
    assert result["reason"] == "SIGNAL_REVERSAL"
    assert result["exit_at"] == 1_010


def test_exit_replay_ignores_same_direction_or_below_threshold_signals():
    same_direction = _trade("LONG", [
        _point(1_000, 100.0, signal_direction="LONG", signal_net_edge_bps=900),
        _point(1_010, 100.5, peak=100.5, signal_direction="LONG", signal_net_edge_bps=900),
        _point(1_020, 100.6, peak=100.6, signal_direction="LONG", signal_net_edge_bps=900),
    ], close=1_040)
    below_threshold = _trade("LONG", [
        _point(1_000, 100.0, signal_direction="LONG", signal_net_edge_bps=900),
        _point(1_010, 100.5, peak=100.5, signal_direction="SHORT", signal_net_edge_bps=119.99),
        _point(1_020, 100.6, peak=100.6, signal_direction="SHORT", signal_net_edge_bps=100),
    ], close=1_040)
    assert AdaptiveParameterOptimizer._simulate_tactical_exit(same_direction, _policy()) is None
    assert AdaptiveParameterOptimizer._simulate_tactical_exit(below_threshold, _policy()) is None


def test_reversal_threshold_changes_counterfactual_exit_time():
    trade = _trade("LONG", [
        _point(1_000, 100.0, signal_direction="LONG", signal_net_edge_bps=900),
        _point(1_010, 100.5, peak=100.5, signal_direction="SHORT", signal_net_edge_bps=150),
        _point(1_020, 100.6, peak=100.6, signal_direction="SHORT", signal_net_edge_bps=200),
    ], close=1_040)
    early = AdaptiveParameterOptimizer._simulate_tactical_exit(
        trade, _policy(tactical_reversal_exit_bps=120),
    )
    later = AdaptiveParameterOptimizer._simulate_tactical_exit(
        trade, _policy(tactical_reversal_exit_bps=180),
    )
    assert early is not None and later is not None
    assert early["exit_at"] == 1_010
    assert later["exit_at"] == 1_020


def test_exit_optimizer_waits_for_sufficient_realized_paths(db):
    registry = ModelRegistry(db)
    optimizer = AdaptiveParameterOptimizer(db, registry)
    result = optimizer.optimize_tactical_exits(now=1_800_000_000.0)
    assert result["status"] == "INSUFFICIENT_VALIDATED_EXIT_PATHS"
    assert result["samples"] == 0


def test_registry_rejects_exit_candidate_without_holdout_quality(db):
    registry = ModelRegistry(db)
    params = dict(TACTICAL_DEFAULTS)
    registry.register_candidate(
        "exit-policy-bad",
        "strategy_tactical",
        registry.active("strategy_tactical"),
        params,
        {
            "kind": "path_replay_exit_policy",
            "samples": 60,
            "training_samples": 42,
            "validation_samples": 18,
            "training_coverage": 0.95,
            "validation_coverage": 0.95,
            "training_improvement_bps": 12.0,
            "validation_improvement_bps": -2.0,
            "candidate_validation_mean_net_bps": 0.5,
            "baseline_validation_max_drawdown": 0.05,
            "candidate_validation_max_drawdown": 0.05,
        },
    )
    assert registry.promote_adaptive_policy("exit-policy-bad") is False


def test_registry_rejects_tactical_reversal_threshold_outside_hard_bounds(db):
    registry = ModelRegistry(db)
    params = dict(TACTICAL_DEFAULTS)
    params["tactical_reversal_exit_bps"] = 500.01
    registry.register_candidate(
        "exit-policy-reversal-out-of-bounds",
        "strategy_tactical",
        registry.active("strategy_tactical"),
        params,
        {
            "kind": "path_replay_exit_policy",
            "samples": 60,
            "training_samples": 42,
            "validation_samples": 18,
            "training_coverage": 0.90,
            "validation_coverage": 0.90,
            "training_improvement_bps": 12.0,
            "validation_improvement_bps": 7.0,
            "candidate_validation_mean_net_bps": 8.0,
            "baseline_validation_max_drawdown": 0.05,
            "candidate_validation_max_drawdown": 0.055,
        },
    )
    assert registry.promote_adaptive_policy("exit-policy-reversal-out-of-bounds") is False
    assert registry.active("strategy_tactical") != "exit-policy-reversal-out-of-bounds"


def test_registry_rejects_invalid_tactical_volatility_band(db):
    registry = ModelRegistry(db)
    params = dict(TACTICAL_DEFAULTS)
    # Both values are individually within their bounds, but the band is inverted.
    params["tactical_min_volatility_bps"] = 50.0
    params["tactical_max_volatility_bps"] = 25.0
    registry.register_candidate(
        "exit-policy-invalid-volatility-band",
        "strategy_tactical",
        registry.active("strategy_tactical"),
        params,
        {"kind": "bounded_online_controller", "samples": 60},
    )
    assert registry.promote_adaptive_policy("exit-policy-invalid-volatility-band") is False
    assert registry.active("strategy_tactical") != "exit-policy-invalid-volatility-band"


def test_registry_rejects_tactical_candidate_changing_immutable_position_cap(db):
    registry = ModelRegistry(db)
    params = dict(TACTICAL_DEFAULTS)
    params["tactical_position_limit_pct"] = 81.0
    registry.register_candidate(
        "policy-changed-position-cap",
        "strategy_tactical",
        registry.active("strategy_tactical"),
        params,
        {"kind": "bounded_online_controller", "samples": 60},
    )
    assert registry.promote_adaptive_policy("policy-changed-position-cap") is False
    assert registry.active("strategy_tactical") != "policy-changed-position-cap"


def test_registry_promotes_exit_candidate_only_with_validated_holdout(db):
    registry = ModelRegistry(db)
    params = dict(TACTICAL_DEFAULTS)
    params.update({
        "tactical_stop_loss_pct": 0.9,
        "tactical_take_profit_pct": 2.4,
        "tactical_trailing_trigger_bps": 125.0,
        "tactical_trailing_stop_pct": 0.6,
        "tactical_max_hold_seconds": 1500,
    })
    registry.register_candidate(
        "exit-policy-good",
        "strategy_tactical",
        registry.active("strategy_tactical"),
        params,
        {
            "kind": "path_replay_exit_policy",
            "samples": 60,
            "training_samples": 42,
            "validation_samples": 18,
            "training_coverage": 0.90,
            "validation_coverage": 0.90,
            "training_improvement_bps": 12.0,
            "validation_improvement_bps": 7.0,
            "candidate_validation_mean_net_bps": 8.0,
            "baseline_validation_max_drawdown": 0.05,
            "candidate_validation_max_drawdown": 0.055,
        },
    )
    assert registry.promote_adaptive_policy("exit-policy-good") is True
    assert registry.active("strategy_tactical") == "exit-policy-good"



def test_exit_replay_detects_short_stop_loss_at_exact_one_percent_price_rise():
    trade = _trade("SHORT", [
        _point(1_000, 100.0),
        _point(1_010, 101.0, peak=101.0, trough=100.0),
    ], close=1_010)
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(trade, _policy())
    assert result is not None
    assert result["reason"] == "STOP_LOSS"
    assert result["gross_bps"] == -100.0


def test_exit_replay_uses_live_cost_aware_trailing_trigger_and_max_hold():
    trade = _trade("LONG", [
        _point(1_000, 100.0),
        _point(1_301, 102.5, peak=102.5, trough=100.0),
    ], close=1_301)
    trade["cost_snapshot"].update({
        "entry_fee_bps": 80,
        "exit_fee_bps": 80,
        "max_spread_bps": 25,
        "expected_slippage_bps": 25,
        "safety_buffer_bps": 30,
        "margin_open_fee_bps": 0,
        "rollover_fee_bps": 0,
    })
    result = AdaptiveParameterOptimizer._simulate_tactical_exit(
        trade,
        _policy(
            tactical_take_profit_pct=2.2,
            tactical_trailing_trigger_bps=100,
            tactical_trailing_stop_pct=0.7,
            tactical_max_hold_seconds=300,
        ),
    )
    # The peak is +2.5%, below the cost-floor-plus-trailing-distance trigger
    # of +3.35%; it must not arm trailing and suppress the 300-second time stop.
    assert result is not None
    assert result["reason"] == "TIME_STOP"
    assert result["exit_at"] == 1_301
