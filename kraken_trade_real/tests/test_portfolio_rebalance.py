from decimal import Decimal

from app.domain.models import Instrument, PortfolioState, Signal
from app.domain.states import Direction, ProductType
from app.portfolio import PortfolioReconciler
from app.trading.decision import DecisionEngine


def usd_instrument() -> Instrument:
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="XBT/USD",
        instrument_id="XXBTZUSD",
        altname="XBTUSD",
        base="XXBT",
        quote="ZUSD",
        status="online",
        margin_available=False,
        long_available=True,
        short_available=False,
        leverage_levels=(Decimal("1"),),
        min_order_qty=Decimal("0.0001"),
        min_cost=Decimal("1"),
        lot_decimals=4,
        price_decimals=2,
        tick_size=Decimal("0.01"),
        margin_class="spot",
    )


def usd_eur_instrument() -> Instrument:
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="USD/EUR",
        instrument_id="ZUSDZEUR",
        altname="USDEUR",
        base="ZUSD",
        quote="ZEUR",
        status="online",
        margin_available=False,
        long_available=True,
        short_available=False,
        leverage_levels=(Decimal("1"),),
        min_order_qty=Decimal("0.01"),
        min_cost=Decimal("1"),
        lot_decimals=2,
        price_decimals=5,
        tick_size=Decimal("0.00001"),
        margin_class="spot",
    )


def test_usd_quote_is_converted_to_eur_for_order_sizing(db, fake_gateway):
    reconciler = PortfolioReconciler(fake_gateway, db)
    instruments = [usd_instrument(), usd_eur_instrument()]
    reconciler.set_market_context(
        instruments,
        {
            "XXBTZUSD": {"c": ["60000"]},
            "ZUSDZEUR": {"c": ["0.9"]},
        },
    )
    assert reconciler.quote_to_eur_rate("ZUSD") == Decimal("0.9")
    qty = reconciler.quantity_for_eur(
        instruments[0],
        Decimal("90"),
        Decimal("60000"),
    )
    assert qty == Decimal("0.001666666666666666666666666667")


def test_decision_reduces_existing_position_instead_of_ignoring_it(config, instrument):
    engine = DecisionEngine(config)
    features = {"volatility": Decimal("2")}
    long_signal = Signal(
        instrument.symbol,
        Direction.LONG,
        Decimal("20"),
        Decimal("1"),
        Decimal("0.9"),
        "TREND_UP",
        Decimal("0"),
        Decimal("0"),
        features,
    )
    short_signal = Signal(
        instrument.symbol,
        Direction.SHORT,
        Decimal("2"),
        Decimal("5"),
        Decimal("0.3"),
        "TREND_DOWN",
        Decimal("0"),
        Decimal("0"),
        features,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("80"),
        positions={instrument.symbol: Decimal("20")},
    )
    decision = engine.choose(
        instrument,
        long_signal,
        short_signal,
        portfolio,
        "baseline-v1",
        "hash",
        {},
        Decimal("0.5"),
    )
    assert decision is not None
    assert decision.current_position_eur == Decimal("20")
    assert decision.target_position_eur == Decimal("13.50")
    assert decision.execution_direction == Direction.SHORT
    assert decision.reduce_only is True
    assert decision.target_notional_eur == Decimal("6.50")


def test_reversal_flattens_before_opposite_entry(config, instrument):
    engine = DecisionEngine(config)
    features = {"volatility": Decimal("2")}
    long_signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("1"), Decimal("5"), Decimal("0.3"),
        "RANGE", Decimal("0"), Decimal("0"), features,
    )
    short_signal = Signal(
        instrument.symbol, Direction.SHORT, Decimal("20"), Decimal("1"), Decimal("0.9"),
        "TREND_DOWN", Decimal("0"), Decimal("0"), features,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("0"),
        positions={instrument.symbol: Decimal("20")},
    )
    decision = engine.choose(
        instrument, long_signal, short_signal, portfolio,
        "baseline-v1", "hash", {}, Decimal("0.5"),
    )
    assert decision is not None
    assert decision.target_position_eur == Decimal("0")
    assert decision.target_notional_eur == Decimal("20")
    assert decision.execution_direction == Direction.SHORT
    assert decision.reduce_only is True
    assert decision.rationale["reversal_to_flat"] is True
