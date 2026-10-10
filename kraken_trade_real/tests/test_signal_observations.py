from decimal import Decimal

from app.domain.models import MarketSnapshot, Signal
from app.domain.states import Direction

D = Decimal


def _signal(instrument, direction=Direction.LONG, confidence="0.7"):
    return Signal(
        instrument.symbol, direction, D("80"), D("10"), D(confidence),
        "TREND_UP", D("0"), D("0"), {"volatility": D("12")},
    )


def _snapshot(symbol, price, timestamp):
    price = D(str(price))
    return MarketSnapshot(
        symbol=symbol, price=price, bid=price-D("0.01"), ask=price+D("0.01"),
        volume_24h=D("100000"), timestamp=timestamp,
    )


def test_signal_observation_keeps_one_sample_per_symbol_direction_bucket(
    db, instrument
):
    first = db.save_signal_observation(
        instrument, _signal(instrument), D("100"),
        policy_version="strategy-policy-baseline-v1", captured_at=1000.0,
    )
    second = db.save_signal_observation(
        instrument, _signal(instrument, confidence="0.9"), D("101"),
        policy_version="strategy-policy-baseline-v1", captured_at=1010.0,
    )
    assert first is True and second is True
    rows = db.query(
        "SELECT * FROM signal_observations WHERE symbol=? AND direction='LONG'",
        (instrument.symbol,),
    )
    assert len(rows) == 1
    assert D(rows[0]["price"]) == D("100")
    assert rows[0]["policy_version"] == "strategy-policy-baseline-v1"


def test_signal_observation_settlement_uses_future_price_and_expected_costs(
    db, instrument
):
    db.save_signal_observation(
        instrument, _signal(instrument), D("100"), captured_at=1000.0,
    )
    db.save_market(_snapshot(instrument.symbol, "100", 999.0), {})
    db.save_market(_snapshot(instrument.symbol, "101", 1901.0), {})

    result = db.settle_signal_observations(now=2000.0)
    assert result == {"settled": 1, "unscorable": 0}
    row = db.one(
        "SELECT outcome_status,realized_return_bps,net_return_bps,success "
        "FROM signal_observations WHERE symbol=?",
        (instrument.symbol,),
    )
    assert row["outcome_status"] == "SETTLED"
    assert D(row["realized_return_bps"]) == D("100")
    assert D(row["net_return_bps"]) == D("90")
    assert row["success"] == 1


def test_legacy_predictions_are_not_calibration_eligible_after_schema_upgrade(db):
    columns = {
        row["name"] for row in db.query("PRAGMA table_info(predictions)")
    }
    assert "calibration_eligible" in columns
    assert "signal_observations" in {
        row["name"] for row in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
