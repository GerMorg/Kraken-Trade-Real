from __future__ import annotations

from decimal import Decimal

from .learning import walk_forward_validate


def cost_adjusted_returns(gross: list[Decimal], fee: Decimal, spread: Decimal, slippage: Decimal) -> list[Decimal]:
    drag = fee + spread + slippage
    return [r - drag for r in gross]


def validation_summary(returns: list[Decimal]) -> dict[str, Decimal]:
    if not returns:
        return {"net": Decimal("0"), "max_drawdown": Decimal("0"), "expectancy": Decimal("0")}
    equity = Decimal("1")
    peak = equity
    max_dd = Decimal("0")
    for r in returns:
        equity *= Decimal("1") + r
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else Decimal("0"))
    return {"net": equity - Decimal("1"), "max_drawdown": max_dd, "expectancy": sum(returns, Decimal("0")) / Decimal(len(returns))}


def walk_forward_report(returns: list[Decimal], train: int = 100, test: int = 25, step: int = 25) -> list[dict[str, Decimal]]:
    return walk_forward_validate(returns, train, test, step)
