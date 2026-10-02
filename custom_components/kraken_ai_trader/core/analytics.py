from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from statistics import mean, pstdev

from .models import Features, MarketSnapshot, OrderBook, Regime


def _returns(values: tuple[Decimal, ...], lag: int) -> Decimal:
    if len(values) <= lag or values[-lag - 1] <= 0:
        return Decimal("0")
    return values[-1] / values[-lag - 1] - Decimal("1")


def _ema(values: tuple[Decimal, ...], period: int) -> Decimal:
    if not values:
        return Decimal("0")
    alpha = Decimal("2") / Decimal(period + 1)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (Decimal("1") - alpha) * out
    return out


def _vol(values: tuple[Decimal, ...], downside: bool = False) -> Decimal:
    if len(values) < 3:
        return Decimal("0")
    rets = [float(values[i] / values[i - 1] - 1) for i in range(1, len(values)) if values[i - 1] > 0]
    if downside:
        rets = [min(0.0, x) for x in rets]
    return Decimal(str(pstdev(rets) * math.sqrt(len(rets)))) if rets else Decimal("0")


def _depth(book: OrderBook, levels: int = 10) -> Decimal:
    qty = sum((q for _, q in book.bids[:levels]), Decimal("0")) + sum((q for _, q in book.asks[:levels]), Decimal("0"))
    mid = book.best_bid if book.best_bid > 0 else book.best_ask
    return qty * mid


def _imbalance(book: OrderBook, levels: int = 10) -> Decimal:
    bid = sum((q for _, q in book.bids[:levels]), Decimal("0"))
    ask = sum((q for _, q in book.asks[:levels]), Decimal("0"))
    total = bid + ask
    return (bid - ask) / total if total else Decimal("0")


def compute_features(snapshot: MarketSnapshot, btc_regime_score: Decimal = Decimal("0"), previous_open_interest: Decimal | None = None) -> Features:
    closes = snapshot.candles.get(15) or snapshot.candles.get(5) or (snapshot.last,)
    ema_fast = _ema(closes[-20:], 8)
    ema_slow = _ema(closes[-40:], 21)
    atr_ratio = Decimal("0")
    if len(closes) >= 15:
        ranges = [abs(float(closes[i] - closes[i - 1])) / float(closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if ranges:
            atr_ratio = Decimal(str(mean(ranges[-14:])))
    vol20 = _vol(closes[-60:])
    downside = _vol(closes[-60:], downside=True)
    vol_mean = mean([float(c) for c in closes[-20:]]) if closes else 1.0
    return Features(
        return_1=_returns(closes, 1),
        return_5=_returns(closes, 5),
        return_20=_returns(closes, 20),
        momentum=sum((_returns(closes, i) for i in (1, 3, 5, 10)), Decimal("0")) / Decimal("4"),
        ema_slope=(ema_fast - ema_slow) / snapshot.mid if snapshot.mid > 0 else Decimal("0"),
        atr_ratio=atr_ratio,
        realized_vol=vol20,
        downside_vol=downside,
        volume_anomaly=Decimal("0") if not closes else (snapshot.volume_24h / Decimal("1e6") - Decimal("1")),
        spread=snapshot.spread_ratio,
        depth=_depth(snapshot.book),
        imbalance=_imbalance(snapshot.book),
        market_impact=snapshot.spread_ratio + (Decimal("1") / max(_depth(snapshot.book), Decimal("1"))),
        relative_strength=_returns(closes, 5),
        btc_regime_score=btc_regime_score,
        funding=snapshot.funding or Decimal("0"),
        basis=(snapshot.last / snapshot.index_price - Decimal("1")) if snapshot.index_price and snapshot.index_price > 0 else Decimal("0"),
        open_interest_change=((snapshot.open_interest - previous_open_interest) / previous_open_interest) if snapshot.open_interest is not None and previous_open_interest and previous_open_interest > 0 else Decimal("0"),
        liquidity_score=min(Decimal("1"), max(Decimal("0"), _depth(snapshot.book) / Decimal("100000"))),
    )


def detect_regime(features: Features) -> Regime:
    if features.spread > Decimal("0.01") or features.liquidity_score < Decimal("0.05"):
        return Regime.LIQUIDITY_STRESS
    if features.realized_vol > Decimal("0.15"):
        return Regime.PANIC if features.downside_vol < Decimal("-0.08") else Regime.HIGH_VOLATILITY
    if features.ema_slope > Decimal("0.015") and features.momentum > Decimal("0.02"):
        return Regime.TREND_UP
    if features.ema_slope < Decimal("-0.015") and features.momentum < Decimal("-0.02"):
        return Regime.TREND_DOWN
    if abs(features.momentum) < Decimal("0.01") and features.realized_vol < Decimal("0.05"):
        return Regime.RANGE
    if features.momentum > Decimal("0.04") or features.momentum < Decimal("-0.04"):
        return Regime.BREAKOUT
    if abs(features.return_1) < abs(features.return_5) / Decimal("2"):
        return Regime.MEAN_REVERSION
    return Regime.UNKNOWN


def model_hash(strategy_version: str, model_version: str) -> str:
    return hashlib.sha256(f"{strategy_version}:{model_version}".encode()).hexdigest()[:16]
