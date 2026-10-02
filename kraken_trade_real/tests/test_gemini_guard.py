from app.gemini import GeminiAnalyzer

def test_gemini_disabled_has_no_order_authority(db):
    result=GeminiAnalyzer("", "gemini-3.8-flash", False, db).analyze([],{})
    assert result["status"]=="DISABLED"
    assert "order" not in str(result).lower()

def test_gemini_module_has_no_kraken_import():
    source=open("app/gemini/analyzer.py",encoding="utf-8").read().lower()
    assert "kraken" not in source
