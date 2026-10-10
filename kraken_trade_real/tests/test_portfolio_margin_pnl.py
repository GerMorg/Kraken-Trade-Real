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
