from __future__ import annotations

PLATFORMS = ["sensor", "binary_sensor"]

DOMAIN = "kraken_ai_trader"
NAME = "Kraken AI Trader"
VERSION = "1.0.0"

CONF_API_KEY = "api_key"
CONF_API_SECRET = "api_secret"
CONF_ACCOUNT_CURRENCY = "account_currency"
CONF_FUTURES_API_KEY = "futures_api_key"
CONF_FUTURES_API_SECRET = "futures_api_secret"
CONF_GEMINI_API_KEY = "gemini_api_key"
CONF_GEMINI_MODEL = "gemini_model"
CONF_LIVE_ENABLED = "live_enabled"
CONF_ENABLED = "enabled"
CONF_MARKET_INTERVAL = "market_interval"
CONF_MIN_LIQUIDITY = "minimum_liquidity"
CONF_MAX_SPREAD = "max_spread"
CONF_DATA_FRESHNESS = "data_freshness"
CONF_MIN_EDGE = "minimum_expected_edge"
CONF_MIN_CONFIDENCE = "minimum_confidence"
CONF_MAX_POSITION_RISK = "max_position_risk"
CONF_MAX_GROSS_EXPOSURE = "max_gross_exposure"
CONF_MAX_NET_EXPOSURE = "max_net_exposure"
CONF_MAX_MARGIN = "max_margin"
CONF_MAX_LEVERAGE = "max_leverage"
CONF_MAX_POSITIONS = "max_positions"
CONF_DAILY_LOSS = "daily_loss_limit"
CONF_MAX_DRAWDOWN = "max_drawdown"
CONF_CASH_RESERVE = "cash_reserve"
CONF_MAX_SLIPPAGE = "max_slippage"
CONF_ORDER_TIMEOUT = "order_timeout"
CONF_MAX_REPRICES = "max_reprices"
CONF_MAX_ORDERS_DAY = "max_orders_per_day"
CONF_LEARNING = "learning_enabled"
CONF_LOOKBACK = "learning_lookback"
CONF_VALIDATION_INTERVAL = "validation_interval"
CONF_AUTO_CALIBRATION = "auto_calibration"
CONF_AUTO_PROMOTION = "auto_promotion"
CONF_NEWS_ENABLED = "news_enabled"
CONF_NEWS_URLS = "news_urls"
CONF_POLL_INTERVAL = "poll_interval"
CONF_HISTORY_BACKFILL = "history_backfill_instruments"

DEFAULTS = {
    CONF_ENABLED: False,
    CONF_LIVE_ENABLED: False,
    CONF_GEMINI_MODEL: "gemini-3.8-flash",
    CONF_MARKET_INTERVAL: 60,
    CONF_MIN_LIQUIDITY: 100000,
    CONF_MAX_SPREAD: 0.004,
    CONF_DATA_FRESHNESS: 20,
    CONF_MIN_EDGE: 0.003,
    CONF_MIN_CONFIDENCE: 0.55,
    CONF_MAX_POSITION_RISK: 0.02,
    CONF_MAX_GROSS_EXPOSURE: 1.0,
    CONF_MAX_NET_EXPOSURE: 1.0,
    CONF_MAX_MARGIN: 0.25,
    CONF_MAX_LEVERAGE: 3.0,
    CONF_MAX_POSITIONS: 5,
    CONF_DAILY_LOSS: 0.05,
    CONF_MAX_DRAWDOWN: 0.10,
    CONF_CASH_RESERVE: 10.0,
    CONF_MAX_SLIPPAGE: 0.0025,
    CONF_ORDER_TIMEOUT: 20,
    CONF_MAX_REPRICES: 2,
    CONF_MAX_ORDERS_DAY: 20,
    CONF_LEARNING: True,
    CONF_LOOKBACK: 5000,
    CONF_VALIDATION_INTERVAL: 3600,
    CONF_AUTO_CALIBRATION: True,
    CONF_AUTO_PROMOTION: False,
    CONF_NEWS_ENABLED: True,
    CONF_NEWS_URLS: "https://www.coindesk.com/arc/outboundfeeds/rss/",
    CONF_POLL_INTERVAL: 60,
    CONF_ACCOUNT_CURRENCY: "EUR",
    CONF_HISTORY_BACKFILL: 10,
}

BINARY_SENSORS = (
    "trading_enabled",
    "system_ready",
    "market_data_healthy",
    "private_data_healthy",
    "portfolio_consistent",
    "model_ready",
    "news_healthy",
    "gemini_healthy",
    "circuit_breaker",
    "margin_safe",
)

NUMERIC_SENSORS = (
    "portfolio_equity",
    "available_cash",
    "available_margin",
    "used_margin",
    "gross_exposure",
    "net_exposure",
    "realized_pnl",
    "unrealized_pnl",
    "daily_pnl",
    "drawdown",
    "open_positions",
    "open_orders",
    "orders_today",
    "current_leverage",
    "average_slippage",
    "average_latency",
    "expected_edge",
)

TEXT_SENSORS = (
    "system_state",
    "last_action",
    "last_symbol",
    "last_direction",
    "last_blocker",
    "last_trade",
    "active_strategy",
    "active_model",
    "active_regime",
    "last_news_event",
    "gemini_status",
    "learning_status",
)
