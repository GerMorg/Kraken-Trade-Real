from decimal import Decimal
from types import SimpleNamespace
from app.execution import CostModel,ExecutionPolicy
from app.risk import LeverageEngine,MarginEngine
from app.risk.engine import RiskEngine

def test_cost_model_includes_all_cost_buckets():
    market=SimpleNamespace(spread_bps=Decimal("10"),volume_24h=Decimal("1000"),metadata={"volatility":Decimal("3")})
    cost=CostModel().estimate(market,Decimal("10"),Decimal("2"))
    assert cost.total_bps>cost.fee_bps and cost.financing_bps>0

def test_execution_policy_fails_when_slippage_clears_edge():
    ok,reason=ExecutionPolicy(20,2).validate({"method":"market"},Decimal("10"),Decimal("20"))
    assert not ok and "SLIPPAGE" in reason

def test_margin_and_leverage_bounds():
    assert MarginEngine().validate_leverage(Decimal("5"),Decimal("10"),Decimal("5"))[0] is True
    assert MarginEngine().validate_leverage(Decimal("6"),Decimal("10"),Decimal("5"))[0] is False
    assert LeverageEngine.IMMUTABLE_MAX_LEVERAGE==Decimal("5")


def test_decision_rejection_reason_distinguishes_edge_and_confidence(config, instrument):
    from app.domain.models import PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    features = {"volatility": Decimal("2")}
    weak = Signal(
        instrument.symbol, Direction.LONG, Decimal("4"), Decimal("1"), Decimal("0.9"),
        "TREND_UP", Decimal("0"), Decimal("0"), features
    )
    strong_edge_low_conf = Signal(
        instrument.symbol, Direction.SHORT, Decimal("12"), Decimal("1"), Decimal("0.3"),
        "TREND_DOWN", Decimal("0"), Decimal("0"), features
    )
    portfolio = PortfolioState(equity_eur=Decimal("46"), cash_eur=Decimal("46"))

    engine = DecisionEngine(config)
    assert engine.rejection_reason(
        instrument, weak, weak, portfolio, {}
    ) == "MIN_EDGE"
    assert engine.rejection_reason(
        instrument, strong_edge_low_conf, strong_edge_low_conf, portfolio, {}
    ) == "MIN_CONFIDENCE"


def test_reduce_only_execution_policy_allows_risk_reduction_when_alpha_is_negative():
    policy = ExecutionPolicy(40, 2)
    chosen = policy.choose(20, Decimal("-25"), Decimal("5"), reduce_only=True)
    ok, reason = policy.validate(chosen, Decimal("-25"), Decimal("10"), reduce_only=True)
    assert ok is True
    assert reason == "REDUCE_ONLY_RISK_REDUCTION_OK"


def test_reduce_only_execution_policy_still_respects_slippage_limit():
    policy = ExecutionPolicy(20, 2)
    chosen = policy.choose(20, Decimal("-25"), Decimal("20"), reduce_only=True)
    ok, reason = policy.validate(chosen, Decimal("-25"), Decimal("40"), reduce_only=True)
    assert ok is False
    assert reason == "ESTIMATED_SLIPPAGE_EXCEEDS_LIMIT"



def test_margin_required_short_selects_supported_leverage_with_normal_volatility():
    from types import SimpleNamespace
    from app.risk.leverage import LeverageEngine

    instrument = SimpleNamespace(leverage_levels=(Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")))
    engine = LeverageEngine()
    chosen = engine.choose(
        instrument,
        {"volatility": Decimal("20"), "spread_bps": Decimal("20"), "trend": Decimal("0.5")},
        Decimal("0.9"), Decimal("10"), Decimal("9999"), Decimal("5"), require_margin=True,
    )
    assert chosen >= Decimal("2")


def test_margin_required_short_is_blocked_only_by_hard_market_or_margin_guards():
    from types import SimpleNamespace
    from app.risk.leverage import LeverageEngine

    instrument = SimpleNamespace(leverage_levels=(Decimal("1"), Decimal("2"), Decimal("3")))
    engine = LeverageEngine()
    assert engine.choose(
        instrument, {"volatility": Decimal("31"), "spread_bps": Decimal("20"), "trend": Decimal("1")},
        Decimal("0.9"), Decimal("10"), Decimal("9999"), Decimal("5"), require_margin=True,
    ) == Decimal("0")
    assert engine.choose(
        instrument, {"volatility": Decimal("20"), "spread_bps": Decimal("20"), "trend": Decimal("1")},
        Decimal("0.9"), Decimal("10"), Decimal("199"), Decimal("5"), require_margin=True,
    ) == Decimal("0")

def test_risk_engine_accepts_adaptive_core_edge_when_economics_clear(config, instrument):
    from app.domain.models import Decision, MarketSnapshot, PortfolioState, Signal
    from app.domain.states import Direction

    signal = Signal(
        instrument.symbol, Direction.LONG,
        Decimal("150"), Decimal("130"), Decimal("0.85"),
        "TREND_UP", Decimal("0"), Decimal("0"),
        {"volatility": Decimal("5")},
    )
    decision = Decision(
        "adaptive-risk", instrument, signal,
        Decimal("15"), Decimal("1"),
        {
            "risk_profile": "core",
            "edge_tier": "ADAPTIVE",
            "edge_threshold_bps": Decimal("15"),
            "min_cost_eur": "0.5",
        },
        "baseline-v2-rebalance", "test", "", Decimal("0"), Decimal("15"),
        Direction.LONG, False,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("100"),
        positions={}, gross_eur=Decimal("0"), net_eur=Decimal("0"),
    )
    market = MarketSnapshot(
        instrument.symbol, Decimal("100"), Decimal("99.9"), Decimal("100.1"),
        Decimal("100000"), 1.0, tuple(Decimal("100") for _ in range(40)),
    )
    result = RiskEngine(config, SimpleNamespace(), SimpleNamespace(), SimpleNamespace()).evaluate(
        decision, portfolio, market, {}
    )
    assert result.allowed is True


def test_decision_flattens_held_short_when_edge_is_positive_but_below_economic_floor(config, instrument):
    from app.domain.models import PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    features = {"volatility": Decimal("2")}
    weak_long = Signal(
        instrument.symbol, Direction.LONG, Decimal("0"), Decimal("100"),
        Decimal("0.2"), "TREND_UP", Decimal("0"), Decimal("0"), features,
    )
    weak_short = Signal(
        instrument.symbol, Direction.SHORT, Decimal("200"), Decimal("190"),
        Decimal("0.9"), "TREND_DOWN", Decimal("0"), Decimal("0"), features,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("80"),
        positions={instrument.symbol: Decimal("-20")},
        gross_eur=Decimal("20"), net_eur=Decimal("-20"),
    )
    decision = DecisionEngine(config).choose(
        instrument, weak_long, weak_short, portfolio, "test-model", "test-config",
    )

    assert decision is not None
    assert decision.reduce_only is True
    assert decision.current_position_eur == Decimal("-20")
    assert decision.target_position_eur == Decimal("0")
    assert decision.execution_direction == Direction.LONG
    assert decision.rationale["rebalance_action"] == "FLATTEN_STALE_EDGE"



def test_cost_model_normalizes_float_leverage_and_cost_inputs():
    market = SimpleNamespace(
        spread_bps=Decimal("10"),
        volume_24h=Decimal("1000"),
        metadata={"volatility": Decimal("3")},
    )
    cost = CostModel().estimate(
        market,
        Decimal("10"),
        leverage=2.0,
        fee_bps=40.0,
        funding_bps=0.0,
        fx_bps=0.0,
        holding_hours=8.0,
        margin_open_fee_bps=4.0,
        margin_rollover_fee_bps=4.0,
    )
    assert isinstance(cost.financing_bps, Decimal)
    assert cost.financing_bps > 0
    assert isinstance(cost.total_bps, Decimal)


def test_leverage_engine_never_returns_float_for_float_configured_max(instrument):
    engine = LeverageEngine()
    chosen = engine.choose(
        instrument,
        {
            "volatility": Decimal("2"),
            "spread_bps": Decimal("10"),
            "trend": Decimal("1"),
        },
        Decimal("0.9"),
        Decimal("20"),
        Decimal("500"),
        2.0,
        require_margin=True,
    )
    assert isinstance(chosen, Decimal)
    assert chosen == Decimal("2")
