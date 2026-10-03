from decimal import Decimal

from app.domain.models import OrderIntent
from app.domain.states import Direction, OrderState


def test_save_order_intent_persists_all_order_fields(db, instrument):
    intent = OrderIntent(
        intent_id="intent_test",
        client_order_id="client_test",
        decision_id="decision_test",
        instrument=instrument,
        direction=Direction.LONG,
        side="buy",
        order_type="limit",
        quantity=Decimal("0.001"),
        limit_price=Decimal("60000"),
        leverage=Decimal("2"),
        margin=True,
        reduce_only=False,
        expected_edge_bps=Decimal("30"),
        max_slippage_bps=Decimal("40"),
        expires_seconds=45,
        post_only=False,
        state=OrderState.INTENT_CREATED,
    )

    db.save_order_intent(intent)

    row = db.one(
        "SELECT intent_id,client_order_id,decision_id,symbol,direction,side,order_type,"
        "quantity,limit_price,leverage,margin,reduce_only,post_only,state,"
        "expected_edge_bps,max_slippage_bps,expires_seconds "
        "FROM orders WHERE intent_id=?",
        ("intent_test",),
    )
    assert row is not None
    assert row["client_order_id"] == "client_test"
    assert row["decision_id"] == "decision_test"
    assert row["symbol"] == "XBT/EUR"
    assert row["direction"] == "LONG"
    assert row["side"] == "buy"
    assert row["order_type"] == "limit"
    assert row["quantity"] == "0.001"
    assert row["limit_price"] == "60000"
    assert row["leverage"] == "2"
    assert row["margin"] == 1
    assert row["reduce_only"] == 0
    assert row["post_only"] == 0
    assert row["state"] == "INTENT_CREATED"
    assert row["expected_edge_bps"] == "30"
    assert row["max_slippage_bps"] == "40"
    assert row["expires_seconds"] == 45
