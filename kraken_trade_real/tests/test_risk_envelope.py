from decimal import Decimal as D
from types import SimpleNamespace

from app.domain.models import Decision, Instrument, PortfolioState, Signal
from app.domain.states import Direction, ProductType
from app.risk.engine import RiskEngine


class _Margin:
    @staticmethod
    def available(_account, _requested):
        return True, "OK"


def _instrument():
    return Instrument(
        venue="spot",
        product_type=ProductType.SPOT,
        symbol="X/USD",
        instrument_id="XUSD",
        altname="XUSD",
        base="X",
        quote="USD",
        status="online",
        margin_available=True,
        long_available=True,
        short_available=True,
        leverage_levels=(D("1"), D("2"), D("3")),
        min_order_qty=D("0.001"),
        min_cost=D("1"),
        lot_decimals=3,
        price_decimals=2,
        tick_size=D("0.01"),
        margin_class="spot",
    )


def _config():
    # Deliberately simulate un-migrated legacy options bypassing Config.load().
    return SimpleNamespace(
        risk_max_position_pct=80,
        risk_max_gross_pct=100,
        risk_max_net_pct=70,
        risk_max_margin_pct=80,
        risk_max_leverage=5,
        risk_max_open_positions=10,
        risk_daily_loss_pct=10,
        risk_max_drawdown_pct=30,
        risk_cash_reserve_pct=0,
        strategy_min_edge_bps=25,
        strategy_min_confidence=0.55,
        strategy_adaptive_edge_floor_bps=15,
        strategy_adaptive_cost_ratio=1.1,
    )


def _decision(instrument, *, current=D("0"), target=D("20"), reduce_only=False):
    direction = Direction.LONG if target >= current else Direction.SHORT
    signal = Signal(
        instrument.symbol,
        direction,
        D("1000"),
        D("100"),
        D("0.99"),
        "TREND",
        D("0"),
        D("0"),
        {"volatility": D("5")},
    )
    return Decision(
        decision_id="risk-envelope-test",
        instrument=instrument,
        signal=signal,
        target_notional_eur=abs(target - current),
        leverage=D("1"),
        rationale={"risk_profile": "core", "min_cost_eur": "1"},
        strategy_version="test",
        model_version="test",
        config_hash="test",
        current_position_eur=current,
        target_position_eur=target,
        execution_direction=direction,
        reduce_only=reduce_only,
    )


def test_final_risk_gate_clamps_legacy_values_independently_of_config_load():
    instrument = _instrument()
    portfolio = PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("70"),
        positions={"A/EUR": D("10"), "B/EUR": D("10"), "C/EUR": D("10")},
        gross_eur=D("30"),
        net_eur=D("30"),
        daily_pnl_eur=D("-4"),
        drawdown_pct=D("9"),
    )
    engine = RiskEngine(_config(), _Margin(), object(), object())

    result = engine.evaluate(_decision(instrument), portfolio, None)

    assert result.allowed is False
    assert result.checks["position_limit"] is False  # 20% target > immutable 15%
    assert result.checks["daily_loss"] is False  # -4% exceeds immutable 3%
    assert result.checks["drawdown"] is False  # 9% exceeds immutable 8%
    assert result.checks["open_positions_limit"] is False  # max 3, not legacy 10
    assert result.checks["net_limit"] is True  # global 50% cap still allows 50% exactly


def test_reduce_only_exit_is_not_blocked_by_hard_risk_envelope():
    instrument = _instrument()
    portfolio = PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("0"),
        positions={"X/USD": D("20"), "A/EUR": D("10"), "B/EUR": D("10")},
        gross_eur=D("40"),
        net_eur=D("40"),
        daily_pnl_eur=D("-10"),
        drawdown_pct=D("50"),
    )
    engine = RiskEngine(_config(), _Margin(), object(), object())

    result = engine.evaluate(
        _decision(instrument, current=D("20"), target=D("0"), reduce_only=True),
        portfolio,
        None,
    )

    assert result.allowed is True
    assert result.checks["reduce_only_semantics"] is True


def test_core_entry_with_positive_but_weak_net_edge_is_rejected():
    instrument = _instrument()
    portfolio = PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("100"),
        positions={},
        gross_eur=D("0"),
        net_eur=D("0"),
    )
    config = _config()
    config.strategy_min_edge_bps = 15
    config.strategy_stop_loss_pct = 2.0
    engine = RiskEngine(config, _Margin(), object(), object())
    weak_edge = _decision(instrument)
    weak_signal = Signal(
        instrument.symbol,
        Direction.LONG,
        D("249"),
        D("208"),
        D("0.95"),
        "TREND",
        D("0"),
        D("0"),
        {"volatility": D("5")},
    )
    weak_edge = Decision(
        decision_id=weak_edge.decision_id,
        instrument=instrument,
        signal=weak_signal,
        target_notional_eur=D("10"),
        leverage=D("1"),
        rationale={"risk_profile": "core", "edge_tier": "ADAPTIVE",
                   "min_cost_eur": "1"},
        strategy_version="test",
        model_version="test",
        config_hash="test",
        current_position_eur=D("0"),
        target_position_eur=D("10"),
        execution_direction=Direction.LONG,
        reduce_only=False,
    )

    result = engine.evaluate(weak_edge, portfolio, None)

    assert result.allowed is False
    assert result.reason == "edge_positive"
    assert result.checks["edge_positive"] is False



def test_tactical_profile_cannot_override_immutable_single_position_cap():
    instrument = _instrument()
    portfolio = PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("100"),
        positions={},
        gross_eur=D("0"),
        net_eur=D("0"),
        daily_pnl_eur=D("0"),
        drawdown_pct=D("0"),
    )
    decision = _decision(instrument, target=D("20"))
    decision.rationale.update({
        "risk_profile": "tactical",
        "risk_position_limit_pct": "75",
        "risk_max_leverage": "5",
    })
    engine = RiskEngine(_config(), _Margin(), object(), object())

    result = engine.evaluate(decision, portfolio, None)

    assert result.allowed is False
    assert result.checks["position_limit"] is False
    assert result.diagnostics["position_limit_pct"] == "15"


def test_tactical_profile_cannot_raise_single_position_limit_through_learned_parameter():
    instrument = _instrument()
    portfolio = PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("100"),
        positions={},
        gross_eur=D("0"),
        net_eur=D("0"),
        daily_pnl_eur=D("0"),
        drawdown_pct=D("0"),
    )
    decision = _decision(instrument, target=D("14"))
    decision.rationale.update({
        "risk_profile": "tactical",
        "risk_position_limit_pct": "99",
    })
    result = RiskEngine(_config(), _Margin(), object(), object()).evaluate(
        decision, portfolio, None
    )

    assert result.checks["position_limit"] is True
    assert result.diagnostics["position_limit_pct"] == "15"
