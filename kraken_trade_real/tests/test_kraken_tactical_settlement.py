from decimal import Decimal

from app.kraken.client import KrakenGateway


def test_spot_margin_reduce_only_uses_settle_position():
    gateway = KrakenGateway("", "")
    captured = {}

    def fake_private(method, params=None):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"txid": ["O-SETTLE"]}

    gateway.spot_private = fake_private
    gateway.submit_spot_order(
        instrument_id="XXBTZEUR",
        side="sell",
        order_type="limit",
        quantity=Decimal("0.01"),
        price=Decimal("60000"),
        client_order_id="11111111-2222-4333-8444-555555555570",
        leverage=Decimal("2"),
        margin=True,
        reduce_only=True,
    )

    assert captured["method"] == "AddOrder"
    assert captured["params"]["ordertype"] == "settle-position"
    assert captured["params"]["type"] == "sell"
    assert captured["params"]["leverage"] == "2"
    assert "reduce_only" not in captured["params"]
    assert "price" not in captured["params"]
