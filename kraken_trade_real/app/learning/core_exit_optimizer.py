from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from typing import Any

from app.config.adaptive import CORE_DEFAULTS, CORE_EXIT_POLICY_BOUNDS


class CoreExitPolicyOptimizer:
    """Path-replay optimizer for Normal/Core Spot-Margin profit protection.

    It only learns from app-managed, single-order episodes whose exit reason and
    final observed trigger agree with the policy snapshot. Replay is bounded to
    prices/profit observations actually recorded before the real position closed.
    This is a model-selection aid, not a promise of future profitability.
    """

    MIN_EPISODES = 60
    MIN_VALIDATION_EPISODES = 18
    MIN_TRAIN_IMPROVEMENT_BPS = 10.0
    MIN_VALIDATION_IMPROVEMENT_BPS = 5.0
    MIN_COVERAGE = 0.85
    MAX_DRAWDOWN_DETERIORATION = 0.01
    WINDOW_DAYS = 180

    def __init__(self, db: Any, registry: Any, config: Any | None = None) -> None:
        self.db = db
        self.registry = registry
        self.config = config

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _decode_object(cls, value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _policy(cls, value: Any) -> dict[str, float] | None:
        source = value if isinstance(value, dict) else cls._decode_object(value)
        policy: dict[str, float] = {}
        for key, (low, high) in CORE_EXIT_POLICY_BOUNDS.items():
            number = cls._finite(source.get(key))
            if number is None or not low <= number <= high:
                return None
            policy[key] = number
        if (
            policy["strategy_stop_loss_pct"]
            >= policy["strategy_partial_profit_trigger_pct"]
            or policy["strategy_partial_profit_trigger_pct"]
            > policy["strategy_profit_lock_trigger_pct"]
            or policy["strategy_profit_lock_floor_pct"]
            >= policy["strategy_profit_lock_trigger_pct"]
        ):
            return None
        return policy

    @staticmethod
    def _drawdown(outcomes: list[dict[str, Any]]) -> float:
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for outcome in outcomes:
            net_bps = float(outcome["net_bps"])
            equity *= max(0.0001, 1.0 + net_bps / 10000.0)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        return max_drawdown

    def _load_episodes(self, now: float) -> tuple[list[dict[str, Any]], int]:
        rows = self.db.query(
            """SELECT * FROM core_exit_episodes
               WHERE eligible_for_learning=1 AND closed_at IS NOT NULL
                 AND closed_at>=?
                 AND close_reason IN ('STOP_LOSS_EXIT','TRAILING_PROFIT_EXIT')
               ORDER BY closed_at ASC,episode_id ASC LIMIT 1800""",
            (now - self.WINDOW_DAYS * 86400,),
        )
        prepared: list[dict[str, Any]] = []
        for row in rows:
            try:
                episode_id = str(row["episode_id"])
                opened_at = float(row["opened_at"])
                closed_at = float(row["closed_at"])
                direction = str(row.get("direction") or "").upper()
                basis = float(row.get("initial_basis_eur") or 0.0)
                lot_count = int(row.get("open_lot_count") or 0)
                order_ids = json.loads(row.get("open_order_ids_json") or "[]")
                decision_ids = json.loads(row.get("opening_decision_ids_json") or "[]")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not episode_id
                or direction not in {"LONG", "SHORT"}
                or closed_at <= opened_at
                or basis <= 0
                or lot_count != 1
                or not isinstance(order_ids, list)
                or len(set(str(item) for item in order_ids if item)) != 1
                or not isinstance(decision_ids, list)
                or len(set(str(item) for item in decision_ids if item)) != 1
            ):
                continue

            decision_id = str(next((item for item in decision_ids if item), ""))
            decision = self.db.one(
                """SELECT expected_cost_bps,strategy_version
                   FROM decisions WHERE decision_id=?""",
                (decision_id,),
            )
            if not decision or "tactical" in str(decision.get("strategy_version") or "").lower():
                continue
            cost_bps = self._finite(decision.get("expected_cost_bps"))
            if cost_bps is None or cost_bps <= 0:
                continue

            path_rows = self.db.core_exit_path(episode_id, until_timestamp=closed_at)
            if len(path_rows) < 3:
                continue
            path: list[dict[str, Any]] = []
            valid_path = True
            for index, point in enumerate(path_rows):
                observed_at = self._finite(point.get("observed_at"))
                profit_pct = self._finite(point.get("profit_pct"))
                peak_profit_pct = self._finite(point.get("peak_profit_pct"))
                point_basis = self._finite(point.get("basis_eur"))
                quantity = self._finite(point.get("quantity"))
                quality = self._decode_object(point.get("quality_json"))
                policy_snapshot = self._policy(point.get("policy_snapshot_json"))
                if (
                    observed_at is None
                    or profit_pct is None
                    or peak_profit_pct is None
                    or point_basis is None
                    or quantity is None
                    or observed_at < opened_at
                    or observed_at > closed_at
                    or point_basis <= 0
                    or quantity <= 0
                    or quality.get("open_positions_read_ok") is not True
                    or quality.get("pnl_present") is not True
                    or quality.get("basis_positive") is not True
                    or quality.get("quantity_positive") is not True
                    or quality.get("eligible_for_learning") is not True
                    or not policy_snapshot
                ):
                    valid_path = False
                    break
                # A pending order can distort the apparent exit point if a position
                # remains open while execution/reconciliation is unresolved. Permit
                # a pending reduce order only on the final point, which can be the
                # same-cycle snapshot taken after submitting the actual exit.
                if index < len(path_rows) - 1 and str(point.get("pending_order_id") or ""):
                    valid_path = False
                    break
                path.append({
                    "observed_at": observed_at,
                    "profit_pct": profit_pct,
                    "peak_profit_pct": peak_profit_pct,
                    "partial_taken": bool(int(point.get("partial_taken") or 0)),
                    "pending_order_id": str(point.get("pending_order_id") or ""),
                    "policy": policy_snapshot,
                })
            if not valid_path or len(path) < 3:
                continue
            times = [float(point["observed_at"]) for point in path]
            if any(right <= left for left, right in zip(times, times[1:])):
                continue
            intervals = [right - left for left, right in zip(times, times[1:])]
            typical_interval = statistics.median(intervals) if intervals else 60.0
            tolerance = max(120.0, 2.0 * typical_interval)

            episode_policy = self._policy(row.get("exit_policy_snapshot_json"))
            if episode_policy is None:
                episode_policy = path[0]["policy"]
            prepared.append({
                "key": episode_id,
                "episode_id": episode_id,
                "opened_at": opened_at,
                "closed_at": closed_at,
                "direction": direction,
                "close_reason": str(row.get("close_reason") or ""),
                "basis_eur": basis,
                "cost_bps": cost_bps,
                "path": path,
                "typical_interval": typical_interval,
                "tolerance": tolerance,
                "episode_policy": episode_policy,
            })
        prepared.sort(key=lambda item: (float(item["closed_at"]), str(item["episode_id"])))
        return prepared, len(rows)

    @classmethod
    def _simulate(
        cls,
        episode: dict[str, Any],
        policy_override: dict[str, float] | None = None,
    ) -> dict[str, Any] | None:
        """Replay the exit process and value partial realization against initial basis."""
        path = episode.get("path")
        if not isinstance(path, list) or not path:
            return None
        remaining = 1.0
        realized_bps = 0.0
        partial_taken = False
        partial_at: float | None = None
        peak_pct = 0.0
        last_pct = 0.0

        for point in path:
            timestamp = CoreExitPolicyOptimizer._finite(point.get("observed_at"))
            profit_pct = CoreExitPolicyOptimizer._finite(point.get("profit_pct"))
            observed_peak = CoreExitPolicyOptimizer._finite(point.get("peak_profit_pct"))
            if timestamp is None or profit_pct is None or observed_peak is None:
                return None
            policy = policy_override or point.get("policy") or episode.get("episode_policy")
            policy = cls._policy(policy)
            if policy is None:
                return None
            peak_pct = max(peak_pct, profit_pct, observed_peak)
            last_pct = profit_pct

            stop_loss = policy["strategy_stop_loss_pct"]
            lock_activation = policy["strategy_profit_lock_trigger_pct"]
            giveback = policy["strategy_profit_giveback_pct"]
            floor = policy["strategy_profit_lock_floor_pct"]
            partial_trigger = policy["strategy_partial_profit_trigger_pct"]
            partial_fraction = policy["strategy_partial_profit_fraction_pct"] / 100.0

            # Match PositionProfitProtection.apply ordering: hard position stop,
            # then trailing profit-lock exit, then one-time partial profit-taking.
            reason = ""
            if profit_pct <= -stop_loss:
                reason = "STOP_LOSS_EXIT"
            else:
                lock_threshold = max(
                    floor,
                    max(0.0, peak_pct) * (1.0 - giveback / 100.0),
                )
                if peak_pct >= lock_activation and profit_pct <= lock_threshold:
                    reason = "TRAILING_PROFIT_EXIT"
            if reason:
                gross_bps = realized_bps + remaining * profit_pct * 100.0
                return {
                    "gross_bps": gross_bps,
                    "net_bps": gross_bps - float(episode["cost_bps"]),
                    "reason": reason,
                    "exit_at": timestamp,
                    "partial_taken": partial_taken,
                    "partial_at": partial_at,
                }

            if not partial_taken and profit_pct >= partial_trigger:
                realized_bps += remaining * partial_fraction * profit_pct * 100.0
                remaining *= 1.0 - partial_fraction
                partial_taken = True
                partial_at = timestamp

        # A candidate that would not have triggered before the historical close
        # is valued at the last observed point; the replay never extrapolates past
        # the real episode, and treats the actual close as a forced terminal exit.
        gross_bps = realized_bps + remaining * last_pct * 100.0
        return {
            "gross_bps": gross_bps,
            "net_bps": gross_bps - float(episode["cost_bps"]),
            "reason": "CENSORED_CLOSE",
            "exit_at": float(path[-1]["observed_at"]),
            "partial_taken": partial_taken,
            "partial_at": partial_at,
        }

    @classmethod
    def _baseline_matches(cls, episode: dict[str, Any], baseline: dict[str, Any]) -> bool:
        path = episode["path"]
        if baseline["reason"] != episode["close_reason"]:
            return False
        last_observation = float(path[-1]["observed_at"])
        if abs(float(baseline["exit_at"]) - last_observation) > float(episode["tolerance"]):
            return False
        observed_partial_at = next(
            (float(point["observed_at"]) for point in path if point["partial_taken"]),
            None,
        )
        simulated_partial_at = baseline.get("partial_at")
        if simulated_partial_at is None:
            return observed_partial_at is None
        if observed_partial_at is None:
            return False
        return abs(float(simulated_partial_at) - observed_partial_at) <= float(episode["tolerance"])

    def _current_policy(self) -> tuple[str, dict[str, Any], dict[str, float]]:
        version = self.registry.active("strategy_core")
        params = dict(CORE_DEFAULTS)
        active = self.registry.parameters(version, family="strategy_core")
        if isinstance(active, dict):
            params.update({key: active[key] for key in CORE_DEFAULTS if key in active})
        policy = self._policy(params)
        if policy is None:
            policy = {
                key: float(CORE_DEFAULTS[key]) for key in CORE_EXIT_POLICY_BOUNDS
            }
            params.update(policy)
        return version, params, policy

    def _mark_evaluated(self, closed_at: float) -> None:
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES('core_exit_policy_last_evaluated_closed_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(float(closed_at)),),
        )

    def optimize(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        last = self.db.one(
            "SELECT value FROM metadata WHERE key='core_exit_policy_last_evaluated_closed_at'"
        )
        last_at: float | None = None
        if last:
            try:
                last_at = float(last.get("value") or 0.0)
            except (TypeError, ValueError):
                last_at = 0.0
            newer = self.db.query(
                """SELECT episode_id FROM core_exit_episodes
                   WHERE eligible_for_learning=1 AND closed_at>?
                     AND close_reason IN ('STOP_LOSS_EXIT','TRAILING_PROFIT_EXIT')""",
                (last_at,),
            )
            if len(newer) < 10:
                return {
                    "status": "WAITING_FOR_NEW_CORE_EXIT_EPISODES",
                    "samples": None,
                    "new_episodes_since_last_evaluation": len(newer),
                    "minimum_new_episodes": 10,
                }

        episodes, raw_count = self._load_episodes(now)
        if not episodes:
            return {
                "status": "NO_VALID_CORE_EXIT_EPISODES",
                "samples": 0,
                "raw_eligible_episodes": raw_count,
            }
        watermark = max(float(item["closed_at"]) for item in episodes)
        current_version, current_params, current_policy = self._current_policy()
        baseline_by_key: dict[str, dict[str, Any]] = {}
        aligned: list[dict[str, Any]] = []
        for episode in episodes:
            baseline = self._simulate(episode)
            if baseline is None or not self._baseline_matches(episode, baseline):
                continue
            episode["baseline"] = baseline
            baseline_by_key[str(episode["key"])] = baseline
            aligned.append(episode)

        if len(aligned) < self.MIN_EPISODES:
            self._mark_evaluated(watermark)
            result = {
                "status": "INSUFFICIENT_BASELINE_ALIGNED_CORE_EXIT_PATHS",
                "samples": len(aligned),
                "valid_paths": len(episodes),
                "raw_eligible_episodes": raw_count,
                "minimum_samples": self.MIN_EPISODES,
            }
            self.db.learning_event("CORE_EXIT_POLICY_EVALUATED", current_version, result)
            return result

        split = int(len(aligned) * 0.70)
        training, validation = aligned[:split], aligned[split:]
        if len(validation) < self.MIN_VALIDATION_EPISODES:
            self._mark_evaluated(watermark)
            return {
                "status": "INSUFFICIENT_CORE_EXIT_VALIDATION",
                "training_samples": len(training),
                "validation_samples": len(validation),
                "minimum_validation_samples": self.MIN_VALIDATION_EPISODES,
            }

        baseline_train = [item["baseline"] for item in training]
        baseline_validation = [item["baseline"] for item in validation]
        baseline_train_mean = sum(float(x["net_bps"]) for x in baseline_train) / len(baseline_train)
        baseline_validation_mean = (
            sum(float(x["net_bps"]) for x in baseline_validation) / len(baseline_validation)
        )
        baseline_train_dd = self._drawdown(baseline_train)
        baseline_validation_dd = self._drawdown(baseline_validation)

        variants: list[tuple[str, dict[str, float]]] = []
        for key, factors in (
            ("strategy_stop_loss_pct", (0.75, 1.25)),
            ("strategy_partial_profit_trigger_pct", (0.80, 1.25)),
            ("strategy_partial_profit_fraction_pct", (0.75, 1.25)),
            ("strategy_profit_lock_trigger_pct", (0.80, 1.25)),
            ("strategy_profit_giveback_pct", (0.75, 1.25)),
            ("strategy_profit_lock_floor_pct", (0.75, 1.25)),
        ):
            low, high = CORE_EXIT_POLICY_BOUNDS[key]
            for factor in factors:
                candidate = dict(current_policy)
                value = min(high, max(low, float(current_policy[key]) * factor))
                if key == "strategy_partial_profit_fraction_pct":
                    value = round(value / 5.0) * 5.0
                candidate[key] = value
                if abs(value - float(current_policy[key])) < 1e-9 or self._policy(candidate) is None:
                    continue
                variants.append((f"{key}={value:g}", candidate))

        selected: tuple[str, dict[str, float], float, float, float] | None = None
        for label, candidate in variants:
            train_results = [
                self._simulate(item, candidate)
                for item in training
            ]
            if any(result is None for result in train_results):
                continue
            outcomes = [result for result in train_results if result is not None]
            coverage = len(outcomes) / len(training)
            mean_net = sum(float(item["net_bps"]) for item in outcomes) / len(outcomes)
            improvement = mean_net - baseline_train_mean
            drawdown = self._drawdown(outcomes)
            if (
                coverage < self.MIN_COVERAGE
                or improvement < self.MIN_TRAIN_IMPROVEMENT_BPS
                or mean_net <= 0
                or drawdown > baseline_train_dd + self.MAX_DRAWDOWN_DETERIORATION
            ):
                continue
            if selected is None or improvement > selected[2]:
                selected = (label, candidate, improvement, drawdown, coverage)

        if selected is None:
            result = {
                "status": "NO_CORE_EXIT_POLICY_PASSED_TRAINING_GATES",
                "samples": len(aligned),
                "training_samples": len(training),
                "validation_samples": len(validation),
                "baseline_training_mean_net_bps": baseline_train_mean,
                "baseline_training_max_drawdown": baseline_train_dd,
            }
            self._mark_evaluated(watermark)
            self.db.learning_event("CORE_EXIT_POLICY_EVALUATED", current_version, result)
            return result

        label, candidate_policy, train_improvement, candidate_train_dd, train_coverage = selected
        validation_results = [
            self._simulate(item, candidate_policy)
            for item in validation
        ]
        candidate_validation = [result for result in validation_results if result is not None]
        validation_coverage = len(candidate_validation) / len(validation)
        if validation_coverage < self.MIN_COVERAGE or len(candidate_validation) < self.MIN_VALIDATION_EPISODES:
            result = {
                "status": "CORE_EXIT_POLICY_REJECTED_VALIDATION_COVERAGE",
                "candidate": label,
                "samples": len(aligned),
                "validation_coverage": validation_coverage,
            }
        else:
            candidate_validation_mean = (
                sum(float(item["net_bps"]) for item in candidate_validation)
                / len(candidate_validation)
            )
            candidate_validation_dd = self._drawdown(candidate_validation)
            validation_improvement = candidate_validation_mean - baseline_validation_mean
            if (
                validation_improvement >= self.MIN_VALIDATION_IMPROVEMENT_BPS
                and candidate_validation_mean > 0
                and candidate_validation_dd
                <= baseline_validation_dd + self.MAX_DRAWDOWN_DETERIORATION
            ):
                candidate_params = dict(current_params)
                candidate_params.update(candidate_policy)
                changed = {
                    key: candidate_policy[key]
                    for key in CORE_EXIT_POLICY_BOUNDS
                    if abs(candidate_policy[key] - current_policy[key]) > 1e-9
                }
                candidate_id = hashlib.sha256(json.dumps({
                    "parent": current_version,
                    "parameters": candidate_params,
                    "last_closed_at": watermark,
                    "samples": len(aligned),
                }, sort_keys=True, default=str).encode()).hexdigest()[:14]
                version = f"core-exit-policy-{candidate_id}"
                metrics = {
                    "kind": "path_replay_core_exit_policy",
                    "samples": len(aligned),
                    "training_samples": len(training),
                    "validation_samples": len(validation),
                    "training_improvement_bps": train_improvement,
                    "validation_improvement_bps": validation_improvement,
                    "training_coverage": train_coverage,
                    "validation_coverage": validation_coverage,
                    "baseline_training_mean_net_bps": baseline_train_mean,
                    "candidate_training_max_drawdown": candidate_train_dd,
                    "baseline_training_max_drawdown": baseline_train_dd,
                    "baseline_validation_mean_net_bps": baseline_validation_mean,
                    "candidate_validation_mean_net_bps": candidate_validation_mean,
                    "baseline_validation_max_drawdown": baseline_validation_dd,
                    "candidate_validation_max_drawdown": candidate_validation_dd,
                    "max_drawdown_deterioration": self.MAX_DRAWDOWN_DETERIORATION,
                    "selected_candidate": label,
                    "changed_parameters": changed,
                    "cost_basis": "opening_decision_expected_cost_bps_estimate",
                    "exchange_fee_estimates_used": False,
                    "counterfactual_scope": "observed path only, forcibly terminated at actual episode close; no future-price extrapolation",
                }
                existing = self.db.one(
                    "SELECT status FROM model_versions WHERE version=?", (version,)
                )
                promoted = False
                if existing is None:
                    self.registry.register_candidate(
                        version, "strategy_core", current_version, candidate_params, metrics
                    )
                    promoted = self.registry.promote_adaptive_policy(
                        version, minimum_samples=self.MIN_EPISODES
                    )
                result = {
                    "status": "PROMOTED" if promoted else "CANDIDATE_NOT_PROMOTED",
                    "samples": len(aligned),
                    "candidate_version": version,
                    "candidate": label,
                    "changed_parameters": changed,
                    "metrics": metrics,
                    "promoted": promoted,
                }
            else:
                result = {
                    "status": "CORE_EXIT_POLICY_REJECTED_HOLDOUT",
                    "candidate": label,
                    "samples": len(aligned),
                    "training_improvement_bps": train_improvement,
                    "validation_improvement_bps": validation_improvement,
                    "baseline_validation_mean_net_bps": baseline_validation_mean,
                    "candidate_validation_mean_net_bps": candidate_validation_mean,
                    "baseline_validation_max_drawdown": baseline_validation_dd,
                    "candidate_validation_max_drawdown": candidate_validation_dd,
                    "validation_coverage": validation_coverage,
                }

        self._mark_evaluated(watermark)
        self.db.learning_event("CORE_EXIT_POLICY_EVALUATED", current_version, result)
        return result
