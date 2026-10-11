from __future__ import annotations

import json
from types import SimpleNamespace
from decimal import Decimal

from app.config.adaptive import AdaptiveConfig
from app.config.settings import Config
from app.learning.optimizer import AdaptiveParameterOptimizer
from app.learning.registry import ModelRegistry
from app.trading.decision import DecisionEngine


class FakeRegistry:
    def active(self, family="decision"):
        return "policy-v2" if family == "strategy_tactical" else "baseline-v1"

    def parameters(self, version=None, family="decision"):
        if family == "strategy_core":
            return {
                "strategy_min_edge_bps": 33.0,
                "strategy_stop_loss_pct": 2.5,
                "strategy_partial_profit_trigger_pct": 8.0,
                "strategy_partial_profit_fraction_pct": 40.0,
                "strategy_profit_lock_trigger_pct": 12.0,
                "strategy_profit_giveback_pct": 28.0,
                "strategy_profit_lock_floor_pct": 4.0,
            }
        if family == "strategy_tactical":
            return {
                "tactical_min_expected_move_bps": 350,
                "tactical_min_momentum_bps": 60,
                "tactical_portfolio_pct": 20,
                "tactical_stop_loss_pct": 1.25,
                "tactical_take_profit_pct": 3.0,
                "tactical_trailing_trigger_bps": 150,
                "tactical_trailing_stop_pct": 0.55,
                "tactical_max_hold_seconds": 900,
                "tactical_min_volatility_bps": 15.0,
                "tactical_max_volatility_bps": 70.0,
                "tactical_min_imbalance": 0.15,
                "tactical_adaptive_min_confidence": 0.82,
                "tactical_adaptive_min_net_edge_bps": 20.0,
            }
        return {}


def test_adaptive_config_overlays_only_managed_policy_parameters():
    base = SimpleNamespace(
        strategy_min_edge_bps=33.0,
        tactical_min_expected_move_bps=280.0,
        tactical_min_momentum_bps=40.0,
        tactical_portfolio_pct=25.0,
        tactical_stop_loss_pct=1.0,
        tactical_take_profit_pct=2.2,
        tactical_trailing_trigger_bps=100.0,
        tactical_trailing_stop_pct=0.7,
        tactical_max_hold_seconds=1800,
    )
    config = AdaptiveConfig(base, FakeRegistry())
    assert config.tactical_min_expected_move_bps == 350.0
    assert config.tactical_min_momentum_bps == 60.0
    assert config.tactical_portfolio_pct == 20.0
    assert config.tactical_stop_loss_pct == 1.25
    assert config.tactical_take_profit_pct == 3.0
    assert config.tactical_trailing_trigger_bps == 150
    assert config.tactical_trailing_stop_pct == 0.55
    assert config.tactical_max_hold_seconds == 900
    assert config.tactical_min_volatility_bps == 15.0
    assert config.tactical_max_volatility_bps == 70.0
    assert config.tactical_min_imbalance == 0.15
    assert config.tactical_adaptive_min_confidence == 0.82
    assert config.tactical_adaptive_min_net_edge_bps == 20.0
    # Core policy values are supplied by its own versioned model; unrelated settings remain base-config owned.
    assert config.strategy_min_edge_bps == 33.0
    assert config.strategy_stop_loss_pct == 2.5
    assert config.strategy_partial_profit_trigger_pct == 8.0
    assert config.strategy_partial_profit_fraction_pct == 40.0
    assert config.strategy_profit_lock_trigger_pct == 12.0
    assert config.strategy_profit_giveback_pct == 28.0
    assert config.strategy_profit_lock_floor_pct == 4.0
    assert config.as_dict()["strategy_min_edge_bps"] == 33.0



def test_decision_engine_uses_regime_specific_confidence_scale():
    engine = DecisionEngine(SimpleNamespace())
    params = {
        "confidence_scale": 1.1,
        "confidence_scale_by_regime": {"TRENDING": 0.8, "RANGING": 1.2},
    }
    assert engine._confidence_scale(params, "TRENDING") == Decimal("0.8")
    assert engine._confidence_scale(params, "RANGING") == Decimal("1.2")
    assert engine._confidence_scale(params, "UNKNOWN") == Decimal("1.1")

def test_legacy_ha_options_cannot_override_learned_parameters(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({
        "tactical_min_expected_move_bps": 9999,
        "tactical_min_momentum_bps": 9999,
        "tactical_min_volume_ratio": 9999,
        "tactical_min_breakout_bps": 9999,
        "tactical_max_spread_bps": 0.1,
        "tactical_adaptive_min_expected_move_bps": 9999,
        "tactical_portfolio_pct": 99,
        "tactical_stop_loss_pct": 99,
        "tactical_take_profit_pct": 0.01,
        "tactical_trailing_trigger_bps": 99999,
        "tactical_trailing_stop_pct": 99,
        "tactical_max_hold_seconds": 1,
        "tactical_min_volatility_bps": 999,
        "tactical_max_volatility_bps": 1,
        "tactical_min_imbalance": 0.99,
        "tactical_adaptive_min_confidence": 0.01,
        "tactical_adaptive_min_net_edge_bps": 999,
    }), encoding="utf-8")
    config = Config.load(str(path))
    assert config.tactical_min_expected_move_bps == 280.0
    assert config.tactical_min_momentum_bps == 40.0
    assert config.tactical_min_volume_ratio == 2.0
    assert config.tactical_min_breakout_bps == 25.0
    assert config.tactical_max_spread_bps == 25.0
    assert config.tactical_adaptive_min_expected_move_bps == 230.0
    assert config.tactical_portfolio_pct == 25.0
    assert config.tactical_stop_loss_pct == 1.0
    assert config.tactical_take_profit_pct == 2.2
    assert config.tactical_trailing_trigger_bps == 100.0
    assert config.tactical_trailing_stop_pct == 0.7
    assert config.tactical_max_hold_seconds == 1800
    assert config.tactical_min_volatility_bps == 12.0
    assert config.tactical_max_volatility_bps == 55.0
    assert config.tactical_min_imbalance == 0.10
    assert config.tactical_adaptive_min_confidence == 0.75
    assert config.tactical_adaptive_min_net_edge_bps == 15.0


def test_optimizer_promotes_bounded_policy_from_realized_loss_window(db):
    registry = ModelRegistry(db)
    now = 1_800_000_000.0
    for index in range(60):
        closed = now - (60 - index) * 3600
        db.execute(
            """INSERT INTO tactical_trades(
                trade_id,symbol,direction,entry_price,exit_price,quantity,gross_pnl_eur,
                fees_eur,net_pnl_eur,opened_at,closed_at,hold_seconds,exit_reason,
                setup_score,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"loss-{index}", "BTC/USD", "LONG", "100", "99", "1",
                "-1", "0.1", "-1.1", closed - 300, closed, 300,
                "STOP_LOSS", "0.8", "{}",
            ),
        )
    optimizer = AdaptiveParameterOptimizer(db, registry)
    result = optimizer.optimize_tactical(now)
    assert result["status"] == "PROMOTED"
    assert result["promoted"] is True
    assert result["changed_parameters"]["tactical_min_expected_move_bps"] > 280
    assert result["changed_parameters"]["tactical_portfolio_pct"] < 25
    assert result["changed_parameters"]["tactical_min_volatility_bps"] > 12
    assert result["changed_parameters"]["tactical_max_volatility_bps"] < 55
    assert result["changed_parameters"]["tactical_min_imbalance"] > 0.10
    assert result["changed_parameters"]["tactical_adaptive_min_confidence"] > 0.75
    assert result["changed_parameters"]["tactical_adaptive_min_net_edge_bps"] > 15
    active = registry.parameters(registry.active("strategy_tactical"), family="strategy_tactical")
    assert active["tactical_min_expected_move_bps"] <= 1200
    assert 1 <= active["tactical_portfolio_pct"] <= 25
    assert active["tactical_min_volatility_bps"] < active["tactical_max_volatility_bps"]
    repeated = optimizer.optimize_tactical(now + 1)
    assert repeated["status"] == "WAITING_FOR_NEW_TRADE_DATA"
    assert repeated["new_trades_since_last_evaluation"] == 0


class FakeCoreRegistry:
    def active(self, family="decision"):
        return "core-policy-v1" if family == "strategy_core" else "baseline-v1"

    def parameters(self, version=None, family="decision"):
        if family == "strategy_core":
            return {
                "strategy_min_edge_bps": 60.0,
                "strategy_min_confidence": 0.70,
                "strategy_adaptive_edge_floor_bps": 99.0,
                "strategy_adaptive_min_confidence": 0.60,
                "strategy_adaptive_cost_ratio": 0.10,
            }
        return {}


def test_adaptive_config_clamps_core_entry_policy_and_preserves_gate_ordering():
    base = SimpleNamespace(
        strategy_min_edge_bps=25.0,
        strategy_min_confidence=0.58,
        strategy_adaptive_edge_floor_bps=15.0,
        strategy_adaptive_min_confidence=0.75,
        strategy_adaptive_cost_ratio=1.10,
    )
    config = AdaptiveConfig(base, FakeCoreRegistry())
    assert config.strategy_min_edge_bps == 60.0
    assert config.strategy_min_confidence == 0.70
    assert config.strategy_adaptive_edge_floor_bps == 25.0
    assert config.strategy_adaptive_min_confidence == 0.75
    assert config.strategy_adaptive_cost_ratio == 1.10


def test_legacy_ha_options_cannot_override_learned_core_parameters(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({
        "strategy_min_edge_bps": 79.0,
        "strategy_min_confidence": 0.84,
        "strategy_adaptive_edge_floor_bps": 24.0,
        "strategy_adaptive_min_confidence": 0.94,
        "strategy_adaptive_cost_ratio": 1.49,
        "strategy_stop_loss_pct": 3.9,
        "strategy_partial_profit_trigger_pct": 20.0,
        "strategy_partial_profit_fraction_pct": 70.0,
        "strategy_profit_lock_trigger_pct": 22.0,
        "strategy_profit_giveback_pct": 55.0,
        "strategy_profit_lock_floor_pct": 11.0,
    }), encoding="utf-8")
    config = Config.load(str(path))
    assert config.strategy_min_edge_bps == 25.0
    assert config.strategy_min_confidence == 0.58
    assert config.strategy_adaptive_edge_floor_bps == 15.0
    assert config.strategy_adaptive_min_confidence == 0.75
    assert config.strategy_adaptive_cost_ratio == 1.10
    assert config.strategy_stop_loss_pct == 2.0
    assert config.strategy_partial_profit_trigger_pct == 10.0
    assert config.strategy_partial_profit_fraction_pct == 50.0
    assert config.strategy_profit_lock_trigger_pct == 10.0
    assert config.strategy_profit_giveback_pct == 35.0
    assert config.strategy_profit_lock_floor_pct == 5.0


def test_core_optimizer_tightens_entry_policy_from_verified_net_outcomes(db):
    registry = ModelRegistry(db)
    optimizer = AdaptiveParameterOptimizer(db, registry)
    now = 1_800_000_000.0

    for index in range(60):
        decision_id = f"core-decision-{index}"
        closed_at = now - (60 - index) * 3600.0
        db.execute(
            """INSERT INTO decisions(
                 decision_id,created_at,symbol,direction,target_notional_eur,leverage,
                 expected_return_bps,expected_cost_bps,confidence,regime,news_effect_bps,
                 gemini_effect_bps,strategy_version,model_version,config_hash,rationale_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, closed_at - 600, "XBT/EUR", "LONG", "100", "1",
                "150", "20", "0.70", "TRENDING", "0", "0",
                "core-v1", "baseline-v1", "test", "{}",
            ),
        )
        db.execute(
            """INSERT INTO core_realized_outcomes(
                 opening_decision_id,symbol,direction,first_opened_at,last_closed_at,
                 matched_legs,closed_quantity,closed_notional_quote,gross_pnl_quote,
                 fees_est_quote,estimated_net_pnl_quote,gross_return_bps,
                 estimated_net_return_bps,quote_asset,fee_status,net_verified,
                 verified_fees_quote,verified_net_pnl_quote,verified_net_return_bps,detail_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, "XBT/EUR", "LONG", closed_at - 600, closed_at, 1,
                "1", "100", "-0.5", "0.2", "-0.7", "-50",
                "-70", "ZEUR", "LEDGER_VERIFIED_SPOT_NET", 1,
                "0.2", "-0.7", "-70", "{}",
            ),
        )

    result = optimizer.optimize_core_entry_policy(now)
    assert result["status"] == "PROMOTED", result
    assert result["promoted"] is True
    assert result["action"] == "TIGHTEN"
    assert result["data_basis"] == "ledger_verified_non_margin_spot_net_return_bps"
    assert result["data_scope"] == "APP_MANAGED_NON_MARGIN_SPOT_VERIFIED_NET_ONLY"
    assert result["exchange_fee_estimates_used"] is False
    assert result["verified_net_pnl_used"] is True

    active_version = registry.active("strategy_core")
    assert active_version.startswith("core-entry-policy-")
    params = registry.parameters(active_version, family="strategy_core")
    assert params["strategy_min_edge_bps"] > 25.0
    assert params["strategy_min_confidence"] > 0.58
    assert params["strategy_adaptive_edge_floor_bps"] <= params["strategy_min_edge_bps"]
    assert params["strategy_adaptive_min_confidence"] >= params["strategy_min_confidence"]
    assert params["strategy_adaptive_cost_ratio"] >= 1.10

    repeated = optimizer.optimize_core_entry_policy(now + 1)
    assert repeated["status"] == "WAITING_FOR_NEW_CORE_OUTCOMES"
    assert repeated["new_closed_outcomes_since_last_evaluation"] == 0


def test_core_optimizer_ignores_estimated_only_spot_outcomes(db):
    registry = ModelRegistry(db)
    optimizer = AdaptiveParameterOptimizer(db, registry)
    now = 1_800_000_000.0

    for index in range(60):
        decision_id = f"estimated-core-decision-{index}"
        closed_at = now - (60 - index) * 3600.0
        db.execute(
            """INSERT INTO decisions(
                 decision_id,created_at,symbol,direction,target_notional_eur,leverage,
                 expected_return_bps,expected_cost_bps,confidence,regime,news_effect_bps,
                 gemini_effect_bps,strategy_version,model_version,config_hash,rationale_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, closed_at - 600, "XBT/EUR", "LONG", "100", "1",
                "150", "20", "0.70", "TRENDING", "0", "0",
                "core-v1", "baseline-v1", "test", "{}",
            ),
        )
        db.execute(
            """INSERT INTO core_realized_outcomes(
                 opening_decision_id,symbol,direction,first_opened_at,last_closed_at,
                 matched_legs,closed_quantity,closed_notional_quote,gross_pnl_quote,
                 fees_est_quote,estimated_net_pnl_quote,gross_return_bps,
                 estimated_net_return_bps,quote_asset,fee_status,net_verified,
                 verified_fees_quote,verified_net_pnl_quote,verified_net_return_bps,detail_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, "XBT/EUR", "LONG", closed_at - 600, closed_at, 1,
                "1", "100", "-0.5", "0.2", "-0.7", "-50",
                "-70", "ZEUR", "QUOTE_ESTIMATE_ONLY", 0,
                None, None, None, "{}",
            ),
        )

    result = optimizer.optimize_core_entry_policy(now)
    assert result["status"] == "INSUFFICIENT_VERIFIED_CLOSED_SPOT_OUTCOMES"
    assert result["samples"] == 0
    assert registry.active("strategy_core") == "core-entry-baseline-v1"


def test_registry_rejects_legacy_estimated_core_learning_candidate(db):
    registry = ModelRegistry(db)
    active_before = registry.active("strategy_core")
    registry.register_candidate(
        "core-policy-legacy-estimated-outcomes",
        "strategy_core",
        active_before,
        dict(CORE_DEFAULTS),
        {
            "kind": "bounded_core_outcome_controller",
            "samples": 60,
            "previous_samples": 30,
            "recent_samples": 30,
            "action": "TIGHTEN",
            "previous_mean_after_expected_cost_bps": -30.0,
            "recent_mean_after_expected_cost_bps": -70.0,
            "previous_positive_after_expected_cost_rate": 0.0,
            "recent_positive_after_expected_cost_rate": 0.0,
            "data_basis": "gross_return_bps_minus_decision_expected_cost_bps",
            "exchange_fee_estimates_used": False,
            "verified_net_pnl_used": False,
            "data_scope": "APP_MANAGED_SPOT_MARGIN_ONLY",
        },
    )

    assert registry.promote_adaptive_policy(
        "core-policy-legacy-estimated-outcomes", minimum_samples=60
    ) is False
    assert registry.active("strategy_core") == active_before

def test_adaptive_config_restores_safe_tactical_volatility_band_when_registry_is_invalid():
    class InvalidVolatilityRegistry:
        def active(self, family="decision"):
            return "invalid-policy"

        def parameters(self, version=None, family="decision"):
            if family == "strategy_tactical":
                return {
                    "tactical_min_volatility_bps": 80.0,
                    "tactical_max_volatility_bps": 20.0,
                }
            return {}

    config = AdaptiveConfig(SimpleNamespace(), InvalidVolatilityRegistry())

    assert config.tactical_min_volatility_bps == 12.0
    assert config.tactical_max_volatility_bps == 55.0

