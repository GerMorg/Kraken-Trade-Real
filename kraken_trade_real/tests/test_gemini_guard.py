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
