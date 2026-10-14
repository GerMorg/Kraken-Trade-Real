from decimal import Decimal

from app.domain.models import Instrument, MarketSnapshot
from app.domain.states import ProductType
from app.market.features import FeatureEngine
from app.trading.signals import SignalEngine


def make_instrument() -> Instrument:
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT_MARGIN,
        symbol="XBT/EUR",
        instrument_id="XXBTZEUR",
        altname="XBTEUR",
        base="XXBT",
        quote="ZEUR",
        status="online",
        margin_available=True,
        long_available=True,
        short_available=True,
        leverage_levels=(Decimal("1"), Decimal("2")),
        min_order_qty=Decimal("0.0001"),
        min_cost=Decimal("0.5"),
        lot_decimals=4,
        price_decimals=2,
        tick_size=Decimal("0.01"),
        margin_class="spot-margin",
        metadata={},
    )


def test_features_include_multi_horizon_returns():
    closes = tuple(Decimal("100") + Decimal(i) / Decimal("10") for i in range(250))
    snap = MarketSnapshot(
        "XBT/EUR", Decimal("124.9"), Decimal("124.8"), Decimal("125.0"),
        Decimal("100000"), 1.0, closes,
    )
    features = FeatureEngine().calculate(snap)
    assert features["return_15"] > 0
    assert features["return_60"] > 0
    assert features["return_240"] > 0


def test_core_signal_cost_includes_realistic_round_trip_fee_assumption():
    closes = tuple(Decimal("100") + Decimal(i) / Decimal("10") for i in range(250))
    snap = MarketSnapshot(
        "XBT/EUR", Decimal("124.9"), Decimal("124.8"), Decimal("125.0"),
        Decimal("100000"), 1.0, closes,
    )
    config = type(
        "Cfg",
        (),
        {
            "strategy_entry_fee_bps": 40.0,
            "strategy_exit_fee_bps": 80.0,
            "strategy_execution_overhead_bps": 8.0,
        },
    )()
    long_signal, short_signal = SignalEngine(config).evaluate(
        make_instrument(),
        snap,
        FeatureEngine().calculate(snap),
        "TREND_UP",
    )
    assert long_signal.expected_cost_bps >= Decimal("128")
    assert short_signal.expected_cost_bps == long_signal.expected_cost_bps



def test_unvalidated_news_and_gemini_effects_are_diagnostic_only():
    closes = tuple(Decimal("100") + Decimal(i) / Decimal("10") for i in range(250))
    snap = MarketSnapshot(
        "XBT/EUR", Decimal("124.9"), Decimal("124.8"), Decimal("125.0"),
        Decimal("100000"), 1.0, closes,
    )
    features = FeatureEngine().calculate(snap)
    engine = SignalEngine()
    baseline = engine.evaluate(make_instrument(), snap, features, "TREND_UP")
    with_external = engine.evaluate(
        make_instrument(), snap, features, "TREND_UP",
        news_bps=Decimal("500"), gemini_bps=Decimal("900"),
    )
    assert with_external[0].expected_return_bps == baseline[0].expected_return_bps
    assert with_external[1].expected_return_bps == baseline[1].expected_return_bps
    assert with_external[0].confidence == baseline[0].confidence
    assert with_external[0].news_effect_bps == Decimal("0")
    assert with_external[0].gemini_effect_bps == Decimal("0")
    assert with_external[0].features["diagnostic_requested_news_bps"] == Decimal("500")
    assert with_external[0].features["diagnostic_requested_gemini_bps"] == Decimal("900")
    assert with_external[0].features["external_signal_live_enabled"] == Decimal("0")
