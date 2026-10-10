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
            con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','7')")

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
        rows = self.query("SELECT * FROM predictions WHERE outcome_status='OPEN'")
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

    def save_signal_observation(
        self,
        snapshot: MarketSnapshot,
        signal: Any,
        *,
        direction_available: bool,
        model_version: str,
        horizon_seconds: int = 900,
    ) -> bool:
        """Record both accepted and rejected directional signals for unbiased learning.

        One observation per symbol/direction/horizon bucket prevents the 5-minute
        main cycle from creating three overlapping 15-minute labels for the same
        market interval. The input timestamp is the market snapshot timestamp.
        """
        import hashlib

        horizon = max(60, int(horizon_seconds))
        created_at = float(snapshot.timestamp)
        bucket = int(created_at // horizon)
        direction = str(getattr(signal.direction, "value", signal.direction)).upper()
        key = f"{signal.symbol}|{horizon}|{bucket}|{direction}"
        observation_id = "signal_" + hashlib.sha256(key.encode()).hexdigest()[:32]
        features = getattr(signal, "features", {}) or {}
        cursor_before = self.one(
            "SELECT 1 AS found FROM signal_observations "
            "WHERE symbol=? AND horizon_seconds=? AND horizon_bucket=? AND direction=?",
            (str(signal.symbol), horizon, bucket, direction),
        )
        self.execute(
            """INSERT OR IGNORE INTO signal_observations(
               observation_id,created_at,horizon_seconds,horizon_bucket,symbol,direction,
               direction_available,expected_return_bps,expected_cost_bps,expected_net_edge_bps,
               confidence,regime,news_effect_bps,gemini_effect_bps,features_json,model_version,
               outcome_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id,
                created_at,
                horizon,
                bucket,
                str(signal.symbol),
                direction,
                int(bool(direction_available)),
                str(signal.expected_return_bps),
                str(signal.expected_cost_bps),
                str(signal.net_edge_bps),
                float(signal.confidence),
                str(signal.regime or ""),
                str(signal.news_effect_bps),
                str(signal.gemini_effect_bps),
                json.dumps(features, sort_keys=True, default=str),
                str(model_version),
                "OPEN",
            ),
        )
        return cursor_before is None

    def settle_signal_observations(
        self, now: float | None = None, limit: int = 500
    ) -> dict[str, int]:
        """Settle signal labels from observed prices; retain missing data as unscorable."""
        now = time.time() if now is None else float(now)
        batch_limit = max(1, min(2000, int(limit)))
        rows = self.query(
            """SELECT o.*,
                 (SELECT price FROM market_snapshots m
                  WHERE m.symbol=o.symbol AND m.captured_at<=o.created_at
                  ORDER BY m.captured_at DESC LIMIT 1) AS start_price,
                 (SELECT price FROM market_snapshots m
                  WHERE m.symbol=o.symbol AND m.captured_at>=o.created_at+o.horizon_seconds
                  ORDER BY m.captured_at ASC LIMIT 1) AS end_price
               FROM signal_observations o
               WHERE o.outcome_status='OPEN'
                 AND o.created_at+o.horizon_seconds<=?
               ORDER BY o.created_at ASC LIMIT ?""",
            (now, batch_limit),
        )
        updates: list[tuple[Any, ...]] = []
        unscorable: list[tuple[str, str]] = []
        counts = {"settled": 0, "unscorable": 0, "waiting_for_prices": 0}
        for row in rows:
            start_raw, end_raw = row.get("start_price"), row.get("end_price")
            created_at = float(row.get("created_at") or 0)
            horizon = max(60, int(row.get("horizon_seconds") or 900))
            if start_raw in (None, "") or end_raw in (None, ""):
                if now > created_at + horizon + 86400:
                    unscorable.append((str(row["observation_id"]), "PRICE_HISTORY_MISSING"))
                else:
                    counts["waiting_for_prices"] += 1
                continue
            try:
                start_price, end_price = Decimal(str(start_raw)), Decimal(str(end_raw))
            except Exception:
                unscorable.append((str(row["observation_id"]), "INVALID_PRICE"))
                continue
            if start_price <= 0 or end_price <= 0:
                unscorable.append((str(row["observation_id"]), "NON_POSITIVE_PRICE"))
                continue
            direction = str(row.get("direction") or "").upper()
            if direction not in {"LONG", "SHORT"}:
                unscorable.append((str(row["observation_id"]), "UNKNOWN_DIRECTION"))
                continue
            raw_return = (end_price / start_price - Decimal("1")) * Decimal("10000")
            directional_return = raw_return if direction == "LONG" else -raw_return
            cost = max(
                Decimal("0"),
                Decimal(str(row.get("expected_cost_bps") or "0")),
            )
            net_return = directional_return - cost
            updates.append((
                now,
                str(raw_return),
                str(net_return),
                int(net_return > 0),
                json.dumps({
                    "start_price": str(start_price),
                    "end_price": str(end_price),
                    "direction": direction,
                    "raw_return_bps": str(raw_return),
                    "directional_return_bps": str(directional_return),
                    "expected_cost_bps": str(cost),
                    "net_return_bps": str(net_return),
                    "label": "directional_return_minus_expected_cost_gt_zero",
                }, sort_keys=True),
                str(row["observation_id"]),
            ))

        with self.connect() as con:
            con.execute("BEGIN")
            try:
                con.executemany(
                    """UPDATE signal_observations
                       SET settled_at=?,realized_return_bps=?,net_return_bps=?,success=?,
                           outcome_detail_json=?,outcome_status='SETTLED'
                       WHERE observation_id=? AND outcome_status='OPEN'""",
                    updates,
                )
                con.executemany(
                    """UPDATE signal_observations SET outcome_status='UNSCORABLE',
                       outcome_detail_json=? WHERE observation_id=? AND outcome_status='OPEN'""",
                    [(json.dumps({"reason": reason}), observation_id)
                     for observation_id, reason in unscorable],
                )
                con.commit()
            except BaseException:
                con.rollback()
                raise
        counts["settled"] = len(updates)
        counts["unscorable"] = len(unscorable)
        return counts

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
