from decimal import Decimal

from app.domain.models import MarketSnapshot, Signal
from app.domain.states import Direction

D = Decimal


def _snapshot(symbol: str, price: str, timestamp: float) -> MarketSnapshot:
    value = D(price)
    return MarketSnapshot(
        symbol, value, value - D("0.01"), value + D("0.01"),
        D("100000"), timestamp, tuple(value for _ in range(20)),
    )


def _signal(symbol: str, direction: Direction) -> Signal:
    return Signal(
        symbol, direction, D("45"), D("10"), D("0.8"), "TREND_TEST",
        D("0"), D("0"),
        {
            "trend": D("1"), "return_5": D("1"), "return_15": D("1"),
            "return_60": D("1"), "return_240": D("1"), "volatility": D("2"),
            "liquidity": D("10000"), "spread_bps": D("1"),
        },
    )


def test_signal_observation_is_idempotent_within_a_15_minute_bucket(db, instrument):
    created_at = 1_700_000_000.0
    snapshot = _snapshot(instrument.symbol, "100", created_at)

    assert db.save_signal_observation(
        snapshot, _signal(instrument.symbol, Direction.LONG),
        direction_available=True, model_version="decision/policy",
    ) is True
    assert db.save_signal_observation(
        snapshot, _signal(instrument.symbol, Direction.LONG),
        direction_available=True, model_version="decision/policy",
    ) is False
    assert db.save_signal_observation(
        snapshot, _signal(instrument.symbol, Direction.SHORT),
        direction_available=True, model_version="decision/policy",
    ) is True
    assert db.one("SELECT COUNT(*) AS n FROM signal_observations")["n"] == 2


def test_signal_observation_settlement_is_directional_and_cost_adjusted(db, instrument):
    created_at = 1_700_000_000.0
    start = _snapshot(instrument.symbol, "100", created_at)
    end = _snapshot(instrument.symbol, "99.8", created_at + 900)
    db.save_market(start, {})
    db.save_market(end, {})
    db.save_signal_observation(
        start, _signal(instrument.symbol, Direction.SHORT),
        direction_available=True, model_version="decision/policy",
    )

    result = db.settle_signal_observations(now=created_at + 901)

    assert result == {"settled": 1, "unscorable": 0, "waiting_for_prices": 0}
    row = db.one(
        "SELECT success,net_return_bps,outcome_detail_json FROM signal_observations"
    )
    assert row["success"] == 1
    assert D(row["net_return_bps"]) == D("10.0")
    assert '"direction": "SHORT"' in row["outcome_detail_json"]
    assert '"expected_cost_bps": "10"' in row["outcome_detail_json"]
