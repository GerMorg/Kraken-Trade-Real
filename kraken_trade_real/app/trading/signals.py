from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Instrument, MarketSnapshot, NewsItem, Signal
from app.domain.states import Direction


D=Decimal


class SignalEngine:
    def evaluate(self, instrument: Instrument, snapshot: MarketSnapshot,
                 features: dict[str,D], regime: str, news_bps: D= D("0"),
                 gemini_bps: D=D("0")) -> tuple[Signal,Signal]:
        trend=features.get("trend",D("0"))
        momentum=features.get("return_5",D("0"))
        vol=max(D("1"),features.get("volatility",D("999")))
        liquidity=max(D("1"),features.get("liquidity",D("1")))
        spread=features.get("spread_bps",D("999"))
        quality=max(D("0"),D("1")-spread/D("200"))*min(D("1"),liquidity/D("1000"))
        long_raw=trend*D("18")+momentum*D("4")+news_bps+gemini_bps
        short_raw=-trend*D("18")-momentum*D("4")-news_bps-gemini_bps
        long_return=max(D("0"),long_raw)*quality
        short_return=max(D("0"),short_raw)*quality
        cost=spread + vol*D("1.5") + D("8")
        long_conf=max(D("0"),min(D("1"),D("0.5")+long_raw/D("20")-vol/D("100")))
        short_conf=max(D("0"),min(D("1"),D("0.5")+short_raw/D("20")-vol/D("100")))
        return (
            Signal(instrument.symbol,Direction.LONG,long_return,cost,long_conf,regime,news_bps,gemini_bps,features),
            Signal(instrument.symbol,Direction.SHORT,short_return,cost,short_conf,regime,-news_bps,-gemini_bps,features),
        )
