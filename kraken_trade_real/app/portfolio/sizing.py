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
