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
