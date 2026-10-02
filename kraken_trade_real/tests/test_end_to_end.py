from pathlib import Path

from app.execution import CostModel, ExecutionPolicy, ExecutionReconciler
from app.gemini import GeminiAnalyzer
from app.learning import CalibrationEngine, LearningEngine, ModelRegistry, ResearchEngine
from app.market import FeatureEngine, MarketData, MarketScanner, RegimeEngine
from app.monitoring import AuditLogger
from app.news import NewsEngine
from app.portfolio import PortfolioReconciler, RiskSizer
from app.recovery import CircuitBreaker, RecoveryManager
from app.risk import LeverageEngine, MarginEngine, RiskEngine
from app.sensors import SensorPublisher
from app.tax import AustrianTaxLedger
from app.trading import DecisionEngine, OrderIntentBuilder, SignalEngine
from app.trading.authority import TradingAuthority
from app.runtime import TradingRuntime


def test_full_runtime_smoke(config, db, fake_gateway):
    audit = AuditLogger(False)
    discovery = __import__(
        "app.kraken.discovery", fromlist=["InstrumentDiscovery"]
    ).InstrumentDiscovery(fake_gateway)
    market = MarketData(fake_gateway)
    features = FeatureEngine()
    regimes = RegimeEngine()
    scanner = MarketScanner(
        config.market_min_liquidity_eur,
        config.market_max_spread_bps,
        config.market_max_data_age_seconds,
    )
    news = NewsEngine(db, 10)
    gemini = GeminiAnalyzer("", config.gemini_model, False, db)
    signals = SignalEngine()
    decisions = DecisionEngine(config)
    margin = MarginEngine()
    leverage = LeverageEngine()
    risk = RiskEngine(config, margin, leverage, CostModel())
    authority = TradingAuthority(
        config,
        fake_gateway,
        db,
        audit,
        ExecutionPolicy(config.execution_max_slippage_bps, 2),
        ExecutionReconciler(),
    )
    portfolio = PortfolioReconciler(fake_gateway, db)
    breaker = CircuitBreaker()
    recovery = RecoveryManager(db, audit, breaker)
    registry = ModelRegistry(db)
    learning = LearningEngine(
        db, CalibrationEngine(), registry, ResearchEngine(db)
    )
    sensors = SensorPublisher(False, None)
    tax = AustrianTaxLedger(
        db,
        report_dir=str(Path(db.path).parent / "reports"),
        provider_tax_classification=config.tax_provider_classification,
    )
    intent = OrderIntentBuilder(
        config.execution_max_slippage_bps,
        config.execution_order_timeout_seconds,
    )
    runtime = TradingRuntime(
        config,
        db,
        audit,
        fake_gateway,
        discovery,
        market,
        features,
        regimes,
        scanner,
        news,
        gemini,
        signals,
        decisions,
        RiskSizer(config.risk_max_position_pct, config.risk_cash_reserve_pct),
        risk,
        leverage,
        intent,
        authority,
        portfolio,
        recovery,
        learning,
        registry,
        sensors,
        tax,
    )
    assert runtime.startup()
    result = runtime.run_cycle()
    assert result["status"] in {"COMPLETED", "FAILED"}
    assert fake_gateway.orders == []
