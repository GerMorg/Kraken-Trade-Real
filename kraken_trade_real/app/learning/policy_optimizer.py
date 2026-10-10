from __future__ import annotations

import json
import math
from collections import defaultdict
from statistics import median
from typing import Any, Iterable


WEIGHT_KEYS = (
    "signal_weight_trend",
    "signal_weight_return_5",
    "signal_weight_return_15",
    "signal_weight_return_60",
    "signal_weight_return_240",
    "signal_weight_news",
    "signal_weight_gemini",
)
POLICY_KEYS = (
    "strategy_min_edge_bps",
    "strategy_min_confidence",
    "strategy_adaptive_edge_floor_bps",
    "strategy_adaptive_min_confidence",
    "strategy_adaptive_cost_ratio",
)
DEFAULT_WEIGHTS = {
    "signal_weight_trend": 18.0,
    "signal_weight_return_5": 4.0,
    "signal_weight_return_15": 5.0,
    "signal_weight_return_60": 6.0,
    "signal_weight_return_240": 3.0,
    "signal_weight_news": 1.0,
    "signal_weight_gemini": 1.0,
}


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return result if math.isfinite(result) else fallback


class StrategyPolicyOptimizer:
    """Bounded walk-forward tuner for signal weights and entry-policy thresholds.

    It only tunes alpha/policy values. Execution controls, loss limits, leverage
    ceilings, kill switches and account permissions are deliberately out of scope.
    """

    MIN_GROUPS = 300
    MIN_TRAIN_TRADES = 30
    MIN_EVALUATION_TRADES = 20
    MIN_VALIDATION_IMPROVEMENT_BPS = 1.0
    MIN_TEST_IMPROVEMENT_BPS = 2.0

    @staticmethod
    def defaults_from_config(config: Any) -> dict[str, float]:
        values = {
            "strategy_min_edge_bps": _number(getattr(config, "strategy_min_edge_bps", 25), 25),
            "strategy_min_confidence": _number(getattr(config, "strategy_min_confidence", 0.58), 0.58),
            "strategy_adaptive_edge_floor_bps": _number(
                getattr(config, "strategy_adaptive_edge_floor_bps", 15), 15
            ),
            "strategy_adaptive_min_confidence": _number(
                getattr(config, "strategy_adaptive_min_confidence", 0.75), 0.75
            ),
            "strategy_adaptive_cost_ratio": _number(
                getattr(config, "strategy_adaptive_cost_ratio", 1.10), 1.10
            ),
        }
        values.update(DEFAULT_WEIGHTS)
        return values

    @staticmethod
    def normalize_profile(
        profile: dict[str, Any] | None, defaults: dict[str, float]
    ) -> dict[str, float]:
        source = dict(defaults)
        source.update(profile or {})
        normalized: dict[str, float] = {}
        for key in WEIGHT_KEYS:
            base = _number(defaults.get(key), DEFAULT_WEIGHTS[key])
            low, high = ((0.0, 1.5) if key in {"signal_weight_news", "signal_weight_gemini"}
                         else (max(0.1, base * 0.5), base * 1.5))
            normalized[key] = min(high, max(low, _number(source.get(key), base)))
        normalized["strategy_min_edge_bps"] = min(
            100.0, max(5.0, _number(source.get("strategy_min_edge_bps"), 25.0))
        )
        normalized["strategy_min_confidence"] = min(
            0.95, max(0.35, _number(source.get("strategy_min_confidence"), 0.58))
        )
        normalized["strategy_adaptive_edge_floor_bps"] = min(
            50.0, max(3.0, _number(source.get("strategy_adaptive_edge_floor_bps"), 15.0))
        )
        normalized["strategy_adaptive_min_confidence"] = min(
            0.98, max(0.40, _number(source.get("strategy_adaptive_min_confidence"), 0.75))
        )
        normalized["strategy_adaptive_cost_ratio"] = min(
            1.8, max(1.0, _number(source.get("strategy_adaptive_cost_ratio"), 1.10))
        )
        return normalized

    @staticmethod
    def grouped(rows: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if str(row.get("outcome_status", "SETTLED")) != "SETTLED":
                continue
            if row.get("net_return_bps") in (None, ""):
                continue
            key = (str(row.get("symbol", "")), int(_number(row.get("horizon_bucket"), 0)))
            groups[key].append(row)
        result = list(groups.values())
        result.sort(
            key=lambda group: min(_number(row.get("created_at")) for row in group)
        )
        return result

    @staticmethod
    def _features(row: dict[str, Any]) -> dict[str, float]:
        raw = row.get("features_json") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): _number(value, 0.0) for key, value in parsed.items()}

    @classmethod
    def _model_values(
        cls, row: dict[str, Any], profile: dict[str, float]
    ) -> tuple[float, float, float]:
        features = cls._features(row)
        direction = str(row.get("direction") or "").upper()
        direction_sign = 1.0 if direction == "LONG" else -1.0 if direction == "SHORT" else 0.0
        directional = (
            features.get("trend", 0.0) * profile["signal_weight_trend"]
            + features.get("return_5", 0.0) * profile["signal_weight_return_5"]
            + features.get("return_15", 0.0) * profile["signal_weight_return_15"]
            + features.get("return_60", 0.0) * profile["signal_weight_return_60"]
            + features.get("return_240", 0.0) * profile["signal_weight_return_240"]
        )
        raw = (
            direction_sign * directional
            + _number(row.get("news_effect_bps")) * profile["signal_weight_news"]
            + _number(row.get("gemini_effect_bps")) * profile["signal_weight_gemini"]
        )
        spread = max(0.0, features.get("spread_bps", 999.0))
        liquidity = max(1.0, features.get("liquidity", 1.0))
        volatility = max(1.0, features.get("volatility", 999.0))
        quality = max(0.0, 1.0 - spread / 200.0) * min(1.0, liquidity / 1000.0)
        expected_return = max(0.0, raw) * quality
        cost = max(0.0, _number(row.get("expected_cost_bps")))
        edge = expected_return - cost
        confidence = min(1.0, max(0.0, 0.5 + raw / 45.0 - volatility / 120.0))
        return expected_return, edge, confidence

    @classmethod
    def score(
        cls, groups: list[list[dict[str, Any]]], profile: dict[str, float]
    ) -> dict[str, float]:
        selected_outcomes: list[float] = []
        for group in groups:
            eligible: list[tuple[float, float, dict[str, Any]]] = []
            for row in group:
                if not bool(int(_number(row.get("direction_available"), 1))):
                    continue
                expected_return, edge, confidence = cls._model_values(row, profile)
                standard = (
                    confidence >= profile["strategy_min_confidence"]
                    and edge >= profile["strategy_min_edge_bps"]
                )
                adaptive = (
                    confidence >= profile["strategy_min_confidence"]
                    and confidence >= profile["strategy_adaptive_min_confidence"]
                    and edge >= min(
                        profile["strategy_adaptive_edge_floor_bps"],
                        profile["strategy_min_edge_bps"],
                    )
                    and expected_return
                    >= max(0.0, _number(row.get("expected_cost_bps")))
                    * profile["strategy_adaptive_cost_ratio"]
                )
                if standard or adaptive:
                    eligible.append((edge, confidence, row))
            if not eligible:
                continue
            # Match live selection: choose the strongest estimated edge, then confidence.
            chosen = max(eligible, key=lambda item: (item[0], item[1]))[2]
            selected_outcomes.append(_number(chosen.get("net_return_bps")))

        count = len(selected_outcomes)
        if not count:
            return {
                "samples": 0.0, "mean_net_bps": -1_000_000.0,
                "median_net_bps": -1_000_000.0, "hit_rate": 0.0,
                "total_net_bps": 0.0, "profit_factor": 0.0,
            }
        positive = sum(value for value in selected_outcomes if value > 0)
        negative = -sum(value for value in selected_outcomes if value < 0)
        return {
            "samples": float(count),
            "mean_net_bps": sum(selected_outcomes) / count,
            "median_net_bps": float(median(selected_outcomes)),
            "hit_rate": sum(value > 0 for value in selected_outcomes) / count,
            "total_net_bps": sum(selected_outcomes),
            "profit_factor": positive / negative if negative > 0 else (999.0 if positive > 0 else 0.0),
        }

    @staticmethod
    def _variants(key: str, center: float, defaults: dict[str, float]) -> list[float]:
        if key in WEIGHT_KEYS:
            base = defaults.get(key, DEFAULT_WEIGHTS.get(key, 1.0))
            if key in {"signal_weight_news", "signal_weight_gemini"}:
                step = 0.25
                low, high = 0.0, 1.5
            else:
                step = max(0.25, abs(base) * 0.25)
                low, high = max(0.1, base * 0.5), base * 1.5
        elif key in {"strategy_min_edge_bps", "strategy_adaptive_edge_floor_bps"}:
            step = 5.0
            low, high = (5.0, 100.0) if key == "strategy_min_edge_bps" else (3.0, 50.0)
        elif key in {"strategy_min_confidence", "strategy_adaptive_min_confidence"}:
            step = 0.05
            low, high = (0.35, 0.95) if key == "strategy_min_confidence" else (0.40, 0.98)
        else:
            step, low, high = 0.10, 1.0, 1.8
        values = {min(high, max(low, center - step)), min(high, max(low, center)),
                  min(high, max(low, center + step))}
        values.add(min(high, max(low, defaults.get(key, center))))
        return sorted(values)

    def fit(
        self,
        observations: list[dict[str, Any]],
        defaults: dict[str, float],
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        groups = self.grouped(observations)
        if len(groups) < self.MIN_GROUPS:
            return {
                "status": "INSUFFICIENT_DATA",
                "groups": len(groups),
                "minimum_groups": self.MIN_GROUPS,
                "promoted": False,
            }
        defaults_profile = self.normalize_profile(defaults, defaults)
        baseline = self.normalize_profile(current, defaults_profile)
        train_end = max(1, int(len(groups) * 0.60))
        validation_end = max(train_end + 1, int(len(groups) * 0.80))
        train, validation, test = (
            groups[:train_end], groups[train_end:validation_end], groups[validation_end:]
        )
        minimum_train_trades = max(
            self.MIN_TRAIN_TRADES, int(math.ceil(len(train) * 0.05))
        )
        profile = dict(baseline)
        coordinate_keys = (*WEIGHT_KEYS, *POLICY_KEYS)
        for key in coordinate_keys:
            best_profile = dict(profile)
            best_score = self.score(train, profile)
            best_rank = (
                best_score["mean_net_bps"]
                if best_score["samples"] >= minimum_train_trades
                else -1_000_000.0
            )
            for candidate_value in self._variants(key, profile[key], defaults_profile):
                candidate = dict(profile)
                candidate[key] = candidate_value
                measured = self.score(train, candidate)
                rank = (
                    measured["mean_net_bps"]
                    if measured["samples"] >= minimum_train_trades
                    else -1_000_000.0
                )
                if rank > best_rank + 1e-9:
                    best_profile, best_score, best_rank = candidate, measured, rank
            profile = best_profile

        baseline_validation = self.score(validation, baseline)
        candidate_validation = self.score(validation, profile)
        baseline_test = self.score(test, baseline)
        candidate_test = self.score(test, profile)
        stable_samples = (
            baseline_validation["samples"] >= self.MIN_EVALUATION_TRADES
            and candidate_validation["samples"] >= self.MIN_EVALUATION_TRADES
            and baseline_test["samples"] >= self.MIN_EVALUATION_TRADES
            and candidate_test["samples"] >= self.MIN_EVALUATION_TRADES
        )
        validation_improvement = (
            candidate_validation["mean_net_bps"] - baseline_validation["mean_net_bps"]
        )
        test_improvement = candidate_test["mean_net_bps"] - baseline_test["mean_net_bps"]
        changed = any(abs(profile[key] - baseline[key]) > 1e-9 for key in profile)
        promoted = bool(
            changed
            and stable_samples
            and candidate_validation["mean_net_bps"] > 0
            and candidate_test["mean_net_bps"] > 0
            and validation_improvement >= self.MIN_VALIDATION_IMPROVEMENT_BPS
            and test_improvement >= self.MIN_TEST_IMPROVEMENT_BPS
        )
        return {
            "status": "CANDIDATE_VALIDATED" if promoted else "NO_VALIDATED_IMPROVEMENT",
            "groups": len(groups),
            "training_groups": len(train),
            "validation_groups": len(validation),
            "test_groups": len(test),
            "training_start": self.score(train, baseline),
            "training_candidate": self.score(train, profile),
            "validation_baseline": baseline_validation,
            "validation_candidate": candidate_validation,
            "test_baseline": baseline_test,
            "test_candidate": candidate_test,
            "validation_improvement_bps": validation_improvement,
            "test_improvement_bps": test_improvement,
            "minimum_evaluation_trades": self.MIN_EVALUATION_TRADES,
            "promoted": promoted,
            "parameters": profile,
            "baseline_parameters": baseline,
        }

    def compare_out_of_sample(
        self,
        observations: list[dict[str, Any]],
        candidate: dict[str, Any],
        parent: dict[str, Any],
    ) -> dict[str, Any]:
        groups = self.grouped(observations)
        return {
            "groups": float(len(groups)),
            "candidate": self.score(groups, self.normalize_profile(candidate, candidate)),
            "parent": self.score(groups, self.normalize_profile(parent, parent)),
        }
