from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable


class ResearchEngine:
    """Chronological evaluation helpers. Returns supplied here must already be net of costs."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def metrics(
        returns: list[float], *, periods_per_year: float = 252.0
    ) -> dict[str, float]:
        if not returns:
            return {
                "samples": 0.0, "return": 0.0, "drawdown": 0.0,
                "max_drawdown": 0.0, "sharpe": 0.0, "sortino": 0.0,
                "expectancy": 0.0, "win_rate": 0.0, "tail_loss_5pct": 0.0,
                "longest_recovery_periods": 0.0,
            }
        if not math.isfinite(float(periods_per_year)) or periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive and finite")
        clean = [float(value) for value in returns]
        if any(not math.isfinite(value) or value <= -1.0 for value in clean):
            raise ValueError("returns must be finite and greater than -100%")
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        peak_index = 0
        longest_recovery = 0
        last_peak_index = 0
        for index, value in enumerate(clean, start=1):
            equity *= 1.0 + value
            if equity >= peak:
                peak = equity
                last_peak_index = index
            else:
                max_dd = min(max_dd, equity / peak - 1.0)
                longest_recovery = max(longest_recovery, index - last_peak_index)
        mean = sum(clean) / len(clean)
        variance = sum((value - mean) ** 2 for value in clean) / max(1, len(clean) - 1)
        std = math.sqrt(variance)
        sharpe = mean / std * math.sqrt(periods_per_year) if std > 0 else 0.0
        downside = [min(0.0, value) for value in clean]
        downside_rms = math.sqrt(sum(value * value for value in downside) / len(downside))
        sortino = mean / downside_rms * math.sqrt(periods_per_year) if downside_rms > 0 else 0.0
        ordered = sorted(clean)
        tail_count = max(1, math.ceil(len(ordered) * 0.05))
        tail_loss = sum(ordered[:tail_count]) / tail_count
        return {
            "samples": float(len(clean)),
            "return": equity - 1.0,
            "drawdown": max_dd,
            "max_drawdown": max_dd,
            "sharpe": sharpe,
            "sortino": sortino,
            "expectancy": mean,
            "win_rate": sum(value > 0 for value in clean) / len(clean),
            "tail_loss_5pct": tail_loss,
            "longest_recovery_periods": float(longest_recovery),
        }

    def walk_forward(
        self,
        data: list[dict[str, Any]],
        scorer: Callable[[list[dict[str, Any]]], list[float]],
        windows: int = 3,
        *,
        train_test_scorer: Callable[
            [list[dict[str, Any]], list[dict[str, Any]]], list[float]
        ] | None = None,
    ) -> dict[str, Any]:
        """Evaluate disjoint future windows after a minimum initial training history.

        Without train_test_scorer this evaluates an already-frozen scorer on each
        future window; it does not fit or promote parameters. A supplied train_test_scorer
        receives only the past training window and the subsequent test window.
        """
        if windows < 1:
            return {"status": "INVALID_WINDOWS", "windows": []}
        if len(data) < max(60, windows * 20):
            return {"status": "INSUFFICIENT_DATA", "windows": []}
        timestamp_rows = [
            row.get("timestamp", row.get("captured_at")) for row in data
        ]
        present_timestamps = [value for value in timestamp_rows if value is not None]
        if present_timestamps and len(present_timestamps) != len(data):
            return {"status": "MIXED_TIMESTAMP_PROVENANCE", "windows": []}
        if present_timestamps:
            try:
                parsed = [float(value) for value in timestamp_rows]
            except (TypeError, ValueError):
                return {"status": "INVALID_TIMESTAMPS", "windows": []}
            if any(not math.isfinite(value) for value in parsed):
                return {"status": "INVALID_TIMESTAMPS", "windows": []}
            if any(parsed[i] > parsed[i + 1] for i in range(len(parsed) - 1)):
                return {"status": "NON_CHRONOLOGICAL_DATA", "windows": []}

        initial_train = max(20, len(data) // (windows + 1))
        remaining = len(data) - initial_train
        if remaining < windows:
            return {"status": "INSUFFICIENT_DATA", "windows": []}
        base_size, extra = divmod(remaining, windows)
        results: list[dict[str, Any]] = []
        test_start = initial_train
        for idx in range(windows):
            test_size = base_size + (1 if idx < extra else 0)
            test_end = test_start + test_size
            train = data[:test_start]
            test = data[test_start:test_end]
            if not train or not test:
                return {"status": "INSUFFICIENT_DATA", "windows": results}
            if present_timestamps and float(train[-1].get("timestamp", train[-1].get("captured_at"))) > float(
                test[0].get("timestamp", test[0].get("captured_at"))
            ):
                return {"status": "TEMPORAL_OVERLAP", "windows": results}
            train_hash = hashlib.sha256(
                json.dumps(train, sort_keys=True, default=str).encode()
            ).hexdigest()
            test_hash = hashlib.sha256(
                json.dumps(test, sort_keys=True, default=str).encode()
            ).hexdigest()
            returns = (
                train_test_scorer(train, test)
                if train_test_scorer is not None
                else scorer(test)
            )
            metrics = self.metrics(returns)
            results.append({
                "window": idx + 1,
                "train_samples": len(train),
                "test_samples": len(test),
                "train_start_index": 0,
                "train_end_index_exclusive": test_start,
                "test_start_index": test_start,
                "test_end_index_exclusive": test_end,
                "train_hash": train_hash,
                "test_hash": test_hash,
                "evaluation_mode": (
                    "TRAIN_TEST_SCORER" if train_test_scorer is not None
                    else "FROZEN_SCORER"
                ),
                "metrics": metrics,
            })
            test_start = test_end
        stable = sum(
            float(result["metrics"]["return"]) > 0 for result in results
        )
        return {
            "status": "OK",
            "windows": results,
            "stable_windows": stable,
            "stability": stable / len(results),
            "method": "EXPANDING_WINDOW_DISJOINT_TEST",
            "parameters_promoted": False,
        }
