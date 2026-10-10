from __future__ import annotations

from decimal import Decimal

from app.learning.realized import CoreSpotRealizedOutcomeLedger


def _add_order(
    db,
    *,
    exchange_order_id: str,
    client_order_id: str,
    decision_id: str,
    direction: str,
    side: str,
    reduce_only: bool,
    symbol: str = "XBT/EUR",
):
    db.execute(
        """INSERT INTO orders(
             intent_id,client_order_id,created_at,decision_id,symbol,direction,side,
             order_type,quantity,limit_price,leverage,margin,reduce_only,post_only,
             state,submitted_at,kraken_order_id,expected_edge_bps,max_slippage_bps,expires_seconds
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"intent-{client_order_id}", client_order_id, 10.0, decision_id,
            symbol, direction, side, "market", "10", None, "1", 0,
            int(reduce_only), 0, "FILLED", 11.0, exchange_order_id, "20", "30", 60,
        ),
    )


def _add_fill(
    db,
    *,
    exchange_order_id: str,
    client_order_id: str,
    decision_id: str,
    trade_id: str,
    timestamp: float,
    side: str,
    quantity: str,
    price: str,
    fee: str,
    quote_asset: str = "ZEUR",
    symbol: str = "XBT/EUR",
):
    raw = {
        "ordertxid": exchange_order_id,
        "pair": "XXBTZEUR",
        "time": timestamp,
        "type": side,
        "price": price,
        "vol": quantity,
        "fee": fee,
        "trade_id": trade_id,
    }
    return db.save_attributed_fill(
        order_id=exchange_order_id,
        trade_id=trade_id,
        created_at=timestamp,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency="UNVERIFIED",
        client_order_id=client_order_id,
        decision_id=decision_id,
        venue="spot",
        quote_asset=quote_asset,
        raw_payload=raw,
    )


def test_fifo_attribution_handles_partial_long_closes_and_aggregates_by_entry_decision(db):
    _add_order(
        db, exchange_order_id="O-ENTRY", client_order_id="C-ENTRY",
        decision_id="D-ENTRY", direction="LONG", side="buy", reduce_only=False,
    )
    _add_fill(
        db, exchange_order_id="O-ENTRY", client_order_id="C-ENTRY",
        decision_id="D-ENTRY", trade_id="T-ENTRY", timestamp=100,
        side="buy", quantity="2", price="100", fee="0.20",
    )
    _add_order(
        db, exchange_order_id="O-CLOSE-1", client_order_id="C-CLOSE-1",
        decision_id="D-CLOSE-1", direction="SHORT", side="sell", reduce_only=True,
    )
    _add_fill(
        db, exchange_order_id="O-CLOSE-1", client_order_id="C-CLOSE-1",
        decision_id="D-CLOSE-1", trade_id="T-CLOSE-1", timestamp=110,
        side="sell", quantity="0.5", price="120", fee="0.10",
    )
    _add_order(
        db, exchange_order_id="O-CLOSE-2", client_order_id="C-CLOSE-2",
        decision_id="D-CLOSE-2", direction="SHORT", side="sell", reduce_only=True,
    )
    _add_fill(
        db, exchange_order_id="O-CLOSE-2", client_order_id="C-CLOSE-2",
        decision_id="D-CLOSE-2", trade_id="T-CLOSE-2", timestamp=120,
        side="sell", quantity="1.5", price="90", fee="0.15",
    )

    result = CoreSpotRealizedOutcomeLedger(db).rebuild()

    assert result["status"] == "REBUILT"
    assert result["processed_fills"] == 3
    assert result["realized_legs"] == 2
    assert result["realized_decisions"] == 1
    assert result["open_inventory_lots"] == 0
    assert result["attribution_anomalies"] == 0
    assert result["verified_net_outcomes"] == 0

    outcome = db.one(
        """SELECT * FROM core_realized_outcomes
           WHERE opening_decision_id='D-ENTRY'"""
    )
    assert outcome is not None
    assert Decimal(outcome["closed_quantity"]) == Decimal("2")
    assert Decimal(outcome["closed_notional_quote"]) == Decimal("200.0")
    assert Decimal(outcome["gross_pnl_quote"]) == Decimal("-5.0")
    assert Decimal(outcome["fees_est_quote"]) == Decimal("0.45")
    assert Decimal(outcome["estimated_net_pnl_quote"]) == Decimal("-5.45")
    assert Decimal(outcome["gross_return_bps"]) == Decimal("-250.0")
    assert outcome["fee_status"] == "QUOTE_ESTIMATE_ONLY"
    assert outcome["net_verified"] == 0
    legs = db.query("SELECT * FROM core_realized_legs ORDER BY closed_at")
    assert [row["closing_decision_id"] for row in legs] == ["D-CLOSE-1", "D-CLOSE-2"]
    assert [row["opening_decision_id"] for row in legs] == ["D-ENTRY", "D-ENTRY"]


def test_fifo_attribution_handles_short_positions_with_quote_currency_pnl(db):
    _add_order(
        db, exchange_order_id="O-SHORT", client_order_id="C-SHORT",
        decision_id="D-SHORT", direction="SHORT", side="sell", reduce_only=False,
    )
    _add_fill(
        db, exchange_order_id="O-SHORT", client_order_id="C-SHORT",
        decision_id="D-SHORT", trade_id="T-SHORT", timestamp=200,
        side="sell", quantity="2", price="100", fee="0.20",
    )
    _add_order(
        db, exchange_order_id="O-BUYBACK", client_order_id="C-BUYBACK",
        decision_id="D-BUYBACK", direction="LONG", side="buy", reduce_only=True,
    )
    _add_fill(
        db, exchange_order_id="O-BUYBACK", client_order_id="C-BUYBACK",
        decision_id="D-BUYBACK", trade_id="T-BUYBACK", timestamp=210,
        side="buy", quantity="2", price="80", fee="0.10",
    )

    result = CoreSpotRealizedOutcomeLedger(db).rebuild()
    outcome = db.one("SELECT * FROM core_realized_outcomes WHERE opening_decision_id='D-SHORT'")

    assert result["realized_legs"] == 1
    assert outcome is not None
    assert outcome["direction"] == "SHORT"
    assert Decimal(outcome["gross_pnl_quote"]) == Decimal("40")
    assert Decimal(outcome["gross_return_bps"]) == Decimal("2000")
    assert outcome["net_verified"] == 0


def test_fifo_rebuild_is_deterministic_and_preserves_open_inventory(db):
    _add_order(
        db, exchange_order_id="O-OPEN", client_order_id="C-OPEN",
        decision_id="D-OPEN", direction="LONG", side="buy", reduce_only=False,
    )
    _add_fill(
        db, exchange_order_id="O-OPEN", client_order_id="C-OPEN",
        decision_id="D-OPEN", trade_id="T-OPEN", timestamp=300,
        side="buy", quantity="0.25", price="100", fee="0.025",
    )

    ledger = CoreSpotRealizedOutcomeLedger(db)
    first = ledger.rebuild()
    first_lot = db.one("SELECT * FROM core_inventory_lots WHERE lot_id IS NOT NULL")
    second = ledger.rebuild()
    second_lot = db.one("SELECT * FROM core_inventory_lots WHERE lot_id IS NOT NULL")

    assert first["open_inventory_lots"] == 1
    assert second["open_inventory_lots"] == 1
    assert first_lot == second_lot
    assert second["realized_legs"] == 0


def test_fifo_ledger_quarantines_unmatched_close_and_opposite_entry(db):
    _add_order(
        db, exchange_order_id="O-OPEN", client_order_id="C-OPEN",
        decision_id="D-OPEN", direction="LONG", side="buy", reduce_only=False,
    )
    _add_fill(
        db, exchange_order_id="O-OPEN", client_order_id="C-OPEN",
        decision_id="D-OPEN", trade_id="T-OPEN", timestamp=400,
        side="buy", quantity="1", price="100", fee="0.1",
    )
    _add_order(
        db, exchange_order_id="O-OPPOSITE", client_order_id="C-OPPOSITE",
        decision_id="D-OPPOSITE", direction="SHORT", side="sell", reduce_only=False,
    )
    _add_fill(
        db, exchange_order_id="O-OPPOSITE", client_order_id="C-OPPOSITE",
        decision_id="D-OPPOSITE", trade_id="T-OPPOSITE", timestamp=410,
        side="sell", quantity="0.1", price="90", fee="0.01",
    )
    _add_order(
        db, exchange_order_id="O-OVER-CLOSE", client_order_id="C-OVER-CLOSE",
        decision_id="D-OVER-CLOSE", direction="SHORT", side="sell", reduce_only=True,
    )
    _add_fill(
        db, exchange_order_id="O-OVER-CLOSE", client_order_id="C-OVER-CLOSE",
        decision_id="D-OVER-CLOSE", trade_id="T-OVER-CLOSE", timestamp=420,
        side="sell", quantity="2", price="110", fee="0.2",
    )

    result = CoreSpotRealizedOutcomeLedger(db).rebuild()
    reasons = {row["reason"] for row in db.query("SELECT reason FROM core_fill_anomalies")}

    assert result["attribution_anomalies"] == 2
    assert "OPPOSITE_POSITION_OPEN_DURING_ENTRY" in reasons
    assert "REDUCE_ONLY_EXCEEDS_LOCAL_INVENTORY" in reasons
    assert result["realized_legs"] == 1
    leg = db.one("SELECT * FROM core_realized_legs")
    assert leg is not None
    assert Decimal(leg["gross_pnl_quote"]) == Decimal("10")
