from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .analytics import model_hash


@dataclass
class Prediction:
    prediction_id: str
    model_version: str
    probability: Decimal
    expected_return: Decimal
    created_at: float
    outcome: Decimal | None = None


@dataclass
class Calibration:
    count: int = 0
    predicted_sum: Decimal = Decimal("0")
    realized_sum: Decimal = Decimal("0")
    edge_factor: Decimal = Decimal("1")

    def update(self, predicted_probability: Decimal, realized: Decimal) -> None:
        self.count += 1
        self.predicted_sum += predicted_probability
        self.realized_sum += realized
        error = realized - predicted_probability
        self.edge_factor = min(Decimal("1.5"), max(Decimal("0.5"), self.edge_factor + error * Decimal("0.1")))

    @property
    def probability_bias(self) -> Decimal:
        if not self.count:
            return Decimal("0")
        bias = (self.realized_sum / Decimal(self.count)) - (self.predicted_sum / Decimal(self.count))
        return min(Decimal("0.2"), max(Decimal("-0.2"), bias))


@dataclass
class ModelMetrics:
    trades: int = 0
    wins: int = 0
    gross_return: Decimal = Decimal("0")
    costs: Decimal = Decimal("0")
    drawdown: Decimal = Decimal("0")
    downside: Decimal = Decimal("0")

    @property
    def net_return(self) -> Decimal:
        return self.gross_return - self.costs

    @property
    def hit_rate(self) -> Decimal:
        return Decimal(self.wins) / Decimal(self.trades) if self.trades else Decimal("0")


class ModelRegistry:
    def __init__(self) -> None:
        self.active = "baseline"
        self.baseline = "baseline"
        self.candidates: dict[str, ModelMetrics] = {}
        self.metrics: dict[str, ModelMetrics] = {"baseline": ModelMetrics()}

    def register_candidate(self, version: str) -> None:
        self.candidates.setdefault(version, ModelMetrics())
        self.metrics.setdefault(version, ModelMetrics())

    def record(self, version: str, pnl: Decimal, cost: Decimal) -> None:
        m = self.metrics.setdefault(version, ModelMetrics())
        m.trades += 1
        m.wins += int(pnl > 0)
        m.gross_return += pnl
        m.costs += cost

    def evaluate_candidate(self, version: str) -> dict[str, Decimal]:
        m = self.metrics[version]
        return {
            "net_return": m.net_return,
            "hit_rate": m.hit_rate,
            "expectancy": m.net_return / Decimal(m.trades) if m.trades else Decimal("0"),
            "deflated_sharpe_proxy": (m.net_return / Decimal(max(m.trades, 1))) * Decimal("1.0"),
        }

    def promote(self, version: str) -> bool:
        if version not in self.candidates:
            return False
        candidate = self.evaluate_candidate(version)
        active = self.evaluate_candidate(self.active)
        if candidate["net_return"] > active["net_return"] and candidate["expectancy"] > Decimal("0"):
            self.active = version
            return True
        return False

    def rollback(self) -> None:
        self.active = self.baseline


class LearningEngine:
    def __init__(self) -> None:
        self.calibration = Calibration()
        self.registry = ModelRegistry()
        self.predictions: dict[str, Prediction] = {}
        self.non_trade_outcomes: list[Decimal] = []
        self.non_trade_predictions: dict[str, Decimal] = {}

    def create_prediction(self, prediction_id: str, probability: Decimal, expected_return: Decimal, now: float) -> Prediction:
        p = Prediction(prediction_id, self.registry.active, probability, expected_return, now)
        self.predictions[prediction_id] = p
        return p

    def record_outcome(self, prediction_id: str, realized: Decimal, cost: Decimal) -> None:
        p = self.predictions.get(prediction_id)
        if not p:
            return
        p.outcome = realized
        self.calibration.update(p.probability, Decimal("1") if realized > 0 else Decimal("0"))
        self.registry.record(p.model_version, realized, cost)

    def record_non_trade(self, prediction_id: str, expected_return: Decimal, realized_counterfactual: Decimal | None = None) -> None:
        """Record a blocked trade prediction and optionally resolve its counterfactual outcome."""
        if realized_counterfactual is None:
            self.non_trade_predictions[prediction_id] = expected_return
            return
        self.non_trade_predictions.pop(prediction_id, None)
        self.non_trade_outcomes.append(realized_counterfactual - expected_return)

    def resolve_non_trade(self, prediction_id: str, realized_counterfactual: Decimal) -> None:
        expected = self.non_trade_predictions.pop(prediction_id, None)
        if expected is not None:
            self.non_trade_outcomes.append(realized_counterfactual - expected)

    def calibrated(self, probability: Decimal) -> Decimal:
        return min(Decimal("0.99"), max(Decimal("0.01"), probability + self.calibration.probability_bias))

    def active_model_hash(self) -> str:
        return model_hash("baseline", self.registry.active)


def walk_forward_validate(returns: list[Decimal], train: int, test: int, step: int) -> list[dict[str, Decimal]]:
    if train <= 0 or test <= 0 or step <= 0:
        raise ValueError("train/test/step must be positive")
    out: list[dict[str, Decimal]] = []
    start = 0
    while start + train + test <= len(returns):
        train_set = returns[start : start + train]
        test_set = returns[start + train : start + train + test]
        mean_train = sum(train_set, Decimal("0")) / Decimal(len(train_set))
        mean_test = sum(test_set, Decimal("0")) / Decimal(len(test_set))
        downside = [min(Decimal("0"), r) for r in test_set]
        downside_var = sum((d * d for d in downside), Decimal("0")) / Decimal(max(1, len(downside)))
        out.append({
            "train_mean": mean_train,
            "oos_mean": mean_test,
            "downside_risk": downside_var.sqrt() if downside_var else Decimal("0"),
        })
        start += step
    return out
