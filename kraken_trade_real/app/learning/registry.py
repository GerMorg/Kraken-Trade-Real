from __future__ import annotations

import json
import math
import time
from typing import Any

from app.config.adaptive import (
    CORE_DEFAULTS,
    CORE_ENTRY_POLICY_BOUNDS,
    CORE_EXIT_POLICY_BOUNDS,
    LEARNED_TACTICAL_KEYS,
    TACTICAL_DEFAULTS,
    TACTICAL_POLICY_BOUNDS,
    tactical_minimum_move_floors,
)


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

    @staticmethod
    def _same_policy_value(left: Any, right: Any) -> bool:
        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left is right
        try:
            left_number = float(left)
            right_number = float(right)
        except (TypeError, ValueError, OverflowError):
            return left == right
        return (
            math.isfinite(left_number)
            and math.isfinite(right_number)
            and abs(left_number - right_number) <= 1e-9
        )

    def _adaptive_policy_parameter_error(
        self,
        family: str,
        params: dict[str, Any],
        parent_version: str,
    ) -> str | None:
        """Validate learned policy bounds/relationships before changing ACTIVE status."""
        if family == "strategy_core":
            defaults = CORE_DEFAULTS
            bounds = {**CORE_ENTRY_POLICY_BOUNDS, **CORE_EXIT_POLICY_BOUNDS}
        elif family == "strategy_tactical":
            defaults = TACTICAL_DEFAULTS
            bounds = TACTICAL_POLICY_BOUNDS
        else:
            return "UNSUPPORTED_POLICY_FAMILY"

        parent_params: dict[str, Any] = {}
        if parent_version:
            parent = self.db.one(
                "SELECT parameters_json FROM model_versions WHERE version=? AND family=?",
                (parent_version, family),
            )
            if parent:
                try:
                    decoded = json.loads(parent.get("parameters_json") or "{}")
                    if isinstance(decoded, dict):
                        parent_params = decoded
                except (TypeError, ValueError, json.JSONDecodeError):
                    return "INVALID_PARENT_PARAMETERS"

        # Start from validated defaults, then the same-family parent, then the
        # candidate. Missing legacy keys use defaults; they are never treated as 0.
        effective = dict(defaults)
        effective.update({key: value for key, value in parent_params.items() if key in defaults})
        effective.update({key: value for key, value in params.items() if key in defaults})

        for key, (low, high) in bounds.items():
            try:
                value = float(effective[key])
            except (KeyError, TypeError, ValueError, OverflowError):
                return f"INVALID_PARAMETER:{key}"
            if not math.isfinite(value) or not low <= value <= high:
                return f"PARAMETER_OUT_OF_BOUNDS:{key}"
            if key == "tactical_max_hold_seconds" and not value.is_integer():
                return f"PARAMETER_MUST_BE_INTEGER:{key}"

        if family == "strategy_core":
            edge = float(effective["strategy_min_edge_bps"])
            confidence = float(effective["strategy_min_confidence"])
            adaptive_floor = float(effective["strategy_adaptive_edge_floor_bps"])
            adaptive_confidence = float(effective["strategy_adaptive_min_confidence"])
            if adaptive_floor > edge:
                return "CORE_ADAPTIVE_EDGE_FLOOR_EXCEEDS_BASE_EDGE"
            if adaptive_confidence < confidence:
                return "CORE_ADAPTIVE_CONFIDENCE_BELOW_BASE_CONFIDENCE"
            if float(effective["strategy_adaptive_cost_ratio"]) < 1.10:
                return "CORE_COST_RATIO_BELOW_SAFETY_FLOOR"
            stop = float(effective["strategy_stop_loss_pct"])
            partial = float(effective["strategy_partial_profit_trigger_pct"])
            lock = float(effective["strategy_profit_lock_trigger_pct"])
            floor = float(effective["strategy_profit_lock_floor_pct"])
            if not (stop < partial <= lock and floor < lock):
                return "CORE_EXIT_POLICY_RELATIONSHIP_INVALID"

        if family == "strategy_tactical":
            minimum_volatility = float(effective["tactical_min_volatility_bps"])
            maximum_volatility = float(effective["tactical_max_volatility_bps"])
            if minimum_volatility >= maximum_volatility:
                return "TACTICAL_VOLATILITY_BAND_INVALID"
            if float(effective["tactical_stop_loss_pct"]) >= float(
                effective["tactical_take_profit_pct"]
            ):
                return "TACTICAL_STOP_MUST_BE_BELOW_TARGET"

            adaptive_move_floor, normal_move_floor = tactical_minimum_move_floors(
                effective["tactical_max_spread_bps"],
            )
            if float(effective["tactical_adaptive_min_expected_move_bps"]) < adaptive_move_floor:
                return "TACTICAL_ADAPTIVE_MOVE_BELOW_ESTIMATED_COST_FLOOR"
            if float(effective["tactical_min_expected_move_bps"]) < normal_move_floor:
                return "TACTICAL_MOVE_BELOW_ESTIMATED_ROUND_TRIP_COST_FLOOR"

            # These are safety/operation controls, not learnable policy parameters.
            # A candidate can tune entry/exit behaviour, but cannot silently raise
            # the position ceiling or toggle adaptive gating through its model JSON.
            for key in set(defaults) - LEARNED_TACTICAL_KEYS:
                expected = parent_params.get(key, defaults[key])
                proposed = params.get(key, expected)
                if not self._same_policy_value(proposed, expected):
                    return f"IMMUTABLE_TACTICAL_PARAMETER_CHANGED:{key}"

        return None

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
            and kind in {"bounded_core_outcome_controller", "path_replay_core_exit_policy"}
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
        parameter_error = self._adaptive_policy_parameter_error(
            str(row["family"]),
            params,
            str(row.get("parent_version") or ""),
        )
        if parameter_error:
            self.db.learning_event(
                "ADAPTIVE_POLICY_NOT_PROMOTED",
                version,
                {
                    "reason": "POLICY_PARAMETER_CONTRACT_FAILED",
                    "detail": parameter_error,
                    "family": str(row["family"]),
                    "kind": str(kind),
                },
            )
            return False

        # Path-based exit candidates need their own holdout quality gate. Entry-policy
        # candidates keep their existing bounded-controller contract.
        if kind in {"path_replay_exit_policy", "path_replay_core_exit_policy"}:
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
                required_core_keys = {
                    "market_min_liquidity_eur",
                    "market_max_spread_bps",
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
                action = str(metrics.get("action") or "")
                data_contract = (
                    metrics.get("data_basis"),
                    metrics.get("data_scope"),
                )
                allowed_data_contracts = {
                    (
                        "ledger_verified_non_margin_spot_net_return_bps",
                        "APP_MANAGED_NON_MARGIN_SPOT_VERIFIED_NET_ONLY",
                    ),
                    (
                        "ledger_verified_spot_and_short_hold_margin_net_return_bps",
                        "APP_MANAGED_QUOTE_LEDGER_VERIFIED_SPOT_AND_SHORT_HOLD_MARGIN_NET_ONLY",
                    ),
                }
                common_quality = (
                    data_contract in allowed_data_contracts
                    and metrics.get("exchange_fee_estimates_used") is False
                    and metrics.get("verified_net_pnl_used") is True
                    and int(metrics.get("previous_samples", 0)) >= 30
                    and int(metrics.get("recent_samples", 0)) >= 30
                )
                recent_mean = float(metrics.get("recent_mean_verified_net_return_bps", 0.0))
                previous_mean = float(metrics.get("previous_mean_verified_net_return_bps", 0.0))
                recent_positive_rate = float(metrics.get("recent_positive_verified_net_rate", 0.0))
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
                    {"reason": "CORE_ENTRY_VERIFIED_NET_QUALITY_GATE_FAILED"},
                )
                return False

        if kind == "path_replay_core_exit_policy":
            try:
                required_exit_keys = {
                    "strategy_stop_loss_pct",
                    "strategy_partial_profit_trigger_pct",
                    "strategy_partial_profit_fraction_pct",
                    "strategy_profit_lock_trigger_pct",
                    "strategy_profit_giveback_pct",
                    "strategy_profit_lock_floor_pct",
                }
                if not required_exit_keys.issubset(params):
                    raise ValueError("INCOMPLETE_CORE_EXIT_POLICY")
                stop = float(params["strategy_stop_loss_pct"])
                partial_trigger = float(params["strategy_partial_profit_trigger_pct"])
                lock_trigger = float(params["strategy_profit_lock_trigger_pct"])
                floor = float(params["strategy_profit_lock_floor_pct"])
                if not (stop < partial_trigger <= lock_trigger and floor < lock_trigger):
                    raise ValueError("CORE_EXIT_POLICY_RELATIONSHIP_INVALID")
            except (TypeError, ValueError):
                self.db.learning_event(
                    "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                    {"reason": "CORE_EXIT_POLICY_PARAMETER_CONTRACT_FAILED"},
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
        if row["family"] == "strategy_tactical" and "tactical_reversal_exit_bps" in params:
            try:
                reversal_threshold = float(params["tactical_reversal_exit_bps"])
                if not 30.0 <= reversal_threshold <= 500.0:
                    raise ValueError("TACTICAL_REVERSAL_THRESHOLD_OUT_OF_BOUNDS")
            except (TypeError, ValueError):
                self.db.learning_event(
                    "ADAPTIVE_POLICY_NOT_PROMOTED", version,
                    {"reason": "TACTICAL_REVERSAL_THRESHOLD_OUT_OF_BOUNDS"},
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
