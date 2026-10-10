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
    )
    # An unchanged WebSocket tick cannot create duplicate path samples.
    db.record_tactical_price_point(
        "BTC/USD", opened_at, opened_at + 20, "SHORT",
        Decimal("97"), Decimal("300"), Decimal("100"), Decimal("97"), "TRAILING",
    )

    path = db.tactical_price_path("BTC/USD", opened_at)
    assert len(path) == 2
    assert [row["observed_at"] for row in path] == [opened_at + 10, opened_at + 20]
    assert path[0]["direction"] == "SHORT"
    assert path[1]["price"] == "98"
    assert path[1]["state"] == "OPEN"


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
