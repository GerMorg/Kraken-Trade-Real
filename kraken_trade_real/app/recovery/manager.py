
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CircuitBreaker:
    active: bool = False
    reason: str = ""

    BLOCKING_ISSUES = frozenset({
        "AUTH_FAILURE", "PERMISSION_FAILURE", "KRAKEN_UNAVAILABLE", "DATA_STALE",
        "SEQUENCE_GAP", "PORTFOLIO_MISMATCH", "UNKNOWN_ORDER", "EXCESSIVE_SLIPPAGE",
        "ORDER_REJECT_RATE", "LOSS_LIMIT", "DRAWDOWN_LIMIT", "MARGIN_STRESS",
        "VOLATILITY_STRESS", "EXECUTION_FAILURE",
    })

    def trip(self, issue: str, reason: str) -> None:
        if issue in self.BLOCKING_ISSUES:
            self.active = True
            self.reason = f"{issue}:{reason}"[:500]

    def recover(self) -> None:
        self.active = False
        self.reason = ""


class RecoveryManager:
    def __init__(self, db: Any, audit: Any, breaker: CircuitBreaker) -> None:
        self.db = db
        self.audit = audit
        self.breaker = breaker

    def issue(self, problem: str, detail: str) -> None:
        self.breaker.trip(problem, detail)
        self.db.execute(
            "INSERT INTO recovery_events(created_at,issue,state,detail) VALUES(?,?,?,?)",
            (time.time(), problem, "TRIPPED" if self.breaker.active else "OBSERVED", detail[:1000]),
        )
        self.audit.emit(
            "RECOVERY_ISSUE",
            "ERROR" if self.breaker.active else "WARNING",
            issue=problem,
            detail=detail,
        )

    def recovered(self, problem: str) -> None:
        self.db.execute(
            "INSERT INTO recovery_events(created_at,issue,state,detail) VALUES(?,?,?,?)",
            (time.time(), problem, "RECOVERED", "manual/runtime recovery checks passed"),
        )
        self.audit.emit("RECOVERY_RESOLVED", "INFO", issue=problem)
