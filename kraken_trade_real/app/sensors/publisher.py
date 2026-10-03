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

    def publish(self, states: dict[str,Any]) -> dict[str,Any]:
        stats: dict[str, Any] = {"enabled": self.enabled, "attempted": 0, "published": 0, "failed": 0, "last_error": ""}
        if not self.enabled:
            return stats
        for entity_id, state in states.items():
            stats["attempted"] += 1
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
                with urlopen(request,timeout=5):  # nosec B310
                    pass
                stats["published"] += 1
            except (URLError,OSError) as exc:
                stats["failed"] += 1
                stats["last_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
        return stats

    @staticmethod
    def states(status: str, stage: str, cycle_id: str, blocker: str="",
               symbol: str="", edge_bps: Any=0, confidence: Any=0,
               leverage: Any=1, equity_eur: Any=0, gross_eur: Any=0,
               net_eur: Any=0, margin_used_eur: Any=0,
               daily_pnl_eur: Any=0, drawdown_pct: Any=0,
               open_positions: int=0, news_status: str="UNKNOWN",
               gemini_status: str="UNKNOWN", model_version: str="baseline-v1",
               calibration_brier: Any=0, calibration_ece: Any=0,
               breaker_active: bool=False, tax_status: str="DISABLED", tax_estimated_27_5_eur: Any=0,
               tax_incomplete_events: int=0, tax_year: int=0,
               learning_samples: int=0, learning_open_predictions: int=0,
               learning_settled_total: int=0, learning_brier: Any=0,
               learning_improvement: Any=0, portfolio_symbols: list[str]|None=None) -> dict[str,Any]:
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
            "sensor.kraken_trade_open_positions":{"state":str(open_positions),
                "attributes":{"symbols":portfolio_symbols or []}},
            "sensor.kraken_trade_news_status":{"state":news_status},
            "sensor.kraken_trade_gemini_status":{"state":gemini_status},
            "sensor.kraken_trade_model_version":{"state":model_version},
            "sensor.kraken_trade_learning_samples":{"state":str(learning_samples)},
            "sensor.kraken_trade_learning_open_predictions":{"state":str(learning_open_predictions)},
            "sensor.kraken_trade_learning_settled_total":{"state":str(learning_settled_total)},
            "sensor.kraken_trade_learning_brier":{"state":str(learning_brier)},
            "sensor.kraken_trade_learning_improvement":{"state":str(learning_improvement)},
            "sensor.kraken_trade_calibration_brier":{"state":str(calibration_brier)},
            "sensor.kraken_trade_calibration_ece":{"state":str(calibration_ece)},
            "sensor.kraken_trade_circuit_breaker":{"state":"ON" if breaker_active else "OFF"},
            "sensor.kraken_trade_tax_status":{"state":tax_status},
            "sensor.kraken_trade_tax_estimated_27_5_eur":{"state":str(tax_estimated_27_5_eur),"unit_of_measurement":"EUR"},
            "sensor.kraken_trade_tax_incomplete_events":{"state":str(tax_incomplete_events)},
            "sensor.kraken_trade_tax_year":{"state":str(tax_year)},
        }
