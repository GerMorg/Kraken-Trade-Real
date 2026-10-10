from decimal import Decimal
import time

import pytest

from app.domain.models import Decision, MarketSnapshot, Signal
from app.domain.states import Direction

D = Decimal


def _snapshot(symbol, price, timestamp):
    price = D(str(price))
    return MarketSnapshot(
        symbol, price, price - D("0.01"), price + D("0.01"), D("100000"),
        timestamp, tuple(price for _ in range(20)),
    )


@pytest.mark.parametrize(
    ("direction", "start_price", "end_price", "expected_success"),
    [
        (Direction.LONG, "100", "102", 1),
        (Direction.SHORT, "100", "98", 1),
        (Direction.SHORT, "100", "102", 0),
        (Direction.LONG, "100", "100.05", 0),
    ],
)
def test_prediction_outcome_is_directional_and_net_of_costs(
    db, instrument, direction, start_price, end_price, expected_success
):
    decision = Decision(
        "decision-prediction", instrument,
        Signal(
            instrument.symbol, direction, D("100"), D("10"), D("0.9"),
            "TREND_TEST", D("0"), D("0"), {"volatility": D("10")},
        ),
        D("10"), D("2"), {}, "test", "test-model", "",
    )
    prediction_id = f"prediction-{direction.value.lower()}-{start_price}-{end_price}"
    stored_p = db.save_prediction(prediction_id, decision, 0.99)
    row = db.one("SELECT created_at FROM predictions WHERE prediction_id=?", (prediction_id,))
    created_at = float(row["created_at"]) - 2000
    db.execute(
        "UPDATE predictions SET created_at=? WHERE prediction_id=?",
        (created_at, prediction_id),
    )
    db.save_market(_snapshot(instrument.symbol, start_price, created_at - 1), {})
    db.save_market(_snapshot(instrument.symbol, end_price, created_at + 901), {})

    assert stored_p == pytest.approx(0.745)
    assert db.settle_predictions(now=created_at + 902) == 1
    outcome = db.one(
        "SELECT success,detail_json FROM prediction_outcomes WHERE prediction_id=?",
        (prediction_id,),
    )
    assert outcome["success"] == expected_success
    assert '"predicted_direction": "' + direction.value + '"' in outcome["detail_json"]
    assert '"expected_cost_bps": "10"' in outcome["detail_json"]


def test_legacy_prediction_without_direction_is_never_mislabelled_long(db, instrument):
    decision = Decision(
        "decision-legacy", instrument,
        Signal(
            instrument.symbol, Direction.SHORT, D("100"), D("0"), D("0.8"),
            "TREND_DOWN", D("0"), D("0"), {},
        ),
        D("10"), D("2"), {}, "test", "test-model", "",
    )
    prediction_id = "prediction-legacy-direction"
    db.save_prediction(prediction_id, decision, 0.8)
    row = db.one("SELECT created_at FROM predictions WHERE prediction_id=?", (prediction_id,))
    created_at = float(row["created_at"]) - 2000
    db.execute(
        "UPDATE predictions SET created_at=?,predicted_direction='UNKNOWN' WHERE prediction_id=?",
        (created_at, prediction_id),
    )
    db.save_market(_snapshot(instrument.symbol, "100", created_at - 1), {})
    db.save_market(_snapshot(instrument.symbol, "99", created_at + 901), {})

    assert db.settle_predictions(now=created_at + 902) == 0
    row = db.one("SELECT outcome_status FROM predictions WHERE prediction_id=?", (prediction_id,))
    assert row["outcome_status"] == "UNSCORABLE"
    assert db.one(
        "SELECT COUNT(*) AS n FROM prediction_outcomes WHERE prediction_id=?",
        (prediction_id,),
    )["n"] == 0
