from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json, sqlite3, threading, time
from typing import Iterator, Any
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
            return int(cur.lastrowid)

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
        vals=[state]; sets=["state=?"]
        for field in ("kraken_order_id","last_error"):
            if field in extra: sets.append(field+"=?"); vals.append(extra[field])
        vals.append(client_order_id)
        self.execute(f"UPDATE orders SET {','.join(sets)} WHERE client_order_id=?",tuple(vals))
        self.order_event(client_order_id,state,{k:v for k,v in extra.items() if k in {"kraken_order_id","last_error"}})

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

    def learning_event(self,event_type:str,entity_id:str,payload:dict[str,Any])->None:
        self.execute("INSERT INTO learning_events(created_at,event_type,entity_id,payload_json) VALUES(?,?,?,?)",
                     (time.time(),event_type,entity_id,json.dumps(payload,sort_keys=True,default=str)))
