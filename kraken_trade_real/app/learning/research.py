
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
        if not returns:
            return {"samples": 0.0, "return": 0.0, "drawdown": 0.0, "sharpe": 0.0}
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for value in returns:
            equity *= 1 + value
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1)
        mean = sum(returns) / len(returns)
        var = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
        sharpe = mean / math.sqrt(var) * math.sqrt(252) if var > 0 else 0.0
        return {"samples": float(len(returns)), "return": equity - 1, "drawdown": max_dd, "sharpe": sharpe}

    def walk_forward(
        self,
        data: list[dict[str, Any]],
        scorer: Callable[[list[dict[str, Any]]], list[float]],
        windows: int = 3,
    ) -> dict[str, Any]:
        if len(data) < windows * 20:
            return {"status": "INSUFFICIENT_DATA", "windows": []}
        size = len(data) // windows
        results: list[dict[str, Any]] = []
        for idx in range(1, windows + 1):
            train = data[:max(1, idx * size - size)]
            test = data[max(1, idx * size - size):idx * size]
            train_hash = hashlib.sha256(
                json.dumps(train, sort_keys=True, default=str).encode()
            ).hexdigest()
            returns = scorer(test)
            results.append({
                "window": idx,
                "train_samples": len(train),
                "test_samples": len(test),
                "train_hash": train_hash,
                "metrics": self.metrics(returns),
            })
        stable = 0
        for result in results:
            metrics = result["metrics"]
            if isinstance(metrics, dict) and float(metrics["return"]) > 0:
                stable += 1
        return {
            "status": "OK",
            "windows": results,
            "stable_windows": stable,
            "stability": stable / len(results),
        }
