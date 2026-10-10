from __future__ import annotations

import json
from types import SimpleNamespace

from app.config.adaptive import AdaptiveConfig
from app.config.settings import Config
from app.learning.optimizer import AdaptiveParameterOptimizer
from app.learning.registry import ModelRegistry


class FakeRegistry:
    def active(self, family="decision"):
        return "policy-v2" if family == "strategy_tactical" else "baseline-v1"

    def parameters(self, version=None, family="decision"):
        if family == "strategy_tactical":
            return {
                "tactical_min_expected_move_bps": 350,
                "tactical_min_momentum_bps": 60,
                "tactical_portfolio_pct": 20,
            }
        return {}


def test_adaptive_config_overlays_only_managed_policy_parameters():
    base = SimpleNamespace(
        strategy_min_edge_bps=33.0,
        tactical_min_expected_move_bps=280.0,
        tactical_min_momentum_bps=40.0,
        tactical_portfolio_pct=25.0,
    )
    config = AdaptiveConfig(base, FakeRegistry())
    assert config.tactical_min_expected_move_bps == 350.0
    assert config.tactical_min_momentum_bps == 60.0
    assert config.tactical_portfolio_pct == 20.0
    # The adaptive overlay must not silently replace parameters it does not own.
    assert config.strategy_min_edge_bps == 33.0
    assert config.as_dict()["strategy_min_edge_bps"] == 33.0


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
    }), encoding="utf-8")
    config = Config.load(str(path))
    assert config.tactical_min_expected_move_bps == 280.0
    assert config.tactical_min_momentum_bps == 40.0
    assert config.tactical_min_volume_ratio == 2.0
    assert config.tactical_min_breakout_bps == 25.0
    assert config.tactical_max_spread_bps == 25.0
    assert config.tactical_adaptive_min_expected_move_bps == 230.0
    assert config.tactical_portfolio_pct == 25.0


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
    active = registry.parameters(registry.active("strategy_tactical"), family="strategy_tactical")
    assert active["tactical_min_expected_move_bps"] <= 1200
    assert active["tactical_portfolio_pct"] >= 1
