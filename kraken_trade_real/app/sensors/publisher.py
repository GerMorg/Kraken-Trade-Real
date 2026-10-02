from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.monitoring.audit import redact


class SensorPublisher:
    def __init__(self, enabled: bool, supervisor_token: str | None,
                 base_url: str="http://supervisor/core/api") -> None:
        self.enabled=enabled and bool(supervisor_token)
        self.token=supervisor_token or ""
        self.base_url=base_url.rstrip("/")

    def publish(self, states: dict[str,Any]) -> None:
        if not self.enabled:
            return
        for entity_id, state in states.items():
            payload=state if isinstance(state,dict) else {"state":str(state)}
            safe=redact(payload)
            body=json.dumps(safe,ensure_ascii=False,default=str).encode("utf-8")
            request=Request(
                f"{self.base_url}/states/{entity_id}",
                data=body,
                headers={
                    "Authorization":f"Bearer {self.token}",
                    "Content-Type":"application/json",
                },
                method="POST",
            )
            try:
                with urlopen(request,timeout=5):
                    pass
            except URLError:
                # Sensor transport is observational; it must never block trading.
                continue

    @staticmethod
    def states(status: str, stage: str, cycle_id: str, blocker: str="",
               symbol: str="", edge_bps: Any=0, confidence: Any=0,
               leverage: Any=1, equity_eur: Any=0, gross_eur: Any=0,
               net_eur: Any=0, margin_used_eur: Any=0,
               daily_pnl_eur: Any=0, drawdown_pct: Any=0,
               open_positions: int=0, news_status: str="UNKNOWN",
               gemini_status: str="UNKNOWN", model_version: str="baseline-v1",
               calibration_brier: Any=0, calibration_ece: Any=0,
               breaker_active: bool=False) -> dict[str,Any]:
        return {
            "sensor.kraken_trade_status":{"state":status,"attributes":{"stage":stage}},
            "sensor.kraken_trade_stage":{"state":stage},
            "sensor.kraken_trade_cycle":{"state":cycle_id},
            "sensor.kraken_trade_blocker":{"state":blocker or "NONE"},
            "sensor.kraken_trade_selected_symbol":{"state":symbol or "NONE"},
            "sensor.kraken_trade_expected_edge_bps":{"state":str(edge_bps),"unit_of_measurement":"bps"},
            "sensor.kraken_trade_confidence":{"state":str(confidence)},
            "sensor.kraken_trade_leverage":{"state":str(leverage),"unit_of_measurement":"x"},
            "sensor.kraken_trade_equity_eur":{"state":str(equity_eur),"unit_of_measurement":"EUR"},
            "sensor.kraken_trade_gross_eur":{"state":str(gross_eur),"unit_of_measurement":"EUR"},
            "sensor.kraken_trade_net_eur":{"state":str(net_eur),"unit_of_measurement":"EUR"},
            "sensor.kraken_trade_margin_used_eur":{"state":str(margin_used_eur),"unit_of_measurement":"EUR"},
            "sensor.kraken_trade_daily_pnl_eur":{"state":str(daily_pnl_eur),"unit_of_measurement":"EUR"},
            "sensor.kraken_trade_drawdown_pct":{"state":str(drawdown_pct),"unit_of_measurement":"%"},
            "sensor.kraken_trade_open_positions":{"state":str(open_positions)},
            "sensor.kraken_trade_news_status":{"state":news_status},
            "sensor.kraken_trade_gemini_status":{"state":gemini_status},
            "sensor.kraken_trade_model_version":{"state":model_version},
            "sensor.kraken_trade_calibration_brier":{"state":str(calibration_brier)},
            "sensor.kraken_trade_calibration_ece":{"state":str(calibration_ece)},
            "sensor.kraken_trade_circuit_breaker":{"state":"ON" if breaker_active else "OFF"},
        }
