from __future__ import annotations

from decimal import Decimal

from .models import MarketSnapshot


class ExecutionPlanner:
    """Choose passive/aggressive execution from spread, depth, urgency and slippage budget."""

    def __init__(self, max_slippage: Decimal) -> None:
        self.max_slippage = max_slippage

    def plan(self, snapshot: MarketSnapshot, direction, requested_notional: Decimal, quantity: Decimal, leverage: Decimal, **kwargs) -> tuple[str, Decimal | None, bool]:
        del leverage
        spread = snapshot.spread_ratio
        depth = sum((q * p for p, q in snapshot.book.bids[:10]), Decimal("0")) + sum((q * p for p, q in snapshot.book.asks[:10]), Decimal("0"))
        urgency = Decimal(str(kwargs.get("urgency", "0.5")))
        if quantity <= 0 or requested_notional <= 0:
            return "blocked", None, False
        limit_price = snapshot.ask if str(direction) == "long" else snapshot.bid
        if spread <= Decimal("0.001") and depth >= requested_notional * Decimal("5") and urgency < Decimal("0.8"):
            return "limit", limit_price, True
        if spread <= self.max_slippage:
            if urgency >= Decimal("0.8"):
                return "market", None, False
            return "limit", limit_price, False
        if spread <= Decimal("0.003") and urgency < Decimal("0.8"):
            return "limit", limit_price, False
        return "blocked", None, False
