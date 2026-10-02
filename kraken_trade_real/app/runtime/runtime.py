from __future__ import annotations

from decimal import Decimal
import time
from typing import Any

from app.domain.models import digest_config, new_id
from app.domain.states import RuntimeStage
from app.monitoring.audit import AuditLogger


D=Decimal


class TradingRuntime:
    def __init__(self, config: Any, db: Any, audit: AuditLogger, gateway: Any,
                 discovery: Any, market_data: Any, features: Any, regimes: Any,
                 scanner: Any, news: Any, gemini: Any, signals: Any, decisions: Any,
                 sizer: Any, risk: Any, leverage: Any, intents: Any, authority: Any,
                 portfolio: Any, recovery: Any, learning: Any, registry: Any,
                 sensors: Any) -> None:
        self.config=config
        self.db=db
        self.audit=audit
        self.gateway=gateway
        self.discovery=discovery
        self.market_data=market_data
        self.features=features
        self.regimes=regimes
        self.scanner=scanner
        self.news=news
        self.gemini=gemini
        self.signals=signals
        self.decisions=decisions
        self.sizer=sizer
        self.risk=risk
        self.leverage=leverage
        self.intents=intents
        self.authority=authority
        self.portfolio=portfolio
        self.recovery=recovery
        self.learning=learning
        self.registry=registry
        self.sensors=sensors
        self.state=__import__("app.runtime.state",fromlist=["RuntimeState"]).RuntimeState()
        self.config_hash=digest_config(config.__dict__)
        self.instruments=[]

    def startup(self) -> bool:
        self.state.set(RuntimeStage.CONFIG_LOADED)
        self.audit.emit("STARTUP_CONFIG_LOADED","INFO",config_hash=self.config_hash)
        if not self.config.kraken_enabled:
            self.state.set(RuntimeStage.SAFE_MODE,"KRAKEN_DISABLED")
            return False
        try:
            self.gateway.public_status()
            self.state.set(RuntimeStage.API_CHECKED)
            self.audit.emit("STARTUP_KRAKEN_PUBLIC_OK")
        except Exception as exc:
            self.recovery.issue("KRAKEN_UNAVAILABLE",type(exc).__name__)
            self.state.set(RuntimeStage.DEGRADED,"KRAKEN_UNAVAILABLE")
            return False

        try:
            self.instruments=self.discovery.discover()
            self.db.upsert_instruments(self.instruments)
            self.state.set(RuntimeStage.INSTRUMENTS_SYNCED)
            self.audit.emit("STARTUP_INSTRUMENTS_SYNCED",count=len(self.instruments))
        except Exception as exc:
            self.recovery.issue("KRAKEN_UNAVAILABLE",f"instrument discovery:{type(exc).__name__}")
            self.state.set(RuntimeStage.DEGRADED,"INSTRUMENT_DISCOVERY_FAILED")
            return False

        if self.gateway.api_key:
            try:
                permissions=self.gateway.api_permissions()
                perms=permissions.get("permissions") if isinstance(permissions,dict) else []
                allowed="modify-trades" in perms
                self.db.execute(
                    "INSERT OR REPLACE INTO api_permissions(id,checked_at,permissions_json,ok,detail) VALUES(1,?,?,?,?)",
                    (time.time(),__import__("json").dumps(perms),int(allowed),"modify-trades required for live execution"),
                )
                if self.config.live_enabled and not allowed:
                    self.recovery.issue("PERMISSION_FAILURE","modify-trades missing")
                    self.state.set(RuntimeStage.SAFE_MODE,"PERMISSION_FAILURE")
                    return False
            except Exception as exc:
                self.recovery.issue("AUTH_FAILURE",type(exc).__name__)
                self.state.set(RuntimeStage.SAFE_MODE,"AUTH_FAILURE")
                return False

        try:
            portfolio=self.portfolio.reconcile()
            self.db.save_portfolio("startup",portfolio)
            self.state.set(RuntimeStage.ACCOUNT_RECONCILED)
        except Exception as exc:
            self.recovery.issue("PORTFOLIO_MISMATCH",type(exc).__name__)
            self.state.set(RuntimeStage.SAFE_MODE,"PORTFOLIO_RECONCILE_FAILED")
            return False

        self.registry.active()
        self.state.set(RuntimeStage.MARKET_READY)
        self.state.set(RuntimeStage.MODELS_READY)
        self.state.set(RuntimeStage.READY)
        self.audit.emit("STARTUP_READY","INFO",instruments=len(self.instruments))
        return True

    def run_cycle(self) -> dict[str,Any]:
        if self.state.stage not in {RuntimeStage.READY,RuntimeStage.RUNNING,RuntimeStage.DEGRADED}:
            self.startup()
        cycle_id=new_id("cycle")
        self.state.cycle_id=cycle_id
        self.db.start_cycle(cycle_id,self.config_hash)
        self.state.set(RuntimeStage.RUNNING)
        blockers=[]
        try:
            portfolio=self.portfolio.reconcile()
            if self.recovery.breaker.active:
                blockers.append(self.recovery.breaker.reason)
            spot_payload,future_payload=self.gateway.public_tickers()
            snapshots={}
            feature_map={}
            for instrument in self.instruments:
                payload=spot_payload if instrument.venue=="spot" else future_payload
                snapshot=self.market_data.snapshot(instrument,payload)
                if snapshot:
                    snapshots[instrument.symbol]=snapshot
                    feature_map[instrument.symbol]=self.features.calculate(snapshot)
                    self.db.save_market(snapshot,feature_map[instrument.symbol])
            fast=self.scanner.fast_filter(self.instruments,snapshots)
            ranked=self.scanner.rank(fast,snapshots,feature_map)
            news=self.news.collect() if self.config.news_enabled else []
            selected=ranked[:20]
            gemini_context=[{"symbol":i.symbol,"features":feature_map[i.symbol]} for i in selected[:10]]
            gemini=self.gemini.analyze(
                [n.__dict__ for n in news[:20]],{"markets":gemini_context}
            ) if selected else {"status":"SKIPPED","effect_bps":0}
            gemini_bps=D(str(gemini.get("expected_impact_bps",gemini.get("effect_bps",0)) or 0))
            model_version=self.registry.active()
            placed=0
            last_decision=None
            for instrument in selected:
                snap=snapshots[instrument.symbol]
                f=feature_map[instrument.symbol]
                regime=self.regimes.detect(f)
                news_bps=self.news.effect_for(instrument.symbol,news)
                long_signal,short_signal=self.signals.evaluate(
                    instrument,snap,f,regime,news_bps,gemini_bps
                )
                decision=self.decisions.choose(
                    instrument,long_signal,short_signal,portfolio,model_version,self.config_hash
                )
                if not decision:
                    self.learning.record_cycle(cycle_id,0,0,["NO_ACTION"])
                    continue
                lev=self.leverage.choose(
                    instrument,f,long_signal.confidence if decision.signal.direction.value=="LONG" else short_signal.confidence,
                    (portfolio.gross_eur/portfolio.equity_eur*100 if portfolio.equity_eur else D("999")),
                    None,self.config.risk_max_leverage
                )
                decision=__import__("dataclasses").replace(decision,leverage=lev)
                risk=self.risk.evaluate(decision,portfolio,snap)
                self.db.save_decision(decision)
                if not risk.allowed:
                    blockers.append(f"{instrument.symbol}:{risk.reason}")
                    self.db.learning_event("BLOCKER",decision.decision_id,{"reason":risk.reason,"checks":risk.checks})
                    continue
                quantity=decision.target_notional_eur/snap.price
                method=self.authority.policy.choose(
                    snap.spread_bps,decision.signal.net_edge_bps,f.get("volatility",D("999"))
                )
                price=snap.ask if decision.signal.direction.value=="LONG" else snap.bid
                intent=self.intents.build(decision,lev,method["order_type"],quantity,price)
                result=self.authority.submit(intent,snap)
                last_decision=decision
                self.learning.record_order_outcome(intent.client_order_id,result)
                if result.get("state") in {"ACKNOWLEDGED","LIVE","PARTIALLY_FILLED","FILLED"}:
                    placed+=1
            self.db.save_portfolio(cycle_id,portfolio)
            self.learning.record_cycle(cycle_id,len(selected),placed,blockers)
            self.db.finish_cycle(cycle_id,"COMPLETED",";".join(blockers[:5]))
            self.state.selected_symbol=last_decision.instrument.symbol if last_decision else ""
            self.state.last_edge_bps=str(last_decision.signal.net_edge_bps if last_decision else 0)
            self.state.last_confidence=str(last_decision.signal.confidence if last_decision else 0)
            self.state.set(RuntimeStage.READY,blockers[0] if blockers else "")
            self._publish(portfolio,gemini,model_version)
            return {"cycle_id":cycle_id,"status":"COMPLETED","placed":placed,"blockers":blockers}
        except Exception as exc:
            self.recovery.issue("EXECUTION_FAILURE",type(exc).__name__)
            self.db.finish_cycle(cycle_id,"FAILED",type(exc).__name__)
            self.state.set(RuntimeStage.SAFE_MODE,type(exc).__name__)
            return {"cycle_id":cycle_id,"status":"FAILED","error":type(exc).__name__}

    def _publish(self, portfolio: Any, gemini: dict[str,Any], model_version: str) -> None:
        self.sensors.publish(self.sensors.states(
            status=self.state.stage.value,stage=self.state.stage.value,cycle_id=self.state.cycle_id,
            blocker=self.state.blocker,symbol=self.state.selected_symbol,
            edge_bps=self.state.last_edge_bps,confidence=self.state.last_confidence,
            leverage="1",equity_eur=portfolio.equity_eur,gross_eur=portfolio.gross_eur,
            net_eur=portfolio.net_eur,margin_used_eur=portfolio.margin_used_eur,
            daily_pnl_eur=portfolio.daily_pnl_eur,drawdown_pct=portfolio.drawdown_pct,
            open_positions=len(portfolio.positions),news_status="OK" if self.config.news_enabled else "DISABLED",
            gemini_status=str(gemini.get("status","UNKNOWN")),model_version=model_version,
            breaker_active=self.recovery.breaker.active
        ))
