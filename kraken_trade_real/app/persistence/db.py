from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import threading
import time
from typing import Iterator, Any
from decimal import Decimal
from app.domain.models import Decision, Fill, Instrument, MarketSnapshot, NewsItem, OrderIntent, PortfolioState

class Database:
    def __init__(self, path: str="/data/state/trader.db") -> None:
        self.path=path
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self._lock=threading.RLock()
        self._init_schema()

    @contextmanager
    def connect(self)->Iterator[sqlite3.Connection]:
        with self._lock:
            con=sqlite3.connect(self.path,timeout=15,isolation_level=None)
            con.row_factory=sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            try: yield con
            finally: con.close()

    def _init_schema(self)->None:
        schema=Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as con:
            con.executescript(schema)
            con.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version','2')")
            # Idempotent upgrades from the first development snapshot.
            cols={row[1] for row in con.execute("PRAGMA table_info(instruments)")}
            for name,definition in {
                "position_limits_json":"TEXT NOT NULL DEFAULT '{}'",
                "collateral_json":"TEXT NOT NULL DEFAULT '{}'",
                "funding_json":"TEXT NOT NULL DEFAULT '{}'",
                "fee_model_json":"TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if name not in cols: con.execute(f"ALTER TABLE instruments ADD COLUMN {name} {definition}")
            ocols={row[1] for row in con.execute("PRAGMA table_info(orders)")}
            if "post_only" not in ocols: con.execute("ALTER TABLE orders ADD COLUMN post_only INTEGER NOT NULL DEFAULT 0")

    def execute(self,sql:str,params:tuple[Any,...]=())->None:
        with self.connect() as con: con.execute(sql,params)
    def query(self,sql:str,params:tuple[Any,...]=())->list[dict[str,Any]]:
        with self.connect() as con: return [dict(r) for r in con.execute(sql,params).fetchall()]
    def one(self,sql:str,params:tuple[Any,...]=())->dict[str,Any]|None:
        rows=self.query(sql,params); return rows[0] if rows else None

    def event(self,code:str,level:str="INFO",payload:dict[str,Any]|None=None)->int:
        with self.connect() as con:
            cur=con.execute("INSERT INTO events(created_at,code,level,payload_json) VALUES(?,?,?,?)",
                            (time.time(),code,level,json.dumps(payload or {},sort_keys=True,default=str)))
            return int(cur.lastrowid or 0)

    def start_cycle(self,cycle_id:str,config_hash:str)->None:
        self.execute("INSERT INTO cycles(cycle_id,started_at,status,config_hash) VALUES(?,?,?,?)",
                     (cycle_id,time.time(),"RUNNING",config_hash))
    def finish_cycle(self,cycle_id:str,status:str,reason:str="")->None:
        self.execute("UPDATE cycles SET finished_at=?,status=?,reason=? WHERE cycle_id=?",
                     (time.time(),status,reason[:1000],cycle_id))

    def upsert_instruments(self,instruments:list[Instrument])->None:
        rows=[(
            i.symbol,i.venue,i.product_type.value,i.instrument_id,i.altname,i.base,i.quote,i.status,
            int(i.margin_available),int(i.long_available),int(i.short_available),
            json.dumps([str(x) for x in i.leverage_levels]),str(i.min_order_qty),str(i.min_cost),
            i.lot_decimals,i.price_decimals,str(i.tick_size),i.margin_class,
            json.dumps(i.metadata.get("position_limits",{}),sort_keys=True,default=str),
            json.dumps(i.metadata.get("collateral",{}),sort_keys=True,default=str),
            json.dumps(i.metadata.get("funding",{}),sort_keys=True,default=str),
            json.dumps(i.metadata.get("fee_model",{}),sort_keys=True,default=str),
            json.dumps(i.metadata,sort_keys=True,default=str),time.time()
        ) for i in instruments]
        self.executemany(
            """INSERT INTO instruments(symbol,venue,product_type,instrument_id,altname,base,quote,status,
            margin_available,long_available,short_available,leverage_levels,min_order_qty,min_cost,
            lot_decimals,price_decimals,tick_size,margin_class,position_limits_json,collateral_json,
            funding_json,fee_model_json,metadata_json,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,venue) DO UPDATE SET
            product_type=excluded.product_type,instrument_id=excluded.instrument_id,altname=excluded.altname,
            base=excluded.base,quote=excluded.quote,status=excluded.status,
            margin_available=excluded.margin_available,long_available=excluded.long_available,
            short_available=excluded.short_available,leverage_levels=excluded.leverage_levels,
            min_order_qty=excluded.min_order_qty,min_cost=excluded.min_cost,lot_decimals=excluded.lot_decimals,
            price_decimals=excluded.price_decimals,tick_size=excluded.tick_size,margin_class=excluded.margin_class,
            position_limits_json=excluded.position_limits_json,collateral_json=excluded.collateral_json,
            funding_json=excluded.funding_json,fee_model_json=excluded.fee_model_json,
            metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",rows)

    def executemany(self,sql:str,rows:list[tuple[Any,...]])->None:
        with self.connect() as con: con.executemany(sql,rows)

    def save_market(self,snapshot:MarketSnapshot,features:dict[str,Any])->None:
        self.execute("""INSERT INTO market_snapshots(captured_at,symbol,price,bid,ask,volume_24h,age_seconds,
          spread_bps,closes_json,depths_bid_json,depths_ask_json,funding_rate,open_interest,basis_bps,
          liquidation_pressure,features_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            snapshot.timestamp,snapshot.symbol,str(snapshot.price),str(snapshot.bid),str(snapshot.ask),
            str(snapshot.volume_24h),snapshot.age_seconds,str(snapshot.spread_bps),
            json.dumps([str(x) for x in snapshot.closes]),
            json.dumps([[str(a),str(b)] for a,b in snapshot.depths_bid]),
            json.dumps([[str(a),str(b)] for a,b in snapshot.depths_ask]),
            str(snapshot.funding_rate) if snapshot.funding_rate is not None else None,
            str(snapshot.open_interest) if snapshot.open_interest is not None else None,
            str(snapshot.basis_bps) if snapshot.basis_bps is not None else None,
            str(snapshot.liquidation_pressure) if snapshot.liquidation_pressure is not None else None,
            json.dumps(features,sort_keys=True,default=str)))

    def save_news(self,item:NewsItem)->None:
        self.execute("""INSERT OR IGNORE INTO news_items(news_id,published_at,source,url,title,summary,
        topics_json,affected_assets_json,direction,impact_bps,novelty,credibility,horizon,market_confirmed,raw_hash)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            item.news_id,item.published_at,item.source,item.url,item.title,item.summary,
            json.dumps(item.topics),json.dumps(item.affected_assets),item.direction,str(item.impact_bps),
            str(item.novelty),str(item.credibility),item.horizon,int(item.market_confirmed),item.raw_hash))

    def save_decision(self,d:Decision)->None:
        self.execute("""INSERT INTO decisions(decision_id,created_at,symbol,direction,target_notional_eur,leverage,
        expected_return_bps,expected_cost_bps,confidence,regime,news_effect_bps,gemini_effect_bps,strategy_version,
        model_version,config_hash,rationale_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            d.decision_id,time.time(),d.instrument.symbol,d.signal.direction.value,str(d.target_notional_eur),
            str(d.leverage),str(d.signal.expected_return_bps),str(d.signal.expected_cost_bps),str(d.signal.confidence),
            d.signal.regime,str(d.signal.news_effect_bps),str(d.signal.gemini_effect_bps),d.strategy_version,
            d.model_version,d.config_hash,json.dumps(d.rationale,sort_keys=True,default=str)))

    def save_order_intent(self,i:OrderIntent)->None:
        self.execute("""INSERT INTO orders(intent_id,client_order_id,created_at,decision_id,symbol,direction,side,
        order_type,quantity,limit_price,leverage,margin,reduce_only,post_only,state,expected_edge_bps,
        max_slippage_bps,expires_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            i.intent_id,i.client_order_id,time.time(),i.decision_id,i.instrument.symbol,i.direction.value,i.side,
            i.order_type,str(i.quantity),str(i.limit_price) if i.limit_price is not None else None,str(i.leverage),
            int(i.margin),int(i.reduce_only),int(i.post_only),i.state.value,str(i.expected_edge_bps),
            str(i.max_slippage_bps),i.expires_seconds))
        self.order_event(i.client_order_id,i.state.value,{"intent_id":i.intent_id})

    def update_order_state(self,client_order_id:str,state:str,**extra:Any)->None:
        order_id=extra.get("kraken_order_id")
        last_error=extra.get("last_error")
        if order_id is not None and last_error is not None:
            self.execute("UPDATE orders SET state=?, kraken_order_id=?, last_error=? WHERE client_order_id=?",
                         (state,order_id,last_error,client_order_id))
        elif order_id is not None:
            self.execute("UPDATE orders SET state=?, kraken_order_id=? WHERE client_order_id=?",
                         (state,order_id,client_order_id))
        elif last_error is not None:
            self.execute("UPDATE orders SET state=?, last_error=? WHERE client_order_id=?",
                         (state,last_error,client_order_id))
        else:
            self.execute("UPDATE orders SET state=? WHERE client_order_id=?",(state,client_order_id))
        self.order_event(client_order_id,state,{k:v for k,v in extra.items()
                         if k in {"kraken_order_id","last_error"}})

    def order_event(self,client_order_id:str,state:str,detail:dict[str,Any])->None:
        self.execute("INSERT INTO order_events(created_at,client_order_id,state,detail_json) VALUES(?,?,?,?)",
                     (time.time(),client_order_id,state,json.dumps(detail,sort_keys=True,default=str)))

    def save_fill(self,f:Fill)->None:
        self.execute("INSERT OR IGNORE INTO fills(order_id,trade_id,created_at,symbol,side,quantity,price,fee,fee_currency) VALUES(?,?,?,?,?,?,?,?,?)",
                     (f.order_id,f.trade_id,f.timestamp,f.symbol,f.side,str(f.quantity),str(f.price),str(f.fee),f.fee_currency))
    def save_portfolio(self,cycle_id:str,state:PortfolioState)->None:
        self.execute("""INSERT INTO portfolio_snapshots(cycle_id,captured_at,equity_eur,cash_eur,gross_eur,net_eur,
        margin_used_eur,unrealized_pnl_eur,realized_pnl_eur,daily_pnl_eur,drawdown_pct,positions_json,open_orders)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            cycle_id,time.time(),str(state.equity_eur),str(state.cash_eur),str(state.gross_eur),str(state.net_eur),
            str(state.margin_used_eur),str(state.unrealized_pnl_eur),str(state.realized_pnl_eur),
            str(state.daily_pnl_eur),str(state.drawdown_pct),json.dumps({k:str(v) for k,v in state.positions.items()}),
            state.open_orders))

    def save_prediction(self, prediction_id: str, decision: Any, probability: float, horizon: str = "15m") -> None:
        import hashlib
        feature_hash = hashlib.sha256(
            json.dumps(decision.rationale, sort_keys=True, default=str).encode()
        ).hexdigest()
        self.execute(
            """INSERT OR IGNORE INTO predictions(
              prediction_id,created_at,decision_id,symbol,horizon,probability,
              expected_return_bps,model_version,feature_hash,outcome_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id, time.time(), decision.decision_id, decision.instrument.symbol,
                horizon, float(probability), str(decision.signal.expected_return_bps),
                decision.model_version, feature_hash, "OPEN",
            ),
        )

    def settle_predictions(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        rows = self.query(
            "SELECT * FROM predictions WHERE outcome_status='OPEN'"
        )
        settled = 0
        for row in rows:
            horizon_seconds = 900 if row["horizon"] == "15m" else 3600
            if now < float(row["created_at"]) + horizon_seconds:
                continue
            start = self.one(
                """SELECT price FROM market_snapshots
                   WHERE symbol=? AND captured_at<=?
                   ORDER BY captured_at DESC LIMIT 1""",
                (row["symbol"], float(row["created_at"])),
            )
            end = self.one(
                """SELECT price FROM market_snapshots
                   WHERE symbol=? AND captured_at>=?
                   ORDER BY captured_at ASC LIMIT 1""",
                (row["symbol"], float(row["created_at"]) + horizon_seconds),
            )
            if not start or not end:
                continue
            start_price = Decimal(str(start["price"]))
            end_price = Decimal(str(end["price"]))
            if start_price <= 0:
                continue
            realized = (end_price / start_price - Decimal("1")) * Decimal("10000")
            expected = Decimal(str(row["expected_return_bps"]))
            signed = realized if expected >= 0 else -realized
            success = int(signed > 0)
            error = Decimal(str(row["probability"])) - Decimal(success)
            self.execute(
                """INSERT OR REPLACE INTO prediction_outcomes(
                   prediction_id,measured_at,realized_return_bps,success,error_bps,detail_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    row["prediction_id"], now, str(realized), success,
                    str(error * Decimal("10000")),
                    json.dumps({"start_price": str(start_price), "end_price": str(end_price)}),
                ),
            )
            self.execute(
                "UPDATE predictions SET outcome_status='SETTLED' WHERE prediction_id=?",
                (row["prediction_id"],),
            )
            settled += 1
        return settled

    def learning_event(self,event_type:str,entity_id:str,payload:dict[str,Any])->None:
        self.execute("INSERT INTO learning_events(created_at,event_type,entity_id,payload_json) VALUES(?,?,?,?)",
                     (time.time(),event_type,entity_id,json.dumps(payload,sort_keys=True,default=str)))


    def tax_event_exists(self, event_id: str) -> bool:
        return self.one("SELECT event_id FROM tax_events WHERE event_id=?", (event_id,)) is not None

    def record_tax_event(self, event: Any) -> None:
        self.execute(
            """INSERT OR REPLACE INTO tax_events(
              event_id,created_at,timestamp,tax_year,venue,product_type,asset,quote_asset,event_type,
              quantity,proceeds_eur,acquisition_cost_eur,realized_gain_eur,fee_eur,fee_asset,tax_class,
              asset_regime,tax_neutral,kest_withheld_eur,foreign_tax_eur,complete,source,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id, time.time(), event.timestamp, event.year, event.venue, event.product_type,
                event.asset, event.quote_asset, event.event_type, str(event.quantity), str(event.proceeds_eur),
                str(event.acquisition_cost_eur), str(event.realized_gain_eur), str(event.fee_eur),
                event.fee_asset, event.tax_class, event.asset_regime, int(event.tax_neutral),
                str(event.kest_withheld_eur), str(event.foreign_tax_eur), int(event.complete),
                event.source, json.dumps(event.detail, sort_keys=True, default=str),
            ),
        )

    def tax_events(self, year: int | None = None) -> list[Any]:
        from app.tax import TaxEvent
        sql = "SELECT * FROM tax_events"
        params: tuple[Any, ...] = ()
        if year is not None:
            sql += " WHERE tax_year=?"
            params = (year,)
        sql += " ORDER BY timestamp,event_id"
        rows = self.query(sql, params)
        return [
            TaxEvent(
                r["event_id"], float(r["timestamp"]), int(r["tax_year"]), r["venue"], r["product_type"],
                r["asset"], r["quote_asset"], r["event_type"], Decimal(r["quantity"]), Decimal(r["proceeds_eur"]),
                Decimal(r["acquisition_cost_eur"]), Decimal(r["realized_gain_eur"]), Decimal(r["fee_eur"]),
                r["fee_asset"], r["tax_class"], r["asset_regime"], bool(r["tax_neutral"]),
                Decimal(r["kest_withheld_eur"]), Decimal(r["foreign_tax_eur"]), bool(r["complete"]),
                r["source"], json.loads(r["detail_json"]),
            )
            for r in rows
        ]

    def record_tax_report(self, year: int, report: dict[str, Any], paths: dict[str, str]) -> None:
        self.execute(
            """INSERT INTO tax_reports(
              created_at,tax_year,report_version,status,json_path,csv_path,markdown_path,summary_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                time.time(), year, report.get("report_version", ""), report.get("summary", {}).get("status", "UNKNOWN"),
                paths["json"], paths["csv"], paths["markdown"],
                json.dumps(report.get("summary", {}), sort_keys=True),
            ),
        )
