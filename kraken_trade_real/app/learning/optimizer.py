from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.config.adaptive import TACTICAL_DEFAULTS


class AdaptiveParameterOptimizer:
    """Conservative online policy adaptation from realized Tactical trade outcomes.

    This is a bounded controller, not a claim of counterfactual backtesting. It
    only changes entry selectivity and allocation within a fixed safety envelope.
    Exit/stop parameters remain at their validated defaults until the DB records
    per-trade adverse/favourable excursion paths needed to evaluate them honestly.
    """

    MIN_TRADES = 60
    WINDOW = 30

    def __init__(self, db: Any, registry: Any) -> None:
        self.db = db
        self.registry = registry

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def optimize_tactical(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        rows = self.db.query(
            """SELECT trade_id,closed_at,net_pnl_eur,fees_eur,exit_reason,detail_json
               FROM tactical_trades
               WHERE closed_at >= ?
               ORDER BY closed_at DESC LIMIT 600""",
            (now - 180 * 86400,),
        )
        rows.reverse()
        if len(rows) < self.MIN_TRADES:
            return {
                "status": "INSUFFICIENT_REALIZED_TRADES",
                "samples": len(rows),
                "minimum_samples": self.MIN_TRADES,
            }

        recent = rows[-self.WINDOW:]
        previous = rows[-2 * self.WINDOW:-self.WINDOW]
        if len(previous) < self.WINDOW:
            return {
                "status": "INSUFFICIENT_COMPARISON_WINDOW",
                "samples": len(rows),
                "validation_samples": len(recent),
            }

        def stats(items: list[dict[str, Any]]) -> dict[str, float]:
            pnl = [self._number(row.get("net_pnl_eur")) for row in items]
            return {
                "samples": float(len(pnl)),
                "net_pnl_eur": sum(pnl),
                "mean_net_pnl_eur": sum(pnl) / max(1, len(pnl)),
                "win_rate": sum(1 for value in pnl if value > 0) / max(1, len(pnl)),
                "loss_rate": sum(1 for value in pnl if value < 0) / max(1, len(pnl)),
            }

        before = stats(previous)
        after = stats(recent)
        active_version = self.registry.active("strategy_tactical")
        current = dict(TACTICAL_DEFAULTS)
        current.update(self.registry.parameters(active_version, family="strategy_tactical"))
        candidate = dict(current)
        reason = ""

        # Use the latest completed window as a stability signal. A poor window
        # makes entries more selective and reduces allocation; a good, improving
        # window only cautiously relaxes entry filters. Exposure limits never rise.
        if after["mean_net_pnl_eur"] < 0 and after["win_rate"] < 0.50:
            candidate["tactical_min_expected_move_bps"] = min(
                1200.0, float(current["tactical_min_expected_move_bps"]) * 1.08
            )
            candidate["tactical_min_momentum_bps"] = min(
                300.0, float(current["tactical_min_momentum_bps"]) * 1.05
            )
            candidate["tactical_min_volume_ratio"] = min(
                8.0, float(current["tactical_min_volume_ratio"]) * 1.05
            )
            candidate["tactical_min_breakout_bps"] = min(
                250.0, float(current["tactical_min_breakout_bps"]) * 1.05
            )
            candidate["tactical_max_spread_bps"] = max(
                3.0, float(current["tactical_max_spread_bps"]) * 0.95
            )
            candidate["tactical_adaptive_min_expected_move_bps"] = min(
                800.0, float(current["tactical_adaptive_min_expected_move_bps"]) * 1.05
            )
            candidate["tactical_portfolio_pct"] = max(
                1.0, float(current["tactical_portfolio_pct"]) * 0.90
            )
            reason = "NEGATIVE_REALIZED_EXPECTANCY_TIGHTEN_ENTRIES"
        elif (
            after["mean_net_pnl_eur"] > 0
            and after["win_rate"] >= 0.55
            and after["mean_net_pnl_eur"] > before["mean_net_pnl_eur"]
        ):
            candidate["tactical_min_expected_move_bps"] = max(
                80.0, float(current["tactical_min_expected_move_bps"]) * 0.97
            )
            candidate["tactical_min_momentum_bps"] = max(
                10.0, float(current["tactical_min_momentum_bps"]) * 0.98
            )
            candidate["tactical_min_volume_ratio"] = max(
                1.0, float(current["tactical_min_volume_ratio"]) * 0.98
            )
            candidate["tactical_min_breakout_bps"] = max(
                5.0, float(current["tactical_min_breakout_bps"]) * 0.98
            )
            candidate["tactical_max_spread_bps"] = min(
                60.0, float(current["tactical_max_spread_bps"]) * 1.02
            )
            candidate["tactical_adaptive_min_expected_move_bps"] = max(
                80.0, float(current["tactical_adaptive_min_expected_move_bps"]) * 0.98
            )
            reason = "POSITIVE_IMPROVING_EXPECTANCY_SMALL_ENTRY_RELAXATION"

        # Avoid churn and duplicate versions when the measured policy does not change.
        changed = {
            key: value for key, value in candidate.items()
            if key in TACTICAL_DEFAULTS and value != current.get(key)
        }
        if not changed:
            result = {
                "status": "NO_SAFE_ADJUSTMENT",
                "samples": len(rows),
                "previous": before,
                "recent": after,
                "active_version": active_version,
            }
            self.db.learning_event("ADAPTIVE_POLICY_EVALUATED", active_version, result)
            return result

        candidate_id = hashlib.sha256(json.dumps({
            "parent": active_version,
            "candidate": candidate,
            "last_trade": rows[-1].get("trade_id"),
            "sample_count": len(rows),
        }, sort_keys=True, default=str).encode()).hexdigest()[:14]
        version = f"tactical-policy-{candidate_id}"
        metrics = {
            "kind": "bounded_online_controller",
            "samples": len(rows),
            "training_samples": len(rows) - self.WINDOW,
            "validation_samples": self.WINDOW,
            "previous_window": before,
            "recent_window": after,
            "reason": reason,
            "changed_parameters": changed,
            "not_counterfactual_backtest": True,
        }
        existing = self.db.one(
            "SELECT status FROM model_versions WHERE version=?", (version,)
        )
        promoted = False
        if existing is None:
            self.registry.register_candidate(
                version, "strategy_tactical", active_version, candidate, metrics
            )
            promoted = self.registry.promote_adaptive_policy(
                version, minimum_samples=self.MIN_TRADES
            )
        result = {
            "status": "PROMOTED" if promoted else "CANDIDATE_NOT_PROMOTED",
            "samples": len(rows),
            "previous": before,
            "recent": after,
            "changed_parameters": changed,
            "reason": reason,
            "candidate_version": version,
            "promoted": promoted,
        }
        self.db.learning_event("ADAPTIVE_POLICY_EVALUATED", version, result)
        return result
