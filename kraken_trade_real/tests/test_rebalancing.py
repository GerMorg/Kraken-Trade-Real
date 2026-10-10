from decimal import Decimal

from app.domain.models import MarketSnapshot, PortfolioState, Signal
from app.domain.states import Direction
from app.risk.engine import RiskEngine
from app.trading.decision import DecisionEngine


def _signal(symbol, direction, expected_return, expected_cost, confidence):
    return Signal(
        symbol=symbol,
        direction=direction,
        expected_return_bps=Decimal(str(expected_return)),
        expected_cost_bps=Decimal(str(expected_cost)),
        confidence=Decimal(str(confidence)),
        regime="TEST",
        news_effect_bps=Decimal("0"),
        gemini_effect_bps=Decimal("0"),
        features={"volatility": Decimal("1")},
    )


def test_negative_edge_on_held_position_creates_reduce_only_flatten(config, instrument):
    portfolio = PortfolioState(
        equity_eur=Decimal("50"),
        cash_eur=Decimal("42"),
        positions={instrument.symbol: Decimal("8")},
        gross_eur=Decimal("8"),
        net_eur=Decimal("8"),
        spot_open_positions_read_ok=True,
    )
    long_signal = _signal(instrument.symbol, Direction.LONG, 10, 40, "0.9")
    short_signal = _signal(instrument.symbol, Direction.SHORT, 0, 50, "0.1")

    decision = DecisionEngine(config).choose(
        instrument,
        long_signal,
        short_signal,
        portfolio,
        "model-test",
        "config-test",
        {},
        Decimal("0.5"),
    )

    assert decision is not None
    assert decision.reduce_only is True
    assert decision.target_position_eur == Decimal("0")
    assert decision.current_position_eur == Decimal("8")
    assert decision.execution_direction == Direction.SHORT
    assert decision.target_notional_eur == Decimal("8")
    assert decision.rationale["rebalance_action"] == "FLATTEN_NEGATIVE_EDGE"


def test_reduce_only_exit_bypasses_entry_edge_and_confidence_gates(config, instrument):
    portfolio = PortfolioState(
        equity_eur=Decimal("50"),
        cash_eur=Decimal("42"),
        positions={instrument.symbol: Decimal("8")},
        gross_eur=Decimal("8"),
        net_eur=Decimal("8"),
    )
    long_signal = _signal(instrument.symbol, Direction.LONG, 10, 40, "0.1")
    short_signal = _signal(instrument.symbol, Direction.SHORT, 0, 50, "0.1")
    decision = DecisionEngine(config).choose(
        instrument,
        long_signal,
        short_signal,
        portfolio,
        "model-test",
        "config-test",
        {},
        Decimal("0.5"),
    )
    assert decision is not None

    market = MarketSnapshot(
        instrument.symbol,
        Decimal("60005"),
        Decimal("60000"),
        Decimal("60010"),
        Decimal("1000"),
        0,
        tuple(Decimal("60000") for _ in range(40)),
    )
    result = RiskEngine(config, object(), object(), object()).evaluate(
        decision,
        portfolio,
        market,
    )

    assert result.allowed is True
    assert result.reason == "RISK_OK"
    assert result.checks["edge_positive"] is True
    assert result.checks["confidence"] is True


def test_adaptive_core_edge_tier_allows_high_confidence_economic_setup(config, instrument):
    from app.domain.models import PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    portfolio = PortfolioState(
        equity_eur=Decimal("100"),
        cash_eur=Decimal("90"),
        positions={},
        gross_eur=Decimal("0"),
        net_eur=Decimal("0"),
    )
    long_signal = Signal(
        instrument.symbol, Direction.LONG, Decimal("150"), Decimal("130"), Decimal("0.85"),
        "TREND_UP", Decimal("0"), Decimal("0"), {"volatility": Decimal("5")},
    )
    short_signal = Signal(
        instrument.symbol, Direction.SHORT, Decimal("0"), Decimal("130"), Decimal("0.1"),
        "TREND_UP", Decimal("0"), Decimal("0"), {"volatility": Decimal("5")},
    )
    from dataclasses import replace

    test_config = replace(
        config,
        strategy_min_edge_bps=25.0,
        strategy_adaptive_edge_floor_bps=15.0,
        strategy_adaptive_min_confidence=0.75,
        strategy_adaptive_cost_ratio=1.10,
    )
    decision = DecisionEngine(test_config).choose(
        instrument, long_signal, short_signal, portfolio,
        "model-test", "config-test", {}, Decimal("1"),
    )
    assert decision is not None
    assert decision.rationale["edge_tier"] == "ADAPTIVE"
    assert decision.rationale["edge_threshold_bps"] == "15.0"


def test_core_new_short_requires_exchange_direction_availability(config, instrument):
    from dataclasses import replace
    from app.domain.models import PortfolioState, Signal
    from app.domain.states import Direction
    from app.trading.decision import DecisionEngine

    blocked = replace(instrument, short_available=False)
    portfolio = PortfolioState(equity_eur=Decimal("100"), cash_eur=Decimal("100"))
    long_signal = Signal(
        blocked.symbol, Direction.LONG, Decimal("0"), Decimal("130"), Decimal("0.1"),
        "RANGE", Decimal("0"), Decimal("0"), {"volatility": Decimal("5")},
    )
    short_signal = Signal(
        blocked.symbol, Direction.SHORT, Decimal("150"), Decimal("130"), Decimal("0.85"),
        "TREND_DOWN", Decimal("0"), Decimal("0"), {"volatility": Decimal("5")},
    )
    assert DecisionEngine(config).choose(
        blocked, long_signal, short_signal, portfolio, "model-test", "hash", {}, Decimal("1")
    ) is None
    assert DecisionEngine(config).rejection_reason(
        blocked, long_signal, short_signal, portfolio, {}, Decimal("0.5")
    ) == "INSTRUMENT_DIRECTION"


def test_learned_policy_weights_and_thresholds_apply_without_legacy_confidence_scaling(
    config, instrument
):
    portfolio = PortfolioState(
        equity_eur=Decimal("100"), cash_eur=Decimal("100"),
        positions={}, gross_eur=Decimal("0"), net_eur=Decimal("0"),
    )
    long_signal = _signal(instrument.symbol, Direction.LONG, 150, 130, "0.4")
    short_signal = _signal(instrument.symbol, Direction.SHORT, 0, 130, "0.1")
    strategy_parameters = {
        "policy_version": "candidate-policy-test",
        "strategy_min_edge_bps": 25.0,
        "strategy_min_confidence": 0.35,
        "strategy_adaptive_edge_floor_bps": 15.0,
        "strategy_adaptive_min_confidence": 0.4,
        "strategy_adaptive_cost_ratio": 1.1,
    }

    decision = DecisionEngine(config).choose(
        instrument,
        long_signal,
        short_signal,
        portfolio,
        "model-test",
        "config-test",
        {"confidence_scale": 1.5},
        Decimal("1"),
        strategy_parameters=strategy_parameters,
    )

    assert decision is not None
    assert decision.rationale["edge_tier"] == "ADAPTIVE"
    # The old Brier-selected confidence_scale must not inflate trade sizing.
    assert decision.target_notional_eur == Decimal("6.00")
    assert decision.rationale["strategy_policy_version"] == "candidate-policy-test"
    assert decision.rationale["legacy_confidence_scale_ignored"] is True


def test_offsetting_wallet_inventory_does_not_hide_margin_position_for_decisions(
    config, instrument
):
    portfolio = PortfolioState(
        equity_eur=Decimal("50"),
        cash_eur=Decimal("42"),
        positions={instrument.symbol: Decimal("0")},
        gross_eur=Decimal("16"),
        net_eur=Decimal("0"),
        spot_open_positions_read_ok=True,
        spot_wallet_positions_eur={instrument.symbol: Decimal("8")},
        spot_margin_position_eur={instrument.symbol: Decimal("-8")},
        spot_margin_position_symbols=(instrument.symbol,),
    )
    long_signal = _signal(instrument.symbol, Direction.LONG, 10, 40, "0.9")
    short_signal = _signal(instrument.symbol, Direction.SHORT, 0, 50, "0.1")

    decision = DecisionEngine(config).choose(
        instrument, long_signal, short_signal, portfolio,
        "model-test", "config-test", {}, Decimal("0.5"),
    )

    assert decision is not None
    assert decision.current_position_eur == Decimal("-8")
    assert decision.target_position_eur == Decimal("0")
    assert decision.reduce_only is True
    assert decision.execution_direction == Direction.LONG


def test_position_limit_counts_wallet_and_margin_legs_together(config, instrument):
    portfolio = PortfolioState(
        equity_eur=Decimal("50"),
        cash_eur=Decimal("45"),
        positions={instrument.symbol: Decimal("9")},
        gross_eur=Decimal("9"),
        net_eur=Decimal("9"),
        spot_open_positions_read_ok=True,
        spot_wallet_positions_eur={instrument.symbol: Decimal("5")},
        spot_margin_position_eur={instrument.symbol: Decimal("4")},
        spot_margin_position_symbols=(instrument.symbol,),
    )
    signal = _signal(instrument.symbol, Direction.LONG, 150, 10, "0.9")
    from app.domain.models import Decision

    decision = Decision(
        "decision-symbol-cap", instrument, signal, Decimal("1"), Decimal("1"), {},
        "test", "test-model", "", Decimal("4"), Decimal("5"), Direction.LONG, False,
    )
    market = MarketSnapshot(
        instrument.symbol, Decimal("60005"), Decimal("60000"), Decimal("60010"),
        Decimal("1000"), 1.0, tuple(Decimal("60000") for _ in range(40)),
    )
    result = RiskEngine(config, object(), object(), object()).evaluate(
        decision, portfolio, market
    )

    assert result.allowed is False
    assert result.checks["position_limit"] is False
