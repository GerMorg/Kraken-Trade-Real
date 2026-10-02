from __future__ import annotations
from decimal import Decimal
from math import sqrt
from app.domain.models import MarketSnapshot
D=Decimal
class FeatureEngine:
    def calculate(self,s:MarketSnapshot)->dict[str,D]:
        c=list(s.closes)
        if len(c)<5:return {"return_1":D(0),"return_5":D(0),"volatility":D(999),"downside_volatility":D(999),
            "ema_slope":D(0),"sma_slope":D(0),"atr_proxy":D(999),"trend":D(0),"liquidity":s.volume_24h,
            "spread_bps":s.spread_bps,"book_imbalance":D(0),"book_depth":D(0),"impact_bps":D(999)}
        def pct(a:D,b:D)->D:return (a/b-1)*100 if b else D(0)
        rs=[pct(c[i],c[i-1]) for i in range(1,len(c))]
        mean=sum(rs,D(0))/D(len(rs));var=sum((x-mean)**2 for x in rs)/D(max(1,len(rs)-1))
        vol=D(str(sqrt(float(var))*sqrt(len(rs))))
        down=[x for x in rs if x<0];dv=sum((x-mean)**2 for x in down)/D(max(1,len(down)-1)) if down else D(0)
        downside=D(str(sqrt(float(dv))*sqrt(len(rs))))
        ef=self._ema(c,min(12,len(c)));es=self._ema(c,min(26,len(c)))
        sf=sum(c[-min(12,len(c)):],D(0))/D(min(12,len(c)));ss=sum(c[-min(26,len(c)):],D(0))/D(min(26,len(c)))
        bid_depth=sum(p*q for p,q in s.depths_bid);ask_depth=sum(p*q for p,q in s.depths_ask)
        imbalance=(bid_depth-ask_depth)/(bid_depth+ask_depth) if bid_depth+ask_depth else D(0)
        impact=(D(1)/max(D(1),bid_depth+ask_depth))*D(10000)*D(10)
        return {"return_1":rs[-1],"return_5":pct(c[-1],c[-6]) if len(c)>=6 else rs[-1],
            "volatility":vol,"downside_volatility":downside,"ema_slope":pct(ef,es),"sma_slope":pct(sf,ss),
            "atr_proxy":sum(abs(x) for x in rs[-min(14,len(rs)):])/D(min(14,len(rs))),
            "trend":pct(ef,es),"liquidity":s.volume_24h,"spread_bps":s.spread_bps,
            "book_imbalance":imbalance,"book_depth":bid_depth+ask_depth,"impact_bps":impact}
    @staticmethod
    def _ema(v:list[D],p:int)->D:
        a=D(2)/D(p+1);e=v[0]
        for x in v[1:]:e=a*x+(D(1)-a)*e
        return e
