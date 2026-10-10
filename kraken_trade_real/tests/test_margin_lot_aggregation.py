from decimal import Decimal

from app.portfolio.reconcile import PortfolioReconciler

D = Decimal


def test_multiple_open_margin_lots_are_aggregated_not_discarded(db, fake_gateway, instrument):
    fake_gateway.spot_open_positions = lambda: {
        "O-MINA-1": {
            "pair": instrument.altname, "type": "sell", "vol": "1.25",
            "vol_closed": "0.25", "cost": "600", "value": "585",
            "net": "15", "leverage": "2",
        },
        "O-MINA-2": {
            "pair": instrument.altname, "type": "sell", "vol": "0.75",
            "vol_closed": "0.25", "cost": "300", "value": "285",
            "net": "15", "leverage": "2",
        },
    }
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [instrument],
        {instrument.instrument_id: {"b": ["60000"], "a": ["60010"], "c": ["60005"]}},
    )

    portfolio = reconciler.reconcile()

    assert portfolio.spot_open_positions_read_ok is True
    assert portfolio.positions[instrument.symbol] == D("-870")
    assert portfolio.position_quantity[instrument.symbol] == D("1.50")
    assert portfolio.position_basis_eur[instrument.symbol] == D("900")
    assert portfolio.position_pnl_eur[instrument.symbol] == D("30")
    assert portfolio.position_pnl_pct[instrument.symbol] == D("30") / D("900") * D("100")
    assert reconciler.position_leverages[instrument.symbol] == D("2")
