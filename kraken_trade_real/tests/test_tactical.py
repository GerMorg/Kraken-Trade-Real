from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.domain.models import Instrument, MarketSnapshot, PortfolioState
from app.domain.states import Direction, ProductType
from app.domain.symbols import canonical_asset
from app.trading.intent import OrderIntentBuilder
from app.trading.tactical import TacticalPosition, TacticalTrader


D = Decimal


class DummyAudit:
    def __init__(self):
        self.events = []

    def emit(self, code, level="INFO", **payload):
        self.events.append((code, level, payload))


class DummyRecovery:
    def issue(self, *args, **kwargs):
        return None


class DummyWS:
    connected = False

    def __init__(self):
        self.symbols = ()
        self.aliases = {}

    def start(self):
        return None

    def stop(self):
        return None

    def set_symbols(self, symbols, aliases=None):
        self.symbols = tuple(symbols)
        self.aliases = dict(aliases or {})

    def seed_price_history(self, symbol, closes, end_timestamp=None):
        return None

    def market_snapshot(self, symbol):
        return None

    def trade_metrics(self, symbol, lookback_seconds=900):
        return {
            "volume_ratio": D("3"),
            "age_seconds": D("1"),
        }


class DummyDB:
    def tactical_positions(self):
        return []

    def delete_tactical_position(self, symbol):
        return None

    def tactical_today_net_pnl(self):
        return D("0")

    def tactical_trade_count(self, since_timestamp):
        return 0


def cfg(**overrides):
    values = dict(
        tactical_enabled=True,
        tactical_shadow_mode=True,
        tactical_allow_short=True,
        tactical_max_positions=1,
        tactical_candidate_limit=12,
        tactical_max_spread_bps=25,
        tactical_market_max_age_seconds=5,
        tactical_min_volatility_bps=12,
        tactical_max_volatility_bps=55,
        tactical_min_volume_ratio=2,
        tactical_min_momentum_bps=40,
        tactical_min_breakout_bps=25,
        tactical_min_imbalance=0.10,
        tactical_min_expected_move_bps=280,
        tactical_entry_fee_bps=80,
        tactical_exit_fee_bps=80,
        tactical_expected_slippage_bps=25,
        tactical_safety_buffer_bps=30,
        tactical_context_block_bps=80,
        tactical_portfolio_pct=25,
        tactical_max_capital_eur=15,
        tactical_position_limit_pct=25,
        tactical_max_daily_loss_pct=1.5,
        tactical_max_trades_per_hour=2,
        tactical_max_trades_per_day=6,
        tactical_trade_lookback_seconds=900,
        tactical_short_leverage=2,
        tactical_order_confirm_seconds=5,
        tactical_stop_loss_pct=1,
        tactical_take_profit_pct=2.2,
        tactical_trailing_trigger_bps=100,
        tactical_trailing_stop_pct=0.7,
        tactical_max_hold_seconds=1800,
        tactical_reversal_exit_bps=120,
        risk_max_leverage=3,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def instrument(
    *,
    product_type=ProductType.SPOT_MARGIN,
    short_available=True,
    long_available=True,
    leverage_levels=(D("1"), D("2"), D("3")),
):
    return Instrument(
        venue="spot",
        product_type=product_type,
        symbol="BTC/USD",
        instrument_id="XXBTZUSD",
        altname="XBTUSD",
        base="XBT",
        quote="USD",
        status="online",
        margin_available=product_type == ProductType.SPOT_MARGIN,
        long_available=long_available,
        short_available=short_available,
        leverage_levels=leverage_levels,
        min_order_qty=D("0.0001"),
        min_cost=D("5"),
        lot_decimals=8,
        price_decimals=2,
        tick_size=D("0.01"),
        margin_class="spot-margin",
        metadata={"leverage_sell": [str(value) for value in leverage_levels]},
    )


def market_state(now=1000.0, direction="LONG"):
    start = D("100")
    points = []
    for i in range(19):
        if direction == "LONG":
            price = start + D(str(i)) * D("0.28")
        else:
            price = start - D(str(i)) * D("0.28")
        points.append((now - 180 + i * 10, price))
    return {
        "price": points[-1][1],
        "bid": points[-1][1] - D("0.01"),
        "ask": points[-1][1] + D("0.01"),
        "timestamp": now - 1,
        "spread_bps": D("2"),
        "price_points": tuple(points),
        "depths_bid": (
            ((points[-1][1] - D("0.01"), D("10")),)
            if direction == "LONG"
            else ((points[-1][1] - D("0.01"), D("2")),)
        ),
        "depths_ask": (
            ((points[-1][1] + D("0.01"), D("2")),)
            if direction == "LONG"
            else ((points[-1][1] + D("0.01"), D("10")),)
        ),
    }


def test_tactical_long_signal_requires_cost_aware_breakout():
    websocket = DummyWS()
    websocket.market_snapshot = lambda symbol: market_state(direction="LONG")
    trader = TacticalTrader(
        cfg(),
        DummyDB(),
        DummyAudit(),
        None,
        websocket,
        None,
        None,
        None,
        None,
    )
    signal = trader._build_signal(instrument(), market_state(direction="LONG"), 1000.0)
    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.net_edge_bps > 0


def test_tactical_short_signal_is_supported_when_instrument_allows_it():
    websocket = DummyWS()
    trader = TacticalTrader(
        cfg(tactical_allow_short=True),
        DummyDB(),
        DummyAudit(),
        None,
        websocket,
        None,
        None,
        None,
        None,
    )
    signal = trader._build_signal(instrument(short_available=True), market_state(direction="SHORT"), 1000.0)
    assert signal is not None
    assert signal.direction == Direction.SHORT


def test_tactical_short_signal_is_rejected_when_exchange_disallows_short():
    trader = TacticalTrader(
        cfg(tactical_allow_short=True),
        DummyDB(),
        DummyAudit(),
        None,
        DummyWS(),
        None,
        None,
        None,
        None,
    )
    signal = trader._build_signal(instrument(short_available=False), market_state(direction="SHORT"), 1000.0)
    assert signal is None


def test_tactical_short_leverage_uses_supported_margin_level():
    trader = TacticalTrader(
        cfg(tactical_short_leverage=2),
        DummyDB(),
        DummyAudit(),
        None,
        DummyWS(),
        None,
        None,
        None,
        None,
    )
    assert trader._entry_leverage(instrument(), Direction.SHORT) == D("2")
    assert trader._entry_leverage(instrument(leverage_levels=(D("1"),)), Direction.SHORT) == D("0")


def test_short_intent_is_sell_and_has_margin_flag():
    builder = OrderIntentBuilder(40, 45)
    inst = instrument()
    decision = SimpleNamespace(
        decision_id="decision-1",
        instrument=inst,
        execution_direction=Direction.SHORT,
        signal=SimpleNamespace(direction=Direction.SHORT, net_edge_bps=D("500")),
    )
    intent = builder.build(
        decision,
        D("2"),
        "limit",
        D("0.01"),
        D("99"),
        reduce_only=False,
        post_only=False,
    )
    assert intent.direction == Direction.SHORT
    assert intent.side == "sell"
    assert intent.margin is True
    assert intent.leverage == D("2")


def test_tactical_short_pnl_and_fees_use_positive_exposure():
    gross = TacticalTrader._trade_gross_pnl(
        Direction.SHORT, D("10"), D("100"), D("95")
    )
    assert gross == D("10") * (D("100") / D("95") - D("1"))
    trader = TacticalTrader(
        cfg(),
        DummyDB(),
        DummyAudit(),
        None,
        DummyWS(),
        None,
        None,
        None,
        None,
    )
    assert trader._trade_fees(D("-10")) == D("0.16")



def test_tactical_adaptive_entry_can_clear_cost_without_requiring_280_bps():
    trader = TacticalTrader(
        cfg(
            tactical_adaptive_entry_enabled=True,
            tactical_adaptive_min_expected_move_bps=230,
            tactical_adaptive_min_confidence=0.75,
            tactical_adaptive_min_net_edge_bps=15,
        ),
        DummyDB(), DummyAudit(), None, DummyWS(), None, None, None, None,
    )
    assert trader._adaptive_entry_allowed(
        D("250"), D("220"), D("0.80"), True, D("230"), D("0.75"), D("15")
    ) is True
    assert trader._adaptive_entry_allowed(
        D("250"), D("240"), D("0.80"), True, D("230"), D("0.75"), D("15")
    ) is False


def pair_instrument(
    symbol,
    base,
    quote,
    *,
    short_available=True,
    product_type=ProductType.SPOT_MARGIN,
):
    return Instrument(
        venue="spot",
        product_type=product_type,
        symbol=symbol,
        instrument_id=symbol.replace("/", ""),
        altname=symbol.replace("/", ""),
        base=base,
        quote=quote,
        status="online",
        margin_available=product_type == ProductType.SPOT_MARGIN,
        long_available=True,
        short_available=short_available,
        leverage_levels=(D("1"), D("2"), D("3")),
        min_order_qty=D("0.001"),
        min_cost=D("1"),
        lot_decimals=8,
        price_decimals=8,
        tick_size=D("0.00000001"),
        margin_class="spot-margin",
        metadata={
            "leverage_sell": ["2", "3"],
            "wsname": symbol,
        },
    )


def ranking_snapshot(symbol, spread_bps, *, volume=D("10000")):
    closes = tuple(
        D(value)
        for value in ("10", "10.1", "10.05", "10.2", "10.25", "10.3", "10.4", "10.35")
    )
    mid = closes[-1]
    half_spread = mid * D(str(spread_bps)) / D("20000")
    return MarketSnapshot(
        symbol=symbol,
        price=mid,
        bid=mid - half_spread,
        ask=mid + half_spread,
        volume_24h=D(str(volume)),
        timestamp=1000.0,
        closes=closes,
    )


def test_tactical_candidate_selection_deduplicates_quote_pairs_and_keeps_rank_order():
    websocket = DummyWS()
    audit = DummyAudit()
    trader = TacticalTrader(
        cfg(tactical_candidate_limit=12),
        DummyDB(),
        audit,
        None,
        websocket,
        None,
        None,
        None,
        None,
    )
    # Deliberately provide instruments in an order different from their market scores.
    instruments = [
        pair_instrument("STRK/EUR", "STRK", "EUR"),
        pair_instrument("MINA/USD", "MINA", "USD"),
        pair_instrument("STRK/USD", "STRK", "USD"),
        pair_instrument("BTC/USD", "XXBT", "USD"),
        pair_instrument("MINA/EUR", "MINA", "EUR"),
    ]
    spreads = {
        "STRK/EUR": D("12"),
        "MINA/USD": D("10"),
        "STRK/USD": D("3"),
        "BTC/USD": D("1"),
        "MINA/EUR": D("2"),
    }
    snapshots = {
        symbol: ranking_snapshot(symbol, spread)
        for symbol, spread in spreads.items()
    }

    trader.update_context(instruments, snapshots, {}, D("0"))

    assert list(trader._candidates) == ["BTC/USD", "MINA/EUR", "STRK/USD"]
    assert websocket.symbols == ("BTC/USD", "MINA/EUR", "STRK/USD")
    base_keys = [canonical_asset(item.base) for item in trader._candidates.values()]
    assert len(base_keys) == len(set(base_keys)) == 3
    assert trader._candidate_diagnostics["duplicate_quote_pairs_removed"] == 2
    event = next(event for event in audit.events if event[0] == "TACTICAL_CANDIDATES_UPDATED")
    assert event[2]["candidates"] == len(event[2]["symbols"]) == 3
    assert event[2]["entry_candidates"] == 3
    assert event[2]["duplicate_quote_pairs_removed"] == 2
    assert event[2]["quote_pair_choices"]["STRK"]["selected"] == "STRK/USD"
    assert event[2]["quote_pair_choices"]["STRK"]["alternatives"][0]["symbol"] == "STRK/EUR"
    assert event[2]["quote_pair_choices"]["MINA"]["selected"] == "MINA/EUR"
    assert event[2]["quote_pair_choices"]["MINA"]["alternatives"][0]["symbol"] == "MINA/USD"


def test_tactical_keeps_open_position_stream_and_excludes_other_quote_for_held_base():
    websocket = DummyWS()
    audit = DummyAudit()
    trader = TacticalTrader(
        cfg(),
        DummyDB(),
        audit,
        None,
        websocket,
        None,
        None,
        None,
        None,
    )
    opened_at = 1000.0
    trader._positions["MINA/USD"] = TacticalPosition(
        "MINA/USD",
        "spot",
        Direction.SHORT,
        D("10"),
        D("10"),
        D("10"),
        D("10"),
        D("20"),
        D("2"),
        opened_at,
        "test-order",
        D("1"),
    )
    trader.update_portfolio(
        PortfolioState(equity_eur=D("100"), positions={"MINA/EUR": D("5")})
    )
    instruments = [
        pair_instrument("MINA/EUR", "MINA", "EUR"),
        pair_instrument("ETH/EUR", "XETH", "EUR"),
        pair_instrument("MINA/USD", "MINA", "USD"),
    ]
    # The open position does not need to survive the fresh-entry snapshot filter
    # to remain subscribed and managed.
    snapshots = {
        "MINA/EUR": ranking_snapshot("MINA/EUR", D("2")),
        "ETH/EUR": ranking_snapshot("ETH/EUR", D("4")),
    }

    trader.update_context(instruments, snapshots, {}, D("0"))

    assert list(trader._candidates) == ["MINA/USD", "ETH/EUR"]
    assert websocket.symbols == ("MINA/USD", "ETH/EUR")
    assert "MINA/EUR" not in trader._candidates
    event = next(event for event in audit.events if event[0] == "TACTICAL_CANDIDATES_UPDATED")
    assert event[2]["active_position_streams"] == 1
    assert event[2]["held_asset_pairs_excluded"] == 1
    assert event[2]["candidates"] == len(event[2]["symbols"]) == 2


def test_canonical_asset_normalizes_kraken_legacy_aliases():
    assert canonical_asset("XBT") == "BTC"
    assert canonical_asset("XXBT") == "BTC"
    assert canonical_asset("XETH") == "ETH"
    assert canonical_asset("XXETH") == "ETH"
    assert canonical_asset("XETC") == "ETC"
    assert canonical_asset("XLTC") == "LTC"
    assert canonical_asset("XMLN") == "MLN"
    assert canonical_asset("XREP") == "REP"
    assert canonical_asset("XXDG") == "DOGE"
    assert canonical_asset("XDG") == "DOGE"
    assert canonical_asset("XXLM") == "XLM"
    assert canonical_asset("XXMR") == "XMR"
    assert canonical_asset("XXRP") == "XRP"
    assert canonical_asset("XZEC") == "ZEC"
    assert canonical_asset("ZEUR") == "EUR"
    assert canonical_asset("ZUSD") == "USD"


def test_canonical_asset_does_not_strip_real_x_or_z_prefixed_tickers():
    assert canonical_asset("ZBCN") == "ZBCN"
    assert canonical_asset("ZETA") == "ZETA"
    assert canonical_asset("XCN") == "XCN"
    assert canonical_asset("ZRX") == "ZRX"
