from __future__ import annotations

from threading import RLock
from typing import Any


# Tunable policy parameters live in versioned model records, not HA options.
# Risk caps, credentials, execution switches and operational limits are intentionally
# excluded: the learner may never relax the immutable safety envelope.
CORE_DEFAULTS: dict[str, Any] = {
    "strategy_min_edge_bps": 25.0,
    "strategy_min_confidence": 0.58,
    "strategy_adaptive_edge_enabled": True,
    "strategy_adaptive_edge_floor_bps": 15.0,
    "strategy_adaptive_min_confidence": 0.75,
    "strategy_adaptive_cost_ratio": 1.10,
    "strategy_stop_loss_pct": 2.0,
    "strategy_partial_profit_trigger_pct": 10.0,
    "strategy_partial_profit_fraction_pct": 50.0,
    "strategy_profit_lock_trigger_pct": 10.0,
    "strategy_profit_giveback_pct": 35.0,
    "strategy_profit_lock_floor_pct": 5.0,
    "risk_max_position_pct": 15.0,
}

TACTICAL_DEFAULTS: dict[str, Any] = {
    "tactical_portfolio_pct": 25.0,
    "tactical_position_limit_pct": 80.0,
    "tactical_min_volatility_bps": 12.0,
    "tactical_max_volatility_bps": 55.0,
    "tactical_min_volume_ratio": 2.0,
    "tactical_min_momentum_bps": 40.0,
    "tactical_min_breakout_bps": 25.0,
    "tactical_min_imbalance": 0.10,
    "tactical_max_spread_bps": 25.0,
    "tactical_min_expected_move_bps": 280.0,
    "tactical_stop_loss_pct": 1.0,
    "tactical_take_profit_pct": 2.2,
    "tactical_trailing_trigger_bps": 100.0,
    "tactical_trailing_stop_pct": 0.7,
    "tactical_max_hold_seconds": 1800,
    "tactical_reversal_exit_bps": 120.0,
    "tactical_adaptive_entry_enabled": True,
    "tactical_adaptive_min_expected_move_bps": 230.0,
    "tactical_adaptive_min_confidence": 0.75,
    "tactical_adaptive_min_net_edge_bps": 15.0,
}


class AdaptiveConfig:
    """Atomic, runtime-refreshable view of base config plus promoted policy parameters."""

    def __init__(self, base: Any, registry: Any) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_overrides", {})
        object.__setattr__(self, "_lock", RLock())
        self.refresh()

    @property
    def __dict__(self) -> dict[str, Any]:
        # Keep config hashing and diagnostics compatible with the original dataclass.
        with self._lock:
            return {**self._base.__dict__, **self._overrides}

    def refresh(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for family, defaults in (
            ("strategy_core", CORE_DEFAULTS),
            ("strategy_tactical", TACTICAL_DEFAULTS),
        ):
            merged.update(defaults)
            try:
                version = self._registry.active(family)
                params = self._registry.parameters(version, family=family)
                if isinstance(params, dict):
                    for name, value in params.items():
                        if name in defaults:
                            merged[name] = value
            except Exception:
                # A registry read failure must fall back to validated defaults.
                continue
        # Keep type/range constraints independent of any model record.
        for name, bounds in {
            "strategy_min_edge_bps": (5.0, 250.0),
            "strategy_min_confidence": (0.50, 0.95),
            "strategy_adaptive_edge_floor_bps": (5.0, 100.0),
            "strategy_adaptive_min_confidence": (0.60, 0.98),
            "strategy_adaptive_cost_ratio": (1.05, 2.5),
            "strategy_stop_loss_pct": (0.5, 8.0),
            "strategy_partial_profit_trigger_pct": (3.0, 30.0),
            "strategy_partial_profit_fraction_pct": (20.0, 70.0),
            "strategy_profit_lock_trigger_pct": (3.0, 30.0),
            "strategy_profit_giveback_pct": (10.0, 60.0),
            "strategy_profit_lock_floor_pct": (1.0, 15.0),
            "risk_max_position_pct": (1.0, 15.0),
            "tactical_portfolio_pct": (1.0, 25.0),
            "tactical_position_limit_pct": (1.0, 80.0),
            "tactical_min_volatility_bps": (3.0, 100.0),
            "tactical_max_volatility_bps": (15.0, 150.0),
            "tactical_min_volume_ratio": (1.0, 8.0),
            "tactical_min_momentum_bps": (10.0, 300.0),
            "tactical_min_breakout_bps": (5.0, 250.0),
            "tactical_min_imbalance": (0.0, 0.8),
            "tactical_max_spread_bps": (3.0, 60.0),
            "tactical_min_expected_move_bps": (80.0, 1200.0),
            "tactical_stop_loss_pct": (0.3, 3.0),
            "tactical_take_profit_pct": (0.5, 8.0),
            "tactical_trailing_trigger_bps": (20.0, 500.0),
            "tactical_trailing_stop_pct": (0.2, 2.0),
            "tactical_max_hold_seconds": (60, 7200),
            "tactical_reversal_exit_bps": (20.0, 500.0),
            "tactical_adaptive_min_expected_move_bps": (80.0, 800.0),
            "tactical_adaptive_min_confidence": (0.60, 0.98),
            "tactical_adaptive_min_net_edge_bps": (5.0, 100.0),
        }.items():
            try:
                value = float(merged[name])
                merged[name] = max(bounds[0], min(bounds[1], value))
                if isinstance(bounds[0], int):
                    merged[name] = int(round(merged[name]))
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            object.__setattr__(self, "_overrides", merged)
        return dict(merged)

    def __getattr__(self, name: str) -> Any:
        with self._lock:
            if name in self._overrides:
                return self._overrides[name]
        return getattr(self._base, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Runtime operational state should not be mutated through this wrapper.
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._base, name, value)
