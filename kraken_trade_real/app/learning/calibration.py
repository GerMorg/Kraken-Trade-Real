from __future__ import annotations

import math


class CalibrationEngine:
    """Evaluate probabilistic forecasts without hiding invalid or tiny samples."""

    def evaluate(
        self, pairs: list[tuple[float, bool]], bins: int = 10
    ) -> dict:
        if bins < 1:
            raise ValueError("bins must be at least one")
        if not pairs:
            return {
                "sample_count": 0,
                "brier": 1.0,
                "log_loss": math.log(2.0),
                "ece": 1.0,
                "bins": [],
                "status": "INSUFFICIENT_DATA",
            }

        normalized: list[tuple[float, bool]] = []
        for probability, observed in pairs:
            p = float(probability)
            if not math.isfinite(p) or p < 0.0 or p > 1.0:
                raise ValueError("probabilities must be finite values in [0, 1]")
            if not isinstance(observed, (bool, int)) or observed not in (False, True, 0, 1):
                raise ValueError("outcomes must be boolean values")
            normalized.append((p, bool(observed)))

        epsilon = 1e-15
        brier = sum((p - float(y)) ** 2 for p, y in normalized) / len(normalized)
        log_loss = sum(
            -(float(y) * math.log(min(1.0 - epsilon, max(epsilon, p)))
              + (1.0 - float(y)) * math.log1p(-min(1.0 - epsilon, max(epsilon, p))))
            for p, y in normalized
        ) / len(normalized)

        groups: list[dict[str, float | int]] = []
        ece = 0.0
        for index in range(bins):
            lower = index / bins
            upper = (index + 1) / bins
            rows = [
                (p, y) for p, y in normalized
                if lower <= p < upper or (index == bins - 1 and p == 1.0)
            ]
            if not rows:
                continue
            confidence = sum(p for p, _ in rows) / len(rows)
            accuracy = sum(1 for _, y in rows if y) / len(rows)
            ece += len(rows) / len(normalized) * abs(accuracy - confidence)
            groups.append({
                "bin": index,
                "count": len(rows),
                "confidence": confidence,
                "accuracy": accuracy,
            })
        return {
            "sample_count": len(normalized),
            "brier": brier,
            "log_loss": log_loss,
            "ece": ece,
            "bins": groups,
            "status": "OK",
        }
