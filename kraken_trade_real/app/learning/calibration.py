from __future__ import annotations

import math


class CalibrationEngine:
    def evaluate(self, pairs: list[tuple[float, bool]], bins: int = 10) -> dict:
        """Compute Brier score and ECE from finite, valid probability/outcome pairs."""
        valid = []
        dropped = 0
        for probability, outcome in pairs:
            try:
                p = float(probability)
            except (TypeError, ValueError):
                dropped += 1
                continue
            if not math.isfinite(p) or p < 0.0 or p > 1.0:
                dropped += 1
                continue
            valid.append((p, bool(outcome)))
        if not valid:
            return {
                "sample_count": 0, "brier": 1.0, "ece": 1.0, "bins": [],
                "dropped_samples": dropped,
            }
        brier = sum((p - (1 if y else 0)) ** 2 for p, y in valid) / len(valid)
        groups = []
        ece = 0.0
        count_bins = max(1, int(bins))
        for index in range(count_bins):
            lower = index / count_bins
            upper = (index + 1) / count_bins
            rows = [
                (p, y) for p, y in valid
                if lower <= p < upper or (index == count_bins - 1 and p == 1.0)
            ]
            if not rows:
                continue
            confidence = sum(p for p, _ in rows) / len(rows)
            accuracy = sum(1 for _, y in rows if y) / len(rows)
            ece += len(rows) / len(valid) * abs(accuracy - confidence)
            groups.append({
                "bin": index, "count": len(rows),
                "confidence": confidence, "accuracy": accuracy,
            })
        return {
            "sample_count": len(valid), "brier": brier, "ece": ece,
            "bins": groups, "dropped_samples": dropped,
        }
