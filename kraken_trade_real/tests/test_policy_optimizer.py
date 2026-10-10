import json

from app.learning.policy_optimizer import DEFAULT_WEIGHTS, StrategyPolicyOptimizer


def _row(bucket, direction, *, net_return, trend, confidence=0.9, available=True):
    return {
        "observation_id": f"{bucket}-{direction}",
        "created_at": float(bucket * 900),
        "horizon_seconds": 900,
        "horizon_bucket": bucket,
        "symbol": "XBT/EUR",
        "direction": direction,
        "direction_available": int(available),
        "expected_return_bps": "20",
        "expected_cost_bps": "5",
        "expected_net_edge_bps": "15",
        "confidence": confidence,
        "regime": "TREND",
        "news_effect_bps": "0",
        "gemini_effect_bps": "0",
        "features_json": json.dumps({
            "trend": trend, "return_5": 0, "return_15": 0, "return_60": 0,
            "return_240": 0, "volatility": 1, "liquidity": 10000,
            "spread_bps": 1,
        }),
        "model_version": "decision/policy",
        "outcome_status": "SETTLED",
        "net_return_bps": str(net_return),
    }


def _params():
    return {
        **DEFAULT_WEIGHTS,
        "signal_cost_volatility_multiplier": 1.5,
        "signal_quality_spread_scale_bps": 200.0,
        "signal_quality_liquidity_scale": 1000.0,
        "signal_confidence_return_scale_bps": 45.0,
        "signal_confidence_volatility_scale": 120.0,
        "strategy_min_edge_bps": 5.0,
        "strategy_min_confidence": 0.35,
        "strategy_adaptive_edge_floor_bps": 3.0,
        "strategy_adaptive_min_confidence": 0.4,
        "strategy_adaptive_cost_ratio": 1.0,
    }


def test_optimizer_scores_one_direction_per_symbol_and_time_bucket():
    optimizer = StrategyPolicyOptimizer()
    rows = [
        _row(100, "LONG", net_return=12, trend=1.0),
        # An unselected short can have a higher realized return; one 15m group
        # contributes only the outcome of the direction the policy would select.
        _row(100, "SHORT", net_return=100, trend=1.0),
    ]

    score = optimizer.score(optimizer.grouped(rows), _params())

    assert score["samples"] == 1
    assert score["mean_net_bps"] == 12


def test_optimizer_never_fits_from_too_few_independent_time_buckets():
    optimizer = StrategyPolicyOptimizer()
    params = _params()
    rows = [
        _row(bucket, "LONG", net_return=15, trend=1.0)
        for bucket in range(30)
    ]

    result = optimizer.fit(rows, params, params)

    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["groups"] == 30
    assert result["promoted"] is False


def test_optimizer_excludes_exchange_unavailable_directions():
    optimizer = StrategyPolicyOptimizer()
    rows = [
        _row(102, "LONG", net_return=-50, trend=1.0, available=False),
        _row(102, "SHORT", net_return=10, trend=1.0, available=True),
    ]

    score = optimizer.score(optimizer.grouped(rows), _params())

    assert score["samples"] == 0


def test_optimizer_can_search_bounded_cost_and_confidence_parameters():
    optimizer = StrategyPolicyOptimizer()
    defaults = {
        **DEFAULT_WEIGHTS,
        "signal_cost_volatility_multiplier": 1.5,
        "signal_quality_spread_scale_bps": 200.0,
        "signal_quality_liquidity_scale": 1000.0,
        "signal_confidence_return_scale_bps": 45.0,
        "signal_confidence_volatility_scale": 120.0,
        "strategy_min_edge_bps": 25.0,
        "strategy_min_confidence": 0.58,
        "strategy_adaptive_edge_floor_bps": 15.0,
        "strategy_adaptive_min_confidence": 0.75,
        "strategy_adaptive_cost_ratio": 1.1,
    }
    assert optimizer._variants(
        "signal_quality_spread_scale_bps", 200.0, defaults
    ) == [150.0, 200.0, 225.0]
    profile = optimizer.normalize_profile(
        {"signal_quality_spread_scale_bps": 1.0}, defaults
    )
    assert profile["signal_quality_spread_scale_bps"] == 75.0
