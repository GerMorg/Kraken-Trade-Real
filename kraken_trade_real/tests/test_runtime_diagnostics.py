from decimal import Decimal as D
from types import SimpleNamespace
from app.runtime.runtime import _risk_diagnostic_context


def test_tactical_net_diagnostic_uses_global_limit():
    config = SimpleNamespace(
        risk_daily_loss_pct=3, risk_max_drawdown_pct=8, risk_cash_reserve_pct=20,
        risk_max_position_pct=15, risk_max_gross_pct=80, risk_max_net_pct=50,
        risk_max_open_positions=3, risk_max_margin_pct=35, strategy_min_confidence=0.58,
    )
    decision = SimpleNamespace(
        rationale={"risk_profile": "tactical", "risk_position_limit_pct": "80"},
        current_position_eur=D("0"), target_position_eur=D("60"), leverage=D("2"),
        signal=SimpleNamespace(confidence=D(".9"), expected_return_bps=D("600"),
            expected_cost_bps=D("100"), net_edge_bps=D("500")),
    )
    portfolio = SimpleNamespace(equity_eur=D("100"), positions={}, cash_eur=D("80"),
        daily_pnl_eur=D("0"), drawdown_pct=D("0"), gross_eur=D("0"),
        net_eur=D("0"), margin_used_eur=D("0"))
    risk = SimpleNamespace(effective_leverage=D("2"), checks={"net_limit": False})
    result = _risk_diagnostic_context(decision, portfolio, risk, config)
    assert result["position_limit_pct"] == "15"
    assert result["net_limit_pct"] == "50"
    assert result["net_limit_eur"] == "50"
    assert D(result["projected_net_pct"]) == D("60")
    assert result["failed_checks"] == ["net_limit"]
