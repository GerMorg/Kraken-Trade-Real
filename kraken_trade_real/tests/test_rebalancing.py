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
