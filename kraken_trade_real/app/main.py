from __future__ import annotations

import os
import time

from app.config import Config
from app.execution import CostModel, ExecutionPolicy, ExecutionReconciler
from app.gemini import GeminiAnalyzer
from app.kraken import InstrumentDiscovery, KrakenGateway, WebSocketSupervisor
from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine
from app.market import FeatureEngine, MarketData, MarketScanner, RegimeEngine
from app.monitoring import AuditLogger
from app.news import NewsEngine
from app.persistence import Database
from app.portfolio import PortfolioReconciler, RiskSizer
from app.recovery import CircuitBreaker, RecoveryManager
from app.risk import LeverageEngine, MarginEngine, RiskEngine
from app.sensors import SensorPublisher
from app.tax import AustrianTaxLedger
from app.trading import DecisionEngine, OrderIntentBuilder, SignalEngine, TacticalTrader
from app.trading.authority import TradingAuthority
from app.runtime import TradingRuntime


def build_runtime() -> TradingRuntime:
    config = Config.load()
    db = Database()
    audit = AuditLogger(config.log_file_enabled)
    gateway = KrakenGateway(
        config.api_key,
        config.api_secret,
        futures_enabled=config.futures_enabled,
        futures_api_key=config.futures_api_key,
        futures_api_secret=config.futures_api_secret,
        tokenized_assets_enabled=config.tokenized_assets_enabled,
    )
    discovery = InstrumentDiscovery(gateway)
    market_data = MarketData(gateway)
    features = FeatureEngine()
    regimes = RegimeEngine()
    scanner = MarketScanner(
        config.market_min_liquidity_eur,
        config.market_max_spread_bps,
        config.market_max_data_age_seconds,
    )
    news = NewsEngine(db, config.news_refresh_minutes, config.news_source_timeout_seconds)
    gemini = GeminiAnalyzer(
        config.gemini_api_key,
        config.gemini_model,
        config.gemini_enabled,
        db,
        config.gemini_timeout_seconds,
        config.gemini_fallback_models,
    )
    signals = SignalEngine(config)
    decisions = DecisionEngine(config)
    sizer = RiskSizer(config.risk_max_position_pct, config.risk_cash_reserve_pct)
    cost_model = CostModel()
    margin = MarginEngine()
    leverage = LeverageEngine()
    risk = RiskEngine(config, margin, leverage, cost_model)
    policy = ExecutionPolicy(config.execution_max_slippage_bps, config.execution_max_reprices)
    authority = TradingAuthority(
        config, gateway, db, audit, policy, ExecutionReconciler()
    )
    portfolio = PortfolioReconciler(gateway, db)
    breaker = CircuitBreaker()
    recovery = RecoveryManager(db, audit, breaker)
    calibration = CalibrationEngine()
    registry = ModelRegistry(db)
    research = ResearchEngine(db)
    learning = LearningEngine(db, calibration, registry, research)
    sensors = SensorPublisher(config.sensors_enabled, os.getenv("SUPERVISOR_TOKEN"))
    tax = AustrianTaxLedger(
        db,
        report_dir="/config/reports/tax",
        provider_tax_classification=config.tax_provider_classification,
    )
    websocket = WebSocketSupervisor(audit, recovery)
    intents = OrderIntentBuilder(
        config.execution_max_slippage_bps,
        config.execution_order_timeout_seconds,
    )
    tactical = TacticalTrader(
        config=config,
        db=db,
        audit=audit,
        gateway=gateway,
        websocket=websocket,
        authority=authority,
        portfolio=portfolio,
        intents=intents,
        risk=risk,
    )
    return TradingRuntime(
        config, db, audit, gateway, discovery, market_data, features, regimes,
        scanner, news, gemini, signals, decisions, sizer, risk, leverage,
        intents, authority, portfolio, recovery, learning, registry, sensors, tax,
        websocket=websocket,
        tactical=tactical,
    )


def main() -> None:
    runtime = build_runtime()
    runtime.startup()
    next_cycle_at = time.monotonic()
    while True:
        try:
            result = runtime.run_cycle()
            runtime.audit.emit(
                "MAIN_LOOP_CYCLE_RESULT",
                "INFO",
                status=result.get("status","UNKNOWN"),
                cycle_id=result.get("cycle_id",""),
            )
        except Exception as exc:
            runtime.audit.emit(
                "MAIN_LOOP_EXCEPTION",
                "ERROR",
                error=f"{type(exc).__name__}:{str(exc)[:800]}",
                traceback=__import__("traceback").format_exc()[:3500],
            )
        interval = max(1, runtime.config.market_scan_interval_seconds)
        next_cycle_at += interval
        sleep_for = next_cycle_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            runtime.audit.emit(
                "MAIN_LOOP_INTERVAL_OVERRUN",
                "WARNING",
                interval_seconds=interval,
                overdue_seconds=round(-sleep_for, 2),
            )
            next_cycle_at = time.monotonic()


if __name__ == "__main__":
    main()
