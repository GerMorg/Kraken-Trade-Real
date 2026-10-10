from __future__ import annotations

from typing import Any
import time


class LearningEngine:
    def __init__(
        self, db: Any, calibration: Any, registry: Any, research: Any,
        auto_promotion_enabled: bool = True,
    ) -> None:
        self.db = db
        self.calibration = calibration
        self.registry = registry
        self.research = research
        self.auto_promotion_enabled = bool(auto_promotion_enabled)

    def record_cycle(self, cycle_id: str, decisions: int, orders: int, blockers: list[str]) -> None:
        self.db.learning_event(
            "CYCLE_OUTCOME",
            cycle_id,
            {"decisions": decisions, "orders": orders, "blockers": blockers},
        )
        if orders == 0:
            self.db.learning_event(
                "NO_TRADE",
                cycle_id,
                {"reason": "no executable order"},
            )

    def record_order_outcome(self, client_order_id: str, payload: dict) -> None:
        self.db.learning_event("ORDER_OUTCOME", client_order_id, payload)

    def calibrate(self, pairs: list[tuple[float, bool]], model_version: str) -> dict:
        metrics = self.calibration.evaluate(pairs)
        self.db.execute(
            "INSERT INTO calibrations(created_at,model_version,sample_count,brier_score,ece,bins_json) VALUES(strftime('%s','now'),?,?,?,?,?)",
            (
                model_version,
                metrics["sample_count"],
                metrics["brier"],
                metrics["ece"],
                __import__("json").dumps(metrics["bins"]),
            ),
        )
        return metrics

    def process_feedback(self, now: float | None = None) -> dict:
        settled = self.db.settle_predictions(now)
        open_row = self.db.one(
            "SELECT COUNT(*) AS n FROM predictions WHERE outcome_status='OPEN'"
        )
        settled_row = self.db.one(
            "SELECT COUNT(*) AS n FROM predictions WHERE outcome_status='SETTLED'"
        )
        open_predictions = int(open_row["n"]) if open_row else 0
        settled_total = int(settled_row["n"]) if settled_row else 0

        # Chronological split: older rows select a scale, newest rows validate it.
        rows = self.db.query(
            """SELECT p.probability, o.success
               FROM predictions p
               JOIN prediction_outcomes o ON o.prediction_id=p.prediction_id
               WHERE p.outcome_status='SETTLED'
               ORDER BY o.measured_at ASC LIMIT 1000"""
        )
        pairs = [(float(r["probability"]), bool(r["success"])) for r in rows]
        if len(pairs) < 100:
            result = {
                "settled": settled, "settled_total": settled_total,
                "open_predictions": open_predictions, "samples": len(pairs),
                "minimum_samples": 100, "status": "INSUFFICIENT_DATA",
            }
            self.db.learning_event("LEARNING_FEEDBACK", "decision", result)
            return result

        split = min(len(pairs) - 30, max(70, int(len(pairs) * 0.70)))
        training, validation = pairs[:split], pairs[split:]
        parent = self.registry.active()
        parent_params = self.registry.parameters(parent)
        train_base = self.calibration.evaluate(training)
        best_scale = 1.0
        best_train_brier = float(train_base["brier"])
        for scale in (0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30):
            candidate = [(max(0.0, min(1.0, p * scale)), y) for p, y in training]
            metrics = self.calibration.evaluate(candidate)
            if float(metrics["brier"]) < best_train_brier:
                best_train_brier, best_scale = float(metrics["brier"]), scale

        validation_base = self.calibration.evaluate(validation)
        validation_candidate = self.calibration.evaluate([
            (max(0.0, min(1.0, p * best_scale)), y) for p, y in validation
        ])
        base_brier = float(validation_base["brier"])
        candidate_brier = float(validation_candidate["brier"])
        improvement = base_brier - candidate_brier
        promoted, candidate_version = False, ""
        if (
            best_scale != 1.0 and len(validation) >= 30 and improvement >= 0.005
            and self.auto_promotion_enabled
        ):
            candidate_version = f"decision-calibrated-{int(time.time())}"
            parameters = dict(parent_params)
            parameters.update({
                "kind": "calibrated", "confidence_scale": best_scale,
                "validation_method": "chronological_70_30_holdout",
            })
            metrics = {
                "samples": len(validation), "training_samples": len(training),
                "brier": candidate_brier, "ece": float(validation_candidate["ece"]),
                "improvement": improvement, "parent_brier": base_brier,
                "parent_ece": float(validation_base["ece"]), "selected_scale": best_scale,
                "validation_method": "chronological_70_30_holdout",
            }
            self.proposal(candidate_version, parent, metrics, parameters)
            promoted = self.registry.promote(
                candidate_version, min_improvement=0.005, min_samples=30
            )

        result = {
            "settled": settled, "settled_total": settled_total,
            "open_predictions": open_predictions, "samples": len(pairs),
            "training_samples": len(training), "validation_samples": len(validation),
            "status": "OK", "brier": base_brier,
            "candidate_brier": candidate_brier, "ece": float(validation_base["ece"]),
            "candidate_ece": float(validation_candidate["ece"]),
            "best_scale": best_scale, "improvement": improvement,
            "validation_method": "chronological_70_30_holdout",
            "candidate_version": candidate_version, "promoted": promoted,
        }
        self.db.learning_event("LEARNING_FEEDBACK", parent, result)
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
