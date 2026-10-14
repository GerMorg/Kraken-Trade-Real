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
    # These ceilings must not be relaxable by saved HA options or model output.
    IMMUTABLE_MAX_LEVERAGE = D("3")
    IMMUTABLE_MAX_POSITION_PCT = D("15")
    IMMUTABLE_MAX_GROSS_PCT = D("80")
    IMMUTABLE_MAX_NET_PCT = D("50")
    IMMUTABLE_MAX_MARGIN_PCT = D("35")
    IMMUTABLE_MAX_OPEN_POSITIONS = 3
    IMMUTABLE_MAX_DAILY_LOSS_PCT = D("3")
    IMMUTABLE_MAX_DRAWDOWN_PCT = D("8")
    IMMUTABLE_MIN_CASH_RESERVE_PCT = D("20")
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
        # Decisions restored from legacy state/tests or external strategies may
        # contain float leverage. Risk arithmetic is Decimal-only.
        leverage = D(str(d.leverage))
        execution_direction = d.execution_direction or d.signal.direction
        direction = execution_direction.value

        current = d.current_position_eur
        desired = d.target_position_eur
        if d.execution_direction is None and current == 0 and desired == 0:
            desired = d.target_notional_eur if direction == "LONG" else -d.target_notional_eur

        current_abs = abs(current)
        desired_abs = abs(desired)
        valid_reduction = (
            d.reduce_only
            and current != 0
            and desired_abs < current_abs
            and (desired == 0 or current * desired > 0)
        )
        gross_after = p.gross_eur - current_abs + desired_abs
        net_after = p.net_eur - current + desired
        eq = p.equity_eur

        if eq <= 0:
            # An already-open position can still need to be flattened after an
            # insolvency, stale valuation, or extreme loss. A strictly valid
            # reduce-only target cannot create exposure: it must reduce absolute
            # position size and may not cross through zero into the other side.
            # Do not let a non-positive equity snapshot trap that exposure.
            if valid_reduction:
                return RiskResult(
                    True,
                    "REDUCE_ONLY_EXIT_ALLOWED_NO_POSITIVE_EQUITY",
                    {
                        "equity_positive": False,
                        "reduce_only_semantics": True,
                        "valid_reduction": True,
                        "new_exposure": False,
                    },
                    leverage,
                )
            return RiskResult(
                False,
                "NO_POSITIVE_EQUITY",
                {"equity_positive": False, "valid_reduction": False},
                leverage,
            )

        pos_pct = desired_abs / eq * 100
        gross_pct = gross_after / eq * 100
        net_pct = abs(net_after) / eq * 100
        is_new_position = current == 0 and desired != 0
        min_cost_eur = D(str(d.rationale.get("min_cost_eur", "0")))
        risk_profile = str(d.rationale.get("risk_profile", "core"))
        position_limit_pct = min(D(str(self.config.risk_max_position_pct)), self.IMMUTABLE_MAX_POSITION_PCT)
        volatility_limit = D("30")
        if risk_profile == "tactical":
            requested_position_limit = D(str(d.rationale.get("risk_position_limit_pct", "25")))
            # Tactical has its own absolute capital cap and tighter trade-count gates;\n            # retain its configured percentage sizing while global gross/net caps remain immutable.\n            position_limit_pct = min(D("100"), max(D("0.1"), requested_position_limit))
            volatility_limit = min(
                D("60"),
                max(D("30"), D(str(d.rationale.get("risk_volatility_max", "55")))),
            )

        # Core candidate selection uses calibrated confidence. Carry that exact
        # value into the final risk gate; Tactical keeps its direct signal confidence.
        confidence_for_risk = d.signal.confidence
        if risk_profile != "tactical":
            try:
                calibrated = D(str(
                    d.rationale.get("calibrated_confidence", d.signal.confidence)
                ))
                if calibrated.is_finite():
                    confidence_for_risk = max(D("0"), min(D("1"), calibrated))
            except (ArithmeticError, TypeError, ValueError):
                pass

        # Strategy-specific position sizing must never widen the portfolio-wide
        # net-exposure envelope. Tactical position limits may be looser than Core,
        # but the immutable global net cap remains authoritative for every entry.
        net_limit_pct = min(D(str(self.config.risk_max_net_pct)), self.IMMUTABLE_MAX_NET_PCT)

        risk_edge_threshold = D(str(self.config.strategy_min_edge_bps))
        economic_cost_ratio = D("1")
        if risk_profile == "tactical":
            risk_edge_threshold = D(str(
                getattr(self.config, "tactical_adaptive_min_net_edge_bps", 15.0)
            ))
        elif str(d.rationale.get("edge_tier", "STANDARD")) == "ADAPTIVE":
            risk_edge_threshold = D(str(
                getattr(self.config, "strategy_adaptive_edge_floor_bps", 15.0)
            ))
            economic_cost_ratio = D(str(
                getattr(self.config, "strategy_adaptive_cost_ratio", 1.10)
            ))

        # A positive net edge alone is not sufficient when the gross forecast
        # is tiny relative to the strategy's stop distance. Require the expected
        # net edge to cover at least half the stop risk, regardless of learned
        # thresholds. Adaptive promotion may make this stricter, never weaker.
        if risk_profile != "tactical":
            try:
                stop_loss_pct = D(str(getattr(self.config, "strategy_stop_loss_pct", 2.0)))
                if stop_loss_pct.is_finite() and stop_loss_pct > 0:
                    risk_edge_threshold = max(
                        risk_edge_threshold,
                        stop_loss_pct * D("50"),
                    )
            except (ArithmeticError, TypeError, ValueError):
                risk_edge_threshold = max(risk_edge_threshold, D("100"))

        same_side = current == 0 or desired == 0 or current * desired > 0
        incremental_notional = max(D("0"), desired_abs - current_abs) if same_side else desired_abs
        increases_exposure = incremental_notional > 0
        incremental_capital = incremental_notional / leverage if leverage > 1 else incremental_notional
        cash_reserve_pct = max(self.IMMUTABLE_MIN_CASH_RESERVE_PCT, D(str(self.config.risk_cash_reserve_pct)))
        cash_reserve_min = eq * cash_reserve_pct / 100
        projected_cash = p.cash_eur - incremental_capital

        checks = {
            "instrument_direction": (
                valid_reduction
                or (
                    d.instrument.long_available
                    if direction == "LONG"
                    else d.instrument.short_available
                )
            ),
            "daily_loss": valid_reduction or p.daily_pnl_eur >= -(
                eq * min(D(str(self.config.risk_daily_loss_pct)), self.IMMUTABLE_MAX_DAILY_LOSS_PCT) / 100
            ),
            "drawdown": valid_reduction or p.drawdown_pct <= min(
                D(str(self.config.risk_max_drawdown_pct)), self.IMMUTABLE_MAX_DRAWDOWN_PCT
            ),
            "position_limit": valid_reduction or pos_pct <= position_limit_pct,
            "gross_limit": valid_reduction or gross_pct <= min(
                D(str(self.config.risk_max_gross_pct)), self.IMMUTABLE_MAX_GROSS_PCT
            ),
            "net_limit": valid_reduction or net_pct <= net_limit_pct,
            "tactical_absolute_cap": (
                valid_reduction
                or risk_profile != "tactical"
                or not increases_exposure
                or desired_abs <= min(
                    D(str(getattr(self.config, "tactical_max_capital_eur", 15.0))),
                    D("15"),
                )
            ),
            "open_positions_limit": (
                not is_new_position
                or len(p.positions) < min(int(self.config.risk_max_open_positions), self.IMMUTABLE_MAX_OPEN_POSITIONS)
            ),
            "cash_reserve": (
                valid_reduction or not increases_exposure or projected_cash >= cash_reserve_min
            ),
            "leverage_bound": (
                valid_reduction
                or leverage <= min(
                    self.IMMUTABLE_MAX_LEVERAGE,
                    D(str(self.config.risk_max_leverage)),
                    d.instrument.max_leverage,
                )
            ),
            # Entry thresholds protect new risk. They must not block
            # reduce-only exits, whose purpose is to remove existing risk.
            "edge_positive": (
                valid_reduction
                or (
                    d.signal.net_edge_bps >= risk_edge_threshold
                    and d.signal.expected_return_bps >= (
                        d.signal.expected_cost_bps * economic_cost_ratio
                    )
                )
            ),
            "confidence": (
                valid_reduction
                or confidence_for_risk >= D(str(self.config.strategy_min_confidence))
            ),
            "extreme_volatility": (
                valid_reduction
                or d.signal.features.get("volatility", D(999)) <= volatility_limit
            ),
        }

        checks["reduce_only_semantics"] = (not d.reduce_only) or valid_reduction

        if min_cost_eur > 0:
            checks["minimum_cost"] = valid_reduction or desired_abs >= min_cost_eur

        if leverage > 1 and not valid_reduction:
            checks["margin_available"] = margin_account is not None
            if margin_account is not None:
                requested_margin = incremental_notional / leverage
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
                checks["margin_budget"] = used <= min(
                    D(str(self.config.risk_max_margin_pct)), self.IMMUTABLE_MAX_MARGIN_PCT
                )

        failed = next((key for key, value in checks.items() if not value), None)
        return RiskResult(
            not failed,
            failed or "RISK_OK",
            checks,
            leverage,
        )
