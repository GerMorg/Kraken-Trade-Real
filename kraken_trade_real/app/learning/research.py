
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
        if any(not math.isfinite(float(value)) or float(value) <= -1.0 for value in returns):
            raise ValueError("returns must be finite fractional returns greater than -100%")
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


    @classmethod
    def verified_trade_metrics(cls, trades: list[dict[str, Any]]) -> dict[str, Any]:
        """Score only independently verified net trade outcomes.

        Each included record must contain net_return_fraction after actual
        fees/FX/funding and net_verified=True. Price-only prediction labels,
        estimated fees, and unknown financing are intentionally excluded.
        This is an outcome scorer, not a market-data execution backtest.
        """
        verified: list[float] = []
        excluded = 0
        for trade in trades:
            if trade.get("net_verified") is not True:
                excluded += 1
                continue
            # Explicitly estimated/incomplete cost ledgers override a mistaken
            # caller-supplied boolean; verification must cover all cost legs.
            incomplete_cost = any(
                str(trade.get(key, "")).upper() in {
                    "ESTIMATED", "UNKNOWN", "UNVERIFIED", "QUOTE_ESTIMATE_ONLY",
                    "MISSING", "INCOMPLETE",
                }
                for key in ("fee_status", "fx_status", "funding_status")
            )
            if incomplete_cost:
                excluded += 1
                continue
            try:
                value = float(trade["net_return_fraction"])
            except (KeyError, TypeError, ValueError, OverflowError):
                excluded += 1
                continue
            if not math.isfinite(value) or value <= -1.0:
                excluded += 1
                continue
            verified.append(value)
        return {
            "status": "OK" if verified else "NO_VERIFIED_NET_OUTCOMES",
            "verified_samples": len(verified),
            "excluded_unverified_samples": excluded,
            "metrics": cls.metrics(verified) if verified else cls.metrics([]),
            "promotion_eligible": False,
            "limitation": "REALIZED_OUTCOME_SCORER_NOT_EXECUTION_BACKTEST",
        }

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
            "evaluation_only": True,
            "limitation": "SCORER_EVALUATES_TEST_ROWS_ONLY; NOT_A_FITTED_WALK_FORWARD_BACKTEST",
            "windows": results,
            "stable_windows": stable,
            "stability": stable / len(results),
        }
