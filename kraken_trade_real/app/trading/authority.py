from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any, Iterable

from app.domain.models import Instrument, OrderIntent, is_valid_kraken_client_order_id
from app.domain.states import OrderState
from app.kraken.client import KrakenAmbiguous, KrakenError


D = Decimal


class TradingAuthority:
    """The only real-order authority. No other subsystem receives a write-capable gateway."""

    OPEN_STATES = (
        "SUBMITTING",
        "ACKNOWLEDGED",
        "LIVE",
        "PARTIALLY_FILLED",
        "UNKNOWN_RECONCILING",
    )
    RECONCILE_STATES = ("SUBMITTING", "UNKNOWN_RECONCILING")

    def __init__(
        self,
        config: Any,
        gateway: Any,
        db: Any,
        audit: Any,
        policy: Any,
        reconciler: Any,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.db = db
        self.audit = audit
        self.policy = policy
        self.reconciler = reconciler

    def reconcile_pending(
        self,
        instruments: Iterable[Instrument],
        *,
        limit: int | None = None,
    ) -> dict[str, int]:
        """Reconcile stale/ambiguous submissions before allowing new orders.

        UNKNOWN_RECONCILING is never cleared by age alone. A successful
        exchange-side lookup must either return a terminal/open order state or
        positively show that no order exists for the client order id.
        """

        max_items = max(
            1,
            int(
                limit
                if limit is not None
                else getattr(self.config, "execution_reconciliation_limit", 3)
            ),
        )
        stale_seconds = max(
            5.0,
            float(getattr(self.config, "execution_reconciliation_stale_seconds", 30)),
        )
        instrument_map = {instrument.symbol: instrument for instrument in instruments}
        rows = self.db.query(
            "SELECT client_order_id,symbol,state,created_at,submitted_at "
            "FROM orders WHERE state IN ('SUBMITTING','UNKNOWN_RECONCILING') "
            "ORDER BY COALESCE(submitted_at,created_at),created_at LIMIT ?",
            (max_items,),
        )

        stats = {"checked": 0, "resolved": 0, "still_unknown": 0, "skipped": 0}
        for row in rows:
            client_order_id = str(row.get("client_order_id", ""))
            symbol = str(row.get("symbol", ""))
            state = str(row.get("state", ""))
            instrument = instrument_map.get(symbol)
            reference = row.get("submitted_at") or row.get("created_at") or time.time()
            age_seconds = time_since(reference)

            if state == "SUBMITTING" and age_seconds < stale_seconds:
                stats["skipped"] += 1
                continue
            if not client_order_id or instrument is None:
                stats["still_unknown"] += 1
                continue

            if not is_valid_kraken_client_order_id(client_order_id):
                self.db.update_order_state(
                    client_order_id,
                    OrderState.REJECTED.value,
                    last_error="INVALID_CLIENT_ORDER_ID_LEGACY",
                )
                stats["resolved"] += 1
                self.audit.emit(
                    "ORDER_RECONCILED",
                    "WARNING",
                    client_order_id=client_order_id,
                    symbol=symbol,
                    previous_state=state,
                    new_state=OrderState.REJECTED.value,
                    outcome="INVALID_CLIENT_ORDER_ID_LEGACY",
                    age_seconds=round(age_seconds, 2),
                )
                continue

            stats["checked"] += 1
            if not is_valid_kraken_client_order_id(client_order_id):
                self.db.update_order_state(
                    client_order_id,
                    OrderState.REJECTED.value,
                    last_error="INVALID_CLIENT_ORDER_ID_LEGACY",
                )
                self.audit.emit(
                    "PREFLIGHT_ORDER_CLEARED",
                    "WARNING",
                    symbol=instrument.symbol,
                    client_order_id=client_order_id,
                    previous_state=state,
                    outcome="INVALID_CLIENT_ORDER_ID_LEGACY",
                    age_seconds=round(age_seconds, 2),
                )
                continue

            try:
                found = self.gateway.lookup_order(
                    client_order_id=client_order_id,
                    instrument=instrument,
                )
                if found:
                    resolved_state, order_id = self.reconciler.reconcile(found)
                    if resolved_state != OrderState.UNKNOWN_RECONCILING:
                        self.db.update_order_state(
                            client_order_id,
                            resolved_state.value,
                            kraken_order_id=order_id,
                        )
                        stats["resolved"] += 1
                        self.audit.emit(
                            "ORDER_RECONCILED",
                            "INFO",
                            client_order_id=client_order_id,
                            symbol=symbol,
                            previous_state=state,
                            new_state=resolved_state.value,
                            kraken_order_id=order_id or "",
                            age_seconds=round(age_seconds, 2),
                        )
                    else:
                        stats["still_unknown"] += 1
                    continue

                # A successful exact client-order lookup returning no record is
                # the exchange-side confirmation that no order is currently
                # associated with this cl_ord_id. Do not clear on timeout alone.
                self.db.update_order_state(
                    client_order_id,
                    OrderState.REJECTED.value,
                    last_error="EXCHANGE_CONFIRMED_NO_ORDER",
                )
                stats["resolved"] += 1
                self.audit.emit(
                    "ORDER_RECONCILED",
                    "WARNING",
                    client_order_id=client_order_id,
                    symbol=symbol,
                    previous_state=state,
                    new_state=OrderState.REJECTED.value,
                    outcome="EXCHANGE_CONFIRMED_NO_ORDER",
                    age_seconds=round(age_seconds, 2),
                )
            except Exception as exc:
                stats["still_unknown"] += 1
                self.db.event(
                    "ORDER_RECONCILIATION_FAILED",
                    "ERROR",
                    {
                        "client_order_id": client_order_id,
                        "symbol": symbol,
                        "state": state,
                        "error": type(exc).__name__,
                    },
                )
                self.audit.emit(
                    "ORDER_RECONCILIATION_RETRY_FAILED",
                    "WARNING",
                    client_order_id=client_order_id,
                    symbol=symbol,
                    state=state,
                    error=type(exc).__name__,
                )
        return stats

    def submit(self, intent: OrderIntent, market: Any) -> dict[str, Any]:
        self.db.save_order_intent(intent)
        if not is_valid_kraken_client_order_id(intent.client_order_id):
            self.db.update_order_state(
                intent.client_order_id,
                OrderState.REJECTED.value,
                last_error="INVALID_CLIENT_ORDER_ID",
            )
            self.audit.emit(
                "ORDER_REJECTED_EXCHANGE",
                "WARNING",
                intent_id=intent.intent_id,
                error_type="InvalidClientOrderId",
                error="INVALID_CLIENT_ORDER_ID",
            )
            return {
                "state": OrderState.REJECTED.value,
                "reason": "INVALID_CLIENT_ORDER_ID",
                "reconciled": True,
            }
        checks = self._preflight(intent, market)
        if not checks["allowed"]:
            self.db.update_order_state(
                intent.client_order_id,
                OrderState.REJECTED.value,
                last_error=checks["reason"],
            )
            self.audit.emit(
                "ORDER_BLOCKED",
                "WARNING",
                intent_id=intent.intent_id,
                reason=checks["reason"],
                gate="PREFLIGHT",
                detail=checks.get("detail", {}),
            )
            return {"state": OrderState.REJECTED.value, "reason": checks["reason"]}
        if not (self.config.live_enabled and not self.config.kill_switch):
            self.db.update_order_state(
                intent.client_order_id,
                OrderState.REJECTED.value,
                last_error="LIVE_TRADING_DISABLED",
            )
            self.audit.emit(
                "ORDER_BLOCKED",
                "INFO",
                intent_id=intent.intent_id,
                reason="LIVE_TRADING_DISABLED",
            )
            return {"state": OrderState.REJECTED.value, "reason": "LIVE_TRADING_DISABLED"}

        self.db.update_order_state(
            intent.client_order_id,
            OrderState.SUBMITTING.value,
            submitted_at=time.time(),
        )
        try:
            if intent.instrument.product_type.value == "DERIVATIVE":
                response = self.gateway.submit_futures_order(
                    instrument_id=intent.instrument.instrument_id,
                    side=intent.side,
                    order_type=intent.order_type,
                    quantity=intent.quantity,
                    price=intent.limit_price,
                    client_order_id=intent.client_order_id,
                    reduce_only=intent.reduce_only,
                    post_only=intent.post_only,
                )
                status = response.get("sendStatus", {}) if isinstance(response, dict) else {}
                order_id = status.get("order_id") or status.get("orderId")
            else:
                response = self.gateway.submit_spot_order(
                    instrument_id=intent.instrument.instrument_id,
                    side=intent.side,
                    order_type=intent.order_type,
                    quantity=intent.quantity,
                    price=intent.limit_price,
                    client_order_id=intent.client_order_id,
                    leverage=intent.leverage,
                    margin=intent.margin,
                    reduce_only=intent.reduce_only,
                    post_only=intent.post_only,
                )
                ids = response.get("txid", []) if isinstance(response, dict) else []
                order_id = ids[0] if ids else response.get("order_id") if isinstance(response, dict) else None
            self.db.update_order_state(
                intent.client_order_id,
                OrderState.ACKNOWLEDGED.value,
                kraken_order_id=order_id,
            )
            self.audit.emit("ORDER_ACKNOWLEDGED", "INFO", intent_id=intent.intent_id)
            return {
                "state": OrderState.ACKNOWLEDGED.value,
                "kraken_order_id": order_id,
                "response": response,
            }
        except KrakenAmbiguous as exc:
            # Transport/time-out failures are genuinely ambiguous and remain
            # blocked until the exact client order id can be reconciled.
            error_text = str(exc)[:700] or type(exc).__name__
            self.db.update_order_state(
                intent.client_order_id,
                OrderState.UNKNOWN_RECONCILING.value,
                last_error=error_text,
            )
            self.audit.emit(
                "ORDER_RECONCILING",
                "ERROR",
                intent_id=intent.intent_id,
                error_type=type(exc).__name__,
                error=error_text,
            )
            try:
                found = self.gateway.lookup_order(
                    client_order_id=intent.client_order_id,
                    instrument=intent.instrument,
                )
                if found:
                    state, order_id = self.reconciler.reconcile(found)
                    self.db.update_order_state(
                        intent.client_order_id,
                        state.value,
                        kraken_order_id=order_id,
                    )
                    return {
                        "state": state.value,
                        "kraken_order_id": order_id,
                        "reconciled": state != OrderState.UNKNOWN_RECONCILING,
                    }
                self.db.update_order_state(
                    intent.client_order_id,
                    OrderState.REJECTED.value,
                    last_error="EXCHANGE_CONFIRMED_NO_ORDER",
                )
                self.audit.emit(
                    "ORDER_RECONCILED",
                    "WARNING",
                    intent_id=intent.intent_id,
                    new_state=OrderState.REJECTED.value,
                    outcome="EXCHANGE_CONFIRMED_NO_ORDER",
                )
                return {
                    "state": OrderState.REJECTED.value,
                    "reason": "EXCHANGE_CONFIRMED_NO_ORDER",
                    "reconciled": True,
                }
            except Exception as rex:
                self.db.event(
                    "ORDER_RECONCILIATION_FAILED",
                    "ERROR",
                    {
                        "client_order_id": intent.client_order_id,
                        "error": type(rex).__name__,
                    },
                )
                return {
                    "state": OrderState.UNKNOWN_RECONCILING.value,
                    "reconciled": False,
                }
        except KrakenError as exc:
            # A Kraken API/application error is a deterministic submission
            # failure. It must not become a permanent open-order gate.
            error_text = str(exc)[:700] or type(exc).__name__
            self.db.update_order_state(
                intent.client_order_id,
                OrderState.REJECTED.value,
                last_error=error_text,
            )
            self.audit.emit(
                "ORDER_REJECTED_EXCHANGE",
                "WARNING",
                intent_id=intent.intent_id,
                error_type=type(exc).__name__,
                error=error_text,
            )
            return {
                "state": OrderState.REJECTED.value,
                "reason": "KRAKEN_ORDER_REJECTED",
                "exchange_error": error_text,
                "reconciled": True,
            }

    def _preflight(self, intent: OrderIntent, market: Any) -> dict[str, Any]:
        if not self.config.kraken_enabled:
            return {"allowed": False, "reason": "KRAKEN_DISABLED"}
        if not intent.instrument.tradeable:
            return {"allowed": False, "reason": "INSTRUMENT_NOT_TRADEABLE"}
        if (
            intent.direction.value == "SHORT"
            and not intent.instrument.short_available
            and not intent.reduce_only
        ):
            return {"allowed": False, "reason": "SHORT_NOT_AVAILABLE"}
        if intent.quantity < intent.instrument.min_order_qty:
            return {"allowed": False, "reason": "MIN_ORDER_QTY"}
        if intent.limit_price and intent.quantity * intent.limit_price < intent.instrument.min_cost:
            return {"allowed": False, "reason": "MIN_ORDER_COST"}
        if intent.leverage > intent.instrument.max_leverage:
            return {"allowed": False, "reason": "LEVERAGE_INSTRUMENT_LIMIT"}

        open_orders = self.db.query(
            "SELECT client_order_id,state,submitted_at,created_at FROM orders WHERE symbol=? "
            "AND state IN ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED','UNKNOWN_RECONCILING')",
            (intent.instrument.symbol,),
        )
        if open_orders:
            open_orders = self._reconcile_symbol_open_orders(
                intent.instrument,
                open_orders,
            )
        if open_orders:
            return {
                "allowed": False,
                "reason": "DUPLICATE_OPEN_ORDER",
                "detail": {
                    "open_states": [str(row.get("state", "")) for row in open_orders],
                    "open_orders": [
                        {
                            "client_order_id": str(row.get("client_order_id", "")),
                            "state": str(row.get("state", "")),
                        }
                        for row in open_orders
                    ],
                },
            }

        count = self.db.one(
            "SELECT COUNT(*) AS n FROM orders WHERE submitted_at IS NOT NULL "
            "AND submitted_at>=strftime('%s','now','start of day')"
        )
        if count and int(count["n"]) >= self.config.execution_max_orders_per_day:
            return {
                "allowed": False,
                "reason": "DAILY_ORDER_LIMIT",
                "detail": {
                    "submitted_today": int(count["n"]),
                    "limit": self.config.execution_max_orders_per_day,
                },
            }

        recent = self.db.query(
            "SELECT submitted_at,state FROM orders "
            "WHERE symbol=? AND direction=? AND submitted_at IS NOT NULL "
            "ORDER BY submitted_at DESC LIMIT 1",
            (intent.instrument.symbol, intent.direction.value),
        )
        if recent:
            age = time_since(recent[0]["submitted_at"])
            if age < 60:
                return {
                    "allowed": False,
                    "reason": "ORDER_COOLDOWN",
                    "detail": {
                        "age_seconds": round(age, 3),
                        "state": str(recent[0].get("state", "")),
                    },
                }

        chosen = self.policy.choose(
            market.spread_bps,
            intent.expected_edge_bps,
            self._volatility(market),
        )
        estimated = self._volatility(market) * D(2)
        ok, reason = self.policy.validate(
            chosen,
            intent.expected_edge_bps,
            estimated,
        )
        return {
            "allowed": ok,
            "reason": reason if not ok else "PRECHECK_OK",
            "method": chosen["method"],
        }

    def _reconcile_symbol_open_orders(
        self,
        instrument: Instrument,
        open_orders: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        remaining: list[dict[str, Any]] = []
        for row in open_orders:
            state = str(row.get("state", ""))
            if state not in {"SUBMITTING", "UNKNOWN_RECONCILING"}:
                remaining.append(row)
                continue
            client_order_id = str(row.get("client_order_id", ""))
            if not client_order_id:
                remaining.append(row)
                continue
            age_seconds = time_since(
                row.get("submitted_at")
                or row.get("created_at")
                or time.time()
            )
            try:
                found = self.gateway.lookup_order(
                    client_order_id=client_order_id,
                    instrument=instrument,
                )
                if found:
                    resolved_state, order_id = self.reconciler.reconcile(found)
                    if resolved_state != OrderState.UNKNOWN_RECONCILING:
                        self.db.update_order_state(
                            client_order_id,
                            resolved_state.value,
                            kraken_order_id=order_id,
                        )
                        self.audit.emit(
                            "PREFLIGHT_ORDER_RECONCILED",
                            "INFO",
                            symbol=instrument.symbol,
                            client_order_id=client_order_id,
                            previous_state=state,
                            new_state=resolved_state.value,
                            kraken_order_id=order_id or "",
                            age_seconds=round(age_seconds, 2),
                        )
                        if resolved_state in {
                            OrderState.SUBMITTING,
                            OrderState.ACKNOWLEDGED,
                            OrderState.LIVE,
                            OrderState.PARTIALLY_FILLED,
                            OrderState.UNKNOWN_RECONCILING,
                        }:
                            remaining.append(
                                {
                                    "client_order_id": client_order_id,
                                    "state": resolved_state.value,
                                }
                            )
                        continue
                self.db.update_order_state(
                    client_order_id,
                    OrderState.REJECTED.value,
                    last_error="EXCHANGE_CONFIRMED_NO_ORDER",
                )
                self.audit.emit(
                    "PREFLIGHT_ORDER_CLEARED",
                    "WARNING",
                    symbol=instrument.symbol,
                    client_order_id=client_order_id,
                    previous_state=state,
                    outcome="EXCHANGE_CONFIRMED_NO_ORDER",
                    age_seconds=round(age_seconds, 2),
                )
            except Exception as exc:
                self.audit.emit(
                    "PREFLIGHT_ORDER_RECONCILIATION_FAILED",
                    "WARNING",
                    symbol=instrument.symbol,
                    client_order_id=client_order_id,
                    previous_state=state,
                    error_type=type(exc).__name__,
                    error=str(exc)[:700],
                    age_seconds=round(age_seconds, 2),
                )
                remaining.append(row)
        return remaining

    @staticmethod
    def _volatility(market: Any) -> D:
        c = getattr(market, "closes", ())
        if len(c) < 2:
            return D(999)
        x = [abs(c[i] / c[i - 1] - 1) * 100 for i in range(1, len(c)) if c[i - 1]]
        return sum(x, D(0)) / D(max(1, len(x)))


def time_since(ts: Any) -> float:
    try:
        return max(
            0.0,
            datetime.now(timezone.utc).timestamp() - float(ts),
        )
    except (TypeError, ValueError):
        return 999999.0
