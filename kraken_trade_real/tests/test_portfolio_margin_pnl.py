from decimal import Decimal

from app.portfolio.reconcile import PortfolioReconciler

D = Decimal


def test_spot_margin_pnl_and_basis_are_reconciled_in_eur(db, fake_gateway, instrument):
    fake_gateway.spot_open_positions = lambda: {
        "O-MINA-1": {
            "pair": instrument.altname,
            "type": "sell",
            "vol": "1.25",
            "vol_closed": "0.25",
            "cost": "600",
            "value": "585",
            "net": "15",
            "leverage": "2",
        }
    }
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [instrument],
        {
            instrument.instrument_id: {
                "b": ["60000"],
                "a": ["60010"],
                "c": ["60005"],
            }
        },
    )

    portfolio = reconciler.reconcile()

    assert portfolio.spot_open_positions_read_ok is True
    assert portfolio.position_pnl_eur[instrument.symbol] == D("15")
    assert portfolio.position_pnl_pct[instrument.symbol] == D("2.5")
    assert portfolio.position_basis_eur[instrument.symbol] == D("600")
    assert portfolio.position_quantity[instrument.symbol] == D("1.00")
    assert portfolio.unrealized_pnl_eur == D("15")
    assert portfolio.positions[instrument.symbol] == D("-585")


def test_open_positions_read_failure_is_not_treated_as_flat_portfolio(
    db, fake_gateway, instrument
):
    def fail_open_positions():
        raise RuntimeError("temporary Kraken failure")

    fake_gateway.spot_open_positions = fail_open_positions
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context([instrument], {})

    portfolio = reconciler.reconcile()

    assert portfolio.spot_open_positions_read_ok is False
    assert portfolio.position_pnl_pct == {}



def test_spot_margin_used_margin_and_daily_pnl_are_accounted_once(db, fake_gateway):
    import datetime
    import time

    now = time.time()
    day_start = datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc
    ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    db.execute(
        """INSERT INTO portfolio_snapshots(
             cycle_id,captured_at,equity_eur,cash_eur,gross_eur,net_eur,
             margin_used_eur,unrealized_pnl_eur,realized_pnl_eur,daily_pnl_eur,
             drawdown_pct,positions_json,open_orders
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("prior-day", day_start - 60, "100", "80", "20", "20",
         "7", "0", "0", "0", "0", "{}", 0),
    )
    fake_gateway.spot_balance = lambda: {"ZEUR": "50"}
    fake_gateway.spot_private = lambda method, params=None: (
        {"eb": "90", "tb": "90", "e": "90", "m": "12", "mf": "78", "ml": "750"}
        if method == "TradeBalance" else {}
    )
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context([], {})
    portfolio = reconciler.reconcile()
    assert portfolio.margin_used_eur == D("12")
    assert portfolio.daily_pnl_eur == D("-10")



def test_futures_position_side_and_executable_quote_are_reconciled(
    db, fake_gateway, instrument
):
    from dataclasses import replace
    from app.domain.states import ProductType

    future = replace(
        instrument, venue="futures", product_type=ProductType.DERIVATIVE,
        symbol="PF_XBTUSD", instrument_id="PF_XBTUSD", altname="PF_XBTUSD",
        base="XBT", quote="USD", margin_class="futures",
    )
    fake_gateway.futures_enabled = True
    fake_gateway.spot_open_positions = lambda: {}
    fake_gateway.futures_accounts = lambda: {
        "accounts": {"default": {
            "availableMargin": "80", "portfolioValue": "100",
            "initialMargin": "20", "marginLevel": "500",
        }}
    }
    fake_gateway.futures_open_positions = lambda: {
        "openPositions": [{
            "symbol": "PF_XBTUSD", "side": "long", "size": "10",
            "price": "100", "leverage": "2",
        }]
    }
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [future], {}, {"tickers": [{
            "symbol": "PF_XBTUSD", "bid": "105", "ask": "105.1", "last": "105",
        }]},
    )

    portfolio = reconciler.reconcile()

    assert portfolio.futures_positions_read_ok is True
    assert portfolio.futures_position_symbols == ("PF_XBTUSD",)
    assert portfolio.positions["PF_XBTUSD"] == D("10")
    assert portfolio.position_pnl_pct["PF_XBTUSD"] == D("5")


def test_futures_missing_executable_quote_does_not_invent_pnl(
    db, fake_gateway, instrument
):
    from dataclasses import replace
    from app.domain.states import ProductType

    future = replace(
        instrument, venue="futures", product_type=ProductType.DERIVATIVE,
        symbol="PF_XBTUSD", instrument_id="PF_XBTUSD", altname="PF_XBTUSD",
        base="XBT", quote="USD", margin_class="futures",
    )
    fake_gateway.futures_enabled = True
    fake_gateway.spot_open_positions = lambda: {}
    fake_gateway.futures_accounts = lambda: {"accounts": {"default": {
        "availableMargin": "80", "portfolioValue": "100", "initialMargin": "20",
    }}}
    fake_gateway.futures_open_positions = lambda: {"openPositions": [{
        "symbol": "PF_XBTUSD", "side": "short", "size": "10", "price": "100",
    }]}
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context([future], {}, {"tickers": []})

    portfolio = reconciler.reconcile()

    assert portfolio.positions["PF_XBTUSD"] == D("-10")
    assert "PF_XBTUSD" not in portfolio.position_pnl_pct



def test_futures_short_pnl_uses_entry_notional_return(db, fake_gateway, instrument):
    from dataclasses import replace
    from app.domain.states import ProductType

    future = replace(
        instrument, venue="futures", product_type=ProductType.DERIVATIVE,
        symbol="PF_XBTUSD", instrument_id="PF_XBTUSD", altname="PF_XBTUSD",
        base="XBT", quote="USD", margin_class="futures",
    )
    fake_gateway.futures_enabled = True
    fake_gateway.spot_open_positions = lambda: {}
    fake_gateway.futures_accounts = lambda: {"accounts": {"default": {
        "availableMargin": "80", "portfolioValue": "100", "initialMargin": "20",
    }}}
    fake_gateway.futures_open_positions = lambda: {"openPositions": [{
        "symbol": "PF_XBTUSD", "side": "short", "value": "10",
        "price": "100", "leverage": "2",
    }]}
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [future], {}, {"tickers": [{
            "symbol": "PF_XBTUSD", "bid": "105", "ask": "105.1", "last": "105",
        }]},
    )

    portfolio = reconciler.reconcile()

    assert portfolio.futures_positions_read_ok is True
    assert portfolio.positions["PF_XBTUSD"] == D("-10")
    # A 5.1% adverse move from the entry price must not be diluted by
    # measuring the return against the current ask.
    assert portfolio.position_pnl_pct["PF_XBTUSD"] == D("-5.100")
