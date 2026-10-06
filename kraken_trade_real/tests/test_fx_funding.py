from __future__ import annotations

from decimal import Decimal

from app.trading.fx import FXConversionManager
from app.domain.models import Instrument
from app.domain.states import ProductType, OrderState


def _instrument(symbol, base, quote, instrument_id, min_cost="1"):
    return Instrument(
        venue="spot", product_type=ProductType.SPOT, symbol=symbol,
        instrument_id=instrument_id, altname=instrument_id, base=base, quote=quote,
        status="online", margin_available=False, long_available=True, short_available=False,
        leverage_levels=(Decimal("1"),), min_order_qty=Decimal("0.00001"),
        min_cost=Decimal(min_cost), lot_decimals=8, price_decimals=5,
        tick_size=Decimal("0.00001"), margin_class="spot",
    )


class _Portfolio:
    def __init__(self):
        self.balances = {"EUR": Decimal("100"), "USD": Decimal("2"), "BTC": Decimal("1")}

    def cash_balance(self, asset):
        return self.balances.get(str(asset).upper(), Decimal("0"))

    def _ticker_raw(self, instrument):
        if instrument.symbol == "EUR/USD":
            return {"a": ["1.10"], "b": ["1.099"]}
        if instrument.symbol == "BTC/USD":
            return {"a": ["50000"], "b": ["49900"]}
        if instrument.symbol == "BTC/EUR":
            return {"a": ["45500"], "b": ["45400"]}
        return {"a": ["1.10"], "b": ["1.09"]}


class _DB:
    def __init__(self):
        self.rows = []
        self.n = 0

    def one(self, *_):
        return {"n": self.n}

    def save_order_intent(self, intent):
        self.rows.append(intent)

    def update_order_state(self, *args, **kwargs):
        pass


class _Audit:
    def emit(self, *_args, **_kwargs):
        pass


class _Gateway:
    def __init__(self):
        self.calls = []

    def spot_balance(self):
        return {"ZEUR": "0", "ZUSD": "2", "XXBT": "1"}

    def submit_spot_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"txid": [f"FX-{len(self.calls)}"]}

    def lookup_order(self, **kwargs):
        return [{"status": "closed", "vol": "100", "vol_exec": "100", "txid": "FX-1"}]


class _Authority:
    def __init__(self, gateway):
        self.gateway = gateway

    def submit_funding_order(self, intent, timeout_seconds=30.0):
        return {"state": OrderState.FILLED.value, "kraken_order_id": "FX-1"}


class _Config:
    execution_fx_cost_bps = 40
    execution_max_slippage_bps = 40
    execution_order_timeout_seconds = 45
    execution_max_orders_per_day = 10
    execution_allow_position_funding = True
    execution_fx_position_switch_min_edge_bps = 250


def _manager(portfolio=None, db=None, gateway=None, instruments=None):
    portfolio = portfolio or _Portfolio()
    db = db or _DB()
    gateway = gateway or _Gateway()
    authority = _Authority(gateway)
    return FXConversionManager(_Config(), db, _Audit(), authority, portfolio, instruments), db, gateway


def test_eur_usd_funding_executes_conversion():
    manager, db, _ = _manager(
        instruments=[_instrument("EUR/USD", "EUR", "USD", "ZEURZUSD")]
    )
    result = manager.ensure_quote_funds(
        quote="USD", required_quote=Decimal("20"), cycle_id="cycle_fx_test",
        dependent_edge_bps=Decimal("500"),
    )
    assert result["ready"] is True
    assert result["converted"] is True
    assert db.rows[0].instrument.symbol == "EUR/USD"
    assert db.rows[0].quantity > Decimal("16")


def test_usd_to_eur_conversion_is_supported():
    portfolio = _Portfolio()
    portfolio.balances = {"EUR": Decimal("1"), "USD": Decimal("100")}
    manager, db, _ = _manager(
        portfolio=portfolio,
        instruments=[_instrument("EUR/USD", "EUR", "USD", "ZEURZUSD")],
    )
    result = manager.ensure_quote_funds(
        quote="EUR", required_quote=Decimal("20"), cycle_id="cycle_reverse",
        dependent_edge_bps=Decimal("500"),
    )
    assert result["ready"] is True
    assert result["converted"] is True
    assert db.rows[0].instrument.symbol == "EUR/USD"
    assert db.rows[0].side == "buy"
    assert db.rows[0].quantity >= Decimal("20")


def test_existing_position_can_fund_exceptionally_strong_trade():
    portfolio = _Portfolio()
    portfolio.balances = {"EUR": Decimal("0"), "USD": Decimal("0"), "BTC": Decimal("1")}
    manager, db, gateway = _manager(
        portfolio=portfolio,
        instruments=[
            _instrument("BTC/USD", "BTC", "USD", "XXBTZUSD"),
            _instrument("EUR/USD", "EUR", "USD", "ZEURZUSD"),
        ],
    )
    result = manager.ensure_quote_funds(
        quote="USD", required_quote=Decimal("1000"), cycle_id="cycle_position",
        dependent_edge_bps=Decimal("600"), protected_symbol="ETH/USD",
    )
    assert result["ready"] is True
    assert result["source_type"] == "POSITION"
    assert db.rows[0].instrument.symbol == "BTC/USD"
    assert db.rows[0].side == "sell"


def test_position_funding_is_blocked_when_edge_is_not_high_enough():
    portfolio = _Portfolio()
    portfolio.balances = {"EUR": Decimal("0"), "USD": Decimal("0"), "BTC": Decimal("1")}
    manager, _, gateway = _manager(
        portfolio=portfolio,
        instruments=[_instrument("BTC/USD", "BTC", "USD", "XXBTZUSD")],
    )
    result = manager.ensure_quote_funds(
        quote="USD", required_quote=Decimal("1000"), cycle_id="cycle_position_low_edge",
        dependent_edge_bps=Decimal("100"),
    )
    assert result["ready"] is False
    assert result["reason"] == "POSITION_FUNDING_EDGE_TOO_LOW"
    assert gateway.calls == []


def test_fx_funding_reserves_last_order_slot():
    portfolio = _Portfolio()
    db = _DB()
    db.n = 9
    manager, _, gateway = _manager(
        portfolio=portfolio, db=db,
        instruments=[_instrument("EUR/USD", "EUR", "USD", "ZEURZUSD")],
    )
    result = manager.ensure_quote_funds(
        quote="USD", required_quote=Decimal("20"), cycle_id="cycle_fx_limit",
        dependent_edge_bps=Decimal("500"),
    )
    assert result["ready"] is False
    assert result["reason"] == "DAILY_ORDER_LIMIT_FX_RESERVE"
    assert gateway.calls == []
