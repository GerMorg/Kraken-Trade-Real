from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    futures_enabled: bool
    futures_api_key: str
    futures_api_secret: str
    tokenized_assets_enabled: bool
    kraken_enabled: bool
    live_enabled: bool
    kill_switch: bool
    gemini_api_key: str
    gemini_enabled: bool
    gemini_model: str
    gemini_fallback_models: str
    gemini_timeout_seconds: int
    market_scan_interval_seconds: int
    market_min_liquidity_eur: float
    market_max_spread_bps: float
    market_max_data_age_seconds: int
    market_history_candidate_limit: int
    market_history_cache_seconds: int
    market_exploration_candidate_limit: int
    market_exploration_slots_per_family: int
    strategy_min_edge_bps: float
    strategy_min_confidence: float
    risk_max_position_pct: float
    risk_max_gross_pct: float
    risk_max_net_pct: float
    risk_max_margin_pct: float
    risk_max_leverage: float
    risk_max_open_positions: int
    risk_daily_loss_pct: float
    risk_max_drawdown_pct: float
    risk_cash_reserve_pct: float
    execution_max_slippage_bps: float
    execution_order_timeout_seconds: int
    execution_max_reprices: int
    execution_max_orders_per_day: int
    execution_reconciliation_limit: int
    execution_reconciliation_stale_seconds: int
    learning_enabled: bool
    learning_lookback_days: int
    learning_validation_interval_hours: int
    learning_auto_calibration: bool
    learning_auto_promotion: bool
    news_enabled: bool
    news_refresh_minutes: int
    news_source_timeout_seconds: int
    sensors_enabled: bool
    log_file_enabled: bool
    tax_enabled: bool
    tax_provider_classification: str
    tax_report_enabled: bool

    @classmethod
    def load(cls, path: str = "/data/options.json") -> "Config":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}

        def b(name: str, default: bool) -> bool:
            value = raw.get(name, default)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        def i(name: str, default: int, lo: int = 1) -> int:
            try:
                return max(lo, int(raw.get(name, default)))
            except (TypeError, ValueError):
                return default

        def f(
            name: str,
            default: float,
            lo: float | None = None,
            hi: float | None = None,
        ) -> float:
            try:
                value = float(raw.get(name, default))
            except (TypeError, ValueError):
                value = default
            if lo is not None:
                value = max(lo, value)
            if hi is not None:
                value = min(hi, value)
            return value

        cfg = cls(
            api_key=str(raw.get("kraken_api_key", os.getenv("KRAKEN_API_KEY", ""))).strip(),
            api_secret=str(raw.get("kraken_api_secret", os.getenv("KRAKEN_API_SECRET", ""))).strip(),
            futures_enabled=b("futures_enabled", False),
            futures_api_key=str(raw.get("futures_api_key", "")).strip(),
            futures_api_secret=str(raw.get("futures_api_secret", "")).strip(),
            tokenized_assets_enabled=b("tokenized_assets_enabled", False),
            kraken_enabled=b("kraken_enabled", True),
            live_enabled=b("live_enabled", False),
            kill_switch=b("kill_switch", True),
            gemini_api_key=str(raw.get("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))).strip(),
            gemini_enabled=b("gemini_enabled", True),
            gemini_model=str(raw.get("gemini_model", "gemini-3.8-flash")).strip(),
            gemini_fallback_models=str(raw.get("gemini_fallback_models", "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-2.5-flash-lite")).strip(),
            gemini_timeout_seconds=i("gemini_timeout_seconds", 30, 5),
            market_scan_interval_seconds=i("market_scan_interval_seconds", 300, 30),
            market_min_liquidity_eur=f("market_min_liquidity_eur", 50.0, 0.0),
            market_max_spread_bps=f("market_max_spread_bps", 80.0, 0.0, 2000.0),
            market_max_data_age_seconds=i("market_max_data_age_seconds", 30, 5),
            market_history_candidate_limit=i("market_history_candidate_limit", 200, 20),
            market_history_cache_seconds=i("market_history_cache_seconds", 900, 60),
            market_exploration_candidate_limit=i("market_exploration_candidate_limit", 80, 1),
            market_exploration_slots_per_family=i("market_exploration_slots_per_family", 2, 1),
            strategy_min_edge_bps=f("strategy_min_edge_bps", 25.0, 0.0, 5000.0),
            strategy_min_confidence=f("strategy_min_confidence", 0.58, 0.0, 1.0),
            risk_max_position_pct=f("risk_max_position_pct", 15.0, 0.1, 100.0),
            risk_max_gross_pct=f("risk_max_gross_pct", 80.0, 0.1, 100.0),
            risk_max_net_pct=f("risk_max_net_pct", 50.0, 0.1, 100.0),
            risk_max_margin_pct=f("risk_max_margin_pct", 35.0, 0.1, 100.0),
            risk_max_leverage=f("risk_max_leverage", 3.0, 1.0, 5.0),
            risk_max_open_positions=i("risk_max_open_positions", 3, 1),
            risk_daily_loss_pct=f("risk_daily_loss_pct", 3.0, 0.1, 100.0),
            risk_max_drawdown_pct=f("risk_max_drawdown_pct", 8.0, 0.1, 100.0),
            risk_cash_reserve_pct=f("risk_cash_reserve_pct", 20.0, 0.0, 100.0),
            execution_max_slippage_bps=f("execution_max_slippage_bps", 40.0, 0.0, 2000.0),
            execution_order_timeout_seconds=i("execution_order_timeout_seconds", 45, 5),
            execution_max_reprices=i("execution_max_reprices", 2, 0),
            execution_max_orders_per_day=i("execution_max_orders_per_day", 10, 1),
            execution_reconciliation_limit=i("execution_reconciliation_limit", 20, 1),
            execution_reconciliation_stale_seconds=i("execution_reconciliation_stale_seconds", 30, 5),
            learning_enabled=b("learning_enabled", True),
            learning_lookback_days=i("learning_lookback_days", 365, 1),
            learning_validation_interval_hours=i("learning_validation_interval_hours", 24, 1),
            learning_auto_calibration=b("learning_auto_calibration", True),
            learning_auto_promotion=b("learning_auto_promotion", True),
            news_enabled=b("news_enabled", True),
            news_refresh_minutes=i("news_refresh_minutes", 10, 1),
            news_source_timeout_seconds=i("news_source_timeout_seconds", 10, 3),
            sensors_enabled=b("sensors_enabled", True),
            log_file_enabled=b("log_file_enabled", True),
            tax_enabled=b("tax_enabled", True),
            tax_provider_classification=str(
                raw.get("tax_provider_classification", "FOREIGN")
            ).strip().upper(),
            tax_report_enabled=b("tax_report_enabled", True),
        )
        cls.validate(cfg)
        return cfg

    @staticmethod
    def validate(cfg: "Config") -> None:
        if cfg.futures_enabled and not (cfg.futures_api_key and cfg.futures_api_secret):
            raise ValueError("Futures enabled requires separate Futures API key and secret")
        if cfg.risk_max_net_pct > cfg.risk_max_gross_pct:
            raise ValueError("net risk ceiling cannot exceed gross risk ceiling")
        if cfg.risk_max_position_pct > cfg.risk_max_gross_pct:
            raise ValueError("position ceiling cannot exceed gross risk ceiling")
        if cfg.risk_max_leverage < 1:
            raise ValueError("leverage must be >= 1")
        if cfg.tax_provider_classification not in {"FOREIGN", "DOMESTIC", "UNVERIFIED"}:
            raise ValueError("tax_provider_classification must be FOREIGN, DOMESTIC or UNVERIFIED")
