from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


D=Decimal


@dataclass(frozen=True)
class CostEstimate:
    fee_bps: D
    spread_bps: D
    slippage_bps: D
    impact_bps: D
    funding_bps: D
    financing_bps: D
    fx_bps: D
    safety_buffer_bps: D

    @property
    def total_bps(self) -> D:
        return sum((
            self.fee_bps,self.spread_bps,self.slippage_bps,self.impact_bps,
            self.funding_bps,self.financing_bps,self.fx_bps,self.safety_buffer_bps
        ), D("0"))


class CostModel:
    def estimate(self, market: Any, notional_eur: D, leverage: D= D("1"),
                 fee_bps: D=D("40"), funding_bps: D=D("0"), fx_bps: D=D("0")) -> CostEstimate:
        spread=market.spread_bps
        volatility=D(str(max(D("0"),market.metadata.get("volatility",D("0"))))) if hasattr(market,"metadata") else D("0")
        impact=max(D("2"), notional_eur/max(D("1"),market.volume_24h)*D("10000"))
        slippage=max(D("2"),min(D("80"),volatility*D("2")))
        financing=D("0") if leverage<=1 else (leverage-D("1"))*D("4")
        safety=max(D("5"),slippage*D("0.25"))
        return CostEstimate(
            fee_bps=fee_bps,spread_bps=spread,slippage_bps=slippage,
            impact_bps=impact,funding_bps=funding_bps,financing_bps=financing,
            fx_bps=fx_bps,safety_buffer_bps=safety,
        )


class ExecutionPolicy:
    def __init__(self, max_slippage_bps: float, max_reprices: int) -> None:
        self.max_slippage=D(str(max_slippage_bps))
        self.max_reprices=max_reprices

    def choose(self, spread_bps: D, edge_bps: D, volatility: D,
               urgency_bps_per_sec: D=D("0"), reduce_only: bool=False) -> dict[str,Any]:
        if reduce_only:
            # Exits should prefer a fillable limit over post-only liquidity.
            # The order only removes exposure, so crossing a tight spread is
            # acceptable as long as the hard slippage ceiling still passes.
            if spread_bps<=D("60") and volatility<=D("12"):
                return {"method":"marketable_limit","order_type":"limit","post_only":False}
            return {"method":"limit","order_type":"limit","post_only":False}
        if spread_bps<=D("15") and volatility<=D("5"):
            return {"method":"post_only_limit","order_type":"limit","post_only":True}
        if spread_bps<=D("60") and edge_bps>=spread_bps*D("1.5"):
            return {"method":"marketable_limit","order_type":"limit","post_only":False}
        if edge_bps>=D("100") and urgency_bps_per_sec>=D("2"):
            return {"method":"market","order_type":"market","post_only":False}
        return {"method":"limit","order_type":"limit","post_only":False}

    def validate(self, chosen: dict[str,Any], expected_edge_bps: D,
                 estimated_slippage_bps: D, reduce_only: bool=False,
                 max_slippage_bps: D|None=None) -> tuple[bool,str]:
        ceiling = self.max_slippage if max_slippage_bps is None else max(
            self.max_slippage,
            D(str(max_slippage_bps)),
        )
        if estimated_slippage_bps>ceiling:
            return False,"ESTIMATED_SLIPPAGE_EXCEEDS_LIMIT"
        if reduce_only:
            return True,"REDUCE_ONLY_RISK_REDUCTION_OK"
        if expected_edge_bps<=estimated_slippage_bps:
            return False,"EDGE_DOES_NOT_CLEAR_SLIPPAGE"
        if chosen.get("method")=="market" and expected_edge_bps<D("50"):
            return False,"MARKET_TOO_EXPENSIVE_FOR_EDGE"
        return True,"EXECUTION_POLICY_OK"
