from __future__ import annotations

import json

from app.config.adaptive import CORE_DEFAULTS
from app.learning.core_exit_optimizer import CoreExitPolicyOptimizer
from app.learning.registry import ModelRegistry


EXIT_POLICY = {
    key: CORE_DEFAULTS[key]
    for key in (
        "strategy_stop_loss_pct",
        "strategy_partial_profit_trigger_pct",
        "strategy_partial_profit_fraction_pct",
        "strategy_profit_lock_trigger_pct",
        "strategy_profit_giveback_pct",
        "strategy_profit_lock_floor_pct",
    )
}


def _add_core_exit_episode(db, index: int, now: float) -> None:
    opened_at = now - (120 - index) * 3600.0
    closed_at = opened_at + 380.0
    symbol = f"ASSET{index}/EUR"
    decision_id = f"core-exit-decision-{index}"
    episode_id = f"core-exit-episode-{index}"
    order_id = f"core-exit-order-{index}"

    db.execute(
        """INSERT INTO decisions(
             decision_id,created_at,symbol,direction,target_notional_eur,leverage,
             expected_return_bps,expected_cost_bps,confidence,regime,news_effect_bps,
             gemini_effect_bps,strategy_version,model_version,config_hash,rationale_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, opened_at - 1, symbol, "LONG", "100", "1", "250",
            "100", "0.8", "TRENDING", "0", "0", "core-v2-profit-protection",
            "baseline-v1", "test", "{}",
        ),
    )
    db.create_core_exit_episode(
        episode_id=episode_id,
        symbol=symbol,
        direction="LONG",
        opened_at=opened_at,
        observed_at=opened_at,
        order_ids=[order_id],
        decision_ids=[decision_id],
        open_lot_count=1,
        basis_eur="100",
        first_profit_pct="0",
        peak_profit_pct="0",
        eligible_for_learning=True,
        eligibility_reason="APP_MANAGED_SINGLE_ORDER_CAPTURED_FROM_ENTRY_CYCLE",
        exit_policy_snapshot=EXIT_POLICY,
        detail={"test_fixture": True},
    )

    # The observed position path reaches +12%, then fades through the original
    # 35% giveback threshold. Each point is a real, chronological observation;
    # the final +7% point corresponds to the recorded trailing-profit exit.
    profits = [0.0, 5.0, 10.0, 12.0, 9.0, 8.0, 7.0]
    peak = 0.0
    for point_index, profit in enumerate(profits):
        peak = max(peak, profit)
        observed_at = opened_at + point_index * 60.0
        quality = {
            "open_positions_read_ok": True,
            "pnl_present": True,
            "basis_positive": True,
            "quantity_positive": True,
            "direction": "LONG",
            "open_order_ids": [order_id],
            "open_lot_count": 1,
            "eligible_for_learning": True,
            "eligibility_reason": "APP_MANAGED_SINGLE_ORDER_CAPTURED_FROM_ENTRY_CYCLE",
        }
        db.record_core_exit_observation(
            episode_id=episode_id,
            cycle_id=f"core-exit-cycle-{index}-{point_index}",
            observed_at=observed_at,
            profit_pct=str(profit),
            peak_profit_pct=str(peak),
            basis_eur="100",
            quantity="1",
            position_pnl_eur=str(profit),
            partial_taken=point_index >= 3,
            pending_order_id="",
            policy_snapshot=EXIT_POLICY,
            quality=quality,
        )

    db.close_core_exit_episode(episode_id, closed_at, "TRAILING_PROFIT_EXIT")


def test_core_exit_optimizer_promotes_only_a_validated_path_policy(db):
    now = 1_800_000_000.0
    for index in range(60):
        _add_core_exit_episode(db, index, now)
    registry = ModelRegistry(db)
    optimizer = CoreExitPolicyOptimizer(db, registry)
    
    result = optimizer.optimize(now)

    assert result["status"] == "PROMOTED", result
    assert result["promoted"] is True
    assert result["metrics"]["training_samples"] == 42
    assert result["metrics"]["validation_samples"] == 18
    assert result["metrics"]["training_coverage"] >= 0.85
    assert result["metrics"]["validation_coverage"] >= 0.85
    assert result["metrics"]["training_improvement_bps"] >= 10.0
    assert result["metrics"]["validation_improvement_bps"] >= 5.0

    active_version = registry.active("strategy_core")
    assert active_version.startswith("core-exit-policy-")
    parameters = registry.parameters(active_version, family="strategy_core")
    assert parameters["strategy_profit_giveback_pct"] < EXIT_POLICY["strategy_profit_giveback_pct"]
    assert parameters["strategy_stop_loss_pct"] == EXIT_POLICY["strategy_stop_loss_pct"]


def test_core_exit_optimizer_does_not_promote_without_minimum_episode_count(db):
    now = 1_800_000_000.0
    for index in range(59):
        _add_core_exit_episode(db, index, now)
    registry = ModelRegistry(db)

    result = CoreExitPolicyOptimizer(db, registry).optimize(now)

    assert result["status"] == "INSUFFICIENT_BASELINE_ALIGNED_CORE_EXIT_PATHS"
    assert result["samples"] == 59
    assert registry.active("strategy_core") == "core-entry-baseline-v1"


def test_core_exit_optimizer_rejects_path_points_with_unresolved_quality_flags(db):
    now = 1_800_000_000.0
    _add_core_exit_episode(db, 0, now)
    episode_id = "core-exit-episode-0"
    db.execute(
        """UPDATE core_exit_path_points SET quality_json=?
           WHERE episode_id=? AND cycle_id=?""",
        (
            json.dumps({"open_positions_read_ok": False}),
            episode_id,
            "core-exit-cycle-0-2",
        ),
    )
    optimizer = CoreExitPolicyOptimizer(db, ModelRegistry(db))

    episodes, raw_count = optimizer._load_episodes(now)

    assert raw_count == 1
    assert episodes == []
