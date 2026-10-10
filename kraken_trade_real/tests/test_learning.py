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



def test_managed_strategy_parameters_persist_and_override_legacy_values(db, tmp_path):
    from app.config import Config
    from app.learning import LearningEngine, CalibrationEngine, ModelRegistry, ResearchEngine

    engine = LearningEngine(db, CalibrationEngine(), ModelRegistry(db), ResearchEngine(db))
    cfg = Config.load(str(tmp_path / "missing-options.json"))
    initial = engine.apply_managed_parameters(cfg)
    assert initial["core"]["version"] == 1
    core_row = db.managed_strategy_parameters("core")
    assert core_row is not None
    parameters = __import__("json").loads(core_row["parameters_json"])
    parameters["strategy_min_edge_bps"] = 31.0
    saved = db.save_managed_strategy_parameters(
        "core", parameters, {"status": "TEST_PROMOTION"}, 450, "TEST",
    )
    assert saved["changed"] is True
    next_cfg = Config.load(str(tmp_path / "still-missing.json"))
    engine.apply_managed_parameters(next_cfg)
    assert next_cfg.strategy_min_edge_bps == 31.0
    assert db.managed_strategy_parameters("core")["version"] == 2


def test_managed_parameter_optimizer_requires_outcome_evidence(db, tmp_path):
    from app.config import Config
    from app.learning import LearningEngine, CalibrationEngine, ModelRegistry, ResearchEngine

    engine = LearningEngine(db, CalibrationEngine(), ModelRegistry(db), ResearchEngine(db))
    cfg = Config.load(str(tmp_path / "missing-options.json"))
    engine.apply_managed_parameters(cfg)
    result = engine.recalibrate_strategy_parameters(cfg, now=1_800_000_000)
    assert result["status"] == "OK"
    assert result["core"]["status"] == "INSUFFICIENT_DATA"
    assert result["tactical"]["status"] == "INSUFFICIENT_TRADE_CONTEXT"
