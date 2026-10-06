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
        row=self.db.one("SELECT version FROM model_versions LIMIT 1")
        if not row:
            self.db.execute(
                "INSERT INTO model_versions(version,family,status,created_at,parent_version,parameters_json,metrics_json,reason) VALUES(?,?,?,?,?,?,?,?)",
                ("baseline-v1","decision","ACTIVE",time.time(),None,json.dumps({"kind":"deterministic"}),json.dumps({}),"initial baseline"),
            )

    def active(self, family: str="decision") -> str:
        row=self.db.one("SELECT version FROM model_versions WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",(family,))
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
            con.execute("UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",(row["family"],))
            con.execute("UPDATE model_versions SET status='ACTIVE' WHERE version=?",(version,))
        self.db.learning_event("MODEL_PROMOTED",version,metrics)
        return True

    def rollback(self, family: str="decision") -> str:
        row=self.db.one("SELECT parent_version FROM model_versions WHERE family=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",(family,))
        parent=row.get("parent_version") if row else None
        if not parent:
            return self.active(family)
        with self.db.connect() as con:
            con.execute("UPDATE model_versions SET status='RETIRED' WHERE family=? AND status='ACTIVE'",(family,))
            con.execute("UPDATE model_versions SET status='ACTIVE' WHERE version=?",(parent,))
        self.db.learning_event("MODEL_ROLLBACK",parent,{"family":family})
        return parent
