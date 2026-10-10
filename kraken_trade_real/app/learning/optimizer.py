from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from app.config.adaptive import CORE_DEFAULTS, TACTICAL_DEFAULTS


class AdaptiveParameterOptimizer:
    """Conservative online policy adaptation from realized Tactical trade outcomes.

    Entry adaptation is a bounded realized-outcome controller. Exit adaptation is
    a separate path replay and is promoted only after chronological holdout gates.
    Neither controller can change the application's hard risk envelope.
    """

    MIN_TRADES = 60
    WINDOW = 30

    def __init__(self, db: Any, registry: Any, config: Any | None = None) -> None:
        self.db = db
        self.registry = registry
        self.config = config

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def optimize_tactical(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        rows = self.db.query(
            """SELECT trade_id,closed_at,net_pnl_eur,fees_eur,exit_reason,detail_json
               FROM tactical_trades
               WHERE closed_at >= ?
               ORDER BY closed_at DESC LIMIT 600""",
            (now - 180 * 86400,),
        )
        rows.reverse()
        if len(rows) < self.MIN_TRADES:
            return {
                "status": "INSUFFICIENT_REALIZED_TRADES",
                "samples": len(rows),
                "minimum_samples": self.MIN_TRADES,
            }

        last_evaluated = self.db.one(
            "SELECT value FROM metadata WHERE key='tactical_policy_last_evaluated_closed_at'"
        )
        if last_evaluated:
            try:
                last_evaluated_at = float(last_evaluated["value"])
            except (TypeError, ValueError):
                last_evaluated_at = 0.0
            new_trade_row = self.db.one(
                "SELECT COUNT(*) AS n FROM tactical_trades WHERE closed_at > ?",
                (last_evaluated_at,),
            )
            new_trade_count = int(new_trade_row["n"]) if new_trade_row else 0
            if new_trade_count < 10:
                return {
                    "status": "WAITING_FOR_NEW_TRADE_DATA",
                    "samples": len(rows),
                    "new_trades_since_last_evaluation": new_trade_count,
                    "minimum_new_trades": 10,
                }

        recent = rows[-self.WINDOW:]
        previous = rows[-2 * self.WINDOW:-self.WINDOW]
        if len(previous) < self.WINDOW:
            return {
                "status": "INSUFFICIENT_COMPARISON_WINDOW",
                "samples": len(rows),
                "validation_samples": len(recent),
            }

        def mark_evaluated() -> None:
            latest_closed_at = float(rows[-1].get("closed_at") or now)
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('tactical_policy_last_evaluated_closed_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(latest_closed_at),),
            )

        def stats(items: list[dict[str, Any]]) -> dict[str, float]:
            pnl = [self._number(row.get("net_pnl_eur")) for row in items]
            return {
                "samples": float(len(pnl)),
                "net_pnl_eur": sum(pnl),
                "mean_net_pnl_eur": sum(pnl) / max(1, len(pnl)),
                "win_rate": sum(1 for value in pnl if value > 0) / max(1, len(pnl)),
                "loss_rate": sum(1 for value in pnl if value < 0) / max(1, len(pnl)),
            }

        before = stats(previous)
        after = stats(recent)
        active_version = self.registry.active("strategy_tactical")
        current = dict(TACTICAL_DEFAULTS)
        current.update(self.registry.parameters(active_version, family="strategy_tactical"))
        candidate = dict(current)
        reason = ""

        # Use the latest completed window as a stability signal. A poor window
        # makes entries more selective and reduces allocation; a good, improving
        # window only cautiously relaxes entry filters. Exposure limits never rise.
        if after["mean_net_pnl_eur"] < 0 and after["win_rate"] < 0.50:
            candidate["tactical_min_expected_move_bps"] = min(
                1200.0, float(current["tactical_min_expected_move_bps"]) * 1.08
            )
            candidate["tactical_min_momentum_bps"] = min(
                300.0, float(current["tactical_min_momentum_bps"]) * 1.05
            )
            candidate["tactical_min_volume_ratio"] = min(
                8.0, float(current["tactical_min_volume_ratio"]) * 1.05
            )
            candidate["tactical_min_breakout_bps"] = min(
                250.0, float(current["tactical_min_breakout_bps"]) * 1.05
            )
            candidate["tactical_max_spread_bps"] = max(
                3.0, float(current["tactical_max_spread_bps"]) * 0.95
            )
            candidate["tactical_adaptive_min_expected_move_bps"] = min(
                800.0, float(current["tactical_adaptive_min_expected_move_bps"]) * 1.05
            )
            candidate["tactical_portfolio_pct"] = max(
                1.0, float(current["tactical_portfolio_pct"]) * 0.90
            )
            reason = "NEGATIVE_REALIZED_EXPECTANCY_TIGHTEN_ENTRIES"
        elif (
            after["mean_net_pnl_eur"] > 0
            and after["win_rate"] >= 0.55
            and after["mean_net_pnl_eur"] > before["mean_net_pnl_eur"]
        ):
            candidate["tactical_min_expected_move_bps"] = max(
                80.0, float(current["tactical_min_expected_move_bps"]) * 0.97
            )
            candidate["tactical_min_momentum_bps"] = max(
                10.0, float(current["tactical_min_momentum_bps"]) * 0.98
            )
            candidate["tactical_min_volume_ratio"] = max(
                1.0, float(current["tactical_min_volume_ratio"]) * 0.98
            )
            candidate["tactical_min_breakout_bps"] = max(
                5.0, float(current["tactical_min_breakout_bps"]) * 0.98
            )
            candidate["tactical_max_spread_bps"] = min(
                60.0, float(current["tactical_max_spread_bps"]) * 1.02
            )
            candidate["tactical_adaptive_min_expected_move_bps"] = max(
                80.0, float(current["tactical_adaptive_min_expected_move_bps"]) * 0.98
            )
            reason = "POSITIVE_IMPROVING_EXPECTANCY_SMALL_ENTRY_RELAXATION"

        # Avoid churn and duplicate versions when the measured policy does not change.
        changed = {
            key: value for key, value in candidate.items()
            if key in TACTICAL_DEFAULTS and value != current.get(key)
        }
        if not changed:
            result = {
                "status": "NO_SAFE_ADJUSTMENT",
                "samples": len(rows),
                "previous": before,
                "recent": after,
                "active_version": active_version,
            }
            mark_evaluated()
            self.db.learning_event("ADAPTIVE_POLICY_EVALUATED", active_version, result)
            return result

        candidate_id = hashlib.sha256(json.dumps({
            "parent": active_version,
            "candidate": candidate,
            "last_trade": rows[-1].get("trade_id"),
            "sample_count": len(rows),
        }, sort_keys=True, default=str).encode()).hexdigest()[:14]
        version = f"tactical-policy-{candidate_id}"
        metrics = {
            "kind": "bounded_online_controller",
            "samples": len(rows),
            "training_samples": len(rows) - self.WINDOW,
            "validation_samples": self.WINDOW,
            "previous_window": before,
            "recent_window": after,
            "reason": reason,
            "changed_parameters": changed,
            "not_counterfactual_backtest": True,
        }
        existing = self.db.one(
            "SELECT status FROM model_versions WHERE version=?", (version,)
        )
        promoted = False
        if existing is None:
            self.registry.register_candidate(
                version, "strategy_tactical", active_version, candidate, metrics
            )
            promoted = self.registry.promote_adaptive_policy(
                version, minimum_samples=self.MIN_TRADES
            )
        result = {
            "status": "PROMOTED" if promoted else "CANDIDATE_NOT_PROMOTED",
            "samples": len(rows),
            "previous": before,
            "recent": after,
            "changed_parameters": changed,
            "reason": reason,
            "candidate_version": version,
            "promoted": promoted,
        }
        mark_evaluated()
        self.db.learning_event("ADAPTIVE_POLICY_EVALUATED", version, result)
        return result


    EXIT_POLICY_BOUNDS = {
        "tactical_stop_loss_pct": (0.35, 2.0),
        "tactical_take_profit_pct": (1.0, 5.0),
        "tactical_trailing_trigger_bps": (50.0, 500.0),
        "tactical_trailing_stop_pct": (0.2, 1.5),
        "tactical_max_hold_seconds": (300, 7200),
    }
    MIN_EXIT_TRADES = 60
    MIN_EXIT_COVERAGE = 0.85
    MIN_TRAIN_IMPROVEMENT_BPS = 10.0
    MIN_VALIDATION_IMPROVEMENT_BPS = 5.0
    MAX_DRAWDOWN_DETERIORATION = 0.01

    CORE_WINDOW = 30
    MIN_CORE_OUTCOMES = 60
    CORE_POLICY_BOUNDS = {
        "strategy_min_edge_bps": (25.0, 80.0),
        "strategy_min_confidence": (0.58, 0.85),
        "strategy_adaptive_edge_floor_bps": (15.0, 25.0),
        "strategy_adaptive_min_confidence": (0.75, 0.95),
        "strategy_adaptive_cost_ratio": (1.10, 1.50),
    }

    def optimize_core_entry_policy(self, now: float | None = None) -> dict[str, Any]:
        """Conservatively adapt Normal entry gates from fully closed Spot/Margin outcomes.

        The label is directional gross return in basis points minus the cost
        estimate stored with the opening decision. Exchange-reported fee amounts
        and ledger net estimates are deliberately not read: fee currency is still
        unverified. The controller only tightens after poor results or restores
        earlier, tighter settings toward the documented defaults after a strong
        subsequent window. It never relaxes below those defaults or changes risk caps.
        """
        now = time.time() if now is None else float(now)
        rows = self.db.query(
            """SELECT o.opening_decision_id,o.symbol,o.direction,o.last_closed_at,
                      CAST(o.gross_return_bps AS REAL) AS gross_return_bps,
                      CAST(d.expected_cost_bps AS REAL) AS expected_cost_bps
               FROM core_realized_outcomes AS o
               JOIN decisions AS d ON d.decision_id=o.opening_decision_id
               WHERE o.last_closed_at >= ?
                 AND lower(d.strategy_version) NOT LIKE '%tactical%'
                 AND CAST(o.gross_return_bps AS REAL) IS NOT NULL
                 AND CAST(d.expected_cost_bps AS REAL) > 0
                 AND NOT EXISTS (
                   SELECT 1 FROM core_inventory_lots AS l
                   WHERE l.opening_decision_id=o.opening_decision_id
                     AND l.symbol=o.symbol AND l.direction=o.direction
                     AND CAST(l.remaining_quantity AS REAL) > 0
                 )
               ORDER BY o.last_closed_at DESC LIMIT 600""",
            (now - 180 * 86400,),
        )
        rows.reverse()
        if len(rows) < self.MIN_CORE_OUTCOMES:
            return {
                "status": "INSUFFICIENT_CLOSED_CORE_OUTCOMES",
                "samples": len(rows),
                "minimum_samples": self.MIN_CORE_OUTCOMES,
                "data_scope": "APP_MANAGED_SPOT_MARGIN_ONLY",
            }

        last = self.db.one(
            "SELECT value FROM metadata WHERE key='core_entry_policy_last_evaluated_closed_at'"
        )
        if last:
            try:
                last_at = float(last.get("value") or 0.0)
            except (TypeError, ValueError):
                last_at = 0.0
            new_count_row = self.db.one(
                """SELECT COUNT(*) AS n
                   FROM core_realized_outcomes AS o
                   JOIN decisions AS d ON d.decision_id=o.opening_decision_id
                   WHERE o.last_closed_at > ?
                     AND lower(d.strategy_version) NOT LIKE '%tactical%'
                     AND CAST(o.gross_return_bps AS REAL) IS NOT NULL
                     AND CAST(d.expected_cost_bps AS REAL) > 0
                     AND NOT EXISTS (
                       SELECT 1 FROM core_inventory_lots AS l
                       WHERE l.opening_decision_id=o.opening_decision_id
                         AND l.symbol=o.symbol AND l.direction=o.direction
                         AND CAST(l.remaining_quantity AS REAL) > 0
                     )""",
                (last_at,),
            )
            new_count = int(new_count_row.get("n") or 0) if new_count_row else 0
            if new_count < 10:
                return {
                    "status": "WAITING_FOR_NEW_CORE_OUTCOMES",
                    "samples": len(rows),
                    "new_closed_outcomes_since_last_evaluation": new_count,
                    "minimum_new_outcomes": 10,
                }

        clean: list[dict[str, Any]] = []
        for row in rows:
            try:
                gross = float(row["gross_return_bps"])
                cost = float(row["expected_cost_bps"])
                closed_at = float(row["last_closed_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in (gross, cost, closed_at)):
                continue
            if cost < 0 or closed_at <= 0:
                continue
            clean.append({
                "opening_decision_id": str(row["opening_decision_id"]),
                "symbol": str(row["symbol"]),
                "direction": str(row["direction"]),
                "closed_at": closed_at,
                "gross_return_bps": gross,
                "expected_cost_bps": cost,
                "after_expected_cost_bps": gross - cost,
            })
        if len(clean) < self.MIN_CORE_OUTCOMES:
            return {
                "status": "INSUFFICIENT_VALID_CORE_OUTCOMES",
                "samples": len(clean),
                "minimum_samples": self.MIN_CORE_OUTCOMES,
            }

        previous = clean[-2 * self.CORE_WINDOW:-self.CORE_WINDOW]
        recent = clean[-self.CORE_WINDOW:]
        if len(previous) < self.CORE_WINDOW or len(recent) < self.CORE_WINDOW:
            return {
                "status": "INSUFFICIENT_CORE_COMPARISON_WINDOWS",
                "samples": len(clean),
                "previous_samples": len(previous),
                "recent_samples": len(recent),
            }

        def stats(items: list[dict[str, Any]]) -> dict[str, float]:
            gross = [float(item["gross_return_bps"]) for item in items]
            cost = [float(item["expected_cost_bps"]) for item in items]
            adjusted = [float(item["after_expected_cost_bps"]) for item in items]
            return {
                "samples": float(len(items)),
                "mean_gross_return_bps": sum(gross) / len(gross),
                "mean_decision_cost_estimate_bps": sum(cost) / len(cost),
                "mean_after_expected_cost_bps": sum(adjusted) / len(adjusted),
                "positive_after_expected_cost_rate": (
                    sum(1 for value in adjusted if value > 0) / len(adjusted)
                ),
            }

        before = stats(previous)
        after = stats(recent)
        active_version = self.registry.active("strategy_core")
        current = dict(CORE_DEFAULTS)
        active_params = self.registry.parameters(active_version, family="strategy_core")
        if isinstance(active_params, dict):
            current.update({
                key: active_params[key]
                for key in CORE_DEFAULTS
                if key in active_params
            })
        # Re-apply controller bounds before calculating a neighbour candidate.
        for key, (low, high) in self.CORE_POLICY_BOUNDS.items():
            try:
                current[key] = max(low, min(high, float(current[key])))
            except (TypeError, ValueError, KeyError):
                current[key] = CORE_DEFAULTS[key]
        current["strategy_adaptive_edge_floor_bps"] = min(
            current["strategy_min_edge_bps"],
            current["strategy_adaptive_edge_floor_bps"],
        )
        current["strategy_adaptive_min_confidence"] = max(
            current["strategy_min_confidence"],
            current["strategy_adaptive_min_confidence"],
        )
        current["strategy_adaptive_cost_ratio"] = max(
            1.10, current["strategy_adaptive_cost_ratio"]
        )

        candidate = dict(current)
        action = "NO_ADJUSTMENT"
        reason = "OUTCOMES_WITHIN_POLICY_GUARD_BANDS"
        if (
            after["mean_after_expected_cost_bps"] <= -10.0
            and after["positive_after_expected_cost_rate"] < 0.45
        ):
            action = "TIGHTEN"
            reason = "NEGATIVE_RECENT_CORE_RETURN_AFTER_DECISION_COST_ESTIMATE"
            candidate["strategy_min_edge_bps"] = min(
                80.0, max(current["strategy_min_edge_bps"] + 2.0,
                           current["strategy_min_edge_bps"] * 1.08)
            )
            candidate["strategy_min_confidence"] = min(
                0.85, current["strategy_min_confidence"] + 0.02
            )
            candidate["strategy_adaptive_edge_floor_bps"] = min(
                candidate["strategy_min_edge_bps"],
                25.0,
                max(current["strategy_adaptive_edge_floor_bps"] + 1.0,
                    current["strategy_adaptive_edge_floor_bps"] * 1.08),
            )
            candidate["strategy_adaptive_min_confidence"] = min(
                0.95, current["strategy_adaptive_min_confidence"] + 0.02
            )
            candidate["strategy_adaptive_cost_ratio"] = min(
                1.50, current["strategy_adaptive_cost_ratio"] + 0.05
            )
        elif (
            after["mean_after_expected_cost_bps"] >= 15.0
            and after["positive_after_expected_cost_rate"] >= 0.60
            and after["mean_after_expected_cost_bps"]
                >= before["mean_after_expected_cost_bps"] + 5.0
        ):
            action = "RESTORE_TOWARD_DEFAULTS"
            reason = "STRONG_IMPROVING_CORE_RETURN_RESTORE_TOWARD_BASELINE"
            # Positive observed outcomes only restore previous extra strictness;
            # no setting can become looser than the documented baseline defaults.
            candidate["strategy_min_edge_bps"] = max(
                CORE_DEFAULTS["strategy_min_edge_bps"],
                current["strategy_min_edge_bps"] - max(
                    1.0, current["strategy_min_edge_bps"] * 0.03
                ),
            )
            candidate["strategy_min_confidence"] = max(
                CORE_DEFAULTS["strategy_min_confidence"],
                current["strategy_min_confidence"] - 0.01,
            )
            candidate["strategy_adaptive_edge_floor_bps"] = max(
                CORE_DEFAULTS["strategy_adaptive_edge_floor_bps"],
                current["strategy_adaptive_edge_floor_bps"] - 0.5,
            )
            candidate["strategy_adaptive_min_confidence"] = max(
                CORE_DEFAULTS["strategy_adaptive_min_confidence"],
                current["strategy_adaptive_min_confidence"] - 0.01,
            )
            candidate["strategy_adaptive_cost_ratio"] = max(
                CORE_DEFAULTS["strategy_adaptive_cost_ratio"],
                current["strategy_adaptive_cost_ratio"] - 0.02,
            )

        changed = {
            key: round(float(candidate[key]), 6)
            for key in CORE_DEFAULTS
            if abs(float(candidate[key]) - float(current[key])) > 1e-9
        }
        last_closed_at = max(float(item["closed_at"]) for item in clean)

        def mark_evaluated() -> None:
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('core_entry_policy_last_evaluated_closed_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(last_closed_at),),
            )

        metrics = {
            "kind": "bounded_core_outcome_controller",
            "action": action,
            "reason": reason,
            "samples": len(clean),
            "previous_samples": int(before["samples"]),
            "recent_samples": int(after["samples"]),
            "previous_mean_after_expected_cost_bps": before["mean_after_expected_cost_bps"],
            "recent_mean_after_expected_cost_bps": after["mean_after_expected_cost_bps"],
            "previous_positive_after_expected_cost_rate": before["positive_after_expected_cost_rate"],
            "recent_positive_after_expected_cost_rate": after["positive_after_expected_cost_rate"],
            "data_basis": "gross_return_bps_minus_decision_expected_cost_bps",
            "exchange_fee_estimates_used": False,
            "verified_net_pnl_used": False,
            "data_scope": "APP_MANAGED_SPOT_MARGIN_ONLY",
            "changed_parameters": changed,
        }
        if not changed:
            result = {
                "status": "NO_SAFE_CORE_ADJUSTMENT",
                "active_version": active_version,
                "samples": len(clean),
                "previous": before,
                "recent": after,
                "action": action,
                "data_basis": metrics["data_basis"],
            }
            mark_evaluated()
            self.db.learning_event("CORE_ENTRY_POLICY_EVALUATED", active_version, result)
            return result

        candidate_id = hashlib.sha256(json.dumps({
            "parent": active_version,
            "parameters": candidate,
            "last_closed_at": last_closed_at,
            "samples": len(clean),
            "action": action,
        }, sort_keys=True, default=str).encode()).hexdigest()[:14]
        version = f"core-entry-policy-{candidate_id}"
        existing = self.db.one(
            "SELECT status FROM model_versions WHERE version=?", (version,)
        )
        if existing is None:
            self.registry.register_candidate(
                version, "strategy_core", active_version, candidate, metrics
            )
        promoted = self.registry.promote_adaptive_policy(
            version, minimum_samples=self.MIN_CORE_OUTCOMES
        )
        result = {
            "status": "PROMOTED" if promoted else "CANDIDATE_NOT_PROMOTED",
            "samples": len(clean),
            "previous": before,
            "recent": after,
            "action": action,
            "reason": reason,
            "changed_parameters": changed,
            "candidate_version": version,
            "active_version": self.registry.active("strategy_core"),
            "promoted": promoted,
            "data_basis": metrics["data_basis"],
            "exchange_fee_estimates_used": False,
            "verified_net_pnl_used": False,
        }
        mark_evaluated()
        self.db.learning_event("CORE_ENTRY_POLICY_EVALUATED", version, result)
        return result

    def _prepare_exit_policy_trades(self, now: float) -> list[dict[str, Any]]:
        """Aggregate partial closes into position episodes and load only path-complete live trades."""
        rows = self.db.query(
            """SELECT trade_id,symbol,direction,entry_price,exit_price,quantity,
                      gross_pnl_eur,fees_eur,net_pnl_eur,opened_at,closed_at,
                      hold_seconds,exit_reason,detail_json
               FROM tactical_trades
               WHERE closed_at >= ?
               ORDER BY closed_at ASC LIMIT 2400""",
            (now - 180 * 86400,),
        )
        active_rows = self.db.query(
            "SELECT symbol,opened_at FROM tactical_positions WHERE state='OPEN'"
        )
        active = {(str(row["symbol"]), float(row["opened_at"])) for row in active_rows}
        grouped: dict[tuple[str, float, str], dict[str, Any]] = {}
        for row in rows:
            try:
                symbol = str(row["symbol"])
                opened_at = float(row["opened_at"])
                direction = str(row["direction"]).upper()
                closed_at = float(row["closed_at"])
                detail = json.loads(row.get("detail_json") or "{}")
                if not isinstance(detail, dict) or detail.get("mode") != "LIVE":
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            key = (symbol, opened_at, direction)
            current = grouped.get(key)
            if current is None:
                current = {
                    "key": key,
                    "symbol": symbol,
                    "opened_at": opened_at,
                    "direction": direction,
                    "entry_price": self._number(row.get("entry_price")),
                    "closed_at": closed_at,
                    "exit_price": self._number(row.get("exit_price")),
                    "exit_reason": str(row.get("exit_reason") or ""),
                    "net_pnl_eur": self._number(row.get("net_pnl_eur")),
                    "gross_pnl_eur": self._number(row.get("gross_pnl_eur")),
                    "fees_eur": self._number(row.get("fees_eur")),
                    "quantity": self._number(row.get("quantity")),
                    "detail": detail,
                    "trade_ids": [str(row.get("trade_id") or "")],
                }
                grouped[key] = current
            else:
                current["net_pnl_eur"] += self._number(row.get("net_pnl_eur"))
                current["gross_pnl_eur"] += self._number(row.get("gross_pnl_eur"))
                current["fees_eur"] += self._number(row.get("fees_eur"))
                current["quantity"] += self._number(row.get("quantity"))
                current["trade_ids"].append(str(row.get("trade_id") or ""))
                if closed_at >= current["closed_at"]:
                    current["closed_at"] = closed_at
                    current["exit_price"] = self._number(row.get("exit_price"))
                    current["exit_reason"] = str(row.get("exit_reason") or "")
                    current["detail"] = detail

        supported_reasons = {"STOP_LOSS", "TRAILING_STOP", "TIME_STOP"}
        result: list[dict[str, Any]] = []
        for key, trade in grouped.items():
            if (key[0], key[1]) in active or trade["exit_reason"] not in supported_reasons:
                continue
            detail = trade.get("detail") or {}
            context = detail.get("position_context") or {}
            costs = detail.get("cost_snapshot") or {}
            if not isinstance(context, dict) or not isinstance(costs, dict):
                continue
            if "leverage" not in context or "venue" not in context:
                continue
            required_cost_keys = {
                "entry_fee_bps", "exit_fee_bps", "max_spread_bps",
                "expected_slippage_bps", "safety_buffer_bps",
                "margin_open_fee_bps", "rollover_fee_bps",
            }
            if not required_cost_keys.issubset(costs):
                continue
            if trade["entry_price"] <= 0 or trade["closed_at"] <= trade["opened_at"]:
                continue
            path = self.db.tactical_price_path(
                trade["symbol"], trade["opened_at"], trade["closed_at"]
            )
            if len(path) < 3:
                continue
            trade["path"] = path
            trade["position_context"] = context
            trade["cost_snapshot"] = costs
            result.append(trade)
        result.sort(key=lambda item: (item["closed_at"], item["symbol"], item["opened_at"]))
        return result

    @staticmethod
    def _simulate_tactical_exit(
        trade: dict[str, Any], policy: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Replay a bounded exit policy without using prices after the real close."""
        try:
            entry = float(trade["entry_price"])
            opened_at = float(trade["opened_at"])
            closed_at = float(trade["closed_at"])
            direction = str(trade["direction"]).upper()
            context = trade["position_context"]
            costs = trade["cost_snapshot"]
            leverage = max(1.0, float(context.get("leverage", 1.0)))
            venue = str(context.get("venue", "spot")).lower()
            if entry <= 0 or direction not in {"LONG", "SHORT"}:
                return None
            stop_loss_pct = float(policy["tactical_stop_loss_pct"])
            take_profit_pct = float(policy["tactical_take_profit_pct"])
            trailing_trigger_bps = float(policy["tactical_trailing_trigger_bps"])
            trailing_stop_pct = float(policy["tactical_trailing_stop_pct"])
            max_hold_seconds = int(float(policy["tactical_max_hold_seconds"]))
            entry_fee_bps = max(0.0, float(costs["entry_fee_bps"]))
            exit_fee_bps = max(0.0, float(costs["exit_fee_bps"]))
            spread_bps = max(0.0, float(costs["max_spread_bps"]))
            slippage_bps = max(0.0, float(costs["expected_slippage_bps"]))
            safety_bps = max(0.0, float(costs["safety_buffer_bps"]))
            margin_open_fee_bps = max(0.0, float(costs["margin_open_fee_bps"]))
            rollover_fee_bps = max(0.0, float(costs["rollover_fee_bps"]))
        except (KeyError, TypeError, ValueError):
            return None

        path = trade.get("path", [])
        peak = entry
        trough = entry
        state = "OPEN"
        # When a position survives an app restart, the first persisted observation
        # carries its durable peak/trough and state. Use it as the replay baseline
        # rather than pretending the path was observed from the entry price.
        if path:
            first = path[0]
            try:
                peak = max(entry, float(first.get("peak_price", entry)))
                trough = min(entry, float(first.get("trough_price", entry)))
                first_state = str(first.get("state") or "OPEN").upper()
                state = "TRAILING" if first_state == "TRAILING" else "OPEN"
            except (TypeError, ValueError):
                peak = entry
                trough = entry
                state = "OPEN"
        for point in path:
            try:
                timestamp = float(point["observed_at"])
                price = float(point["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0 or timestamp < opened_at or timestamp > closed_at:
                continue
            peak = max(peak, price)
            trough = min(trough, price)
            if direction == "LONG":
                pnl_bps = (price / entry - 1.0) * 10000.0
                peak_gain_bps = (peak / entry - 1.0) * 10000.0
                trail_crossed = price <= peak * (1.0 - trailing_stop_pct / 100.0)
            else:
                pnl_bps = (entry / price - 1.0) * 10000.0
                peak_gain_bps = (entry / trough - 1.0) * 10000.0
                trail_crossed = price >= trough * (1.0 + trailing_stop_pct / 100.0)

            elapsed = max(0.0, timestamp - opened_at)
            financing_bps = 0.0
            if venue == "spot" and leverage > 1.0:
                borrowed_share = (leverage - 1.0) / leverage
                financing_bps = borrowed_share * (
                    margin_open_fee_bps + rollover_fee_bps * int(elapsed / 14400.0)
                )
            estimated_net_cost_bps = (
                entry_fee_bps + exit_fee_bps + spread_bps + slippage_bps + financing_bps
            )
            # Safety buffer is a trigger margin, not an actual cash cost.
            profit_floor_bps = estimated_net_cost_bps + safety_bps + 25.0
            trailing_activation_bps = max(
                trailing_trigger_bps,
                profit_floor_bps + trailing_stop_pct * 100.0,
            )
            trailing_triggered = peak_gain_bps >= profit_floor_bps and (
                (peak_gain_bps >= trailing_activation_bps and trail_crossed)
                or pnl_bps < profit_floor_bps
            )

            # Match the production engine's order: arm trend following before its
            # non-trailing time stop, and evaluate stop-loss before trailing exit.
            if peak_gain_bps >= take_profit_pct * 100.0 and state != "TRAILING":
                state = "TRAILING"
            reason = ""
            if pnl_bps <= -stop_loss_pct * 100.0:
                reason = "STOP_LOSS"
            elif trailing_triggered:
                reason = "TRAILING_STOP"
            elif state != "TRAILING" and elapsed >= max_hold_seconds:
                reason = "TIME_STOP"
            if reason:
                return {
                    "net_bps": pnl_bps - estimated_net_cost_bps,
                    "gross_bps": pnl_bps,
                    "reason": reason,
                    "exit_at": timestamp,
                    "hold_seconds": elapsed,
                }
        return None

    @staticmethod
    def _exit_drawdown(outcomes: list[dict[str, Any]]) -> float:
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for result in outcomes:
            equity *= max(0.0001, 1.0 + float(result["net_bps"]) / 10000.0)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        return max_drawdown

    def _mark_exit_policy_evaluated(self, closed_at: float) -> None:
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES('tactical_exit_policy_last_evaluated_closed_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(float(closed_at)),),
        )

    def optimize_tactical_exits(self, now: float | None = None) -> dict[str, Any]:
        """Promote exit settings only after chronological path replay clears strict holdout gates."""
        now = time.time() if now is None else float(now)
        last = self.db.one(
            "SELECT value FROM metadata WHERE key='tactical_exit_policy_last_evaluated_closed_at'"
        )
        last_at: float | None = None
        if last:
            try:
                last_at = float(last["value"])
            except (TypeError, ValueError):
                last_at = 0.0
            # Count position episodes, not partial-close rows. Do this cheap
            # preflight before reconstructing thousands of price-path records.
            fresh_episodes = self.db.query(
                "SELECT symbol,opened_at FROM tactical_trades WHERE closed_at>? "
                "GROUP BY symbol,opened_at",
                (last_at,),
            )
            if len(fresh_episodes) < 10:
                return {
                    "status": "WAITING_FOR_NEW_EXIT_PATHS",
                    "samples": None,
                    "new_trades_since_last_evaluation": len(fresh_episodes),
                    "minimum_new_trades": 10,
                }

        watermark_row = self.db.one(
            "SELECT MAX(closed_at) AS closed_at FROM tactical_trades WHERE closed_at>=?",
            (now - 180 * 86400,),
        )
        watermark = (
            float(watermark_row["closed_at"])
            if watermark_row and watermark_row.get("closed_at") is not None
            else None
        )
        trades = self._prepare_exit_policy_trades(now)
        if len(trades) < self.MIN_EXIT_TRADES:
            if watermark is not None and (last_at is None or watermark > last_at):
                self._mark_exit_policy_evaluated(watermark)
            return {
                "status": "INSUFFICIENT_VALIDATED_EXIT_PATHS",
                "samples": len(trades),
                "minimum_samples": self.MIN_EXIT_TRADES,
            }

        active_version = self.registry.active("strategy_tactical")
        current = dict(TACTICAL_DEFAULTS)
        current.update(self.registry.parameters(active_version, family="strategy_tactical"))
        current_exit = {key: current[key] for key in self.EXIT_POLICY_BOUNDS}

        # First validate that the current-policy replay agrees with the actual exit
        # reason and timing. Mismatched paths are discarded instead of "explaining"
        # their observed outcome with a different counterfactual.
        aligned: list[dict[str, Any]] = []
        current_replay: dict[tuple[str, float, str], dict[str, Any]] = {}
        tolerance = max(
            90.0,
            4.0 * float(getattr(self.config, "tactical_poll_seconds", 10)),
        )
        for trade in trades:
            outcome = self._simulate_tactical_exit(trade, current_exit)
            if outcome is None or outcome["reason"] != trade["exit_reason"]:
                continue
            if abs(float(outcome["exit_at"]) - float(trade["closed_at"])) > tolerance:
                continue
            aligned.append(trade)
            current_replay[trade["key"]] = outcome
        if len(aligned) < self.MIN_EXIT_TRADES:
            if watermark is not None and (last_at is None or watermark > last_at):
                self._mark_exit_policy_evaluated(watermark)
            return {
                "status": "INSUFFICIENT_BASELINE_ALIGNED_PATHS",
                "samples": len(aligned),
                "path_samples": len(trades),
                "minimum_samples": self.MIN_EXIT_TRADES,
            }

        last_closed_at = watermark or max(float(trade["closed_at"]) for trade in aligned)

        split = max(1, int(len(aligned) * 0.70))
        training = aligned[:split]
        validation = aligned[split:]
        if len(validation) < 18:
            return {
                "status": "INSUFFICIENT_EXIT_VALIDATION_WINDOW",
                "training_samples": len(training),
                "validation_samples": len(validation),
                "minimum_validation_samples": 18,
            }

        def paired(items: list[dict[str, Any]], policy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
            base_outcomes: list[dict[str, Any]] = []
            candidate_outcomes: list[dict[str, Any]] = []
            for trade in items:
                base = current_replay.get(trade["key"])
                candidate = self._simulate_tactical_exit(trade, policy)
                if base is None or candidate is None:
                    continue
                base_outcomes.append(base)
                candidate_outcomes.append(candidate)
            coverage = len(candidate_outcomes) / max(1, len(items))
            return base_outcomes, candidate_outcomes, coverage

        # Coordinate-wise neighbours limit the search degrees of freedom. Training
        # chooses one candidate; the later holdout is evaluated exactly once.
        variants: list[tuple[str, dict[str, Any]]] = []
        for key, factors in (
            ("tactical_stop_loss_pct", (0.75, 1.25)),
            ("tactical_take_profit_pct", (0.80, 1.25)),
            ("tactical_trailing_trigger_bps", (0.75, 1.25)),
            ("tactical_trailing_stop_pct", (0.75, 1.25)),
            ("tactical_max_hold_seconds", (0.75, 1.25)),
        ):
            low, high = self.EXIT_POLICY_BOUNDS[key]
            for factor in factors:
                value = min(high, max(low, float(current_exit[key]) * factor))
                if key == "tactical_max_hold_seconds":
                    value = int(round(value / 30.0) * 30)
                if abs(value - float(current_exit[key])) < 1e-9:
                    continue
                candidate = dict(current_exit)
                candidate[key] = value
                if float(candidate["tactical_stop_loss_pct"]) >= float(candidate["tactical_take_profit_pct"]):
                    continue
                variants.append((f"{key}={value}", candidate))

        baseline_train = [current_replay[item["key"]] for item in training]
        baseline_train_mean = sum(float(x["net_bps"]) for x in baseline_train) / len(baseline_train)
        selected: tuple[str, dict[str, Any], float, float, float] | None = None
        for label, candidate in variants:
            base_values, candidate_values, coverage = paired(training, candidate)
            if coverage < self.MIN_EXIT_COVERAGE or len(candidate_values) < 30:
                continue
            improvement = (
                sum(float(x["net_bps"]) for x in candidate_values) / len(candidate_values)
                - sum(float(x["net_bps"]) for x in base_values) / len(base_values)
            )
            candidate_dd = self._exit_drawdown(candidate_values)
            base_dd = self._exit_drawdown(base_values)
            if (
                improvement >= self.MIN_TRAIN_IMPROVEMENT_BPS
                and sum(float(x["net_bps"]) for x in candidate_values) / len(candidate_values) > 0
                and candidate_dd <= base_dd + self.MAX_DRAWDOWN_DETERIORATION
            ):
                if selected is None or improvement > selected[2]:
                    selected = (label, candidate, improvement, candidate_dd, coverage)

        if selected is None:
            result = {
                "status": "NO_EXIT_POLICY_PASSED_TRAINING_GATES",
                "samples": len(aligned),
                "training_samples": len(training),
                "validation_samples": len(validation),
                "baseline_training_mean_net_bps": baseline_train_mean,
            }
            self._mark_exit_policy_evaluated(last_closed_at)
            self.db.learning_event("TACTICAL_EXIT_POLICY_EVALUATED", active_version, result)
            return result

        label, candidate_exit, training_improvement, training_dd, training_coverage = selected
        baseline_valid, candidate_valid, validation_coverage = paired(validation, candidate_exit)
        if validation_coverage < self.MIN_EXIT_COVERAGE or len(candidate_valid) < 18:
            result = {
                "status": "EXIT_POLICY_REJECTED_VALIDATION_COVERAGE",
                "candidate": label,
                "samples": len(aligned),
                "validation_coverage": validation_coverage,
            }
        else:
            base_mean = sum(float(x["net_bps"]) for x in baseline_valid) / len(baseline_valid)
            candidate_mean = sum(float(x["net_bps"]) for x in candidate_valid) / len(candidate_valid)
            base_dd = self._exit_drawdown(baseline_valid)
            candidate_dd = self._exit_drawdown(candidate_valid)
            validation_improvement = candidate_mean - base_mean
            if (
                validation_improvement >= self.MIN_VALIDATION_IMPROVEMENT_BPS
                and candidate_mean > 0
                and candidate_dd <= base_dd + self.MAX_DRAWDOWN_DETERIORATION
            ):
                candidate_params = dict(current)
                candidate_params.update(candidate_exit)
                changed = {
                    key: value for key, value in candidate_exit.items()
                    if value != current_exit.get(key)
                }
                candidate_id = hashlib.sha256(json.dumps({
                    "parent": active_version,
                    "parameters": candidate_params,
                    "last_closed_at": last_closed_at,
                    "samples": len(aligned),
                }, sort_keys=True, default=str).encode()).hexdigest()[:14]
                version = f"tactical-exit-{candidate_id}"
                metrics = {
                    "kind": "path_replay_exit_policy",
                    "samples": len(aligned),
                    "training_samples": len(training),
                    "validation_samples": len(validation),
                    "training_improvement_bps": training_improvement,
                    "validation_improvement_bps": validation_improvement,
                    "training_coverage": training_coverage,
                    "validation_coverage": validation_coverage,
                    "baseline_training_max_drawdown": self._exit_drawdown(baseline_train),
                    "candidate_training_max_drawdown": training_dd,
                    "baseline_validation_mean_net_bps": base_mean,
                    "candidate_validation_mean_net_bps": candidate_mean,
                    "baseline_validation_max_drawdown": base_dd,
                    "candidate_validation_max_drawdown": candidate_dd,
                    "max_drawdown_deterioration": self.MAX_DRAWDOWN_DETERIORATION,
                    "selected_candidate": label,
                    "changed_parameters": changed,
                    "counterfactual_scope": "observed path only; no extrapolation beyond actual close",
                }
                existing = self.db.one(
                    "SELECT status FROM model_versions WHERE version=?", (version,)
                )
                promoted = False
                if existing is None:
                    self.registry.register_candidate(
                        version, "strategy_tactical", active_version, candidate_params, metrics
                    )
                    promoted = self.registry.promote_adaptive_policy(
                        version, minimum_samples=self.MIN_EXIT_TRADES
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
                self._mark_exit_policy_evaluated(last_closed_at)
                self.db.learning_event("TACTICAL_EXIT_POLICY_EVALUATED", version, result)
                return result
            result = {
                "status": "EXIT_POLICY_REJECTED_HOLDOUT",
                "candidate": label,
                "samples": len(aligned),
                "training_improvement_bps": training_improvement,
                "validation_improvement_bps": validation_improvement,
                "baseline_validation_mean_net_bps": base_mean,
                "candidate_validation_mean_net_bps": candidate_mean,
                "baseline_validation_max_drawdown": base_dd,
                "candidate_validation_max_drawdown": candidate_dd,
                "validation_coverage": validation_coverage,
            }

        self._mark_exit_policy_evaluated(last_closed_at)
        self.db.learning_event("TACTICAL_EXIT_POLICY_EVALUATED", active_version, result)
        return result
