from __future__ import annotations

from decimal import Decimal

from app.trading.fx import FXConversionManager
from app.domain.models import Instrument
from app.domain.states import ProductType


def _instrument(symbol, base, quote, instrument_id):
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol=symbol,
        instrument_id=instrument_id,
        altname=instrument_id,
        base=base,
        quote=quote,
        status="online",
        margin_available=False,
        long_available=True,
        short_available=False,
        leverage_levels=(Decimal("1"),),
        min_order_qty=Decimal("0.00001"),
        min_cost=Decimal("1"),
        lot_decimals=8,
        price_decimals=5,
        tick_size=Decimal("0.00001"),
        margin_class="spot",
    )


class _Portfolio:
    def __init__(self):
        self.balances = {"EUR": Decimal("100"), "USD": Decimal("2")}

    def cash_balance(self, asset):
        return self.balances.get(str(asset).upper(), Decimal("0"))

    def _ticker_raw(self, instrument):
        return {"a": ["1.10"]}


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
        self.calls = 0

    def submit_spot_order(self, **kwargs):
        self.calls += 1
        return {"txid": ["FX-1"]}

    def lookup_order(self, **kwargs):
        return [{
            "status": "closed",
            "vol": "10",
            "vol_exec": "10",
            "txid": "FX-1",
        }]


class _Config:
    execution_fx_cost_bps = 40
    execution_max_slippage_bps = 40
    execution_order_timeout_seconds = 45
    execution_max_orders_per_day = 10


def test_eur_usd_funding_executes_conversion_before_dependent_trade():
    portfolio = _Portfolio()
    db = _DB()
    gateway = _Gateway()
    manager = FXConversionManager(
        _Config(), db, _Audit(), gateway, portfolio,
        [_instrument("EUR/USD", "EUR", "USD", "ZEURZUSD")],
    )

    result = manager.ensure_quote_funds(
        quote="USD",
        required_quote=Decimal("20"),
        cycle_id="cycle_fx_test",
    )

    assert result["ready"] is True
    assert result["converted"] is True
    assert gateway.calls == 1
    assert db.rows[0].symbol == "EUR/USD"
    assert db.rows[0].quantity > Decimal("16")
    assert result["expected_cost_bps"] == "40"


def test_fx_funding_reserves_last_order_slot():
    portfolio = _Portfolio()
    db = _DB()
    db.n = 9
    gateway = _Gateway()
    result = FXConversionManager(
        _Config(), db, _Audit(), gateway, portfolio,
        [_instrument("EUR/USD", "EUR", "USD", "ZEURZUSD")],
    ).ensure_quote_funds(
        quote="USD", required_quote=Decimal("20"), cycle_id="cycle_fx_limit"
    )

    assert result["ready"] is False
    assert result["reason"] == "DAILY_ORDER_LIMIT_FX_RESERVE"
    assert gateway.calls == 0
