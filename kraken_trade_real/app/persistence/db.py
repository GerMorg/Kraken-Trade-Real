from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import threading
import time
import datetime
from typing import Iterator, Any
from decimal import Decimal
from app.domain.models import Decision, Fill, Instrument, MarketSnapshot, NewsItem, OrderIntent, PortfolioState


def _safe_json_object(value: Any) -> dict[str,Any]:
    try:
        parsed=json.loads(value or "{}")
        return parsed if isinstance(parsed,dict) else {}
    except (TypeError,ValueError):
        return {}

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
            if "submitted_at" not in ocols: con.execute("ALTER TABLE orders ADD COLUMN submitted_at REAL")
            # Legacy OPEN predictions don't carry direction, so migration marks
            # them UNKNOWN rather than incorrectly scoring shorts as longs.
            pcols={row[1] for row in con.execute("PRAGMA table_info(predictions)")}
            for name,definition in {
                "predicted_direction":"TEXT NOT NULL DEFAULT 'UNKNOWN'",
                "regime":"TEXT NOT NULL DEFAULT ''",
                "expected_cost_bps":"TEXT NOT NULL DEFAULT '0'",
                "raw_confidence":"REAL NOT NULL DEFAULT 0.5",
            }.items():
                if name not in pcols:
                    con.execute(f"ALTER TABLE predictions ADD COLUMN {name} {definition}")
            fcols={row[1] for row in con.execute("PRAGMA table_info(fills)")}
            for name,definition in {
                "client_order_id":"TEXT NOT NULL DEFAULT ''",
                "decision_id":"TEXT NOT NULL DEFAULT ''",
                "venue":"TEXT NOT NULL DEFAULT 'spot'",
                "quote_asset":"TEXT NOT NULL DEFAULT ''",
                "raw_json":"TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if name not in fcols:
                    con.execute(f"ALTER TABLE fills ADD COLUMN {name} {definition}")
            con.execute("CREATE INDEX IF NOT EXISTS idx_fills_decision_time ON fills(decision_id, created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_fills_client_order ON fills(client_order_id, created_at)")
            con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','10')")

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
        if not rows:
            return
        with self.connect() as con:
            # sqlite is configured for autocommit, so executemany() would
            # otherwise commit every row separately. That is prohibitively
            # slow for a full Kraken universe on HA storage.
            con.execute("BEGIN")
            try:
                con.executemany(sql,rows)
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def latest_market_closes(self, symbols: list[str]) -> dict[str, tuple[float, tuple[Decimal,...]]]:
        wanted=set(symbols)
        if not wanted:
            return {}
        rows=self.query(
            "SELECT symbol, captured_at, closes_json FROM market_history_cache"
        )
        result: dict[str, tuple[float, tuple[Decimal,...]]] = {}
        for row in rows:
            symbol=str(row.get("symbol",""))
            if symbol not in wanted:
                continue
            try:
                values=json.loads(row.get("closes_json") or "[]")
            except (TypeError,ValueError):
                values=[]
            if not isinstance(values,list):
                values=[]
            closes=tuple(Decimal(str(value)) for value in values if value not in (None,""))
            result[symbol]=(float(row.get("captured_at",0)), closes)
        return result

    def save_market_history_cache(
        self, symbol: str, captured_at: float, closes: tuple[Decimal,...]
    ) -> None:
        self.execute(
            """INSERT INTO market_history_cache(symbol,captured_at,closes_json)
               VALUES(?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
               captured_at=excluded.captured_at,closes_json=excluded.closes_json""",
            (symbol, captured_at, json.dumps([str(x) for x in closes])),
        )

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
        max_slippage_bps,expires_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            i.intent_id,i.client_order_id,time.time(),i.decision_id,i.instrument.symbol,i.direction.value,i.side,
            i.order_type,str(i.quantity),str(i.limit_price) if i.limit_price is not None else None,str(i.leverage),
            int(i.margin),int(i.reduce_only),int(i.post_only),i.state.value,str(i.expected_edge_bps),
            str(i.max_slippage_bps),i.expires_seconds))
        self.order_event(i.client_order_id,i.state.value,{"intent_id":i.intent_id})

    def update_order_state(self,client_order_id:str,state:str,**extra:Any)->None:
        order_id=extra.get("kraken_order_id")
        last_error=extra.get("last_error")
        submitted_at=extra.get("submitted_at")
        if submitted_at is not None:
            self.execute("UPDATE orders SET state=?, submitted_at=? WHERE client_order_id=?",(state,submitted_at,client_order_id))
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

    def save_attributed_fill(
        self,
        *,
        order_id: str,
        trade_id: str,
        created_at: float,
        symbol: str,
        side: str,
        quantity: Any,
        price: Any,
        fee: Any,
        fee_currency: str,
        client_order_id: str,
        decision_id: str,
        venue: str,
        quote_asset: str,
        raw_payload: dict[str, Any],
    ) -> bool:
        """Persist an exchange fill exactly once with its originating local decision."""
        with self.connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO fills(
                     order_id,trade_id,created_at,symbol,side,quantity,price,fee,fee_currency,
                     client_order_id,decision_id,venue,quote_asset,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(order_id), str(trade_id), float(created_at), str(symbol), str(side).lower(),
                    str(quantity), str(price), str(fee), str(fee_currency),
                    str(client_order_id), str(decision_id), str(venue), str(quote_asset),
                    json.dumps(raw_payload, sort_keys=True, default=str),
                ),
            )
            return cur.rowcount > 0

    def active_core_exit_episode(self, symbol: str) -> dict[str, Any] | None:
        return self.one(
            """SELECT * FROM core_exit_episodes
               WHERE symbol=? AND closed_at IS NULL
               ORDER BY opened_at DESC LIMIT 1""",
            (symbol,),
        )

    def core_exit_episodes(
        self,
        *,
        eligible_only: bool = False,
        since_timestamp: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if eligible_only:
            clauses.append("eligible_for_learning=1")
        if since_timestamp is not None:
            clauses.append("opened_at>=?")
            params.append(float(since_timestamp))
        return self.query(
            "SELECT * FROM core_exit_episodes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY opened_at ASC",
            tuple(params),
        )

    def create_core_exit_episode(
        self,
        *,
        episode_id: str,
        symbol: str,
        direction: str,
        opened_at: float,
        observed_at: float,
        order_ids: list[str] | tuple[str, ...],
        decision_ids: list[str] | tuple[str, ...],
        open_lot_count: int,
        basis_eur: Any,
        first_profit_pct: Any,
        peak_profit_pct: Any,
        eligible_for_learning: bool,
        eligibility_reason: str,
        exit_policy_snapshot: dict[str, Any],
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            """INSERT INTO core_exit_episodes(
                 episode_id,symbol,direction,opened_at,last_seen_at,closed_at,
                 open_order_ids_json,opening_decision_ids_json,open_lot_count,
                 initial_basis_eur,first_profit_pct,last_profit_pct,peak_profit_pct,
                 eligible_for_learning,eligibility_reason,exit_policy_snapshot_json,
                 close_reason,detail_json
               ) VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                episode_id, symbol, direction, float(opened_at), float(observed_at),
                json.dumps(list(order_ids), sort_keys=True),
                json.dumps(list(decision_ids), sort_keys=True), int(open_lot_count),
                str(basis_eur), str(first_profit_pct), str(first_profit_pct),
                str(peak_profit_pct), int(bool(eligible_for_learning)),
                str(eligibility_reason), json.dumps(exit_policy_snapshot, sort_keys=True, default=str),
                "", json.dumps(detail or {}, sort_keys=True, default=str),
            ),
        )

    def record_core_exit_observation(
        self,
        *,
        episode_id: str,
        cycle_id: str,
        observed_at: float,
        profit_pct: Any,
        peak_profit_pct: Any,
        basis_eur: Any,
        quantity: Any,
        position_eur: Any,
        partial_taken: bool,
        pending_order_id: str,
        policy_snapshot: dict[str, Any],
        quality: dict[str, Any] | None = None,
    ) -> None:
        """Upsert one cycle observation; final portfolio reconciliation wins over the early one."""
        if not str(cycle_id or "").strip():
            return
        self.execute(
            """INSERT INTO core_exit_path_points(
                 episode_id,cycle_id,observed_at,profit_pct,peak_profit_pct,basis_eur,
                 quantity,position_eur,partial_taken,pending_order_id,
                 policy_snapshot_json,quality_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(episode_id,cycle_id) DO UPDATE SET
                 observed_at=excluded.observed_at,
                 profit_pct=excluded.profit_pct,
                 peak_profit_pct=excluded.peak_profit_pct,
                 basis_eur=excluded.basis_eur,
                 quantity=excluded.quantity,
                 position_eur=excluded.position_eur,
                 partial_taken=excluded.partial_taken,
                 pending_order_id=excluded.pending_order_id,
                 policy_snapshot_json=excluded.policy_snapshot_json,
                 quality_json=excluded.quality_json""",
            (
                episode_id, cycle_id, float(observed_at), str(profit_pct),
                str(peak_profit_pct), str(basis_eur), str(quantity), str(position_eur),
                int(bool(partial_taken)), str(pending_order_id or ""),
                json.dumps(policy_snapshot, sort_keys=True, default=str),
                json.dumps(quality or {}, sort_keys=True, default=str),
            ),
        )
        self.execute(
            """UPDATE core_exit_episodes
               SET last_seen_at=?,last_profit_pct=?,peak_profit_pct=?
               WHERE episode_id=? AND closed_at IS NULL""",
            (float(observed_at), str(profit_pct), str(peak_profit_pct), episode_id),
        )

    def close_core_exit_episode(
        self, episode_id: str, closed_at: float, close_reason: str
    ) -> bool:
        with self.connect() as con:
            cur = con.execute(
                """UPDATE core_exit_episodes
                   SET closed_at=?,last_seen_at=?,close_reason=?
                   WHERE episode_id=? AND closed_at IS NULL""",
                (float(closed_at), float(closed_at), str(close_reason), episode_id),
            )
            return cur.rowcount > 0

    def core_exit_path(
        self, episode_id: str, *, until_timestamp: float | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM core_exit_path_points WHERE episode_id=?"
        params: tuple[Any, ...] = (episode_id,)
        if until_timestamp is not None:
            sql += " AND observed_at<=?"
            params += (float(until_timestamp),)
        sql += " ORDER BY observed_at ASC,point_id ASC"
        return self.query(sql, params)

    def save_portfolio(self,cycle_id:str,state:PortfolioState)->None:
        self.execute("""INSERT INTO portfolio_snapshots(cycle_id,captured_at,equity_eur,cash_eur,gross_eur,net_eur,
        margin_used_eur,unrealized_pnl_eur,realized_pnl_eur,daily_pnl_eur,drawdown_pct,positions_json,open_orders)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            cycle_id,time.time(),str(state.equity_eur),str(state.cash_eur),str(state.gross_eur),str(state.net_eur),
            str(state.margin_used_eur),str(state.unrealized_pnl_eur),str(state.realized_pnl_eur),
            str(state.daily_pnl_eur),str(state.drawdown_pct),json.dumps({k:str(v) for k,v in state.positions.items()}),
            state.open_orders))

    @staticmethod
    def prediction_probability_from_confidence(confidence: float) -> float:
        """Cap a rule-derived confidence away from false 0%/100% certainty.

        Keep this score on the same scale that DecisionEngine multiplies by the
        learned confidence_scale. It is a calibration input, not a proven event
        probability; only observed, cost-adjusted outcomes can validate it.
        """
        raw = max(0.0, min(1.0, float(confidence)))
        return max(0.01, min(0.99, raw))

    def save_prediction(
        self, prediction_id: str, decision: Any, probability: float, horizon: str = "15m"
    ) -> float:
        import hashlib
        feature_hash = hashlib.sha256(
            json.dumps(decision.rationale, sort_keys=True, default=str).encode()
        ).hexdigest()
        raw_confidence = max(0.0, min(1.0, float(probability)))
        stored_probability = self.prediction_probability_from_confidence(raw_confidence)
        signal = decision.signal
        self.execute(
            """INSERT OR IGNORE INTO predictions(
              prediction_id,created_at,decision_id,symbol,horizon,probability,
              expected_return_bps,model_version,feature_hash,outcome_status,
              predicted_direction,regime,expected_cost_bps,raw_confidence
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id, time.time(), decision.decision_id, decision.instrument.symbol,
                horizon, stored_probability, str(signal.expected_return_bps),
                decision.model_version, feature_hash, "OPEN",
                str(getattr(signal.direction, "value", signal.direction)).upper(),
                str(signal.regime or ""),
                str(max(Decimal("0"), Decimal(str(signal.expected_cost_bps)))),
                raw_confidence,
            ),
        )
        return stored_probability

    def settle_predictions(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        # Never score against stale prices: bound both sampling windows.
        max_snapshot_gap = 600.0
        rows = self.query("SELECT * FROM predictions WHERE outcome_status='OPEN'")
        settled = 0
        for row in rows:
            created_at = float(row["created_at"])
            horizon_seconds = 900 if row["horizon"] == "15m" else 3600
            target_at = created_at + horizon_seconds
            if now < target_at:
                continue
            start = self.one(
                """SELECT price, captured_at FROM market_snapshots
                   WHERE symbol=? AND captured_at BETWEEN ? AND ?
                   ORDER BY captured_at DESC LIMIT 1""",
                (row["symbol"], created_at - max_snapshot_gap, created_at),
            )
            end = self.one(
                """SELECT price, captured_at FROM market_snapshots
                   WHERE symbol=? AND captured_at BETWEEN ? AND ?
                   ORDER BY ABS(captured_at - ?) ASC, captured_at ASC LIMIT 1""",
                (
                    row["symbol"], target_at - max_snapshot_gap,
                    target_at + max_snapshot_gap, target_at,
                ),
            )
            if not start or not end:
                if now >= target_at + max_snapshot_gap:
                    reason = (
                        "MISSING_OR_STALE_START_SNAPSHOT" if not start
                        else "MISSING_OR_STALE_HORIZON_SNAPSHOT"
                    )
                    self.execute(
                        "UPDATE predictions SET outcome_status='UNSCORABLE' WHERE prediction_id=?",
                        (row["prediction_id"],),
                    )
                    self.learning_event(
                        "PREDICTION_UNSCORABLE", str(row["prediction_id"]),
                        {"reason": reason, "horizon_seconds": horizon_seconds},
                    )
                continue
            start_price = Decimal(str(start["price"]))
            end_price = Decimal(str(end["price"]))
            if start_price <= 0 or end_price <= 0:
                self.execute(
                    "UPDATE predictions SET outcome_status='UNSCORABLE' WHERE prediction_id=?",
                    (row["prediction_id"],),
                )
                self.learning_event(
                    "PREDICTION_UNSCORABLE", str(row["prediction_id"]),
                    {"reason": "NON_POSITIVE_SNAPSHOT_PRICE"},
                )
                continue
            direction = str(row.get("predicted_direction") or "UNKNOWN").upper()
            if direction not in {"LONG", "SHORT"}:
                self.execute(
                    "UPDATE predictions SET outcome_status='UNSCORABLE' WHERE prediction_id=?",
                    (row["prediction_id"],),
                )
                self.learning_event(
                    "PREDICTION_UNSCORABLE", str(row["prediction_id"]),
                    {"reason": "LEGACY_DIRECTION_UNKNOWN"},
                )
                continue
            realized = (end_price / start_price - Decimal("1")) * Decimal("10000")
            directional = realized if direction == "LONG" else -realized
            cost_bps = max(Decimal("0"), Decimal(str(row.get("expected_cost_bps") or "0")))
            net_directional = directional - cost_bps
            success = int(net_directional > 0)
            error = Decimal(str(row["probability"])) - Decimal(success)
            detail = {
                "start_price": str(start_price), "end_price": str(end_price),
                "predicted_direction": direction,
                "raw_realized_return_bps": str(realized),
                "directional_return_bps": str(directional),
                "expected_cost_bps": str(cost_bps),
                "net_directional_return_bps": str(net_directional),
                "success_definition": "directional_return_after_expected_costs_gt_zero",
                "raw_confidence": float(row.get("raw_confidence") or 0.5),
                "regime": str(row.get("regime") or ""),
            }
            self.execute(
                """INSERT OR REPLACE INTO prediction_outcomes(
                   prediction_id,measured_at,realized_return_bps,success,error_bps,detail_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    row["prediction_id"], now, str(realized), success,
                    str(error * Decimal("10000")), json.dumps(detail, sort_keys=True),
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



    def save_tactical_position(
        self,
        symbol: str,
        venue: str,
        direction: str,
        quantity: Any,
        entry_price: Any,
        peak_price: Any,
        trough_price: Any,
        notional_eur: Any,
        leverage: Any,
        opened_at: float,
        entry_client_order_id: str,
        setup_score: Any,
        state: str,
    ) -> None:
        self.execute(
            """INSERT INTO tactical_positions(
               symbol,venue,direction,quantity,entry_price,peak_price,trough_price,
               notional_eur,leverage,opened_at,last_update,entry_client_order_id,
               setup_score,state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
               venue=excluded.venue,direction=excluded.direction,quantity=excluded.quantity,
               entry_price=excluded.entry_price,peak_price=excluded.peak_price,
               trough_price=excluded.trough_price,notional_eur=excluded.notional_eur,
               leverage=excluded.leverage,last_update=excluded.last_update,
               entry_client_order_id=excluded.entry_client_order_id,
               setup_score=excluded.setup_score,state=excluded.state""",
            (
                symbol, venue, direction, str(quantity), str(entry_price), str(peak_price),
                str(trough_price), str(abs(Decimal(str(notional_eur)))), str(leverage), opened_at, time.time(),
                entry_client_order_id, str(setup_score), state,
            ),
        )

    def record_tactical_price_point(
        self,
        symbol: str,
        opened_at: float,
        observed_at: float,
        direction: str,
        price: Any,
        pnl_bps: Any,
        peak_price: Any,
        trough_price: Any,
        state: str,
    ) -> None:
        """Persist a deduplicated market observation for later exit-policy replay."""
        try:
            numeric_price = Decimal(str(price))
            timestamp = float(observed_at)
            opened = float(opened_at)
            if not numeric_price.is_finite() or numeric_price <= 0 or timestamp < opened:
                return
        except (ArithmeticError, TypeError, ValueError):
            return
        with self.connect() as con:
            con.execute(
                """INSERT OR IGNORE INTO tactical_price_path(
                    symbol,opened_at,observed_at,direction,price,pnl_bps,
                    peak_price,trough_price,state
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    symbol, opened, timestamp, direction, str(numeric_price),
                    str(pnl_bps), str(peak_price), str(trough_price), state,
                ),
            )
            # Bound per-position history without truncating normal Tactical trades.
            # Pruning every 128 points keeps the hot path cheap while capping long
            # running positions at 2,048 retained observations.
            count = con.execute(
                "SELECT COUNT(*) FROM tactical_price_path WHERE symbol=? AND opened_at=?",
                (symbol, opened),
            ).fetchone()[0]
            if int(count or 0) > 2048:
                con.execute(
                    """DELETE FROM tactical_price_path
                       WHERE point_id IN (
                         SELECT point_id FROM tactical_price_path
                         WHERE symbol=? AND opened_at=?
                         ORDER BY observed_at DESC LIMIT -1 OFFSET 2048
                       )""",
                    (symbol, opened),
                )

    def tactical_price_path(
        self,
        symbol: str,
        opened_at: float,
        closed_at: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return a position's observed path, optionally truncated at a close time."""
        sql = (
            "SELECT symbol,opened_at,observed_at,direction,price,pnl_bps,"
            "peak_price,trough_price,state FROM tactical_price_path "
            "WHERE symbol=? AND opened_at=?"
        )
        params: tuple[Any, ...] = (symbol, float(opened_at))
        if closed_at is not None:
            sql += " AND observed_at<=?"
            params += (float(closed_at),)
        sql += " ORDER BY observed_at ASC"
        return self.query(sql, params)

    def tactical_positions(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM tactical_positions WHERE state='OPEN' ORDER BY opened_at"
        )

    def delete_tactical_position(self, symbol: str) -> None:
        self.execute("DELETE FROM tactical_positions WHERE symbol=?", (symbol,))

    def save_tactical_trade(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        entry_price: Any,
        exit_price: Any,
        quantity: Any,
        gross_pnl_eur: Any,
        fees_eur: Any,
        net_pnl_eur: Any,
        opened_at: float,
        closed_at: float,
        hold_seconds: float,
        exit_reason: str,
        setup_score: Any,
        detail: dict[str, Any],
    ) -> None:
        self.execute(
            """INSERT OR REPLACE INTO tactical_trades(
               trade_id,symbol,direction,entry_price,exit_price,quantity,
               gross_pnl_eur,fees_eur,net_pnl_eur,opened_at,closed_at,
               hold_seconds,exit_reason,setup_score,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id, symbol, direction, str(entry_price), str(exit_price), str(quantity),
                str(gross_pnl_eur), str(fees_eur), str(net_pnl_eur), opened_at, closed_at,
                hold_seconds, exit_reason, str(setup_score),
                json.dumps(detail, sort_keys=True, default=str),
            ),
        )

    def tactical_trade_count(self, since_timestamp: float) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM tactical_trades WHERE closed_at>=?",
            (since_timestamp,),
        )
        return int(row["n"]) if row else 0

    def tactical_today_net_pnl(self) -> Decimal:
        now = datetime.datetime.now(datetime.timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self.one(
            "SELECT COALESCE(SUM(CAST(net_pnl_eur AS REAL)),0) AS pnl "
            "FROM tactical_trades WHERE closed_at>=?",
            (start,),
        )
        return Decimal(str(row["pnl"])) if row else Decimal("0")

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
                r.get("event_id",""),
                float(r.get("timestamp",r.get("created_at",time.time()))),
                int(r.get("tax_year",1970)),
                r.get("venue","kraken"), r.get("product_type","SPOT"),
                r.get("asset",""), r.get("quote_asset","UNKNOWN"), r.get("event_type","OTHER"),
                Decimal(str(r.get("quantity","0"))), Decimal(str(r.get("proceeds_eur","0"))),
                Decimal(str(r.get("acquisition_cost_eur","0"))), Decimal(str(r.get("realized_gain_eur","0"))),
                Decimal(str(r.get("fee_eur","0"))),
                r.get("fee_asset","EUR"), r.get("tax_class","CRYPTO_27_5"),
                r.get("asset_regime","NEUVERMÖGEN"), bool(r.get("tax_neutral",0)),
                Decimal(str(r.get("kest_withheld_eur","0"))), Decimal(str(r.get("foreign_tax_eur","0"))),
                bool(r.get("complete",0)), r.get("source","kraken"), _safe_json_object(r.get("detail_json")),
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
                paths.get("json",""), paths.get("csv",""), paths.get("markdown",""),
                json.dumps(report.get("summary", {}), sort_keys=True),
            ),
        )


    def tactical_order_progress(self, client_order_id: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT client_order_id,last_filled_quantity,last_average_price,terminal "
            "FROM tactical_order_progress WHERE client_order_id=?",
            (client_order_id,),
        )

    def save_tactical_order_progress(
        self, client_order_id: str, last_filled_quantity: Any,
        last_average_price: Any, *, terminal: bool,
    ) -> None:
        self.execute(
            """INSERT INTO tactical_order_progress(
                 client_order_id,last_filled_quantity,last_average_price,terminal,updated_at
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(client_order_id) DO UPDATE SET
                 last_filled_quantity=excluded.last_filled_quantity,
                 last_average_price=excluded.last_average_price,
                 terminal=excluded.terminal,updated_at=excluded.updated_at""",
            (
                client_order_id, str(last_filled_quantity), str(last_average_price),
                int(terminal), time.time(),
            ),
        )
