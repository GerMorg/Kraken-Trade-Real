from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Blocker, CostEstimate, DecisionState, Direction, Instrument, PortfolioSnapshot, Signal


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    blocker: Blocker
    reason: str
    notional: Decimal
    leverage: Decimal


class RiskEngine:
    """Immutable risk authority: learned parameters never mutate configured safety limits."""

    def __init__(self, limits) -> None:
        self.limits = limits

    def choose_leverage(
        self,
        signal: Signal,
        volatility: Decimal,
        liquidity: Decimal,
        margin_utilization: Decimal,
        drawdown: Decimal,
        instrument: Instrument,
    ) -> Decimal:
        requested = Decimal("1") + max(Decimal("0"), signal.confidence - Decimal("0.5")) * Decimal("4")
        requested *= max(Decimal("0.25"), min(Decimal("1.0"), signal.expected_return / Decimal("0.02") if signal.expected_return else Decimal("0.25")))
        if volatility > Decimal("0.10"):
            requested *= Decimal("0.5")
        if volatility > Decimal("0.20"):
            requested = Decimal("1")
        if liquidity < Decimal("0.2"):
            requested *= Decimal("0.5")
        if margin_utilization >= self.limits.max_margin:
            requested = Decimal("1")
        if drawdown > self.limits.max_drawdown * Decimal("0.5"):
            requested = min(requested, Decimal("1.5"))
        return min(self.limits.max_leverage, instrument.max_leverage, max(Decimal("1"), requested))

    def evaluate(
        self,
        portfolio: PortfolioSnapshot,
        instrument: Instrument,
        signal: Signal,
        costs: CostEstimate,
        decision_state: DecisionState,
        requested_notional: Decimal,
        leverage: Decimal,
    ) -> RiskResult:
        equity = portfolio.equity
        if equity <= 0:
            return RiskResult(False, Blocker.BLOCKED_PORTFOLIO, "non-positive portfolio equity", Decimal("0"), Decimal("1"))
        if portfolio.drawdown >= self.limits.max_drawdown:
            return RiskResult(False, Blocker.CIRCUIT_BREAKER, "max drawdown reached", Decimal("0"), Decimal("1"))
        if portfolio.daily_pnl <= -self.limits.daily_loss_limit * equity:
            return RiskResult(False, Blocker.CIRCUIT_BREAKER, "daily loss limit reached", Decimal("0"), Decimal("1"))
        if leverage < 1 or leverage > self.limits.max_leverage or leverage > instrument.max_leverage:
            return RiskResult(False, Blocker.BLOCKED_LEVERAGE, "selected leverage exceeds configured/instrument limits", Decimal("0"), Decimal("1"))
        margin_utilization = portfolio.used_margin / equity
        if margin_utilization >= self.limits.max_margin and decision_state not in {
            DecisionState.REDUCE_LONG,
            DecisionState.CLOSE_LONG,
            DecisionState.REDUCE_SHORT,
            DecisionState.CLOSE_SHORT,
        }:
            return RiskResult(False, Blocker.BLOCKED_MARGIN, "margin utilization at configured maximum", Decimal("0"), Decimal("1"))
        if signal.confidence < Decimal("0.55"):
            return RiskResult(False, Blocker.BLOCKED_RISK, "confidence below deterministic safety floor", Decimal("0"), Decimal("1"))
        if costs.total >= signal.expected_return and decision_state not in {
            DecisionState.REDUCE_LONG,
            DecisionState.CLOSE_LONG,
            DecisionState.REDUCE_SHORT,
            DecisionState.CLOSE_SHORT,
        }:
            return RiskResult(False, Blocker.BLOCKED_COST, "total expected costs consume the edge", Decimal("0"), Decimal("1"))

        # Close/reduce actions are bounded by the existing position and do not create additional gross exposure.
        if decision_state in {DecisionState.REDUCE_LONG, DecisionState.CLOSE_LONG, DecisionState.REDUCE_SHORT, DecisionState.CLOSE_SHORT}:
            return RiskResult(True, Blocker.NONE, "risk-reducing action", max(Decimal("0"), requested_notional), Decimal("1"))

        if decision_state in {DecisionState.OPEN_LONG, DecisionState.OPEN_SHORT} and len(portfolio.positions) >= self.limits.max_positions:
            return RiskResult(False, Blocker.BLOCKED_PORTFOLIO, "maximum open positions reached", Decimal("0"), Decimal("1"))

        max_risk_cash = equity * self.limits.max_position_risk
        risk_per_unit = max(costs.total + abs(signal.uncertainty), Decimal("0.01"))
        size_by_risk = max_risk_cash / risk_per_unit * leverage
        available = max(Decimal("0"), portfolio.available_margin - self.limits.cash_reserve)
        requested = min(requested_notional, size_by_risk, available * leverage)

        if requested < instrument.cost_min:
            return RiskResult(False, Blocker.BLOCKED_POSITION_SIZE, "minimum trade cost cannot be funded safely", Decimal("0"), Decimal("1"))
        requested = min(requested, equity * self.limits.max_gross_exposure - portfolio.gross_exposure)
        if signal.preferred_direction == Direction.LONG:
            projected_net = portfolio.net_exposure + requested
        else:
            projected_net = portfolio.net_exposure - requested
        if abs(projected_net) > equity * self.limits.max_net_exposure:
            return RiskResult(False, Blocker.BLOCKED_PORTFOLIO, "projected net exposure exceeds configured maximum", Decimal("0"), Decimal("1"))
        if portfolio.gross_exposure + requested > equity * self.limits.max_gross_exposure:
            return RiskResult(False, Blocker.BLOCKED_RISK, "projected gross exposure exceeds configured maximum", Decimal("0"), Decimal("1"))
        projected_margin = portfolio.used_margin + requested / max(leverage, Decimal("1"))
        if projected_margin / equity > self.limits.max_margin:
            return RiskResult(False, Blocker.BLOCKED_MARGIN, "projected margin exceeds configured maximum", Decimal("0"), Decimal("1"))
        return RiskResult(True, Blocker.NONE, "risk checks passed", requested, leverage)
