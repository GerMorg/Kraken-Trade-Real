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

def test_spot_snapshot_keeps_base_volume_and_calculates_quote_turnover(instrument):
    from app.market.data import MarketData

    snap = MarketData(object()).snapshot(
        instrument,
        {
            "XXBTZEUR": {
                "b": ["60000"],
                "a": ["60010"],
                "c": ["60005"],
                "v": ["100", "1000"],
            }
        },
        include_history=False,
        include_orderbook=False,
    )

    assert snap is not None
    # Ticker volume is 1,000 XBT, not 1,000 EUR.
    assert snap.volume_24h == Decimal("1000")
    assert snap.quote_asset == "EUR"
    assert snap.volume_24h_quote == Decimal("60005000")
    assert snap.liquidity_eur_24h is None


def test_scanner_uses_fx_normalized_eur_turnover_not_raw_base_volume(instrument):
    from dataclasses import replace
    from time import time

    from app.market.scanner import MarketScanner

    btc_usd = replace(
        instrument,
        symbol="BTC/USD",
        instrument_id="XXBTZUSD",
        altname="XBTUSD",
        base="XXBT",
        quote="ZUSD",
    )
    doge_eur = replace(
        instrument,
        symbol="DOGE/EUR",
        instrument_id="XXDGZEUR",
        altname="XDGEUR",
        base="XXDG",
        quote="ZEUR",
    )
    eur_usd = replace(
        instrument,
        symbol="EUR/USD",
        instrument_id="ZEURZUSD",
        altname="EURUSD",
        base="ZEUR",
        quote="ZUSD",
    )
    now = time()

    snapshots = {
        # One BTC (~60,000 USD) must pass a €50 turnover floor after FX conversion,
        # even though the raw base-volume number is only 1.
        "BTC/USD": MarketSnapshot(
            "BTC/USD", Decimal("60000"), Decimal("59990"), Decimal("60010"),
            Decimal("1"), now, (), quote_asset="USD",
        ),
        # 100 DOGE at €0.10 is only €10; the old raw-quantity comparison incorrectly
        # treated its base volume of 100 as €100 and let it pass a €50 floor.
        "DOGE/EUR": MarketSnapshot(
            "DOGE/EUR", Decimal("0.1"), Decimal("0.0999"), Decimal("0.1001"),
            Decimal("100"), now, (), quote_asset="EUR",
        ),
        # 1 EUR = 1.10 USD gives the scanner a current USD->EUR conversion path.
        "EUR/USD": MarketSnapshot(
            "EUR/USD", Decimal("1.1"), Decimal("1.099"), Decimal("1.101"),
            Decimal("1000"), now, (), quote_asset="USD",
        ),
    }
    scanner = MarketScanner(50, 100, 60)

    selected, diagnostics = scanner.fast_filter_with_diagnostics(
        [btc_usd, doge_eur, eur_usd],
        snapshots,
        require_history=False,
    )

    assert {item.symbol for item in selected} == {"BTC/USD", "EUR/USD"}
    assert snapshots["BTC/USD"].liquidity_eur_24h is not None
    assert snapshots["BTC/USD"].liquidity_eur_24h > Decimal("50000")
    assert snapshots["DOGE/EUR"].liquidity_eur_24h == Decimal("10.0")
    assert diagnostics["excluded_by_reason"]["LIQUIDITY_TOO_LOW"] == 1


def test_scanner_fails_closed_when_spot_quote_cannot_be_converted_to_eur(instrument):
    from dataclasses import replace
    from time import time

    from app.market.scanner import MarketScanner

    unknown_quote = replace(
        instrument,
        symbol="DOGE/ABC",
        instrument_id="DOGEABC",
        base="DOGE",
        quote="ABC",
    )
    snap = MarketSnapshot(
        "DOGE/ABC", Decimal("0.1"), Decimal("0.0999"), Decimal("0.1001"),
        Decimal("100000"), time(), (),
    )

    selected, diagnostics = MarketScanner(1, 100, 60).fast_filter_with_diagnostics(
        [unknown_quote], {unknown_quote.symbol: snap}, require_history=False,
    )

    assert selected == []
    assert diagnostics["excluded_by_reason"]["LIQUIDITY_EUR_UNAVAILABLE"] == 1


def test_features_use_eur_normalized_liquidity_when_available():
    closes = tuple(Decimal(str(100 + i)) for i in range(40))
    snap = MarketSnapshot(
        "DOGE/EUR", Decimal("0.1"), Decimal("0.0999"), Decimal("0.1001"),
        Decimal("100000"), __import__("time").time(), closes,
        quote_asset="EUR", volume_24h_quote=Decimal("10000"),
        liquidity_eur_24h=Decimal("10000"),
    )

    features = FeatureEngine().calculate(snap)

    assert features["liquidity"] == Decimal("10000")

def test_held_position_stays_in_history_candidates_when_entry_filter_drops_it(instrument):
    from app.market.scanner import MarketScanner

    candidates, added = MarketScanner.include_held_positions_in_history(
        history_candidates=[],
        held_positions=[instrument],
        available_ticker_symbols={instrument.symbol},
    )

    assert candidates == [instrument]
    assert added == 1

    repeated, added_again = MarketScanner.include_held_positions_in_history(
        history_candidates=candidates,
        held_positions=[instrument],
        available_ticker_symbols={instrument.symbol},
    )
    assert repeated == [instrument]
    assert added_again == 0

def test_scanner_uses_promoted_market_filter_thresholds_without_restart(instrument):
    from time import time
    from types import SimpleNamespace

    from app.market.scanner import MarketScanner

    config = SimpleNamespace(
        market_min_liquidity_eur=50.0,
        market_max_spread_bps=80.0,
    )
    scanner = MarketScanner(50.0, 80.0, 60, config=config)
    snapshot = MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("59900"),
        Decimal("60100"),
        Decimal("100"),
        time(),
        (),
    )

    selected, diagnostics = scanner.fast_filter_with_diagnostics(
        [instrument], {instrument.symbol: snapshot}, require_history=False,
    )
    assert selected == [instrument]
    assert diagnostics["active_thresholds"] == {
        "market_min_liquidity_eur": "50.0",
        "market_max_spread_bps": "80.0",
    }

    # A learned tighter spread policy takes effect on the next scan.
    config.market_max_spread_bps = 20.0
    selected, diagnostics = scanner.fast_filter_with_diagnostics(
        [instrument], {instrument.symbol: snapshot}, require_history=False,
    )
    assert selected == []
    assert diagnostics["excluded_by_reason"]["SPREAD_TOO_WIDE"] == 1
    assert diagnostics["active_thresholds"]["market_max_spread_bps"] == "20.0"

    # A stricter learned EUR-notional floor likewise takes effect immediately.
    config.market_max_spread_bps = 80.0
    config.market_min_liquidity_eur = 7_000_000.0
    selected, diagnostics = scanner.fast_filter_with_diagnostics(
        [instrument], {instrument.symbol: snapshot}, require_history=False,
    )
    assert selected == []
    assert diagnostics["excluded_by_reason"]["LIQUIDITY_TOO_LOW"] == 1
    assert diagnostics["active_thresholds"]["market_min_liquidity_eur"] == "7000000.0"
