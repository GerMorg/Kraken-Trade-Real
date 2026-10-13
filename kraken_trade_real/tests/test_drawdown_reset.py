from __future__ import annotations

from typing import Any

from app.portfolio.reconcile import PortfolioReconciler


class FakeDB:
    def __init__(self, metadata: dict[str, str] | None = None) -> None:
        self.metadata = metadata or {}
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        if "portfolio_drawdown_tracking_reset_at" in sql:
            value = self.metadata.get("portfolio_drawdown_tracking_reset_at")
            return {"value": value} if value is not None else None
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        if "INSERT INTO metadata" in sql and len(params) == 2:
            self.metadata[str(params[0])] = str(params[1])

    def event(self, code: str, level: str, payload: dict[str, Any]) -> None:
        self.events.append((code, level, payload))


def test_drawdown_epoch_initializes_once_and_emits_audit_event() -> None:
    db = FakeDB()
    reconciler = PortfolioReconciler(gateway=object(), db=db)

    first = reconciler._drawdown_tracking_reset_at(1_800_000_000.0)
    second = reconciler._drawdown_tracking_reset_at(1_800_000_300.0)

    assert first == 1_800_000_000.0
    assert second == first
    assert db.metadata["portfolio_drawdown_tracking_reset_at"] == str(first)
    assert [event[0] for event in db.events] == ["PORTFOLIO_DRAWDOWN_TRACKING_RESET"]
    assert db.events[0][2]["historical_snapshots_deleted"] is False


def test_drawdown_epoch_preserves_existing_valid_reset_timestamp() -> None:
    db = FakeDB({"portfolio_drawdown_tracking_reset_at": "1790000000.0"})
    reconciler = PortfolioReconciler(gateway=object(), db=db)

    result = reconciler._drawdown_tracking_reset_at(1_800_000_000.0)

    assert result == 1_790_000_000.0
    assert db.events == []
