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
