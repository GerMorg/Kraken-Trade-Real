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


def test_ticker_only_snapshot_skips_history_and_orderbook(instrument):
    calls = []

    class Gateway:
        def spot_public(self, method, params=None):
            calls.append((method, params))
            raise AssertionError("ticker-only snapshot must not call Spot public history/orderbook")

    from app.market.data import MarketData

    snap = MarketData(Gateway()).snapshot(
        instrument,
        {
            "XXBTZEUR": {
                "b": ["60000"],
                "a": ["60010"],
                "c": ["60005"],
                "v": ["10", "1000"],
            }
        },
        include_history=False,
        include_orderbook=False,
    )

    assert snap is not None
    assert snap.closes == ()
    assert snap.depths_bid == ()
    assert snap.depths_ask == ()
    assert calls == []


def test_market_scanner_supports_ticker_prefilter_without_history(instrument):
    snap = MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("60000"),
        Decimal("60010"),
        Decimal("1000"),
        __import__("time").time(),
        (),
    )
    scanner = __import__("app.market.scanner", fromlist=["MarketScanner"]).MarketScanner(
        1, 100, 60
    )

    assert scanner.fast_filter(
        [instrument], {instrument.symbol: snap}, require_history=False
    ) == [instrument]
    assert scanner.fast_filter(
        [instrument], {instrument.symbol: snap}, require_history=True
    ) == []


def test_market_history_cache_round_trip(db):
    from time import time
    closes = (Decimal("1"), Decimal("2"), Decimal("3"))
    captured_at = time()
    db.save_market_history_cache("XBT/EUR", captured_at, closes)
    cached = db.latest_market_closes(["XBT/EUR"])
    assert cached["XBT/EUR"][0] == captured_at
    assert cached["XBT/EUR"][1] == closes


def test_market_selection_deduplicates_eur_usd_quote_pairs(instrument):
    from dataclasses import replace
    from app.market.scanner import MarketScanner
    eur=instrument
    usd=replace(instrument,symbol="XBT/USD",instrument_id="XXBTZUSD",quote="USD")
    eth=replace(instrument,symbol="ETH/EUR",instrument_id="XETHZEUR",base="XETH",quote="EUR")
    selected,removed=MarketScanner(1,100,60).select_for_cycle([usd,eur,eth],limit=3)
    assert {item.symbol for item in selected} == {"XBT/USD","ETH/EUR"}
    assert removed == 1


def test_market_selection_always_preserves_held_position(instrument):
    from dataclasses import replace
    from app.market.scanner import MarketScanner
    usd=replace(instrument,symbol="XBT/USD",instrument_id="XXBTZUSD",quote="USD")
    selected,removed=MarketScanner(1,100,60).select_for_cycle(
        [usd,instrument],limit=1,preserve_symbols={"XBT/EUR"}
    )
    assert [item.symbol for item in selected] == ["XBT/EUR"]
    assert removed == 1

def test_product_family_classifies_xstocks_and_crypto(instrument):
    from dataclasses import replace
    from app.market.scanner import MarketScanner

    scanner = MarketScanner(1, 100, 60)
    xstock = replace(instrument, symbol="AAPLx/EUR", base="AAPLx")
    crypto = replace(instrument, symbol="STX/EUR", base="STX")

    assert scanner.product_family(xstock) == "XSTOCK"
    assert scanner.product_family(crypto) == "CRYPTO"


def test_history_candidate_exploration_reaches_non_core_family(instrument):
    from dataclasses import replace
    from app.market.scanner import MarketScanner

    scanner = MarketScanner(1, 100, 60)
    core = [
        replace(
            instrument,
            symbol=f"C{i}/EUR",
            instrument_id=f"C{i}EUR",
            base=f"C{i}",
        )
        for i in range(200)
    ]
    xstock = replace(
        instrument,
        symbol="AAPLx/EUR",
        instrument_id="AAPLxEUR",
        base="AAPLx",
    )

    candidates = scanner.build_history_candidates(
        core + [xstock],
        core_limit=200,
        exploration_limit=10,
        exploration_slots_per_family=1,
        cycle_key="test-cycle",
    )

    assert len(candidates) == 201
    assert xstock in candidates


def test_detailed_selection_keeps_family_coverage_inside_limit(instrument):
    from dataclasses import replace
    from app.market.scanner import MarketScanner
    from app.domain.states import ProductType

    scanner = MarketScanner(1, 100, 60)
    xstock = replace(
        instrument,
        symbol="AAPLx/EUR",
        instrument_id="AAPLxEUR",
        base="AAPLx",
    )
    derivative = replace(
        instrument,
        venue="futures",
        product_type=ProductType.DERIVATIVE,
        symbol="PF_XBTUSD",
        instrument_id="PF_XBTUSD",
        quote="USD",
        base="XBT",
    )
    crypto = replace(
        instrument,
        symbol="SOL/EUR",
        instrument_id="SOLEUR",
        base="SOL",
    )

    selected, _ = scanner.select_for_cycle(
        [xstock, derivative, crypto],
        limit=3,
        family_slots=1,
    )

    assert len(selected) == 3
    assert {scanner.product_family(item) for item in selected} == {
        "XSTOCK",
        "DERIVATIVE",
        "CRYPTO",
    }
