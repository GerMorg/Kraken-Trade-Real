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

