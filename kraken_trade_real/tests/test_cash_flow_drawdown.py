from __future__ import annotations

from decimal import Decimal

from app.domain.models import PortfolioState
from app.persistence.db import Database
from app.portfolio.reconcile import PortfolioReconciler


D = Decimal


class LedgerGateway:
    api_key = "test-api-key"

    def __init__(self, start: float, *, foreign_asset: bool = False) -> None:
        self.start = start
        self.foreign_asset = foreign_asset
        self.calls = []

    def spot_ledgers(self, params):
        self.calls.append(dict(params))
        if params["type"] == "deposit":
            asset = "XUSD" if self.foreign_asset else "ZEUR"
            return {
                "count": 1,
                "ledger": {
                    "LDEPOSIT": {
                        "time": self.start + 2,
                        "type": "deposit",
                        "asset": asset,
                        "amount": "100",
                        "fee": "0",
                    }
                },
            }
        if params["type"] == "withdrawal" and not self.foreign_asset:
            return {
                "count": 1,
                "ledger": {
                    "LWITHDRAWAL": {
                        "time": self.start + 3,
                        "type": "withdrawal",
                        "asset": "ZEUR",
                        "amount": "-20",
                        "fee": "0.5",
                    }
                },
            }
        return {"count": 0, "ledger": {}}


def _portfolio_db(tmp_path):
    db = Database(str(tmp_path / "cashflows.db"))
    state = PortfolioState(
        equity_eur=D("1000"), cash_eur=D("1000"), positions={},
        gross_eur=D("0"), net_eur=D("0"),
    )
    db.save_portfolio("baseline", state)
    start = float(db.one("SELECT MIN(captured_at) AS start_at FROM portfolio_snapshots")["start_at"])
    return db, start


def test_external_eur_flows_are_idempotent_and_net_of_withdrawal_fee(tmp_path):
    db, start = _portfolio_db(tmp_path)
    gateway = LedgerGateway(start)
    reconciler = PortfolioReconciler(gateway, db)
    now = start + 10

    complete, series_start = reconciler._sync_external_cash_flows(now)
    assert complete is True
    assert series_start == start
    assert reconciler._external_flow_sum(start, now) == D("79.5")
    assert len(db.query("SELECT * FROM portfolio_external_cash_flows")) == 2

    # Repeated polling inside the sync interval must not hit the private endpoint.
    complete_again, same_start = reconciler._sync_external_cash_flows(now + 3600)
    assert complete_again is True
    assert same_start == start
    assert len(gateway.calls) == 2


def test_unvalued_external_flow_keeps_drawdown_basis_unadjusted(tmp_path):
    db, start = _portfolio_db(tmp_path)
    gateway = LedgerGateway(start, foreign_asset=True)
    reconciler = PortfolioReconciler(gateway, db)

    complete, _ = reconciler._sync_external_cash_flows(start + 10)
    assert complete is False
    row = db.one("SELECT asset,flow_eur FROM portfolio_external_cash_flows")
    assert row["asset"] == "USD"
    assert row["flow_eur"] is None
