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
    assert qty == Decimal("0.0016")


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


def test_subminimum_position_reduction_is_returned_for_runtime_dust_audit(config, instrument):
    engine = DecisionEngine(config)
    features = {"volatility": Decimal("2")}
    long_signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("20"), Decimal("1"),
        Decimal("0.9"), "TREND_UP", Decimal("0"), Decimal("0"), features,
    )
    short_signal = Signal(
        instrument.symbol, Direction.SHORT, Decimal("0"), Decimal("10"),
        Decimal("0.1"), "TREND_DOWN", Decimal("0"), Decimal("0"), features,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("80"),
        positions={instrument.symbol: Decimal("13.6")},
        gross_eur=Decimal("13.6"),
        net_eur=Decimal("13.6"),
    )
    decision = engine.choose(
        instrument, long_signal, short_signal, portfolio,
        "model-test", "config-test", {}, Decimal("0.5"),
    )
    assert decision is not None
    assert decision.reduce_only is True
    assert decision.current_position_eur == Decimal("13.6")
    assert decision.target_position_eur == Decimal("13.50")
    assert decision.target_notional_eur == Decimal("0.10")


def test_open_spot_margin_position_leverage_is_reconciled_by_canonical_symbol(
    db, fake_gateway, instrument
):
    fake_gateway.spot_open_positions = lambda: {
        "O-MARGIN-1": {
            "pair": instrument.altname,
            "type": "sell",
            "value": "8.0",
            "leverage": "2",
        }
    }
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [instrument],
        {instrument.instrument_id: {"c": ["60005"]}},
    )

    portfolio = reconciler.reconcile()

    assert portfolio.positions[instrument.symbol] == Decimal("-8.0")
    assert reconciler.position_leverages[instrument.symbol] == Decimal("2")



def test_spot_rebalance_delta_is_not_called_dust_when_wallet_holds_orderable_balance():
    from app.portfolio.reconcile import resolve_spot_cash_reduction_quantity

    instrument = Instrument(
        venue="spot", product_type=ProductType.SPOT,
        symbol="W/EUR", instrument_id="WEUR", altname="WEUR",
        base="W", quote="EUR", status="online",
        margin_available=False, long_available=True, short_available=False,
        leverage_levels=(Decimal("1"),), min_order_qty=Decimal("0.01"),
        min_cost=Decimal("1"), lot_decimals=2, price_decimals=4,
        tick_size=Decimal("0.0001"), margin_class="spot",
    )
    quantity, reason = resolve_spot_cash_reduction_quantity(
        instrument, Decimal("0.01"), Decimal("0.05"), Decimal("4000"),
        flattening=False, managed_position_quantity=Decimal("4000"),
    )
    assert quantity == Decimal("0.05")
    assert reason == "REBALANCE_DELTA_BELOW_MINIMUM"


def test_spot_full_exit_uses_orderable_wallet_balance_instead_of_tiny_eur_delta():
    from app.portfolio.reconcile import resolve_spot_cash_reduction_quantity

    instrument = Instrument(
        venue="spot", product_type=ProductType.SPOT,
        symbol="W/EUR", instrument_id="WEUR", altname="WEUR",
        base="W", quote="EUR", status="online",
        margin_available=False, long_available=True, short_available=False,
        leverage_levels=(Decimal("1"),), min_order_qty=Decimal("0.01"),
        min_cost=Decimal("1"), lot_decimals=2, price_decimals=4,
        tick_size=Decimal("0.0001"), margin_class="spot",
    )
    quantity, reason = resolve_spot_cash_reduction_quantity(
        instrument, Decimal("0.01"), Decimal("0.05"), Decimal("4000"),
        flattening=True, managed_position_quantity=Decimal("4000"),
    )
    assert quantity == Decimal("4000.00")
    assert reason == "FULL_EXIT_PAIR_INVENTORY_CAP"


def test_spot_full_exit_is_capped_to_pair_inventory_when_base_is_shared_by_quote_pairs():
    from app.portfolio.reconcile import resolve_spot_cash_reduction_quantity

    # The wallet has 0.03 XBT, but this pair owns only its reconciled 0.01 XBT lot.
    usd_quantity, usd_reason = resolve_spot_cash_reduction_quantity(
        usd_instrument(),
        Decimal("60000"),
        Decimal("0.03"),
        Decimal("0.03"),
        flattening=True,
        managed_position_quantity=Decimal("0.01"),
    )
    assert usd_quantity == Decimal("0.01")
    assert usd_reason == "FULL_EXIT_PAIR_INVENTORY_CAP"

    # The second quote pair may close only its own separate 0.02 XBT lot.
    eur_quantity, eur_reason = resolve_spot_cash_reduction_quantity(
        xbt_eur_instrument(),
        Decimal("55000"),
        Decimal("0.03"),
        Decimal("0.03"),
        flattening=True,
        managed_position_quantity=Decimal("0.02"),
    )
    assert eur_quantity == Decimal("0.02")
    assert eur_reason == "FULL_EXIT_PAIR_INVENTORY_CAP"


def test_spot_full_exit_fails_closed_without_verified_pair_inventory():
    from app.portfolio.reconcile import resolve_spot_cash_reduction_quantity

    quantity, reason = resolve_spot_cash_reduction_quantity(
        usd_instrument(),
        Decimal("60000"),
        Decimal("0.01"),
        Decimal("0.03"),
        flattening=True,
        managed_position_quantity=None,
    )
    assert quantity is None
    assert reason == "CASH_SPOT_PAIR_INVENTORY_UNVERIFIED"


def test_spot_balance_smaller_than_exchange_minimum_is_real_dust():
    from app.portfolio.reconcile import resolve_spot_cash_reduction_quantity

    instrument = Instrument(
        venue="spot", product_type=ProductType.SPOT,
        symbol="W/EUR", instrument_id="WEUR", altname="WEUR",
        base="W", quote="EUR", status="online",
        margin_available=False, long_available=True, short_available=False,
        leverage_levels=(Decimal("1"),), min_order_qty=Decimal("0.01"),
        min_cost=Decimal("1"), lot_decimals=2, price_decimals=4,
        tick_size=Decimal("0.0001"), margin_class="spot",
    )
    quantity, reason = resolve_spot_cash_reduction_quantity(
        instrument, Decimal("0.01"), Decimal("0.05"), Decimal("20"),
        flattening=False, managed_position_quantity=Decimal("20"),
    )
    assert quantity == Decimal("0.05")
    assert reason == "DUST_POSITION"


def xbt_eur_instrument() -> Instrument:
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="XBT/EUR",
        instrument_id="XXBTZEUR",
        altname="XBTEUR",
        base="XXBT",
        quote="ZEUR",
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


def _add_open_cash_lot(
    db, *, symbol: str, lot_id: str, quantity: str, quote_asset: str, entry_price: str
):
    db.execute(
        """INSERT INTO core_inventory_lots(
             lot_id,symbol,direction,opened_at,entry_order_id,entry_trade_id,
             opening_decision_id,entry_price,remaining_quantity,
             remaining_entry_fee_est_quote,remaining_entry_fee_verified_quote,
             entry_fee_verified,quote_asset,detail_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            lot_id, symbol, "LONG", 100.0, f"order-{lot_id}", f"trade-{lot_id}",
            f"decision-{lot_id}", entry_price, quantity, "0", "0", 0,
            quote_asset, '{"margin":false,"leverage":"1"}',
        ),
    )


def test_portfolio_reconciliation_keeps_cash_spot_positions_on_their_opening_pairs(
    db, fake_gateway
):
    instruments = [usd_instrument(), xbt_eur_instrument(), usd_eur_instrument()]
    _add_open_cash_lot(
        db, symbol="XBT/USD", lot_id="usd-lot", quantity="0.01",
        quote_asset="ZUSD", entry_price="59000",
    )
    _add_open_cash_lot(
        db, symbol="XBT/EUR", lot_id="eur-lot", quantity="0.02",
        quote_asset="ZEUR", entry_price="54000",
    )

    fake_gateway.spot_balance = lambda: {"XXBT": "0.03", "ZEUR": "500"}
    fake_gateway.spot_private = lambda method, params=None: (
        {"XXBT": "0.03", "ZEUR": "500"}
        if method == "Balance"
        else {"eb": "2500", "e": "2500", "n": "0", "m": "0", "mf": "2500", "ml": "9999"}
        if method == "TradeBalance"
        else {}
    )
    fake_gateway.spot_open_positions = lambda: {}
    fake_gateway.spot_ledgers = lambda params: {"count": 0, "ledger": {}}

    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        instruments,
        {
            "XXBTZUSD": {"c": ["60000"]},
            "XXBTZEUR": {"c": ["55000"]},
            "ZUSDZEUR": {"c": ["0.9"]},
        },
    )

    portfolio = reconciler.reconcile()

    assert "XBT/USD" in portfolio.positions, {
        "positions": {key: str(value) for key, value in portfolio.positions.items()},
        "events": [
            (row.get("code"), row.get("payload_json"))
            for row in db.query("SELECT code,payload_json FROM events ORDER BY id DESC LIMIT 8")
        ],
    }
    assert portfolio.positions["XBT/USD"] == Decimal("540.000")
    assert portfolio.positions["XBT/EUR"] == Decimal("1100.00")
    assert set(portfolio.spot_cash_position_symbols) == {"XBT/USD", "XBT/EUR"}
    assert "XBT/USD" in portfolio.position_pnl_pct
    assert "XBT/EUR" in portfolio.position_pnl_pct


def test_zero_wallet_balance_with_stale_cash_spot_fifo_lot_blocks_reentry(db, fake_gateway, config):
    from app.domain.symbols import canonical_asset

    _add_open_cash_lot(
        db, symbol="XBT/USD", lot_id="stale-zero-wallet-lot",
        quantity="0.01", quote_asset="ZUSD", entry_price="59000",
    )
    fake_gateway.spot_balance = lambda: {"ZEUR": "500"}
    fake_gateway.spot_private = lambda method, params=None: (
        {"ZEUR": "500"}
        if method == "Balance"
        else {"eb": "500", "e": "500", "n": "0", "m": "0", "mf": "500", "ml": "9999"}
        if method == "TradeBalance"
        else {}
    )
    fake_gateway.spot_open_positions = lambda: {}
    fake_gateway.spot_ledgers = lambda params: {"count": 0, "ledger": {}}

    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [usd_instrument(), usd_eur_instrument()],
        {
            "XXBTZUSD": {"b": ["60000"], "a": ["60010"], "c": ["60005"]},
            "ZUSDZEUR": {"b": ["0.899"], "a": ["0.901"], "c": ["0.9"]},
        },
    )
    portfolio = reconciler.reconcile()

    base = canonical_asset("XXBT")
    assert base in portfolio.unreconciled_spot_inventory_bases
    mismatch = db.one(
        "SELECT payload_json FROM events WHERE code='PORTFOLIO_CASH_SPOT_LEDGER_MISMATCH' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert mismatch is not None
    assert "LEDGER_INVENTORY_WITHOUT_WALLET_BALANCE" in mismatch["payload_json"]

    # Zero wallet balance means no position key exists. The stale ledger alone
    # must nevertheless prevent a new buy under the same or a different quote pair.
    assert "XBT/USD" not in portfolio.positions
    alternate = usd_instrument()
    features = {"volatility": Decimal("2")}
    long_signal = Signal(
        alternate.symbol, Direction.LONG, Decimal("200"), Decimal("50"),
        Decimal("0.9"), "TREND_UP", Decimal("0"), Decimal("0"), features,
    )
    short_signal = Signal(
        alternate.symbol, Direction.SHORT, Decimal("0"), Decimal("100"),
        Decimal("0.1"), "TREND_DOWN", Decimal("0"), Decimal("0"), features,
    )
    engine = DecisionEngine(config)
    assert engine.choose(
        alternate, long_signal, short_signal, portfolio, "model", "config", {},
        Decimal("0.5"),
    ) is None
    assert engine.rejection_reason(
        alternate, long_signal, short_signal, portfolio, {}, Decimal("0.5")
    ) == "UNMANAGED_SPOT_POSITION_KEEP"


def test_unmatched_cash_spot_wallet_inventory_is_not_signal_sold(config):
    from app.domain.models import Instrument, PortfolioState, Signal
    from app.domain.states import Direction, ProductType

    instrument = Instrument(
        venue="spot", product_type=ProductType.SPOT,
        symbol="XBT/EUR", instrument_id="XXBTZEUR", altname="XBTEUR",
        base="XXBT", quote="ZEUR", status="online",
        margin_available=False, long_available=True, short_available=False,
        leverage_levels=(Decimal("1"),), min_order_qty=Decimal("0.0001"),
        min_cost=Decimal("1"), lot_decimals=4, price_decimals=2,
        tick_size=Decimal("0.01"), margin_class="spot",
    )
    features = {"volatility": Decimal("2")}
    long_signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("200"), Decimal("50"),
        Decimal("0.9"), "TREND_UP", Decimal("0"), Decimal("0"), features,
    )
    short_signal = Signal(
        instrument.symbol, Direction.SHORT, Decimal("0"), Decimal("100"),
        Decimal("0.1"), "TREND_DOWN", Decimal("0"), Decimal("0"), features,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("80"),
        positions={instrument.symbol: Decimal("20")},
        spot_cash_position_symbols=(),
    )
    engine = DecisionEngine(config)

    assert engine.choose(
        instrument, long_signal, short_signal, portfolio, "model", "config", {},
        Decimal("0.5"),
    ) is None
    assert engine.rejection_reason(
        instrument, long_signal, short_signal, portfolio, {}, Decimal("0.5")
    ) == "UNMANAGED_SPOT_POSITION_KEEP"

    # The same unverified XBT wallet units must not be opened again under the
    # alternate quote market just because that pair's position key is currently 0.
    alternate = usd_instrument()
    alt_long = Signal(
        alternate.symbol, Direction.LONG, Decimal("200"), Decimal("50"),
        Decimal("0.9"), "TREND_UP", Decimal("0"), Decimal("0"), features,
    )
    alt_short = Signal(
        alternate.symbol, Direction.SHORT, Decimal("0"), Decimal("100"),
        Decimal("0.1"), "TREND_DOWN", Decimal("0"), Decimal("0"), features,
    )
    assert engine.choose(
        alternate, alt_long, alt_short, portfolio, "model", "config", {},
        Decimal("0.5"),
    ) is None
    assert engine.rejection_reason(
        alternate, alt_long, alt_short, portfolio, {}, Decimal("0.5")
    ) == "UNMANAGED_SPOT_POSITION_KEEP"
