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
    from dataclasses import replace
    from app.domain.models import PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    # Learned Core entry thresholds are no longer HA options; inject the model
    # values that this decision-engine reason test intends to exercise.
    config = replace(config, strategy_min_edge_bps=5.0, strategy_min_confidence=0.5)
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
        Decimal("250"), Decimal("130"), Decimal("0.85"),
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



def test_risk_engine_normalizes_float_leverage_from_legacy_decision(config, instrument):
    from app.domain.models import Decision, PortfolioState, Signal
    from app.domain.states import Direction

    signal = Signal(
        instrument.symbol,
        Direction.LONG,
        Decimal("150"),
        Decimal("10"),
        Decimal("0.9"),
        "TEST",
        Decimal("0"),
        Decimal("0"),
        {"volatility": Decimal("2")},
    )
    decision = Decision(
        "decision-float-leverage",
        instrument,
        signal,
        Decimal("10"),
        2.0,  # Legacy or external producers may violate the Decimal type contract.
        {"min_cost_eur": "0.5"},
        "test",
        "test",
        "test",
        current_position_eur=Decimal("0"),
        target_position_eur=Decimal("10"),
        execution_direction=Direction.LONG,
        reduce_only=False,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("100"),
        positions={},
        gross_eur=Decimal("0"),
        net_eur=Decimal("0"),
        margin_used_eur=Decimal("0"),
    )
    result = RiskEngine(
        config, MarginEngine(), LeverageEngine(), CostModel()
    ).evaluate(
        decision,
        portfolio,
        SimpleNamespace(),
        {"free_margin": "100", "margin_level_pct": "500"},
    )
    assert result.allowed is True
    assert isinstance(result.effective_leverage, Decimal)
    assert result.effective_leverage == Decimal("2")



def test_core_calibrated_confidence_is_reused_by_final_risk_gate(config, instrument):
    from app.domain.models import MarketSnapshot, PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    config = __import__("dataclasses").replace(
        config, strategy_min_confidence=0.58, strategy_min_edge_bps=25.0
    )
    long_signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("300"), Decimal("100"),
        Decimal("0.4"), "TREND_UP", Decimal("0"), Decimal("0"),
        {"volatility": Decimal("5")},
    )
    short_signal = Signal(
        instrument.symbol, Direction.SHORT, Decimal("-50"), Decimal("100"),
        Decimal("0.4"), "TREND_DOWN", Decimal("0"), Decimal("0"),
        {"volatility": Decimal("5")},
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("100"),
        positions={}, gross_eur=Decimal("0"), net_eur=Decimal("0"),
        margin_used_eur=Decimal("0"), unrealized_pnl_eur=Decimal("0"),
        realized_pnl_eur=Decimal("0"), daily_pnl_eur=Decimal("0"),
        drawdown_pct=Decimal("0"), open_orders=0, source_timestamp=1.0,
    )
    decision = DecisionEngine(config).choose(
        instrument, long_signal, short_signal, portfolio,
        "test-model", "test-config", model_parameters={"confidence_scale": 1.5},
    )
    assert decision is not None
    assert Decimal(decision.rationale["calibrated_confidence"]) == Decimal("0.6")

    market = MarketSnapshot(
        instrument.symbol, Decimal("100"), Decimal("99.9"), Decimal("100.1"),
        Decimal("100000"), 1.0, tuple(Decimal("100") for _ in range(40)),
    )
    result = RiskEngine(
        config, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
    ).evaluate(decision, portfolio, market, {})
    assert result.checks["confidence"] is True
    assert result.allowed is True



def test_cash_reserve_checks_projected_cash_for_long_and_short(config, instrument):
    from app.domain.models import Decision, MarketSnapshot, PortfolioState, Signal
    from app.domain.states import Direction
    from app.risk.margin import MarginEngine
    from app.risk.leverage import LeverageEngine

    def decision(target, direction):
        signal = Signal(
            instrument.symbol, direction, Decimal("300"), Decimal("100"),
            Decimal("0.9"), "TREND", Decimal("0"), Decimal("0"),
            {"volatility": Decimal("2")},
        )
        return Decision(
            "cash-reserve-" + direction.value, instrument, signal,
            abs(target), Decimal("1"), {"min_cost_eur": "0"},
            "baseline-v2-rebalance", "test", "test",
            current_position_eur=Decimal("0"), target_position_eur=target,
            execution_direction=direction, reduce_only=False,
        )

    market = MarketSnapshot(
        instrument.symbol, Decimal("100"), Decimal("99.9"), Decimal("100.1"),
        Decimal("10000"), 1.0, tuple(Decimal("100") for _ in range(40)),
    )
    engine = RiskEngine(config, MarginEngine(), LeverageEngine(), CostModel())
    # At 15 EUR notional, 30 EUR cash leaves 15 EUR, below the 20 EUR reserve.
    for target, direction in ((Decimal("15"), Direction.LONG), (Decimal("-30"), Direction.SHORT)):
        portfolio = PortfolioState(
            equity_eur=Decimal("100"), cash_eur=Decimal("30"), positions={},
            gross_eur=Decimal("0"), net_eur=Decimal("0"),
        )
        result = engine.evaluate(decision(target, direction), portfolio, market, None)
        assert result.checks["cash_reserve"] is False

    # The exact threshold is accepted: 35 - 15 = 20 EUR reserve remaining.
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("35"), positions={},
        gross_eur=Decimal("0"), net_eur=Decimal("0"),
    )
    result = engine.evaluate(
        decision(Decimal("15"), Direction.LONG), portfolio, market, None,
    )
    assert result.checks["cash_reserve"] is True


def test_core_decision_does_not_flatten_held_position_when_history_is_insufficient(config, instrument):
    from app.domain.models import PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    features = {"volatility": Decimal("999"), "history_sufficient": Decimal("0")}
    long_signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("0"), Decimal("200"),
        Decimal("0.1"), "UNKNOWN", Decimal("0"), Decimal("0"), features,
    )
    short_signal = Signal(
        instrument.symbol, Direction.SHORT, Decimal("0"), Decimal("200"),
        Decimal("0.1"), "UNKNOWN", Decimal("0"), Decimal("0"), features,
    )
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("80"),
        positions={instrument.symbol: Decimal("20")}, gross_eur=Decimal("20"),
        net_eur=Decimal("20"),
    )
    engine = DecisionEngine(config)
    assert engine.choose(
        instrument, long_signal, short_signal, portfolio, "model", "config",
    ) is None
    assert engine.rejection_reason(
        instrument, long_signal, short_signal, portfolio,
    ) == "INSUFFICIENT_HISTORY_KEEP_POSITION"



def test_cost_model_impact_requires_eur_normalized_turnover():
    model = CostModel()
    # Huge/small raw base-volume values must not be mistaken for EUR turnover.
    low_raw_volume = SimpleNamespace(
        spread_bps=Decimal("10"), volume_24h=Decimal("0.01"), metadata={"volatility": Decimal("3")}
    )
    high_raw_volume = SimpleNamespace(
        spread_bps=Decimal("10"), volume_24h=Decimal("1000000000"), metadata={"volatility": Decimal("3")}
    )
    assert model.estimate(low_raw_volume, Decimal("10"), Decimal("1")).impact_bps == Decimal("80")
    assert model.estimate(high_raw_volume, Decimal("10"), Decimal("1")).impact_bps == Decimal("80")

    normalized = SimpleNamespace(
        spread_bps=Decimal("10"),
        volume_24h=Decimal("0.01"),
        liquidity_eur_24h=Decimal("100000"),
        quote_asset="USD",
        metadata={"volatility": Decimal("3")},
    )
    assert model.estimate(normalized, Decimal("100"), Decimal("1")).impact_bps == Decimal("10")


def test_cost_model_impact_uses_quote_turnover_only_when_quote_is_eur():
    model = CostModel()
    eur_quote = SimpleNamespace(
        spread_bps=Decimal("10"),
        volume_24h=Decimal("100"),
        volume_24h_quote=Decimal("50000"),
        quote_asset="EUR",
        metadata={"volatility": Decimal("3")},
    )
    usd_quote_without_fx = SimpleNamespace(
        spread_bps=Decimal("10"),
        volume_24h=Decimal("100"),
        volume_24h_quote=Decimal("50000"),
        quote_asset="USD",
        metadata={"volatility": Decimal("3")},
    )
    assert model.estimate(eur_quote, Decimal("100"), Decimal("1")).impact_bps == Decimal("20")
    assert model.estimate(usd_quote_without_fx, Decimal("100"), Decimal("1")).impact_bps == Decimal("80")


def test_drawdown_gate_blocks_new_exposure_but_allows_valid_reduce_only_exit(config, instrument):
    from dataclasses import replace
    from decimal import Decimal
    from types import SimpleNamespace

    from app.domain.models import Decision, MarketSnapshot, PortfolioState, Signal
    from app.domain.states import Direction

    config = replace(config, risk_max_drawdown_pct=18.0)
    signal = Signal(
        instrument.symbol,
        Direction.LONG,
        Decimal("150"),
        Decimal("130"),
        Decimal("0.85"),
        "TREND_UP",
        Decimal("0"),
        Decimal("0"),
        {"volatility": Decimal("5")},
    )
    market = MarketSnapshot(
        instrument.symbol,
        Decimal("100"),
        Decimal("99.9"),
        Decimal("100.1"),
        Decimal("100000"),
        1.0,
        tuple(Decimal("100") for _ in range(40)),
    )
    engine = RiskEngine(config, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    entry = Decision(
        "drawdown-new-entry",
        instrument,
        signal,
        Decimal("15"),
        Decimal("1"),
        {"risk_profile": "core", "edge_tier": "ADAPTIVE", "min_cost_eur": "0"},
        "baseline-v2",
        "test-config",
        "test",
        Decimal("0"),
        Decimal("15"),
        Direction.LONG,
        False,
    )
    flat_portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("100"),
        positions={},
        gross_eur=Decimal("0"),
        net_eur=Decimal("0"),
        daily_pnl_eur=Decimal("0"),
        drawdown_pct=Decimal("50"),
    )
    entry_result = engine.evaluate(entry, flat_portfolio, market, {})
    assert entry_result.allowed is False
    assert entry_result.reason == "drawdown"
    assert entry_result.checks["drawdown"] is False

    reduction = Decision(
        "drawdown-reduce-only",
        instrument,
        signal,
        Decimal("0"),
        Decimal("1"),
        {"risk_profile": "core", "min_cost_eur": "0"},
        "baseline-v2",
        "test-config",
        "risk reduction",
        Decimal("15"),
        Decimal("0"),
        Direction.SHORT,
        True,
    )
    held_portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("85"),
        positions={instrument.symbol: Decimal("15")},
        gross_eur=Decimal("15"),
        net_eur=Decimal("15"),
        daily_pnl_eur=Decimal("0"),
        drawdown_pct=Decimal("50"),
    )
    reduction_result = engine.evaluate(reduction, held_portfolio, market, {})
    assert reduction_result.allowed is True
    assert reduction_result.checks["drawdown"] is True
    assert reduction_result.checks["reduce_only_semantics"] is True
