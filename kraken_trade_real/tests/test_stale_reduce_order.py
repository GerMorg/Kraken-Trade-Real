from decimal import Decimal
import time
from types import SimpleNamespace

from app.domain.models import Decision, OrderIntent, Signal
from app.domain.states import Direction, OrderState
from app.execution.policy import ExecutionPolicy
from app.trading.authority import TradingAuthority
from app.trading.intent import OrderIntentBuilder

D = Decimal


class FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, code, level="INFO", **payload):
        self.events.append((code, level, payload))


class FakeReconciler:
    def reconcile(self, rows):
        payload = rows[0]
        status = str(payload.get("status", "")).lower()
        if status == "open":
            return OrderState.LIVE, str(payload.get("txid") or "")
        if status == "canceled":
            return OrderState.CANCELED, str(payload.get("txid") or "")
        if status == "closed":
            return OrderState.FILLED, str(payload.get("txid") or "")
        return OrderState.UNKNOWN_RECONCILING, str(payload.get("txid") or "")


def live_reduce_intent(instrument):
    signal = Signal(
        instrument.symbol,
        Direction.SHORT,
        D("100"),
        D("20"),
        D("0.9"),
        "TREND_DOWN",
        D("0"),
        D("0"),
        {"volatility": D("2")},
    )
    decision = Decision(
        decision_id="decision-stale-exit",
        instrument=instrument,
        signal=signal,
        target_notional_eur=D("10"),
        leverage=D("2"),
        rationale={"risk_profile": "core"},
        strategy_version="test",
        model_version="test",
        config_hash="test",
        current_position_eur=D("-20"),
        target_position_eur=D("-10"),
        execution_direction=Direction.LONG,
        reduce_only=True,
    )
    return OrderIntentBuilder(40, 45).build(
        decision,
        D("2"),
        "limit",
        D("0.01"),
        D("60000"),
        reduce_only=True,
    )


def setup_stale_order(db, instrument, config, fake_gateway, states):
    intent = live_reduce_intent(instrument)
    db.save_order_intent(intent)
    db.update_order_state(
        intent.client_order_id,
        OrderState.LIVE.value,
        kraken_order_id="O-STALE-123",
        submitted_at=time.time() - 200,
    )
    lookup_index = {"n": 0}
    cancel_calls = []

    def lookup_order(**kwargs):
        lookup_index["n"] += 1
        index = min(lookup_index["n"] - 1, len(states) - 1)
        payload = dict(states[index])
        payload.setdefault("txid", "O-STALE-123")
        return [payload]

    def cancel_order(**kwargs):
        cancel_calls.append(kwargs)
        return {"count": 1}

    fake_gateway.lookup_order = lookup_order
    fake_gateway.cancel_order = cancel_order
    audit = FakeAudit()
    authority = TradingAuthority(
        config,
        fake_gateway,
        db,
        audit,
        ExecutionPolicy(40, 2),
        FakeReconciler(),
    )
    market = SimpleNamespace(
        spread_bps=D("5"),
        closes=tuple(D("100") for _ in range(40)),
        volume_24h=D("100000"),
        metadata={},
    )
    return authority, intent, market, audit, cancel_calls


def test_stale_reduce_only_order_is_canceled_and_requires_fresh_portfolio(
    db, config, fake_gateway, instrument
):
    authority, intent, market, audit, cancel_calls = setup_stale_order(
        db,
        instrument,
        config,
        fake_gateway,
        [
            {"status": "open", "vol": "0.01", "vol_exec": "0"},
            {"status": "canceled", "vol": "0.01", "vol_exec": "0.004"},
        ],
    )

    result = authority._preflight(intent, market)

    assert result["allowed"] is False
    assert result["reason"] == "STALE_REDUCE_ORDER_RESOLVED_REFRESH_REQUIRED"
    assert len(cancel_calls) == 1
    row = db.one(
        "SELECT state,last_error FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert row["state"] == OrderState.CANCELED.value
    assert "PARTIAL_FILL" in row["last_error"]
    assert any(event[0] == "ORDER_STALE_REDUCE_CANCEL_CONFIRMED" for event in audit.events)


def test_stale_reduce_only_order_remains_blocking_when_cancel_is_not_confirmed(
    db, config, fake_gateway, instrument
):
    authority, intent, market, audit, cancel_calls = setup_stale_order(
        db,
        instrument,
        config,
        fake_gateway,
        [
            {"status": "open", "vol": "0.01", "vol_exec": "0"},
            {"status": "open", "vol": "0.01", "vol_exec": "0"},
        ],
    )

    result = authority._preflight(intent, market)

    assert result["allowed"] is False
    assert result["reason"] == "DUPLICATE_OPEN_ORDER"
    assert len(cancel_calls) == 1
    row = db.one(
        "SELECT state FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    )
    assert row["state"] == OrderState.LIVE.value
    assert any(event[0] == "ORDER_STALE_REDUCE_CANCEL_FAILED" for event in audit.events)
