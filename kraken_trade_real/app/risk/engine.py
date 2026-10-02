from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from app.domain.models import Decision,PortfolioState
D=Decimal
@dataclass(frozen=True)
class RiskResult:
    allowed:bool; reason:str; checks:dict[str,bool]; effective_leverage:D
class RiskEngine:
    IMMUTABLE_MAX_LEVERAGE=D(5); IMMUTABLE_MIN_MARGIN_LEVEL=D(200)
    def __init__(self,config:Any,margin_engine:Any,leverage_engine:Any,cost_model:Any)->None:
        self.config=config;self.margin_engine=margin_engine;self.leverage_engine=leverage_engine;self.cost_model=cost_model
    def evaluate(self,d:Decision,p:PortfolioState,market:Any,margin_account:dict[str,Any]|None=None)->RiskResult:
        direction=d.signal.direction.value
        gross_after=p.gross_eur+abs(d.target_notional_eur); delta=d.target_notional_eur if direction=="LONG" else -d.target_notional_eur
        net_after=p.net_eur+delta; eq=p.equity_eur
        if eq<=0:return RiskResult(False,"NO_POSITIVE_EQUITY",{"equity_positive":False},d.leverage)
        pos_pct=abs(d.target_notional_eur)/eq*100;gross_pct=gross_after/eq*100;net_pct=abs(net_after)/eq*100
        checks={"instrument_direction":d.instrument.long_available if direction=="LONG" else d.instrument.short_available,
            "daily_loss":p.daily_pnl_eur>=-(eq*D(str(self.config.risk_daily_loss_pct))/100),
            "drawdown":p.drawdown_pct<=D(str(self.config.risk_max_drawdown_pct)),
            "position_limit":pos_pct<=D(str(self.config.risk_max_position_pct)),
            "gross_limit":gross_pct<=D(str(self.config.risk_max_gross_pct)),
            "net_limit":net_pct<=D(str(self.config.risk_max_net_pct)),
            "open_positions_limit":len(p.positions)<self.config.risk_max_open_positions,
            "cash_reserve":p.cash_eur>=eq*D(str(self.config.risk_cash_reserve_pct))/100,
            "leverage_bound":d.leverage<=min(self.IMMUTABLE_MAX_LEVERAGE,D(str(self.config.risk_max_leverage)),d.instrument.max_leverage),
            "edge_positive":d.signal.net_edge_bps>=D(str(self.config.strategy_min_edge_bps)),
            "confidence":d.signal.confidence>=D(str(self.config.strategy_min_confidence)),
            "extreme_volatility":d.signal.features.get("volatility",D(999))<=D(30)}
        if d.instrument.min_cost>0:checks["minimum_cost"]=d.target_notional_eur>=d.instrument.min_cost
        if d.leverage>1:
            checks["margin_available"]=margin_account is not None
            if margin_account is not None:
                ok,_=self.margin_engine.available(margin_account,d.target_notional_eur/d.leverage)
                checks["margin_free"]=ok
                level=D(str(margin_account.get("margin_level_pct") or 0))
                checks["margin_level"]=level>=self.IMMUTABLE_MIN_MARGIN_LEVEL
                used=(p.margin_used_eur+d.target_notional_eur/d.leverage)/eq*100
                checks["margin_budget"]=used<=D(str(self.config.risk_max_margin_pct))
        failed=next((k for k,v in checks.items() if not v),None)
        return RiskResult(not failed,failed or "RISK_OK",checks,d.leverage)
