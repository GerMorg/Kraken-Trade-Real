from __future__ import annotations

import hashlib
import json
import math
import time
from itertools import product
from typing import Any


class LearningEngine:
    """Experience-driven learning with separate probability and execution-policy models.

    Calibration changes only the probability stored for future forecasts. Strategy
    thresholds are evaluated on cost-adjusted outcomes of saved LONG and SHORT signal
    candidates and may be promoted only after chronological hold-out validation.
    Absolute portfolio, leverage, daily-loss and drawdown limits are not tunable here.
    """

    CALIBRATION_FAMILY = "probability_calibration"
    POLICY_FAMILY = "strategy_policy"
    MIN_CALIBRATION_SAMPLES = 100
    MIN_POLICY_OBSERVATIONS = 300
    MIN_POLICY_BUCKETS = 60
    MIN_VALIDATION_BUCKETS = 20
    MIN_VALIDATION_TRADES = 30
    MAX_RESEARCH_ROWS = 50000

    def __init__(
        self,
        db: Any,
        calibration: Any,
        registry: Any,
        research: Any,
        auto_promotion_enabled: bool = True,
        *,
        auto_calibration_enabled: bool = True,
        validation_interval_hours: int = 24,
        lookback_days: int = 365,
        policy_defaults: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.calibration = calibration
        self.registry = registry
        self.research = research
        self.auto_promotion_enabled = bool(auto_promotion_enabled)
        self.auto_calibration_enabled = bool(auto_calibration_enabled)
        self.validation_interval_hours = max(1, int(validation_interval_hours))
        self.lookback_days = max(1, int(lookback_days))
        self.policy_defaults = {
            "strategy_min_edge_bps": 25.0,
            "strategy_min_confidence": 0.58,
            "strategy_adaptive_edge_floor_bps": 15.0,
            "strategy_adaptive_min_confidence": 0.75,
            "strategy_adaptive_cost_ratio": 1.10,
            "strategy_adaptive_edge_enabled": True,
            "risk_max_position_pct": 15.0,
            "risk_max_gross_pct": 80.0,
            "risk_max_net_pct": 50.0,
            "risk_max_open_positions": 3,
        }
        self.policy_defaults.update(policy_defaults or {})

    def record_cycle(
        self, cycle_id: str, decisions: int, orders: int, blockers: list[str]
    ) -> None:
        # Only the final cycle summary calls this method. Per-symbol rejections are
        # recorded as signal/policy feedback instead of false cycle-level NO_TRADE events.
        self.db.learning_event(
            "CYCLE_OUTCOME",
            cycle_id,
            {"decisions": decisions, "orders": orders, "blockers": blockers},
        )
        if orders == 0:
            self.db.learning_event(
                "NO_TRADE", cycle_id, {"reason": "no executable order", "decisions": decisions}
            )

    def record_order_outcome(self, client_order_id: str, payload: dict) -> None:
        self.db.learning_event("ORDER_OUTCOME", client_order_id, payload)

    def calibrate(self, pairs: list[tuple[float, bool]], model_version: str) -> dict:
        metrics = self.calibration.evaluate(pairs)
        self.db.execute(
            """INSERT INTO calibrations(
               created_at,model_version,sample_count,brier_score,ece,bins_json
            ) VALUES(strftime('%s','now'),?,?,?,?,?)""",
            (
                model_version, metrics["sample_count"], metrics["brier"], metrics["ece"],
                json.dumps(metrics["bins"]),
            ),
        )
        return metrics

    def _metadata_timestamp(self, key: str) -> float | None:
        row = self.db.one("SELECT value FROM metadata WHERE key=?", (key,))
        if not row:
            return None
        try:
            return float(row["value"])
        except (TypeError, ValueError):
            return None

    def _mark_attempt(self, key: str, now: float) -> None:
        self.db.execute(
            """INSERT INTO metadata(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(float(now))),
        )

    def _interval_due(self, key: str, now: float) -> bool:
        previous = self._metadata_timestamp(key)
        return previous is None or now - previous >= self.validation_interval_hours * 3600

    @staticmethod
    def _bounded_probability(raw_confidence: Any, scale: float) -> float:
        try:
            raw = float(raw_confidence)
        except (TypeError, ValueError):
            raw = 0.5
        raw = max(0.0, min(1.0, raw))
        return max(0.01, min(0.99, raw * max(0.5, min(1.5, float(scale)))))

    def _probability_feedback(self, now: float) -> dict[str, Any]:
        cutoff = now - self.lookback_days * 86400
        current_version = self.registry.active(self.CALIBRATION_FAMILY)
        current_params = self.registry.parameters(
            current_version, family=self.CALIBRATION_FAMILY
        )
        current_scale = max(
            0.5, min(1.5, float(current_params.get("probability_scale", 1.0)))
        )
        rows = self.db.query(
            """SELECT bucket,confidence,success,created_at
               FROM signal_observations
               WHERE outcome_status='SETTLED' AND success IS NOT NULL
                 AND created_at>=?
               ORDER BY bucket DESC,created_at DESC LIMIT ?""",
            (cutoff, self.MAX_RESEARCH_ROWS),
        )
        rows.sort(key=lambda row: (int(row["bucket"]), float(row["created_at"])))
        buckets = sorted({int(row["bucket"]) for row in rows})
        if len(rows) < self.MIN_CALIBRATION_SAMPLES or len(buckets) < 30:
            return {
                "status": "INSUFFICIENT_DATA", "samples": len(rows),
                "buckets": len(buckets), "minimum_samples": self.MIN_CALIBRATION_SAMPLES,
            }

        split = max(1, min(len(buckets) - 1, int(len(buckets) * 0.70)))
        training_buckets = set(buckets[:split])
        validation_buckets = set(buckets[split:])
        training = [row for row in rows if int(row["bucket"]) in training_buckets]
        validation = [row for row in rows if int(row["bucket"]) in validation_buckets]
        if len(validation) < 30:
            return {
                "status": "INSUFFICIENT_VALIDATION", "samples": len(rows),
                "validation_samples": len(validation), "validation_buckets": len(validation_buckets),
            }

        def pairs(values: list[dict[str, Any]], scale: float) -> list[tuple[float, bool]]:
            return [
                (self._bounded_probability(row["confidence"], scale), bool(row["success"]))
                for row in values
            ]

        current_training = self.calibration.evaluate(pairs(training, current_scale))
        best_scale = current_scale
        best_training_brier = float(current_training["brier"])
        for scale in (0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30):
            result = self.calibration.evaluate(pairs(training, scale))
            if float(result["brier"]) < best_training_brier:
                best_training_brier = float(result["brier"])
                best_scale = scale

        baseline = self.calibration.evaluate(pairs(validation, current_scale))
        candidate = self.calibration.evaluate(pairs(validation, best_scale))
        improvement = float(baseline["brier"]) - float(candidate["brier"])
        ece_change = float(candidate["ece"]) - float(baseline["ece"])
        candidate_version = ""
        promoted = False
        if (
            best_scale != current_scale
            and improvement >= 0.005
            and ece_change <= 0.02
        ):
            digest = hashlib.sha256(
                json.dumps(
                    {"parent": current_version, "scale": best_scale, "buckets": buckets[-1]},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:10]
            candidate_version = f"prob-cal-{int(now)}-{digest}"
            parameters = {
                **current_params,
                "kind": "probability_calibration",
                "probability_scale": best_scale,
                "method": "chronological_70_30_holdout",
                "training_buckets": len(training_buckets),
                "validation_buckets": len(validation_buckets),
            }
            metrics = {
                "samples": len(validation),
                "brier": float(candidate["brier"]),
                "ece": float(candidate["ece"]),
                "parent_brier": float(baseline["brier"]),
                "parent_ece": float(baseline["ece"]),
                "improvement": improvement,
                "ece_change": ece_change,
                "selected_scale": best_scale,
                "validation_buckets": len(validation_buckets),
                "validation_method": "chronological_70_30_holdout",
            }
            self.registry.register_candidate(
                candidate_version, self.CALIBRATION_FAMILY, current_version, parameters, metrics
            )
            self.db.learning_event("MODEL_CANDIDATE", candidate_version, metrics)
            if self.auto_promotion_enabled:
                promoted = self.registry.promote(
                    candidate_version, min_improvement=0.005, min_samples=30
                )

        return {
            "status": "OK", "samples": len(rows), "training_samples": len(training),
            "validation_samples": len(validation), "buckets": len(buckets),
            "current_scale": current_scale, "candidate_scale": best_scale,
            "brier": float(baseline["brier"]), "candidate_brier": float(candidate["brier"]),
            "ece": float(baseline["ece"]), "candidate_ece": float(candidate["ece"]),
            "improvement": improvement, "ece_change": ece_change,
            "candidate_version": candidate_version, "promoted": promoted,
            "family": self.CALIBRATION_FAMILY,
        }

    def _policy_candidates(self, current: dict[str, Any]) -> list[dict[str, float]]:
        edge_base = max(
            0.1, float(self.policy_defaults.get("strategy_min_edge_bps", 25.0))
        )
        edge_floor = min(
            edge_base,
            max(0.1, float(self.policy_defaults.get("strategy_adaptive_edge_floor_bps", 15.0))),
        )
        conf_base = max(
            0.0, min(1.0, float(self.policy_defaults.get("strategy_min_confidence", 0.58)))
        )
        ratio_base = max(
            1.0, min(3.0, float(self.policy_defaults.get("strategy_adaptive_cost_ratio", 1.10)))
        )
        edge_values = sorted({
            round(max(edge_floor, edge_base * 0.80), 4),
            round(edge_base, 4),
            round(min(max(edge_base * 1.50, edge_base + 1.0), edge_base * 1.20), 4),
        })
        confidence_values = sorted({
            round(max(0.0, conf_base - 0.05), 4),
            round(conf_base, 4),
            round(min(1.0, conf_base + 0.05), 4),
        })
        ratio_values = sorted({
            round(max(1.0, ratio_base - 0.10), 4),
            round(ratio_base, 4),
            round(min(1.5, ratio_base + 0.15), 4),
        })
        candidates = [
            {
                "strategy_min_edge_bps": edge,
                "strategy_min_confidence": confidence,
                "strategy_adaptive_cost_ratio": ratio,
            }
            for edge, confidence, ratio in product(
                edge_values, confidence_values, ratio_values
            )
        ]
        active_candidate = {
            "strategy_min_edge_bps": float(current["strategy_min_edge_bps"]),
            "strategy_min_confidence": float(current["strategy_min_confidence"]),
            "strategy_adaptive_cost_ratio": float(current["strategy_adaptive_cost_ratio"]),
        }
        if active_candidate not in candidates:
            candidates.append(active_candidate)
        return candidates

    def _normalise_policy(self, parameters: dict[str, Any] | None) -> dict[str, float]:
        raw = parameters or {}
        result = {
            "strategy_min_edge_bps": float(raw.get(
                "strategy_min_edge_bps", self.policy_defaults["strategy_min_edge_bps"]
            )),
            "strategy_min_confidence": float(raw.get(
                "strategy_min_confidence", self.policy_defaults["strategy_min_confidence"]
            )),
            "strategy_adaptive_cost_ratio": float(raw.get(
                "strategy_adaptive_cost_ratio",
                self.policy_defaults["strategy_adaptive_cost_ratio"],
            )),
        }
        floor = min(
            float(self.policy_defaults["strategy_min_edge_bps"]),
            float(self.policy_defaults["strategy_adaptive_edge_floor_bps"]),
        )
        result["strategy_min_edge_bps"] = max(
            floor, min(
                max(float(self.policy_defaults["strategy_min_edge_bps"]) * 1.5,
                    float(self.policy_defaults["strategy_min_edge_bps"]) + 1.0),
                result["strategy_min_edge_bps"],
            )
        )
        result["strategy_min_confidence"] = max(
            0.0, min(1.0, result["strategy_min_confidence"])
        )
        result["strategy_adaptive_cost_ratio"] = max(
            1.0, min(3.0, result["strategy_adaptive_cost_ratio"])
        )
        return result

    def _simulate_policy(
        self, rows: list[dict[str, Any]], policy: dict[str, float]
    ) -> dict[str, Any]:
        groups: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (int(row["bucket"]), str(row["venue"]), str(row["symbol"]))
            groups.setdefault(key, []).append(row)

        selected_by_bucket: dict[int, list[dict[str, Any]]] = {}
        min_edge = float(policy["strategy_min_edge_bps"])
        min_confidence = float(policy["strategy_min_confidence"])
        cost_ratio = float(policy["strategy_adaptive_cost_ratio"])
        adaptive_enabled = bool(self.policy_defaults.get("strategy_adaptive_edge_enabled", True))
        adaptive_floor = float(self.policy_defaults.get("strategy_adaptive_edge_floor_bps", 15.0))
        adaptive_confidence = float(self.policy_defaults.get("strategy_adaptive_min_confidence", 0.75))
        for (bucket, _venue, _symbol), candidates in groups.items():
            qualifying = []
            for row in candidates:
                confidence = max(0.0, min(1.0, float(row["confidence"])))
                expected_return = float(row["expected_return_bps"])
                expected_cost = max(0.0, float(row["expected_cost_bps"]))
                edge = expected_return - expected_cost
                if confidence < min_confidence:
                    continue
                cost_supported = expected_return >= expected_cost * cost_ratio
                standard = edge >= min_edge and cost_supported
                adaptive = (
                    adaptive_enabled and confidence >= adaptive_confidence
                    and edge >= adaptive_floor and cost_supported
                )
                if standard or adaptive:
                    qualifying.append((edge, confidence, row))
            if not qualifying:
                continue
            qualifying.sort(key=lambda item: (item[0], item[1]), reverse=True)
            selected_by_bucket.setdefault(bucket, []).append(qualifying[0][2])

        all_buckets = sorted({int(row["bucket"]) for row in rows})
        max_positions = max(1, int(self.policy_defaults.get("risk_max_open_positions", 3)))
        max_position_weight = max(
            0.001, float(self.policy_defaults.get("risk_max_position_pct", 15.0)) / 100.0
        )
        max_gross_weight = max(
            0.001, float(self.policy_defaults.get("risk_max_gross_pct", 80.0)) / 100.0
        )
        max_net_weight = max(
            0.001, float(self.policy_defaults.get("risk_max_net_pct", 50.0)) / 100.0
        )
        account_returns_bps: list[float] = []
        trade_returns_bps: list[float] = []
        for bucket in all_buckets:
            signals = selected_by_bucket.get(bucket, [])
            signals.sort(
                key=lambda row: (
                    float(row["expected_return_bps"]) - float(row["expected_cost_bps"]),
                    float(row["confidence"]),
                ),
                reverse=True,
            )
            signals = signals[:max_positions]
            weighted: list[tuple[float, float]] = []
            for row in signals:
                confidence = max(0.0, min(1.0, float(row["confidence"])))
                weight = max_position_weight * confidence
                sign = 1.0 if str(row["direction"]).upper() == "LONG" else -1.0
                net_return = float(row["net_return_bps"])
                weighted.append((weight * sign, weight * net_return))
                trade_returns_bps.append(net_return)
            gross_weight = sum(abs(signed_weight) for signed_weight, _ in weighted)
            gross_scale = min(1.0, max_gross_weight / gross_weight) if gross_weight else 1.0
            net_weight = sum(signed_weight for signed_weight, _ in weighted) * gross_scale
            net_scale = (
                min(1.0, max_net_weight / abs(net_weight)) if abs(net_weight) > max_net_weight
                else 1.0
            )
            account_returns_bps.append(
                sum(contribution for _, contribution in weighted) * gross_scale * net_scale
            )

        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for value in account_returns_bps:
            equity *= max(0.0, 1.0 + value / 10000.0)
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = min(max_drawdown, equity / peak - 1.0)
        total_return = sum(account_returns_bps)
        utility = total_return - abs(max_drawdown) * 100.0
        return {
            "samples": len(trade_returns_bps),
            "trades": len(trade_returns_bps),
            "validation_buckets": len(all_buckets),
            "validation_trades": len(trade_returns_bps),
            "validation_total_account_return_bps": total_return,
            "validation_mean_account_return_bps": (
                sum(account_returns_bps) / len(account_returns_bps)
                if account_returns_bps else 0.0
            ),
            "validation_mean_net_bps": (
                sum(trade_returns_bps) / len(trade_returns_bps)
                if trade_returns_bps else 0.0
            ),
            "validation_win_rate": (
                sum(value > 0 for value in trade_returns_bps) / len(trade_returns_bps)
                if trade_returns_bps else 0.0
            ),
            "validation_max_drawdown_pct": max_drawdown,
            "validation_utility_bps": utility,
            "account_return_series_bps": account_returns_bps,
        }

    def _strategy_policy_feedback(self, now: float) -> dict[str, Any]:
        cutoff = now - self.lookback_days * 86400
        rows = self.db.query(
            """SELECT bucket,venue,symbol,direction,confidence,expected_return_bps,
                      expected_cost_bps,net_return_bps,success,created_at
               FROM signal_observations
               WHERE outcome_status='SETTLED' AND net_return_bps IS NOT NULL
                 AND created_at>=?
               ORDER BY bucket DESC,created_at DESC LIMIT ?""",
            (cutoff, self.MAX_RESEARCH_ROWS),
        )
        rows.sort(key=lambda row: (int(row["bucket"]), float(row["created_at"])))
        buckets = sorted({int(row["bucket"]) for row in rows})
        if len(rows) < self.MIN_POLICY_OBSERVATIONS or len(buckets) < self.MIN_POLICY_BUCKETS:
            return {
                "status": "INSUFFICIENT_DATA", "samples": len(rows), "buckets": len(buckets),
                "minimum_samples": self.MIN_POLICY_OBSERVATIONS,
                "minimum_buckets": self.MIN_POLICY_BUCKETS,
            }

        policy_version = self.registry.active(self.POLICY_FAMILY)
        active_parameters = self._normalise_policy(
            self.registry.parameters(policy_version, family=self.POLICY_FAMILY)
        )
        split = max(1, min(len(buckets) - 1, int(len(buckets) * 0.70)))
        training_buckets = set(buckets[:split])
        validation_buckets = set(buckets[split:])
        training = [row for row in rows if int(row["bucket"]) in training_buckets]
        validation = [row for row in rows if int(row["bucket"]) in validation_buckets]
        if len(validation_buckets) < self.MIN_VALIDATION_BUCKETS:
            return {
                "status": "INSUFFICIENT_VALIDATION", "samples": len(rows),
                "validation_buckets": len(validation_buckets),
            }

        baseline_training = self._simulate_policy(training, active_parameters)
        best_parameters = active_parameters
        best_training = baseline_training
        for candidate in self._policy_candidates(active_parameters):
            scored = self._simulate_policy(training, candidate)
            if scored["validation_trades"] < self.MIN_VALIDATION_TRADES:
                continue
            if scored["validation_utility_bps"] > best_training["validation_utility_bps"]:
                best_parameters = candidate
                best_training = scored

        baseline_validation = self._simulate_policy(validation, active_parameters)
        candidate_validation = self._simulate_policy(validation, best_parameters)
        improvement = (
            float(candidate_validation["validation_utility_bps"])
            - float(baseline_validation["validation_utility_bps"])
        )
        drawdown_delta = (
            float(candidate_validation["validation_max_drawdown_pct"])
            - float(baseline_validation["validation_max_drawdown_pct"])
        )
        promoted = False
        candidate_version = ""
        eligible = (
            best_parameters != active_parameters
            and candidate_validation["validation_trades"] >= self.MIN_VALIDATION_TRADES
            and candidate_validation["validation_buckets"] >= self.MIN_VALIDATION_BUCKETS
            and candidate_validation["validation_total_account_return_bps"] > 0
            and candidate_validation["validation_mean_account_return_bps"] > 0
            and improvement >= 1.0
            and drawdown_delta >= -0.05
        )
        if eligible:
            digest = hashlib.sha256(
                json.dumps(
                    {"parent": policy_version, "parameters": best_parameters,
                     "validation_last_bucket": max(validation_buckets)},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:10]
            candidate_version = f"strategy-policy-{int(now)}-{digest}"
            metrics = {
                "samples": candidate_validation["validation_trades"],
                "training_samples": best_training["validation_trades"],
                "training_buckets": len(training_buckets),
                "validation_buckets": len(validation_buckets),
                "validation_trades": candidate_validation["validation_trades"],
                "validation_utility_bps": candidate_validation["validation_utility_bps"],
                "validation_total_account_return_bps": candidate_validation[
                    "validation_total_account_return_bps"
                ],
                "validation_mean_account_return_bps": candidate_validation[
                    "validation_mean_account_return_bps"
                ],
                "validation_mean_net_bps": candidate_validation["validation_mean_net_bps"],
                "validation_win_rate": candidate_validation["validation_win_rate"],
                "validation_max_drawdown_pct": candidate_validation[
                    "validation_max_drawdown_pct"
                ],
                "parent_validation_utility_bps": baseline_validation["validation_utility_bps"],
                "parent_validation_total_account_return_bps": baseline_validation[
                    "validation_total_account_return_bps"
                ],
                "parent_validation_max_drawdown_pct": baseline_validation[
                    "validation_max_drawdown_pct"
                ],
                "improvement": improvement,
                "drawdown_delta": drawdown_delta,
                "parameters": best_parameters,
                "method": "chronological_70_30_signal_policy_replay",
            }
            self.registry.register_candidate(
                candidate_version, self.POLICY_FAMILY, policy_version, best_parameters, metrics
            )
            self.db.learning_event("STRATEGY_POLICY_CANDIDATE", candidate_version, metrics)
            if self.auto_promotion_enabled:
                promoted = self.registry.promote(
                    candidate_version, min_improvement=1.0,
                    min_samples=self.MIN_VALIDATION_TRADES,
                )

        result = {
            "status": "CANDIDATE_READY" if eligible else "NO_IMPROVEMENT",
            "samples": len(rows), "buckets": len(buckets),
            "training_buckets": len(training_buckets),
            "validation_buckets": len(validation_buckets),
            "training_trades": best_training["validation_trades"],
            "active_parameters": active_parameters,
            "candidate_parameters": best_parameters,
            "active_validation": {
                key: baseline_validation[key] for key in (
                    "validation_trades", "validation_total_account_return_bps",
                    "validation_mean_account_return_bps", "validation_mean_net_bps",
                    "validation_win_rate", "validation_max_drawdown_pct",
                    "validation_utility_bps",
                )
            },
            "candidate_validation": {
                key: candidate_validation[key] for key in (
                    "validation_trades", "validation_total_account_return_bps",
                    "validation_mean_account_return_bps", "validation_mean_net_bps",
                    "validation_win_rate", "validation_max_drawdown_pct",
                    "validation_utility_bps",
                )
            },
            "improvement": improvement,
            "drawdown_delta": drawdown_delta,
            "candidate_version": candidate_version,
            "promoted": promoted,
            "family": self.POLICY_FAMILY,
        }
        return result

    def process_feedback(self, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        settled = self.db.settle_predictions(now)
        signal_settlement = self.db.settle_signal_observations(now)
        open_row = self.db.one(
            "SELECT COUNT(*) AS n FROM predictions WHERE outcome_status='OPEN'"
        )
        settled_row = self.db.one(
            "SELECT COUNT(*) AS n FROM predictions WHERE outcome_status='SETTLED'"
        )
        open_predictions = int(open_row["n"]) if open_row else 0
        settled_total = int(settled_row["n"]) if settled_row else 0
        result: dict[str, Any] = {
            "settled": settled,
            "settled_total": settled_total,
            "open_predictions": open_predictions,
            "signal_settled": signal_settlement["settled"],
            "signal_unscorable": signal_settlement["unscorable"],
            "auto_calibration_enabled": self.auto_calibration_enabled,
            "auto_promotion_enabled": self.auto_promotion_enabled,
            "validation_interval_hours": self.validation_interval_hours,
            "lookback_days": self.lookback_days,
        }

        if not self.auto_calibration_enabled:
            result["probability_calibration"] = {"status": "DISABLED"}
            result["strategy_policy"] = {"status": "DISABLED"}
            self.db.learning_event("LEARNING_FEEDBACK", "decision", result)
            return result

        calibration_key = "learning_last_probability_calibration_attempt"
        if self._interval_due(calibration_key, now):
            self._mark_attempt(calibration_key, now)
            try:
                result["probability_calibration"] = self._probability_feedback(now)
            except Exception as exc:
                result["probability_calibration"] = {
                    "status": "ERROR", "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                }
        else:
            result["probability_calibration"] = {
                "status": "WAITING_INTERVAL",
                "next_due_at": (self._metadata_timestamp(calibration_key) or now)
                + self.validation_interval_hours * 3600,
            }

        policy_key = "learning_last_strategy_policy_attempt"
        if self._interval_due(policy_key, now):
            self._mark_attempt(policy_key, now)
            cutoff = now - self.lookback_days * 86400
            try:
                result["strategy_policy"] = self._strategy_policy_feedback(now)
                pruned = self.db.prune_signal_observations(cutoff)
                result["signal_observations_pruned"] = pruned
            except Exception as exc:
                result["strategy_policy"] = {
                    "status": "ERROR", "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                }
        else:
            result["strategy_policy"] = {
                "status": "WAITING_INTERVAL",
                "next_due_at": (self._metadata_timestamp(policy_key) or now)
                + self.validation_interval_hours * 3600,
            }

        result["status"] = (
            "OK"
            if result["probability_calibration"].get("status") in {
                "OK", "NO_IMPROVEMENT", "CANDIDATE_READY", "WAITING_INTERVAL"
            }
            else "INSUFFICIENT_DATA"
        )
        self.db.learning_event("LEARNING_FEEDBACK", "decision", result)
        return result
