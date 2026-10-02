from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cycles (cycle_id TEXT PRIMARY KEY, started_at REAL, completed_at REAL, state TEXT, blocker TEXT);
CREATE TABLE IF NOT EXISTS market_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, symbol TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS instrument_metadata (instrument_id TEXT PRIMARY KEY, payload TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS portfolio_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, symbol TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, cycle_id TEXT, symbol TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS decision_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT, check_name TEXT, passed INTEGER, detail TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS orders (intent_id TEXT PRIMARY KEY, decision_id TEXT, client_order_id TEXT, kraken_order_id TEXT, state TEXT, payload TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT, event_code TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, payload TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS news_events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS news_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS gemini_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS predictions (prediction_id TEXT PRIMARY KEY, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS prediction_outcomes (prediction_id TEXT PRIMARY KEY, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS learning_events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS calibration_history (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS strategy_versions (version TEXT PRIMARY KEY, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS model_versions (version TEXT PRIMARY KEY, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS model_evaluations (id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS risk_events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS health_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS app_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_code TEXT, payload TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS error_events (id INTEGER PRIMARY KEY AUTOINCREMENT, error_code TEXT, payload TEXT, created_at REAL);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self._connection:
            return
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self.set_meta("schema_version", "1")

    def close(self) -> None:
        if self._connection:
            self._connection.close()
            self._connection = None

    def set_meta(self, key: str, value: str) -> None:
        self.connect()
        assert self._connection
        self._connection.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (key, value))
        self._connection.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        self.connect()
        assert self._connection
        row = self._connection.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def event(self, table: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect()
        assert self._connection
        allowed = {
            "learning_events", "risk_events", "health_snapshots", "app_events", "error_events",
            "gemini_analysis", "news_events", "news_analysis", "calibration_history",
        }
        if table not in allowed:
            raise ValueError(f"unsupported event table: {table}")
        if table == "app_events":
            self._connection.execute("INSERT INTO app_events(event_code,payload,created_at) VALUES(?,?,?)", (payload.get("event_code", "UNKNOWN"), json.dumps(payload, default=str), created_at))
        elif table == "error_events":
            self._connection.execute("INSERT INTO error_events(error_code,payload,created_at) VALUES(?,?,?)", (payload.get("error_code", "UNKNOWN"), json.dumps(payload, default=str), created_at))
        elif table == "health_snapshots":
            self._connection.execute("INSERT INTO health_snapshots(payload,created_at) VALUES(?,?)", (json.dumps(payload, default=str), created_at))
        elif table == "risk_events":
            self._connection.execute("INSERT INTO risk_events(payload,created_at) VALUES(?,?)", (json.dumps(payload, default=str), created_at))
        elif table == "learning_events":
            self._connection.execute("INSERT INTO learning_events(payload,created_at) VALUES(?,?)", (json.dumps(payload, default=str), created_at))
        elif table == "gemini_analysis":
            self._connection.execute("INSERT INTO gemini_analysis(payload,created_at) VALUES(?,?)", (json.dumps(payload, default=str), created_at))
        elif table == "news_events":
            self._connection.execute("INSERT INTO news_events(payload,created_at) VALUES(?,?)", (json.dumps(payload, default=str), created_at))
        elif table == "news_analysis":
            self._connection.execute("INSERT INTO news_analysis(news_id,payload,created_at) VALUES(?,?,?)", (payload.get("news_id"), json.dumps(payload, default=str), created_at))
        elif table == "calibration_history":
            self._connection.execute("INSERT INTO calibration_history(payload,created_at) VALUES(?,?)", (json.dumps(payload, default=str), created_at))
        self._connection.commit()

    def upsert_instrument(self, instrument_id: str, payload: dict[str, Any], updated_at: float) -> None:
        self.connect()
        assert self._connection
        self._connection.execute("INSERT OR REPLACE INTO instrument_metadata(instrument_id,payload,updated_at) VALUES(?,?,?)", (instrument_id, json.dumps(payload, default=str), updated_at))
        self._connection.commit()


    def save_market_snapshot(self, cycle_id: str, symbol: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT INTO market_snapshots(cycle_id,symbol,payload,created_at) VALUES(?,?,?,?)",
            (cycle_id, symbol, json.dumps(payload, default=str), created_at),
        )
        self._connection.commit()

    def save_portfolio_snapshot(self, cycle_id: str, portfolio: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT INTO portfolio_snapshots(cycle_id,payload,created_at) VALUES(?,?,?)",
            (cycle_id, json.dumps(portfolio, default=str), created_at),
        )
        self._connection.commit()

    def save_portfolio_position(self, cycle_id: str, symbol: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT INTO portfolio_positions(cycle_id,symbol,payload,created_at) VALUES(?,?,?,?)",
            (cycle_id, symbol, json.dumps(payload, default=str), created_at),
        )
        self._connection.commit()

    def save_cycle(self, cycle_id: str, started_at: float, state: str, blocker: str = "") -> None:
        self.connect(); assert self._connection
        self._connection.execute("INSERT OR REPLACE INTO cycles(cycle_id,started_at,state,blocker) VALUES(?,?,?,?)", (cycle_id, started_at, state, blocker)); self._connection.commit()

    def complete_cycle(self, cycle_id: str, completed_at: float, state: str, blocker: str = "") -> None:
        self.connect(); assert self._connection
        self._connection.execute("UPDATE cycles SET completed_at=?,state=?,blocker=? WHERE cycle_id=?", (completed_at, state, blocker, cycle_id)); self._connection.commit()

    def save_decision(self, decision_id: str, cycle_id: str, symbol: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute("INSERT OR REPLACE INTO decisions(decision_id,cycle_id,symbol,payload,created_at) VALUES(?,?,?,?,?)", (decision_id, cycle_id, symbol, json.dumps(payload, default=str), created_at)); self._connection.commit()

    def save_decision_check(self, decision_id: str, check_name: str, passed: bool, detail: str, created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT INTO decision_checks(decision_id,check_name,passed,detail,created_at) VALUES(?,?,?,?,?)",
            (decision_id, check_name, int(passed), detail, created_at),
        )
        self._connection.commit()

    def save_order(self, intent_id: str, decision_id: str, client_order_id: str, state: str, payload: dict[str, Any], created_at: float, kraken_order_id: str = "") -> None:
        self.connect(); assert self._connection
        self._connection.execute("INSERT OR REPLACE INTO orders(intent_id,decision_id,client_order_id,kraken_order_id,state,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (intent_id, decision_id, client_order_id, kraken_order_id, state, json.dumps(payload, default=str), created_at, created_at)); self._connection.commit()

    def save_prediction(self, prediction_id: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT OR REPLACE INTO predictions(prediction_id,payload,created_at) VALUES(?,?,?)",
            (prediction_id, json.dumps(payload, default=str), created_at),
        )
        self._connection.commit()

    def save_prediction_outcome(self, prediction_id: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT OR REPLACE INTO prediction_outcomes(prediction_id,payload,created_at) VALUES(?,?,?)",
            (prediction_id, json.dumps(payload, default=str), created_at),
        )
        self._connection.commit()

    def order_by_kraken_order_id(self, kraken_order_id: str) -> sqlite3.Row | None:
        self.connect(); assert self._connection
        return self._connection.execute(
            "SELECT * FROM orders WHERE kraken_order_id=? LIMIT 1",
            (kraken_order_id,),
        ).fetchone()

    def save_fill(self, intent_id: str, payload: dict[str, Any], created_at: float) -> None:
        self.connect(); assert self._connection
        self._connection.execute(
            "INSERT INTO fills(intent_id,payload,created_at) VALUES(?,?,?)",
            (intent_id, json.dumps(payload, default=str), created_at),
        )
        self._connection.commit()

    def order_by_client_order_id(self, client_order_id: str) -> sqlite3.Row | None:
        self.connect(); assert self._connection
        return self._connection.execute(
            "SELECT * FROM orders WHERE client_order_id=? LIMIT 1",
            (client_order_id,),
        ).fetchone()

    def update_order(self, intent_id: str, state: str, payload: dict[str, Any], updated_at: float, kraken_order_id: str = "") -> None:
        self.connect(); assert self._connection
        self._connection.execute("UPDATE orders SET state=?,payload=?,updated_at=?,kraken_order_id=COALESCE(NULLIF(?,''),kraken_order_id) WHERE intent_id=?", (state, json.dumps(payload, default=str), updated_at, kraken_order_id, intent_id)); self._connection.commit()

    def has_open_intent(self, symbol: str) -> bool:
        self.connect(); assert self._connection
        row = self._connection.execute("SELECT 1 FROM orders WHERE state IN ('INTENT_CREATED','PRECHECK_PASSED','SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED') AND json_extract(payload,'$.symbol')=? LIMIT 1", (symbol,)).fetchone()
        return row is not None

    def count_orders_today(self, since_epoch: float) -> int:
        self.connect(); assert self._connection
        row = self._connection.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (since_epoch,)).fetchone()
        return int(row[0]) if row else 0

    def open_orders_for_symbol(self, symbol: str) -> int:
        self.connect(); assert self._connection
        row = self._connection.execute("SELECT COUNT(*) FROM orders WHERE state IN ('LIVE','PARTIALLY_FILLED') AND json_extract(payload,'$.symbol')=?", (symbol,)).fetchone()
        return int(row[0]) if row else 0
