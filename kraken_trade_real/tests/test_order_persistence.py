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
        db.update_order_state(
            intent.client_order_id,
            OrderState.REJECTED.value,
            submitted_at=time.time(),
            last_error="TEST_REJECTED",
        )
    candidate=OrderIntent(
        "intent_candidate","client_candidate","decision_candidate",instrument,
        Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
        True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
    )
    check=authority._preflight(candidate,market)
    assert check["allowed"] is True

    # An exchange-accepted submission must consume the daily budget even before fill;
    # a deterministic rejection must not.
    for i in range(config.execution_max_orders_per_day):
        intent=OrderIntent(
            f"intent_accepted_{i}",f"client_accepted_{i}",f"decision_accepted_{i}",instrument,
            Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
            True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
        )
        db.save_order_intent(intent)
        db.update_order_state(
            intent.client_order_id,
            OrderState.CANCELED.value,
            submitted_at=time.time(),
        )

    blocked=authority._preflight(
        OrderIntent(
            "intent_after_budget","client_after_budget","decision_after_budget",instrument,
            Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
            True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
        ),
        market,
    )
    assert blocked["allowed"] is False
    assert blocked["reason"] == "DAILY_ORDER_LIMIT"


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
    db.update_order_state(first.client_order_id,OrderState.CANCELED.value,submitted_at=time.time(),last_error="TEST_SUBMITTED")
    candidate=OrderIntent(
        "intent_candidate2","client_candidate2","decision_candidate2",instrument,
        Direction.LONG,"buy","limit",Decimal("0.001"),Decimal("60000"),Decimal("1"),
        True,False,Decimal("30"),Decimal("40"),45,state=OrderState.INTENT_CREATED,
    )
    check=authority._preflight(candidate,market)
    assert check["allowed"] is False
    assert check["reason"] == "ORDER_COOLDOWN"

def test_stale_unknown_order_is_reconciled_to_exchange_confirmed_no_order(
    config, db, instrument
):
    from app.domain.models import MarketSnapshot
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    class Gateway:
        def lookup_order(self, **kwargs):
            return []

    MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("60000"),
        Decimal("60010"),
        Decimal("1000"),
        time.time(),
        tuple(Decimal("60000") for _ in range(40)),
    )
    intent = OrderIntent(
        "intent_unknown",
        "client_unknown",
        "decision_unknown",
        instrument,
        Direction.LONG,
        "buy",
        "limit",
        Decimal("0.001"),
        Decimal("60000"),
        Decimal("1"),
        True,
        False,
        Decimal("30"),
        Decimal("40"),
        45,
        state=OrderState.UNKNOWN_RECONCILING,
    )
    db.save_order_intent(intent)
    db.update_order_state(
        intent.client_order_id,
        OrderState.UNKNOWN_RECONCILING.value,
        submitted_at=time.time() - 120,
        last_error="NETWORK",
    )

    from dataclasses import replace

    test_config = replace(config, live_enabled=True, kill_switch=False)
    authority = TradingAuthority(
        test_config,
        Gateway(),
        db,
        AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    stats = authority.reconcile_pending([instrument])

    row = db.one(
        "SELECT state,last_error FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert stats["resolved"] == 1
    assert row is not None
    assert row["state"] == OrderState.REJECTED.value
    assert row["last_error"] == "EXCHANGE_CONFIRMED_NO_ORDER"


def test_unknown_order_with_exchange_open_state_becomes_live(config, db, instrument):
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    class Gateway:
        def lookup_order(self, **kwargs):
            return [{"status": "open", "txid": "O-123"}]

    intent = OrderIntent(
        "intent_open",
        "client_open",
        "decision_open",
        instrument,
        Direction.LONG,
        "buy",
        "limit",
        Decimal("0.001"),
        Decimal("60000"),
        Decimal("1"),
        True,
        False,
        Decimal("30"),
        Decimal("40"),
        45,
        state=OrderState.UNKNOWN_RECONCILING,
    )
    db.save_order_intent(intent)
    db.update_order_state(
        intent.client_order_id,
        OrderState.UNKNOWN_RECONCILING.value,
        submitted_at=time.time() - 120,
    )

    authority = TradingAuthority(
        config,
        Gateway(),
        db,
        AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    stats = authority.reconcile_pending([instrument])

    row = db.one(
        "SELECT state,kraken_order_id FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert stats["resolved"] == 1
    assert row is not None
    assert row["state"] == OrderState.LIVE.value
    assert row["kraken_order_id"] == "O-123"

def test_deterministic_kraken_error_does_not_create_unknown_gate(config, db, instrument):
    from app.domain.models import MarketSnapshot
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.kraken.client import KrakenError
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    market = MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("60000"),
        Decimal("60010"),
        Decimal("1000"),
        time.time(),
        tuple(Decimal("60000") for _ in range(40)),
    )

    class Gateway:
        def submit_spot_order(self, **kwargs):
            raise KrakenError("KRAKEN_PRIVATE:EOrder:Insufficient funds")

    from dataclasses import replace

    test_config = replace(config, live_enabled=True, kill_switch=False)
    authority = TradingAuthority(
        test_config,
        Gateway(),
        db,
        AuditLogger(False),
        ExecutionPolicy(
            test_config.execution_max_slippage_bps,
            test_config.execution_max_reprices,
        ),
        ExecutionReconciler(),
    )
    intent = OrderIntent(
        "intent_deterministic_error",
        "11111111-2222-4333-8444-555555555555",
        "decision_deterministic_error",
        instrument,
        Direction.LONG,
        "buy",
        "limit",
        Decimal("0.001"),
        Decimal("60000"),
        Decimal("1"),
        True,
        False,
        Decimal("30"),
        Decimal("40"),
        45,
        state=OrderState.INTENT_CREATED,
    )

    result = authority.submit(intent, market)
    row = db.one(
        "SELECT state,last_error FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    )

    assert result["state"] == OrderState.REJECTED.value
    assert result["reason"] == "KRAKEN_ORDER_REJECTED"
    assert row is not None
    assert row["state"] == OrderState.REJECTED.value
    assert "Insufficient funds" in row["last_error"]


def test_new_client_order_id_matches_kraken_supported_format():
    import uuid

    from app.domain.models import is_valid_kraken_client_order_id, new_client_order_id

    client_order_id = new_client_order_id()

    assert str(uuid.UUID(client_order_id)) == client_order_id
    assert is_valid_kraken_client_order_id(client_order_id)
    assert len(client_order_id) == 36


def test_legacy_invalid_client_order_id_is_cleared_from_unknown_gate(
    config, db, instrument
):
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    class Gateway:
        def lookup_order(self, **kwargs):
            raise AssertionError("invalid legacy client IDs must not be queried")

    intent = OrderIntent(
        "intent_legacy_invalid",
        "client_ceb235528b7640c6a00c0b215c1e0ddb",
        "decision_legacy_invalid",
        instrument,
        Direction.LONG,
        "buy",
        "limit",
        Decimal("0.001"),
        Decimal("60000"),
        Decimal("1"),
        True,
        False,
        Decimal("30"),
        Decimal("40"),
        45,
        state=OrderState.UNKNOWN_RECONCILING,
    )
    db.save_order_intent(intent)
    db.update_order_state(
        intent.client_order_id,
        OrderState.UNKNOWN_RECONCILING.value,
        submitted_at=time.time() - 120,
        last_error="KRAKEN_PRIVATE:EGeneral:Invalid arguments",
    )

    authority = TradingAuthority(
        config,
        Gateway(),
        db,
        AuditLogger(False),
        ExecutionPolicy(
            config.execution_max_slippage_bps,
            config.execution_max_reprices,
        ),
        ExecutionReconciler(),
    )

    stats = authority.reconcile_pending([instrument])

    row = db.one(
        "SELECT state,last_error FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert stats["resolved"] == 1
    assert stats["still_unknown"] == 0
    assert row is not None
    assert row["state"] == OrderState.REJECTED.value
    assert row["last_error"] == "INVALID_CLIENT_ORDER_ID_LEGACY"
