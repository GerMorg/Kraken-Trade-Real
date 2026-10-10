from __future__ import annotations

import json
import time
from typing import Any


class ModelRegistry:
    MAX_PROMOTED_BRIER = 0.25
    MAX_PROMOTED_ECE = 0.15

    def __init__(self, db: Any) -> None:
        self.db=db
        self.ensure()

    def ensure(self) -> None:
        # A crash during an older promotion/rollback must not leave the trader
        # without an active decision model or with multiple active versions.
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT version FROM model_versions WHERE family='decision' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO model_versions(version,family,status,created_at,parent_version,parameters_json,metrics_json,reason) VALUES(?,?,?,?,?,?,?,?)",
                    ("baseline-v1", "decision", "ACTIVE", time.time(), None,
                     json.dumps({"kind": "deterministic"}), json.dumps({}), "initial baseline"),
                )
            active_rows = con.execute(
                "SELECT version FROM model_versions WHERE family='decision' AND status='ACTIVE' ORDER BY created_at DESC"
            ).fetchall()
            if not active_rows:
                fallback = con.execute(
                    "SELECT version FROM model_versions WHERE family='decision' ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if fallback:
                    con.execute(
                        "UPDATE model_versions SET status='ACTIVE' WHERE version=?",
                        (fallback["version"],),
                    )
            elif len(active_rows) > 1:
                keep = active_rows[0]["version"]
                con.execute(
                    "UPDATE model_versions SET status='RETIRED' WHERE family='decision' AND status='ACTIVE' AND version<>?",
                    (keep,),
                )

            # Strategy policies own version histories. Give each a real baseline
            # in its own family so promotion/rollback can never point at a Decision
            # model from an unrelated family.
            for family, baseline_version in (
                ("strategy_core", "core-entry-baseline-v1"),
                ("strategy_tactical", "tactical-policy-baseline-v1"),
            ):
                active = con.execute(
                    "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' "
                    "ORDER BY created_at DESC",
                    (family,),
                ).fetchall()
                if not active:
                    baseline = con.execute(
                        "SELECT version FROM model_versions WHERE version=? AND family=?",
                        (baseline_version, family),
                    ).fetchone()
                    if baseline is None:
                        con.execute(
                            "INSERT INTO model_versions(version,family,status,created_at,parent_version,"
                            "parameters_json,metrics_json,reason) VALUES(?,?,?,?,?,?,?,?)",
                            (
                                baseline_version, family, "ACTIVE", time.time(), None,
                                json.dumps({}), json.dumps({"kind": "bounded_default_policy"}),
                                "initial strategy policy baseline",
                            ),
                        )
                    else:
                        con.execute(
                            "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=?",
                            (baseline_version, family),
                        )
                elif len(active) > 1:
                    keep = active[0]["version"]
                    con.execute(
                        "UPDATE model_versions SET status='RETIRED' "
                        "WHERE family=? AND status='ACTIVE' AND version<>?",
                        (family, keep),
                    )
            con.commit()

    def active(self, family: str="decision") -> str:
        if family == "decision":
            self.ensure()
        row = self.db.one(
            "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (family,),
        )
        return row["version"] if row else "baseline-v1"

    def parameters(self, version: str | None = None, family: str = "decision") -> dict:
        row = self.db.one(
            "SELECT parameters_json FROM model_versions WHERE version=?"
            if version else
            "SELECT parameters_json FROM model_versions WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (version,) if version else (family,),
        )
        if not row:
            return {}
        try:
            value = json.loads(row["parameters_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def register_candidate(self, version: str, family: str, parent: str, parameters: dict, metrics: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO model_versions(version,family,status,created_at,parent_version,parameters_json,metrics_json,reason) VALUES(?,?,?,?,?,?,?,?)",
            (version,family,"CANDIDATE",time.time(),parent,json.dumps(parameters,sort_keys=True),json.dumps(metrics,sort_keys=True),"research candidate"),
        )

    def promote(self, version: str, min_improvement: float=0.05, min_samples: int=100) -> bool:
        row=self.db.one("SELECT * FROM model_versions WHERE version=? AND status='CANDIDATE'",(version,))
        if not row:
            return False
        metrics=json.loads(row["metrics_json"])
        invalid_quality = (
            float(metrics.get("brier", 1.0)) > self.MAX_PROMOTED_BRIER
            or float(metrics.get("ece", 1.0)) > self.MAX_PROMOTED_ECE
        )
        if (
            int(metrics.get("samples",0)) < min_samples
            or float(metrics.get("improvement",0)) < min_improvement
            or invalid_quality
        ):
            self.db.learning_event(
                "MODEL_NOT_PROMOTED",
                version,
                {
                    **metrics,
                    "quality_gate": {
                        "max_brier": self.MAX_PROMOTED_BRIER,
                        "max_ece": self.MAX_PROMOTED_ECE,
                        "failed": invalid_quality,
                    },
                },
            )
            return False
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",
                (row["family"],),
            ).fetchone()
            if current and current["version"] == version:
                con.rollback()
                return True
            con.execute(
                "UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",
                (row["family"],),
            )
            con.execute(
                "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND status='CANDIDATE'",
                (version,),
            )
            con.commit()
        self.db.learning_event("MODEL_PROMOTED",version,metrics)
        return True

    def promote_adaptive_policy(self, version: str, minimum_samples: int = 60) -> bool:
        """Promote a bounded policy-controller result without mislabeling it as a backtest."""
        row = self.db.one(
            "SELECT * FROM model_versions WHERE version=? AND status='CANDIDATE'",
            (version,),
        )
        if not row:
            return False
        try:
            metrics = json.loads(row["metrics_json"])
            params = json.loads(row["parameters_json"])
        except (TypeError, ValueError):
            return False
        kind = metrics.get("kind")
        supported_policy = (
            row["family"] == "strategy_tactical"
            and kind in {"bounded_online_controller", "path_replay_exit_policy"}
        ) or (
            row["family"] == "strategy_core"
            and kind == "bounded_core_outcome_controller"
        )
        if (
            not supported_policy
            or int(metrics.get("samples", 0)) < minimum_samples
            or not isinstance(params, dict)
        ):
            self.db.learning_event(
                "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                {"reason": "POLICY_QUALITY_GATE_FAILED", "minimum_samples": minimum_samples},
            )
            return False
        # Path-based exit candidates need their own holdout quality gate. Entry-policy
        # candidates keep their existing bounded-controller contract.
        if metrics.get("kind") == "path_replay_exit_policy":
            try:
                valid_metrics = (
                    int(metrics.get("training_samples", 0)) >= 42
                    and int(metrics.get("validation_samples", 0)) >= 18
                    and float(metrics.get("training_coverage", 0.0)) >= 0.85
                    and float(metrics.get("validation_coverage", 0.0)) >= 0.85
                    and float(metrics.get("training_improvement_bps", 0.0)) >= 10.0
                    and float(metrics.get("validation_improvement_bps", 0.0)) >= 5.0
                    and float(metrics.get("candidate_validation_mean_net_bps", 0.0)) > 0.0
                    and float(metrics.get("candidate_validation_max_drawdown", 1.0))
                    <= float(metrics.get("baseline_validation_max_drawdown", 0.0)) + 0.01
                )
            except (TypeError, ValueError):
                valid_metrics = False
            if not valid_metrics:
                self.db.learning_event(
                    "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                    {"reason": "EXIT_POLICY_VALIDATION_QUALITY_GATE_FAILED"},
                )
                return False

        if kind == "bounded_core_outcome_controller":
            try:
                action = str(metrics.get("action") or "")
                common_quality = (
                    metrics.get("data_basis")
                    == "gross_return_bps_minus_decision_expected_cost_bps"
                    and metrics.get("exchange_fee_estimates_used") is False
                    and metrics.get("verified_net_pnl_used") is False
                    and int(metrics.get("previous_samples", 0)) >= 30
                    and int(metrics.get("recent_samples", 0)) >= 30
                )
                recent_mean = float(metrics.get("recent_mean_after_expected_cost_bps", 0.0))
                previous_mean = float(metrics.get("previous_mean_after_expected_cost_bps", 0.0))
                recent_positive_rate = float(
                    metrics.get("recent_positive_after_expected_cost_rate", 0.0)
                )
                if action == "TIGHTEN":
                    action_quality = recent_mean <= -10.0 and recent_positive_rate < 0.45
                elif action == "RESTORE_TOWARD_DEFAULTS":
                    action_quality = (
                        recent_mean >= 15.0
                        and recent_positive_rate >= 0.60
                        and recent_mean >= previous_mean + 5.0
                    )
                else:
                    action_quality = False
                valid_metrics = common_quality and action_quality
            except (TypeError, ValueError):
                valid_metrics = False
            if not valid_metrics:
                self.db.learning_event(
                    "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                    {"reason": "CORE_ENTRY_OUTCOME_QUALITY_GATE_FAILED"},
                )
                return False

        # Hard bounds are enforced again at promotion, independently of the optimizer.
        bounds = {
            "strategy_min_edge_bps": (25.0, 80.0),
            "strategy_min_confidence": (0.58, 0.85),
            "strategy_adaptive_edge_floor_bps": (15.0, 25.0),
            "strategy_adaptive_min_confidence": (0.75, 0.95),
            "strategy_adaptive_cost_ratio": (1.10, 1.50),
            "tactical_min_expected_move_bps": (80.0, 1200.0),
            "tactical_min_momentum_bps": (10.0, 300.0),
            "tactical_min_volume_ratio": (1.0, 8.0),
            "tactical_min_breakout_bps": (5.0, 250.0),
            "tactical_max_spread_bps": (3.0, 60.0),
            "tactical_adaptive_min_expected_move_bps": (80.0, 800.0),
            "tactical_portfolio_pct": (1.0, 25.0),
            "tactical_position_limit_pct": (1.0, 80.0),
            "tactical_stop_loss_pct": (0.35, 2.0),
            "tactical_take_profit_pct": (1.0, 5.0),
            "tactical_trailing_trigger_bps": (50.0, 500.0),
            "tactical_trailing_stop_pct": (0.2, 1.5),
            "tactical_max_hold_seconds": (300.0, 7200.0),
        }
        try:
            for key, (lo, hi) in bounds.items():
                if key in params and not lo <= float(params[key]) <= hi:
                    raise ValueError(key)
        except (TypeError, ValueError):
            self.db.learning_event(
                "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                {"reason": "PARAMETER_OUT_OF_BOUNDS"},
            )
            return False
        if kind == "bounded_core_outcome_controller":
            try:
                required_core_keys = {
                    "strategy_min_edge_bps",
                    "strategy_min_confidence",
                    "strategy_adaptive_edge_floor_bps",
                    "strategy_adaptive_min_confidence",
                    "strategy_adaptive_cost_ratio",
                }
                if not required_core_keys.issubset(params):
                    raise ValueError("INCOMPLETE_CORE_POLICY")
                if (
                    float(params["strategy_adaptive_edge_floor_bps"])
                    > float(params["strategy_min_edge_bps"])
                    or float(params["strategy_adaptive_min_confidence"])
                    < float(params["strategy_min_confidence"])
                    or float(params["strategy_adaptive_cost_ratio"]) < 1.10
                ):
                    raise ValueError("CORE_POLICY_RELATIONSHIP_INVALID")
            except (TypeError, ValueError):
                self.db.learning_event(
                    "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                    {"reason": "CORE_POLICY_RELATIONSHIP_INVALID"},
                )
                return False

        try:
            if (
                "tactical_stop_loss_pct" in params
                and "tactical_take_profit_pct" in params
                and float(params["tactical_stop_loss_pct"]) >= float(params["tactical_take_profit_pct"])
            ):
                raise ValueError("EXIT_STOP_MUST_BE_BELOW_TARGET")
        except (TypeError, ValueError):
            self.db.learning_event(
                "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                {"reason": "EXIT_STOP_MUST_BE_BELOW_TARGET"},
            )
            return False
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' "
                "ORDER BY created_at DESC LIMIT 1",
                (row["family"],),
            ).fetchone()
            if current and current["version"] == version:
                con.rollback()
                return True
            con.execute(
                "UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",
                (row["family"],),
            )
            con.execute(
                "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=? AND status='CANDIDATE'",
                (version, row["family"]),
            )
            changed = con.total_changes > 0
            con.commit()
        if changed:
            self.db.learning_event("ADAPTIVE_POLICY_PROMOTED", version, metrics)
        return changed

    def rollback(self, family: str="decision") -> str:
        row=self.db.one("SELECT parent_version FROM model_versions WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",(family,))
        parent=row.get("parent_version") if row else None
        if not parent:
            return self.active(family)
        parent_row = self.db.one(
            "SELECT version FROM model_versions WHERE version=? AND family=?",
            (parent, family),
        )
        if not parent_row:
            return self.active(family)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",
                (family,),
            )
            con.execute(
                "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=?",
                (parent, family),
            )
            con.commit()
        self.db.learning_event("MODEL_ROLLBACK",parent,{"family":family})
        return parent
