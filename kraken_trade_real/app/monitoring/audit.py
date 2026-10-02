from __future__ import annotations

from pathlib import Path
import json
import logging
import re
import sys
import threading
import time
from typing import Any

_SECRET_KEYS = re.compile(r"(api[_-]?key|api[_-]?secret|token|password|authorization|authent)", re.I)
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
log = logging.getLogger("kraken_trade_real")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "***REDACTED***" if _SECRET_KEYS.search(str(k)) else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(r"\1***REDACTED***", value)[:4000]
    return value


class AuditLogger:
    def __init__(self, enabled: bool = True, path: str = "/data/logs/trader-events.jsonl") -> None:
        self.enabled = enabled
        self.path = Path(path)
        self.lock = threading.RLock()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        if not log.handlers:
            log.addHandler(handler)
        log.setLevel(logging.INFO)
        self.handler = handler

    def emit(self, code: str, level: str = "INFO", **payload: Any) -> None:
        safe = redact(payload)
        event = {
            "ts": time.time(),
            "code": code,
            "level": level.upper(),
            "payload": safe,
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        with self.lock:
            log.info(line)
            if self.enabled:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
