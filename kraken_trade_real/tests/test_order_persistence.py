from decimal import Decimal
import time

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


def test_rejected_intents_do_not_trigger_cooldown_or_daily_limit(config, db, instrument):
    from app.domain.models import MarketSnapshot
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    market=MarketSnapshot(
        "XBT/EUR",Decimal("60005"),Decimal("60000"),Decimal("60010"),Decimal("1000"),time.time(),
        tuple(Decimal("60000") for _ in range(40)),
    )
    authority=TradingAuthority(
        config, object(), db, AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    for i in range(config.execution_max_orders_per_day):
        intent=OrderIntent(
            f"intent_gate_{i}",f"client_gate_{i}",f"decision_gate_{i}",instrument,
            Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
            True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
        )
        db.save_order_intent(intent)
        db.update_order_state(intent.client_order_id,OrderState.REJECTED.value,last_error="TEST_REJECTED")
    candidate=OrderIntent(
        "intent_candidate","client_candidate","decision_candidate",instrument,
        Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
        True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
    )
    check=authority._preflight(candidate,market)
    assert check["allowed"] is True


def test_real_submission_timestamp_triggers_cooldown(config, db, instrument):
    from app.domain.models import MarketSnapshot
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    market=MarketSnapshot(
        "XBT/EUR",Decimal("60005"),Decimal("60000"),Decimal("60010"),Decimal("1000"),time.time(),
        tuple(Decimal("60000") for _ in range(40)),
    )
    authority=TradingAuthority(
        config, object(), db, AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    first=OrderIntent(
        "intent_submitted","client_submitted","decision_submitted",instrument,
        Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
        True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
    )
    db.save_order_intent(first)
    db.update_order_state(first.client_order_id,OrderState.SUBMITTING.value,submitted_at=time.time())
    candidate=OrderIntent(
        "intent_candidate2","client_candidate2","decision_candidate2",instrument,
        Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
        True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
    )
    check=authority._preflight(candidate,market)
    assert check["allowed"] is False
    assert check["reason"] == "ORDER_COOLDOWN"
