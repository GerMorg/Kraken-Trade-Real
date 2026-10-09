from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.models import Instrument
from app.domain.states import ProductType
from app.domain.symbols import resolve_instrument_symbol
from app.execution import CostModel
from app.kraken.client import KrakenError, KrakenGateway


D = Decimal


def make_instrument() -> Instrument:
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="MINA/USD",
        instrument_id="MINAZUSD",
        altname="MINAUSD",
        base="MINA",
        quote="USD",
        status="online",
        margin_available=True,
        long_available=True,
        short_available=True,
        leverage_levels=(D("1"), D("2"), D("3")),
        min_order_qty=D("1"),
        min_cost=D("1"),
        lot_decimals=4,
        price_decimals=4,
        tick_size=D("0.0001"),
        margin_class="spot",
        metadata={},
    )


def test_kraken_margin_position_altname_resolves_to_canonical_symbol():
    instrument = make_instrument()
    assert resolve_instrument_symbol("MINAUSD", [instrument]) is instrument
    assert resolve_instrument_symbol("MINAZUSD", [instrument]) is instrument
    assert resolve_instrument_symbol("MINA/USD", [instrument]) is instrument
    assert resolve_instrument_symbol("UNKNOWNUSD", [instrument]) is None


def test_spot_margin_reduce_only_requires_real_leverage():
    gateway = KrakenGateway("", "")
    gateway.spot_private = lambda method, params=None: {"txid": ["O-MARGIN"]}

    with pytest.raises(KrakenError, match="INVALID_REDUCE_ONLY_REQUIRES_LEVERAGED_MARGIN_ORDER"):
        gateway.submit_spot_order(
            instrument_id="MINAZUSD",
            side="buy",
            order_type="limit",
            quantity=D("5"),
            price=D("0.5"),
            client_order_id="11111111-2222-4333-8444-555555555560",
            leverage=D("1"),
            margin=True,
            reduce_only=True,
        )


def test_leveraged_spot_margin_reduce_only_is_sent_to_kraken():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured.update(params or {})
        return {"txid": ["O-MARGIN-CLOSE"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="MINAZUSD",
        side="buy",
        order_type="limit",
        quantity=D("5"),
        price=D("0.5"),
        client_order_id="11111111-2222-4333-8444-555555555561",
        leverage=D("2"),
        margin=True,
        reduce_only=True,
    )
    assert captured["leverage"] == "2"
    assert captured["reduce_only"] == "true"


def test_cost_model_charges_margin_opening_and_four_hour_rollover():
    market = SimpleNamespace(
        spread_bps=D("5"),
        volume_24h=D("1000000"),
        metadata={},
    )
    model = CostModel()

    spot = model.estimate(market, D("100"), leverage=D("1"), holding_hours=D("8"))
    margin_before_rollover = model.estimate(
        market, D("100"), leverage=D("2"), holding_hours=D("3")
    )
    margin_four_hours = model.estimate(
        market, D("100"), leverage=D("2"), holding_hours=D("4")
    )
    margin_eight_hours = model.estimate(
        market, D("100"), leverage=D("2"), holding_hours=D("8")
    )

    assert spot.financing_bps == D("0")
    assert margin_before_rollover.financing_bps == D("1")
    assert margin_four_hours.financing_bps == D("2")
    assert margin_eight_hours.financing_bps == D("3")


def test_cost_model_uses_observed_market_specific_margin_rates():
    market = SimpleNamespace(
        spread_bps=D("5"),
        volume_24h=D("1000000"),
        metadata={
            "margin_open_fee_bps": "4",
            "margin_rollover_fee_bps": "6",
        },
    )
    estimate = CostModel().estimate(
        market, D("100"), leverage=D("2"), holding_hours=D("8")
    )
    assert estimate.financing_bps == D("8")
