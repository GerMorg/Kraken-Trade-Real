from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import time
import threading
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
        self._submission_lock = threading.RLock()
        self._spot_fill_sync_lock = threading.RLock()
        self._last_spot_fill_sync_monotonic = 0.0

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
            "SELECT client_order_id,symbol,state,created_at,submitted_at,kraken_order_id "
            "FROM orders WHERE state IN ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED','UNKNOWN_RECONCILING') "
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
            try:
                found = self.gateway.lookup_order(
                    client_order_id=client_order_id,
                    instrument=instrument,
                    kraken_order_id=str(row.get("kraken_order_id") or "") or None,
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

    def sync_spot_fills(
        self,
        instruments: Iterable[Instrument],
        *,
        max_pages: int = 2,
        page_size: int = 50,
        minimum_interval_seconds: float = 60.0,
        lookback_days: int = 180,
    ) -> dict[str, Any]:
        """Backfill known Spot/Margin order fills without blocking trading on failure.

        Each sync session pins a start/end window and persists Kraken's cursor
        token between cycles. The DB's (order_id, trade_id) key makes retries
        idempotent even if a page is replayed after a process restart.
        """
        if not callable(getattr(self.gateway, "spot_trades_history", None)):
            return {"status": "UNSUPPORTED", "pages": 0, "inserted": 0, "matched": 0}
        with self._spot_fill_sync_lock:
            now = time.time()
            session = {
                key: self.db.one("SELECT value FROM metadata WHERE key=?", (key,))
                for key in (
                    "core_spot_fill_sync_start",
                    "core_spot_fill_sync_end",
                    "core_spot_fill_sync_cursor",
                )
            }
            active_session = all(session[key] is not None for key in session)
            monotonic_now = time.monotonic()
            if (
                not active_session
                and self._last_spot_fill_sync_monotonic > 0.0
                and monotonic_now - self._last_spot_fill_sync_monotonic
                < max(1.0, float(minimum_interval_seconds))
            ):
                return {"status": "THROTTLED", "pages": 0, "inserted": 0, "matched": 0}
            self._last_spot_fill_sync_monotonic = monotonic_now

            instrument_map = {
                instrument.symbol: instrument
                for instrument in instruments
                if str(getattr(instrument, "venue", "")).lower() == "spot"
            }
            if not instrument_map:
                return {"status": "NO_SPOT_INSTRUMENTS", "pages": 0, "inserted": 0, "matched": 0}
            order_rows = self.db.query(
                """SELECT client_order_id,decision_id,symbol,created_at,submitted_at,
                          kraken_order_id,state,reduce_only,direction
                   FROM orders
                   WHERE kraken_order_id IS NOT NULL AND kraken_order_id!=''
                     AND state!='REJECTED'
                   ORDER BY COALESCE(submitted_at,created_at)"""
            )
            tracked_orders: dict[str, dict[str, Any]] = {}
            for row in order_rows:
                symbol = str(row.get("symbol") or "")
                if symbol not in instrument_map:
                    continue
                order_id = str(row.get("kraken_order_id") or "")
                if order_id:
                    tracked_orders[order_id] = row
            if not tracked_orders:
                return {"status": "NO_TRACKED_SPOT_ORDERS", "pages": 0, "inserted": 0, "matched": 0}

            def metadata_value(key: str, default: str = "") -> str:
                row = self.db.one("SELECT value FROM metadata WHERE key=?", (key,))
                return str(row.get("value") or default) if row else default

            def set_metadata(key: str, value: Any) -> None:
                self.db.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

            if active_session:
                start_at = int(float(session["core_spot_fill_sync_start"]["value"]))
                end_at = int(float(session["core_spot_fill_sync_end"]["value"]))
                cursor = str(session["core_spot_fill_sync_cursor"].get("value") or "")
            else:
                watermark = metadata_value("core_spot_fill_sync_watermark")
                cutoff = int(now - max(1, int(lookback_days)) * 86400)
                if watermark:
                    try:
                        start_at = max(cutoff, int(float(watermark)) - 60)
                    except (TypeError, ValueError):
                        start_at = cutoff
                else:
                    first_order = min(
                        float(row.get("submitted_at") or row.get("created_at") or now)
                        for row in tracked_orders.values()
                    )
                    start_at = max(cutoff, int(first_order) - 2)
                end_at = int(now)
                cursor = ""
                if end_at <= start_at:
                    return {"status": "UP_TO_DATE", "pages": 0, "inserted": 0, "matched": 0}
                set_metadata("core_spot_fill_sync_start", start_at)
                set_metadata("core_spot_fill_sync_end", end_at)
                set_metadata("core_spot_fill_sync_cursor", "")

            stats: dict[str, Any] = {
                "status": "IN_PROGRESS",
                "pages": 0,
                "inserted": 0,
                "matched": 0,
                "unmatched": 0,
                "cursor_page_start": bool(cursor),
                "window_start": start_at,
                "window_end": end_at,
            }
            max_pages = max(1, min(5, int(max_pages)))
            page_size = max(1, min(50, int(page_size)))
            for _ in range(max_pages):
                request_params: dict[str, Any] = {
                    "start": start_at,
                    "end": end_at,
                    "with_cursor": True,
                    "limit": page_size,
                    "trades": True,
                }
                if cursor:
                    request_params["cursor"] = cursor
                response = self.gateway.spot_trades_history(request_params)
                if not isinstance(response, dict):
                    raise KrakenError("SPOT_TRADES_HISTORY_INVALID_RESPONSE")
                trades = response.get("trades")
                if not isinstance(trades, dict):
                    raise KrakenError("SPOT_TRADES_HISTORY_MISSING_TRADES")
                cursor_response = response.get("cursor")
                next_cursor = (
                    str(cursor_response.get("next") or "")
                    if isinstance(cursor_response, dict)
                    else ""
                )
                stats["pages"] += 1
                for trade_id, payload in trades.items():
                    if not isinstance(payload, dict):
                        continue
                    exchange_order_id = str(payload.get("ordertxid") or "")
                    order = tracked_orders.get(exchange_order_id)
                    if order is None:
                        stats["unmatched"] += 1
                        continue
                    instrument = instrument_map[str(order.get("symbol") or "")]
                    try:
                        trade_time = float(str(payload.get("time") or "0"))
                        price = D(str(payload.get("price")))
                        quantity = D(str(payload.get("vol")))
                        fee = D(str(payload.get("fee") or "0"))
                    except Exception:
                        stats["unmatched"] += 1
                        continue
                    side = str(payload.get("type") or "").strip().lower()
                    if side not in {"buy", "sell"} or trade_time <= 0 or price <= 0 or quantity <= 0:
                        stats["unmatched"] += 1
                        continue
                    inserted = self.db.save_attributed_fill(
                        order_id=exchange_order_id,
                        trade_id=str(trade_id),
                        created_at=trade_time,
                        symbol=str(order.get("symbol") or ""),
                        side=side,
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        # Kraken TradesHistory exposes a fee amount but no fee
                        # currency field. Do not invent a currency before PnL
                        # conversion logic is verified.
                        fee_currency="UNVERIFIED",
                        client_order_id=str(order.get("client_order_id") or ""),
                        decision_id=str(order.get("decision_id") or ""),
                        venue="spot",
                        quote_asset=str(instrument.quote or ""),
                        raw_payload=payload,
                    )
                    stats["matched"] += 1
                    if inserted:
                        stats["inserted"] += 1

                # With cursor pagination, an absent cursor.next marks the final
                # page. Do not infer completion from count/offset values.
                done = not trades or not next_cursor
                if done:
                    stats["status"] = "COMPLETE"
                    set_metadata("core_spot_fill_sync_watermark", end_at)
                    self.db.execute(
                        """DELETE FROM metadata WHERE key IN (
                           'core_spot_fill_sync_start','core_spot_fill_sync_end',
                           'core_spot_fill_sync_cursor'
                        )"""
                    )
                    break
                cursor = next_cursor
                set_metadata("core_spot_fill_sync_cursor", cursor)
                stats["status"] = "IN_PROGRESS"
            return stats

    def submit(self, intent: OrderIntent, market: Any) -> dict[str, Any]:
        with self._submission_lock:
            return self._submit_locked(intent, market)

    def _submit_locked(self, intent: OrderIntent, market: Any) -> dict[str, Any]:
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
                status = status if isinstance(status, dict) else {}
                exchange_status = str(status.get("status") or "").strip().lower()
                order_id = status.get("order_id") or status.get("orderId")
                if exchange_status == "rejected":
                    error_text = str(
                        status.get("rejectionReason")
                        or status.get("reason")
                        or "FUTURES_SEND_STATUS_REJECTED"
                    )[:700]
                    self.db.update_order_state(
                        intent.client_order_id,
                        OrderState.REJECTED.value,
                        kraken_order_id=order_id,
                        last_error=error_text,
                    )
                    self.audit.emit(
                        "ORDER_REJECTED_EXCHANGE",
                        "WARNING",
                        intent_id=intent.intent_id,
                        venue="futures",
                        exchange_status=exchange_status,
                        error=error_text,
                    )
                    return {
                        "state": OrderState.REJECTED.value,
                        "reason": "KRAKEN_ORDER_REJECTED",
                        "exchange_status": exchange_status,
                        "exchange_error": error_text,
                        "reconciled": True,
                    }
                if exchange_status in {"cancelled", "canceled"}:
                    self.db.update_order_state(
                        intent.client_order_id,
                        OrderState.CANCELED.value,
                        kraken_order_id=order_id,
                        last_error="FUTURES_ORDER_CANCELLED_AT_SUBMISSION",
                    )
                    return {
                        "state": OrderState.CANCELED.value,
                        "reason": "FUTURES_ORDER_CANCELLED_AT_SUBMISSION",
                        "exchange_status": exchange_status,
                        "reconciled": True,
                    }
                if exchange_status not in {
                    "placed", "filled", "partiallyfilled", "partially_filled",
                    "pending", "received", "acknowledged",
                } or not order_id:
                    self.db.update_order_state(
                        intent.client_order_id,
                        OrderState.UNKNOWN_RECONCILING.value,
                        kraken_order_id=order_id,
                        last_error="FUTURES_SEND_STATUS_MISSING_OR_UNRECOGNIZED",
                    )
                    self.audit.emit(
                        "ORDER_RECONCILING",
                        "WARNING",
                        intent_id=intent.intent_id,
                        venue="futures",
                        exchange_status=exchange_status or "MISSING",
                        has_order_id=bool(order_id),
                    )
                    return {
                        "state": OrderState.UNKNOWN_RECONCILING.value,
                        "reason": "FUTURES_SEND_STATUS_MISSING_OR_UNRECOGNIZED",
                        "kraken_order_id": order_id,
                        "reconciled": False,
                    }
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
                    asset_class=(
                        "tokenized_asset"
                        if intent.instrument.metadata.get("asset_class") == "tokenized_asset"
                        else None
                    ),
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
                    kraken_order_id=str(order_id or "") or None,
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

    def submit_funding_order(self, intent: OrderIntent, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Submit a preparatory funding order and confirm its exchange-side fill."""
        if not is_valid_kraken_client_order_id(intent.client_order_id):
            return {"state": OrderState.REJECTED.value, "reason": "INVALID_CLIENT_ORDER_ID"}
        if not (self.config.live_enabled and not self.config.kill_switch):
            return {"state": OrderState.REJECTED.value, "reason": "LIVE_TRADING_DISABLED"}
        if not self.config.kraken_enabled or not intent.instrument.tradeable:
            return {"state": OrderState.REJECTED.value, "reason": "FUNDING_INSTRUMENT_UNAVAILABLE"}
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM orders WHERE submitted_at IS NOT NULL AND state != 'REJECTED' "
            "AND submitted_at>=strftime('%s','now','start of day')"
        )
        if (
            count
            and int(count["n"]) >= self.config.execution_max_orders_per_day
            and not intent.reduce_only
        ):
            return {"state": OrderState.REJECTED.value, "reason": "DAILY_ORDER_LIMIT"}
        open_orders = self.db.query(
            "SELECT client_order_id,state FROM orders WHERE symbol=? "
            "AND state IN ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED','UNKNOWN_RECONCILING')",
            (intent.instrument.symbol,),
        )
        if open_orders:
            return {"state": OrderState.REJECTED.value, "reason": "DUPLICATE_OPEN_ORDER"}
        self.db.save_order_intent(intent)
        self.db.update_order_state(intent.client_order_id, OrderState.SUBMITTING.value, submitted_at=time.time())
        try:
            response = self.gateway.submit_spot_order(
                instrument_id=intent.instrument.instrument_id,
                side=intent.side,
                order_type=intent.order_type,
                quantity=intent.quantity,
                price=intent.limit_price,
                client_order_id=intent.client_order_id,
                leverage=intent.leverage,
                margin=False,
                reduce_only=False,
                post_only=False,
                asset_class=(
                    "tokenized_asset"
                    if intent.instrument.metadata.get("asset_class") == "tokenized_asset"
                    else None
                ),
            )
            ids = response.get("txid", []) if isinstance(response, dict) else []
            order_id = ids[0] if ids else response.get("order_id") if isinstance(response, dict) else None
            if not order_id:
                self.db.update_order_state(intent.client_order_id, OrderState.REJECTED.value, last_error="FUNDING_NO_ORDER_ID")
                return {"state": OrderState.REJECTED.value, "reason": "FUNDING_NO_ORDER_ID"}
            self.db.update_order_state(intent.client_order_id, OrderState.ACKNOWLEDGED.value, kraken_order_id=order_id)
            deadline = time.monotonic() + max(1.0, float(timeout_seconds))
            while time.monotonic() < deadline:
                found = self.gateway.lookup_order(
                    client_order_id=intent.client_order_id,
                    instrument=intent.instrument,
                    kraken_order_id=order_id,
                )
                if found:
                    state, resolved_id = self.reconciler.reconcile(found)
                    if state != OrderState.UNKNOWN_RECONCILING:
                        self.db.update_order_state(
                            intent.client_order_id, state.value, kraken_order_id=resolved_id or order_id
                        )
                        if state in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
                            self.audit.emit("FUNDING_ORDER_FILLED", "INFO",
                                             intent_id=intent.intent_id, symbol=intent.instrument.symbol,
                                             state=state.value, kraken_order_id=resolved_id or order_id)
                            return {"state": state.value, "kraken_order_id": resolved_id or order_id}
                        if state in {OrderState.REJECTED, OrderState.CANCELED, OrderState.EXPIRED}:
                            return {"state": state.value, "reason": "FUNDING_ORDER_NOT_FILLED"}
                time.sleep(1.0)
            self.db.update_order_state(intent.client_order_id, OrderState.UNKNOWN_RECONCILING.value,
                                       last_error="FUNDING_FILL_TIMEOUT")
            self.audit.emit("FUNDING_ORDER_RECONCILING", "WARNING",
                             intent_id=intent.intent_id, symbol=intent.instrument.symbol)
            return {"state": OrderState.UNKNOWN_RECONCILING.value, "reason": "FUNDING_FILL_TIMEOUT"}
        except KrakenAmbiguous as exc:
            self.db.update_order_state(intent.client_order_id, OrderState.UNKNOWN_RECONCILING.value,
                                       last_error=str(exc)[:700])
            return {"state": OrderState.UNKNOWN_RECONCILING.value, "reason": "FUNDING_AMBIGUOUS"}
        except KrakenError as exc:
            self.db.update_order_state(intent.client_order_id, OrderState.REJECTED.value,
                                       last_error=str(exc)[:700])
            return {"state": OrderState.REJECTED.value, "reason": "FUNDING_KRAKEN_REJECTED",
                    "exchange_error": str(exc)[:700]}
        except Exception as exc:
            self.db.update_order_state(intent.client_order_id, OrderState.UNKNOWN_RECONCILING.value,
                                       last_error=f"{type(exc).__name__}:{str(exc)[:500]}")
            return {"state": OrderState.UNKNOWN_RECONCILING.value, "reason": "FUNDING_EXCEPTION"}

    @staticmethod
    def _supported_leverage_levels(instrument: Instrument, side: str) -> tuple[D, ...]:
        raw = instrument.metadata.get(f"leverage_{side}")
        if isinstance(raw, (list, tuple)):
            parsed = tuple(D(str(value)) for value in raw if str(value))
            if parsed:
                return parsed
        return instrument.leverage_levels

    def _preflight(self, intent: OrderIntent, market: Any) -> dict[str, Any]:
        if not self.config.kraken_enabled:
            return {"allowed": False, "reason": "KRAKEN_DISABLED"}
        if not intent.instrument.tradeable:
            return {"allowed": False, "reason": "INSTRUMENT_NOT_TRADEABLE"}
        if (
            intent.direction.value == "SHORT"
            and intent.instrument.product_type.value == "SPOT_MARGIN"
            and not intent.reduce_only
            and intent.leverage <= D("1")
        ):
            return {
                "allowed": False,
                "reason": "SPOT_MARGIN_SHORT_REQUIRES_LEVERAGE",
            }
        if (
            intent.direction.value == "SHORT"
            and not intent.instrument.short_available
            and not intent.reduce_only
        ):
            return {"allowed": False, "reason": "SHORT_NOT_AVAILABLE"}
        is_settle_position = (
            str(intent.order_type).strip().lower().replace("_", "-") == "settle-position"
        )
        if is_settle_position and not (
            intent.reduce_only
            and intent.instrument.venue == "spot"
            and intent.instrument.product_type.value == "SPOT_MARGIN"
            and intent.quantity == D("0")
            and intent.limit_price is None
            and intent.leverage > D("1")
            and not intent.post_only
        ):
            return {"allowed": False, "reason": "INVALID_SETTLE_POSITION_INTENT"}
        if not is_settle_position and intent.quantity < intent.instrument.min_order_qty:
            return {"allowed": False, "reason": "MIN_ORDER_QTY"}
        if (
            not is_settle_position
            and intent.instrument.venue != "futures"
            and intent.instrument.min_cost > 0
        ):
            order_reference_price = intent.limit_price
            if order_reference_price is None:
                side_price = (
                    getattr(market, "ask", None)
                    if intent.side.lower() == "buy"
                    else getattr(market, "bid", None)
                )
                order_reference_price = side_price or getattr(market, "price", None)
            if (
                order_reference_price is not None
                and D(str(order_reference_price)) > 0
                and intent.quantity * D(str(order_reference_price)) < intent.instrument.min_cost
            ):
                return {"allowed": False, "reason": "MIN_ORDER_COST"}
        if intent.leverage < D("1"):
            return {"allowed": False, "reason": "LEVERAGE_BELOW_ONE"}
        if (
            intent.instrument.product_type.value != "DERIVATIVE"
            and intent.leverage > D("1")
        ):
            if not intent.instrument.margin_available:
                return {"allowed": False, "reason": "MARGIN_NOT_AVAILABLE"}
            # Spot Margin settlement is exchange-side position closing. Kraken
            # accepts the dedicated settle-position order without requiring the
            # closing side to expose the same leverage level as the entry side.
            if not (
                intent.reduce_only
                and intent.instrument.product_type.value == "SPOT_MARGIN"
            ):
                supported = self._supported_leverage_levels(
                    intent.instrument, intent.side.lower()
                )
                if intent.leverage not in supported:
                    return {
                        "allowed": False,
                        "reason": "LEVERAGE_UNSUPPORTED_BY_INSTRUMENT",
                        "detail": {
                            "requested": str(intent.leverage),
                            "supported": [str(level) for level in supported],
                            "side": intent.side.lower(),
                        },
                    }
        elif intent.leverage > intent.instrument.max_leverage:
            return {"allowed": False, "reason": "LEVERAGE_INSTRUMENT_LIMIT"}

        open_orders = self.db.query(
            "SELECT client_order_id,state,submitted_at,created_at,kraken_order_id,"
            "reduce_only,quantity,limit_price,side FROM orders WHERE symbol=? "
            "AND state IN ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED','UNKNOWN_RECONCILING')",
            (intent.instrument.symbol,),
        )
        stale_reduce_order_resolved = False
        if open_orders:
            open_orders, stale_reduce_order_resolved = self._reconcile_symbol_open_orders(
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

        if stale_reduce_order_resolved:
            return {
                "allowed": False,
                "reason": "STALE_REDUCE_ORDER_RESOLVED_REFRESH_REQUIRED",
                "detail": {
                    "symbol": intent.instrument.symbol,
                    "instruction": "Reconcile portfolio on the next cycle before retrying the reduction",
                },
            }

        count = self.db.one(
            "SELECT COUNT(*) AS n FROM orders WHERE submitted_at IS NOT NULL "
            "AND state != 'REJECTED' "
            "AND submitted_at>=strftime('%s','now','start of day')"
        )
        if (
            count
            and int(count["n"]) >= self.config.execution_max_orders_per_day
            and not intent.reduce_only
        ):
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
            "AND state != 'REJECTED' "
            "ORDER BY submitted_at DESC LIMIT 1",
            (intent.instrument.symbol, intent.direction.value),
        )
        if recent and not intent.reduce_only:
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
            reduce_only=intent.reduce_only,
        )
        estimated = self._volatility(market) * D(2)
        ok, reason = self.policy.validate(
            chosen,
            intent.expected_edge_bps,
            estimated,
            reduce_only=intent.reduce_only,
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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Reconcile open orders and cancel stale reduce-only limits safely.

        A reduce-only limit is allowed a bounded chance to fill. Once older than
        execution_order_timeout_seconds, request exchange-side cancellation and
        require an exchange-confirmed terminal state. Never clear an ambiguous
        order merely because its timeout elapsed.
        """
        remaining: list[dict[str, Any]] = []
        refresh_required = False
        reduce_timeout = max(
            5.0,
            float(getattr(self.config, "execution_order_timeout_seconds", 45)),
        )

        def raw_status(payload: dict[str, Any]) -> str:
            return str(payload.get("status") or payload.get("state") or "").strip().lower()

        def is_terminal_status(payload: dict[str, Any], resolved_state: OrderState) -> bool:
            status = raw_status(payload)
            return (
                status in {"closed", "canceled", "cancelled", "expired", "rejected"}
                or resolved_state in {
                    OrderState.FILLED, OrderState.CANCELED,
                    OrderState.EXPIRED, OrderState.REJECTED,
                }
            )

        def persisted_terminal_state(
            payload: dict[str, Any], resolved_state: OrderState
        ) -> tuple[OrderState, str]:
            status = raw_status(payload)
            executed = D(str(
                payload.get("vol_exec") or payload.get("executed_volume") or "0"
            ))
            requested = D(str(payload.get("vol") or payload.get("volume") or "0"))
            if resolved_state == OrderState.FILLED:
                return OrderState.FILLED, ""
            if status in {"canceled", "cancelled", "expired", "rejected"} or (
                status == "closed"
                and (
                    resolved_state == OrderState.PARTIALLY_FILLED
                    or (requested > 0 and executed < requested)
                )
            ):
                detail = ""
                if executed > 0:
                    detail = (
                        "EXCHANGE_TERMINAL_AFTER_PARTIAL_FILL:"
                        f"executed={executed};requested={requested};exchange_status={status}"
                    )
                return (
                    OrderState.EXPIRED if status == "expired" else
                    OrderState.REJECTED if status == "rejected" else
                    OrderState.CANCELED,
                    detail,
                )
            return resolved_state, ""

        for row in open_orders:
            state = str(row.get("state", ""))
            if state not in self.OPEN_STATES:
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
            is_reduce_only = bool(int(row.get("reduce_only") or 0))
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
            order_id = str(row.get("kraken_order_id") or "") or None
            try:
                found = self.gateway.lookup_order(
                    client_order_id=client_order_id,
                    instrument=instrument,
                    kraken_order_id=order_id,
                )
                if not found:
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
                    continue

                payload = found[0] if isinstance(found[0], dict) else {}
                resolved_state, resolved_id = self.reconciler.reconcile(found)
                exchange_status = raw_status(payload)
                if is_terminal_status(payload, resolved_state):
                    terminal_state, terminal_detail = persisted_terminal_state(
                        payload, resolved_state
                    )
                    self.db.update_order_state(
                        client_order_id,
                        terminal_state.value,
                        kraken_order_id=resolved_id or order_id,
                        **({"last_error": terminal_detail} if terminal_detail else {}),
                    )
                    self.audit.emit(
                        "PREFLIGHT_ORDER_RECONCILED",
                        "INFO",
                        symbol=instrument.symbol,
                        client_order_id=client_order_id,
                        previous_state=state,
                        new_state=terminal_state.value,
                        exchange_status=exchange_status,
                        kraken_order_id=resolved_id or order_id or "",
                        executed_volume=str(
                            payload.get("vol_exec") or payload.get("executed_volume") or "0"
                        ),
                        requested_volume=str(payload.get("vol") or payload.get("volume") or "0"),
                        age_seconds=round(age_seconds, 2),
                    )
                    if is_reduce_only:
                        # Any exchange-terminal reduce order may have changed the
                        # position after this cycle's portfolio snapshot, even if
                        # its age is shorter than the timeout threshold.
                        refresh_required = True
                    continue

                if resolved_state != OrderState.UNKNOWN_RECONCILING:
                    self.db.update_order_state(
                        client_order_id,
                        resolved_state.value,
                        kraken_order_id=resolved_id or order_id,
                    )
                    self.audit.emit(
                        "PREFLIGHT_ORDER_RECONCILED",
                        "INFO",
                        symbol=instrument.symbol,
                        client_order_id=client_order_id,
                        previous_state=state,
                        new_state=resolved_state.value,
                        kraken_order_id=resolved_id or order_id or "",
                        age_seconds=round(age_seconds, 2),
                    )

                # A live reduce-only order is the one order class for which an
                # aged, non-filling limit must not prevent future risk-reduction
                # attempts. Only cancel when Kraken gave us its exact order id.
                if is_reduce_only and age_seconds >= reduce_timeout:
                    if not order_id:
                        self.audit.emit(
                            "ORDER_STALE_REDUCE_CANCEL_FAILED",
                            "WARNING",
                            symbol=instrument.symbol,
                            client_order_id=client_order_id,
                            reason="KRAKEN_ORDER_ID_MISSING",
                            state=resolved_state.value,
                            age_seconds=round(age_seconds, 2),
                        )
                    elif exchange_status not in {"open", "pending", "new", "partially_filled", "partiallyfilled"}:
                        self.audit.emit(
                            "ORDER_STALE_REDUCE_CANCEL_FAILED",
                            "WARNING",
                            symbol=instrument.symbol,
                            client_order_id=client_order_id,
                            reason="NONTERMINAL_STATUS_UNRECOGNIZED",
                            exchange_status=exchange_status,
                            state=resolved_state.value,
                            age_seconds=round(age_seconds, 2),
                        )
                    else:
                        self.audit.emit(
                            "ORDER_STALE_REDUCE_CANCEL_REQUESTED",
                            "WARNING",
                            symbol=instrument.symbol,
                            client_order_id=client_order_id,
                            kraken_order_id=order_id,
                            state=resolved_state.value,
                            age_seconds=round(age_seconds, 2),
                            timeout_seconds=reduce_timeout,
                            quantity=str(row.get("quantity") or ""),
                            limit_price=str(row.get("limit_price") or ""),
                        )
                        cancel_error = ""
                        try:
                            self.gateway.cancel_order(
                                instrument=instrument,
                                kraken_order_id=order_id,
                                client_order_id=client_order_id,
                            )
                        except Exception as exc:
                            cancel_error = f"{type(exc).__name__}:{str(exc)[:400]}"
                        # Verify the exact exchange order after the cancel request.
                        # A timeout or cancel API response alone is not proof.
                        try:
                            after_cancel = self.gateway.lookup_order(
                                client_order_id=client_order_id,
                                instrument=instrument,
                                kraken_order_id=order_id,
                            )
                            if after_cancel and isinstance(after_cancel[0], dict):
                                after_payload = after_cancel[0]
                                after_state, after_id = self.reconciler.reconcile(after_cancel)
                                after_status = raw_status(after_payload)
                                if is_terminal_status(after_payload, after_state):
                                    terminal_state, terminal_detail = persisted_terminal_state(
                                        after_payload, after_state
                                    )
                                    detail = terminal_detail or (
                                        f"STALE_REDUCE_ORDER_TERMINAL:{after_status}"
                                    )
                                    self.db.update_order_state(
                                        client_order_id,
                                        terminal_state.value,
                                        kraken_order_id=after_id or order_id,
                                        last_error=detail,
                                    )
                                    executed = str(
                                        after_payload.get("vol_exec")
                                        or after_payload.get("executed_volume")
                                        or "0"
                                    )
                                    self.audit.emit(
                                        "ORDER_STALE_REDUCE_CANCEL_CONFIRMED",
                                        "WARNING",
                                        symbol=instrument.symbol,
                                        client_order_id=client_order_id,
                                        kraken_order_id=after_id or order_id,
                                        previous_state=state,
                                        new_state=terminal_state.value,
                                        exchange_status=after_status,
                                        executed_volume=executed,
                                        requested_volume=str(
                                            after_payload.get("vol")
                                            or after_payload.get("volume") or "0"
                                        ),
                                        cancel_error=cancel_error,
                                        age_seconds=round(age_seconds, 2),
                                        action="REFRESH_PORTFOLIO_BEFORE_RETRY",
                                    )
                                    refresh_required = True
                                    continue
                                if after_state != OrderState.UNKNOWN_RECONCILING:
                                    self.db.update_order_state(
                                        client_order_id,
                                        after_state.value,
                                        kraken_order_id=after_id or order_id,
                                    )
                                self.audit.emit(
                                    "ORDER_STALE_REDUCE_CANCEL_FAILED",
                                    "WARNING",
                                    symbol=instrument.symbol,
                                    client_order_id=client_order_id,
                                    kraken_order_id=after_id or order_id,
                                    reason="ORDER_STILL_NONTERMINAL_AFTER_CANCEL",
                                    exchange_status=after_status,
                                    cancel_error=cancel_error,
                                    age_seconds=round(age_seconds, 2),
                                )
                                remaining.append({
                                    "client_order_id": client_order_id,
                                    "state": after_state.value,
                                })
                                continue
                            self.audit.emit(
                                "ORDER_STALE_REDUCE_CANCEL_FAILED",
                                "WARNING",
                                symbol=instrument.symbol,
                                client_order_id=client_order_id,
                                kraken_order_id=order_id,
                                reason="POST_CANCEL_LOOKUP_EMPTY",
                                cancel_error=cancel_error,
                                age_seconds=round(age_seconds, 2),
                            )
                        except Exception as exc:
                            self.audit.emit(
                                "ORDER_STALE_REDUCE_CANCEL_FAILED",
                                "WARNING",
                                symbol=instrument.symbol,
                                client_order_id=client_order_id,
                                kraken_order_id=order_id,
                                reason="POST_CANCEL_RECONCILIATION_FAILED",
                                error_type=type(exc).__name__,
                                error=str(exc)[:400],
                                cancel_error=cancel_error,
                                age_seconds=round(age_seconds, 2),
                            )

                remaining.append({
                    "client_order_id": client_order_id,
                    "state": resolved_state.value,
                })
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
        return remaining, refresh_required

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
