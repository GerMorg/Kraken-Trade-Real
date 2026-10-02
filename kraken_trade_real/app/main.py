from __future__ import annotations

import time

from app.config import Config
from app.execution import CostModel, ExecutionPolicy, ExecutionReconciler
from app.gemini import GeminiAnalyzer
from app.kraken import KrakenGateway, InstrumentDiscovery
from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine
from app.market import FeatureEngine, MarketData, MarketScanner, RegimeEngine
from app.monitoring import AuditLogger
from app.news import NewsEngine
from app.persistence import Database
from app.portfolio import PortfolioReconciler, RiskSizer
from app.recovery import CircuitBreaker, RecoveryManager
from app.risk import LeverageEngine, MarginEngine, RiskEngine
from app.sensors import SensorPublisher
from app.trading import DecisionEngine, OrderIntentBuilder, SignalEngine
from app.trading.authority import TradingAuthority
from app.runtime import TradingRuntime


def build_runtime() -> TradingRuntime:
    config=Config.load()
    db=Database()
    audit=AuditLogger(config.log_file_enabled)
    gateway=KrakenGateway(config.api_key,config.api_secret)
    discovery=InstrumentDiscovery(gateway)
    market_data=MarketData(gateway)
    features=FeatureEngine()
    regimes=RegimeEngine()
    scanner=MarketScanner(config.market_min_liquidity_eur,config.market_max_spread_bps,config.market_max_data_age_seconds)
    news=NewsEngine(db,config.news_refresh_minutes)
    gemini=GeminiAnalyzer(config.gemini_api_key,config.gemini_model,config.gemini_enabled,db)
    signals=SignalEngine()
    decisions=DecisionEngine(config)
    sizer=RiskSizer(config.risk_max_position_pct,config.risk_cash_reserve_pct)
    cost_model=CostModel()
    margin=MarginEngine()
    leverage=LeverageEngine()
    risk=RiskEngine(config,margin,leverage,cost_model)
    policy=ExecutionPolicy(config.execution_max_slippage_bps,config.execution_max_reprices)
    authority=TradingAuthority(config,gateway,db,audit,policy,ExecutionReconciler())
    portfolio=PortfolioReconciler(gateway,db)
    breaker=CircuitBreaker()
    recovery=RecoveryManager(db,audit,breaker)
    calibration=CalibrationEngine()
    registry=ModelRegistry(db)
    research=ResearchEngine(db)
    learning=LearningEngine(db,calibration,registry,research)
    sensors=SensorPublisher(config.sensors_enabled,__import__("os").getenv("SUPERVISOR_TOKEN"))
    return TradingRuntime(
        config,db,audit,gateway,discovery,market_data,features,regimes,scanner,news,gemini,
        signals,decisions,sizer,risk,leverage, __import__("app.trading",fromlist=["OrderIntentBuilder"]).OrderIntentBuilder(
            config.execution_max_slippage_bps,config.execution_order_timeout_seconds
        ), authority,portfolio,recovery,learning,registry,sensors
    )


def main() -> None:
    runtime=build_runtime()
    runtime.startup()
    while True:
        runtime.run_cycle()
        time.sleep(runtime.config.market_scan_interval_seconds)


if __name__=="__main__":
    main()
