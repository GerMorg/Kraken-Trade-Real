from __future__ import annotations

from decimal import Decimal

from app.persistence.db import Database


def test_tactical_price_path_is_durable_ordered_and_deduplicated(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    opened_at = 1_800_000_000.0

    db.record_tactical_price_point(
        "BTC/USD", opened_at, opened_at + 10, "SHORT",
        Decimal("100"), Decimal("0"), Decimal("100"), Decimal("100"), "OPEN",
    )
    db.record_tactical_price_point(
        "BTC/USD", opened_at, opened_at + 20, "SHORT",
        Decimal("98"), Decimal("204.0816"), Decimal("100"), Decimal("98"), "OPEN",
        "LONG", Decimal("35"),
    )
    # An unchanged WebSocket tick cannot create duplicate path samples, including
    # when the second caller happens to calculate a different signal.
    db.record_tactical_price_point(
        "BTC/USD", opened_at, opened_at + 20, "SHORT",
        Decimal("97"), Decimal("300"), Decimal("100"), Decimal("97"), "TRAILING",
        "SHORT", Decimal("300"),
    )

    path = db.tactical_price_path("BTC/USD", opened_at)
    assert len(path) == 2
    assert [row["observed_at"] for row in path] == [opened_at + 10, opened_at + 20]
    assert path[0]["direction"] == "SHORT"
    assert path[1]["price"] == "98"
    assert path[1]["state"] == "OPEN"
    assert path[1]["signal_direction"] == "LONG"
    assert path[1]["signal_net_edge_bps"] == "35"


def test_tactical_price_path_can_be_truncated_at_close_time(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    opened_at = 1_800_000_000.0
    for offset, price in [(10, "100"), (20, "99"), (30, "101")]:
        db.record_tactical_price_point(
            "ETH/USD", opened_at, opened_at + offset, "LONG",
            Decimal(price), Decimal("0"), Decimal("101"), Decimal("99"), "OPEN",
        )

    path = db.tactical_price_path("ETH/USD", opened_at, closed_at=opened_at + 20)
    assert [row["price"] for row in path] == ["100", "99"]


def test_tactical_price_path_rejects_invalid_prices_and_pre_entry_points(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    opened_at = 1_800_000_000.0
    db.record_tactical_price_point(
        "ETH/USD", opened_at, opened_at - 1, "LONG",
        Decimal("100"), Decimal("0"), Decimal("100"), Decimal("100"), "OPEN",
    )
    db.record_tactical_price_point(
        "ETH/USD", opened_at, opened_at + 1, "LONG",
        Decimal("0"), Decimal("0"), Decimal("100"), Decimal("100"), "OPEN",
    )
    assert db.tactical_price_path("ETH/USD", opened_at) == []



def test_tactical_price_path_migrates_legacy_table_with_default_signal_fields(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.execute(
            """CREATE TABLE tactical_price_path(
                point_id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                opened_at REAL NOT NULL,
                observed_at REAL NOT NULL,
                direction TEXT NOT NULL,
                price TEXT NOT NULL,
                pnl_bps TEXT NOT NULL,
                peak_price TEXT NOT NULL,
                trough_price TEXT NOT NULL,
                state TEXT NOT NULL,
                UNIQUE(symbol, opened_at, observed_at)
            )"""
        )
        con.execute(
            """INSERT INTO tactical_price_path(
                symbol,opened_at,observed_at,direction,price,pnl_bps,
                peak_price,trough_price,state
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("BTC/USD", 1_800_000_000.0, 1_800_000_001.0, "LONG", "100", "0", "100", "100", "OPEN"),
        )

    db = Database(str(path))
    point = db.tactical_price_path("BTC/USD", 1_800_000_000.0)[0]
    assert point["signal_direction"] == ""
    assert point["signal_net_edge_bps"] == "0"
