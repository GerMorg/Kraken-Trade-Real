from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.domain.states import ProductType
from app.portfolio.sizing import exchange_order_minimum_violation


def test_exchange_minimum_gate_reports_quantity_before_order_submission(instrument):
    constrained = replace(instrument, min_order_qty=Decimal("0.01"))
    result = exchange_order_minimum_violation(
        constrained, Decimal("0.009"), Decimal("100")
    )
    assert result == {
        "reason": "MIN_ORDER_QTY",
        "quantity": "0.009",
        "minimum_order_qty": "0.01",
    }


def test_exchange_minimum_gate_reports_quote_cost_below_minimum(instrument):
    result = exchange_order_minimum_violation(
        instrument, Decimal("0.001"), Decimal("100")
    )
    assert result == {
        "reason": "MIN_ORDER_COST",
        "quantity": "0.001",
        "reference_price": "100",
        "estimated_cost_quote": "0.100",
        "minimum_order_cost_quote": "0.5",
    }


def test_futures_minimum_gate_uses_contract_quantity_not_spot_cost(instrument):
    futures = replace(
        instrument,
        venue="futures",
        product_type=ProductType.DERIVATIVE,
        min_order_qty=Decimal("1"),
        min_cost=Decimal("100000"),
    )
    assert exchange_order_minimum_violation(
        futures, Decimal("1"), Decimal("1")
    ) is None


def test_exchange_minimum_gate_never_accepts_nonpositive_or_invalid_quantity(instrument):
    assert exchange_order_minimum_violation(
        instrument, Decimal("0"), Decimal("100")
    ) == {"reason": "ORDER_MINIMUM_METADATA_INVALID"}
