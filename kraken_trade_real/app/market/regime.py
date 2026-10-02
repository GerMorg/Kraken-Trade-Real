from decimal import Decimal
class RegimeEngine:
    def detect(self,features:dict[str,Decimal])->str:
        spread=features.get("spread_bps",Decimal("999999"));vol=features.get("volatility",Decimal("999999"));trend=features.get("trend",Decimal("0"));ret5=features.get("return_5",Decimal("0"))
        if spread>Decimal("250"):return "LIQUIDITY_STRESS"
        if vol>Decimal("15"):return "HIGH_VOLATILITY"
        if vol<Decimal("2") and abs(trend)<Decimal(".2"):return "LOW_VOLATILITY"
        if ret5>Decimal("3") and trend>Decimal(".2"):return "BREAKOUT"
        if trend>Decimal(".3"):return "TREND_UP"
        if trend<Decimal("-.3"):return "TREND_DOWN"
        if abs(trend)<Decimal(".05"):return "RANGE"
        return "UNKNOWN"
