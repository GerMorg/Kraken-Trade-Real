from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Decision, Instrument, PortfolioState, Signal, new_id


D=Decimal


class DecisionEngine:
    STRATEGY_VERSION="baseline-v1"

    def __init__(self, config: Any) -> None:
        self.config=config

    def _confidence_scale(self, model_parameters: dict[str, Any] | None) -> D:
        parameters = model_parameters or {}
        scale = D(str(parameters.get("confidence_scale", "1")))
        return max(D("0.5"), min(D("1.5"), scale))

    def rejection_reason(
        self,
        instrument: Instrument,
        long_signal: Signal,
        short_signal: Signal,
        portfolio: PortfolioState,
        model_parameters: dict[str, Any] | None = None,
    ) -> str:
        scale = self._confidence_scale(model_parameters)
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
        best = max(confidence_candidates, key=lambda signal: (signal.net_edge_bps, signal.confidence))
        calibrated_confidence = max(D("0.0"), min(D("1.0"), best.confidence * scale))
        target = portfolio.equity_eur * D(str(self.config.risk_max_position_pct)) / 100
        target *= max(D("0.25"), min(D("1"), calibrated_confidence))
        if target < instrument.min_cost:
            return "MINIMUM_COST"
        return "NO_ACTION"

    def choose(self, instrument: Instrument, long_signal: Signal, short_signal: Signal,
               portfolio: PortfolioState, model_version: str, config_hash: str,
               model_parameters: dict[str, Any] | None = None) -> Decision | None:
        scale = self._confidence_scale(model_parameters)
        candidates = [
            signal for signal in (long_signal, short_signal)
            if signal.net_edge_bps >= D(str(self.config.strategy_min_edge_bps))
            and signal.confidence * scale >= D(str(self.config.strategy_min_confidence))
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda signal: (signal.net_edge_bps, signal.confidence), reverse=True)
        signal = candidates[0]
        calibrated_confidence = max(D("0.0"), min(D("1.0"), signal.confidence * scale))
        target = portfolio.equity_eur * D(str(self.config.risk_max_position_pct)) / 100
        target *= max(D("0.25"), min(D("1"), calibrated_confidence))
        if target < instrument.min_cost:
            return None
        return Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=signal,
            target_notional_eur=target,
            leverage=D("1"),
            rationale={
                "long_net_edge_bps": str(long_signal.net_edge_bps),
                "short_net_edge_bps": str(short_signal.net_edge_bps),
                "selected_direction": signal.direction.value,
                "regime": signal.regime,
                "news_effect_bps": str(signal.news_effect_bps),
                "gemini_effect_bps": str(signal.gemini_effect_bps),
            },
            strategy_version=self.STRATEGY_VERSION,
            model_version=model_version,
            config_hash=config_hash,
        )
