from __future__ import annotations

from decimal import Decimal


D=Decimal


class RiskSizer:
    def __init__(self, max_position_pct: float, cash_reserve_pct: float) -> None:
        self.max_position_pct=D(str(max_position_pct))/100
        self.cash_reserve_pct=D(str(cash_reserve_pct))/100

    def target(self, equity: D, confidence: D, volatility: D, leverage: D,
               current_notional: D, min_cost: D) -> D:
        if equity<=0:
            return D("0")
        confidence=max(D("0"),min(D("1"),confidence))
        vol=max(D("1"),volatility)
        risk_budget=equity*self.max_position_pct
        quality=confidence*D("100")/vol
        notional=risk_budget*min(D("1"),quality)
        notional=min(notional,equity*(D("1")-self.cash_reserve_pct)*max(D("1"),leverage))
        if notional < min_cost:
            return D("0")
        if current_notional!=0 and abs(notional-current_notional) < min_cost:
            return D("0")
        return notional


def exchange_order_minimum_violation(
    instrument: object,
    quantity: Decimal,
    reference_price: Decimal,
) -> dict[str, str] | None:
    """Return a diagnostic when a proposed entry cannot satisfy exchange minimums.

    This is a pre-submit check only: it never rounds a trade upward to force an
    order through, and callers should retain the portfolio/risk budget unchanged.
    """
    try:
        qty = D(str(quantity))
        min_qty = D(str(getattr(instrument, "min_order_qty", 0) or 0))
        price = D(str(reference_price))
        min_cost = D(str(getattr(instrument, "min_cost", 0) or 0))
    except (ArithmeticError, TypeError, ValueError):
        return {"reason": "ORDER_MINIMUM_METADATA_INVALID"}
    if not qty.is_finite() or qty <= 0 or not min_qty.is_finite() or min_qty < 0:
        return {"reason": "ORDER_MINIMUM_METADATA_INVALID"}
    if not min_cost.is_finite() or min_cost < 0:
        return {"reason": "ORDER_MINIMUM_METADATA_INVALID"}
    if qty < min_qty:
        return {
            "reason": "MIN_ORDER_QTY",
            "quantity": str(qty),
            "minimum_order_qty": str(min_qty),
        }
    if str(getattr(instrument, "venue", "")).lower() != "futures" and min_cost > 0:
        if not price.is_finite() or price <= 0:
            return {"reason": "ORDER_MINIMUM_METADATA_INVALID"}
        estimated_cost = qty * price
        if estimated_cost < min_cost:
            return {
                "reason": "MIN_ORDER_COST",
                "quantity": str(qty),
                "reference_price": str(price),
                "estimated_cost_quote": str(estimated_cost),
                "minimum_order_cost_quote": str(min_cost),
            }
    return None
