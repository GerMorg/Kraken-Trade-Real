from __future__ import annotations
from dataclasses import dataclass,field
from decimal import Decimal
from typing import Any
import hashlib
import json
import time
import uuid
from .states import Direction,OrderState,ProductType

@dataclass(frozen=True)
class Instrument:
    venue:str; product_type:ProductType; symbol:str; instrument_id:str; altname:str; base:str; quote:str
    status:str; margin_available:bool; long_available:bool; short_available:bool
    leverage_levels:tuple[Decimal,...]; min_order_qty:Decimal; min_cost:Decimal; lot_decimals:int
    price_decimals:int; tick_size:Decimal; margin_class:str; metadata:dict[str,Any]=field(default_factory=dict)
    @property
    def max_leverage(self)->Decimal:return max(self.leverage_levels,default=Decimal("1"))
    @property
    def tradeable(self)->bool:return self.status.lower() in {"online","active","open"} and (self.long_available or self.short_available)

@dataclass(frozen=True)
class MarketSnapshot:
    symbol:str; price:Decimal; bid:Decimal; ask:Decimal; volume_24h:Decimal; timestamp:float
    closes:tuple[Decimal,...]=(); depths_bid:tuple[tuple[Decimal,Decimal],...]=()
    depths_ask:tuple[tuple[Decimal,Decimal],...]=(); funding_rate:Decimal|None=None
    open_interest:Decimal|None=None; basis_bps:Decimal|None=None; liquidation_pressure:Decimal|None=None
    @property
    def age_seconds(self)->float:return max(0.0,time.time()-self.timestamp)
    @property
    def spread_bps(self)->Decimal:
        if self.bid<=0 or self.ask<=0:return Decimal("999999")
        return (self.ask-self.bid)/((self.ask+self.bid)/2)*10000

@dataclass(frozen=True)
class Signal:
    symbol:str; direction:Direction; expected_return_bps:Decimal; expected_cost_bps:Decimal; confidence:Decimal
    regime:str; news_effect_bps:Decimal; gemini_effect_bps:Decimal; features:dict[str,Decimal]
    @property
    def net_edge_bps(self)->Decimal:return self.expected_return_bps-self.expected_cost_bps

@dataclass(frozen=True)
class Decision:
    decision_id:str; instrument:Instrument; signal:Signal; target_notional_eur:Decimal; leverage:Decimal
    rationale:dict[str,Any]; strategy_version:str; model_version:str; config_hash:str

@dataclass(frozen=True)
class OrderIntent:
    intent_id:str; client_order_id:str; decision_id:str; instrument:Instrument; direction:Direction
    side:str; order_type:str; quantity:Decimal; limit_price:Decimal|None; leverage:Decimal; margin:bool
    reduce_only:bool; expected_edge_bps:Decimal; max_slippage_bps:Decimal; expires_seconds:int
    post_only:bool=False; state:OrderState=OrderState.INTENT_CREATED

@dataclass
class PortfolioState:
    equity_eur:Decimal=Decimal("0"); cash_eur:Decimal=Decimal("0"); positions:dict[str,Decimal]=field(default_factory=dict)
    gross_eur:Decimal=Decimal("0"); net_eur:Decimal=Decimal("0"); margin_used_eur:Decimal=Decimal("0")
    unrealized_pnl_eur:Decimal=Decimal("0"); realized_pnl_eur:Decimal=Decimal("0"); daily_pnl_eur:Decimal=Decimal("0")
    drawdown_pct:Decimal=Decimal("0"); open_orders:int=0; source_timestamp:float=field(default_factory=time.time)

@dataclass(frozen=True)
class Fill:
    order_id:str; trade_id:str; symbol:str; side:str; quantity:Decimal; price:Decimal; fee:Decimal; fee_currency:str; timestamp:float

@dataclass(frozen=True)
class NewsItem:
    news_id:str; source:str; url:str; title:str; summary:str; published_at:float; topics:tuple[str,...]
    affected_assets:tuple[str,...]; direction:str; impact_bps:Decimal; novelty:Decimal; credibility:Decimal
    horizon:str; market_confirmed:bool; raw_hash:str
    @staticmethod
    def fingerprint(source:str,title:str,url:str)->str:
        return hashlib.sha256(json.dumps([source,title.strip().lower(),url.strip()],sort_keys=True).encode()).hexdigest()

def new_id(prefix:str)->str:return f"{prefix}_{uuid.uuid4().hex}"
def digest_config(data:Any)->str:return hashlib.sha256(json.dumps(data,sort_keys=True,default=str,separators=(",",":")).encode()).hexdigest()
