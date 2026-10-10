from __future__ import annotations

from dataclasses import replace
import json
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

    def _exit_policy_snapshot(self) -> dict[str, str]:
        """Capture the exact non-risk Core exit policy used for each episode observation."""
        keys = (
            "strategy_stop_loss_pct",
            "strategy_partial_profit_trigger_pct",
            "strategy_partial_profit_fraction_pct",
            "strategy_profit_lock_trigger_pct",
            "strategy_profit_giveback_pct",
            "strategy_profit_lock_floor_pct",
        )
        defaults = {
            "strategy_stop_loss_pct": 2.0,
            "strategy_partial_profit_trigger_pct": 10.0,
            "strategy_partial_profit_fraction_pct": 50.0,
            "strategy_profit_lock_trigger_pct": 10.0,
            "strategy_profit_giveback_pct": 35.0,
            "strategy_profit_lock_floor_pct": 5.0,
        }
        return {
            key: str(getattr(self.config, key, defaults[key]))
            for key in keys
        }

    def _position_exit_close_reason(self, symbol: str, opened_at: float) -> str:
        rows = self.db.query(
            """SELECT o.created_at,d.rationale_json
               FROM orders AS o
               JOIN decisions AS d ON d.decision_id=o.decision_id
               WHERE o.symbol=? AND o.reduce_only=1 AND o.created_at>=?
               ORDER BY o.created_at DESC LIMIT 20""",
            (symbol, float(opened_at)),
        )
        for row in rows:
            try:
                rationale = json.loads(row.get("rationale_json") or "{}")
            except (TypeError, ValueError):
                rationale = {}
            if not isinstance(rationale, dict):
                continue
            action = str(rationale.get("position_management_action") or "")
            if action in {"STOP_LOSS_EXIT", "TRAILING_PROFIT_EXIT"}:
                return action
            if action == "PARTIAL_TAKE_PROFIT":
                # This can only close an episode if the actual remaining position
                # disappeared; keep the specific trigger for later validation.
                return action
            if str(rationale.get("risk_profile") or "").lower() == "core":
                return "STRATEGY_REDUCTION"
        return "EXTERNAL_OR_UNATTRIBUTED_CLOSE"

    def _ensure_core_exit_episode(
        self,
        portfolio: PortfolioState,
        symbol: str,
        cycle_id: str,
        observed_at: float,
        current_pct: D,
    ) -> dict[str, Any] | None:
        """Create or reuse a position epoch; never label an inherited/manual path learnable."""
        order_ids = sorted({
            str(value).strip()
            for value in getattr(portfolio, "spot_margin_open_order_ids", {}).get(symbol, ())
            if str(value).strip()
        })
        direction = str(
            getattr(portfolio, "spot_margin_position_directions", {}).get(symbol, "MIXED")
            or "MIXED"
        ).upper()
        lot_count = int(
            getattr(portfolio, "spot_margin_open_lot_count", {}).get(symbol, len(order_ids))
            or 0
        )
        active = self.db.active_core_exit_episode(symbol)
        same_identity = False
        if active is not None:
            try:
                stored_ids = sorted(json.loads(active.get("open_order_ids_json") or "[]"))
            except (TypeError, ValueError):
                stored_ids = []
            same_identity = (
                stored_ids == order_ids
                and str(active.get("direction") or "").upper() == direction
            )
            if not same_identity:
                self.db.close_core_exit_episode(
                    str(active["episode_id"]), observed_at, "POSITION_IDENTITY_CHANGED"
                )
                self.db.execute(
                    "DELETE FROM position_profit_state WHERE symbol=?",
                    (symbol,),
                )
                self.audit.emit(
                    "CORE_EXIT_EPISODE_ROTATED",
                    "INFO",
                    cycle_id=cycle_id,
                    symbol=symbol,
                    old_episode_id=str(active["episode_id"]),
                    old_order_ids=stored_ids,
                    new_order_ids=order_ids,
                    old_direction=str(active.get("direction") or ""),
                    new_direction=direction,
                )
                active = None
        if active is not None and same_identity:
            return active

        cycle = self.db.one(
            "SELECT started_at FROM cycles WHERE cycle_id=?",
            (cycle_id,),
        ) if cycle_id else None
        try:
            cycle_started_at = float(cycle["started_at"]) if cycle and cycle.get("started_at") is not None else None
        except (TypeError, ValueError):
            cycle_started_at = None

        local_rows: list[dict[str, Any]] = []
        if order_ids:
            marks = ",".join("?" for _ in order_ids)
            local_rows = self.db.query(
                f"""SELECT o.kraken_order_id,o.decision_id,o.direction,o.reduce_only,
                           o.state,o.created_at,o.submitted_at,d.strategy_version
                    FROM orders AS o
                    JOIN decisions AS d ON d.decision_id=o.decision_id
                    WHERE o.symbol=? AND o.kraken_order_id IN ({marks})
                    ORDER BY o.created_at DESC""",
                tuple([symbol, *order_ids]),
            )
        opening_rows = [
            row for row in local_rows
            if not bool(int(row.get("reduce_only") or 0))
            and str(row.get("direction") or "").upper() == direction
            and "tactical" not in str(row.get("strategy_version") or "").lower()
        ]
        decision_ids = sorted({
            str(row.get("decision_id") or "")
            for row in opening_rows if str(row.get("decision_id") or "")
        })
        matching_open_order = (
            opening_rows[0]
            if len(order_ids) == 1
            and len(opening_rows) == 1
            and str(opening_rows[0].get("kraken_order_id") or "") == order_ids[0]
            else None
        )
        has_pnl = symbol in portfolio.position_pnl_pct
        basis = _d(portfolio.position_basis_eur.get(symbol))
        quantity = _d(portfolio.position_quantity.get(symbol))
        data_complete = has_pnl and basis > 0 and quantity > 0
        order_created_at = None
        if matching_open_order is not None:
            try:
                order_created_at = float(
                    matching_open_order.get("submitted_at")
                    or matching_open_order.get("created_at")
                )
            except (TypeError, ValueError):
                order_created_at = None
        current_cycle_open = (
            matching_open_order is not None
            and order_created_at is not None
            and cycle_started_at is not None
            and float(matching_open_order.get("created_at") or 0.0) >= cycle_started_at - 1.0
        )
        eligible = bool(
            direction in {"LONG", "SHORT"}
            and len(order_ids) == 1
            and lot_count == 1
            and matching_open_order is not None
            and len(decision_ids) == 1
            and current_cycle_open
            and data_complete
        )
        if eligible:
            reason = "APP_MANAGED_SINGLE_ORDER_CAPTURED_FROM_ENTRY_CYCLE"
            episode_opened_at = min(order_created_at or observed_at, observed_at)
        elif direction not in {"LONG", "SHORT"}:
            reason = "MIXED_OR_UNKNOWN_POSITION_DIRECTION"
            episode_opened_at = observed_at
        elif not data_complete:
            reason = "POSITION_PNL_OR_BASIS_UNAVAILABLE"
            episode_opened_at = observed_at
        elif len(order_ids) != 1 or lot_count != 1:
            reason = "MULTIPLE_OPEN_MARGIN_LOTS"
            episode_opened_at = observed_at
        elif matching_open_order is None or len(decision_ids) != 1:
            reason = "EXTERNAL_OR_UNATTRIBUTED_POSITION"
            episode_opened_at = observed_at
        elif not current_cycle_open:
            reason = "POSITION_PREDATES_PATH_CAPTURE"
            episode_opened_at = observed_at
        else:
            reason = "PATH_NOT_ELIGIBLE"
            episode_opened_at = observed_at

        policy = self._exit_policy_snapshot()
        previous_state = self._state(symbol)
        peak = max(
            current_pct,
            _d(previous_state.get("peak_profit_pct")) if previous_state else D("0"),
        )
        episode_id = new_id("core_exit_episode")
        self.db.create_core_exit_episode(
            episode_id=episode_id,
            symbol=symbol,
            direction=direction,
            opened_at=episode_opened_at,
            observed_at=observed_at,
            order_ids=order_ids,
            decision_ids=decision_ids,
            open_lot_count=lot_count,
            basis_eur=basis,
            first_profit_pct=current_pct,
            peak_profit_pct=peak,
            eligible_for_learning=eligible,
            eligibility_reason=reason,
            exit_policy_snapshot=policy,
            detail={
                "source": "KRAKEN_OPENPOSITIONS_AGGREGATED",
                "cycle_id": cycle_id,
                "cycle_started_at": cycle_started_at,
                "order_created_at": order_created_at,
                "position_quantity": str(quantity),
                "position_pnl_eur": str(portfolio.position_pnl_eur.get(symbol, D("0"))),
            },
        )
        self.audit.emit(
            "CORE_EXIT_EPISODE_STARTED",
            "INFO",
            cycle_id=cycle_id,
            episode_id=episode_id,
            symbol=symbol,
            direction=direction,
            eligible_for_learning=eligible,
            eligibility_reason=reason,
            order_ids=order_ids,
            open_lot_count=lot_count,
        )
        return self.db.active_core_exit_episode(symbol)

    def observe(self, portfolio: PortfolioState, cycle_id: str = "") -> None:
        """Persist PnL high-water marks and reconcile outstanding partial exits."""
        if not portfolio.spot_open_positions_read_ok:
            # A failed OpenPositions read must never erase a high-water mark or
            # conclude that a live position has disappeared.
            return

        active_symbols = set(portfolio.spot_margin_position_symbols)
        active_symbols.update(
            symbol for symbol in portfolio.position_pnl_pct
            if symbol and symbol in portfolio.positions and portfolio.positions.get(symbol, D("0")) != 0
        )
        # A confirmed successful OpenPositions read is the only signal allowed
        # to close old path episodes. An API failure returned above without erasing
        # either high-water marks or learning evidence.
        try:
            for episode in self.db.query(
                "SELECT episode_id,symbol,opened_at FROM core_exit_episodes WHERE closed_at IS NULL"
            ):
                episode_symbol = str(episode.get("symbol") or "")
                if episode_symbol and episode_symbol not in active_symbols:
                    episode_id = str(episode.get("episode_id") or "")
                    opened_at = float(episode.get("opened_at") or 0.0)
                    close_reason = self._position_exit_close_reason(episode_symbol, opened_at)
                    if self.db.close_core_exit_episode(episode_id, time.time(), close_reason):
                        self.audit.emit(
                            "CORE_EXIT_EPISODE_CLOSED",
                            "INFO",
                            cycle_id=cycle_id,
                            episode_id=episode_id,
                            symbol=episode_symbol,
                            close_reason=close_reason,
                        )
        except Exception as exc:
            # Path-ledger failures must not prevent live protection from continuing.
            self.audit.emit(
                "CORE_EXIT_EPISODE_CLOSE_FAILED",
                "WARNING",
                cycle_id=cycle_id,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )

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

        observation_time = float(portfolio.source_timestamp or time.time())
        for symbol in sorted(active_symbols):
            current_pct = _d(portfolio.position_pnl_pct.get(symbol))
            episode = None
            try:
                episode = self._ensure_core_exit_episode(
                    portfolio, symbol, cycle_id, observation_time, current_pct
                )
            except Exception as exc:
                # Exit-learning bookkeeping is isolated from live stop/profit logic.
                self.audit.emit(
                    "CORE_EXIT_EPISODE_SETUP_FAILED",
                    "WARNING",
                    cycle_id=cycle_id,
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
            row = self._state(symbol)
            previous_peak = _d(row.get("peak_profit_pct")) if row else D("0")
            peak = max(previous_peak, current_pct)
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
            if current_pct > previous_peak and peak > D("0"):
                self.audit.emit(
                    "POSITION_PROFIT_PEAK_UPDATED",
                    "INFO",
                    cycle_id=cycle_id,
                    symbol=symbol,
                    peak_profit_pct=str(peak),
                    current_profit_pct=str(current_pct),
                )

            # One path sample per cycle; the later/final reconciliation upserts
            # the same cycle row so a duplicate observation cannot bias training.
            try:
                episode = self.db.active_core_exit_episode(symbol) or episode
            except Exception as exc:
                self.audit.emit(
                    "CORE_EXIT_EPISODE_LOOKUP_FAILED",
                    "WARNING",
                    cycle_id=cycle_id,
                    symbol=symbol,
                    error_type=type(exc).__name__,
                )
            if episode is not None:
                final_state = self._state(symbol) or {}
                basis = _d(portfolio.position_basis_eur.get(symbol))
                quantity = _d(portfolio.position_quantity.get(symbol))
                quality = {
                    "open_positions_read_ok": bool(portfolio.spot_open_positions_read_ok),
                    "pnl_present": symbol in portfolio.position_pnl_pct,
                    "basis_positive": basis > 0,
                    "quantity_positive": quantity > 0,
                    "direction": str(
                        getattr(portfolio, "spot_margin_position_directions", {}).get(
                            symbol, "MIXED"
                        )
                    ),
                    "open_order_ids": list(
                        getattr(portfolio, "spot_margin_open_order_ids", {}).get(symbol, ())
                    ),
                    "open_lot_count": int(
                        getattr(portfolio, "spot_margin_open_lot_count", {}).get(symbol, 0)
                        or 0
                    ),
                    "eligible_for_learning": bool(int(episode.get("eligible_for_learning") or 0)),
                    "eligibility_reason": str(episode.get("eligibility_reason") or ""),
                }
                try:
                    self.db.record_core_exit_observation(
                        episode_id=str(episode["episode_id"]),
                        cycle_id=cycle_id,
                        observed_at=observation_time,
                        profit_pct=current_pct,
                        peak_profit_pct=_d(final_state.get("peak_profit_pct"), str(peak)),
                        basis_eur=basis,
                        quantity=quantity,
                        position_pnl_eur=portfolio.position_pnl_eur.get(symbol, D("0")),
                        partial_taken=bool(int(final_state.get("partial_taken") or 0)),
                        pending_order_id=str(final_state.get("pending_order_id") or ""),
                        policy_snapshot=self._exit_policy_snapshot(),
                        quality=quality,
                    )
                except Exception as exc:
                    # Path telemetry may never hold up a stop-loss or real exit.
                    self.audit.emit(
                        "CORE_EXIT_PATH_RECORD_FAILED",
                        "WARNING",
                        cycle_id=cycle_id,
                        symbol=symbol,
                        episode_id=str(episode.get("episode_id") or ""),
                        error_type=type(exc).__name__,
                        error=str(exc)[:240],
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
        stop_loss_pct = D(str(getattr(self.config, "strategy_stop_loss_pct", 2.0)))

        action = ""
        lock_threshold = max(D("0"), peak_pct * (D("1") - giveback_pct / D("100")))
        lock_threshold = max(lock_threshold, profit_floor)
        if current_pct <= -stop_loss_pct:
            action = "STOP_LOSS_EXIT"
        elif peak_pct >= lock_activation and current_pct <= lock_threshold:
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

        target = (
            D("0")
            if action in {"TRAILING_PROFIT_EXIT", "STOP_LOSS_EXIT"}
            else current * (D("1") - partial_fraction)
        )
        if action == "PARTIAL_TAKE_PROFIT" and decision is not None:
            desired = _d(decision.target_position_eur)
            # Keep an existing full exit or stronger reduction; a profit-taking
            # instruction must never weaken a signal-driven reduction. Tag a
            # stronger nonzero reduction as profit-taking too, so its accepted
            # order is reconciled and the anti-reentry flag is persisted.
            if desired == 0:
                return decision
            if desired * current > 0 and abs(desired) < abs(target):
                rationale = dict(decision.rationale)
                rationale.update({
                    "position_management_action": action,
                    "position_profit_pct": str(current_pct),
                    "position_profit_peak_pct": str(peak_pct),
                    "position_profit_lock_threshold_pct": str(lock_threshold),
                    "position_profit_partial_fraction_pct": str(partial_fraction * D("100")),
                })
                return replace(decision, rationale=rationale)

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
            "position_loss_stop_pct": str(stop_loss_pct),
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
            "POSITION_STOP_LOSS_TRIGGERED" if action == "STOP_LOSS_EXIT"
            else "POSITION_PROFIT_PROTECTION_TRIGGERED",
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
