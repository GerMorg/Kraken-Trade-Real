from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.models import Decision, PortfolioState

D = Decimal


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reason: str
    checks: dict[str, bool]
    effective_leverage: D


class RiskEngine:
    IMMUTABLE_MAX_LEVERAGE = D(5)
    IMMUTABLE_MIN_MARGIN_LEVEL = D(200)

    def __init__(self, config: Any, margin_engine: Any, leverage_engine: Any, cost_model: Any) -> None:
        self.config = config
        self.margin_engine = margin_engine
        self.leverage_engine = leverage_engine
        self.cost_model = cost_model

    def evaluate(
        self,
        d: Decision,
        p: PortfolioState,
        market: Any,
        margin_account: dict[str, Any] | None = None,
    ) -> RiskResult:
        execution_direction = d.execution_direction or d.signal.direction
        direction = execution_direction.value

        current = d.current_position_eur
        desired = d.target_position_eur
        if d.execution_direction is None and current == 0 and desired == 0:
            desired = d.target_notional_eur if direction == "LONG" else -d.target_notional_eur

        current_abs = abs(current)
        desired_abs = abs(desired)
        gross_after = p.gross_eur - current_abs + desired_abs
        net_after = p.net_eur - current + desired
        delta = desired - current
        eq = p.equity_eur

        if eq <= 0:
            return RiskResult(
                False,
                "NO_POSITIVE_EQUITY",
                {"equity_positive": False},
                d.leverage,
            )

        pos_pct = desired_abs / eq * 100
        gross_pct = gross_after / eq * 100
        net_pct = abs(net_after) / eq * 100
        is_new_position = current == 0 and desired != 0
        min_cost_eur = D(str(d.rationale.get("min_cost_eur", "0")))
        risk_profile = str(d.rationale.get("risk_profile", "core"))
        position_limit_pct = D(str(self.config.risk_max_position_pct))
        volatility_limit = D("30")
        if risk_profile == "tactical":
            requested_position_limit = D(str(d.rationale.get("risk_position_limit_pct", "25")))
            position_limit_pct = min(D("30"), max(D("0.1"), requested_position_limit))
            volatility_limit = min(
                D("60"),
                max(D("30"), D(str(d.rationale.get("risk_volatility_max", "55")))),
            )

        checks = {
            "instrument_direction": (
                d.reduce_only
                or (
                    d.instrument.long_available
                    if direction == "LONG"
                    else d.instrument.short_available
                )
            ),
            "daily_loss": p.daily_pnl_eur >= -(
                eq * D(str(self.config.risk_daily_loss_pct)) / 100
            ),
            "drawdown": p.drawdown_pct <= D(str(self.config.risk_max_drawdown_pct)),
            "position_limit": pos_pct <= position_limit_pct,
            "gross_limit": gross_pct <= D(str(self.config.risk_max_gross_pct)),
            "net_limit": net_pct <= D(str(self.config.risk_max_net_pct)),
            "open_positions_limit": (
                not is_new_position
                or len(p.positions) < self.config.risk_max_open_positions
            ),
            "cash_reserve": (
                p.cash_eur >= eq * D(str(self.config.risk_cash_reserve_pct)) / 100
                if delta > 0
                else True
            ),
            "leverage_bound": d.leverage <= min(
                self.IMMUTABLE_MAX_LEVERAGE,
                D(str(self.config.risk_max_leverage)),
                d.instrument.max_leverage,
            ),
            # Entry thresholds protect new risk. They must not block
            # reduce-only exits, whose purpose is to remove existing risk.
            "edge_positive": (
                d.reduce_only
                or d.signal.net_edge_bps >= D(str(self.config.strategy_min_edge_bps))
            ),
            "confidence": (
                d.reduce_only
                or d.signal.confidence >= D(str(self.config.strategy_min_confidence))
            ),
            "extreme_volatility": (
                d.reduce_only
                or d.signal.features.get("volatility", D(999)) <= volatility_limit
            ),
        }

        if min_cost_eur > 0:
            checks["minimum_cost"] = d.reduce_only or desired_abs >= min_cost_eur

        if d.leverage > 1:
            checks["margin_available"] = margin_account is not None
            if margin_account is not None:
                requested_margin = desired_abs / d.leverage
                ok, _ = self.margin_engine.available(
                    margin_account,
                    requested_margin,
                )
                checks["margin_free"] = ok
                level = D(str(margin_account.get("margin_level_pct") or 0))
                checks["margin_level"] = level >= self.IMMUTABLE_MIN_MARGIN_LEVEL
                used = (
                    p.margin_used_eur + requested_margin
                ) / eq * 100
                checks["margin_budget"] = used <= D(
                    str(self.config.risk_max_margin_pct)
                )

        failed = next((key for key, value in checks.items() if not value), None)
        return RiskResult(
            not failed,
            failed or "RISK_OK",
            checks,
            d.leverage,
        )
