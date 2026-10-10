from __future__ import annotations

import json
import math
import time
from typing import Any


class ModelRegistry:
    MAX_PROMOTED_BRIER = 0.25
    MAX_PROMOTED_ECE = 0.15

    BASELINES = {
        "decision": ("baseline-v1", {"kind": "deterministic"}),
        "probability_calibration": (
            "probability-calibration-baseline-v1",
            {"probability_scale": 1.0, "kind": "raw_confidence"},
        ),
        "strategy_policy": ("strategy-policy-baseline-v1", {"kind": "configured_defaults"}),
    }

    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure()

    def ensure(self) -> None:
        """Ensure each independently governed model family has one active version."""
        now = time.time()
        with self.db.connect() as con:
            for family, (baseline_version, parameters) in self.BASELINES.items():
                active = con.execute(
                    "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' LIMIT 1",
                    (family,),
                ).fetchone()
                if active:
                    continue
                latest = con.execute(
                    "SELECT version FROM model_versions WHERE family=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (family,),
                ).fetchone()
                if latest:
                    con.execute(
                        "UPDATE model_versions SET status='ACTIVE' WHERE version=?",
                        (latest["version"],),
                    )
                else:
                    con.execute(
                        """INSERT INTO model_versions(
                           version,family,status,created_at,parent_version,parameters_json,
                           metrics_json,reason
                        ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            baseline_version, family, "ACTIVE", now, None,
                            json.dumps(parameters, sort_keys=True), json.dumps({}),
                            "initial governed baseline",
                        ),
                    )

    def active(self, family: str = "decision") -> str:
        row = self.db.one(
            "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' "
            "ORDER BY created_at DESC LIMIT 1",
            (family,),
        )
        if row:
            return str(row["version"])
        baseline = self.BASELINES.get(family)
        return baseline[0] if baseline else f"{family}-baseline-v1"

    def parameters(self, version: str | None = None, family: str = "decision") -> dict:
        row = self.db.one(
            "SELECT parameters_json FROM model_versions WHERE version=? AND family=?"
            if version else
            "SELECT parameters_json FROM model_versions WHERE family=? AND status='ACTIVE' "
            "ORDER BY created_at DESC LIMIT 1",
            (version, family) if version else (family,),
        )
        if not row:
            baseline = self.BASELINES.get(family)
            return dict(baseline[1]) if baseline else {}
        try:
            value = json.loads(row["parameters_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def register_candidate(
        self,
        version: str,
        family: str,
        parent: str,
        parameters: dict,
        metrics: dict,
    ) -> None:
        if family not in self.BASELINES:
            raise ValueError(f"UNSUPPORTED_MODEL_FAMILY:{family}")
        self.db.execute(
            """INSERT OR REPLACE INTO model_versions(
               version,family,status,created_at,parent_version,parameters_json,metrics_json,reason
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                version, family, "CANDIDATE", time.time(), parent,
                json.dumps(parameters, sort_keys=True, default=str),
                json.dumps(metrics, sort_keys=True, default=str),
                "out-of-sample research candidate",
            ),
        )

    def promote(
        self, version: str, min_improvement: float = 0.05, min_samples: int = 100
    ) -> bool:
        row = self.db.one(
            "SELECT * FROM model_versions WHERE version=? AND status='CANDIDATE'",
            (version,),
        )
        if not row:
            return False
        try:
            metrics = json.loads(row["metrics_json"])
            if not isinstance(metrics, dict):
                metrics = {}
        except (TypeError, ValueError):
            metrics = {}
        family = str(row["family"])
        try:
            sample_count = int(metrics.get("samples", 0))
            improvement = float(metrics.get("improvement", 0))
            finite = all(
                math.isfinite(float(metrics.get(key, 0)))
                for key in ("improvement",)
            )
        except (TypeError, ValueError):
            sample_count, improvement, finite = 0, 0.0, False

        if family in {"decision", "probability_calibration"}:
            try:
                brier = float(metrics.get("brier", 1.0))
                ece = float(metrics.get("ece", 1.0))
                invalid_quality = (
                    not math.isfinite(brier)
                    or not math.isfinite(ece)
                    or brier > self.MAX_PROMOTED_BRIER
                    or ece > self.MAX_PROMOTED_ECE
                )
            except (TypeError, ValueError):
                invalid_quality = True
        else:
            # Strategy-policy candidates use realised cost-adjusted returns, not
            # probability-calibration metrics. Hard risk ceilings are not learnable.
            try:
                utility = float(metrics.get("validation_utility_bps", -1.0))
                mean_return = float(metrics.get("validation_mean_account_return_bps", -1.0))
                drawdown = float(metrics.get("validation_max_drawdown_pct", 1.0))
                validation_buckets = int(metrics.get("validation_buckets", 0))
                invalid_quality = (
                    not all(math.isfinite(x) for x in (utility, mean_return, drawdown))
                    or utility <= 0
                    or mean_return <= 0
                    or drawdown < -0.35
                    or validation_buckets < 20
                )
            except (TypeError, ValueError):
                invalid_quality = True

        if (
            not finite
            or sample_count < min_samples
            or improvement < min_improvement
            or invalid_quality
        ):
            self.db.learning_event(
                "MODEL_NOT_PROMOTED",
                version,
                {
                    **metrics,
                    "family": family,
                    "quality_gate": {
                        "minimum_samples": min_samples,
                        "required_improvement": min_improvement,
                        "failed": invalid_quality or not finite,
                    },
                },
            )
            return False

        # The old connection context used autocommit, so two consecutive UPDATEs
        # could leave a family with no active model if the second operation failed.
        try:
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",
                    (family,),
                )
                con.execute(
                    "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=?",
                    (version, family),
                )
                con.commit()
        except BaseException:
            try:
                con.rollback()
            except Exception:
                pass
            raise
        self.db.learning_event("MODEL_PROMOTED", version, {**metrics, "family": family})
        return True

    def rollback(self, family: str = "decision") -> str:
        row = self.db.one(
            "SELECT parent_version FROM model_versions WHERE family=? AND status='ACTIVE' "
            "ORDER BY created_at DESC LIMIT 1",
            (family,),
        )
        parent = row.get("parent_version") if row else None
        if not parent:
            return self.active(family)
        parent_row = self.db.one(
            "SELECT version FROM model_versions WHERE version=? AND family=?",
            (parent, family),
        )
        if not parent_row:
            self.db.learning_event(
                "MODEL_ROLLBACK_UNAVAILABLE", str(parent),
                {"family": family, "reason": "PARENT_VERSION_NOT_FOUND"},
            )
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
        self.db.learning_event("MODEL_ROLLBACK", str(parent), {"family": family})
        return str(parent)
