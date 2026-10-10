from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Decision, Instrument, PortfolioState, Signal, new_id
from app.domain.states import Direction

D = Decimal


class DecisionEngine:
    STRATEGY_VERSION = "baseline-v3-learned-policy"

    def __init__(self, config: Any) -> None:
        self.config = config

    def _confidence_scale(self, model_parameters: dict[str, Any] | None) -> D:
        """Keep prediction-score calibration separate from live policy thresholds.

        Previous releases applied a Brier-selected confidence_scale to entry
        thresholds and sizing, despite training it on selected-trade outcomes.
        It is intentionally ignored; bounded strategy_policy parameters now own
        entry thresholds, while signal weights are learned through observations.
        """
        return D("1")

    @staticmethod
    def _policy_value(
        strategy_parameters: dict[str, Any] | None,
        key: str,
        default: Any,
    ) -> D:
        raw = (strategy_parameters or {}).get(key, default)
        try:
            return D(str(raw))
        except Exception:
            return D(str(default))

    def _target_position(self, equity: D, confidence: D, signal_direction: Direction) -> D:
        if equity <= 0:
            return D("0")
        target = equity * D(str(self.config.risk_max_position_pct)) / 100
        target *= max(D("0.25"), min(D("1"), confidence))
        return target if signal_direction == Direction.LONG else -target

    def _edge_policy(
        self,
        signal: Signal,
        calibrated_confidence: D,
        strategy_parameters: dict[str, Any] | None = None,
    ) -> tuple[bool, D, str]:
        standard = self._policy_value(
            strategy_parameters, "strategy_min_edge_bps", self.config.strategy_min_edge_bps
        )
        if signal.net_edge_bps >= standard:
            return True, standard, "STANDARD"
        adaptive_enabled = bool(
            getattr(self.config, "strategy_adaptive_edge_enabled", True)
        )
        floor = min(
            self._policy_value(
                strategy_parameters,
                "strategy_adaptive_edge_floor_bps",
                getattr(self.config, "strategy_adaptive_edge_floor_bps", 15.0),
            ),
            standard,
        )
        min_conf = self._policy_value(
            strategy_parameters,
            "strategy_adaptive_min_confidence",
            getattr(self.config, "strategy_adaptive_min_confidence", 0.75),
        )
        cost_ratio = self._policy_value(
            strategy_parameters,
            "strategy_adaptive_cost_ratio",
            getattr(self.config, "strategy_adaptive_cost_ratio", 1.10),
        )
        economically_supported = (
            signal.expected_cost_bps > 0
            and signal.expected_return_bps >= signal.expected_cost_bps * cost_ratio
        )
        if (
            adaptive_enabled
            and calibrated_confidence >= min_conf
            and signal.net_edge_bps >= floor
            and economically_supported
        ):
            return True, floor, "ADAPTIVE"
        return False, standard, "STANDARD"

    @staticmethod
    def _current_position(instrument: Instrument, portfolio: PortfolioState) -> D:
        # A Kraken Spot Margin position is an independent exposure leg; do not
        # let an offsetting ordinary wallet holding hide its direction or sizing.
        if instrument.symbol in portfolio.spot_margin_position_eur:
            return portfolio.spot_margin_position_eur[instrument.symbol]
        return portfolio.positions.get(instrument.symbol, D("0"))

    @staticmethod
    def _direction_available(instrument: Instrument, direction: Direction) -> bool:
        return (
            instrument.long_available
            if direction == Direction.LONG
            else instrument.short_available
        )

    def _candidate_signals(
        self,
        instrument: Instrument,
        long_signal: Signal,
        short_signal: Signal,
        scale: D,
        *,
        current: D,
        strategy_parameters: dict[str, Any] | None = None,
    ) -> list[tuple[Signal, D, str]]:
        result: list[tuple[Signal, D, str]] = []
        min_confidence = self._policy_value(
            strategy_parameters, "strategy_min_confidence", self.config.strategy_min_confidence
        )
        for signal in (long_signal, short_signal):
            calibrated = max(D("0"), min(D("1"), signal.confidence * scale))
            if calibrated < min_confidence:
                continue
            # Opening a new position must respect the exchange-reported direction.
            # Reductions are handled separately and are allowed to remove risk.
            if current == 0 and not self._direction_available(instrument, signal.direction):
                continue
            ok, threshold, tier = self._edge_policy(signal, calibrated, strategy_parameters)
            if ok:
                result.append((signal, threshold, tier))
        return result

    def _trade_plan(
        self,
        instrument: Instrument,
        portfolio: PortfolioState,
        signal: Signal,
        calibrated_confidence: D,
        min_cost_eur: D,
        force_flatten: bool = False,
    ) -> dict[str, Any]:
        current = self._current_position(instrument, portfolio)
        desired = (
            D("0")
            if force_flatten and current != 0
            else self._target_position(portfolio.equity_eur, calibrated_confidence, signal.direction)
        )
        reversal = (
            not force_flatten
            and current != 0
            and ((current > 0 and desired < 0) or (current < 0 and desired > 0))
        )
        if reversal:
            # Reverse in two safe stages: first flatten, then wait for a fresh cycle.
            desired = D("0")
        delta = desired - current
        trade_notional = abs(delta)
        reducing = current != 0 and abs(desired) < abs(current)
        execution_direction = (
            Direction.LONG if delta > 0 else Direction.SHORT if delta < 0 else None
        )
        reduce_only = reducing and execution_direction is not None
        return {
            "current_position_eur": current,
            "target_position_eur": desired,
            "trade_notional_eur": trade_notional,
            "execution_direction": execution_direction,
            "reduce_only": reduce_only,
            "reversal_to_flat": reversal,
            # A reduction below exchange minimums still needs a Decision so runtime
            # can log the retained residual as DUST instead of silently ignoring it.
            "balanced": trade_notional < min_cost_eur and not reduce_only,
        }

    def _held_position_needs_exit(
        self,
        signal: Signal,
        scale: D,
        strategy_parameters: dict[str, Any] | None = None,
    ) -> bool:
        """Exit held risk when its signal no longer clears entry-quality economics.

        A merely positive edge is not enough to justify continuing to pay spread,
        fees and (where applicable) financing. Use the same confidence and edge
        floors as entry selection, plus a hard expected-return-versus-cost check.
        """
        calibrated = max(D("0"), min(D("1"), signal.confidence * scale))
        ok, _, _ = self._edge_policy(signal, calibrated, strategy_parameters)
        cost_ratio = max(
            D("1"),
            self._policy_value(
                strategy_parameters,
                "strategy_adaptive_cost_ratio",
                getattr(self.config, "strategy_adaptive_cost_ratio", 1.10),
            ),
        )
        required_return = signal.expected_cost_bps * cost_ratio
        return (
            not ok
            or signal.expected_return_bps < required_return
            or signal.net_edge_bps <= D("0")
        )

    def rejection_reason(
        self,
        instrument: Instrument,
        long_signal: Signal,
        short_signal: Signal,
        portfolio: PortfolioState,
        model_parameters: dict[str, Any] | None = None,
        min_cost_eur: D | None = None,
        strategy_parameters: dict[str, Any] | None = None,
    ) -> str:
        scale = self._confidence_scale(model_parameters)
        effective_min_cost = min_cost_eur if min_cost_eur is not None else instrument.min_cost
        current = self._current_position(instrument, portfolio)
        if current != 0:
            held_signal = long_signal if current > 0 else short_signal
            if self._held_position_needs_exit(held_signal, scale, strategy_parameters):
                return "REBALANCE_EXIT"

        candidates = self._candidate_signals(
            instrument, long_signal, short_signal, scale, current=current,
            strategy_parameters=strategy_parameters,
        )
        if not candidates and current != 0:
            held_signal = long_signal if current > 0 else short_signal
            if held_signal.confidence * scale < self._policy_value(
                strategy_parameters, "strategy_min_confidence", self.config.strategy_min_confidence
            ):
                return "MIN_CONFIDENCE"
            return "TARGET_BALANCED"

        if not candidates:
            standard_edge = self._policy_value(strategy_parameters, "strategy_min_edge_bps", self.config.strategy_min_edge_bps)
            standard_conf = self._policy_value(strategy_parameters, "strategy_min_confidence", self.config.strategy_min_confidence)
            standard_signals = [
                signal for signal in (long_signal, short_signal)
                if signal.net_edge_bps >= standard_edge
            ]
            if standard_signals and not any(
                signal.confidence * scale >= standard_conf for signal in standard_signals
            ):
                return "MIN_CONFIDENCE"
            raw_directional = [
                signal for signal in (long_signal, short_signal)
                if self._direction_available(instrument, signal.direction)
            ]
            adaptive_floor = min(
                self._policy_value(
                    strategy_parameters,
                    "strategy_adaptive_edge_floor_bps",
                    getattr(self.config, "strategy_adaptive_edge_floor_bps", 15.0),
                ),
                standard_edge,
            )
            if any(signal.net_edge_bps >= adaptive_floor for signal in raw_directional):
                return "ECONOMIC_EDGE_GUARD"
            if any(
                signal.net_edge_bps >= standard_edge
                and signal.confidence * scale >= standard_conf
                for signal in (long_signal, short_signal)
            ):
                return "INSTRUMENT_DIRECTION"
            if any(
                signal.net_edge_bps >= adaptive_floor
                for signal in (long_signal, short_signal)
            ) and not any(
                self._direction_available(instrument, signal.direction)
                for signal in (long_signal, short_signal)
            ):
                return "INSTRUMENT_DIRECTION"
            return "MIN_EDGE"

        best = max(candidates, key=lambda item: (item[0].net_edge_bps, item[0].confidence))
        signal, _, _ = best
        calibrated_confidence = max(D("0"), min(D("1"), signal.confidence * scale))
        plan = self._trade_plan(
            instrument, portfolio, signal, calibrated_confidence, effective_min_cost
        )
        if plan["trade_notional_eur"] <= 0 or plan["balanced"]:
            return "TARGET_BALANCED"
        if (
            effective_min_cost > 0
            and plan["trade_notional_eur"] < effective_min_cost
            and not plan["reduce_only"]
        ):
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
        strategy_parameters: dict[str, Any] | None = None,
    ) -> Decision | None:
        scale = self._confidence_scale(model_parameters)
        effective_min_cost = min_cost_eur if min_cost_eur is not None else instrument.min_cost
        current = self._current_position(instrument, portfolio)

        candidates = self._candidate_signals(
            instrument, long_signal, short_signal, scale, current=current,
            strategy_parameters=strategy_parameters,
        )
        force_flatten = False
        edge_tier = "STANDARD"
        edge_threshold = self._policy_value(strategy_parameters, "strategy_min_edge_bps", self.config.strategy_min_edge_bps)
        held_signal = long_signal if current > 0 else short_signal
        held_needs_exit = current != 0 and self._held_position_needs_exit(
            held_signal, scale, strategy_parameters
        )
        # A stale held-position signal must take priority over position-size
        # targeting. Otherwise the sizing branch can keep an unintended residual.
        if held_needs_exit:
            signal = held_signal
            force_flatten = True
        elif candidates:
            candidates.sort(
                key=lambda item: (item[0].net_edge_bps, item[0].confidence),
                reverse=True,
            )
            signal, edge_threshold, edge_tier = candidates[0]
        elif current != 0:
            return None
        else:
            return None

        calibrated_confidence = max(
            D("0.0"), min(D("1.0"), signal.confidence * scale)
        )
        plan = self._trade_plan(
            instrument,
            portfolio,
            signal,
            calibrated_confidence,
            effective_min_cost,
            force_flatten=force_flatten,
        )
        if force_flatten and current != 0:
            # Preserve the explicit two-stage reversal state: flatten now, wait
            # for a fresh cycle before considering a new order in the other direction.
            plan["reversal_to_flat"] = any(
                (current > 0 and candidate[0].direction == Direction.SHORT)
                or (current < 0 and candidate[0].direction == Direction.LONG)
                for candidate in candidates
            )
        if plan["trade_notional_eur"] <= 0 or plan["balanced"]:
            return None
        if (
            effective_min_cost > 0
            and plan["trade_notional_eur"] < effective_min_cost
            and not plan["reduce_only"]
        ):
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
            "rebalance_action": (
                "FLATTEN_NEGATIVE_EDGE"
                if force_flatten and signal.net_edge_bps <= D("0")
                else "FLATTEN_STALE_EDGE" if force_flatten else ""
            ),
            "rebalance_reason": (
                "held_position_net_edge_non_positive"
                if force_flatten and signal.net_edge_bps <= D("0")
                else "held_position_failed_current_edge_or_confidence_or_cost_guard"
                if force_flatten else ""
            ),
            "min_cost_eur": str(effective_min_cost),
            "edge_threshold_bps": str(edge_threshold),
            "edge_tier": edge_tier,
            "strategy_policy_version": str((strategy_parameters or {}).get("policy_version", "CONFIG_DEFAULTS")),
            "strategy_policy_parameters": {
                key: str(value)
                for key, value in (strategy_parameters or {}).items()
                if key in {
                    "strategy_min_edge_bps", "strategy_min_confidence",
                    "strategy_adaptive_edge_floor_bps", "strategy_adaptive_min_confidence",
                    "strategy_adaptive_cost_ratio", "signal_weight_trend",
                    "signal_weight_return_5", "signal_weight_return_15",
                    "signal_weight_return_60", "signal_weight_return_240",
                    "signal_weight_news", "signal_weight_gemini",
                    "signal_cost_volatility_multiplier",
                    "signal_quality_spread_scale_bps",
                    "signal_quality_liquidity_scale",
                    "signal_confidence_return_scale_bps",
                    "signal_confidence_volatility_scale",
                }
            },
            "legacy_confidence_scale_ignored": (
                abs(_legacy_scale(model_parameters) - D("1")) > D("0.000001")
            ),
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


def _legacy_scale(parameters: dict[str, Any] | None) -> D:
    try:
        return D(str((parameters or {}).get("confidence_scale", "1")))
    except Exception:
        return D("1")
