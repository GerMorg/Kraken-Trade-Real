from __future__ import annotations

import json
import sqlite3
import time

from app.persistence.db import Database


def _insert_order(db, *, order_id: str, client_id: str, decision_id: str, symbol: str, at: float):
    db.execute(
        """INSERT INTO orders(
             intent_id,client_order_id,created_at,decision_id,symbol,direction,side,
             order_type,quantity,limit_price,leverage,margin,reduce_only,post_only,
             state,submitted_at,kraken_order_id,expected_edge_bps,max_slippage_bps,expires_seconds
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "intent-" + client_id, client_id, at, decision_id, symbol, "LONG", "buy",
            "market", "0.1", None, "1", 0, 0, 0, "ACKNOWLEDGED", at,
            order_id, "20", "30", 60,
        ),
    )


def _trade(trade_id: str, order_id: str, timestamp: float, price: str) -> dict:
    return {
        "ordertxid": order_id,
        "pair": "XXBTZEUR",
        "time": timestamp,
        "type": "buy",
        "ordertype": "market",
        "price": price,
        "cost": str(float(price) * 0.01),
        "fee": "0.01",
        "vol": "0.01",
        "margin": "0.00000",
        "leverage": "0",
        "misc": "",
        "trade_id": trade_id,
    }


def test_spot_history_sync_paginates_durably_and_attributes_local_decision(
    db, fake_gateway, instrument, config
):
    from app.trading.authority import TradingAuthority

    now = time.time()
    _insert_order(
        db, order_id="OLOCAL1", client_id="local-client-1",
        decision_id="decision-1", symbol=instrument.symbol, at=now - 100,
    )
    pages = {
        "": {
            "trades": {"T1": _trade("T1", "OLOCAL1", now - 50, "100")},
            "cursor": {"next": "CURSOR-2"},
        },
        "CURSOR-2": {
            "trades": {"T2": _trade("T2", "OLOCAL1", now - 40, "101")},
            "cursor": {},
        },
    }
    seen_cursors = []

    def history(params):
        assert params["with_cursor"] is True
        assert params["limit"] == 1
        assert params["trades"] is True
        assert params["ledgers"] is True
        seen_cursors.append((params["start"], params["end"], params.get("cursor", "")))
        return pages.get(params.get("cursor", ""), {"trades": {}, "cursor": {}})

    fake_gateway.spot_trades_history = history
    authority = TradingAuthority(config, fake_gateway, db, None, None, None)

    first = authority.sync_spot_fills([instrument], max_pages=1, page_size=1)
    assert first["status"] == "IN_PROGRESS"
    assert first["matched"] == 1
    assert db.one("SELECT value FROM metadata WHERE key='core_spot_fill_sync_cursor'")["value"] == "CURSOR-2"

    second = authority.sync_spot_fills([instrument], max_pages=1, page_size=1)
    assert second["status"] == "COMPLETE"
    assert second["inserted"] == 1
    assert seen_cursors[0][0] == seen_cursors[1][0]
    assert seen_cursors[0][1] == seen_cursors[1][1]
    assert [entry[2] for entry in seen_cursors] == ["", "CURSOR-2"]

    fills = db.query("SELECT * FROM fills ORDER BY created_at")
    assert [row["trade_id"] for row in fills] == ["T1", "T2"]
    assert {row["decision_id"] for row in fills} == {"decision-1"}
    assert {row["client_order_id"] for row in fills} == {"local-client-1"}
    assert {row["fee_currency"] for row in fills} == {"UNVERIFIED"}
    assert json.loads(fills[0]["raw_json"])["ordertxid"] == "OLOCAL1"
    assert db.one("SELECT value FROM metadata WHERE key='core_spot_fill_sync_watermark'")["value"]


def test_spot_history_sync_ignores_untracked_manual_trades(
    db, fake_gateway, instrument, config
):
    from app.trading.authority import TradingAuthority

    now = time.time()
    _insert_order(
        db, order_id="OLOCAL2", client_id="local-client-2",
        decision_id="decision-2", symbol=instrument.symbol, at=now - 100,
    )
    fake_gateway.spot_trades_history = lambda params: {
        "trades": {
            "TMANUAL": _trade("TMANUAL", "OMANUAL", now - 30, "100"),
            "TLOCAL": _trade("TLOCAL", "OLOCAL2", now - 20, "102"),
        },
        "cursor": {},
    }
    authority = TradingAuthority(config, fake_gateway, db, None, None, None)

    result = authority.sync_spot_fills([instrument], max_pages=1)

    assert result["status"] == "COMPLETE"
    assert result["matched"] == 1
    assert result["unmatched"] == 1
    rows = db.query("SELECT trade_id,decision_id FROM fills")
    assert rows == [{"trade_id": "TLOCAL", "decision_id": "decision-2"}]


def test_attributed_fill_insert_is_idempotent(db):
    fields = dict(
        order_id="O1", trade_id="T1", created_at=1_800_000_000.0,
        symbol="XBT/EUR", side="buy", quantity="0.01", price="100",
        fee="0.01", fee_currency="UNVERIFIED", client_order_id="C1",
        decision_id="D1", venue="spot", quote_asset="ZEUR",
        raw_payload={"ordertxid": "O1", "trade_id": "T1"},
    )
    assert db.save_attributed_fill(**fields) is True
    assert db.save_attributed_fill(**fields) is False
    assert db.one("SELECT COUNT(*) AS n FROM fills")["n"] == 1


def test_legacy_fill_table_migrates_before_attribution_indexes_are_created(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as con:
        con.execute(
            """CREATE TABLE fills(
                 order_id TEXT NOT NULL, trade_id TEXT NOT NULL, created_at REAL NOT NULL,
                 symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
                 price TEXT NOT NULL, fee TEXT NOT NULL, fee_currency TEXT NOT NULL,
                 PRIMARY KEY(order_id, trade_id)
               )"""
        )
        con.execute(
            "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)",
            ("OOLD", "TOLD", 1_800_000_000.0, "XBT/EUR", "buy", "0.01",
             "100", "0.01", "ZEUR"),
        )

    migrated = Database(str(database_path))
    columns = {row["name"] for row in migrated.query("PRAGMA table_info(fills)")}
    assert {"client_order_id", "decision_id", "venue", "quote_asset", "base_asset", "raw_json"} <= columns
    assert migrated.one(
        "SELECT decision_id,raw_json FROM fills WHERE order_id='OOLD' AND trade_id='TOLD'"
    ) == {"decision_id": "", "raw_json": "{}"}
    assert migrated.one(
        "SELECT value FROM metadata WHERE key='schema_version'"
    )["value"] == "12"

def test_spot_history_fill_fee_currency_is_resolved_from_linked_ledgers(db, fake_gateway, instrument, config):
    from app.trading.authority import TradingAuthority

    now = time.time()
    _insert_order(
        db, order_id="O-FEE-LEDGER", client_id="C-FEE-LEDGER",
        decision_id="D-FEE-LEDGER", symbol=instrument.symbol, at=now - 50,
    )
    trade = _trade("T-FEE-LEDGER", "O-FEE-LEDGER", now - 10, "100")
    trade["ledgers"] = ["L-FEE", "L-OTHER"]
    request_params = []

    def history(params):
        request_params.append(dict(params))
        return {"trades": {"T-FEE-LEDGER": trade}, "cursor": {}}

    def ledgers(params):
        assert set(params["id"].split(",")) == {"L-FEE", "L-OTHER"}
        return {
            "L-FEE": {"type": "trade", "asset": str(instrument.quote), "fee": "0.01"},
            "L-OTHER": {"type": "trade", "asset": str(instrument.base), "fee": "0"},
        }

    fake_gateway.spot_trades_history = history
    fake_gateway.spot_query_ledgers = ledgers
    result = TradingAuthority(config, fake_gateway, db, None, None, None).sync_spot_fills(
        [instrument], max_pages=1,
    )

    fill = db.one("SELECT * FROM fills WHERE trade_id='T-FEE-LEDGER'")
    assert result["status"] == "COMPLETE"
    assert request_params[0]["ledgers"] is True
    assert fill["fee_currency"] == str(instrument.quote)
    assert fill["base_asset"] == str(instrument.base)
    attribution = json.loads(fill["raw_json"])["_ledger_attribution"]
    assert attribution["status"] == "LEDGER_VERIFIED"
    assert attribution["fee_currency"] == str(instrument.quote)


def test_spot_fill_ledgers_failure_never_blocks_fill_persistence(db, fake_gateway, instrument, config):
    from app.trading.authority import TradingAuthority

    now = time.time()
    _insert_order(
        db, order_id="O-FEE-FAIL", client_id="C-FEE-FAIL",
        decision_id="D-FEE-FAIL", symbol=instrument.symbol, at=now - 50,
    )
    trade = _trade("T-FEE-FAIL", "O-FEE-FAIL", now - 10, "100")
    trade["ledgers"] = ["L-MISSING"]
    fake_gateway.spot_trades_history = lambda params: {"trades": {"T-FEE-FAIL": trade}, "cursor": {}}

    def denied(_params):
        raise RuntimeError("permission denied")

    fake_gateway.spot_query_ledgers = denied
    result = TradingAuthority(config, fake_gateway, db, None, None, None).sync_spot_fills(
        [instrument], max_pages=1,
    )

    fill = db.one("SELECT * FROM fills WHERE trade_id='T-FEE-FAIL'")
    assert result["status"] == "COMPLETE"
    assert result["ledger_lookup_failures"] == 1
    assert fill["fee_currency"] == "UNVERIFIED"
    assert json.loads(fill["raw_json"])["_ledger_attribution"]["status"] == "UNVERIFIED"
    assert db.one(
        "SELECT COUNT(*) AS n FROM events WHERE code='SPOT_FILL_LEDGER_LOOKUP_FAILED'"
    )["n"] == 1


def test_existing_unverified_fill_is_enriched_once_when_ledger_data_becomes_available(
    db, fake_gateway, instrument, config
):
    from app.trading.authority import TradingAuthority

    now = time.time()
    _insert_order(
        db, order_id="O-FEE-ENRICH", client_id="C-FEE-ENRICH",
        decision_id="D-FEE-ENRICH", symbol=instrument.symbol, at=now - 50,
    )
    trade = _trade("T-FEE-ENRICH", "O-FEE-ENRICH", now - 10, "100")
    db.save_attributed_fill(
        order_id="O-FEE-ENRICH", trade_id="T-FEE-ENRICH", created_at=now - 10,
        symbol=instrument.symbol, side="buy", quantity="0.01", price="100",
        fee="0.01", fee_currency="UNVERIFIED", client_order_id="C-FEE-ENRICH",
        decision_id="D-FEE-ENRICH", venue="spot", quote_asset=str(instrument.quote),
        raw_payload=trade,
    )
    trade["ledgers"] = ["L-FEE-ENRICH"]
    fake_gateway.spot_trades_history = lambda params: {"trades": {"T-FEE-ENRICH": trade}, "cursor": {}}
    fake_gateway.spot_query_ledgers = lambda params: {
        "L-FEE-ENRICH": {
            "type": "trade", "asset": str(instrument.quote), "fee": "0.01",
        }
    }

    TradingAuthority(config, fake_gateway, db, None, None, None).sync_spot_fills(
        [instrument], max_pages=1,
    )

    fill = db.one("SELECT * FROM fills WHERE trade_id='T-FEE-ENRICH'")
    assert fill["fee_currency"] == str(instrument.quote)
    assert fill["quantity"] == "0.01"
    assert fill["price"] == "100"
    assert json.loads(fill["raw_json"])["_ledger_attribution"]["status"] == "LEDGER_VERIFIED"

