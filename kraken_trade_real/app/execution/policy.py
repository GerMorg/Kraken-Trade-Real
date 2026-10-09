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
    # Kraken Spot Margin rollover begins after four hours and repeats every
    # four hours. Rates vary by asset and market conditions, so callers may
    # provide the observed rate in market.metadata. The fallback is a
    # conservative 4 bps (0.04%) per component on borrowed notional. Rates
    # vary by asset and conditions; this is a fallback, not a live quote.
    DEFAULT_MARGIN_OPEN_FEE_BPS = D("4")
    DEFAULT_MARGIN_ROLLOVER_FEE_BPS = D("4")
    MARGIN_ROLLOVER_INTERVAL_HOURS = D("4")

    def estimate(
        self,
        market: Any,
        notional_eur: D,
        leverage: D = D("1"),
        fee_bps: D = D("40"),
        funding_bps: D = D("0"),
        fx_bps: D = D("0"),
        holding_hours: D = D("4"),
        margin_open_fee_bps: D | None = None,
        margin_rollover_fee_bps: D | None = None,
    ) -> CostEstimate:
        spread = D(str(market.spread_bps))
        metadata = market.metadata if hasattr(market, "metadata") else {}
        volatility = D(str(max(D("0"), metadata.get("volatility", D("0")))))
        impact = max(D("2"), notional_eur / max(D("1"), D(str(market.volume_24h))) * D("10000"))
        slippage = max(D("2"), min(D("80"), volatility * D("2")))
        if leverage <= D("1"):
            financing = D("0")
        else:
            borrowed_share = (leverage - D("1")) / leverage
            opening_rate = D(str(
                margin_open_fee_bps
                if margin_open_fee_bps is not None
                else metadata.get("margin_open_fee_bps", self.DEFAULT_MARGIN_OPEN_FEE_BPS)
            ))
            rollover_rate = D(str(
                margin_rollover_fee_bps
                if margin_rollover_fee_bps is not None
                else metadata.get(
                    "margin_rollover_fee_bps", self.DEFAULT_MARGIN_ROLLOVER_FEE_BPS
                )
            ))
            hours = max(D("0"), D(str(holding_hours)))
            # A rollover is charged at each completed four-hour boundary.
            rollover_periods = int(hours / self.MARGIN_ROLLOVER_INTERVAL_HOURS)
            financing = borrowed_share * (
                opening_rate + rollover_rate * D(rollover_periods)
            )
        safety = max(D("5"), slippage * D("0.25"))
        return CostEstimate(
            fee_bps=fee_bps,
            spread_bps=spread,
            slippage_bps=slippage,
            impact_bps=impact,
            funding_bps=funding_bps,
            financing_bps=financing,
            fx_bps=fx_bps,
            safety_buffer_bps=safety,
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
                 estimated_slippage_bps: D, reduce_only: bool=False) -> tuple[bool,str]:
        if estimated_slippage_bps>self.max_slippage:
            return False,"ESTIMATED_SLIPPAGE_EXCEEDS_LIMIT"
        if reduce_only:
            return True,"REDUCE_ONLY_RISK_REDUCTION_OK"
        if expected_edge_bps<=estimated_slippage_bps:
            return False,"EDGE_DOES_NOT_CLEAR_SLIPPAGE"
        if chosen.get("method")=="market" and expected_edge_bps<D("50"):
            return False,"MARKET_TOO_EXPENSIVE_FOR_EDGE"
        return True,"EXECUTION_POLICY_OK"
