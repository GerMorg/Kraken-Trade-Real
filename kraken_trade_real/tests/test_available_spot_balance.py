from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.domain.models import MarketSnapshot, OrderIntent
from app.domain.states import Direction, OrderState
from app.monitoring import AuditLogger
from app.execution import ExecutionPolicy, ExecutionReconciler
from app.persistence.db import Database
from app.portfolio import PortfolioReconciler
from app.trading.authority import TradingAuthority


def test_portfolio_keeps_total_balance_separate_from_spendable_balance(
    db, fake_gateway, instrument
):
    fake_gateway.spot_balance_extended = lambda: {
        "ZEUR": {
            "balance": "100",
            "credit": "0",
            "credit_used": "0",
            "hold_trade": "65",
        },
        "XXBT": {
            "balance": "0.01",
            "credit": "0",
            "credit_used": "0",
            "hold_trade": "0.002",
        },
    }
    portfolio = PortfolioReconciler(fake_gateway, db)
    portfolio.set_market_context(
        [instrument],
        {"XXBTZEUR": {"c": ["60005"]}},
    )

    portfolio.reconcile()

    assert portfolio.cash_balance("EUR") == Decimal("100")
    assert portfolio.available_cash_balance("EUR") == Decimal("35")
    assert portfolio.cash_balance("BTC") == Decimal("0.01")
    assert portfolio.available_cash_balance("BTC") == Decimal("0.008")


def test_portfolio_fails_closed_for_spending_when_balance_ex_fails(
    db, fake_gateway, instrument
):
    def failed_balance_ex():
        raise RuntimeError("extended balance unavailable")

    fake_gateway.spot_balance_extended = failed_balance_ex
    fake_gateway.spot_balance = lambda: {"ZEUR": "50"}
    portfolio = PortfolioReconciler(fake_gateway, db)
    portfolio.set_market_context(
        [instrument],
        {"XXBTZEUR": {"c": ["60005"]}},
    )

    portfolio.reconcile()

    assert portfolio.cash_balance("EUR") == Decimal("50")
    assert portfolio.available_cash_balance("EUR") == Decimal("0")
    event = db.one(
        "SELECT payload_json FROM events WHERE code='PORTFOLIO_EXTENDED_BALANCE_FAILED'"
    )
    assert event is not None


def test_preflight_blocks_spot_buy_when_open_orders_hold_funds(
    config, db, instrument
):
    from dataclasses import replace

    instrument = replace(
        instrument,
        venue="spot",
        product_type=__import__("app.domain.states", fromlist=["ProductType"]).ProductType.SPOT,
        base="XXBT",
        quote="ZEUR",
        min_order_qty=Decimal("0.0001"),
        min_cost=Decimal("0.5"),
        leverage_levels=(Decimal("1"),),
        margin_available=False,
    )

    class AvailableBalanceGateway:
        def spot_balance_extended(self):
            return {
                "ZEUR": {
                    "balance": "100",
                    "credit": "0",
                    "credit_used": "0",
                    "hold_trade": "70",
                }
            }

    authority = TradingAuthority(
        config,
        AvailableBalanceGateway(),
        db,
        AuditLogger(False),
        ExecutionPolicy(
            config.execution_max_slippage_bps,
            config.execution_max_reprices,
        ),
        ExecutionReconciler(),
    )
    intent = OrderIntent(
        "intent-free-cash",
        "11111111-2222-4333-8444-555555555571",
        "decision-free-cash",
        instrument,
        Direction.LONG,
        "buy",
        "limit",
        Decimal("0.0008"),
        Decimal("60000"),
        Decimal("1"),
        False,
        False,
        Decimal("500"),
        Decimal("40"),
        45,
        state=OrderState.INTENT_CREATED,
    )
    market = MarketSnapshot(
        instrument.symbol,
        Decimal("60000"),
        Decimal("59999"),
        Decimal("60000"),
        Decimal("100000"),
        1.0,
        tuple(Decimal("60000") for _ in range(40)),
    )

    result = authority._preflight(intent, market)

    assert result["allowed"] is False
    assert result["reason"] == "SPOT_AVAILABLE_QUOTE_INSUFFICIENT"
    assert result["detail"]["asset"] == "ZEUR"
    assert Decimal(result["detail"]["available"]) == Decimal("30")
    assert Decimal(result["detail"]["required"]) > Decimal("48")


def test_spot_cash_budget_includes_fee_slippage_and_rounding_buffer(config):
    from app.trading.cash_budget import spot_cash_required_quote

    required = spot_cash_required_quote(
        Decimal("1"), Decimal("100"), config
    )
    assert required > Decimal("100")
    assert required == Decimal("101.35")
