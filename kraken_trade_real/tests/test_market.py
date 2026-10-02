from decimal import Decimal

from app.domain.models import MarketSnapshot
from app.market.features import FeatureEngine
from app.market.regime import RegimeEngine


def test_features_and_regime():
    closes=tuple(Decimal(str(100+i)) for i in range(40))
    snap=MarketSnapshot("XBT/EUR",Decimal("140"),Decimal("139.9"),Decimal("140.1"),Decimal("1000"),0,closes)
    f=FeatureEngine().calculate(snap)
    assert f["return_1"]>0
    assert f["trend"]>0
    assert RegimeEngine().detect(f) in {"TREND_UP","BREAKOUT","RANGE","LOW_VOLATILITY"}
