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


def _point(t: float, price: float, *, peak: float | None = None,
           trough: float | None = None, state: str = "OPEN") -> dict:
    return {
        "observed_at": t,
        "price": str(price),
        "peak_price": str(peak if peak is not None else price),
        "trough_price": str(trough if trough is not None else price),
        "state": state,
    }


def _policy(**overrides) -> dict:
    result = {
        "tactical_stop_loss_pct": 1.0,
        "tactical_take_profit_pct": 4.0,
        "tactical_trailing_trigger_bps": 500.0,
        "tactical_trailing_stop_pct": 0.7,
        "tactical_max_hold_seconds": 300,
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
    # The restored peak is above the entry, so a drop below the stored trail
    # must be evaluated against that peak rather than a fabricated fresh entry.
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
