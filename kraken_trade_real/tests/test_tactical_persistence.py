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
        entry_context={"confidence": "0.90", "volume_ratio": "2.5"},
        entry_parameters={"tactical_min_momentum_bps": "40"},
    )
    rows = db.tactical_positions()
    assert len(rows) == 1
    assert rows[0]["direction"] == "SHORT"
    assert rows[0]["notional_eur"] == "10"
    assert __import__("json").loads(rows[0]["entry_context_json"])["volume_ratio"] == "2.5"
    assert __import__("json").loads(rows[0]["entry_parameters_json"])["tactical_min_momentum_bps"] == "40"

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



def test_tactical_position_refresh_preserves_learning_context(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    db.save_tactical_position(
        "ETH/USD", "spot", "LONG",
        Decimal("0.2"), Decimal("100"), Decimal("105"),
        Decimal("99"), Decimal("20"), Decimal("1"),
        1000.0, "entry-context", Decimal("120"), "OPEN",
        entry_context={"confidence": "0.85"},
        entry_parameters={"tactical_min_volume_ratio": "2.0"},
    )
    db.save_tactical_position(
        "ETH/USD", "spot", "LONG",
        Decimal("0.2"), Decimal("101"), Decimal("105"),
        Decimal("99"), Decimal("20.2"), Decimal("1"),
        1000.0, "entry-context", Decimal("120"), "OPEN",
    )
    row = db.tactical_positions()[0]
    assert __import__("json").loads(row["entry_context_json"])["confidence"] == "0.85"
    assert __import__("json").loads(row["entry_parameters_json"])["tactical_min_volume_ratio"] == "2.0"



def test_tactical_exit_fill_and_position_watermark_commit_atomically(tmp_path):
    db = Database(str(tmp_path / "trader.db"))
    db.save_tactical_position(
        "ETH/USD", "spot", "LONG",
        Decimal("1.0"), Decimal("100"), Decimal("110"),
        Decimal("95"), Decimal("100"), Decimal("1"),
        1000.0, "entry-order", Decimal("150"), "OPEN",
        entry_context={"confidence": "0.9"},
        entry_parameters={"tactical_min_volume_ratio": "2.0"},
    )
    trade = {
        "trade_id": "exit-order:0.4",
        "symbol": "ETH/USD",
        "direction": "LONG",
        "entry_price": "100",
        "exit_price": "110",
        "quantity": "0.4",
        "gross_pnl_eur": "4",
        "fees_eur": "0.5",
        "net_pnl_eur": "3.5",
        "opened_at": 1000.0,
        "closed_at": 1100.0,
        "hold_seconds": 100.0,
        "exit_reason": "TRAILING_STOP",
        "setup_score": "150",
        "detail": {"entry_context": {"confidence": "0.9"}},
    }
    remaining = {
        "symbol": "ETH/USD",
        "venue": "spot",
        "direction": "LONG",
        "quantity": Decimal("0.6"),
        "entry_price": Decimal("100"),
        "peak_price": Decimal("110"),
        "trough_price": Decimal("95"),
        "notional_eur": Decimal("60"),
        "leverage": Decimal("1"),
        "opened_at": 1000.0,
        "entry_client_order_id": "entry-order",
        "setup_score": Decimal("150"),
        "state": "OPEN",
        "entry_context": {"confidence": "0.9"},
        "entry_parameters": {"tactical_min_volume_ratio": "2.0"},
    }

    assert db.commit_tactical_exit_fill(
        client_order_id="exit-order",
        previous_filled_quantity=Decimal("0"),
        cumulative_filled_quantity=Decimal("0.4"),
        cumulative_average_price=Decimal("110"),
        terminal=False,
        trade=trade,
        remaining_position=remaining,
    ) is True

    # Replaying the same exchange cumulative volume must not debit the position
    # or duplicate the close trade.
    assert db.commit_tactical_exit_fill(
        client_order_id="exit-order",
        previous_filled_quantity=Decimal("0"),
        cumulative_filled_quantity=Decimal("0.4"),
        cumulative_average_price=Decimal("110"),
        terminal=False,
        trade=trade,
        remaining_position=remaining,
    ) is False
    position = db.tactical_positions()[0]
    assert Decimal(position["quantity"]) == Decimal("0.6")
    assert Decimal(position["notional_eur"]) == Decimal("60")
    progress = db.tactical_order_progress("exit-order")
    assert progress is not None
    assert Decimal(progress["last_filled_quantity"]) == Decimal("0.4")
    assert db.one("SELECT COUNT(*) AS n FROM tactical_trades")["n"] == 1
