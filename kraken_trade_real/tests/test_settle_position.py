from decimal import Decimal
import time

import pytest

from app.domain.models import Decision, MarketSnapshot, OrderIntent, Signal
from app.domain.states import Direction, OrderState
from app.execution import ExecutionPolicy, ExecutionReconciler
from app.kraken.client import KrakenError, KrakenGateway
from app.monitoring import AuditLogger
from app.trading.authority import TradingAuthority
from app.trading.intent import OrderIntentBuilder

D = Decimal


def _decision(instrument, current):
    signal = Signal(
        instrument.symbol, Direction.LONG, D("100"), D("10"), D("0.9"),
        "TREND_DOWN", D("0"), D("0"), {"volatility": D("10")},
    )
    return Decision(
        "decision-settle", instrument, signal, abs(D(str(current))), D("2"), {},
        "test", "test-model", "", D(str(current)), D("0"), Direction.LONG, True,
    )


def test_settlement_builder_uses_documented_kraken_side_for_a_short(instrument):
    decision = _decision(instrument, "-8.5")
    intent = OrderIntentBuilder(40, 45).build(
        decision, D("2"), "settle-position", D("1.2345"), None,
        reduce_only=True,
    )
    assert intent.order_type == "settle-position"
    assert intent.quantity == D("1.2345")
    assert intent.side == "sell"
    assert intent.direction == Direction.SHORT
    assert intent.reduce_only is True
    assert intent.margin is True
    assert intent.limit_price is None


def test_settlement_builder_uses_buy_side_for_a_margin_long(instrument):
    decision = _decision(instrument, "8.5")
    intent = OrderIntentBuilder(40, 45).build(
        decision, D("2"), "settle-position", D("1.2345"), None,
        reduce_only=True,
    )
    assert intent.side == "buy"
    assert intent.direction == Direction.LONG


def test_spot_gateway_sends_documented_zero_volume_settle_all_order():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-SETTLE"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="MINAZUSD", side="sell", order_type="settle-position",
        quantity=D("0"), price=None,
        client_order_id="11111111-2222-4333-8444-555555555580",
        leverage=D("2"), margin=True, reduce_only=True,
    )
    assert captured["method"] == "AddOrder"
    assert captured["params"]["ordertype"] == "settle-position"
    assert captured["params"]["volume"] == "0"
    assert captured["params"]["type"] == "sell"
    assert captured["params"]["leverage"] == "2"
    assert "reduce_only" not in captured["params"]
    assert "price" not in captured["params"]


def test_spot_gateway_rejects_negative_volume_settlement():
    gateway = KrakenGateway("", "")
    with pytest.raises(KrakenError, match="INVALID_SETTLE_POSITION_PARAMETERS"):
        gateway.submit_spot_order(
            instrument_id="MINAZUSD", side="sell", order_type="settle-position",
            quantity=D("-1"), price=None,
            client_order_id="11111111-2222-4333-8444-555555555581",
            leverage=D("2"), margin=True, reduce_only=True,
        )


def test_authority_preflight_allows_valid_settlement_below_trade_cost_minimum(
    config, db, instrument
):
    authority = TradingAuthority(
        config, object(), db, AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    market = MarketSnapshot(
        instrument.symbol, D("60005"), D("60000"), D("60010"), D("1000"),
        time.time(), tuple(D("60000") for _ in range(40)),
    )
    intent = OrderIntent(
        "intent-settle", "11111111-2222-4333-8444-555555555582",
        "decision-settle", instrument, Direction.SHORT, "sell",
        "settle-position", D("0"), None, D("2"), True, True,
        D("0"), D("40"), 45, state=OrderState.INTENT_CREATED,
    )
    check = authority._preflight(intent, market)
    assert check["allowed"] is True
