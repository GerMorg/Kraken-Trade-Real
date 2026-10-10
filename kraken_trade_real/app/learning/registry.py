from __future__ import annotations

import json
import time
from typing import Any


class ModelRegistry:
    MAX_PROMOTED_BRIER = 0.25
    MAX_PROMOTED_ECE = 0.15

    def __init__(self, db: Any) -> None:
        self.db = db
        self.ensure()

    def ensure(self) -> None:
        row = self.db.one(
            "SELECT version FROM model_versions WHERE family='decision' AND status='ACTIVE' LIMIT 1"
        )
        if not row:
            self.db.execute(
                """INSERT OR IGNORE INTO model_versions(
                   version,family,status,created_at,parent_version,parameters_json,metrics_json,reason
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "baseline-v1", "decision", "ACTIVE", time.time(), None,
                    json.dumps({"kind": "deterministic"}), json.dumps({}), "initial baseline",
                ),
            )

    def ensure_family_baseline(
        self,
        family: str,
        version: str,
        parameters: dict[str, Any],
        reason: str = "initial family baseline",
    ) -> str:
        """Ensure each independently promoted model family has its own fallback."""
        active = self.db.one(
            "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' "
            "ORDER BY created_at DESC LIMIT 1",
            (family,),
        )
        if active:
            active_version = str(active["version"])
            if active_version == version:
                # An unpromoted baseline must continue to reflect current HA options.
                self.db.execute(
                    "UPDATE model_versions SET parameters_json=?,reason=? "
                    "WHERE version=? AND family=? AND status='ACTIVE'",
                    (json.dumps(parameters, sort_keys=True), reason, version, family),
                )
            return active_version
        existing = self.db.one(
            "SELECT version FROM model_versions WHERE version=? AND family=?",
            (version, family),
        )
        if existing:
            self.db.execute(
                "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=?",
                (version, family),
            )
        else:
            self.db.execute(
                """INSERT INTO model_versions(
                   version,family,status,created_at,parent_version,parameters_json,metrics_json,reason
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    version, family, "ACTIVE", time.time(), None,
                    json.dumps(parameters, sort_keys=True), json.dumps({}), reason,
                ),
            )
        return version

    def active(self, family: str = "decision") -> str:
        row = self.db.one(
            "SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' "
            "ORDER BY created_at DESC LIMIT 1",
            (family,),
        )
        if row:
            return str(row["version"])
        return "baseline-v1" if family == "decision" else f"baseline-{family}-v1"

    def parameters(
        self, version: str | None = None, family: str = "decision"
    ) -> dict[str, Any]:
        row = self.db.one(
            """SELECT parameters_json FROM model_versions WHERE version=? AND family=?"""
            if version else
            """SELECT parameters_json FROM model_versions
               WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1""",
            (version, family) if version else (family,),
        )
        if not row:
            return {}
        try:
            value = json.loads(row["parameters_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def register_candidate(
        self, version: str, family: str, parent: str,
        parameters: dict, metrics: dict,
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO model_versions(
               version,family,status,created_at,parent_version,parameters_json,metrics_json,reason
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                version, family, "CANDIDATE", time.time(), parent,
                json.dumps(parameters, sort_keys=True),
                json.dumps(metrics, sort_keys=True),
                "validated policy candidate" if family == "strategy_policy" else "research candidate",
            ),
        )

    def promote(self, version: str, min_improvement: float = 0.05, min_samples: int = 100) -> bool:
        row = self.db.one(
            "SELECT * FROM model_versions WHERE version=? AND status='CANDIDATE'",
            (version,),
        )
        if not row:
            return False
        metrics = json.loads(row["metrics_json"])
        family = str(row["family"])
        if family == "strategy_policy":
            sample_count = int(metrics.get("samples", 0))
            improvement = float(metrics.get("improvement", metrics.get("test_improvement_bps", 0)))
            invalid_quality = (
                float(metrics.get("test_mean_net_bps", -1.0)) <= 0
                or float(metrics.get("validation_mean_net_bps", -1.0)) <= 0
                or float(metrics.get("validation_improvement_bps", -1.0)) < 0
            )
            reason_code = "POLICY_VALIDATION_GATE"
        else:
            sample_count = int(metrics.get("samples", 0))
            improvement = float(metrics.get("improvement", 0))
            invalid_quality = (
                float(metrics.get("brier", 1.0)) > self.MAX_PROMOTED_BRIER
                or float(metrics.get("ece", 1.0)) > self.MAX_PROMOTED_ECE
            )
            reason_code = "MODEL_VALIDATION_GATE"
        if sample_count < min_samples or improvement < min_improvement or invalid_quality:
            self.db.learning_event(
                "MODEL_NOT_PROMOTED",
                version,
                {
                    **metrics,
                    "family": family,
                    "quality_gate": {
                        "reason": reason_code,
                        "min_samples": min_samples,
                        "min_improvement": min_improvement,
                        "failed": invalid_quality,
                        "max_brier": self.MAX_PROMOTED_BRIER if family != "strategy_policy" else None,
                        "max_ece": self.MAX_PROMOTED_ECE if family != "strategy_policy" else None,
                    },
                },
            )
            return False
        with self.db.connect() as con:
            con.execute(
                "UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",
                (family,),
            )
            con.execute(
                "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=?",
                (version, family),
            )
        self.db.learning_event("MODEL_PROMOTED", version, {**metrics, "family": family})
        return True

    def rollback(self, family: str = "decision") -> str:
        row = self.db.one(
            """SELECT parent_version FROM model_versions WHERE family=? AND status='ACTIVE'
               ORDER BY created_at DESC LIMIT 1""",
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
            return self.active(family)
        with self.db.connect() as con:
            con.execute(
                "UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",
                (family,),
            )
            con.execute(
                "UPDATE model_versions SET status='ACTIVE' WHERE version=? AND family=?",
                (parent, family),
            )
        self.db.learning_event("MODEL_ROLLBACK", str(parent), {"family": family})
        return str(parent)
