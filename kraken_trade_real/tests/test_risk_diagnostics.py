from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.runtime.runtime import _risk_diagnostic_context


def test_risk_diagnostics_explain_daily_loss_and_projected_exposure(config):
    decision = SimpleNamespace(
        current_position_eur=Decimal("100"),
        target_position_eur=Decimal("200"),
        leverage=Decimal("1"),
        rationale={"risk_profile": "core"},
        signal=SimpleNamespace(
            confidence=Decimal("0.8"),
            expected_return_bps=Decimal("120"),
            expected_cost_bps=Decimal("80"),
            net_edge_bps=Decimal("40"),
        ),
    )
    portfolio = SimpleNamespace(
        equity_eur=Decimal("1000"),
        cash_eur=Decimal("250"),
        daily_pnl_eur=Decimal("-40"),
        drawdown_pct=Decimal("2.5"),
        gross_eur=Decimal("500"),
        net_eur=Decimal("100"),
        margin_used_eur=Decimal("50"),
        positions={"BTC/EUR": Decimal("100"), "ETH/EUR": Decimal("400")},
    )
    risk = SimpleNamespace(
        effective_leverage=Decimal("1"),
        checks={"daily_loss": False, "gross_limit": True, "edge_positive": True},
    )

    context = _risk_diagnostic_context(decision, portfolio, risk, config)

    assert context["equity_eur"] == "1000"
    assert context["daily_pnl_eur"] == "-40"
    assert Decimal(context["daily_loss_limit_eur"]) == Decimal("30")
    assert Decimal(context["daily_loss_headroom_eur"]) == Decimal("-10")
    assert Decimal(context["projected_gross_eur"]) == Decimal("600")
    assert Decimal(context["projected_gross_pct"]) == Decimal("60")
    assert Decimal(context["gross_limit_eur"]) == Decimal("800")
    assert Decimal(context["projected_net_eur"]) == Decimal("200")
    assert Decimal(context["projected_net_pct"]) == Decimal("20")
    assert Decimal(context["target_position_pct"]) == Decimal("20")
    assert Decimal(context["position_limit_eur"]) == Decimal("150")
    assert Decimal(context["cash_reserve_min_eur"]) == Decimal("200")
    assert context["open_positions_count"] == 2
    assert context["failed_checks"] == ["daily_loss"]
