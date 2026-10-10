from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.domain.models import Decision, Instrument, PortfolioState, Signal
from app.domain.states import Direction, ProductType
from app.risk.engine import RiskEngine


D = Decimal


def make_config():
    return SimpleNamespace(
        risk_max_position_pct=15.0,
        risk_max_gross_pct=80.0,
        risk_max_net_pct=50.0,
        risk_max_open_positions=3,
        risk_daily_loss_pct=3.0,
        risk_max_drawdown_pct=8.0,
        risk_cash_reserve_pct=20.0,
        risk_max_leverage=3.0,
        risk_max_margin_pct=35.0,
        strategy_min_edge_bps=25.0,
        strategy_min_confidence=0.58,
    )


def make_instrument():
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT_MARGIN,
        symbol="BTC/USD",
        instrument_id="XXBTZUSD",
        altname="XBTUSD",
        base="XBT",
        quote="USD",
        status="online",
        margin_available=True,
        long_available=True,
        short_available=True,
        leverage_levels=(D("1"), D("2"), D("3")),
        min_order_qty=D("0.0001"),
        min_cost=D("5"),
        lot_decimals=8,
        price_decimals=2,
        tick_size=D("0.01"),
        margin_class="spot-margin",
        metadata={"leverage_buy": ["1", "2", "3"], "leverage_sell": ["1", "2", "3"]},
    )


def make_snapshot():
    from app.domain.models import MarketSnapshot
    closes = tuple(D("100") + D(i) / D("10") for i in range(20))
    return MarketSnapshot(
        symbol="BTC/USD",
        price=D("101.9"),
        bid=D("101.89"),
        ask=D("101.91"),
        volume_24h=D("1000000"),
        timestamp=1.0,
        closes=closes,
    )


def test_tactical_position_limit_can_use_25_percent_without_changing_core_limit():
    cfg = make_config()
    engine = RiskEngine(
        cfg,
        SimpleNamespace(available=lambda account, amount: (True, "OK")),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    signal = Signal(
        "BTC/USD",
        Direction.LONG,
        D("400"),
        D("100"),
        D("0.9"),
        "TACTICAL_VOLATILITY",
        D("0"),
        D("0"),
        {"volatility": D("50")},
    )
    instrument = make_instrument()
    decision = Decision(
        "decision",
        instrument,
        signal,
        D("12.5"),
        D("1"),
        {"risk_profile": "tactical", "risk_position_limit_pct": D("25"), "risk_volatility_max": D("55")},
        "tactical-volatility-v1",
        "tactical-v1",
        "",
        D("0"),
        D("12.5"),
        Direction.LONG,
        False,
    )
    portfolio = PortfolioState(
        equity_eur=D("50"),
        cash_eur=D("50"),
        positions={},
        gross_eur=D("0"),
        net_eur=D("0"),
        margin_used_eur=D("0"),
        unrealized_pnl_eur=D("0"),
        realized_pnl_eur=D("0"),
        daily_pnl_eur=D("0"),
        drawdown_pct=D("0"),
        open_orders=0,
        source_timestamp=1.0,
        spot_open_positions_read_ok=True,
    )
    result = engine.evaluate(decision, portfolio, make_snapshot(), {})
    assert result.allowed is True
    assert result.checks["position_limit"] is True
    assert result.checks["extreme_volatility"] is True


def test_tactical_short_uses_absolute_margin_exposure():
    cfg = make_config()
    engine = RiskEngine(
        cfg,
        SimpleNamespace(
            available=lambda account, amount: (
                D(str(account["free_margin"])) >= amount, "OK"
            )
        ),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    instrument = make_instrument()
    signal = Signal(
        "BTC/USD", Direction.SHORT, D("400"), D("100"), D("0.9"),
        "TACTICAL_VOLATILITY", D("0"), D("0"), {"volatility": D("50")}
    )
    decision = Decision(
        "decision-short-margin",
        instrument,
        signal,
        D("-12.5"),
        D("2"),
        {
            "risk_profile": "tactical",
            "risk_position_limit_pct": D("25"),
            "risk_volatility_max": D("55"),
            "min_cost_eur": "5",
        },
        "tactical-volatility-v1",
        "tactical-v1",
        "",
        D("0"),
        D("-12.5"),
        Direction.SHORT,
        False,
    )
    portfolio = PortfolioState(
        equity_eur=D("50"), cash_eur=D("50"), positions={},
        gross_eur=D("0"), net_eur=D("0"), margin_used_eur=D("0"),
        unrealized_pnl_eur=D("0"), realized_pnl_eur=D("0"),
        daily_pnl_eur=D("0"), drawdown_pct=D("0"), open_orders=0,
        source_timestamp=1.0,
        spot_open_positions_read_ok=True,
    )
    blocked = engine.evaluate(
        decision, portfolio, make_snapshot(),
        {"free_margin": "5", "margin_level_pct": "500"},
    )
    assert blocked.allowed is False
    assert blocked.reason == "margin_free"
    allowed = engine.evaluate(
        decision, portfolio, make_snapshot(),
        {"free_margin": "7", "margin_level_pct": "500"},
    )
    assert allowed.allowed is True
    assert allowed.checks["minimum_cost"] is True
    assert allowed.checks["margin_budget"] is True


def test_tactical_volatility_guard_still_blocks_extreme_market():
    cfg = make_config()
    engine = RiskEngine(
        cfg,
        SimpleNamespace(available=lambda account, amount: (True, "OK")),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    signal = Signal(
        "BTC/USD",
        Direction.LONG,
        D("800"),
        D("100"),
        D("0.95"),
        "TACTICAL_VOLATILITY",
        D("0"),
        D("0"),
        {"volatility": D("61")},
    )
    d = Decision(
        "decision",
        make_instrument(),
        signal,
        D("10"),
        D("1"),
        {"risk_profile": "tactical", "risk_position_limit_pct": D("25"), "risk_volatility_max": D("55")},
        "tactical-volatility-v1",
        "tactical-v1",
        "",
        D("0"),
        D("10"),
        Direction.LONG,
        False,
    )
    p = PortfolioState(
        D("50"), D("50"), {}, D("0"), D("0"), D("0"), D("0"), D("0"),
        D("0"), D("0"), 0, 1, spot_open_positions_read_ok=True,
    )
    result = engine.evaluate(d, p, make_snapshot(), {})
    assert result.allowed is False
    assert result.reason == "extreme_volatility"


def test_high_conviction_tactical_exposure_respects_global_risk_gates():
    cfg = make_config()
    engine = RiskEngine(
        cfg,
        SimpleNamespace(
            available=lambda account, amount: (
                D(str(account["free_margin"])) >= amount, "OK"
            )
        ),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    instrument = make_instrument()
    signal = Signal(
        "BTC/USD", Direction.LONG, D("400"), D("100"), D("0.95"),
        "TACTICAL_VOLATILITY", D("0"), D("0"), {"volatility": D("50")},
    )
    decision = Decision(
        "decision-high-conviction",
        instrument,
        signal,
        D("37.5"),
        D("3"),
        {
            "risk_profile": "tactical",
            "risk_position_limit_pct": D("80"),
            "risk_volatility_max": D("55"),
            "min_cost_eur": "5",
        },
        "tactical-volatility-v1",
        "tactical-v1",
        "",
        D("0"),
        D("37.5"),
        Direction.LONG,
        False,
    )
    portfolio = PortfolioState(
        equity_eur=D("50"), cash_eur=D("50"), positions={},
        gross_eur=D("0"), net_eur=D("0"), margin_used_eur=D("0"),
        unrealized_pnl_eur=D("0"), realized_pnl_eur=D("0"), daily_pnl_eur=D("0"),
        drawdown_pct=D("0"), open_orders=0, source_timestamp=1.0,
        spot_open_positions_read_ok=True,
    )
    result = engine.evaluate(
        decision, portfolio, make_snapshot(),
        {"free_margin": "20", "margin_level_pct": "500"},
    )
    assert result.allowed is True
    assert result.checks["position_limit"] is True
    assert result.checks["gross_limit"] is True
    assert result.checks["net_limit"] is True
    assert result.checks["margin_budget"] is True
