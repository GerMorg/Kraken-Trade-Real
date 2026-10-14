from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.trading.signals import gate_unvalidated_external_effects


D = Decimal


def test_live_execution_suppresses_unvalidated_news_and_gemini_effects():
    config = SimpleNamespace(live_enabled=True, kill_switch=False)

    result = gate_unvalidated_external_effects(config, D("80"), D("-45"))

    assert result["news_bps"] == D("0")
    assert result["gemini_bps"] == D("0")
    assert result["shadow_only"] is True
    assert result["reason"] == "LIVE_EXTERNAL_EFFECTS_NOT_OUT_OF_SAMPLE_VALIDATED"


def test_shadow_or_kill_switch_mode_preserves_finite_raw_external_effects():
    for config in (
        SimpleNamespace(live_enabled=False, kill_switch=False),
        SimpleNamespace(live_enabled=True, kill_switch=True),
    ):
        result = gate_unvalidated_external_effects(config, D("12.5"), D("-3.25"))
        assert result["news_bps"] == D("12.5")
        assert result["gemini_bps"] == D("-3.25")
        assert result["shadow_only"] is False
        assert result["invalid_inputs"] == []


def test_nonfinite_external_effect_is_zeroed_even_outside_live_mode():
    config = SimpleNamespace(live_enabled=False, kill_switch=True)

    result = gate_unvalidated_external_effects(config, D("NaN"), D("Infinity"))

    assert result["news_bps"] == D("0")
    assert result["gemini_bps"] == D("0")
    assert result["shadow_only"] is False
    assert set(result["invalid_inputs"]) == {"news_bps", "gemini_bps"}
