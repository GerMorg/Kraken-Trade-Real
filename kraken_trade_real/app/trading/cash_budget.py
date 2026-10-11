from __future__ import annotations

from decimal import Decimal
from typing import Any


D = Decimal


def spot_cash_required_quote(quantity: D, price: D, config: Any) -> D:
    """Conservative quote-currency requirement for a non-margin Spot order.

    Reserve the larger of the Normal and Tactical entry fee assumptions, plus
    the execution slippage ceiling and a small rounding/fee safety buffer.
    Keeping one shared formula prevents funding, runtime and order preflight
    from disagreeing about what an order can actually afford.
    """
    if quantity <= 0 or price <= 0:
        return D("0")
    fees = max(
        D("0"),
        D(str(getattr(config, "strategy_entry_fee_bps", 40))),
        D(str(getattr(config, "tactical_entry_fee_bps", 80))),
    )
    slippage = max(
        D("0"),
        D(str(getattr(config, "execution_max_slippage_bps", 40))),
    )
    safety = D("15")
    return quantity * price * (D("1") + (fees + slippage + safety) / D("10000"))
