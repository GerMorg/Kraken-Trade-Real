from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


ZERO = Decimal("0")


class ProductType(StrEnum):
    SPOT = "spot"
    SPOT_MARGIN = "spot_margin"
    FUTURES = "futures"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class DecisionState(StrEnum):
    NO_POSITION = "NO_POSITION"
    OPEN_LONG = "OPEN_LONG"
    INCREASE_LONG = "INCREASE_LONG"
    REDUCE_LONG = "REDUCE_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    INCREASE_SHORT = "INCREASE_SHORT"
    REDUCE_SHORT = "REDUCE_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    REVERSE_LONG_TO_SHORT = "REVERSE_LONG_TO_SHORT"
    REVERSE_SHORT_TO_LONG = "REVERSE_SHORT_TO_LONG"


class OrderState(StrEnum):
    INTENT_CREATED = "INTENT_CREATED"
    PRECHECK_PASSED = "PRECHECK_PASSED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    LIVE = "LIVE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN_RECONCILING = "UNKNOWN_RECONCILING"


class Regime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    LIQUIDITY_STRESS = "LIQUIDITY_STRESS"
    PANIC = "PANIC"
    RECOVERY = "RECOVERY"
    BREAKOUT = "BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"
    UNKNOWN = "UNKNOWN"


class Blocker(StrEnum):
    NONE = "NONE"
    BLOCKED_CONFIG = "BLOCKED_CONFIG"
    BLOCKED_API_PERMISSIONS = "BLOCKED_API_PERMISSIONS"
    BLOCKED_KRAKEN_STATUS = "BLOCKED_KRAKEN_STATUS"
    BLOCKED_INSTRUMENT = "BLOCKED_INSTRUMENT"
    BLOCKED_MARKET_DATA = "BLOCKED_MARKET_DATA"
    BLOCKED_PRIVATE_DATA = "BLOCKED_PRIVATE_DATA"
    BLOCKED_HISTORY = "BLOCKED_HISTORY"
    BLOCKED_NEWS = "BLOCKED_NEWS"
    BLOCKED_GEMINI = "BLOCKED_GEMINI"
    BLOCKED_STRATEGY = "BLOCKED_STRATEGY"
    BLOCKED_EXPECTED_EDGE = "BLOCKED_EXPECTED_EDGE"
    BLOCKED_COST = "BLOCKED_COST"
    BLOCKED_LIQUIDITY = "BLOCKED_LIQUIDITY"
    BLOCKED_MARGIN = "BLOCKED_MARGIN"
    BLOCKED_LEVERAGE = "BLOCKED_LEVERAGE"
    BLOCKED_RISK = "BLOCKED_RISK"
    BLOCKED_POSITION_SIZE = "BLOCKED_POSITION_SIZE"
    BLOCKED_PORTFOLIO = "BLOCKED_PORTFOLIO"
    BLOCKED_ORDER_LIMIT = "BLOCKED_ORDER_LIMIT"
    BLOCKED_RECONCILIATION = "BLOCKED_RECONCILIATION"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    SAFE_STOP = "SAFE_STOP"


class ErrorCode(StrEnum):
    DATA_ERROR = "DATA_ERROR"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    PRIVATE_DATA_STALE = "PRIVATE_DATA_STALE"
    API_ERROR = "API_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    ORDER_REJECTED = "ORDER_REJECTED"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_VOLUME = "INVALID_VOLUME"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    LIQUIDATION_RISK = "LIQUIDATION_RISK"
    STALE_DECISION = "STALE_DECISION"
    DUPLICATE_ORDER_RISK = "DUPLICATE_ORDER_RISK"
    NETWORK_AMBIGUITY = "NETWORK_AMBIGUITY"
    MODEL_ERROR = "MODEL_ERROR"
    BAD_SIGNAL = "BAD_SIGNAL"
    BAD_EXIT = "BAD_EXIT"
    EXCESSIVE_SLIPPAGE = "EXCESSIVE_SLIPPAGE"
    EXCESSIVE_SPREAD = "EXCESSIVE_SPREAD"
    NEWS_MISINTERPRETATION = "NEWS_MISINTERPRETATION"
    GEMINI_MISINTERPRETATION = "GEMINI_MISINTERPRETATION"
    REGIME_MISCLASSIFICATION = "REGIME_MISCLASSIFICATION"


@dataclass(frozen=True)
class SafetyLimits:
    max_position_risk: Decimal
    max_gross_exposure: Decimal
    max_net_exposure: Decimal
    max_margin: Decimal
    max_leverage: Decimal
    max_positions: int
    daily_loss_limit: Decimal
    max_drawdown: Decimal
    cash_reserve: Decimal
    max_slippage: Decimal
    max_orders_per_day: int


@dataclass(frozen=True)
class Instrument:
    venue: str
    product_type: ProductType
    symbol: str
    instrument_id: str
    altname: str
    base: str
    quote: str
    contract_type: str | None
    status: str
    margin: bool
    long: bool
    short: bool
    leverage_levels: tuple[Decimal, ...]
    max_leverage: Decimal
    order_min: Decimal
    cost_min: Decimal
    lot_precision: int
    price_precision: int
    tick_size: Decimal
    position_limit_long: Decimal | None = None
    position_limit_short: Decimal | None = None
    margin_class: str | None = None
    collateral: str | None = None
    funding_rate: Decimal | None = None
    fee_rate: Decimal = Decimal("0.0026")
    updated_at: float = 0.0


@dataclass(frozen=True)
class OrderBook:
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    timestamp: float

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0][0] if self.bids else ZERO

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0][0] if self.asks else ZERO


@dataclass(frozen=True)
class MarketSnapshot:
    instrument: Instrument
    last: Decimal
    bid: Decimal
    ask: Decimal
    volume_24h: Decimal
    book: OrderBook
    candles: dict[int, tuple[Decimal, ...]]
    funding: Decimal | None
    open_interest: Decimal | None
    timestamp: float
    index_price: Decimal | None = None

    @property
    def spread_ratio(self) -> Decimal:
        if self.last <= ZERO or self.bid <= ZERO or self.ask <= ZERO:
            return Decimal("1")
        return (self.ask - self.bid) / self.last

    @property
    def mid(self) -> Decimal:
        if self.bid > ZERO and self.ask > ZERO:
            return (self.bid + self.ask) / Decimal("2")
        return self.last


@dataclass(frozen=True)
class Position:
    symbol: str
    direction: Direction
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal
    unrealized_pnl: Decimal
    notional: Decimal


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    cash: Decimal
    available_margin: Decimal
    used_margin: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    daily_pnl: Decimal
    drawdown: Decimal
    positions: tuple[Position, ...]
    open_orders: int
    orders_today: int
    timestamp: float


@dataclass(frozen=True)
class NewsEvent:
    source: str
    url: str
    title: str
    published_at: float
    asset: str | None
    event: str
    direction: str
    impact: Decimal
    novelty: Decimal
    credibility: Decimal
    horizon: str
    market_confirmation: Decimal


@dataclass(frozen=True)
class GeminiAnalysis:
    asset: str | None
    event: str
    direction: str
    impact: Decimal
    confidence: Decimal
    time_horizon: str
    novelty: Decimal
    market_confirmation: Decimal
    risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Features:
    return_1: Decimal
    return_5: Decimal
    return_20: Decimal
    momentum: Decimal
    ema_slope: Decimal
    atr_ratio: Decimal
    realized_vol: Decimal
    downside_vol: Decimal
    volume_anomaly: Decimal
    spread: Decimal
    depth: Decimal
    imbalance: Decimal
    market_impact: Decimal
    relative_strength: Decimal
    btc_regime_score: Decimal
    funding: Decimal
    basis: Decimal
    open_interest_change: Decimal
    liquidity_score: Decimal


@dataclass(frozen=True)
class Signal:
    long_score: Decimal
    short_score: Decimal
    confidence: Decimal
    expected_return: Decimal
    uncertainty: Decimal
    strategy_version: str
    model_version: str

    @property
    def preferred_direction(self) -> Direction | None:
        if max(self.long_score, self.short_score) < Decimal("0.5"):
            return None
        return Direction.LONG if self.long_score >= self.short_score else Direction.SHORT


@dataclass(frozen=True)
class CostEstimate:
    fee: Decimal
    spread: Decimal
    slippage: Decimal
    market_impact: Decimal
    funding: Decimal
    financing: Decimal
    safety_buffer: Decimal

    @property
    def total(self) -> Decimal:
        return sum((self.fee, self.spread, self.slippage, self.market_impact, self.funding, self.financing, self.safety_buffer), ZERO)


@dataclass(frozen=True)
class OrderIntent:
    cycle_id: str
    decision_id: str
    intent_id: str
    client_order_id: str
    symbol: str
    product_type: ProductType
    direction: Direction
    effect: str
    order_type: str
    price: Decimal | None
    quantity: Decimal
    leverage: Decimal
    reduce_only: bool
    post_only: bool
    strategy_version: str
    model_version: str
    config_hash: str
    created_at: float


@dataclass(frozen=True)
class Decision:
    state: DecisionState
    symbol: str
    direction: Direction | None
    target_notional: Decimal
    leverage: Decimal
    expected_edge: Decimal
    confidence: Decimal
    blocker: Blocker
    reason: str
    decision_id: str
    cycle_id: str


@dataclass
class RuntimeState:
    system_state: str = "BOOT"
    system_ready: bool = False
    market_data_healthy: bool = False
    private_data_healthy: bool = False
    portfolio_consistent: bool = False
    model_ready: bool = False
    news_healthy: bool = False
    gemini_healthy: bool = False
    circuit_breaker: bool = False
    margin_safe: bool = False
    last_action: str = ""
    last_symbol: str = ""
    last_direction: str = ""
    last_blocker: str = ""
    last_trade: str = ""
    active_strategy: str = "baseline"
    active_model: str = "baseline"
    active_regime: str = Regime.UNKNOWN
    last_news_event: str = ""
    gemini_status: str = "disabled"
    learning_status: str = "idle"
    portfolio: PortfolioSnapshot | None = None
    expected_edge: Decimal = ZERO
    average_slippage: Decimal = ZERO
    average_latency: Decimal = ZERO
    metadata: dict[str, Any] = field(default_factory=dict)
