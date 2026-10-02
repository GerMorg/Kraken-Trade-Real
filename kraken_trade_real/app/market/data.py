from __future__ import annotations
from decimal import Decimal,InvalidOperation
import time
from typing import Any
from app.domain.models import Instrument,MarketSnapshot

D=Decimal
def d(v:Any)->D:
    try:return D(str(v if v not in (None,"") else "0"))
    except (InvalidOperation,ValueError):return D("0")

class MarketData:
    def __init__(self,gateway:Any)->None:self.gateway=gateway
    def snapshot(self,instrument:Instrument,ticker_payload:dict[str,Any])->MarketSnapshot|None:
        raw=self._find(instrument,ticker_payload)
        if not raw:return None
        bid,ask,last=(self._spot_values(raw) if instrument.venue=="spot" else self._future_values(raw))
        if min(bid,ask,last)<=0:return None
        volume=d(raw.get("v",[0,0])[1] if isinstance(raw.get("v"),list) else raw.get("volume") or raw.get("vol24h"))
        closes=self._spot_ohlc(instrument.instrument_id) if instrument.venue=="spot" else self._future_candles(instrument.instrument_id)
        bids,asks=self._orderbook(instrument)
        return MarketSnapshot(instrument.symbol,last,bid,ask,volume,time.time(),tuple(closes[-250:]),bids,asks,
                              d(raw.get("fundingRate")) if raw.get("fundingRate") is not None else None,
                              d(raw.get("openInterest")) if raw.get("openInterest") is not None else None,
                              d(raw.get("basisBps")) if raw.get("basisBps") is not None else None,
                              d(raw.get("liquidationPressure")) if raw.get("liquidationPressure") is not None else None)
    @staticmethod
    def _find(i:Instrument,p:dict[str,Any])->dict[str,Any]|None:
        if i.venue=="spot":
            keys=(i.instrument_id,i.altname,i.symbol,i.symbol.replace("/",""))
            for k in keys:
                if isinstance(p.get(k),dict):return p[k]
            for k,v in p.items():
                if str(k).replace("/","").upper()==i.symbol.replace("/","").upper() and isinstance(v,dict):return v
            return None
        for row in p.get("tickers",[]) if isinstance(p.get("tickers"),list) else []:
            if isinstance(row,dict) and str(row.get("symbol"))==i.instrument_id:return row
        return None
    @staticmethod
    def _spot_values(r:dict[str,Any])->tuple[D,D,D]:
        def x(k:str)->D:
            v=r.get(k); return d(v[0] if isinstance(v,list) and v else v)
        return x("b"),x("a"),x("c")
    @staticmethod
    def _future_values(r:dict[str,Any])->tuple[D,D,D]:
        return d(r.get("bid")),d(r.get("ask")),d(r.get("last") or r.get("lastTradePrice"))
    def _spot_ohlc(self,pair:str)->list[D]:
        try:r=self.gateway.spot_public("OHLC",{"pair":pair,"interval":60})
        except Exception:return []
        rows=next((v for v in r.values() if isinstance(v,list)),[])
        rows=rows[:-1] if rows else []  # Kraken's final row is the current incomplete candle.
        return [d(x[4]) for x in rows if isinstance(x,list) and len(x)>4 and d(x[4])>0]
    def _future_candles(self,symbol:str)->list[D]:
        try:r=self.gateway.futures_public("candles",{"symbol":symbol,"interval":1})
        except Exception:return []
        rows=r.get("candles",[]) if isinstance(r,dict) else []
        rows=rows[:-1] if rows else []
        return [d(x.get("close")) for x in rows if isinstance(x,dict) and d(x.get("close"))>0]
    def _orderbook(self,i:Instrument)->tuple[tuple[tuple[D,D],...],tuple[tuple[D,D],...]]:
        try:
            r=(self.gateway.spot_public("Depth",{"pair":i.instrument_id,"count":25})
               if i.venue=="spot" else self.gateway.futures_public("orderbook",{"symbol":i.instrument_id}))
            if i.venue=="spot":
                root=next((v for v in r.values() if isinstance(v,dict)),{})
            else: root=r.get("orderBook",r)
            bids=tuple((d(x[0]),d(x[1])) for x in root.get("bids",[]) if isinstance(x,list) and len(x)>1)
            asks=tuple((d(x[0]),d(x[1])) for x in root.get("asks",[]) if isinstance(x,list) and len(x)>1)
            return bids[:25],asks[:25]
        except Exception:return (),()
