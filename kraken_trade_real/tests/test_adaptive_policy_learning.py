from decimal import Decimal

from app.domain.models import Instrument, Signal
from app.domain.states import Direction, ProductType
from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine
from app.trading.decision import DecisionEngine

D = Decimal


def _signal(symbol, direction, confidence, expected_return, expected_cost):
    return Signal(
        symbol, direction, D(str(expected_return)), D(str(expected_cost)),
        D(str(confidence)), "TEST_REGIME", D("0"), D("0"),
        {"volatility": D("10")},
    )


def _instrument():
    return Instrument(
        venue="spot", product_type=ProductType.SPOT_MARGIN,
        symbol="XBT/EUR", instrument_id="XXBTZEUR", altname="XBTEUR",
        base="XXBT", quote="ZEUR", status="online", margin_available=True,
        long_available=True, short_available=True,
        leverage_levels=(D("1"), D("2")), min_order_qty=D("0.0001"),
        min_cost=D("1"), lot_decimals=4, price_decimals=2,
        tick_size=D("0.01"), margin_class="spot-margin",
    )


def test_probability_calibration_scale_does_not_change_live_trade_confidence(config):
    engine = DecisionEngine(config)
    assert engine._confidence_scale({"confidence_scale": 1.5}) == D("1")
    assert engine._confidence_scale({"probability_scale": 0.7}) == D("1")


def test_live_decision_policy_accepts_validated_policy_overrides(config):
    engine = DecisionEngine(config)
    instrument = _instrument()
    signal = _signal(instrument.symbol, Direction.LONG, 0.60, 60, 20)
    allowed, threshold, tier = engine._edge_policy(
        signal, D("0.60"),
        {
            "strategy_min_edge_bps": 20,
            "strategy_min_confidence": 0.55,
            "strategy_adaptive_cost_ratio": 1.2,
        },
    )
    assert allowed is True
    assert threshold == D("20")
    assert tier == "STANDARD"


def test_model_registry_governs_strategy_policy_separately_from_probability(db):
    registry = ModelRegistry(db)
    version = registry.active("strategy_policy")
    assert version == "strategy-policy-baseline-v1"
    registry.register_candidate(
        "policy-good",
        "strategy_policy",
        version,
        {
            "strategy_min_edge_bps": 20,
            "strategy_min_confidence": 0.60,
            "strategy_adaptive_cost_ratio": 1.1,
        },
        {
            "samples": 60,
            "improvement": 3.0,
            "validation_utility_bps": 9.0,
            "validation_mean_account_return_bps": 0.2,
            "validation_max_drawdown_pct": -0.08,
            "validation_buckets": 40,
        },
    )
    assert registry.promote("policy-good", min_improvement=1.0, min_samples=30) is True
    assert registry.active("strategy_policy") == "policy-good"
    assert registry.active("probability_calibration") == "probability-calibration-baseline-v1"


def test_registry_rejects_strategy_policy_with_excessive_validation_drawdown(db):
    registry = ModelRegistry(db)
    parent = registry.active("strategy_policy")
    registry.register_candidate(
        "policy-high-drawdown",
        "strategy_policy",
        parent,
        {"strategy_min_edge_bps": 20},
        {
            "samples": 80,
            "improvement": 10.0,
            "validation_utility_bps": 10.0,
            "validation_mean_account_return_bps": 0.5,
            "validation_max_drawdown_pct": -0.45,
            "validation_buckets": 50,
        },
    )
    assert registry.promote(
        "policy-high-drawdown", min_improvement=1.0, min_samples=30
    ) is False
    assert registry.active("strategy_policy") == parent


def test_expanding_walk_forward_passes_training_data_to_scorer(db):
    research = ResearchEngine(db)
    data = [{"created_at": float(index), "feature": index} for index in range(120)]
    fold_sizes = []

    def scorer(train, test):
        fold_sizes.append((len(train), len(test), train[-1]["created_at"], test[0]["created_at"]))
        assert train and test
        assert train[-1]["created_at"] < test[0]["created_at"]
        return [0.001] * len(test)

    result = research.walk_forward(data, scorer, windows=3, embargo_samples=2)
    assert result["status"] == "OK"
    assert len(result["windows"]) == 3
    assert fold_sizes[0][0] < fold_sizes[1][0] < fold_sizes[2][0]
    assert all(train_end < test_start for _, _, train_end, test_start in fold_sizes)
