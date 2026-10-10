from __future__ import annotations

import json
import math
import time
from typing import Any


CORE_BOUNDS: dict[str, tuple[float, float]] = {
    "strategy_min_edge_bps": (10.0, 250.0),
    "strategy_min_confidence": (0.50, 0.90),
    "strategy_adaptive_edge_floor_bps": (5.0, 200.0),
    "strategy_adaptive_min_confidence": (0.60, 0.98),
    "strategy_adaptive_cost_ratio": (1.01, 3.0),
}
TACTICAL_BOUNDS: dict[str, tuple[float, float]] = {
    "tactical_min_volatility_bps": (2.0, 100.0),
    "tactical_max_volatility_bps": (15.0, 200.0),
    "tactical_min_volume_ratio": (1.1, 10.0),
    "tactical_min_momentum_bps": (5.0, 500.0),
    "tactical_min_breakout_bps": (5.0, 500.0),
    "tactical_min_imbalance": (0.02, 0.80),
    "tactical_max_spread_bps": (5.0, 80.0),
    "tactical_min_expected_move_bps": (230.0, 2000.0),
    "tactical_stop_loss_pct": (0.3, 2.5),
    "tactical_take_profit_pct": (1.0, 6.0),
    "tactical_trailing_stop_pct": (0.2, 2.0),
    "tactical_adaptive_min_expected_move_bps": (220.0, 1500.0),
    "tactical_adaptive_min_confidence": (0.60, 0.98),
    "tactical_adaptive_min_net_edge_bps": (5.0, 200.0),
}
GROUP_BOUNDS = {"core": CORE_BOUNDS, "tactical": TACTICAL_BOUNDS}
MIN_CORE_SAMPLES = 300
MIN_CORE_VALIDATION = 90
MIN_TACTICAL_PATH_SAMPLES = 60


def _object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, ArithmeticError):
        return None


def _metrics(rows: list[dict[str, Any]], returns_key: str) -> dict[str, float]:
    values = [_number(row.get(returns_key)) for row in rows]
    returns = [value for value in values if value is not None]
    if not returns:
        return {"samples": 0.0, "average": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "total": 0.0}
    positives = sum(value for value in returns if value > 0)
    negatives = abs(sum(value for value in returns if value < 0))
    pf = positives / negatives if negatives > 0 else (999.0 if positives > 0 else 0.0)
    return {
        "samples": float(len(returns)),
        "average": sum(returns) / len(returns),
        "win_rate": sum(1 for value in returns if value > 0) / len(returns),
        "profit_factor": pf,
        "total": sum(returns),
    }


class ManagedStrategyParameters:
    """Persists, bounds and walk-forward validates learner-owned parameters.

    Hard risk limits (leverage, exposure, cash reserve, daily loss and drawdown)
    are deliberately absent from both parameter groups and can never be relaxed
    by this optimizer.
    """

    def __init__(self, db: Any, *, enabled: bool = True, lookback_days: int = 365,
                 validation_interval_hours: int = 24) -> None:
        self.db = db
        self.enabled = bool(enabled)
        self.lookback_days = max(1, int(lookback_days))
        self.validation_interval_hours = max(1, int(validation_interval_hours))

    @staticmethod
    def _defaults(config: Any, family: str) -> dict[str, Any]:
        bounds = GROUP_BOUNDS[family]
        return {key: object.__getattribute__(config, key) for key in bounds}

    @staticmethod
    def _clean(family: str, values: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, (low, high) in GROUP_BOUNDS[family].items():
            raw = values.get(key, defaults.get(key, low))
            number = _number(raw)
            if number is None:
                number = float(defaults.get(key, low))
            number = max(low, min(high, number))
            result[key] = int(round(number)) if key == "tactical_max_hold_seconds" else round(number, 6)

        if family == "core":
            result["strategy_adaptive_edge_floor_bps"] = min(
                result["strategy_adaptive_edge_floor_bps"], result["strategy_min_edge_bps"]
            )
        else:
            if result["tactical_min_volatility_bps"] >= result["tactical_max_volatility_bps"]:
                result["tactical_min_volatility_bps"] = max(
                    GROUP_BOUNDS[family]["tactical_min_volatility_bps"][0],
                    result["tactical_max_volatility_bps"] - 1.0,
                )
            if result["tactical_stop_loss_pct"] >= result["tactical_take_profit_pct"]:
                result["tactical_stop_loss_pct"] = max(
                    GROUP_BOUNDS[family]["tactical_stop_loss_pct"][0],
                    result["tactical_take_profit_pct"] - 0.1,
                )
        return result

    def apply(self, config: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        all_parameters: dict[str, Any] = {}
        for family in ("core", "tactical"):
            defaults = self._defaults(config, family)
            row = self.db.managed_strategy_parameters(family)
            if row is None:
                parameters = self._clean(family, defaults, defaults)
                saved = self.db.save_managed_strategy_parameters(
                    family, parameters,
                    {"status": "BASELINE_INITIALIZED", "optimizer": "bounded_walk_forward"},
                    0, "SAFE_DEFAULTS",
                )
                version = int(saved.get("version", 1))
                source = "SAFE_DEFAULTS"
                metrics = {"status": "BASELINE_INITIALIZED"}
                sample_count = 0
            else:
                raw = _object(row.get("parameters_json"))
                parameters = self._clean(family, raw, defaults)
                version = int(row.get("version") or 1)
                source = str(row.get("source") or "PERSISTED")
                metrics = _object(row.get("metrics_json"))
                sample_count = int(row.get("sample_count") or 0)
                if parameters != raw:
                    saved = self.db.save_managed_strategy_parameters(
                        family, parameters,
                        {"status": "SAFE_BOUND_REPAIR", "previous_version": version},
                        sample_count, "SAFE_BOUND_REPAIR",
                    )
                    version = int(saved.get("version", version))
                    source = "SAFE_BOUND_REPAIR"
            all_parameters.update(parameters)
            result[family] = {
                "version": version, "source": source, "sample_count": sample_count,
                "parameters": parameters, "metrics": metrics,
            }
        # One pointer swap publishes the entire learned version atomically to all
        # components sharing Config, including Tactical's independent thread.
        object.__setattr__(config, "_managed_parameters", all_parameters)
        return result

    def recalibrate(self, config: Any, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        if not self.enabled:
            return {"status": "LEARNING_DISABLED"}
        last = self.db.one(
            "SELECT value FROM metadata WHERE key='managed_strategy_last_validation_at'"
        )
        if last:
            try:
                previous = float(last["value"])
            except (TypeError, ValueError):
                previous = 0.0
            remaining = self.validation_interval_hours * 3600 - (now - previous)
            if remaining > 0:
                return {
                    "status": "INTERVAL_NOT_ELAPSED",
                    "next_validation_in_seconds": int(remaining),
                }

        current = {
            family: self._clean(
                family,
                _object((self.db.managed_strategy_parameters(family) or {}).get("parameters_json")),
                self._defaults(config, family),
            )
            for family in ("core", "tactical")
        }
        core = self._optimize_core(config, current["core"], now)
        tactical = self._optimize_tactical(current["tactical"], now)
        if core.get("status") == "PROMOTED":
            saved = self.db.save_managed_strategy_parameters(
                "core", core["parameters"], core["metrics"], int(core["sample_count"]),
                "WALK_FORWARD_FORECAST_OUTCOMES",
            )
            core["version"] = saved.get("version")
            core["changed"] = bool(saved.get("changed"))
        if tactical.get("status") == "PROMOTED":
            saved = self.db.save_managed_strategy_parameters(
                "tactical", tactical["parameters"], tactical["metrics"], int(tactical["sample_count"]),
                "WALK_FORWARD_TACTICAL_TRADES",
            )
            tactical["version"] = saved.get("version")
            tactical["changed"] = bool(saved.get("changed"))

        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES('managed_strategy_last_validation_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(now),),
        )
        result = {
            "status": "OK",
            "core": {key: value for key, value in core.items() if key != "parameters"},
            "tactical": {key: value for key, value in tactical.items() if key != "parameters"},
            "validated_at": now,
        }
        self.db.learning_event("STRATEGY_PARAMETER_VALIDATION", "managed", result)
        return result

    @staticmethod
    def _core_selected(row: dict[str, Any], params: dict[str, Any], adaptive_enabled: bool) -> bool:
        confidence = float(row["confidence"])
        edge = float(row["edge"])
        expected_return = float(row["expected_return"])
        costs = float(row["expected_cost"])
        if confidence < float(params["strategy_min_confidence"]):
            return False
        if edge >= float(params["strategy_min_edge_bps"]):
            return True
        return (
            adaptive_enabled
            and confidence >= float(params["strategy_adaptive_min_confidence"])
            and edge >= float(params["strategy_adaptive_edge_floor_bps"])
            and expected_return >= costs * float(params["strategy_adaptive_cost_ratio"])
        )

    def _optimize_core(self, config: Any, current: dict[str, Any], now: float) -> dict[str, Any]:
        rows = self.db.query(
            """SELECT p.prediction_id,p.created_at,p.probability,p.raw_confidence,
                      p.expected_return_bps,p.expected_cost_bps,o.success,o.detail_json
               FROM predictions p
               JOIN prediction_outcomes o ON o.prediction_id=p.prediction_id
               WHERE p.outcome_status='SETTLED' AND o.success IS NOT NULL AND p.created_at>=?
               ORDER BY p.created_at DESC LIMIT 2000""",
            (now - self.lookback_days * 86400,),
        )
        rows.reverse()
        records: list[dict[str, Any]] = []
        for row in rows:
            detail = _object(row.get("detail_json"))
            net = _number(detail.get("net_directional_return_bps"))
            expected = _number(row.get("expected_return_bps"))
            costs = _number(row.get("expected_cost_bps"))
            confidence = _number(row.get("raw_confidence"))
            if confidence is None:
                confidence = _number(row.get("probability"))
            if net is None or expected is None or costs is None or confidence is None:
                continue
            records.append({
                "created_at": float(row.get("created_at") or 0),
                "confidence": confidence,
                "expected_return": expected,
                "expected_cost": max(0.0, costs),
                "edge": expected - costs,
                "net_bps": net,
                "success": bool(row.get("success")),
            })
        if len(records) < MIN_CORE_SAMPLES:
            return {"status": "INSUFFICIENT_DATA", "sample_count": len(records)}
        split = int(len(records) * 0.70)
        train, validation = records[:split], records[split:]
        if len(validation) < MIN_CORE_VALIDATION:
            return {"status": "INSUFFICIENT_VALIDATION_DATA", "sample_count": len(records)}

        base_train = self._score_core(train, current, bool(config.strategy_adaptive_edge_enabled))
        base_validation = self._score_core(validation, current, bool(config.strategy_adaptive_edge_enabled))
        train_min = max(50, int(len(train) * 0.35))
        val_min = max(30, int(len(validation) * 0.35))
        if base_train["samples"] < train_min or base_validation["samples"] < val_min:
            return {
                "status": "INSUFFICIENT_BASELINE_COVERAGE", "sample_count": len(records),
                "training_baseline": base_train, "validation_baseline": base_validation,
            }

        best: tuple[float, dict[str, Any], dict[str, float]] | None = None
        edge_grid = sorted(set([
            current["strategy_min_edge_bps"],
            min(250.0, current["strategy_min_edge_bps"] + 5.0),
            min(250.0, current["strategy_min_edge_bps"] + 10.0),
        ]))
        confidence_grid = sorted(set([
            current["strategy_min_confidence"],
            min(0.90, current["strategy_min_confidence"] + 0.02),
            min(0.90, current["strategy_min_confidence"] + 0.04),
        ]))
        for edge in edge_grid:
            for confidence in confidence_grid:
                for floor_step in (0.0, 2.5):
                    for confidence_step in (0.0, 0.03):
                        for cost_step in (0.0, 0.10):
                            candidate = dict(current)
                            candidate.update({
                                "strategy_min_edge_bps": edge,
                                "strategy_min_confidence": confidence,
                                "strategy_adaptive_edge_floor_bps": min(
                                    edge, current["strategy_adaptive_edge_floor_bps"] + floor_step
                                ),
                                "strategy_adaptive_min_confidence": min(
                                    0.98, current["strategy_adaptive_min_confidence"] + confidence_step
                                ),
                                "strategy_adaptive_cost_ratio": min(
                                    3.0, current["strategy_adaptive_cost_ratio"] + cost_step
                                ),
                            })
                            train_score = self._score_core(
                                train, candidate, bool(config.strategy_adaptive_edge_enabled)
                            )
                            if train_score["samples"] < train_min:
                                continue
                            if train_score["average"] < base_train["average"] + 3.0:
                                continue
                            if train_score["win_rate"] < base_train["win_rate"] - 0.02:
                                continue
                            objective = train_score["average"] + min(0.15, train_score["win_rate"] - base_train["win_rate"])
                            if best is None or objective > best[0]:
                                best = (objective, candidate, train_score)
        if best is None:
            return {
                "status": "NO_TRAINING_CANDIDATE", "sample_count": len(records),
                "training_baseline": base_train, "validation_baseline": base_validation,
            }
        # Try candidates in training-rank order, but promote only after the untouched
        # newest chronological holdout independently validates them.
        candidates: list[tuple[float, dict[str, Any], dict[str, float]]] = []
        for edge in edge_grid:
            for confidence in confidence_grid:
                for floor_step in (0.0, 2.5):
                    for confidence_step in (0.0, 0.03):
                        for cost_step in (0.0, 0.10):
                            candidate = dict(current)
                            candidate.update({
                                "strategy_min_edge_bps": edge,
                                "strategy_min_confidence": confidence,
                                "strategy_adaptive_edge_floor_bps": min(
                                    edge, current["strategy_adaptive_edge_floor_bps"] + floor_step
                                ),
                                "strategy_adaptive_min_confidence": min(
                                    0.98, current["strategy_adaptive_min_confidence"] + confidence_step
                                ),
                                "strategy_adaptive_cost_ratio": min(
                                    3.0, current["strategy_adaptive_cost_ratio"] + cost_step
                                ),
                            })
                            score = self._score_core(
                                train, candidate, bool(config.strategy_adaptive_edge_enabled)
                            )
                            if (
                                score["samples"] >= train_min
                                and score["average"] >= base_train["average"] + 3.0
                                and score["win_rate"] >= base_train["win_rate"] - 0.02
                            ):
                                rank = score["average"] + min(0.15, score["win_rate"] - base_train["win_rate"])
                                candidates.append((rank, candidate, score))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, candidate, train_score in candidates:
            val_score = self._score_core(
                validation, candidate, bool(config.strategy_adaptive_edge_enabled)
            )
            if (
                val_score["samples"] >= val_min
                and val_score["average"] >= base_validation["average"] + 2.0
                and val_score["win_rate"] >= base_validation["win_rate"] - 0.01
                and val_score["profit_factor"] >= base_validation["profit_factor"] * 0.95
                and val_score["average"] > 0
            ):
                return {
                    "status": "PROMOTED", "sample_count": len(records),
                    "parameters": candidate,
                    "metrics": {
                        "training_baseline": base_train, "training_candidate": train_score,
                        "validation_baseline": base_validation, "validation_candidate": val_score,
                        "validation_method": "chronological_70_30_holdout",
                        "objective": "directional_return_after_expected_costs",
                    },
                }
        return {
            "status": "NO_VALIDATED_IMPROVEMENT", "sample_count": len(records),
            "training_baseline": base_train, "validation_baseline": base_validation,
        }

    def _score_core(
        self, records: list[dict[str, Any]], params: dict[str, Any], adaptive_enabled: bool
    ) -> dict[str, float]:
        selected = [row for row in records if self._core_selected(row, params, adaptive_enabled)]
        return _metrics(selected, "net_bps")

    def _optimize_tactical(self, current: dict[str, Any], now: float) -> dict[str, Any]:
        rows = self.db.query(
            """SELECT trade_id,opened_at,closed_at,exit_reason,net_pnl_eur,detail_json
               FROM tactical_trades WHERE closed_at>=? ORDER BY closed_at DESC LIMIT 1000""",
            (now - self.lookback_days * 86400,),
        )
        rows.reverse()
        records: list[dict[str, Any]] = []
        for row in rows:
            detail = _object(row.get("detail_json"))
            context = _object(detail.get("entry_context"))
            net_pct = _number(detail.get("net_return_pct"))
            if net_pct is None or not context:
                continue
            record = {
                "closed_at": float(row.get("closed_at") or 0),
                "exit_reason": str(row.get("exit_reason") or ""),
                "net_return_pct": net_pct,
                "context": context,
                "detail": detail,
            }
            records.append(record)
        if len(records) < MIN_TACTICAL_PATH_SAMPLES:
            return {
                "status": "INSUFFICIENT_TRADE_CONTEXT",
                "sample_count": len(records),
                "minimum_samples": MIN_TACTICAL_PATH_SAMPLES,
            }
        split = int(len(records) * 0.70)
        train, validation = records[:split], records[split:]
        if len(train) < 40 or len(validation) < 18:
            return {"status": "INSUFFICIENT_VALIDATION_DATA", "sample_count": len(records)}

        result = self._optimize_tactical_entry(train, validation, current, len(records))
        if result.get("status") == "PROMOTED":
            return result
        exit_result = self._optimize_tactical_exits(train, validation, current, len(records))
        return exit_result if exit_result.get("status") == "PROMOTED" else {
            "status": "NO_VALIDATED_IMPROVEMENT",
            "sample_count": len(records),
            "entry_status": result.get("status"),
            "exit_status": exit_result.get("status"),
        }

    @staticmethod
    def _entry_value(row: dict[str, Any], feature: str) -> float | None:
        context = row["context"]
        raw = _number(context.get(feature))
        if raw is None:
            return None
        if feature in {"momentum_60_bps", "breakout_bps", "imbalance"}:
            return abs(raw)
        return raw

    @staticmethod
    def _filter_tactical(rows: list[dict[str, Any]], key: str, value: float) -> list[dict[str, Any]]:
        specs: dict[str, tuple[str, str]] = {
            "tactical_min_volatility_bps": ("volatility_bps", "min"),
            "tactical_max_volatility_bps": ("volatility_bps", "max"),
            "tactical_min_volume_ratio": ("volume_ratio", "min"),
            "tactical_min_momentum_bps": ("momentum_60_bps", "min"),
            "tactical_min_breakout_bps": ("breakout_bps", "min"),
            "tactical_min_imbalance": ("imbalance", "min"),
            "tactical_max_spread_bps": ("spread_bps", "max"),
            "tactical_min_expected_move_bps": ("expected_move_bps", "min"),
            "tactical_adaptive_min_expected_move_bps": ("expected_move_bps", "min"),
            "tactical_adaptive_min_confidence": ("confidence", "min"),
            "tactical_adaptive_min_net_edge_bps": ("net_edge_bps", "min"),
        }
        feature, sense = specs[key]
        result = []
        for row in rows:
            number = ManagedStrategyParameters._entry_value(row, feature)
            if number is None:
                continue
            if (sense == "min" and number >= value) or (sense == "max" and number <= value):
                result.append(row)
        return result

    def _optimize_tactical_entry(
        self, train: list[dict[str, Any]], validation: list[dict[str, Any]],
        current: dict[str, Any], sample_count: int,
    ) -> dict[str, Any]:
        base_train = _metrics(train, "net_return_pct")
        base_validation = _metrics(validation, "net_return_pct")
        train_min = max(25, int(len(train) * 0.60))
        val_min = max(12, int(len(validation) * 0.60))
        specs: dict[str, tuple[float, list[float]]] = {}
        min_steps = {
            "tactical_min_volatility_bps": (5.0, 10.0),
            "tactical_min_volume_ratio": (0.10, 0.20),
            "tactical_min_momentum_bps": (5.0, 10.0),
            "tactical_min_breakout_bps": (5.0, 10.0),
            "tactical_min_imbalance": (0.02, 0.04),
            "tactical_min_expected_move_bps": (10.0, 20.0),
            "tactical_adaptive_min_expected_move_bps": (10.0, 20.0),
            "tactical_adaptive_min_confidence": (0.02, 0.04),
            "tactical_adaptive_min_net_edge_bps": (5.0, 10.0),
        }
        max_steps = {
            "tactical_max_volatility_bps": (5.0, 10.0),
            "tactical_max_spread_bps": (2.0, 4.0),
        }
        for key, steps in min_steps.items():
            high = TACTICAL_BOUNDS[key][1]
            specs[key] = (
                float(current[key]),
                sorted(set([
                    float(current[key]),
                    min(high, float(current[key]) + steps[0]),
                    min(high, float(current[key]) + steps[1]),
                ])),
            )
        for key, steps in max_steps.items():
            low = TACTICAL_BOUNDS[key][0]
            specs[key] = (
                float(current[key]),
                sorted(set([
                    float(current[key]),
                    max(low, float(current[key]) - steps[0]),
                    max(low, float(current[key]) - steps[1]),
                ])),
            )

        candidates = []
        for key, (baseline_value, values) in specs.items():
            for value in values:
                if abs(value - baseline_value) < 1e-8:
                    continue
                filtered_train = self._filter_tactical(train, key, value)
                if len(filtered_train) < train_min:
                    continue
                score = _metrics(filtered_train, "net_return_pct")
                if (
                    score["average"] >= base_train["average"] + 0.10
                    and score["profit_factor"] >= base_train["profit_factor"]
                ):
                    candidates.append((score["average"], key, value, score, filtered_train))
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, key, value, train_score, _ in candidates:
            filtered_validation = self._filter_tactical(validation, key, value)
            if len(filtered_validation) < val_min:
                continue
            val_score = _metrics(filtered_validation, "net_return_pct")
            if (
                val_score["average"] >= base_validation["average"] + 0.10
                and val_score["profit_factor"] >= base_validation["profit_factor"] * 0.98
                and val_score["win_rate"] >= base_validation["win_rate"]
            ):
                parameters = dict(current)
                parameters[key] = value
                parameters = self._clean("tactical", parameters, current)
                return {
                    "status": "PROMOTED", "sample_count": sample_count,
                    "parameters": parameters,
                    "metrics": {
                        "optimized_parameter": key, "previous_value": current[key],
                        "selected_value": parameters[key], "training_baseline": base_train,
                        "training_candidate": train_score, "validation_baseline": base_validation,
                        "validation_candidate": val_score,
                        "validation_method": "chronological_70_30_holdout",
                        "objective": "realized_tactical_trade_net_return_pct",
                    },
                }
        return {"status": "NO_ENTRY_CANDIDATE", "sample_count": sample_count}

    @staticmethod
    def _simulate_exit(row: dict[str, Any], params: dict[str, Any]) -> float | None:
        detail = row["detail"]
        gross = _number(detail.get("gross_return_pct"))
        fee_pct = _number(detail.get("fees_pct"))
        adverse = _number(detail.get("max_adverse_pct"))
        peak = _number(detail.get("peak_gain_pct"))
        if gross is None or fee_pct is None or adverse is None or peak is None:
            return None
        stop_loss = float(params["tactical_stop_loss_pct"])
        take_profit = float(params["tactical_take_profit_pct"])
        trailing_stop = float(params["tactical_trailing_stop_pct"])
        # These extrema-based simulations are deliberately gated by a holdout;
        # they are an approximation, not a claim of tick-accurate execution.
        if adverse <= -stop_loss:
            gross = -stop_loss
        if peak >= take_profit:
            trailing_exit = peak - trailing_stop * (1.0 + peak / 100.0)
            if gross < trailing_exit:
                gross = trailing_exit
        return gross - fee_pct

    def _optimize_tactical_exits(
        self, train: list[dict[str, Any]], validation: list[dict[str, Any]],
        current: dict[str, Any], sample_count: int,
    ) -> dict[str, Any]:
        path_train = [row for row in train if self._simulate_exit(row, current) is not None]
        path_validation = [row for row in validation if self._simulate_exit(row, current) is not None]
        if len(path_train) < 40 or len(path_validation) < 18:
            return {"status": "INSUFFICIENT_PATH_DATA", "sample_count": sample_count}
        train_values = [self._simulate_exit(row, current) for row in path_train]
        val_values = [self._simulate_exit(row, current) for row in path_validation]
        train_records = [{"value": x} for x in train_values if x is not None]
        val_records = [{"value": x} for x in val_values if x is not None]
        base_train = _metrics(train_records, "value")
        base_validation = _metrics(val_records, "value")
        candidate_results: list[tuple[float, dict[str, Any], dict[str, float]]] = []
        for sl_factor in (0.90, 1.0, 1.10):
            for tp_factor in (0.90, 1.0, 1.10):
                for trail_factor in (0.90, 1.0, 1.10):
                    params = dict(current)
                    params["tactical_stop_loss_pct"] = max(0.3, min(2.5, current["tactical_stop_loss_pct"] * sl_factor))
                    params["tactical_take_profit_pct"] = max(1.0, min(6.0, current["tactical_take_profit_pct"] * tp_factor))
                    params["tactical_trailing_stop_pct"] = max(0.2, min(2.0, current["tactical_trailing_stop_pct"] * trail_factor))
                    if params["tactical_stop_loss_pct"] >= params["tactical_take_profit_pct"]:
                        continue
                    values = [self._simulate_exit(row, params) for row in path_train]
                    metric_rows = [{"value": x} for x in values if x is not None]
                    score = _metrics(metric_rows, "value")
                    if (
                        score["samples"] >= 40
                        and score["average"] >= base_train["average"] + 0.10
                        and score["profit_factor"] >= base_train["profit_factor"]
                    ):
                        candidate_results.append((score["average"], params, score))
        candidate_results.sort(key=lambda item: item[0], reverse=True)
        for _, params, train_score in candidate_results:
            values = [self._simulate_exit(row, params) for row in path_validation]
            metric_rows = [{"value": x} for x in values if x is not None]
            val_score = _metrics(metric_rows, "value")
            if (
                val_score["samples"] >= 18
                and val_score["average"] >= base_validation["average"] + 0.10
                and val_score["profit_factor"] >= base_validation["profit_factor"] * 0.98
            ):
                return {
                    "status": "PROMOTED", "sample_count": sample_count,
                    "parameters": self._clean("tactical", params, current),
                    "metrics": {
                        "optimized_parameters": [
                            "tactical_stop_loss_pct", "tactical_take_profit_pct",
                            "tactical_trailing_stop_pct",
                        ],
                        "training_baseline": base_train, "training_candidate": train_score,
                        "validation_baseline": base_validation, "validation_candidate": val_score,
                        "validation_method": "chronological_70_30_holdout",
                        "objective": "extrema_based_simulated_net_return_pct",
                        "simulation_warning": "Approximate extrema replay; real fills/slippage can differ.",
                    },
                }
        return {"status": "NO_EXIT_CANDIDATE", "sample_count": sample_count}
