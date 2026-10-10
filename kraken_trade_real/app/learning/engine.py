from __future__ import annotations

import time
from typing import Any

from app.learning.policy_optimizer import StrategyPolicyOptimizer


class LearningEngine:
    def __init__(
        self,
        db: Any,
        calibration: Any,
        registry: Any,
        research: Any,
        auto_promotion_enabled: bool = True,
        config: Any | None = None,
    ) -> None:
        self.db = db
        self.calibration = calibration
        self.registry = registry
        self.research = research
        self.auto_promotion_enabled = bool(auto_promotion_enabled)
        self.optimizer = StrategyPolicyOptimizer()
        self.policy_defaults = self.optimizer.defaults_from_config(config)
        self.registry.ensure_family_baseline(
            "strategy_policy",
            "baseline-policy-v1",
            self.policy_defaults,
            "initial bounded strategy-policy baseline",
        )

    def record_cycle(self, cycle_id: str, decisions: int, orders: int, blockers: list[str]) -> None:
        self.db.learning_event(
            "CYCLE_OUTCOME",
            cycle_id,
            {"decisions": decisions, "orders": orders, "blockers": blockers},
        )
        if orders == 0:
            self.db.learning_event("NO_TRADE", cycle_id, {"reason": "no executable order"})

    def record_order_outcome(self, client_order_id: str, payload: dict) -> None:
        self.db.learning_event("ORDER_OUTCOME", client_order_id, payload)

    def calibrate(self, pairs: list[tuple[float, bool]], model_version: str) -> dict:
        metrics = self.calibration.evaluate(pairs)
        self.db.execute(
            """INSERT INTO calibrations(
               created_at,model_version,sample_count,brier_score,ece,bins_json
            ) VALUES(strftime('%s','now'),?,?,?,?,?)""",
            (
                model_version, metrics["sample_count"], metrics["brier"],
                metrics["ece"], __import__("json").dumps(metrics["bins"]),
            ),
        )
        return metrics

    def _monitor_policy(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        version = self.registry.active("strategy_policy")
        active_row = self.db.one(
            """SELECT created_at,parent_version FROM model_versions
               WHERE version=? AND family='strategy_policy'""",
            (version,),
        )
        parent_version = str(active_row.get("parent_version") or "") if active_row else ""
        activated_at = float(active_row.get("created_at") or 0) if active_row else 0.0
        if not parent_version or activated_at <= 0:
            return {"status": "BASELINE_ACTIVE", "version": version}
        post_activation = [
            row for row in observations
            if float(row.get("created_at") or 0) >= activated_at
        ]
        groups = self.optimizer.grouped(post_activation)
        if len(groups) < 100:
            return {
                "status": "MONITORING",
                "version": version,
                "parent_version": parent_version,
                "out_of_sample_groups": len(groups),
                "minimum_groups": 100,
            }
        candidate_parameters = self.registry.parameters(version, family="strategy_policy")
        parent_parameters = self.registry.parameters(parent_version, family="strategy_policy")
        comparison = self.optimizer.compare_out_of_sample(
            post_activation, candidate_parameters, parent_parameters
        )
        candidate_score = comparison["candidate"]
        parent_score = comparison["parent"]
        degraded = (
            candidate_score["samples"] >= 30
            and parent_score["samples"] >= 30
            and candidate_score["mean_net_bps"] < 0
            and parent_score["mean_net_bps"] - candidate_score["mean_net_bps"] >= 5.0
        )
        if degraded:
            rolled_back_to = self.registry.rollback("strategy_policy")
            result = {
                "status": "ROLLED_BACK",
                "version": version,
                "rolled_back_to": rolled_back_to,
                "out_of_sample_groups": len(groups),
                "candidate": candidate_score,
                "parent": parent_score,
                "reason": "POST_PROMOTION_NET_EXPECTANCY_DEGRADED",
            }
            self.db.learning_event("STRATEGY_POLICY_AUTO_ROLLBACK", version, result)
            return result
        return {
            "status": "HEALTHY",
            "version": version,
            "parent_version": parent_version,
            "out_of_sample_groups": len(groups),
            "candidate": candidate_score,
            "parent": parent_score,
        }

    def _tune_strategy_policy(self, observations: list[dict[str, Any]], now: float) -> dict[str, Any]:
        current_version = self.registry.active("strategy_policy")
        current_parameters = self.registry.parameters(
            current_version, family="strategy_policy"
        )
        current_groups = self.optimizer.grouped(observations)
        newest_created = max(
            (float(row.get("created_at") or 0) for row in observations),
            default=0.0,
        )
        watermark_row = self.db.one(
            "SELECT value FROM metadata WHERE key='policy_optimizer_last_tune_created_at'"
        )
        watermark = float(watermark_row["value"]) if watermark_row else 0.0
        fresh_rows = [
            row for row in observations
            if float(row.get("created_at") or 0) > watermark
        ]
        fresh_groups = self.optimizer.grouped(fresh_rows)
        if watermark and len(fresh_groups) < 100:
            return {
                "status": "WAITING_FOR_NEW_DATA",
                "active_policy_version": current_version,
                "settled_groups": len(current_groups),
                "new_groups_since_last_tune": len(fresh_groups),
                "required_new_groups": 100,
                "promoted": False,
            }
        result = self.optimizer.fit(observations, self.policy_defaults, current_parameters)
        if len(current_groups) >= self.optimizer.MIN_GROUPS:
            # Prevent repeatedly searching the same holdout after a failed result.
            self.db.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                ("policy_optimizer_last_tune_created_at", str(newest_created)),
            )
        summary: dict[str, Any] = {
            "status": result["status"],
            "active_policy_version": current_version,
            "settled_groups": result.get("groups", len(current_groups)),
            "promoted": False,
            "auto_promotion_enabled": self.auto_promotion_enabled,
        }
        for key in (
            "training_groups", "validation_groups", "test_groups",
            "validation_improvement_bps", "test_improvement_bps",
        ):
            if key in result:
                summary[key] = result[key]
        if not result.get("promoted"):
            self.db.learning_event("STRATEGY_POLICY_TUNING", current_version, summary)
            return summary

        candidate_version = f"strategy-policy-{int(now * 1000)}"
        validation = result["validation_candidate"]
        test = result["test_candidate"]
        metrics = {
            "samples": int(test["samples"]),
            "improvement": float(result["test_improvement_bps"]),
            "test_improvement_bps": float(result["test_improvement_bps"]),
            "validation_improvement_bps": float(result["validation_improvement_bps"]),
            "test_mean_net_bps": float(test["mean_net_bps"]),
            "validation_mean_net_bps": float(validation["mean_net_bps"]),
            "test_hit_rate": float(test["hit_rate"]),
            "validation_hit_rate": float(validation["hit_rate"]),
            "training_groups": int(result["training_groups"]),
            "validation_groups": int(result["validation_groups"]),
            "test_groups": int(result["test_groups"]),
            "validation_method": "chronological_60_20_20_with_grouped_15m_observations",
        }
        self.registry.register_candidate(
            candidate_version,
            "strategy_policy",
            current_version,
            result["parameters"],
            metrics,
        )
        if self.auto_promotion_enabled:
            promoted = self.registry.promote(
                candidate_version,
                min_improvement=self.optimizer.MIN_TEST_IMPROVEMENT_BPS,
                min_samples=self.optimizer.MIN_EVALUATION_TRADES,
            )
            summary["status"] = "POLICY_PROMOTED" if promoted else "PROMOTION_REJECTED"
            summary["active_policy_version"] = (
                candidate_version if promoted else current_version
            )
            summary["promoted"] = promoted
        else:
            summary["status"] = "CANDIDATE_REQUIRES_REVIEW"
            summary["candidate_version"] = candidate_version
        summary["candidate_version"] = candidate_version
        summary["metrics"] = metrics
        self.db.learning_event("STRATEGY_POLICY_TUNING", candidate_version, summary)
        return summary

    def process_feedback(self, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        settled_predictions = self.db.settle_predictions(now)
        signal_settlement = self.db.settle_signal_observations(now)
        open_row = self.db.one(
            "SELECT COUNT(*) AS n FROM predictions WHERE outcome_status='OPEN'"
        )
        settled_row = self.db.one(
            "SELECT COUNT(*) AS n FROM predictions WHERE outcome_status='SETTLED'"
        )
        open_predictions = int(open_row["n"]) if open_row else 0
        settled_total = int(settled_row["n"]) if settled_row else 0
        prediction_rows = self.db.query(
            """SELECT p.probability,o.success FROM predictions p
               JOIN prediction_outcomes o ON o.prediction_id=p.prediction_id
               WHERE p.outcome_status='SETTLED'
               ORDER BY p.created_at ASC,o.measured_at ASC LIMIT 1000"""
        )
        pairs = [(float(row["probability"]), bool(row["success"])) for row in prediction_rows]
        calibration_status = "INSUFFICIENT_DATA"
        calibration_metrics: dict[str, Any] = {}
        if len(pairs) >= 20:
            calibration_metrics = self.calibration.evaluate(pairs)
            calibration_status = "DIAGNOSTIC_ONLY"
            self.db.execute(
                """INSERT INTO calibrations(
                   created_at,model_version,sample_count,brier_score,ece,bins_json
                ) VALUES(strftime('%s','now'),?,?,?,?,?)""",
                (
                    self.registry.active(),
                    calibration_metrics["sample_count"],
                    calibration_metrics["brier"],
                    calibration_metrics["ece"],
                    __import__("json").dumps(calibration_metrics["bins"]),
                ),
            )
            self.db.learning_event(
                "PREDICTION_CALIBRATION_DIAGNOSTIC",
                self.registry.active(),
                {
                    **calibration_metrics,
                    "interpretation": "confidence-score diagnostic on selected trades, not calibrated event probability",
                    "automatic_decision_promotion": False,
                },
            )

        observations = self.db.query(
            """SELECT * FROM signal_observations WHERE outcome_status='SETTLED'
               ORDER BY created_at DESC LIMIT 5000"""
        )
        rollback = self._monitor_policy(observations)
        policy_tuning = self._tune_strategy_policy(observations, now)
        policy_status = str(policy_tuning.get("status", "UNKNOWN"))
        overall_status = (
            "OK" if len(pairs) >= 20 or policy_status in {
                "POLICY_PROMOTED", "WAITING_FOR_NEW_DATA", "NO_VALIDATED_IMPROVEMENT",
                "CANDIDATE_REQUIRES_REVIEW", "PROMOTION_REJECTED",
            } else "INSUFFICIENT_DATA"
        )
        result = {
            "settled": settled_predictions,
            "settled_total": settled_total,
            "open_predictions": open_predictions,
            "samples": len(pairs),
            "status": overall_status,
            "calibration_status": calibration_status,
            "calibration_metrics": calibration_metrics,
            "settled_signal_observations": signal_settlement["settled"],
            "unscorable_signal_observations": signal_settlement["unscorable"],
            "policy_tuning": policy_tuning,
            "policy_rollback_monitor": rollback,
        }
        self.db.learning_event("LEARNING_FEEDBACK", "decision", result)
        return result

    def proposal(
        self,
        candidate_version: str,
        parent_version: str,
        metrics: dict,
        parameters: dict,
    ) -> None:
        self.registry.register_candidate(
            candidate_version,
            "decision",
            parent_version,
            parameters,
            metrics,
        )
        self.db.learning_event("MODEL_CANDIDATE", candidate_version, metrics)
