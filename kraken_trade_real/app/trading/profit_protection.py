from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import Decision, Instrument, PortfolioState, Signal, new_id
from app.domain.states import Direction, ProductType

D = Decimal


def _d(value: Any, default: str = "0") -> D:
    try:
        return D(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return D(default)


class PositionProfitProtection:
    """Persistent partial-profit and trailing-profit protection for Spot Margin holdings.

    Profit percentages are measured against Kraken's current OpenPositions cost basis,
    not against account equity. Exchange risk gates remain authoritative for every exit.
    """

    def __init__(self, config: Any, db: Any, audit: Any) -> None:
        self.config = config
        self.db = db
        self.audit = audit

    def observe(self, portfolio: PortfolioState, cycle_id: str = "") -> None:
        """Persist PnL high-water marks and reconcile outstanding partial exits."""
        if not portfolio.spot_open_positions_read_ok:
            # A failed OpenPositions read must never erase a high-water mark or
            # conclude that a live position has disappeared.
            return

        active_symbols = {
            symbol for symbol, pct in portfolio.position_pnl_pct.items()
            if symbol and symbol in portfolio.positions and portfolio.positions.get(symbol, D("0")) != 0
        }
        rows = self.db.query(
            "SELECT symbol FROM position_profit_state"
        )
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol and symbol not in active_symbols:
                self.db.execute(
                    "DELETE FROM position_profit_state WHERE symbol=?",
                    (symbol,),
                )
                self.audit.emit(
                    "POSITION_PROFIT_STATE_CLEARED",
                    "INFO",
                    cycle_id=cycle_id,
                    symbol=symbol,
                    reason="EXCHANGE_CONFIRMED_POSITION_ABSENT",
                )

        for symbol in sorted(active_symbols):
            current_pct = _d(portfolio.position_pnl_pct.get(symbol))
            row = self._state(symbol)
            peak = max(_d(row.get("peak_profit_pct")) if row else D("0"), current_pct)
            if row is None:
                self.db.execute(
                    """INSERT INTO position_profit_state
                       (symbol, peak_profit_pct, partial_taken, pending_order_id,
                        pending_start_position_eur, updated_at)
                       VALUES(?,?,?,?,?,strftime('%s','now'))""",
                    (symbol, str(max(D("0"), peak)), 0, "", "0"),
                )
                row = self._state(symbol) or {}
            else:
                pending_id = str(row.get("pending_order_id") or "")
                partial_taken = bool(int(row.get("partial_taken") or 0))
                pending_start = _d(row.get("pending_start_position_eur"))
                if pending_id:
                    order = self.db.one(
                        "SELECT state FROM orders WHERE client_order_id=?",
                        (pending_id,),
                    )
                    order_state = str(order.get("state") or "") if order else ""
                    current_position = _d(portfolio.positions.get(symbol))
                    material_reduction = (
                        pending_start != 0
                        and abs(current_position) <= abs(pending_start) * D("0.90")
                    )
                    if material_reduction:
                        partial_taken = True
                        pending_id = ""
                        pending_start = D("0")
                        self.audit.emit(
                            "POSITION_PARTIAL_PROFIT_CONFIRMED",
                            "INFO",
                            cycle_id=cycle_id,
                            symbol=symbol,
                            order_state=order_state,
                            current_position_eur=str(current_position),
                        )
                    elif order_state in {"CANCELED", "EXPIRED", "REJECTED"}:
                        # No meaningful reduction took place. Permit a later retry
                        # after a fresh portfolio snapshot rather than locking out
                        # profit-taking forever on a canceled/unfilled limit.
                        pending_id = ""
                        pending_start = D("0")
                        self.audit.emit(
                            "POSITION_PARTIAL_PROFIT_RETRY_READY",
                            "WARNING",
                            cycle_id=cycle_id,
                            symbol=symbol,
                            order_state=order_state,
                        )
                    elif order_state == "FILLED":
                        # A terminal fill without a visible size change can be a
                        # stale portfolio snapshot or a small partial. Keep the
                        # pending guard until a fresh reconciliation confirms size.
                        pass
                self.db.execute(
                    """UPDATE position_profit_state
                       SET peak_profit_pct=?, partial_taken=?, pending_order_id=?,
                           pending_start_position_eur=?, updated_at=strftime('%s','now')
                       WHERE symbol=?""",
                    (str(max(D("0"), peak)), int(partial_taken), pending_id,
                     str(pending_start), symbol),
                )
            if current_pct >= peak and peak > D("0"):
                self.audit.emit(
                    "POSITION_PROFIT_PEAK_UPDATED",
                    "INFO",
                    cycle_id=cycle_id,
                    symbol=symbol,
                    peak_profit_pct=str(peak),
                    current_profit_pct=str(current_pct),
                )

    def apply(
        self,
        *,
        cycle_id: str,
        instrument: Instrument,
        portfolio: PortfolioState,
        decision: Decision | None,
        long_signal: Signal,
        short_signal: Signal,
        model_version: str,
        config_hash: str,
        min_cost_eur: D,
    ) -> Decision | None:
        """Apply a profit-management override or forbid increasing a partly taken position."""
        if (
            instrument.venue != "spot"
            or instrument.product_type != ProductType.SPOT_MARGIN
        ):
            return decision

        symbol = instrument.symbol
        current = _d(portfolio.positions.get(symbol))
        pnl_pct_value = portfolio.position_pnl_pct.get(symbol)
        if current == 0 or pnl_pct_value is None:
            return decision

        row = self._state(symbol)
        if row is None:
            # observe() normally creates this record; make the boundary robust if
            # a caller invokes apply() directly in tests or after partial startup.
            self.db.execute(
                """INSERT INTO position_profit_state
                   (symbol, peak_profit_pct, partial_taken, pending_order_id,
                    pending_start_position_eur, updated_at)
                   VALUES(?,?,?,?,?,strftime('%s','now'))""",
                (symbol, str(max(D("0"), _d(pnl_pct_value))), 0, "", "0"),
            )
            row = self._state(symbol) or {}

        current_pct = _d(pnl_pct_value)
        peak_pct = max(_d(row.get("peak_profit_pct")), current_pct)
        partial_taken = bool(int(row.get("partial_taken") or 0))
        pending_id = str(row.get("pending_order_id") or "")
        lock_activation = D(str(getattr(self.config, "strategy_profit_lock_trigger_pct", 5.0)))
        giveback_pct = D(str(getattr(self.config, "strategy_profit_giveback_pct", 35.0)))
        profit_floor = D(str(getattr(self.config, "strategy_profit_lock_floor_pct", 2.0)))
        partial_trigger = D(str(getattr(self.config, "strategy_partial_profit_trigger_pct", 3.5)))
        partial_fraction = D(str(getattr(self.config, "strategy_partial_profit_fraction_pct", 50.0))) / D("100")

        action = ""
        lock_threshold = max(D("0"), peak_pct * (D("1") - giveback_pct / D("100")))
        lock_threshold = max(lock_threshold, profit_floor)
        if peak_pct >= lock_activation and current_pct <= lock_threshold:
            action = "TRAILING_PROFIT_EXIT"
        elif current_pct >= partial_trigger and not partial_taken and not pending_id:
            action = "PARTIAL_TAKE_PROFIT"

        if not action:
            # Once a partial gain has been taken, don't allow the normal sizing
            # strategy to immediately rebuild the same exposure in the same asset.
            if partial_taken and decision is not None:
                desired = _d(decision.target_position_eur)
                expands_same_side = (
                    desired != 0
                    and desired * current > 0
                    and abs(desired) > abs(current)
                )
                if expands_same_side:
                    self.audit.emit(
                        "POSITION_PROFIT_REENTRY_BLOCKED",
                        "WARNING",
                        cycle_id=cycle_id,
                        symbol=symbol,
                        current_position_eur=str(current),
                        requested_target_position_eur=str(desired),
                        partial_taken=True,
                    )
                    return None
            return decision

        target = D("0") if action == "TRAILING_PROFIT_EXIT" else current * (D("1") - partial_fraction)
        if action == "PARTIAL_TAKE_PROFIT" and decision is not None:
            desired = _d(decision.target_position_eur)
            # Keep an existing full exit or stronger reduction; a profit-taking
            # instruction must never weaken a signal-driven reduction.
            if desired == 0 or (
                desired * current > 0 and abs(desired) < abs(target)
            ):
                return decision

        delta = target - current
        if delta == 0:
            return decision
        execution_direction = Direction.LONG if delta > 0 else Direction.SHORT
        held_signal = short_signal if current < 0 else long_signal
        rationale = dict(decision.rationale) if decision is not None else {}
        rationale.update({
            "position_management_action": action,
            "position_profit_pct": str(current_pct),
            "position_profit_peak_pct": str(peak_pct),
            "position_profit_lock_threshold_pct": str(lock_threshold),
            "position_profit_partial_fraction_pct": str(partial_fraction * D("100")),
            "risk_profile": "core",
            "min_cost_eur": str(min_cost_eur),
        })

        if decision is None:
            managed = Decision(
                decision_id=new_id("decision"),
                instrument=instrument,
                signal=held_signal,
                target_notional_eur=abs(delta),
                leverage=D("1"),
                rationale=rationale,
                strategy_version="core-v2-profit-protection",
                model_version=model_version,
                config_hash=config_hash,
                current_position_eur=current,
                target_position_eur=target,
                execution_direction=execution_direction,
                reduce_only=True,
            )
        else:
            managed = replace(
                decision,
                target_notional_eur=abs(delta),
                rationale=rationale,
                current_position_eur=current,
                target_position_eur=target,
                execution_direction=execution_direction,
                reduce_only=True,
            )
        self.audit.emit(
            "POSITION_PROFIT_PROTECTION_TRIGGERED",
            "WARNING",
            cycle_id=cycle_id,
            symbol=symbol,
            action=action,
            current_profit_pct=str(current_pct),
            peak_profit_pct=str(peak_pct),
            lock_threshold_pct=str(lock_threshold),
            current_position_eur=str(current),
            target_position_eur=str(target),
            reduce_only=True,
        )
        return managed

    def record_pending(self, symbol: str, client_order_id: str, current_position_eur: D) -> None:
        """Record an accepted partial-profit order until the next exchange reconciliation."""
        row = self._state(symbol)
        peak = _d(row.get("peak_profit_pct")) if row else D("0")
        taken = int(row.get("partial_taken") or 0) if row else 0
        self.db.execute(
            """INSERT INTO position_profit_state
               (symbol, peak_profit_pct, partial_taken, pending_order_id,
                pending_start_position_eur, updated_at)
               VALUES(?,?,?,?,?,strftime('%s','now'))
               ON CONFLICT(symbol) DO UPDATE SET
                 peak_profit_pct=excluded.peak_profit_pct,
                 partial_taken=excluded.partial_taken,
                 pending_order_id=excluded.pending_order_id,
                 pending_start_position_eur=excluded.pending_start_position_eur,
                 updated_at=excluded.updated_at""",
            (symbol, str(peak), taken, client_order_id, str(current_position_eur)),
        )
        self.audit.emit(
            "POSITION_PARTIAL_PROFIT_ORDER_TRACKED",
            "INFO",
            symbol=symbol,
            client_order_id=client_order_id,
            starting_position_eur=str(current_position_eur),
        )

    def _state(self, symbol: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT symbol,peak_profit_pct,partial_taken,pending_order_id,"
            "pending_start_position_eur FROM position_profit_state WHERE symbol=?",
            (symbol,),
        )
