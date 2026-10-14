from app.learning import CalibrationEngine,ModelRegistry

def test_calibration_is_data_driven():
    m=CalibrationEngine().evaluate([(0.9,True),(0.8,True),(0.2,False),(0.1,False)])
    assert m["sample_count"]==4 and 0<=m["brier"]<=1 and 0<=m["ece"]<=1

def test_model_registry_requires_minimum_samples(db):
    r=ModelRegistry(db)
    r.register_candidate("candidate-v2","decision","baseline-v1",{"x":1},{"samples":10,"improvement":1})
    assert r.promote("candidate-v2",min_improvement=.05,min_samples=100) is False
    assert r.active()=="baseline-v1"


def test_model_registry_rejects_poor_absolute_quality(db):
    r=ModelRegistry(db)
    r.register_candidate(
        "candidate-poor-quality", "decision", "baseline-v1", {"x":1},
        {"samples":1000,"improvement":0.3,"brier":0.31,"ece":0.20},
    )
    assert r.promote("candidate-poor-quality",min_improvement=.05,min_samples=1000) is False
    assert r.active()=="baseline-v1"


def test_learning_promotion_respects_configuration_flag(db):
    from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine

    engine = LearningEngine(
        db, CalibrationEngine(), ModelRegistry(db), ResearchEngine(db),
        auto_promotion_enabled=False,
    )
    assert engine.auto_promotion_enabled is False



def test_registry_recovers_when_no_model_is_active(db):
    registry = ModelRegistry(db)
    db.execute("UPDATE model_versions SET status='RETIRED' WHERE family='decision'")
    assert registry.active() == "baseline-v1"
    assert db.one(
        "SELECT COUNT(*) AS n FROM model_versions WHERE family='decision' AND status='ACTIVE'"
    )["n"] == 1


def test_registry_recovers_multiple_active_models_deterministically(db):
    registry = ModelRegistry(db)
    registry.register_candidate(
        "candidate-active-conflict", "decision", "baseline-v1",
        {"confidence_scale": 1.1}, {"samples": 100},
    )
    db.execute(
        "UPDATE model_versions SET status='ACTIVE' WHERE version='candidate-active-conflict'"
    )
    registry.active()
    assert db.one(
        "SELECT COUNT(*) AS n FROM model_versions WHERE family='decision' AND status='ACTIVE'"
    )["n"] == 1


def test_model_registry_rejects_ece_regression(db):
    registry = ModelRegistry(db)
    registry.register_candidate(
        "candidate-ece-regression", "decision", "baseline-v1",
        {"confidence_scale": 1.1},
        {"samples": 100, "improvement": 0.02, "brier": 0.20, "ece": 0.16},
    )
    assert registry.promote("candidate-ece-regression", min_improvement=0.01, min_samples=90) is False
    assert registry.active() == "baseline-v1"



def test_learning_disabled_flag_is_honored(db):
    from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine

    engine = LearningEngine(
        db, CalibrationEngine(), ModelRegistry(db), ResearchEngine(db),
        enabled=False,
    )
    assert engine.process_feedback(now=1_800_000_000)["status"] == "LEARNING_DISABLED"


def test_learning_validation_interval_is_persisted(db):
    from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine

    db.execute(
        "INSERT INTO metadata(key,value) VALUES('learning_last_validation_at','1000000')"
    )
    engine = LearningEngine(
        db, CalibrationEngine(), ModelRegistry(db), ResearchEngine(db),
        validation_interval_hours=24,
    )
    result = engine.process_feedback(now=1_000_100)
    assert result["status"] == "VALIDATION_INTERVAL_NOT_ELAPSED"


def test_learning_promotes_validated_regime_specific_scales(db):
    from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine

    registry = ModelRegistry(db)
    now = 1_800_000_000.0
    prediction_rows = []
    outcome_rows = []
    for index in range(150):
        created_at = now - (300 - index) * 60
        for regime, probability, success_rate, success_period in (
            ("LOW_QUALITY", 0.65, 3, 10),
            ("HIGH_QUALITY", 0.85, 19, 20),
        ):
            prediction_id = f"{regime}-{index}"
            success = int(index % success_period < success_rate)
            prediction_rows.append((
                prediction_id, created_at, prediction_id, "BTC/EUR", "1h",
                probability, "50", "baseline-v1", "features", "SETTLED",
                "LONG", regime, "10", probability,
            ))
            outcome_rows.append((
                prediction_id, created_at + 30, "25" if success else "-25",
                success, "0", "{}",
            ))
    db.executemany(
        """INSERT INTO predictions(
            prediction_id,created_at,decision_id,symbol,horizon,probability,
            expected_return_bps,model_version,feature_hash,outcome_status,
            predicted_direction,regime,expected_cost_bps,raw_confidence
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        prediction_rows,
    )
    db.executemany(
        """INSERT INTO prediction_outcomes(
            prediction_id,measured_at,realized_return_bps,success,error_bps,detail_json
        ) VALUES(?,?,?,?,?,?)""",
        outcome_rows,
    )
    engine = LearningEngine(
        db, CalibrationEngine(), registry, ResearchEngine(db),
        auto_promotion_enabled=True, enabled=True, auto_calibration_enabled=True,
    )
    result = engine.process_feedback(now=now)
    assert result["promoted"] is True, {key: result.get(key) for key in ("status", "best_scale", "regime_scales", "improvement", "brier", "candidate_brier", "ece", "candidate_ece", "candidate_version", "promoted")}
    assert "LOW_QUALITY" in result["regime_scales"]
    assert "HIGH_QUALITY" in result["regime_scales"]
    parameters = registry.parameters(registry.active(), family="decision")
    assert parameters["confidence_scale_by_regime"]["LOW_QUALITY"] < 1.0
    assert parameters["confidence_scale_by_regime"]["HIGH_QUALITY"] > 1.0




def test_core_exit_optimizer_result_is_returned_by_learning_feedback(db):
    from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine

    class FakeOptimizer:
        def optimize_core_entry_policy(self, now=None):
            return {"status": "CORE_ENTRY_TEST"}

        def optimize_core_exits(self, now=None):
            return {"status": "CORE_EXIT_TEST", "promoted": False}

        def optimize_tactical(self, now=None):
            return {"status": "TACTICAL_ENTRY_TEST"}

        def optimize_tactical_exits(self, now=None):
            return {"status": "TACTICAL_EXIT_TEST"}

    engine = LearningEngine(
        db,
        CalibrationEngine(),
        ModelRegistry(db),
        ResearchEngine(db),
        enabled=True,
        optimizer=FakeOptimizer(),
    )

    result = engine.process_feedback(now=1_800_000_000.0)

    assert result["adaptive_core_policy"]["status"] == "CORE_ENTRY_TEST"
    assert result["adaptive_core_exit_policy"]["status"] == "CORE_EXIT_TEST"
    assert result["adaptive_policy"]["status"] == "TACTICAL_ENTRY_TEST"
    assert result["adaptive_exit_policy"]["status"] == "TACTICAL_EXIT_TEST"



def test_calibration_reports_log_loss_and_reliability_bins():
    metrics = CalibrationEngine().evaluate([
        (0.9, True), (0.8, True), (0.2, False), (0.1, False),
    ])
    assert metrics["status"] == "OK"
    assert metrics["sample_count"] == 4
    assert 0 <= metrics["brier"] <= 1
    assert metrics["log_loss"] >= 0
    assert 0 <= metrics["ece"] <= 1
    assert sum(row["count"] for row in metrics["bins"]) == 4


def test_calibration_rejects_nonfinite_or_out_of_range_probabilities():
    import math
    import pytest

    for invalid in (float("nan"), float("inf"), -0.1, 1.1):
        with pytest.raises(ValueError):
            CalibrationEngine().evaluate([(invalid, True)])
    assert math.isclose(
        CalibrationEngine().evaluate([(0.5, True)])["log_loss"],
        math.log(2.0),
    )


def test_walk_forward_uses_disjoint_future_test_windows_and_frozen_scorer():
    engine = __import__(
        "app.learning.research", fromlist=["ResearchEngine"]
    ).ResearchEngine(None)
    data = [{"timestamp": float(i), "value": i} for i in range(100)]
    seen = []

    def scorer(test_rows):
        seen.append((test_rows[0]["timestamp"], test_rows[-1]["timestamp"]))
        return [0.01 if row["value"] % 2 else -0.005 for row in test_rows]

    result = engine.walk_forward(data, scorer, windows=3)
    assert result["status"] == "OK"
    assert result["method"] == "EXPANDING_WINDOW_DISJOINT_TEST"
    assert result["parameters_promoted"] is False
    assert result["windows"][0]["train_samples"] >= 20
    assert result["windows"][0]["test_start_index"] >= result["windows"][0]["train_samples"]
    assert len({(row["test_start_index"], row["test_end_index_exclusive"]) for row in result["windows"]}) == 3
    assert all(seen[i][1] < seen[i + 1][0] for i in range(len(seen) - 1))


def test_walk_forward_rejects_nonchronological_data():
    engine = __import__(
        "app.learning.research", fromlist=["ResearchEngine"]
    ).ResearchEngine(None)
    data = [{"timestamp": float(i)} for i in range(80)]
    data[50], data[51] = data[51], data[50]
    result = engine.walk_forward(data, lambda rows: [0.0 for _ in rows], windows=3)
    assert result["status"] == "NON_CHRONOLOGICAL_DATA"


def test_research_metrics_include_sortino_expectancy_win_rate_and_tail_loss():
    engine = __import__(
        "app.learning.research", fromlist=["ResearchEngine"]
    ).ResearchEngine(None)
    metrics = engine.metrics([0.02, -0.01, 0.01, -0.03], periods_per_year=12)
    assert metrics["samples"] == 4
    assert metrics["expectancy"] < 0
    assert metrics["win_rate"] == 0.5
    assert metrics["sortino"] < 0
    assert metrics["tail_loss_5pct"] == -0.03
    assert metrics["max_drawdown"] < 0
