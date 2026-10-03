import signal
import time

import pytest

from app.gemini import GeminiAnalyzer


def test_gemini_disabled_has_no_order_authority(db):
    result=GeminiAnalyzer("", "gemini-3.8-flash", False, db).analyze([],{})
    assert result["status"]=="DISABLED"
    assert "order" not in str(result).lower()


def test_gemini_module_has_no_kraken_import():
    source=open("app/gemini/analyzer.py",encoding="utf-8").read().lower()
    assert "kraken" not in source


def test_gemini_request_has_hard_timeout(db):
    analyzer = GeminiAnalyzer("key", "gemini-3.8-flash", True, db, timeout_seconds=30)

    class FakeResponse:
        output_text = '{"relevance":0.5,"sentiment":0,"expected_impact_bps":1,"confidence":0.6,"regime_hint":"neutral","topics":[],"affected_assets":[],"counterargument":"none"}'

    class FakeInteractions:
        def __init__(self):
            self.timeout = None

        def create(self, **kwargs):
            self.timeout = kwargs["timeout"]
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.interactions = FakeInteractions()

    analyzer._client = FakeClient()
    result = analyzer.analyze([], {})
    assert result["status"] == "OK"
    assert analyzer._client.interactions.timeout == 30.0


def test_gemini_timeout_returns_without_breaking_cycle(db):
    analyzer = GeminiAnalyzer("key", "gemini-3.8-flash", True, db, timeout_seconds=30)

    class FakeInteractions:
        def create(self, **kwargs):
            raise TimeoutError("simulated timeout")

    class FakeClient:
        interactions = FakeInteractions()

    analyzer._client = FakeClient()
    result = analyzer.analyze([], {})
    assert result["status"] == "TIMEOUT"
    assert result["timeout_seconds"] == 30
    row = db.one("SELECT code FROM events WHERE code='GEMINI_TIMEOUT' ORDER BY id DESC LIMIT 1")
    assert row is not None


def test_gemini_falls_back_after_quota_exhaustion(db):
    analyzer = GeminiAnalyzer(
        "key",
        "gemini-3.8-flash",
        True,
        db,
        timeout_seconds=30,
        fallback_models="gemini-3.5-flash-lite,gemini-3.6-flash",
    )

    class FakeResponse:
        output_text = '{"relevance":0.5,"sentiment":0,"expected_impact_bps":2,"confidence":0.7,"regime_hint":"neutral","topics":[],"affected_assets":[],"counterargument":"none"}'

    class FakeInteractions:
        def __init__(self):
            self.models=[]

        def create(self, **kwargs):
            self.models.append(kwargs["model"])
            if kwargs["model"]=="gemini-3.8-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.interactions = FakeInteractions()

    analyzer._client = FakeClient()
    result = analyzer.analyze([], {})
    assert result["status"] == "OK"
    assert result["model"] == "gemini-3.5-flash-lite"
    assert result["fallback_used"] is True
    assert result["attempted_models"] == ["gemini-3.8-flash","gemini-3.5-flash-lite"]
    assert analyzer._client.interactions.models == result["attempted_models"]
    row = db.one("SELECT code FROM events WHERE code='GEMINI_MODEL_FALLBACK' ORDER BY id DESC LIMIT 1")
    assert row is not None


def test_gemini_all_models_exhausted_returns_zero_impact(db):
    analyzer = GeminiAnalyzer(
        "key",
        "gemini-3.8-flash",
        True,
        db,
        timeout_seconds=30,
        fallback_models="gemini-3.5-flash-lite,gemini-3.6-flash",
    )

    class FakeInteractions:
        def create(self, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

    class FakeClient:
        interactions = FakeInteractions()

    analyzer._client = FakeClient()
    result = analyzer.analyze([], {})
    assert result["status"] == "QUOTA_EXHAUSTED"
    assert result["effect_bps"] == 0.0
    assert result["expected_impact_bps"] == 0.0
    assert result["attempted_models"] == [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
    ]
    assert result["reason"] == "ALL_MODELS_FAILED"
    row = db.one("SELECT code FROM events WHERE code='GEMINI_FALLBACK_EXHAUSTED' ORDER BY id DESC LIMIT 1")
    assert row is not None

@pytest.mark.skipif(not hasattr(signal,"SIGALRM"),reason="hard timeout uses POSIX SIGALRM")
def test_gemini_hard_timeout_interrupts_blocking_operation(db):
    analyzer=GeminiAnalyzer("", "gemini-3.8-flash", False, db)
    started=time.monotonic()
    with pytest.raises(TimeoutError,match="hard timeout"):
        analyzer._run_hard_timeout(lambda: time.sleep(1),0.05)
    assert time.monotonic()-started < 0.5

