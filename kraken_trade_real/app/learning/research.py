from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable


class ResearchEngine:
    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def metrics(returns: list[float]) -> dict[str, float]:
        clean = [float(value) for value in returns if math.isfinite(float(value))]
        if not clean:
            return {"samples": 0.0, "return": 0.0, "drawdown": 0.0, "sharpe": 0.0}
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for value in clean:
            equity *= max(0.0, 1.0 + value)
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1.0 if peak > 0 else -1.0)
        mean = sum(clean) / len(clean)
        var = sum((value - mean) ** 2 for value in clean) / max(1, len(clean) - 1)
        sharpe = mean / math.sqrt(var) * math.sqrt(252) if var > 0 else 0.0
        return {
            "samples": float(len(clean)), "return": equity - 1.0,
            "drawdown": max_dd, "sharpe": sharpe,
        }

    def walk_forward(
        self,
        data: list[dict[str, Any]],
        scorer: Callable[[list[dict[str, Any]], list[dict[str, Any]]], list[float]],
        windows: int = 3,
        embargo_samples: int = 0,
    ) -> dict[str, Any]:
        """Expanding-window, forward-only validation with an optional embargo.

        The scorer receives (train, test), making it possible to fit/calibrate only
        from prior observations. The earlier implementation constructed a 1-row
        first training set and never passed training data into the scorer at all.
        """
        windows = max(1, int(windows))
        embargo = max(0, int(embargo_samples))
        if len(data) < (windows + 1) * 20 + embargo * windows:
            return {"status": "INSUFFICIENT_DATA", "windows": []}

        ordered = sorted(data, key=lambda row: float(row.get("created_at", row.get("timestamp", 0))))
        test_size = (len(ordered) - embargo * windows) // (windows + 1)
        if test_size < 20:
            return {"status": "INSUFFICIENT_DATA", "windows": []}
        results: list[dict[str, Any]] = []
        for idx in range(windows):
            train_end = test_size * (idx + 1)
            test_start = train_end + embargo
            test_end = min(len(ordered), test_start + test_size)
            train = ordered[:train_end]
            test = ordered[test_start:test_end]
            if len(train) < 20 or len(test) < 20:
                continue
            train_hash = hashlib.sha256(
                json.dumps(train, sort_keys=True, default=str).encode()
            ).hexdigest()
            returns = scorer(train, test)
            metrics = self.metrics(returns)
            results.append({
                "window": idx + 1,
                "train_samples": len(train),
                "test_samples": len(test),
                "train_hash": train_hash,
                "train_start": float(train[0].get("created_at", train[0].get("timestamp", 0))),
                "train_end": float(train[-1].get("created_at", train[-1].get("timestamp", 0))),
                "test_start": float(test[0].get("created_at", test[0].get("timestamp", 0))),
                "test_end": float(test[-1].get("created_at", test[-1].get("timestamp", 0))),
                "metrics": metrics,
            })
        if not results:
            return {"status": "INSUFFICIENT_DATA", "windows": []}
        stable = sum(
            1 for result in results
            if isinstance(result["metrics"], dict)
            and float(result["metrics"]["return"]) > 0
        )
        return {
            "status": "OK", "windows": results, "stable_windows": stable,
            "stability": stable / len(results),
            "method": "expanding_chronological_walk_forward",
        }
