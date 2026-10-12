from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

D = Decimal


class CycleExposureLedger:
    """Conservative working portfolio for one order-decision cycle.

    Accepted, partially filled and exchange-unknown orders reserve their target
    exposure until the next exchange reconciliation. This ledger is process-local;
    persisted order recovery remains the cross-restart authority.
    """

    def __init__(self) -> None:
        self._targets: dict[str, tuple[D, D]] = {}
        self._reserved_cash_eur = D("0")
        self._reserved_margin_eur = D("0")

    def reserve(self, decision: Any, leverage: Any) -> dict[str, str]:
        target = D(str(decision.target_position_eur))
        current = D(str(decision.current_position_eur))
        lev = D(str(leverage))
        if not lev.is_finite() or lev < D("1"):
            lev = D("1")

        same_side = current == 0 or target == 0 or current * target > 0
        incremental = (
            max(D("0"), abs(target) - abs(current))
            if same_side else abs(target)
        )
        capital = incremental / lev if lev > D("1") else incremental
        margin = incremental / lev if lev > D("1") else D("0")
        self._targets[str(decision.instrument.symbol)] = (target, current)
        self._reserved_cash_eur += capital
        self._reserved_margin_eur += margin
        return {
            "symbol": str(decision.instrument.symbol),
            "target_position_eur": str(target),
            "incremental_notional_eur": str(incremental),
            "reserved_cash_eur": str(self._reserved_cash_eur),
            "reserved_margin_eur": str(self._reserved_margin_eur),
        }

    def project(self, base: Any) -> Any:
        original = {
            str(symbol): D(str(value))
            for symbol, value in dict(base.positions).items()
            if D(str(value)) != 0
        }
        positions = dict(original)
        for symbol, (target, current) in self._targets.items():
            existing = original.get(symbol, current)
            # Do not pre-credit a pending close or same-side trim: it may not fill.
            if target == 0 or (existing != 0 and existing * target > 0 and abs(target) < abs(existing)):
                positions[symbol] = existing
                continue
            if existing != 0 and target != 0 and existing * target < 0:
                # During a reversal, prefer the possible side that creates the
                # larger portfolio-wide net exposure. Gross is reserved below for
                # the larger absolute amount on either side.
                without_symbol = sum(
                    (value for key, value in positions.items() if key != symbol), D("0")
                )
                positions[symbol] = (
                    target if abs(without_symbol + target) > abs(without_symbol + existing)
                    else existing
                )
            else:
                positions[symbol] = target

        net = sum(positions.values(), D("0"))
        gross = D("0")
        symbols = set(positions) | set(self._targets)
        for symbol in symbols:
            value = abs(positions.get(symbol, D("0")))
            if symbol in self._targets:
                target, existing = self._targets[symbol]
                value = max(value, abs(original.get(symbol, existing)), abs(target))
            gross += value
        return replace(
            base,
            positions=positions,
            gross_eur=gross,
            net_eur=net,
            cash_eur=D(str(base.cash_eur)) - self._reserved_cash_eur,
            margin_used_eur=D(str(base.margin_used_eur)) + self._reserved_margin_eur,
        )

    @property
    def reserved_cash_eur(self) -> D:
        return self._reserved_cash_eur

    @property
    def reserved_margin_eur(self) -> D:
        return self._reserved_margin_eur
