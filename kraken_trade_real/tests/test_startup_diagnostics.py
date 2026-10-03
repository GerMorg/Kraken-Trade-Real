from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.states import RuntimeStage
from app.kraken.client import HTTP, KrakenError, KrakenGateway
from app.runtime.runtime import TradingRuntime
from app.runtime.state import RuntimeState
from app.sensors.publisher import SensorPublisher


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"result":{"ok":true}}'


def test_http_adds_identifying_headers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("app.kraken.client.urlopen", fake_urlopen)

    result = HTTP().request("https://example.invalid/test")

    assert result["result"]["ok"] is True
    assert captured["request"].get_header("Accept") == "application/json"
    assert captured["request"].get_header("User-agent") == "Kraken-Trade-Real/0.1.6"
    assert captured["timeout"] == 15.0


def test_spot_public_exposes_kraken_error(monkeypatch):
    gateway = KrakenGateway("", "")

    monkeypatch.setattr(
        gateway.http,
        "request",
        lambda *args, **kwargs: {"error": ["EGeneral:Service unavailable"]},
    )

    with pytest.raises(KrakenError, match="KRAKEN_PUBLIC:EGeneral:Service unavailable"):
        gateway.spot_public("SystemStatus")


def test_sensor_states_include_startup_diagnostics():
    states = SensorPublisher.states(
        status="DEGRADED",
        stage="DEGRADED",
        cycle_id="",
        blocker="KRAKEN_UNAVAILABLE",
        tax_year=2026,
    )

    assert states["sensor.kraken_trade_status"]["state"] == "DEGRADED"
    assert states["sensor.kraken_trade_blocker"]["state"] == "KRAKEN_UNAVAILABLE"
    assert "sensor.kraken_trade_tax_year" in states


def test_startup_failure_is_published_and_does_not_enter_cycle():
    published = []
    events = []

    class FakeGateway:
        api_key = ""

        def spot_public(self, method):
            raise KrakenError("NETWORK:[Errno -3] Temporary failure in name resolution")

    class FakeSensors:
        enabled = True

        @staticmethod
        def states(**kwargs):
            return {"sensor.kraken_trade_status": {"state": kwargs["status"]}}

        def publish(self, states):
            published.append(states)

    class FakeRecovery:
        breaker = SimpleNamespace(active=True, reason="KRAKEN_UNAVAILABLE:NETWORK")

        def issue(self, problem, detail):
            events.append((problem, detail))

    class FakeAudit:
        def emit(self, code, level="INFO", **payload):
            events.append((code, level, payload))

    runtime = TradingRuntime.__new__(TradingRuntime)
    runtime.config = SimpleNamespace(kraken_enabled=True)
    runtime.config_hash = "test-hash"
    runtime.state = RuntimeState()
    runtime.audit = FakeAudit()
    runtime.gateway = FakeGateway()
    runtime.recovery = FakeRecovery()
    runtime.sensors = FakeSensors()

    assert runtime.startup() is False
    assert runtime.state.stage is RuntimeStage.DEGRADED
    assert runtime.state.blocker == "KRAKEN_UNAVAILABLE"
    assert published
    assert published[-1]["sensor.kraken_trade_status"]["state"] == "DEGRADED"

    result = runtime.run_cycle()
    assert result["status"] == "DEGRADED"


def test_run_script_uses_with_contenv():
    from pathlib import Path

    run_sh = Path(__file__).resolve().parents[1].joinpath("run.sh")
    assert run_sh.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/with-contenv bashio"



def test_futures_disabled_by_default_and_requires_separate_credentials(tmp_path):
    from app.config import Config

    cfg = Config.load(str(tmp_path / "missing.json"))
    assert cfg.futures_enabled is False
    assert cfg.futures_api_key == ""
    assert cfg.futures_api_secret == ""

    path = tmp_path / "futures.json"
    path.write_text('{"futures_enabled": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="separate Futures API key"):
        Config.load(str(path))


def test_gateway_does_not_call_futures_when_disabled(monkeypatch):
    gateway = KrakenGateway("spot-key", "spot-secret")
    calls = []
    monkeypatch.setattr(gateway, "spot_public", lambda method: calls.append(method) or {})
    spot, futures = gateway.public_instruments()
    assert calls == ["AssetPairs"]
    assert spot == {}
    assert futures == {"instruments": []}


def test_tax_report_failure_is_diagnostic_and_non_blocking():
    events = []

    class FakeConfig:
        tax_enabled = True
        tax_report_enabled = True

    class FakeAudit:
        def emit(self, code, level="INFO", **payload):
            events.append((code, level, payload))

    class FakeTax:
        def sync_kraken_spot_history(self, gateway):
            raise KeyError("missing-tax-field")

    runtime = TradingRuntime.__new__(TradingRuntime)
    runtime.config = FakeConfig()
    runtime.audit = FakeAudit()
    runtime.tax = FakeTax()
    runtime.gateway = object()
    runtime._last_tax_sync = 0.0
    runtime._tax_status = "UNKNOWN"
    runtime._tax_error = ""

    runtime._update_tax_report(force=True)

    assert runtime._tax_status == "ERROR"
    assert "KeyError" in runtime._tax_error
    assert events[-1][0] == "TAX_REPORT_FAILED"
    assert events[-1][2]["stage"] == "tax_history_sync"
    assert "KeyError" in events[-1][2]["traceback"]
