from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import time

import pytest

from app.domain.states import RuntimeStage
from app.kraken.client import HTTP, KrakenError, KrakenGateway
from app.runtime.runtime import TradingRuntime, _run_with_hard_timeout
from app.runtime.state import RuntimeState
from app.sensors.publisher import SensorPublisher


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"result":{"ok":true}}'


def test_http_request_supports_per_call_timeout(monkeypatch):
    gateway_http = HTTP(timeout=15.0)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("app.kraken.client.urlopen", fake_urlopen)
    gateway_http.request("https://example.invalid/test", timeout=4.0)

    assert captured["timeout"] == 4.0


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
    assert captured["request"].get_header("User-agent") == "Kraken-Trade-Real/0.1.20"
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
    from contextlib import contextmanager
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


def test_gateway_keeps_tokenized_markets_out_of_startup_by_default(monkeypatch):
    gateway = KrakenGateway("spot-key", "spot-secret")
    calls = []

    def fake_public(method, params=None, *, timeout=None):
        calls.append((method, params or {}, timeout))
        return {}

    monkeypatch.setattr(gateway, "spot_public", fake_public)
    spot, futures = gateway.public_instruments()

    assert calls == [("AssetPairs", {}, None)]
    assert gateway.tokenized_assets_enabled is False
    assert spot == {}
    assert futures == {"instruments": []}


def test_gateway_can_enable_tokenized_markets_explicitly(monkeypatch):
    gateway = KrakenGateway(
        "spot-key",
        "spot-secret",
        tokenized_assets_enabled=True,
    )
    calls = []

    def fake_public(method, params=None, *, timeout=None):
        calls.append((method, params or {}, timeout))
        return {}

    monkeypatch.setattr(gateway, "spot_public", fake_public)
    gateway.public_instruments()

    assert calls[0] == ("AssetPairs", {}, None)
    assert calls[1] == (
        "AssetPairs",
        {"aclass_base": "tokenized_asset"},
        5.0,
    )


def test_optional_xstock_discovery_failure_does_not_hide_spot_universe(monkeypatch):
    gateway = KrakenGateway(
        "spot-key",
        "spot-secret",
        tokenized_assets_enabled=True,
    )

    def fake_public(method, params=None, *, timeout=None):
        if params and params.get("aclass_base") == "tokenized_asset":
            from app.kraken.client import KrakenAmbiguous
            raise KrakenAmbiguous("NETWORK:xstock endpoint timeout")
        return {"XXBTZEUR": {
            "altname": "XBTEUR",
            "wsname": "XBT/EUR",
            "base": "XXBT",
            "quote": "ZEUR",
            "status": "online",
            "ordermin": "0.0001",
            "leverage_buy": [],
            "leverage_sell": [],
        }}

    monkeypatch.setattr(gateway, "spot_public", fake_public)
    spot, futures = gateway.public_instruments()

    assert "XXBTZEUR" in spot
    assert futures == {"instruments": []}
    assert gateway.last_public_instrument_warnings == [
        "KrakenAmbiguous:NETWORK:xstock endpoint timeout"
    ]


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

def test_spot_query_orders_normalizes_txid_keyed_response(monkeypatch):
    gateway = KrakenGateway("spot-key", "spot-secret")
    monkeypatch.setattr(
        gateway,
        "spot_private",
        lambda method, params: {
            "O-123": {"status": "open", "vol": "1.0"},
            "O-456": {"status": "closed", "vol": "2.0"},
        },
    )

    result = gateway.lookup_order(
        client_order_id="client-123",
        instrument=SimpleNamespace(
            product_type=SimpleNamespace(value="SPOT")
        ),
    )

    assert result == [
        {"status": "open", "vol": "1.0", "txid": "O-123"},
        {"status": "closed", "vol": "2.0", "txid": "O-456"},
    ]

def test_bulk_executemany_uses_one_transaction(db, monkeypatch):
    traces = []
    original_connect = db.connect

    @contextmanager
    def traced_connect():
        with original_connect() as con:
            con.set_trace_callback(traces.append)
            yield con

    monkeypatch.setattr(db, "connect", traced_connect)
    rows = [(f"bulk-test-{i}", str(i)) for i in range(20)]
    db.executemany(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
        rows,
    )

    assert sum(trace == "BEGIN" for trace in traces) == 1
    assert sum(trace == "COMMIT" for trace in traces) == 1


def test_hard_timeout_interrupts_blocking_startup_operation():
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="STARTUP_INSTRUMENTS"):
        _run_with_hard_timeout(
            lambda: time.sleep(0.2),
            0.05,
            "STARTUP_INSTRUMENTS",
        )
    assert time.monotonic() - started < 0.15


def test_http_transport_timeout_is_ambiguous(monkeypatch):
    from app.kraken.client import KrakenAmbiguous

    def fake_urlopen(request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr("app.kraken.client.urlopen", fake_urlopen)

    with pytest.raises(KrakenAmbiguous, match="NETWORK:"):
        HTTP().request("https://example.invalid/test")


def test_http_server_error_is_ambiguous(monkeypatch):
    from urllib.error import HTTPError
    from app.kraken.client import KrakenAmbiguous

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr("app.kraken.client.urlopen", fake_urlopen)

    with pytest.raises(KrakenAmbiguous, match="HTTP_503"):
        HTTP().request("https://example.invalid/test")
