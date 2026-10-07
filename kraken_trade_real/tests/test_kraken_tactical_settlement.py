from decimal import Decimal

from app.kraken.client import KrakenGateway


def test_spot_margin_reduce_only_remains_a_reduce_only_limit_order():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-REDUCE"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="XXBTZEUR",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.01"),
        price=Decimal("60000"),
        client_order_id="11111111-2222-4333-8444-555555555570",
        leverage=Decimal("2"),
        margin=True,
        reduce_only=True,
    )

    assert captured["method"] == "AddOrder"
    assert captured["params"]["ordertype"] == "limit"
    assert captured["params"]["type"] == "buy"
    assert captured["params"]["leverage"] == "2"
    assert captured["params"]["reduce_only"] == "true"
    assert captured["params"]["price"] == "60000"
