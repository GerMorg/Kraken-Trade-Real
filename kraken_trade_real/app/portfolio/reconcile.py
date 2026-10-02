
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import time
from typing import Any

from app.domain.models import PortfolioState

D = Decimal


def dec(v: Any) -> D:
    try:
        return D(str(v if v not in (None, "") else "0"))
    except (InvalidOperation, ValueError):
        return D(0)


class PortfolioReconciler:
    def __init__(self, gateway: Any, db: Any) -> None:
        self.gateway = gateway
        self.db = db
        self.margin_account: dict[str, Any] | None = None

    def reconcile(self) -> PortfolioState:
        cash = equity = gross = net = margin = unreal = realized = D(0)
        positions: dict[str, D] = {}
        if not self.gateway.api_key:
            self.margin_account = None
            return PortfolioState()
        try:
            balances = self.gateway.spot_balance()
            cash = sum(
                (dec(v) for a, v in balances.items() if str(a).upper() in {"ZEUR", "EUR"}),
                D(0),
            )
            tb = self.gateway.spot_private("TradeBalance", {"aclass": "currency", "asset": "ZEUR"})
            equity = dec(tb.get("eb") or tb.get("tb") or cash)
            realized = dec(tb.get("n"))
        except Exception as exc:
            self.db.event("PORTFOLIO_SPOT_READ_FAILED", "WARNING", {"error": type(exc).__name__})
        try:
            for item in self.gateway.spot_open_positions().values():
                if isinstance(item, dict):
                    symbol = str(item.get("pair") or item.get("symbol") or "")
                    value = dec(item.get("value") or item.get("cost"))
                    if symbol:
                        positions[symbol] = value
                        gross += abs(value)
                        net += value
        except Exception as exc:
            self.db.event("PORTFOLIO_SPOT_POSITIONS_FAILED", "WARNING", {"error": type(exc).__name__})
        try:
            accounts = self.gateway.futures_accounts().get("accounts", {})
            acct: dict[str, Any] = (
                next(iter(accounts.values()), {}) if isinstance(accounts, dict) else {}
            )
            self.margin_account = {
                "free_margin": str(acct.get("availableMargin") or acct.get("freeMargin") or 0),
                "margin_level_pct": str(acct.get("marginLevel") or 9999),
            }
            equity = max(equity, dec(acct.get("portfolioValue") or acct.get("equity") or 0))
            margin += dec(acct.get("initialMargin") or acct.get("marginUsed") or 0)
            unreal += dec(acct.get("unrealizedPnl") or 0)
            rows = self.gateway.futures_open_positions().get("openPositions", [])
            for item in rows if isinstance(rows, list) else []:
                if isinstance(item, dict):
                    symbol = str(item.get("symbol") or "")
                    value = dec(item.get("value") or item.get("size") or item.get("quantity"))
                    if symbol:
                        positions[symbol] = value
                        gross += abs(value)
                        net += value if str(item.get("side", "buy")).lower() == "buy" else -abs(value)
        except Exception as exc:
            self.margin_account = None
            self.db.event("PORTFOLIO_FUTURES_READ_FAILED", "WARNING", {"error": type(exc).__name__})
        equity = equity if equity > 0 else cash + max(D(0), unreal)
        peak_row = self.db.one("SELECT MAX(CAST(equity_eur AS REAL)) AS peak FROM portfolio_snapshots")
        peak = dec(peak_row.get("peak") if peak_row else equity)
        drawdown = (D(1) - equity / peak) * 100 if peak > 0 and equity < peak else D(0)
        return PortfolioState(
            equity, cash, positions, gross, net, margin, unreal, realized,
            unreal + realized, drawdown, 0, time.time()
        )
