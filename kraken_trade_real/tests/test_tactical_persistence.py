from __future__ import annotations

import json
from decimal import Decimal

from app.persistence.db import Database
from app.trading.tactical import TacticalTrader


def test_tactical_position_and_trade_persistence(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    entry_policy = {
        "tactical_min_expected_move_bps": "310.0",
        "tactical_min_momentum_bps": "52.0",
        "tactical_stop_loss_pct": "0.85",
    }
    entry_context = {
        "direction": "SHORT",
        "expected_move_bps": "420",
        "net_edge_bps": "170",
        "reason": "VOL_BREAKOUT_SHORT",
    }
    db.save_tactical_position(
        "BTC/USD", "spot", "SHORT",
        Decimal("0.01"), Decimal("100"), Decimal("100"),
        Decimal("101"), Decimal("-10"), Decimal("2"),
        1000.0, "entry-1", Decimal("250"), "OPEN",
        entry_policy=entry_policy, entry_context=entry_context,
    )
    rows = db.tactical_positions()
    assert len(rows) == 1
    assert rows[0]["direction"] == "SHORT"
    assert rows[0]["notional_eur"] == "10"
    assert json.loads(rows[0]["entry_policy_json"]) == entry_policy
    assert json.loads(rows[0]["entry_context_json"]) == entry_context

    # Routine price/quantity updates must not overwrite the entry-time settings
    # with empty default arguments.
    db.save_tactical_position(
        "BTC/USD", "spot", "SHORT",
        Decimal("0.009"), Decimal("100"), Decimal("100"), Decimal("101"),
        Decimal("9"), Decimal("2"), 1000.0, "entry-1", Decimal("250"), "OPEN",
    )
    refreshed = db.tactical_positions()[0]
    assert json.loads(refreshed["entry_policy_json"]) == entry_policy
    assert json.loads(refreshed["entry_context_json"]) == entry_context

    # The snapshots survive reconstruction through the same recovery path used
    # by TacticalTrader after a process/container restart.
    restored = object.__new__(TacticalTrader)
    restored.db = db
    restored._positions = {}
    restored._load_positions()
    recovered = restored._positions["BTC/USD"]
    assert recovered.entry_policy == entry_policy
    assert recovered.entry_context == entry_context

    db.save_tactical_trade(
        "trade-1", "BTC/USD", "SHORT",
        Decimal("100"), Decimal("98"),
        Decimal("0.01"), Decimal("0.20"), Decimal("0.16"),
        Decimal("0.04"), 1000.0, 1100.0, 100.0,
        "TAKE_PROFIT", Decimal("250"), {"mode": "SHADOW"},
    )
    # This fixture models a completed SHADOW exit, so remove its active
    # position row before testing the live-only episode counter.
    db.delete_tactical_position("BTC/USD")
    assert db.tactical_trade_count(900.0) == 0
    assert db.tactical_today_net_pnl() == Decimal("0")
    assert db.tactical_positions() == []



def test_tactical_order_fill_progress_is_cumulative_and_durable(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    db.save_tactical_order_progress(
        "tactical-order-1", Decimal("0.25"), Decimal("100"), terminal=False,
    )
    first = db.tactical_order_progress("tactical-order-1")
    assert first is not None
    assert first["last_filled_quantity"] == "0.25"
    assert first["terminal"] == 0

    db.save_tactical_order_progress(
        "tactical-order-1", Decimal("0.60"), Decimal("101"), terminal=True,
    )
    second = db.tactical_order_progress("tactical-order-1")
    assert second is not None
    assert second["last_filled_quantity"] == "0.60"
    assert second["last_average_price"] == "101"
    assert second["terminal"] == 1

def test_tactical_entry_snapshot_columns_migrate_idempotently(tmp_path):
    db_path = str(tmp_path / "trader.db")
    db = Database(db_path)
    columns = {row["name"] for row in db.query("PRAGMA table_info(tactical_positions)")}
    assert "entry_policy_json" in columns
    assert "entry_context_json" in columns
    assert db.one("SELECT value FROM metadata WHERE key='schema_version'")["value"] == "15"

    # Opening the existing database a second time is safe.
    reopened = Database(db_path)
    reopened_columns = {
        row["name"] for row in reopened.query("PRAGMA table_info(tactical_positions)")
    }
    assert "entry_policy_json" in reopened_columns
    assert "entry_context_json" in reopened_columns




def test_trailing_position_is_restored_after_restart(tmp_path):
    db = Database(str(tmp_path / "trailing.db"))
    db.save_tactical_position(
        "BTC/USD", "spot", "SHORT", Decimal("0.01"), Decimal("100"),
        Decimal("98"), Decimal("102"), Decimal("10"), Decimal("2"),
        1_800_000_000.0, "entry-trailing", Decimal("250"), "TRAILING",
    )
    rows = db.tactical_positions()
    assert len(rows) == 1
    assert rows[0]["state"] == "TRAILING"

    restored = object.__new__(TacticalTrader)
    restored.db = db
    restored._positions = {}
    restored._load_positions()
    assert restored._positions["BTC/USD"].state == "TRAILING"


def test_tactical_live_guards_ignore_shadow_and_count_position_episodes(tmp_path):
    from time import time

    db = Database(str(tmp_path / "episodes.db"))
    now = time()
    opened = now - 300
    closed_1 = now - 20
    closed_2 = now - 10

    def save(trade_id, mode, pnl, closed_at, verified=False):
        db.save_tactical_trade(
            trade_id, "BTC/USD", "LONG", Decimal("100"), Decimal("101"),
            Decimal("0.1"), Decimal(pnl) + Decimal("3"), Decimal("3"),
            Decimal(pnl), opened, closed_at, closed_at - opened, "TIME_STOP",
            Decimal("200"), {"mode": mode}, net_verified=verified,
        )

    save("live-partial-1", "LIVE", "5", closed_1, verified=True)
    save("live-partial-2", "LIVE", "-2", closed_2, verified=True)
    save("shadow-loss", "SHADOW", "-500", closed_2, verified=False)

    assert db.tactical_trade_count(opened - 1) == 1
    assert db.tactical_today_net_pnl() == Decimal("3")
    verified = db.query(
        "SELECT trade_id,net_verified FROM tactical_trades WHERE trade_id LIKE 'live-%'"
    )
    assert len(verified) == 2
    assert all(row["net_verified"] == 1 for row in verified)
