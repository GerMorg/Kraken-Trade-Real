from __future__ import annotations

from decimal import Decimal
from app.domain.models import Instrument, MarketSnapshot, OrderIntent
from app.domain.states import Direction, OrderState, ProductType
from app.kraken.client import KrakenGateway
from app.kraken.discovery import InstrumentDiscovery
from app.market.data import MarketData
from app.monitoring import AuditLogger
from app.execution import ExecutionPolicy, ExecutionReconciler
from app.trading.authority import TradingAuthority


def test_spot_margin_short_payload_contains_leverage():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-MARGIN"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="MINAZUSD",
        side="sell",
        order_type="limit",
        quantity=Decimal("5"),
        price=Decimal("0.5"),
        client_order_id="11111111-2222-4333-8444-555555555560",
        leverage=Decimal("2"),
        margin=True,
        reduce_only=False,
    )
    assert captured["method"] == "AddOrder"
    assert captured["params"]["leverage"] == "2"
    assert captured["params"]["type"] == "sell"
    assert "margin" not in captured["params"]


def test_spot_margin_order_omits_invalid_leverage_one():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-1"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="XXSTRKZUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        price=Decimal("0.2"),
        client_order_id="11111111-2222-4333-8444-555555555555",
        leverage=Decimal("1"),
        margin=True,
    )

    assert captured["method"] == "AddOrder"
    assert "leverage" not in captured["params"]


def test_spot_xstock_order_uses_asset_class_and_supported_leverage():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-2"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="AAPLxUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.1"),
        price=Decimal("250"),
        client_order_id="11111111-2222-4333-8444-555555555556",
        leverage=Decimal("3"),
        margin=True,
        post_only=True,
        asset_class="tokenized_asset",
    )

    assert captured["params"]["asset_class"] == "tokenized_asset"
    assert captured["params"]["leverage"] == "3"
    assert captured["params"]["oflags"] == "post"
    assert "postOnly" not in captured["params"]


def test_futures_order_uses_kraken_v3_order_types():
    gateway = KrakenGateway("", "")
    calls = []

    def fake_private(method, params=None):
        calls.append((method, dict(params or {})))
        return {"result": "success"}

    gateway.futures_private = fake_private
    gateway.submit_futures_order(
        instrument_id="PF_XBTUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("10"),
        price=Decimal("60000"),
        client_order_id="11111111-2222-4333-8444-555555555557",
        post_only=True,
    )
    gateway.submit_futures_order(
        instrument_id="PF_XBTUSD",
        side="sell",
        order_type="market",
        quantity=Decimal("10"),
        price=None,
        client_order_id="11111111-2222-4333-8444-555555555558",
    )

    assert calls[0][0] == "sendorder"
    assert calls[0][1]["orderType"] == "post"
    assert "postOnly" not in calls[0][1]
    assert calls[1][1]["orderType"] == "mkt"
    assert "limitPrice" not in calls[1][1]


def test_futures_chart_request_uses_current_charts_api():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_request(url, **kwargs):
        captured["url"] = url
        return {"candles": []}

    gateway.http.request = fake_request
    gateway.futures_chart_candles(
        symbol="PF_XBTUSD",
        tick_type="trade",
        resolution="1m",
        count=250,
    )

    assert captured["url"] == (
        "https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/1m?count=250"
    )


def test_discovery_preserves_direction_specific_margin_leverage_and_boolean_tradeable():
    class Gateway:
        def public_instruments(self):
            return (
                {
                    "AAPLxUSD": {
                        "altname": "AAPLxUSD",
                        "wsname": "AAPLx/USD",
                        "base": "AAPLx",
                        "quote": "USD",
                        "status": "online",
                        "aclass_base": "tokenized_asset",
                        "leverage_buy": ["2", "3"],
                        "leverage_sell": ["2"],
                        "ordermin": "0.01",
                        "costmin": "1",
                    }
                },
                {
                    "instruments": [
                        {
                            "symbol": "PF_XBTUSD",
                            "type": "futures_vanilla",
                            "underlying": "rr_xbtusd",
                            "quoteCurrency": "USD",
                            "tradeable": True,
                            "contractSize": "0.001",
                            "minOrderSize": "5",
                            "tickSize": "0.1",
                            "marginLevels": [{"initialMargin": "0.1"}],
                        }
                    ]
                },
            )

    instruments = InstrumentDiscovery(Gateway()).discover()
    xstock = next(i for i in instruments if i.symbol == "AAPLx/USD")
    future = next(i for i in instruments if i.symbol == "PF_XBTUSD")

    assert xstock.metadata["asset_class"] == "tokenized_asset"
    assert xstock.leverage_levels == (Decimal("2"), Decimal("3"))
    assert xstock.short_available is True
    assert future.tradeable is True
    assert future.max_leverage == Decimal("10")
    assert future.min_order_qty == Decimal("5")


def test_xstock_ohlc_and_orderbook_include_asset_class():
    xstock = Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="AAPLx/USD",
        instrument_id="AAPLxUSD",
        altname="AAPLxUSD",
        base="AAPLx",
        quote="USD",
        status="online",
        margin_available=False,
        long_available=True,
        short_available=False,
        leverage_levels=(Decimal("1"),),
        min_order_qty=Decimal("0.01"),
        min_cost=Decimal("1"),
        lot_decimals=2,
        price_decimals=2,
        tick_size=Decimal("0.01"),
        margin_class="tokenized_asset",
        metadata={"asset_class": "tokenized_asset"},
    )

    class Gateway:
        def __init__(self):
            self.calls = []

        def spot_public(self, method, params=None):
            self.calls.append((method, dict(params or {})))
            if method == "OHLC":
                return {"AAPLxUSD": [
                    [1, 1, 1, 1, 249, 1, 1, 1],
                    [2, 2, 2, 2, 250, 1, 1, 1],
                ]}
            return {
                "AAPLxUSD": {
                    "bids": [["249.9", "1"]],
                    "asks": [["250.1", "1"]],
                }
            }

    gateway = Gateway()
    data = MarketData(gateway)
    assert data._spot_ohlc(xstock)
    data._orderbook(xstock)

    assert gateway.calls[0][0] == "OHLC"
    assert gateway.calls[0][1]["asset_class"] == "tokenized_asset"
    assert gateway.calls[1][0] == "Depth"
    assert gateway.calls[1][1]["asset_class"] == "tokenized_asset"


def test_authority_rejects_unsupported_spot_margin_leverage(config, db, instrument):
    from dataclasses import replace

    instrument = replace(
        instrument,
        metadata={"leverage_buy": ["2", "3"], "leverage_sell": ["2", "3"]},
        leverage_levels=(Decimal("2"), Decimal("3")),
    )
    market = MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("60000"),
        Decimal("60010"),
        Decimal("1000"),
        1.0,
        tuple(Decimal("60000") for _ in range(40)),
    )
    authority = TradingAuthority(
        config,
        object(),
        db,
        AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    intent = OrderIntent(
        "intent_bad_leverage",
        "11111111-2222-4333-8444-555555555559",
        "decision_bad_leverage",
        instrument,
        Direction.LONG,
        "buy",
        "limit",
        Decimal("0.001"),
        Decimal("60000"),
        Decimal("2.5"),
        True,
        False,
        Decimal("30"),
        Decimal("40"),
        45,
        state=OrderState.INTENT_CREATED,
    )

    check = authority._preflight(intent, market)
    assert check["allowed"] is False
    assert check["reason"] == "LEVERAGE_UNSUPPORTED_BY_INSTRUMENT"
    assert check["detail"]["supported"] == ["2", "3"]


def test_authority_rejects_unleveraged_spot_margin_short(config, db, instrument):
    from app.domain.models import MarketSnapshot, OrderIntent
    from app.execution import ExecutionPolicy, ExecutionReconciler
    from app.monitoring import AuditLogger
    from app.trading.authority import TradingAuthority

    authority = TradingAuthority(
        config,
        object(),
        db,
        AuditLogger(False),
        ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices),
        ExecutionReconciler(),
    )
    intent = OrderIntent(
        "intent_margin_short_unleveraged",
        "11111111-2222-4333-8444-555555555562",
        "decision_margin_short_unleveraged",
        instrument,
        Direction.SHORT,
        "sell",
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
    market = MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("60000"),
        Decimal("60010"),
        Decimal("1000"),
        1.0,
        tuple(Decimal("60000") for _ in range(40)),
    )
    check = authority._preflight(intent, market)
    assert check["allowed"] is False
    assert check["reason"] == "SPOT_MARGIN_SHORT_REQUIRES_LEVERAGE"


def test_spot_lookup_order_uses_kraken_order_id_not_client_order_id():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"O-HISTORICAL": {"status": "closed", "vol": "1", "vol_exec": "1"}}

    gateway.spot_private = fake_private
    instrument = Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="BTC/USD",
        instrument_id="XXBTZUSD",
        altname="XBTUSD",
        base="BTC",
        quote="USD",
        status="online",
        margin_available=False,
        long_available=True,
        short_available=False,
        leverage_levels=(Decimal("1"),),
        min_order_qty=Decimal("0.0001"),
        min_cost=Decimal("1"),
        lot_decimals=8,
        price_decimals=2,
        tick_size=Decimal("0.01"),
        margin_class="spot",
        metadata={},
    )

    found = gateway.lookup_order(
        client_order_id="11111111-2222-4333-8444-555555555563",
        instrument=instrument,
        kraken_order_id="O-HISTORICAL",
    )

    assert captured["method"] == "QueryOrders"
    assert captured["params"] == {"txid": "O-HISTORICAL"}
    assert found[0]["txid"] == "O-HISTORICAL"


def test_spot_intent_marks_leveraged_orders_as_margin_funded(config, instrument):
    from app.domain.models import Decision, Signal
    from app.trading.intent import OrderIntentBuilder

    signal = Signal(
        instrument.symbol,
        Direction.SHORT,
        Decimal("100"),
        Decimal("20"),
        Decimal("0.9"),
        "TREND",
        Decimal("0"),
        Decimal("0"),
        {"volatility": Decimal("5")},
    )
    decision = Decision(
        "decision_margin_intent",
        instrument,
        signal,
        Decimal("100"),
        Decimal("2"),
        {},
        "test",
        "test",
        "test",
        current_position_eur=Decimal("0"),
        target_position_eur=Decimal("-100"),
        execution_direction=Direction.SHORT,
    )
    intent = OrderIntentBuilder(40, 45).build(
        decision,
        Decimal("2"),
        "limit",
        Decimal("1"),
        Decimal("10"),
    )
    assert intent.margin is True
    assert intent.leverage == Decimal("2")


def test_spot_cash_intent_remains_non_margin(config, instrument):
    from app.domain.models import Decision, Signal
    from app.trading.intent import OrderIntentBuilder

    signal = Signal(
        instrument.symbol,
        Direction.LONG,
        Decimal("100"),
        Decimal("20"),
        Decimal("0.9"),
        "TREND",
        Decimal("0"),
        Decimal("0"),
        {"volatility": Decimal("5")},
    )
    decision = Decision(
        "decision_spot_intent",
        instrument,
        signal,
        Decimal("100"),
        Decimal("1"),
        {},
        "test",
        "test",
        "test",
        current_position_eur=Decimal("0"),
        target_position_eur=Decimal("100"),
        execution_direction=Direction.LONG,
    )
    intent = OrderIntentBuilder(40, 45).build(
        decision,
        Decimal("1"),
        "limit",
        Decimal("1"),
        Decimal("10"),
    )
    assert intent.margin is False


def test_valid_reduce_only_exit_bypasses_entry_only_margin_and_loss_gates(
    config, instrument
):
    from dataclasses import replace
    from app.domain.models import Decision, PortfolioState, Signal
    from app.risk.engine import RiskEngine
    from app.risk.margin import MarginEngine
    from app.risk.leverage import LeverageEngine
    from app.execution import CostModel

    leveraged = replace(
        instrument,
        long_available=True,
        short_available=True,
        margin_available=True,
        leverage_levels=(Decimal("2"), Decimal("3")),
    )
    signal = Signal(
        leveraged.symbol,
        Direction.LONG,
        Decimal("0"),
        Decimal("100"),
        Decimal("0.1"),
        "RISK_OFF",
        Decimal("0"),
        Decimal("0"),
        {"volatility": Decimal("999")},
    )
    decision = Decision(
        "decision_reduce_only",
        leveraged,
        signal,
        Decimal("0"),
        Decimal("2"),
        {"risk_profile": "core"},
        "test",
        "test",
        "test",
        current_position_eur=Decimal("-100"),
        target_position_eur=Decimal("0"),
        execution_direction=Direction.LONG,
        reduce_only=True,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("0"),
        positions={leveraged.symbol: Decimal("-100")},
        gross_eur=Decimal("100"),
        net_eur=Decimal("-100"),
        daily_pnl_eur=Decimal("-100"),
        drawdown_pct=Decimal("99"),
    )
    risk = RiskEngine(config, MarginEngine(), LeverageEngine(), CostModel())
    result = risk.evaluate(decision, portfolio, object(), margin_account=None)
    assert result.allowed is True
    assert result.checks["reduce_only_semantics"] is True


def test_reduce_only_cannot_flip_or_increase_a_position(config, instrument):
    from app.domain.models import Decision, PortfolioState, Signal
    from app.risk.engine import RiskEngine
    from app.risk.margin import MarginEngine
    from app.risk.leverage import LeverageEngine
    from app.execution import CostModel

    signal = Signal(
        instrument.symbol,
        Direction.LONG,
        Decimal("100"),
        Decimal("20"),
        Decimal("0.9"),
        "TREND",
        Decimal("0"),
        Decimal("0"),
        {"volatility": Decimal("5")},
    )
    decision = Decision(
        "decision_invalid_reduce_only",
        instrument,
        signal,
        Decimal("10"),
        Decimal("1"),
        {},
        "test",
        "test",
        "test",
        current_position_eur=Decimal("-100"),
        target_position_eur=Decimal("10"),
        execution_direction=Direction.LONG,
        reduce_only=True,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("1000"),
        cash_eur=Decimal("500"),
        positions={instrument.symbol: Decimal("-100")},
        gross_eur=Decimal("100"),
        net_eur=Decimal("-100"),
    )
    risk = RiskEngine(config, MarginEngine(), LeverageEngine(), CostModel())
    result = risk.evaluate(decision, portfolio, object(), margin_account=None)
    assert result.allowed is False
    assert result.checks["reduce_only_semantics"] is False


def test_spot_market_order_omits_price_even_if_a_quote_hint_was_passed():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-MARKET"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="XXBTZUSD", side="buy", order_type="market",
        quantity=Decimal("0.00123456789"), price=Decimal("60000"),
        client_order_id="11111111-2222-4333-8444-555555555564",
    )
    assert captured["method"] == "AddOrder"
    assert captured["params"]["ordertype"] == "market"
    assert "price" not in captured["params"]


def test_gateway_rejects_limit_orders_without_required_price():
    import pytest
    from app.kraken.client import KrakenError

    gateway = KrakenGateway("", "")
    gateway.spot_private = lambda *args, **kwargs: {"txid": ["SHOULD-NOT-HAPPEN"]}
    with pytest.raises(KrakenError, match="SPOT_LIMIT_ORDER_REQUIRES_POSITIVE_PRICE"):
        gateway.submit_spot_order(
            instrument_id="XXBTZUSD", side="buy", order_type="limit",
            quantity=Decimal("0.001"), price=None,
            client_order_id="11111111-2222-4333-8444-555555555565",
        )


def test_spot_discovery_preserves_zero_lot_and_price_decimals():
    class Gateway:
        def public_instruments(self):
            return (
                {
                    "ZEROUSD": {
                        "altname": "ZEROUSD", "wsname": "ZERO/USD",
                        "base": "ZERO", "quote": "ZUSD", "status": "online",
                        "ordermin": "1", "costmin": "1",
                        "lot_decimals": 0, "pair_decimals": 0,
                    }
                },
                {"instruments": []},
            )

    item = next(i for i in InstrumentDiscovery(Gateway()).discover() if i.symbol == "ZERO/USD")
    assert item.lot_decimals == 0
    assert item.price_decimals == 0
    assert item.tick_size == Decimal("1")


def test_futures_rejected_send_status_is_not_recorded_as_acknowledged(config, db, instrument):
    from dataclasses import replace

    derivative = Instrument(
        venue="futures", product_type=ProductType.DERIVATIVE,
        symbol="PF_XBTUSD", instrument_id="PF_XBTUSD", altname="PF_XBTUSD",
        base="XBT", quote="USD", status="online",
        margin_available=True, long_available=True, short_available=True,
        leverage_levels=(Decimal("1"), Decimal("3")), min_order_qty=Decimal("1"),
        min_cost=Decimal("1"), lot_decimals=8, price_decimals=1,
        tick_size=Decimal("0.1"), margin_class="USD", metadata={},
    )

    class Gateway:
        def submit_futures_order(self, **kwargs):
            return {
                "result": "success",
                "sendStatus": {
                    "status": "rejected", "order_id": "FUT-REJECTED",
                    "rejectionReason": "invalidArgument",
                },
            }

    live_config = replace(config, live_enabled=True, kill_switch=False)
    authority = TradingAuthority(
        live_config, Gateway(), db, AuditLogger(False),
        ExecutionPolicy(live_config.execution_max_slippage_bps, live_config.execution_max_reprices),
        ExecutionReconciler(),
    )
    intent = OrderIntent(
        "intent_future_rejected", "11111111-2222-4333-8444-555555555566",
        "decision_future_rejected", derivative, Direction.LONG, "buy", "limit",
        Decimal("10"), Decimal("60000"), Decimal("1"), False, False,
        Decimal("500"), Decimal("40"), 45, state=OrderState.INTENT_CREATED,
    )
    market = MarketSnapshot(
        derivative.symbol, Decimal("60000"), Decimal("59999.9"),
        Decimal("60000.1"), Decimal("1000000"), 1.0,
        tuple(Decimal("60000") for _ in range(40)),
    )
    result = authority.submit(intent, market)
    row = db.one("SELECT state,last_error FROM orders WHERE client_order_id=?",
                 (intent.client_order_id,))
    assert result["state"] == OrderState.REJECTED.value
    assert result["reason"] == "KRAKEN_ORDER_REJECTED"
    assert row is not None
    assert row["state"] == OrderState.REJECTED.value
    assert "invalidArgument" in row["last_error"]


def test_builder_normalizes_market_price_and_spot_lot_precision(config, instrument):
    from app.domain.models import Decision, Signal
    from app.trading.intent import OrderIntentBuilder

    signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("100"), Decimal("20"),
        Decimal("0.9"), "TEST", Decimal("0"), Decimal("0"), {},
    )
    decision = Decision(
        "decision-market-normalization", instrument, signal, Decimal("10"),
        Decimal("1"), {}, "test", "test", "test",
        execution_direction=Direction.LONG,
    )
    intent = OrderIntentBuilder(40, 45).build(
        decision, Decimal("1"), "market", Decimal("0.00123456"),
        Decimal("60000"), reduce_only=False,
    )
    assert intent.quantity == Decimal("0.0012")
    assert intent.limit_price is None
