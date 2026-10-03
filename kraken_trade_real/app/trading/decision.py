
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Decision, Instrument, PortfolioState, Signal, new_id
from app.domain.states import Direction

D = Decimal


class DecisionEngine:
    STRATEGY_VERSION = "baseline-v1"

    def __init__(self, config: Any) -> None:
        self.config = config

    def _confidence_scale(self, model_parameters: dict[str, Any] | None) -> D:
        parameters = model_parameters or {}
        scale = D(str(parameters.get("confidence_scale", "1")))
        return max(D("0.5"), min(D("1.5"), scale))

    def _target_position(
        self,
        equity: D,
        confidence: D,
        signal_direction: Direction,
    ) -> D:
        if equity <= 0:
            return D("0")
        target = equity * D(str(self.config.risk_max_position_pct)) / 100
        target *= max(D("0.25"), min(D("1"), confidence))
        return target if signal_direction == Direction.LONG else -target

    def _trade_plan(
        self,
        instrument: Instrument,
        portfolio: PortfolioState,
        signal: Signal,
        calibrated_confidence: D,
        min_cost_eur: D,
    ) -> dict[str, Any]:
        current = portfolio.positions.get(instrument.symbol, D("0"))
        desired = self._target_position(
            portfolio.equity_eur,
            calibrated_confidence,
            signal.direction,
        )
        reversal = current != 0 and ((current > 0 and desired < 0) or (current < 0 and desired > 0))
        if reversal:
            # Reverse in two safe stages: first flatten, then wait for a fresh cycle
            # before opening the opposite exposure.
            desired = D("0")
        delta = desired - current
        trade_notional = abs(delta)
        reducing = current != 0 and abs(desired) < abs(current)
        execution_direction = Direction.LONG if delta > 0 else Direction.SHORT if delta < 0 else None
        reduce_only = reducing and execution_direction is not None
        return {
            "current_position_eur": current,
            "target_position_eur": desired,
            "trade_notional_eur": trade_notional,
            "execution_direction": execution_direction,
            "reduce_only": reduce_only,
            "reversal_to_flat": reversal,
            "balanced": trade_notional < min_cost_eur,
        }

    def rejection_reason(
        self,
        instrument: Instrument,
        long_signal: Signal,
        short_signal: Signal,
        portfolio: PortfolioState,
        model_parameters: dict[str, Any] | None = None,
        min_cost_eur: D | None = None,
    ) -> str:
        scale = self._confidence_scale(model_parameters)
        effective_min_cost = (
            min_cost_eur if min_cost_eur is not None
            else instrument.min_cost
        )
        signals = [long_signal, short_signal]
        edge_candidates = [
            signal for signal in signals
            if signal.net_edge_bps >= D(str(self.config.strategy_min_edge_bps))
        ]
        if not edge_candidates:
            return "MIN_EDGE"
        confidence_candidates = [
            signal for signal in edge_candidates
            if signal.confidence * scale >= D(str(self.config.strategy_min_confidence))
        ]
        if not confidence_candidates:
            return "MIN_CONFIDENCE"
        best = max(
            confidence_candidates,
            key=lambda signal: (signal.net_edge_bps, signal.confidence),
        )
        calibrated_confidence = max(
            D("0.0"), min(D("1.0"), best.confidence * scale)
        )
        plan = self._trade_plan(
            instrument,
            portfolio,
            best,
            calibrated_confidence,
            effective_min_cost,
        )
        if plan["trade_notional_eur"] <= 0 or plan["balanced"]:
            return "TARGET_BALANCED"
        if effective_min_cost > 0 and plan["trade_notional_eur"] < effective_min_cost:
            return "MINIMUM_COST"
        return "NO_ACTION"

    def choose(
        self,
        instrument: Instrument,
        long_signal: Signal,
        short_signal: Signal,
        portfolio: PortfolioState,
        model_version: str,
        config_hash: str,
        model_parameters: dict[str, Any] | None = None,
        min_cost_eur: D | None = None,
    ) -> Decision | None:
        scale = self._confidence_scale(model_parameters)
        effective_min_cost = (
            min_cost_eur if min_cost_eur is not None
            else instrument.min_cost
        )
        candidates = [
            signal for signal in (long_signal, short_signal)
            if signal.net_edge_bps >= D(str(self.config.strategy_min_edge_bps))
            and signal.confidence * scale >= D(str(self.config.strategy_min_confidence))
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda signal: (signal.net_edge_bps, signal.confidence),
            reverse=True,
        )
        signal = candidates[0]
        calibrated_confidence = max(
            D("0.0"), min(D("1.0"), signal.confidence * scale)
        )
        plan = self._trade_plan(
            instrument,
            portfolio,
            signal,
            calibrated_confidence,
            effective_min_cost,
        )
        if plan["trade_notional_eur"] <= 0 or plan["balanced"]:
            return None
        if effective_min_cost > 0 and plan["trade_notional_eur"] < effective_min_cost:
            return None
        execution_direction = plan["execution_direction"]
        if execution_direction is None:
            return None
        rationale = {
            "long_net_edge_bps": str(long_signal.net_edge_bps),
            "short_net_edge_bps": str(short_signal.net_edge_bps),
            "selected_direction": signal.direction.value,
            "execution_direction": execution_direction.value,
            "regime": signal.regime,
            "news_effect_bps": str(signal.news_effect_bps),
            "gemini_effect_bps": str(signal.gemini_effect_bps),
            "current_position_eur": str(plan["current_position_eur"]),
            "target_position_eur": str(plan["target_position_eur"]),
            "trade_notional_eur": str(plan["trade_notional_eur"]),
            "reduce_only": plan["reduce_only"],
            "reversal_to_flat": plan["reversal_to_flat"],
            "min_cost_eur": str(effective_min_cost),
        }
        return Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=signal,
            target_notional_eur=plan["trade_notional_eur"],
            leverage=D("1"),
            rationale=rationale,
            strategy_version=self.STRATEGY_VERSION,
            model_version=model_version,
            config_hash=config_hash,
            current_position_eur=plan["current_position_eur"],
            target_position_eur=plan["target_position_eur"],
            execution_direction=execution_direction,
            reduce_only=plan["reduce_only"],
        )
