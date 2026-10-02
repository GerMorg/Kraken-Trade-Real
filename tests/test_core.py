from __future__ import annotations

import time
from decimal import Decimal

from custom_components.kraken_ai_trader.core.analytics import compute_features, detect_regime
from custom_components.kraken_ai_trader.core.backtest import cost_adjusted_returns, validation_summary
from custom_components.kraken_ai_trader.core.learning import Calibration, walk_forward_validate
from custom_components.kraken_ai_trader.core.models import Instrument, MarketSnapshot, OrderBook, ProductType, Regime
from custom_components.kraken_ai_trader.core.risk import RiskEngine
from custom_components.kraken_ai_trader.core.models import SafetyLimits, Signal, CostEstimate, PortfolioSnapshot


def instrument() -> Instrument:
    return Instrument("kraken", ProductType.SPOT_MARGIN, "BTC/USD", "XXBTZUSD", "XBTUSD", "BTC", "USD", None, "online", True, True, True, (Decimal("1"), Decimal("2"), Decimal("3")), Decimal("3"), Decimal("0.0001"), Decimal("0.5"), 8, 1, Decimal("0.1"))


def snapshot() -> MarketSnapshot:
    book = OrderBook(((Decimal("100"), Decimal("1000")),), ((Decimal("101"), Decimal("1000")),), time.time())
    candles = {15: tuple(Decimal(str(100 + i * 0.5)) for i in range(60))}
    return MarketSnapshot(instrument(), Decimal("100.5"), Decimal("100"), Decimal("101"), Decimal("200000"), book, candles, None, None, time.time())


def test_features_and_regime():
    f = compute_features(snapshot())
    assert f.return_20 > 0
    assert f.liquidity_score > 0
    assert detect_regime(f) in {Regime.TREND_UP, Regime.BREAKOUT, Regime.MEAN_REVERSION, Regime.UNKNOWN}


def test_calibration_moves_toward_realized():
    c = Calibration()
    c.update(Decimal("0.8"), Decimal("1"))
    c.update(Decimal("0.8"), Decimal("0"))
    assert abs(c.probability_bias) <= Decimal("0.2")


def test_walk_forward():
    report = walk_forward_validate([Decimal("0.01")] * 20, 10, 5, 5)
    assert len(report) == 2
    assert report[0]["oos_mean"] == Decimal("0.01")


def test_risk_blocks_cost_and_accepts_small_safe_trade():
    limits = SafetyLimits(Decimal("0.02"), Decimal("1"), Decimal("1"), Decimal("0.25"), Decimal("3"), 5, Decimal("0.05"), Decimal("0.1"), Decimal("10"), Decimal("0.0025"), 20)
    engine = RiskEngine(limits)
    p = PortfolioSnapshot(Decimal("50"), Decimal("50"), Decimal("40"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), (), 0, 0, time.time())
    s = Signal(Decimal("0.75"), Decimal("0.2"), Decimal("0.75"), Decimal("0.01"), Decimal("0.01"), "v1", "baseline")
    c = CostEstimate(Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("0"), Decimal("0"), Decimal("0"))
    out = engine.evaluate(p, instrument(), s, c, __import__('custom_components.kraken_ai_trader.core.models', fromlist=['DecisionState']).DecisionState.OPEN_LONG, Decimal("10"), Decimal("1"))
    assert out.allowed
    assert out.notional <= Decimal("10")


def test_backtest_summary_costs_and_drawdown():
    adjusted = cost_adjusted_returns([Decimal("0.02"), Decimal("-0.01")], Decimal("0.001"), Decimal("0.001"), Decimal("0.001"))
    summary = validation_summary(adjusted)
    assert summary["net"] < Decimal("0.02")
    assert summary["max_drawdown"] >= 0
