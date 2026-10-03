from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
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
                 sensors: Any, tax: Any) -> None:
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
        self.tax=tax
        self._last_tax_sync=0.0
        self.state=__import__("app.runtime.state",fromlist=["RuntimeState"]).RuntimeState()
        self.config_hash=digest_config(config.__dict__)
        self.instruments: list[Any]=[]

    def startup(self) -> bool:
        self.state.set(RuntimeStage.CONFIG_LOADED)
        self.audit.emit("STARTUP_CONFIG_LOADED","INFO",config_hash=self.config_hash)
        self._publish_runtime_status()
        if not self.config.kraken_enabled:
            self.state.set(RuntimeStage.SAFE_MODE,"KRAKEN_DISABLED")
            self.audit.emit("STARTUP_KRAKEN_DISABLED","WARNING",blocker="KRAKEN_DISABLED")
            self._publish_runtime_status()
            return False
        try:
            server_time = self.gateway.spot_public("Time")
            self.state.set(RuntimeStage.API_CHECKED)
            self.audit.emit(
                "STARTUP_KRAKEN_PUBLIC_OK",
                "INFO",
                server_time=server_time.get("unixtime") if isinstance(server_time,dict) else "",
            )
            self._publish_runtime_status()
            try:
                status = self.gateway.public_status()
                self.audit.emit("STARTUP_KRAKEN_STATUS_OK","INFO",status_summary=str(status)[:500])
            except Exception as exc:
                self.audit.emit(
                    "STARTUP_KRAKEN_STATUS_DEGRADED",
                    "WARNING",
                    error=f"{type(exc).__name__}:{str(exc)[:800]}",
                )
        except Exception as exc:
            detail=f"{type(exc).__name__}:{str(exc)[:800]}"
            self.recovery.issue("KRAKEN_UNAVAILABLE",detail)
            self.state.set(RuntimeStage.DEGRADED,"KRAKEN_UNAVAILABLE")
            self._publish_runtime_status()
            return False

        try:
            self.instruments=self.discovery.discover()
            if not self.instruments:
                raise RuntimeError("instrument discovery returned zero instruments")
            self.db.upsert_instruments(self.instruments)
            self.state.set(RuntimeStage.INSTRUMENTS_SYNCED)
            self.audit.emit("STARTUP_INSTRUMENTS_SYNCED",count=len(self.instruments))
            self._publish_runtime_status()
        except Exception as exc:
            detail=f"instrument discovery:{type(exc).__name__}:{str(exc)[:700]}"
            self.recovery.issue("KRAKEN_UNAVAILABLE",detail)
            self.state.set(RuntimeStage.DEGRADED,"INSTRUMENT_DISCOVERY_FAILED")
            self._publish_runtime_status()
            return False

        if self.gateway.api_key:
            try:
                # Validate the Spot key with a read-only account endpoint first.
                self.gateway.spot_balance()
                self.audit.emit("KRAKEN_SPOT_PRIVATE_OK","INFO",mode="READ_ONLY" if not self.config.live_enabled else "LIVE")
                if self.config.live_enabled:
                    permissions=self.gateway.api_permissions()
                    raw_perms=permissions.get("permissions") if isinstance(permissions,dict) else []
                    perms=list(raw_perms) if isinstance(raw_perms,list) else []
                    allowed="modify-trades" in perms
                    self.db.execute(
                        "INSERT OR REPLACE INTO api_permissions(id,checked_at,permissions_json,ok,detail) VALUES(1,?,?,?,?)",
                        (time.time(),__import__("json").dumps(perms),int(allowed),"modify-trades required for live execution"),
                    )
                    if not allowed:
                        self.recovery.issue("PERMISSION_FAILURE","modify-trades missing")
                        self.state.set(RuntimeStage.SAFE_MODE,"PERMISSION_FAILURE")
                        self._publish_runtime_status()
                        return False
            except Exception as exc:
                detail=f"{type(exc).__name__}:{str(exc)[:800]}"
                self.recovery.issue("AUTH_FAILURE",detail)
                self.state.set(RuntimeStage.SAFE_MODE,"AUTH_FAILURE")
                self._publish_runtime_status()
                return False

        try:
            portfolio=self.portfolio.reconcile()
            self.db.save_portfolio("startup",portfolio)
            self.state.set(RuntimeStage.ACCOUNT_RECONCILED)
        except Exception as exc:
            detail=f"{type(exc).__name__}:{str(exc)[:800]}"
            self.recovery.issue("PORTFOLIO_MISMATCH",detail)
            self.state.set(RuntimeStage.SAFE_MODE,"PORTFOLIO_RECONCILE_FAILED")
            self._publish_runtime_status()
            return False

        self._update_tax_report(force=True)
        self.registry.active()
        self.state.set(RuntimeStage.MARKET_READY)
        self.state.set(RuntimeStage.MODELS_READY)
        self.state.set(RuntimeStage.READY)
        self.audit.emit("STARTUP_READY","INFO",instruments=len(self.instruments))
        self._publish_runtime_status()
        return True

    def run_cycle(self) -> dict[str,Any]:
        if self.state.stage not in {RuntimeStage.READY,RuntimeStage.RUNNING}:
            if not self.startup():
                return {"cycle_id":"","status":"DEGRADED","error":self.state.blocker or self.state.stage.value}
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

    def _publish_runtime_status(self) -> None:
        try:
            self.sensors.publish(self.sensors.states(
                status=self.state.stage.value,
                stage=self.state.stage.value,
                cycle_id=self.state.cycle_id,
                blocker=self.state.blocker,
                symbol=self.state.selected_symbol,
                edge_bps=self.state.last_edge_bps,
                confidence=self.state.last_confidence,
                leverage="1",
                equity_eur="0",
                gross_eur="0",
                net_eur="0",
                margin_used_eur="0",
                daily_pnl_eur="0",
                drawdown_pct="0",
                open_positions=0,
                news_status="UNKNOWN",
                gemini_status="UNKNOWN",
                model_version="UNKNOWN",
                breaker_active=self.recovery.breaker.active,
                tax_status="UNKNOWN",
                tax_estimated_27_5_eur="0",
                tax_incomplete_events=0,
                tax_year=datetime.now(timezone.utc).year,
            ))
            self.audit.emit("HA_SENSOR_PUBLISH_ATTEMPTED","INFO",
                            enabled=bool(getattr(self.sensors,"enabled",False)))
        except Exception as exc:
            self.audit.emit("HA_SENSOR_PUBLISH_FAILED","WARNING",
                            error=f"{type(exc).__name__}:{str(exc)[:500]}")

    def _update_tax_report(self, force: bool = False) -> None:
        if not self.config.tax_enabled or not self.config.tax_report_enabled:
            return
        now = time.time()
        if not force and now - self._last_tax_sync < 3600:
            return
        try:
            added = self.tax.sync_kraken_spot_history(self.gateway)
            current_year = datetime.now(timezone.utc).year
            for year in {current_year, current_year - 1}:
                if year < 2021:
                    continue
                paths = self.tax.write_report(year)
                report = self.tax.build_report(year)
                self.db.record_tax_report(year, report, paths)
                self.audit.emit(
                    "TAX_REPORT_UPDATED",
                    "INFO",
                    tax_year=year,
                    events_added=added,
                    status=report["summary"]["status"],
                )
            self._last_tax_sync = now
        except Exception as exc:
            self.audit.emit("TAX_REPORT_FAILED", "WARNING", error=type(exc).__name__)

    def _publish(self, portfolio: Any, gemini: dict[str,Any], model_version: str) -> None:
        tax_report=self.tax.build_report(datetime.now(timezone.utc).year) if self.config.tax_enabled else {}
        tax_summary=tax_report.get("summary",{})
        self.sensors.publish(self.sensors.states(
            status=self.state.stage.value,stage=self.state.stage.value,cycle_id=self.state.cycle_id,
            blocker=self.state.blocker,symbol=self.state.selected_symbol,
            edge_bps=self.state.last_edge_bps,confidence=self.state.last_confidence,
            leverage="1",equity_eur=portfolio.equity_eur,gross_eur=portfolio.gross_eur,
            net_eur=portfolio.net_eur,margin_used_eur=portfolio.margin_used_eur,
            daily_pnl_eur=portfolio.daily_pnl_eur,drawdown_pct=portfolio.drawdown_pct,
            open_positions=len(portfolio.positions),news_status="OK" if self.config.news_enabled else "DISABLED",
            gemini_status=str(gemini.get("status","UNKNOWN")),model_version=model_version,
            breaker_active=self.recovery.breaker.active,
            tax_status=str(tax_summary.get("status","DISABLED")),
            tax_estimated_27_5_eur=tax_summary.get("indicative_27_5_tax_eur","0"),
            tax_incomplete_events=int(tax_summary.get("incomplete_event_count",0)),
            tax_year=datetime.now(timezone.utc).year,
        ))
