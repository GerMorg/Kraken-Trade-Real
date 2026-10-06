from __future__ import annotations

import json
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from datetime import datetime, timezone
import time
from typing import Any, Callable

from app.domain.models import digest_config, new_id
from app.domain.states import RuntimeStage
from app.monitoring.audit import AuditLogger
from app.runtime.watchdog import RuntimeWatchdog, WatchdogSnapshot
from app.trading.fx import FXConversionManager


D=Decimal


class _StageTimeout(TimeoutError):
    pass


def _run_with_hard_timeout(
    fn: Callable[[], Any],
    timeout_seconds: float,
    stage: str,
) -> Any:
    """Run a blocking startup operation with a real POSIX deadline.

    urllib socket timeouts are inactivity limits, not total operation
    deadlines. The startup watchdog cannot interrupt a blocked main thread,
    so startup-critical synchronous work also gets a SIGALRM deadline.
    """
    if threading.current_thread() is not threading.main_thread():
        return fn()
    timeout = max(0.1, float(timeout_seconds))
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)
    def _handler(signum: int, frame: Any) -> None:
        raise _StageTimeout(f"{stage} exceeded {timeout:g}s")
    signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return fn()
    finally:
        remaining = signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            restore_after = max(0.0, previous_timer[0] - (timeout - remaining[0]))
            signal.setitimer(signal.ITIMER_REAL, restore_after)


class TradingRuntime:
    STEP_TIMEOUTS = {
        "STARTUP_CONFIG": 30.0, "STARTUP_API": 45.0, "STARTUP_INSTRUMENTS": 90.0,
        "STARTUP_AUTH": 60.0, "STARTUP_PORTFOLIO": 120.0, "STARTUP_TAX": 180.0,
        "CYCLE_START": 30.0, "LEARNING_FEEDBACK": 45.0, "MARKET_DATA": 180.0,
        "MARKET_SCAN": 30.0, "NEWS": 30.0, "GEMINI": 180.0, "DECISIONS": 180.0,
        "PORTFOLIO_FINAL": 120.0,
    }

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
        self.fx=FXConversionManager(config, db, audit, gateway, portfolio, self.instruments)
        self._last_tax_sync=0.0
        self._tax_status="UNKNOWN"
        self._tax_error=""
        self._learning_summary: dict[str,Any] = {"status":"UNKNOWN","samples":0,"settled":0,"settled_total":0,"open_predictions":0}
        self.state=__import__("app.runtime.state",fromlist=["RuntimeState"]).RuntimeState()
        self.watchdog=RuntimeWatchdog(self._handle_watchdog_timeout)
        self.config_hash=digest_config(config.__dict__)
        self.instruments: list[Any]=[]
        self._startup_instrument_operation = "IDLE"

    def startup(self) -> bool:
        self.state.set(RuntimeStage.CONFIG_LOADED)
        self._watchdog_arm("", "STARTUP_CONFIG")
        self.audit.emit("STARTUP_CONFIG_LOADED","INFO",config_hash=self.config_hash)
        self._publish_runtime_status()
        try:
            self._recover_stale_cycles()
        except Exception as exc:
            self.audit.emit("STALE_CYCLE_RECOVERY_FAILED","WARNING",error=f"{type(exc).__name__}:{str(exc)[:500]}")
        if not self.config.kraken_enabled:
            self.state.set(RuntimeStage.SAFE_MODE,"KRAKEN_DISABLED")
            self.audit.emit("STARTUP_KRAKEN_DISABLED","WARNING",blocker="KRAKEN_DISABLED")
            self._publish_runtime_status()
            self._watchdog_clear("")
            return False
        self._watchdog_arm("", "STARTUP_API")
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
            self._watchdog_clear("")
            return False

        self._watchdog_arm("", "STARTUP_INSTRUMENTS")
        self._startup_instrument_operation = "DISCOVERY"
        self.audit.emit(
            "STARTUP_INSTRUMENT_DISCOVERY_START",
            "INFO",
            hard_timeout_seconds=75,
            operation=self._startup_instrument_operation,
            kraken_operation=getattr(self.gateway, "last_public_instrument_stage", "IDLE"),
        )
        try:
            def _discover_and_persist() -> None:
                self._startup_instrument_operation = "DISCOVERY"
                self.instruments=self.discovery.discover()
                self.audit.emit(
                    "STARTUP_INSTRUMENT_DISCOVERY_COMPLETED",
                    "INFO",
                    count=len(self.instruments),
                    kraken_operation=getattr(
                        self.gateway, "last_public_instrument_stage", "UNKNOWN"
                    ),
                )
                optional_warnings = list(
                    getattr(self.gateway, "last_public_instrument_warnings", [])
                )
                if optional_warnings:
                    self.audit.emit(
                        "STARTUP_OPTIONAL_MARKETS_DEGRADED",
                        "WARNING",
                        component="xstocks",
                        warnings=optional_warnings,
                    )
                if not self.instruments:
                    raise RuntimeError("instrument discovery returned zero instruments")
                self._startup_instrument_operation = "PERSIST_INSTRUMENTS"
                self.audit.emit(
                    "STARTUP_INSTRUMENT_PERSIST_START",
                    "INFO",
                    count=len(self.instruments),
                )
                self.db.upsert_instruments(self.instruments)
            self.fx.set_instruments(self.instruments)
                self._startup_instrument_operation = "COMPLETE"
                self.audit.emit(
                    "STARTUP_INSTRUMENT_PERSIST_COMPLETED",
                    "INFO",
                    count=len(self.instruments),
                )

            _run_with_hard_timeout(
                _discover_and_persist,
                min(self.STEP_TIMEOUTS["STARTUP_INSTRUMENTS"] - 5.0, 75.0),
                "STARTUP_INSTRUMENTS",
            )
            self.state.set(RuntimeStage.INSTRUMENTS_SYNCED)
            self.audit.emit("STARTUP_INSTRUMENTS_SYNCED",count=len(self.instruments))
            self.audit.emit(
                "STARTUP_UNIVERSE_BREAKDOWN",
                "INFO",
                total=len(self.instruments),
                families=self.scanner.count_by_family(self.instruments),
            )
            self._publish_runtime_status()
        except Exception as exc:
            operation=self._startup_instrument_operation
            kraken_operation=getattr(
                self.gateway, "last_public_instrument_stage", "UNKNOWN"
            )
            detail=(
                f"instrument startup:{type(exc).__name__}:"
                f"{str(exc)[:500]}:operation={operation}:"
                f"kraken_operation={kraken_operation}"
            )
            self.recovery.issue("KRAKEN_UNAVAILABLE",detail)
            self.audit.emit(
                "STARTUP_INSTRUMENTS_FAILED",
                "ERROR",
                error=detail,
                operation=operation,
                kraken_operation=kraken_operation,
            )
            self.state.set(RuntimeStage.DEGRADED,"INSTRUMENT_DISCOVERY_FAILED")
            self._publish_runtime_status()
            self._watchdog_clear("")
            return False

        if self.gateway.api_key:
            self._watchdog_arm("", "STARTUP_AUTH")
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
                        self._watchdog_clear("")
                        return False
            except Exception as exc:
                detail=f"{type(exc).__name__}:{str(exc)[:800]}"
                self.recovery.issue("AUTH_FAILURE",detail)
                self.state.set(RuntimeStage.SAFE_MODE,"AUTH_FAILURE")
                self._publish_runtime_status()
                self._watchdog_clear("")
                return False

        self._watchdog_arm("", "STARTUP_PORTFOLIO")
        try:
            try:
                startup_spot_tickers,_=self.gateway.public_tickers()
                ticker_warnings = list(
                    getattr(self.gateway, "last_public_ticker_warnings", [])
                )
                if ticker_warnings:
                    self.audit.emit(
                        "STARTUP_OPTIONAL_MARKETS_DEGRADED",
                        "WARNING",
                        component="xstocks_ticker",
                        warnings=ticker_warnings,
                    )
                self.portfolio.set_market_context(self.instruments,startup_spot_tickers)
                self.audit.emit("STARTUP_PORTFOLIO_MARKET_CONTEXT","INFO",tickers=len(startup_spot_tickers))
            except Exception as exc:
                self.audit.emit(
                    "STARTUP_PORTFOLIO_MARKET_CONTEXT_FAILED",
                    "WARNING",
                    error=f"{type(exc).__name__}:{str(exc)[:500]}",
                )
            portfolio=self.portfolio.reconcile()
            self.db.save_portfolio("startup",portfolio)
            reconcile_pending=getattr(self.authority,"reconcile_pending",None)
            if callable(reconcile_pending):
                try:
                    reconciliation=reconcile_pending(self.instruments)
                    self.audit.emit("STARTUP_ORDER_RECONCILIATION","INFO",**reconciliation)
                except Exception as exc:
                    self.audit.emit(
                        "STARTUP_ORDER_RECONCILIATION_FAILED",
                        "WARNING",
                        error=f"{type(exc).__name__}:{str(exc)[:500]}",
                    )
            self.state.set(RuntimeStage.ACCOUNT_RECONCILED)
        except Exception as exc:
            detail=f"{type(exc).__name__}:{str(exc)[:800]}"
            self.recovery.issue("PORTFOLIO_MISMATCH",detail)
            self.state.set(RuntimeStage.SAFE_MODE,"PORTFOLIO_RECONCILE_FAILED")
            self._publish_runtime_status()
            self._watchdog_clear("")
            return False

        self._watchdog_arm("", "STARTUP_TAX")
        self._update_tax_report(force=True)
        self.registry.active()
        self.state.set(RuntimeStage.MARKET_READY)
        self.state.set(RuntimeStage.MODELS_READY)
        self.state.set(RuntimeStage.READY)
        self._watchdog_clear("")
        self.audit.emit("STARTUP_READY","INFO",instruments=len(self.instruments))
        self._publish_runtime_status()
        return True

    def run_cycle(self) -> dict[str,Any]:
        if self.state.stage not in {RuntimeStage.READY,RuntimeStage.RUNNING}:
            if not self.startup():
                return {"cycle_id":"","status":"DEGRADED","error":self.state.blocker or self.state.stage.value}
        cycle_id=new_id("cycle")
        cycle_started=False
        stage="CYCLE_START"
        blockers=[]
        self.state.cycle_id=cycle_id
        self._watchdog_arm(cycle_id, stage)
        try:
            self.db.start_cycle(cycle_id,self.config_hash)
            cycle_started=True
            self.state.set(RuntimeStage.RUNNING)
            self.audit.emit("CYCLE_START","INFO",cycle_id=cycle_id,instruments=len(self.instruments))
            self._publish_runtime_status()

            stage="LEARNING_FEEDBACK"
            self._watchdog_arm(cycle_id, stage)
            try:
                feedback=self.learning.process_feedback()
                self._learning_summary=feedback
                self.audit.emit("LEARNING_FEEDBACK","INFO",cycle_id=cycle_id,**feedback)
            except Exception as exc:
                self.audit.emit("LEARNING_FEEDBACK_FAILED","WARNING",cycle_id=cycle_id,error=f"{type(exc).__name__}:{str(exc)[:500]}")

            stage="MARKET_DATA"
            self._watchdog_arm(cycle_id, stage)
            market_stage_started=time.monotonic()
            ticker_captured_at=time.time()
            spot_payload,future_payload=self.gateway.public_tickers()
            self.portfolio.set_market_context(self.instruments,spot_payload)
            self.fx.set_instruments(self.instruments)
            portfolio=self.portfolio.reconcile()
            self.audit.emit(
                "CYCLE_PORTFOLIO_RECONCILED",
                "INFO",
                cycle_id=cycle_id,
                equity_eur=str(portfolio.equity_eur),
                cash_eur=str(portfolio.cash_eur),
                gross_eur=str(portfolio.gross_eur),
                open_positions=len(portfolio.positions),
                position_symbols=sorted(portfolio.positions),
            )
            self.audit.emit(
                "CYCLE_PORTFOLIO_POSITIONS",
                "INFO",
                cycle_id=cycle_id,
                count=len(portfolio.positions),
                positions={symbol:str(value) for symbol,value in portfolio.positions.items()},
            )
            if self.recovery.breaker.active:
                blockers.append(self.recovery.breaker.reason)

            reconcile_pending=getattr(self.authority,"reconcile_pending",None)
            if callable(reconcile_pending):
                try:
                    reconciliation=reconcile_pending(self.instruments)
                    self.audit.emit(
                        "CYCLE_ORDER_RECONCILIATION",
                        "INFO",
                        cycle_id=cycle_id,
                        **reconciliation,
                    )
                except Exception as exc:
                    self.audit.emit(
                        "CYCLE_ORDER_RECONCILIATION_FAILED",
                        "WARNING",
                        cycle_id=cycle_id,
                        error=f"{type(exc).__name__}:{str(exc)[:500]}",
                    )
            self._watchdog_heartbeat(cycle_id, stage)

            ticker_snapshots={}
            for instrument in self.instruments:
                payload=spot_payload if instrument.venue=="spot" else future_payload
                snapshot=self.market_data.snapshot(
                    instrument,
                    payload,
                    include_history=False,
                    include_orderbook=False,
                    captured_at=ticker_captured_at,
                )
                if snapshot:
                    ticker_snapshots[instrument.symbol]=snapshot

            prefiltered,prefilter_diagnostics=self.scanner.fast_filter_with_diagnostics(
                self.instruments,
                ticker_snapshots,
                require_history=False,
            )
            position_symbols=set(portfolio.positions)
            position_instruments=[
                instrument for instrument in self.instruments
                if instrument.symbol in position_symbols
            ]
            history_candidates=self.scanner.build_history_candidates(
                prefiltered,
                core_limit=self.config.market_history_candidate_limit,
                exploration_limit=getattr(
                    self.config,"market_exploration_candidate_limit",80
                ),
                exploration_slots_per_family=getattr(
                    self.config,"market_exploration_slots_per_family",2
                ),
                cycle_key=cycle_id,
                preserve_symbols=position_symbols,
            )
            ticker_instruments=[
                instrument for instrument in self.instruments
                if instrument.symbol in ticker_snapshots
            ]
            self.audit.emit(
                "CYCLE_MARKET_PREFILTER",
                "INFO",
                cycle_id=cycle_id,
                ticker_snapshots=len(ticker_snapshots),
                candidates=len(prefiltered),
                history_candidates=len(history_candidates),
                position_candidates=len(position_instruments),
                core_history_limit=self.config.market_history_candidate_limit,
                exploration_candidates=max(
                    0,len(history_candidates)-min(
                        len(prefiltered),self.config.market_history_candidate_limit
                    )-len([
                        instrument for instrument in position_instruments
                        if instrument.symbol not in {
                            item.symbol for item in prefiltered[
                                :self.config.market_history_candidate_limit
                            ]
                        }
                    ])
                ),
                prefilter_excluded_by_reason=prefilter_diagnostics["excluded_by_reason"],
                prefilter_families=prefilter_diagnostics["families"],
            )
            self.audit.emit(
                "UNIVERSE_BREAKDOWN",
                "INFO",
                cycle_id=cycle_id,
                discovered=self.scanner.count_by_family(self.instruments),
                ticker_available=self.scanner.count_by_family(ticker_instruments),
                prefiltered=self.scanner.count_by_family(prefiltered),
                history_candidates=self.scanner.count_by_family(history_candidates),
                held_positions=self.scanner.count_by_family(position_instruments),
            )

            cached=self.db.latest_market_closes([i.symbol for i in history_candidates])
            cache_max_age=float(self.config.market_history_cache_seconds)
            snapshots={}
            feature_map={}
            history_cached=0
            history_fetched=0
            history_insufficient=0

            def hydrate(instrument: Any) -> tuple[Any,Any,str]:
                payload=spot_payload if instrument.venue=="spot" else future_payload
                ticker=ticker_snapshots.get(instrument.symbol)
                if not ticker:
                    return instrument,None,"NO_TICKER"
                cached_row=cached.get(instrument.symbol)
                now=time.time()
                if (
                    cached_row
                    and now-float(cached_row[0]) <= cache_max_age
                    and len(cached_row[1]) >= 30
                ):
                    from dataclasses import replace
                    return instrument,replace(ticker,closes=cached_row[1]),"CACHED"
                return instrument,self.market_data.snapshot(
                    instrument,
                    payload,
                    include_history=True,
                    include_orderbook=False,
                    captured_at=ticker_captured_at,
                ),"FETCHED"

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures=[executor.submit(hydrate,instrument) for instrument in history_candidates]
                for future in as_completed(futures):
                    instrument,snapshot,source=future.result()
                    self._watchdog_heartbeat(cycle_id, stage)
                    if not snapshot or len(snapshot.closes)<30:
                        history_insufficient+=1
                        continue
                    if source=="CACHED":
                        history_cached+=1
                    else:
                        history_fetched+=1
                        self.db.save_market_history_cache(
                            instrument.symbol,
                            time.time(),
                            snapshot.closes,
                        )
                    snapshots[instrument.symbol]=snapshot
                    feature_map[instrument.symbol]=self.features.calculate(snapshot)
                    self.db.save_market(snapshot,feature_map[instrument.symbol])

            ranked=self.scanner.rank(
                [instrument for instrument in history_candidates if instrument.symbol in snapshots],
                snapshots,
                feature_map,
            )
            selected,quote_duplicates_removed=self.scanner.select_for_cycle(
                ranked,
                limit=20,
                preserve_symbols=position_symbols,
                family_slots=getattr(
                    self.config,"market_exploration_slots_per_family",2
                ),
            )
            selected_symbols={instrument.symbol for instrument in selected}
            for instrument in position_instruments:
                if instrument.symbol in snapshots and instrument.symbol not in selected_symbols:
                    selected.append(instrument)
                    selected_symbols.add(instrument.symbol)
            position_evaluated=sum(
                1 for instrument in position_instruments
                if instrument.symbol in selected_symbols
            )
            position_missing=sorted(
                symbol for symbol in position_symbols
                if symbol not in selected_symbols
            )
            self.audit.emit(
                "CYCLE_POSITION_REEVALUATION",
                "INFO",
                cycle_id=cycle_id,
                portfolio_positions=len(position_symbols),
                position_candidates=len(position_instruments),
                evaluated=position_evaluated,
                missing=position_missing,
            )
            self.audit.emit(
                "UNIVERSE_BREAKDOWN",
                "INFO",
                cycle_id=cycle_id,
                history_ready=self.scanner.count_by_family(
                    [i for i in history_candidates if i.symbol in snapshots]
                ),
                selected=self.scanner.count_by_family(selected),
                selected_total=len(selected),
            )

            quote_refresh_started=time.monotonic()
            refreshed=0
            quote_missing=0
            latest_spot_payload=spot_payload
            if selected:
                refreshed_spot,refreshed_future=self.gateway.public_tickers()
                latest_spot_payload=refreshed_spot
                refreshed_at=time.time()
                from dataclasses import replace
                for instrument in selected:
                    payload=refreshed_spot if instrument.venue=="spot" else refreshed_future
                    quote=self.market_data.snapshot(
                        instrument,
                        payload,
                        include_history=False,
                        include_orderbook=False,
                        captured_at=refreshed_at,
                    )
                    if not quote:
                        quote_missing+=1
                        continue
                    snapshots[instrument.symbol]=replace(
                        quote,
                        closes=snapshots[instrument.symbol].closes,
                    )
                    refreshed+=1

            selected=[instrument for instrument in selected if instrument.symbol in snapshots]
            self.audit.emit(
                "CYCLE_MARKET_QUOTES_REFRESHED",
                "INFO",
                cycle_id=cycle_id,
                selected_before_refresh=len(selected),
                refreshed=refreshed,
                missing=quote_missing,
                duration_seconds=round(time.monotonic()-quote_refresh_started,2),
            )

            fx_quotes={}
            for instrument in selected:
                if instrument.venue=="spot":
                    rate=self.portfolio.quote_to_eur_rate(instrument.quote)
                    fx_quotes[instrument.quote]=str(rate) if rate is not None else ""
            self.audit.emit(
                "CYCLE_FX_CONTEXT",
                "INFO",
                cycle_id=cycle_id,
                selected_quotes=fx_quotes,
            )

            def enrich(instrument: Any) -> tuple[Any,Any]:
                return instrument, self.market_data.enrich_orderbook(
                    instrument,
                    snapshots[instrument.symbol],
                )

            with ThreadPoolExecutor(max_workers=8) as orderbook_executor:
                orderbook_futures=[
                    orderbook_executor.submit(enrich,instrument) for instrument in selected
                ]
                for orderbook_future in as_completed(orderbook_futures):
                    instrument,snapshot=orderbook_future.result()
                    self._watchdog_heartbeat(cycle_id, stage)
                    snapshots[instrument.symbol]=snapshot
                    feature_map[instrument.symbol]=self.features.calculate(snapshot)
                    self.db.save_market(snapshot,feature_map[instrument.symbol])

            self.audit.emit(
                "CYCLE_MARKET_DATA",
                "INFO",
                cycle_id=cycle_id,
                instruments=len(self.instruments),
                ticker_snapshots=len(ticker_snapshots),
                candidates=len(prefiltered),
                history_candidates=len(history_candidates),
                history_cached=history_cached,
                history_fetched=history_fetched,
                history_insufficient=history_insufficient,
                snapshots=len(snapshots),
                selected=len(selected),
                duration_seconds=round(time.monotonic()-market_stage_started,2),
            )

            stage="MARKET_SCAN"
            self._watchdog_arm(cycle_id, stage)
            self.audit.emit(
                "CYCLE_MARKET_SCAN",
                "INFO",
                cycle_id=cycle_id,
                fast_candidates=len(snapshots),
                ranked=len(ranked),
                selected=len(selected),
                selected_families=self.scanner.count_by_family(selected),
                quote_duplicates_removed=quote_duplicates_removed,
                prefilter_excluded_by_reason=prefilter_diagnostics["excluded_by_reason"],
            )
            stage="NEWS"
            self._watchdog_arm(cycle_id, stage)
            news_started=time.monotonic()
            news=self.news.collect() if self.config.news_enabled else []
            news_status=getattr(self.news,"last_status",{})
            self.audit.emit(
                "CYCLE_NEWS",
                "INFO",
                cycle_id=cycle_id,
                enabled=self.config.news_enabled,
                count=len(news),
                duration_seconds=round(time.monotonic()-news_started,2),
                **{k:v for k,v in news_status.items() if k!="duration_seconds"},
            )

            stage="GEMINI"
            self._watchdog_arm(cycle_id, stage)
            gemini_started=time.monotonic()
            self.audit.emit(
                "GEMINI_REQUEST_START",
                "INFO",
                cycle_id=cycle_id,
                selected=len(selected),
                configured_models=list(getattr(self.gemini,"models",[])),
                timeout_seconds=int(getattr(self.config,"gemini_timeout_seconds",30)),
            )
            gemini_instruments=[
                instrument for instrument in selected
                if instrument.symbol in position_symbols
            ]
            gemini_instruments += [
                instrument for instrument in selected
                if instrument.symbol not in position_symbols
            ]
            gemini_context=[
                {"symbol":i.symbol,"features":feature_map[i.symbol]}
                for i in gemini_instruments[:10]
            ]
            gemini=self.gemini.analyze(
                [n.__dict__ for n in news[:20]],
                {"markets":gemini_context},
            ) if selected else {"status":"SKIPPED","effect_bps":0}
            gemini_duration=round(time.monotonic()-gemini_started,2)
            gemini_bps=D(str(gemini.get("expected_impact_bps",gemini.get("effect_bps",0)) or 0))
            gemini_status=str(gemini.get("status","UNKNOWN"))
            gemini_model=str(gemini.get("model",""))
            attempted_models=list(gemini.get("attempted_models",[]))
            fallback_used=bool(gemini.get("fallback_used",False))
            gemini_reason=str(gemini.get("reason",""))
            if fallback_used:
                self.audit.emit(
                    "GEMINI_MODEL_FALLBACK",
                    "WARNING",
                    cycle_id=cycle_id,
                    primary_model=attempted_models[0] if attempted_models else "",
                    selected_model=gemini_model,
                    attempted_models=attempted_models,
                )
            if gemini_status=="QUOTA_EXHAUSTED":
                self.audit.emit(
                    "GEMINI_QUOTA_EXHAUSTED",
                    "WARNING",
                    cycle_id=cycle_id,
                    attempted_models=attempted_models,
                    continuation="ZERO_IMPACT_AND_CONTINUE",
                )
            if gemini_status=="TIMEOUT":
                self.audit.emit(
                    "GEMINI_TIMEOUT",
                    "WARNING",
                    cycle_id=cycle_id,
                    duration_seconds=gemini_duration,
                    timeout_seconds=int(getattr(self.config,"gemini_timeout_seconds",30)),
                )
            elif gemini_status in {"UNAVAILABLE","MODEL_UNAVAILABLE","DEGRADED"}:
                self.audit.emit(
                    "GEMINI_UNAVAILABLE",
                    "WARNING",
                    cycle_id=cycle_id,
                    duration_seconds=gemini_duration,
                    reason=gemini_reason,
                )
            self.audit.emit(
                "CYCLE_GEMINI",
                "INFO",
                cycle_id=cycle_id,
                status=gemini_status,
                model=gemini_model,
                attempted_models=attempted_models,
                fallback_used=fallback_used,
                reason=gemini_reason,
                expected_impact_bps=str(gemini_bps),
                duration_seconds=gemini_duration,
            )

            stage="DECISIONS"
            self._watchdog_arm(cycle_id, stage)
            model_version=self.registry.active()
            model_parameters=self.registry.parameters(model_version)
            placed=0
            decisions_count=0
            strategy_rejected=0
            risk_rejected=0
            order_blocked=0
            rebalance_decisions=0
            last_decision=None
            no_action_reasons: dict[str,int]={}
            for instrument in selected:
                snap=snapshots[instrument.symbol]
                f=feature_map[instrument.symbol]
                regime=self.regimes.detect(f)
                news_bps=self.news.effect_for(instrument.symbol,news)
                long_signal,short_signal=self.signals.evaluate(
                    instrument,snap,f,regime,news_bps,gemini_bps
                )
                min_cost_eur=self.portfolio.min_cost_eur(instrument)
                if min_cost_eur is None:
                    reason="FX_RATE_UNAVAILABLE"
                    no_action_reasons[reason]=no_action_reasons.get(reason,0)+1
                    strategy_rejected+=1
                    self.learning.record_cycle(cycle_id,0,0,[f"{instrument.symbol}:{reason}"])
                    self.audit.emit(
                        "CYCLE_DECISION_REJECTED",
                        "WARNING",
                        cycle_id=cycle_id,
                        symbol=instrument.symbol,
                        reason=reason,
                    )
                    continue
                decision=self.decisions.choose(
                    instrument,
                    long_signal,
                    short_signal,
                    portfolio,
                    model_version,
                    self.config_hash,
                    model_parameters,
                    min_cost_eur,
                )
                if not decision:
                    reason=self.decisions.rejection_reason(
                        instrument,
                        long_signal,
                        short_signal,
                        portfolio,
                        model_parameters,
                        min_cost_eur,
                    )
                    no_action_reasons[reason]=no_action_reasons.get(reason,0)+1
                    strategy_rejected+=1
                    self.learning.record_cycle(cycle_id,0,0,[f"{instrument.symbol}:{reason}"])
                    self.audit.emit(
                        "CYCLE_DECISION_REJECTED",
                        "INFO",
                        cycle_id=cycle_id,
                        symbol=instrument.symbol,
                        reason=reason,
                        long_expected_return_bps=str(long_signal.expected_return_bps),
                        long_expected_cost_bps=str(long_signal.expected_cost_bps),
                        long_net_edge_bps=str(long_signal.net_edge_bps),
                        long_confidence=str(long_signal.confidence),
                        short_expected_return_bps=str(short_signal.expected_return_bps),
                        short_expected_cost_bps=str(short_signal.expected_cost_bps),
                        short_net_edge_bps=str(short_signal.net_edge_bps),
                        short_confidence=str(short_signal.confidence),
                        required_edge_bps=str(self.config.strategy_min_edge_bps),
                        required_confidence=str(self.config.strategy_min_confidence),
                    )
                    continue

                decisions_count+=1
                if decision.current_position_eur != 0:
                    rebalance_decisions+=1
                confidence=decision.signal.confidence
                gross_pct=(
                    portfolio.gross_eur/portfolio.equity_eur*100
                    if portfolio.equity_eur else D("999")
                )
                margin_account = (
                    self.portfolio.futures_margin_account
                    if instrument.product_type.value == "DERIVATIVE"
                    else self.portfolio.spot_margin_account
                )
                margin_level_pct = (
                    D(str(margin_account.get("margin_level_pct") or 0))
                    if isinstance(margin_account, dict)
                    else None
                )
                execution_direction = decision.execution_direction or decision.signal.direction
                require_margin = (
                    instrument.product_type.value == "SPOT_MARGIN"
                    and execution_direction.value == "SHORT"
                    and decision.current_position_eur == 0
                )
                lev=self.leverage.choose(
                    instrument,
                    f,
                    confidence,
                    gross_pct,
                    margin_level_pct,
                    self.config.risk_max_leverage,
                    require_margin=require_margin,
                )
                if require_margin and lev < D("2"):
                    reason="SPOT_MARGIN_SHORT_NOT_ELIGIBLE"
                    blockers.append(f"{instrument.symbol}:{reason}")
                    no_action_reasons[reason]=no_action_reasons.get(reason,0)+1
                    self.audit.emit(
                        "CYCLE_ORDER_BLOCKED",
                        "WARNING",
                        cycle_id=cycle_id,
                        symbol=instrument.symbol,
                        reason=reason,
                        detail={
                            "max_supported_leverage": str(instrument.max_leverage),
                            "configured_max_leverage": str(self.config.risk_max_leverage),
                            "margin_level_pct": str(margin_level_pct or 0),
                        },
                    )
                    continue
                decision=__import__("dataclasses").replace(decision,leverage=lev)

                # If the quote wallet is short, the dependent trade has an
                # additional EUR/USD conversion cost. Include that cost in the
                # edge before risk evaluates the entry threshold.
                fx_needed = False
                if (
                    instrument.venue == "spot"
                    and execution_direction.value == "LONG"
                    and decision.target_position_eur > decision.current_position_eur
                ):
                    provisional_qty = self.portfolio.quantity_for_eur(
                        instrument, decision.target_notional_eur, snap.price
                    )
                    if provisional_qty is not None:
                        provisional_quote = provisional_qty * snap.price
                        fx_needed = (
                            self.portfolio.cash_balance(instrument.quote)
                            + D("0.00000001") < provisional_quote
                        )
                if fx_needed:
                    fx_cost_bps = D(str(getattr(
                        self.config, "execution_fx_cost_bps", "40"
                    )))
                    signal = __import__("dataclasses").replace(
                        decision.signal,
                        expected_cost_bps=(
                            decision.signal.expected_cost_bps + fx_cost_bps
                        ),
                    )
                    rationale = dict(decision.rationale)
                    rationale["fx_funding_required"] = True
                    rationale["fx_cost_bps"] = str(fx_cost_bps)
                    decision=__import__("dataclasses").replace(
                        decision, signal=signal, rationale=rationale
                    )
                    self.audit.emit(
                        "CYCLE_FX_COST_INCLUDED",
                        "INFO",
                        cycle_id=cycle_id,
                        symbol=instrument.symbol,
                        fx_cost_bps=str(fx_cost_bps),
                    )

                risk=self.risk.evaluate(
                    decision,
                    portfolio,
                    snap,
                    margin_account,
                )
                self.db.save_decision(decision)
                prediction_id=new_id("prediction")
                self.db.save_prediction(
                    prediction_id,
                    decision,
                    float(decision.signal.confidence),
                )
                self.audit.emit(
                    "PREDICTION_CREATED",
                    "INFO",
                    cycle_id=cycle_id,
                    prediction_id=prediction_id,
                    decision_id=decision.decision_id,
                    symbol=decision.instrument.symbol,
                    horizon="15m",
                    probability=str(decision.signal.confidence),
                    model_version=decision.model_version,
                )
                last_decision=decision
                self.audit.emit(
                    "CYCLE_RISK_DECISION",
                    "INFO",
                    cycle_id=cycle_id,
                    symbol=instrument.symbol,
                    allowed=risk.allowed,
                    reason=risk.reason,
                    checks=risk.checks,
                    current_position_eur=str(decision.current_position_eur),
                    target_position_eur=str(decision.target_position_eur),
                    trade_notional_eur=str(decision.target_notional_eur),
                    leverage=str(decision.leverage),
                    execution_direction=(
                        decision.execution_direction.value
                        if decision.execution_direction else ""
                    ),
                    reduce_only=decision.reduce_only,
                )
                if not risk.allowed:
                    risk_rejected+=1
                    blockers.append(f"{instrument.symbol}:{risk.reason}")
                    self.db.learning_event(
                        "BLOCKER",
                        decision.decision_id,
                        {"reason":risk.reason,"checks":risk.checks},
                    )
                    continue

                quantity=self.portfolio.quantity_for_eur(
                    instrument,
                    decision.target_notional_eur,
                    snap.price,
                )
                if quantity is None or quantity <= 0:
                    reason="FX_RATE_UNAVAILABLE"
                    blockers.append(f"{instrument.symbol}:{reason}")
                    self.db.learning_event(
                        "BLOCKER",
                        decision.decision_id,
                        {"reason":reason},
                    )
                    self.audit.emit(
                        "CYCLE_RISK_DECISION",
                        "WARNING",
                        cycle_id=cycle_id,
                        symbol=instrument.symbol,
                        allowed=False,
                        reason=reason,
                        checks={"quote_to_eur":False},
                    )
                    continue
                if (
                    instrument.venue == "spot"
                    and execution_direction.value == "LONG"
                    and decision.target_position_eur > decision.current_position_eur
                    and lev <= D("1")
                ):
                    required_quote = quantity * snap.price
                    available_quote = self.portfolio.cash_balance(instrument.quote)
                    if available_quote + D("0.00000001") < required_quote:
                        fx_result = self.fx.ensure_quote_funds(
                            quote=instrument.quote,
                            required_quote=required_quote,
                            cycle_id=cycle_id,
                            source_preference="EUR",
                        )
                        if not fx_result["ready"]:
                            reason=str(fx_result["reason"])
                            blockers.append(f"{instrument.symbol}:{reason}")
                            no_action_reasons[reason]=no_action_reasons.get(reason,0)+1
                            continue
                        self.audit.emit(
                            "CYCLE_FX_FUNDING_READY",
                            "INFO",
                            cycle_id=cycle_id,
                            symbol=instrument.symbol,
                            quote_asset=str(instrument.quote),
                            required_quote=str(required_quote),
                            conversion=fx_result,
                        )
                        # Refresh the portfolio after the confirmed FX fill.
                        self.portfolio.set_market_context(self.instruments,latest_spot_payload)
                        self.portfolio.reconcile()
                        available_quote = self.portfolio.cash_balance(instrument.quote)
                        if available_quote + D("0.00000001") < required_quote:
                            reason="FX_FUNDING_INSUFFICIENT_AFTER_FILL"
                            blockers.append(f"{instrument.symbol}:{reason}")
                            no_action_reasons[reason]=no_action_reasons.get(reason,0)+1
                            self.audit.emit(
                                "CYCLE_ORDER_BLOCKED",
                                "ERROR",
                                cycle_id=cycle_id,
                                symbol=instrument.symbol,
                                reason=reason,
                                detail={
                                    "required_quote":str(required_quote),
                                    "available_quote":str(available_quote),
                                    "fx":fx_result,
                                },
                            )
                            continue
                method=self.authority.policy.choose(
                    snap.spread_bps,
                    decision.signal.net_edge_bps,
                    f.get("volatility",D("999")),
                )
                execution_direction=decision.execution_direction or decision.signal.direction
                price=snap.ask if execution_direction.value=="LONG" else snap.bid
                intent=self.intents.build(
                    decision,
                    lev,
                    method["order_type"],
                    quantity,
                    price,
                    reduce_only=decision.reduce_only,
                    post_only=bool(method.get("post_only", False)),
                )
                result=self.authority.submit(intent,snap)
                self._watchdog_heartbeat(cycle_id, stage)
                self.learning.record_order_outcome(intent.client_order_id,result)
                if result.get("state") in {
                    "ACKNOWLEDGED","LIVE","PARTIALLY_FILLED","FILLED"
                }:
                    placed+=1
                else:
                    order_blocked+=1
                    gate_reason=str(
                        result.get("reason")
                        or result.get("state")
                        or "ORDER_BLOCKED"
                    )
                    no_action_reasons[gate_reason]=no_action_reasons.get(gate_reason,0)+1
                    blockers.append(f"{instrument.symbol}:{gate_reason}")
                    self.audit.emit(
                        "CYCLE_ORDER_BLOCKED",
                        "WARNING",
                        cycle_id=cycle_id,
                        symbol=instrument.symbol,
                        reason=gate_reason,
                        state=str(result.get("state","")),
                    )

            self.audit.emit(
                "CYCLE_DECISIONS",
                "INFO",
                cycle_id=cycle_id,
                decisions=decisions_count,
                strategy_rejected=strategy_rejected,
                rebalance_decisions=rebalance_decisions,
                risk_rejected=risk_rejected,
                order_blocked=order_blocked,
                orders=placed,
                blockers=len(blockers),
                no_action_reasons=no_action_reasons,
            )
            stage="PORTFOLIO_FINAL"
            self._watchdog_arm(cycle_id, stage)
            self.portfolio.set_market_context(self.instruments,latest_spot_payload)
            final_portfolio=self.portfolio.reconcile()
            self.db.save_portfolio(cycle_id,final_portfolio)
            self.learning.record_cycle(cycle_id,decisions_count,placed,blockers)
            try:
                feedback=self.learning.process_feedback()
                self._learning_summary=feedback
                self.audit.emit("LEARNING_POST_CYCLE","INFO",cycle_id=cycle_id,**feedback)
            except Exception as exc:
                self.audit.emit("LEARNING_POST_CYCLE_FAILED","WARNING",cycle_id=cycle_id,error=f"{type(exc).__name__}:{str(exc)[:500]}")
            self.db.finish_cycle(cycle_id,"COMPLETED",";".join(blockers[:5]))
            self._watchdog_clear(cycle_id)
            self.state.selected_symbol=last_decision.instrument.symbol if last_decision else ""
            self.state.last_edge_bps=str(last_decision.signal.net_edge_bps if last_decision else 0)
            self.state.last_confidence=str(last_decision.signal.confidence if last_decision else 0)
            self.state.set(RuntimeStage.READY,blockers[0] if blockers else "")
            self._publish(final_portfolio,gemini,model_version)
            self.audit.emit(
                "CYCLE_COMPLETED",
                "INFO",
                cycle_id=cycle_id,
                selected=len(selected),
                orders=placed,
                blockers=blockers[:5],
            )
            return {"cycle_id":cycle_id,"status":"COMPLETED","placed":placed,"selected":len(selected),"blockers":blockers}
        except Exception as exc:
            detail=f"{stage}:{type(exc).__name__}:{str(exc)[:800]}"
            self._watchdog_clear(cycle_id)
            self.audit.emit("CYCLE_FAILED","ERROR",cycle_id=cycle_id,stage=stage,error=f"{type(exc).__name__}:{str(exc)[:800]}",traceback=__import__("traceback").format_exc()[:3500])
            self.recovery.issue("RUNTIME_CYCLE_FAILURE",detail)
            if cycle_started:
                try:
                    self.db.finish_cycle(cycle_id,"FAILED",detail)
                except Exception as finish_exc:
                    self.audit.emit(
                        "CYCLE_FINISH_FAILED","WARNING",
                        cycle_id=cycle_id,error=f"{type(finish_exc).__name__}:{str(finish_exc)[:400]}",
                    )
            self.state.set(RuntimeStage.SAFE_MODE if self.recovery.breaker.active else RuntimeStage.DEGRADED,self.recovery.breaker.reason if self.recovery.breaker.active else detail)
            self._publish_runtime_status()
            return {"cycle_id":cycle_id,"status":"FAILED","error":type(exc).__name__,"stage":stage}

    def _watchdog_arm(self, cycle_id: str, stage: str) -> None:
        if not hasattr(self, "watchdog"):
            self.watchdog=RuntimeWatchdog(self._handle_watchdog_timeout)
        timeout = self.STEP_TIMEOUTS.get(stage, 120.0)
        previous = self.watchdog.snapshot()
        if previous is not None:
            self.audit.emit(
                "RUNTIME_STEP_COMPLETED", "INFO",
                cycle_id=previous.cycle_id,
                stage=previous.stage,
                duration_seconds=round(time.monotonic()-previous.armed_at, 3),
            )
        self.watchdog.arm(cycle_id, stage, timeout)
        self.audit.emit(
            "RUNTIME_STEP_START", "INFO",
            cycle_id=cycle_id,
            stage=stage,
            timeout_seconds=timeout,
        )

    def _watchdog_heartbeat(self, cycle_id: str, stage: str) -> None:
        self.watchdog.heartbeat(cycle_id, stage)

    def _watchdog_clear(self, cycle_id: str) -> None:
        previous = self.watchdog.snapshot()
        if previous is not None and previous.cycle_id == cycle_id:
            self.audit.emit(
                "RUNTIME_STEP_COMPLETED", "INFO",
                cycle_id=previous.cycle_id,
                stage=previous.stage,
                duration_seconds=round(time.monotonic()-previous.armed_at, 3),
            )
        self.watchdog.clear(cycle_id)

    def _handle_watchdog_timeout(self, snapshot: WatchdogSnapshot) -> None:
        payload = {
            "ts": time.time(),
            "code": "RUNTIME_WATCHDOG_TIMEOUT",
            "level": "ERROR",
            "payload": {
                "cycle_id": snapshot.cycle_id,
                "stage": snapshot.stage,
                "timeout_seconds": snapshot.timeout_seconds,
                "silence_seconds": round(time.monotonic()-snapshot.heartbeat_at, 3),
                "operation": self._startup_instrument_operation if snapshot.stage == "STARTUP_INSTRUMENTS" else "",

            },
        }
        try:
            os.write(
                2,
                (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            )
        except OSError:
            os._exit(70)
        try:
            self.recovery.breaker.trip(
                "RUNTIME_WATCHDOG_TIMEOUT",
                f"{snapshot.stage}:{snapshot.cycle_id}",
            )
            self.state.set(
                RuntimeStage.SAFE_MODE,
                f"RUNTIME_WATCHDOG_TIMEOUT:{snapshot.stage}",
            )
        except Exception as exc:
            self._watchdog_fail_safe_error=type(exc).__name__
        os._exit(70)

    def _recover_stale_cycles(self) -> None:
        threshold = max(
            900,
            int(getattr(self.config, "market_scan_interval_seconds", 300)) * 3,
        )
        cutoff = time.time() - threshold
        rows = self.db.query(
            "SELECT cycle_id,started_at FROM cycles WHERE status='RUNNING' AND started_at<?",
            (cutoff,),
        )
        for row in rows:
            cycle_id = str(row.get("cycle_id", ""))
            self.db.finish_cycle(
                cycle_id,
                "FAILED",
                "PROCESS_RESTART_OR_WATCHDOG",
            )
            self.audit.emit(
                "STALE_CYCLE_RECOVERED", "WARNING",
                cycle_id=cycle_id,
                age_seconds=round(
                    time.time()-float(row.get("started_at", time.time())),
                    1,
                ),
                reason="PROCESS_RESTART_OR_WATCHDOG",
            )

    def _publish_runtime_status(self) -> None:
        try:
            publish_result = self.sensors.publish(self.sensors.states(
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
                portfolio_symbols=[],
                learning_samples=int(getattr(self,"_learning_summary",{}).get("samples",0)),
                learning_open_predictions=int(getattr(self,"_learning_summary",{}).get("open_predictions",0)),
                learning_settled_total=int(getattr(self,"_learning_summary",{}).get("settled_total",0)),
                learning_brier=getattr(self,"_learning_summary",{}).get("brier",0),
                learning_improvement=getattr(self,"_learning_summary",{}).get("improvement",0),
                news_status="UNKNOWN",
                gemini_status="UNKNOWN",
                model_version="UNKNOWN",
                breaker_active=self.recovery.breaker.active,
                tax_status="UNKNOWN",
                tax_estimated_27_5_eur="0",
                tax_incomplete_events=0,
                tax_year=datetime.now(timezone.utc).year,
            ))
            self.audit.emit("HA_SENSOR_PUBLISH_RESULT","INFO",**publish_result)
        except Exception as exc:
            self.audit.emit("HA_SENSOR_PUBLISH_FAILED","WARNING",
                            error=f"{type(exc).__name__}:{str(exc)[:500]}")

    def _update_tax_report(self, force: bool = False) -> None:
        if not self.config.tax_enabled or not self.config.tax_report_enabled:
            self._tax_status="DISABLED"
            self._tax_error=""
            return
        now=time.time()
        if not force and now-self._last_tax_sync < 3600:
            return
        stage="tax_start"
        try:
            self.audit.emit("TAX_REPORT_START","INFO",forced=force)
            stage="tax_history_sync"
            added=self.tax.sync_kraken_spot_history(self.gateway)
            self.audit.emit("TAX_HISTORY_SYNCED","INFO",events_added=added)
            current_year=datetime.now(timezone.utc).year
            statuses=[]
            for year in {current_year,current_year-1}:
                if year<2021:
                    continue
                stage=f"tax_report_{year}"
                paths=self.tax.write_report(year)
                report=self.tax.build_report(year)
                self.db.record_tax_report(year,report,paths)
                status=str(report.get("summary",{}).get("status","UNKNOWN"))
                statuses.append(status)
                self.audit.emit(
                    "TAX_REPORT_UPDATED",
                    "INFO",
                    tax_year=year,
                    events_added=added,
                    status=status,
                    report_json=paths.get("json",""),
                    report_csv=paths.get("csv",""),
                    report_markdown=paths.get("markdown",""),
                )
            self._last_tax_sync=now
            self._tax_status="INCOMPLETE_DATA" if "INCOMPLETE_DATA" in statuses else "READY_FOR_REVIEW"
            self._tax_error=""
            self.audit.emit("TAX_REPORT_READY","INFO",status=self._tax_status)
        except Exception as exc:
            self._tax_status="ERROR"
            self._tax_error=f"{stage}:{type(exc).__name__}:{str(exc)[:800]}"
            self.audit.emit(
                "TAX_REPORT_FAILED",
                "WARNING",
                stage=stage,
                error=f"{type(exc).__name__}:{str(exc)[:800]}",
                traceback=__import__("traceback").format_exc()[:3500],
            )

    def _safe_tax_summary(self) -> dict[str,Any]:
        if not self.config.tax_enabled or not self.config.tax_report_enabled:
            return {}
        try:
            report=self.tax.build_report(datetime.now(timezone.utc).year)
            summary=report.get("summary",{})
            return summary if isinstance(summary,dict) else {}
        except Exception as exc:
            self.audit.emit(
                "TAX_REPORT_READ_FAILED",
                "WARNING",
                error=f"{type(exc).__name__}:{str(exc)[:500]}",
            )
            return {
                "status":self._tax_status or "ERROR",
                "indicative_crypto_27_5_tax_eur":"0",
                "incomplete_event_count":0,
            }

    def _publish(self, portfolio: Any, gemini: dict[str,Any], model_version: str) -> None:
        tax_summary=self._safe_tax_summary()
        self.sensors.publish(self.sensors.states(
            status=self.state.stage.value,stage=self.state.stage.value,cycle_id=self.state.cycle_id,
            blocker=self.state.blocker,symbol=self.state.selected_symbol,
            edge_bps=self.state.last_edge_bps,confidence=self.state.last_confidence,
            leverage="1",equity_eur=portfolio.equity_eur,gross_eur=portfolio.gross_eur,
            net_eur=portfolio.net_eur,margin_used_eur=portfolio.margin_used_eur,
            daily_pnl_eur=portfolio.daily_pnl_eur,drawdown_pct=portfolio.drawdown_pct,
            open_positions=len(portfolio.positions),
            portfolio_symbols=sorted(portfolio.positions),
            learning_samples=int(self._learning_summary.get("samples",0)),
            learning_open_predictions=int(self._learning_summary.get("open_predictions",0)),
            learning_settled_total=int(self._learning_summary.get("settled_total",0)),
            learning_brier=self._learning_summary.get("brier",0),
            learning_improvement=self._learning_summary.get("improvement",0),
            news_status="OK" if self.config.news_enabled else "DISABLED",
            gemini_status=str(gemini.get("status","UNKNOWN")),model_version=model_version,
            breaker_active=self.recovery.breaker.active,
            tax_status=str(tax_summary.get("status","DISABLED")),
            tax_estimated_27_5_eur=tax_summary.get("indicative_crypto_27_5_tax_eur","0"),
            tax_incomplete_events=int(tax_summary.get("incomplete_event_count",0)),
            tax_year=datetime.now(timezone.utc).year,
        ))
