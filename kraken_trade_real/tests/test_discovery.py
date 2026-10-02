from decimal import Decimal

from app.domain.states import ProductType
from app.kraken.discovery import InstrumentDiscovery


def test_discovery_uses_kraken_ids_and_detects_margin_and_derivatives(fake_gateway):
    instruments=InstrumentDiscovery(fake_gateway).discover()
    spot=next(x for x in instruments if x.venue=="spot")
    futures=next(x for x in instruments if x.venue=="futures")
    assert spot.instrument_id=="XXBTZEUR"
    assert spot.product_type is ProductType.SPOT_MARGIN
    assert spot.short_available is True
    assert futures.product_type is ProductType.DERIVATIVE
    assert futures.max_leverage==Decimal("5")
