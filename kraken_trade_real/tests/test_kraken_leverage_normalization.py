from decimal import Decimal as D

import pytest

from app.kraken.client import KrakenError, KrakenGateway


def _gateway_with_capture():
    gateway = KrakenGateway("key", "secret")
    captured = {}

    def fake_spot_private(method, body=None):
        captured["method"] = method
        captured["body"] = body
        return {"txid": ["test-order"]}

    gateway.spot_private = fake_spot_private
    return gateway, captured


def test_margin_leverage_uses_kraken_integer_string():
    gateway, captured = _gateway_with_capture()

    result = gateway.submit_spot_order(
        instrument_id="QUSD",
        side="buy",
        order_type="market",
        quantity=D("10"),
        price=None,
        client_order_id="0123456789abcdef",
        leverage=D("4.0"),
        margin=True,
    )

    assert result["txid"] == ["test-order"]
    assert captured["method"] == "AddOrder"
    assert captured["body"]["leverage"] == "4"


def test_fractional_margin_leverage_is_rejected_before_api_call():
    gateway, captured = _gateway_with_capture()

    with pytest.raises(KrakenError, match="INVALID_MARGIN_LEVERAGE"):
        gateway.submit_spot_order(
            instrument_id="QUSD",
            side="buy",
            order_type="market",
            quantity=D("10"),
            price=None,
            client_order_id="0123456789abcdef",
            leverage=D("2.5"),
            margin=True,
        )

    assert captured == {}
