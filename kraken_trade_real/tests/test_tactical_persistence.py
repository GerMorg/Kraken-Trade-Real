from __future__ import annotations

from decimal import Decimal

from app.persistence.db import Database


def test_tactical_position_and_trade_persistence(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    db.save_tactical_position(
        "BTC/USD", "spot", "SHORT",
        Decimal("0.01"), Decimal("100"), Decimal("100"),
        Decimal("101"), Decimal("-10"), Decimal("2"),
        1000.0, "entry-1", Decimal("250"), "OPEN",
    )
    rows = db.tactical_positions()
    assert len(rows) == 1
    assert rows[0]["direction"] == "SHORT"
    assert rows[0]["notional_eur"] == "10"

    db.save_tactical_trade(
        "trade-1", "BTC/USD", "SHORT",
        Decimal("100"), Decimal("98"),
        Decimal("0.01"), Decimal("0.20"), Decimal("0.16"),
        Decimal("0.04"), 1000.0, 1100.0, 100.0,
        "TAKE_PROFIT", Decimal("250"), {"mode": "SHADOW"},
    )
    assert db.tactical_trade_count(900.0) == 1
    assert db.tactical_today_net_pnl() >= Decimal("0")
    db.delete_tactical_position("BTC/USD")
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
