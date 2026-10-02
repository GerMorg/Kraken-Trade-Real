from __future__ import annotations

from typing import Any


class LearningEngine:
    def __init__(self, db: Any, calibration: Any, registry: Any, research: Any) -> None:
        self.db=db
        self.calibration=calibration
        self.registry=registry
        self.research=research

    def record_cycle(self, cycle_id: str, decisions: int, orders: int, blockers: list[str]) -> None:
        self.db.learning_event(
            "CYCLE_OUTCOME",cycle_id,
            {"decisions":decisions,"orders":orders,"blockers":blockers},
        )
        if orders==0:
            self.db.learning_event("NO_TRADE",cycle_id,{"reason":"no executable order"})

    def record_order_outcome(self, client_order_id: str, payload: dict) -> None:
        self.db.learning_event("ORDER_OUTCOME",client_order_id,payload)

    def calibrate(self, pairs: list[tuple[float,bool]], model_version: str) -> dict:
        metrics=self.calibration.evaluate(pairs)
        self.db.execute(
            "INSERT INTO calibrations(created_at,model_version,sample_count,brier_score,ece,bins_json) VALUES(strftime('%s','now'),?,?,?,?,?)",
            (model_version,metrics["sample_count"],metrics["brier"],metrics["ece"],__import__("json").dumps(metrics["bins"])),
        )
        return metrics

    def proposal(self, candidate_version: str, parent_version: str, metrics: dict, parameters: dict) -> None:
        self.registry.register_candidate(candidate_version,"decision",parent_version,parameters,metrics)
        self.db.learning_event("MODEL_CANDIDATE",candidate_version,metrics)
