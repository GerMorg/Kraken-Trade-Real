from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.config import Config
from app.domain.models import Instrument
from app.domain.states import ProductType
from app.persistence import Database


class FakeGateway:
    api_key="test-key"

    def __init__(self):
        self.orders=[]
        self.lookup={}
        self.instrument_payload=(
            {
                "XXBTZEUR":{
                    "altname":"XBTEUR","wsname":"XBT/EUR","base":"XXBT","quote":"ZEUR",
                    "status":"online","ordermin":"0.0001","costmin":"0.5","lot_decimals":4,
                    "pair_decimals":2,"leverage_buy":["2"],"leverage_sell":["2"]
                }
            },
            {"instruments":[{
                "symbol":"PF_XBTUSD","underlying":"XBT","quoteCurrency":"USD",
                "type":"perpetual","tradeable":True,"maxLeverage":5,"minOrderSize":"1","tickSize":"1"
            }]}
        )

    def public_status(self): return ({"status":"online"},{"result":"success"})
    def public_instruments(self): return self.instrument_payload

    def public_tickers(self):
        return (
            {
                "XXBTZEUR":{"b":["60000"],"a":["60010"],"c":["60005"],"v":["10","1000"]}
            },
            {"tickers":[{"symbol":"PF_XBTUSD","bid":"60000","ask":"60010","last":"60005","volume":"1000"}]}
        )

    def spot_public(self, method, params=None):
        if method=="AssetPairs": return self.instrument_payload[0]
        if method=="OHLC":
            return {"XXBTZEUR":[[i,60000+i,60000+i,60000+i,60010+i,0,100,10] for i in range(1,61)]}
        if method=="Ticker": return self.public_tickers()[0]
        if method=="SystemStatus": return {"status":"online"}
        return {}

    def futures_public(self, method, params=None):
        if method=="instruments": return self.instrument_payload[1]
        if method=="tickers": return self.public_tickers()[1]
        if method=="candles":
            return {"candles":[{"close":str(60000+i)} for i in range(1,61)]}
        if method=="status": return {"result":"success","status":"online"}
        return {"result":"success"}

    def spot_private(self, method, params=None):
        if method=="GetApiKeyInfo": return {"permissions":["query-funds","query-open-trades","query-closed-trades","modify-trades","close-trades","create-ws-token"]}
        if method=="Balance": return {"ZEUR":"50.0"}
        if method=="TradeBalance": return {"eb":"50.0","n":"0"}
        if method=="OpenPositions": return {}
        if method=="AddOrder": return {"txid":["OFAKE"]}
        if method=="QueryOrders": return {}
        return {}

    def futures_private(self, method, params=None):
        if method=="accounts": return {"accounts":{}}
        if method=="openpositions": return {"openPositions":[]}
        if method=="sendorder": return {"result":"success","sendStatus":{"status":"placed","order_id":"FUTURE"}}
        if method=="ordersstatus": return {"orders":[]}
        return {"result":"success"}

    def spot_trades_history(self, params=None): return {"trades": {}}
    def api_permissions(self): return self.spot_private("GetApiKeyInfo")
    def websocket_token(self): return {"token":"never-log-this"}
    def submit_spot_order(self, **kwargs):
        self.orders.append(("spot",kwargs))
        return self.spot_private("AddOrder")
    def submit_futures_order(self, **kwargs):
        self.orders.append(("futures",kwargs))
        return self.futures_private("sendorder")
    def lookup_order(self, **kwargs): return self.lookup.get(kwargs["client_order_id"],[])


@pytest.fixture
def fake_gateway():
    return FakeGateway()


@pytest.fixture
def db(tmp_path: Path):
    return Database(str(tmp_path/"state"/"trader.db"))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    path=tmp_path/"options.json"
    path.write_text(json.dumps({
        "kraken_enabled":True,"live_enabled":False,"kill_switch":True,
        "gemini_enabled":False,"market_min_liquidity_eur":1.0,
        "market_max_spread_bps":100,"market_max_data_age_seconds":60,
        "strategy_min_edge_bps":5,"strategy_min_confidence":0.5,
        "risk_max_position_pct":15,"risk_max_gross_pct":80,"risk_max_net_pct":50,
        "risk_max_margin_pct":35,"risk_max_leverage":3,"risk_max_open_positions":3,
        "risk_daily_loss_pct":3,"risk_max_drawdown_pct":8,"risk_cash_reserve_pct":20,
    }),encoding="utf-8")
    return Config.load(str(path))


@pytest.fixture
def instrument():
    return Instrument(
        venue="spot",product_type=ProductType.SPOT_MARGIN,symbol="XBT/EUR",
        instrument_id="XXBTZEUR",altname="XBTEUR",base="XXBT",quote="ZEUR",status="online",
        margin_available=True,long_available=True,short_available=True,
        leverage_levels=(Decimal("1"),Decimal("2")),min_order_qty=Decimal("0.0001"),
        min_cost=Decimal("0.5"),lot_decimals=4,price_decimals=2,tick_size=Decimal("0.01"),
        margin_class="spot-margin",metadata={}
    )
