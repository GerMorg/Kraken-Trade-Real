from __future__ import annotations

from app.persistence.db import Database


def test_core_exit_episode_path_upserts_once_per_cycle_and_closes(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    now = 1_800_000_000.0
    db.create_core_exit_episode(
        episode_id="episode-1",
        symbol="XBT/EUR",
        direction="SHORT",
        opened_at=now,
        observed_at=now + 1,
        order_ids=["order-1"],
        decision_ids=["decision-1"],
        open_lot_count=1,
        basis_eur="100",
        first_profit_pct="0.5",
        peak_profit_pct="0.5",
        eligible_for_learning=True,
        eligibility_reason="APP_MANAGED_SINGLE_ORDER_CAPTURED_FROM_ENTRY_CYCLE",
        exit_policy_snapshot={"strategy_stop_loss_pct": "2.0"},
    )

    db.record_core_exit_observation(
        episode_id="episode-1",
        cycle_id="cycle-1",
        observed_at=now + 10,
        profit_pct="1.0",
        peak_profit_pct="1.0",
        basis_eur="100",
        quantity="1",
        position_pnl_eur="1",
        partial_taken=False,
        pending_order_id="",
        policy_snapshot={"strategy_stop_loss_pct": "2.0"},
        quality={"pnl_present": True},
    )
    # A later reconciliation in the same runtime cycle replaces the early sample.
    db.record_core_exit_observation(
        episode_id="episode-1",
        cycle_id="cycle-1",
        observed_at=now + 12,
        profit_pct="1.5",
        peak_profit_pct="1.5",
        basis_eur="100",
        quantity="1",
        position_pnl_eur="1.5",
        partial_taken=True,
        pending_order_id="close-1",
        policy_snapshot={"strategy_stop_loss_pct": "2.0"},
        quality={"pnl_present": True, "final_reconciliation": True},
    )

    points = db.core_exit_path("episode-1")
    assert len(points) == 1
    assert points[0]["observed_at"] == now + 12
    assert points[0]["profit_pct"] == "1.5"
    assert points[0]["position_pnl_eur"] == "1.5"
    assert points[0]["partial_taken"] == 1

    episode = db.active_core_exit_episode("XBT/EUR")
    assert episode is not None
    assert episode["eligible_for_learning"] == 1
    assert episode["last_profit_pct"] == "1.5"
    assert db.close_core_exit_episode("episode-1", now + 30, "TRAILING_PROFIT_EXIT") is True
    assert db.close_core_exit_episode("episode-1", now + 31, "DUPLICATE") is False
    assert db.active_core_exit_episode("XBT/EUR") is None
    closed = db.core_exit_episodes(eligible_only=True)
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "TRAILING_PROFIT_EXIT"


def test_core_exit_episode_schema_migration_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    assert db.one("SELECT value FROM metadata WHERE key='schema_version'")["value"] == "10"
    db._init_schema()
    assert db.one("SELECT value FROM metadata WHERE key='schema_version'")["value"] == "10"
    assert db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='core_exit_path_points'")
