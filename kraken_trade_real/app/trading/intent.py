from __future__ import annotations

from decimal import Decimal

from app.domain.models import (
    Decision,
    OrderIntent,
    new_client_order_id,
    new_id,
    quantize_limit_price,
    quantize_order_quantity,
)
from app.domain.states import Direction

D = Decimal


class OrderIntentBuilder:
    def __init__(self, max_slippage_bps: float, timeout_seconds: int) -> None:
        self.max_slippage = D(str(max_slippage_bps))
        self.timeout_seconds = timeout_seconds

    def build(
        self,
        decision: Decision,
        leverage: D,
        order_type: str,
        quantity: D,
        limit_price: D | None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> OrderIntent:
        kind = str(order_type).strip().lower().replace("_", "-")
        direction = decision.execution_direction or decision.signal.direction
        side = "buy" if direction == Direction.LONG else "sell"
        if kind == "settle-position":
            # Kraken settlement uses the settlement side: short -> sell/settle,
            # long -> buy/settle. This is intentionally not the normal close side.
            current = D(str(decision.current_position_eur))
            if current < 0:
                direction, side = Direction.SHORT, "sell"
            elif current > 0:
                direction, side = Direction.LONG, "buy"
            else:
                raise ValueError("SETTLE_POSITION_REQUIRES_NONZERO_POSITION")
        normalized_quantity = quantize_order_quantity(
            decision.instrument, D(str(quantity))
        )
        # Quote-side price hints are not limits. Never persist a limit price on
        # a market order, even if the runtime passed the current bid/ask.
        normalized_price = (
            None
            if kind in {"market", "mkt"}
            else quantize_limit_price(decision.instrument, limit_price, side)
        )
        return OrderIntent(
            new_id("intent"),
            new_client_order_id(),
            decision.decision_id,
            decision.instrument,
            direction,
            side,
            order_type,
            normalized_quantity,
            normalized_price,
            leverage,
            leverage > D("1"),
            reduce_only,
            decision.signal.net_edge_bps,
            self.max_slippage,
            self.timeout_seconds,
            post_only,
        )
