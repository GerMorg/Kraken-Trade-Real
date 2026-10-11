from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import time
import threading
from typing import Any, Iterable

from app.domain.models import Instrument, OrderIntent, is_valid_kraken_client_order_id
from app.domain.states import OrderState
from app.kraken.client import KrakenAmbiguous, KrakenError
from app.domain.symbols import canonical_asset
from app.trading.cash_budget import spot_cash_required_quote


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
                "fee_verified": 0,
                "fee_unverified": 0,
                "ledger_batches": 0,
                "ledger_lookup_failures": 0,
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
                    # A trade record has a fee amount but omits its currency.
                    # Ask Kraken for the exact linked ledger IDs in the same query.
                    "ledgers": True,
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

                # Resolve ledger IDs only for app-managed orders. A missing API
                # permission or an ambiguous ledger response downgrades attribution
                # to UNVERIFIED, never a trading-cycle failure.
                ledger_ids_by_trade: dict[str, list[str]] = {}
                all_ledger_ids: list[str] = []
                for trade_id, payload in trades.items():
                    if not isinstance(payload, dict):
                        continue
                    order = tracked_orders.get(str(payload.get("ordertxid") or ""))
                    if order is None:
                        continue
                    raw_ids = payload.get("ledgers") or []
                    if isinstance(raw_ids, dict):
                        ids = [str(value) for value in raw_ids.values() if value]
                    elif isinstance(raw_ids, (list, tuple, set)):
                        ids = [str(value) for value in raw_ids if value]
                    elif isinstance(raw_ids, str):
                        ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
                    else:
                        ids = []
                    # Preserve order and avoid duplicate private API requests.
                    ids = list(dict.fromkeys(ids))
                    ledger_ids_by_trade[str(trade_id)] = ids
                    all_ledger_ids.extend(ids)
                unique_ledger_ids = list(dict.fromkeys(all_ledger_ids))
                ledger_records: dict[str, dict[str, Any]] = {}
                ledger_lookup_failed = False
                query_ledgers = getattr(self.gateway, "spot_query_ledgers", None)
                if unique_ledger_ids and callable(query_ledgers):
                    for offset in range(0, len(unique_ledger_ids), 20):
                        batch = unique_ledger_ids[offset:offset + 20]
                        try:
                            ledger_response = query_ledgers({"id": ",".join(batch)})
                            if not isinstance(ledger_response, dict):
                                raise KrakenError("SPOT_QUERY_LEDGERS_INVALID_RESPONSE")
                            if ledger_response.get("error"):
                                raise KrakenError(
                                    "SPOT_QUERY_LEDGERS:"
                                    + ";".join(str(x) for x in ledger_response["error"])[:300]
                                )
                            ledger_result = ledger_response.get("result")
                            if isinstance(ledger_result, dict):
                                ledger_response = ledger_result
                            for ledger_id, record in ledger_response.items():
                                if ledger_id == "error" or not isinstance(record, dict):
                                    continue
                                if ledger_id in batch:
                                    ledger_records[str(ledger_id)] = record
                            stats["ledger_batches"] += 1
                        except Exception as exc:
                            ledger_lookup_failed = True
                            stats["ledger_lookup_failures"] += 1
                            self.db.event(
                                "SPOT_FILL_LEDGER_LOOKUP_FAILED",
                                "WARNING",
                                {"error_type": type(exc).__name__, "error": str(exc)[:300],
                                 "requested_ids": len(batch)},
                            )
                            if self.audit is not None:
                                self.audit.emit(
                                    "SPOT_FILL_LEDGER_LOOKUP_FAILED",
                                    "WARNING",
                                    error_type=type(exc).__name__,
                                    error=str(exc)[:300],
                                    requested_ids=len(batch),
                                )
                            # Try later pages/cycles; keep every fill durable meanwhile.
                            break
                elif unique_ledger_ids:
                    ledger_lookup_failed = True
                    stats["ledger_lookup_failures"] += 1

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
                        if payload.get("fee") in (None, ""):
                            raise ValueError("MISSING_TRADE_FEE")
                        fee = D(str(payload["fee"]))
                        if not all(value.is_finite() for value in (price, quantity, fee)):
                            raise ValueError("NON_FINITE_TRADE_VALUES")
                    except Exception:
                        stats["unmatched"] += 1
                        continue
                    side = str(payload.get("type") or "").strip().lower()
                    if side not in {"buy", "sell"} or trade_time <= 0 or price <= 0 or quantity <= 0 or fee < 0:
                        stats["unmatched"] += 1
                        continue

                    related_ids = ledger_ids_by_trade.get(str(trade_id), [])
                    attribution: dict[str, Any] = {
                        "status": "UNVERIFIED",
                        "fee_currency": "",
                        "ledger_ids": related_ids,
                        "reason": "LEDGER_LOOKUP_UNAVAILABLE" if ledger_lookup_failed else "NO_MATCHING_LEDGER_FEE",
                    }
                    resolved_currency = "UNVERIFIED"
                    if fee == 0:
                        resolved_currency = "ZERO_FEE"
                        attribution.update({"status": "ZERO_FEE_REPORTED", "fee_currency": "ZERO_FEE",
                                            "reason": "TRADE_HISTORY_REPORTED_ZERO_FEE"})
                        stats["fee_verified"] += 1
                    elif related_ids and all(item in ledger_records for item in related_ids):
                        # Kraken ledger records carry the fee and asset together.
                        # Verify the TradesHistory fee against per-asset ledger fees;
                        # do not guess a currency when the match is absent/ambiguous.
                        fees_by_asset: dict[str, D] = {}
                        for ledger_id in related_ids:
                            ledger = ledger_records[ledger_id]
                            if str(ledger.get("type") or "").lower() != "trade":
                                continue
                            asset = str(ledger.get("asset") or "").strip()
                            ledger_fee: D | None = None
                            try:
                                parsed_fee = D(str(ledger.get("fee") or "0"))
                                if parsed_fee.is_finite() and parsed_fee >= 0:
                                    ledger_fee = parsed_fee
                            except (ArithmeticError, TypeError, ValueError):
                                ledger_fee = None
                            if ledger_fee is not None and asset:
                                fees_by_asset[asset] = fees_by_asset.get(asset, D("0")) + ledger_fee
                        tolerance = max(D("0.00000001"), fee.copy_abs() * D("0.000001"))
                        matches = [
                            asset for asset, total in fees_by_asset.items()
                            if (total - fee).copy_abs() <= tolerance
                        ]
                        if len(matches) == 1:
                            resolved_currency = matches[0]
                            attribution.update({
                                "status": "LEDGER_VERIFIED",
                                "fee_currency": resolved_currency,
                                "reason": "TRADE_HISTORY_FEE_MATCHED_TO_LEDGER_ASSET",
                            })
                            stats["fee_verified"] += 1
                        else:
                            attribution.update({
                                "reason": "LEDGER_FEE_MATCH_NOT_UNIQUE",
                                "ledger_fee_by_asset": {key: str(value) for key, value in fees_by_asset.items()},
                                "trades_history_fee": str(fee),
                            })
                            stats["fee_unverified"] += 1
                    else:
                        stats["fee_unverified"] += 1

                    enriched_payload = dict(payload)
                    enriched_payload["_ledger_attribution"] = attribution
                    inserted = self.db.save_attributed_fill(
                        order_id=exchange_order_id,
                        trade_id=str(trade_id),
                        created_at=trade_time,
                        symbol=str(order.get("symbol") or ""),
                        side=side,
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        fee_currency=resolved_currency,
                        client_order_id=str(order.get("client_order_id") or ""),
                        decision_id=str(order.get("decision_id") or ""),
                        venue="spot",
                        quote_asset=str(instrument.quote or ""),
                        base_asset=str(instrument.base or ""),
                        raw_payload=enriched_payload,
                    )
                    if not inserted and attribution["status"] in {"LEDGER_VERIFIED", "ZERO_FEE_REPORTED"}:
                        self.db.enrich_attributed_fill_fee(
                            order_id=exchange_order_id,
                            trade_id=str(trade_id),
                            fee_currency=resolved_currency,
                            raw_payload=enriched_payload,
                            base_asset=str(instrument.base or ""),
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

    @staticmethod
    def _submission_error_class(error_text: str) -> tuple[str, str]:
        """Classify deterministic exchange rejections without changing retry behaviour."""
        error = str(error_text or "").lower()
        if "insufficient funds" in error:
            return (
                "INSUFFICIENT_FUNDS",
                "REFRESH_BALANCE_MARGIN_AND_OPEN_ORDER_HOLDS_BEFORE_RETRY",
            )
        if "invalid arguments" in error:
            return "INVALID_ARGUMENTS", "REVIEW_EXCHANGE_API_ARGUMENTS;DO_NOT_BLINDLY_RETRY"
        if "min order" in error or "volume minimum" in error:
            return "ORDER_BELOW_EXCHANGE_MINIMUM", "REFRESH_INSTRUMENT_MINIMUMS_AND_REQUANTIZE"
        if "rate limit" in error or "throttled" in error:
            return "RATE_LIMITED", "RETRY_ONLY_AFTER_RATE_LIMIT_WINDOW"
        return "KRAKEN_DETERMINISTIC_REJECTION", "REVIEW_EXCHANGE_ERROR_AND_ORDER_CONTEXT"

    def _intent_audit_context(self, intent: OrderIntent, market: Any) -> dict[str, Any]:
        """Safe structured facts for diagnosing order rejections; never includes credentials."""
        quantity = D(str(intent.quantity))
        reference_price = intent.limit_price
        if reference_price is None or D(str(reference_price or "0")) <= 0:
            reference_price = (
                getattr(market, "ask", None)
                if str(intent.side).lower() == "buy"
                else getattr(market, "bid", None)
            ) or getattr(market, "price", None)
        notional_quote: str | None = None
        try:
            price_value = D(str(reference_price or "0"))
            if quantity > 0 and price_value > 0:
                notional_quote = str(quantity * price_value)
        except (ArithmeticError, TypeError, ValueError):
            notional_quote = None

        context: dict[str, Any] = {
            "intent_id": str(intent.intent_id),
            "client_order_id": str(intent.client_order_id),
            "decision_id": str(intent.decision_id),
            "symbol": str(intent.instrument.symbol),
            "instrument_id": str(intent.instrument.instrument_id),
            "venue": str(intent.instrument.venue),
            "product_type": str(getattr(intent.instrument.product_type, "value", "")),
            "side": str(intent.side).lower(),
            "direction": str(getattr(intent.direction, "value", intent.direction)),
            "order_type": str(intent.order_type),
            "quantity": str(quantity),
            "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
            "reference_price": str(reference_price) if reference_price is not None else None,
            "estimated_notional_quote": notional_quote,
            "leverage": str(intent.leverage),
            "margin": bool(intent.margin),
            "reduce_only": bool(intent.reduce_only),
            "post_only": bool(intent.post_only),
            "expected_edge_bps": str(intent.expected_edge_bps),
        }
        try:
            decision = self.db.one(
                """SELECT target_notional_eur,leverage,rationale_json
                   FROM decisions WHERE decision_id=?""",
                (intent.decision_id,),
            )
            if decision:
                context["decision_target_notional_eur"] = str(
                    decision.get("target_notional_eur") or "0"
                )
                context["decision_leverage"] = str(decision.get("leverage") or "1")
                try:
                    rationale = json.loads(decision.get("rationale_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    rationale = {}
                if isinstance(rationale, dict):
                    for source, target in (
                        ("current_position_eur", "current_position_eur"),
                        ("target_position_eur", "target_position_eur"),
                        ("trade_notional_eur", "decision_trade_notional_eur"),
                        ("target_position_usd", "target_position_usd"),
                    ):
                        if rationale.get(source) is not None:
                            context[target] = str(rationale[source])
                    context["risk_profile"] = str(rationale.get("risk_profile") or "core")
                    for key in ("position_management_action", "rebalance_reason", "edge_tier"):
                        if rationale.get(key) is not None:
                            context[key] = str(rationale[key])
        except Exception:
            # Diagnostics must never be able to block risk checks or live order submission.
            context["decision_context_available"] = False
        return context

    def _record_submission_rejection(
        self,
        intent: OrderIntent,
        market: Any,
        error_text: str,
        *,
        error_type: str,
        venue: str | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error_class, suggested_action = self._submission_error_class(error_text)
        context = self._intent_audit_context(intent, market)
        context.update({
            "error_type": str(error_type),
            "error": str(error_text)[:700],
            "error_class": error_class,
            "suggested_action": suggested_action,
        })
        if venue:
            context["venue"] = venue
        if extra_context:
            context.update(extra_context)
        self.audit.emit("ORDER_REJECTED_EXCHANGE", "WARNING", **context)
        try:
            self.db.event("ORDER_SUBMISSION_REJECTED", "WARNING", context)
        except Exception:
            # Persistence of diagnostics is best effort; preserve the exchange result.
            pass
        return {"error_class": error_class, "suggested_action": suggested_action, **context}

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
                    rejection = self._record_submission_rejection(
                        intent,
                        market,
                        error_text,
                        error_type="KrakenFuturesSendStatusRejected",
                        venue="futures",
                        extra_context={"exchange_status": exchange_status},
                    )
                    return {
                        "state": OrderState.REJECTED.value,
                        "reason": "KRAKEN_ORDER_REJECTED",
                        "exchange_status": exchange_status,
                        "exchange_error": error_text,
                        "error_class": rejection["error_class"],
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
            rejection = self._record_submission_rejection(
                intent,
                market,
                error_text,
                error_type=type(exc).__name__,
            )
            return {
                "state": OrderState.REJECTED.value,
                "reason": "KRAKEN_ORDER_REJECTED",
                "exchange_error": error_text,
                "error_class": rejection["error_class"],
                "suggested_action": rejection["suggested_action"],
                "reconciled": True,
            }

    def submit_funding_order(self, intent: OrderIntent, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Serialize funding-order submissions with all other live order requests."""
        with self._submission_lock:
            return self._submit_funding_order_locked(intent, timeout_seconds=timeout_seconds)

    def _submit_funding_order_locked(
        self, intent: OrderIntent, *, timeout_seconds: float = 30.0
    ) -> dict[str, Any]:
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

    def _spot_available_balance(self, asset: str) -> tuple[D | None, str]:
        """Read Kraken BalanceEx free funds; never treat a failed read as zero holds."""
        reader = getattr(self.gateway, "spot_balance_extended", None)
        if not callable(reader):
            # Lightweight test/legacy gateways without BalanceEx retain preflight
            # compatibility; the shipped KrakenGateway always exposes this method.
            return None, ""
        try:
            rows = reader()
            if not isinstance(rows, dict):
                return None, "BALANCE_EX_INVALID_RESPONSE"
            target = canonical_asset(asset)
            available = D("0")
            for raw_asset, row in rows.items():
                if canonical_asset(raw_asset) != target:
                    continue
                if not isinstance(row, dict):
                    return None, "BALANCE_EX_INVALID_ASSET_ROW"
                balance = D(str(row.get("balance") or "0"))
                credit = D(str(row.get("credit") or "0"))
                credit_used = D(str(row.get("credit_used") or "0"))
                hold_trade = D(str(row.get("hold_trade") or "0"))
                values = (balance, credit, credit_used, hold_trade)
                if not all(value.is_finite() for value in values):
                    return None, "BALANCE_EX_NON_FINITE_VALUE"
                available += balance + credit - credit_used - hold_trade
            return max(D("0"), available), ""
        except Exception as exc:
            return None, f"{type(exc).__name__}:{str(exc)[:250]}"

    def _preflight_available_spot_funds(
        self, intent: OrderIntent, market: Any
    ) -> dict[str, Any] | None:
        """Gate Spot cash buys/sells against free funds, including external open orders."""
        if (
            intent.instrument.venue != "spot"
            or intent.leverage > D("1")
            or bool(getattr(intent, "margin", False))
            or str(getattr(intent.instrument.product_type, "value", "")) == "DERIVATIVE"
        ):
            return None

        side = str(intent.side or "").strip().lower()
        if side == "buy" and not intent.reduce_only:
            asset = intent.instrument.quote
            price = intent.limit_price
            if price is None or D(str(price)) <= 0:
                price = getattr(market, "ask", None) or getattr(market, "price", None)
            try:
                required = spot_cash_required_quote(
                    D(str(intent.quantity)), D(str(price or "0")), self.config
                )
            except (ArithmeticError, TypeError, ValueError):
                required = D("0")
            if required <= 0:
                return {
                    "allowed": False,
                    "reason": "SPOT_CASH_BUDGET_PRICE_UNAVAILABLE",
                    "detail": {"asset": str(asset), "quantity": str(intent.quantity)},
                }
        elif side == "sell":
            asset = intent.instrument.base
            required = D(str(intent.quantity))
        else:
            return None

        available, error = self._spot_available_balance(str(asset))
        if error:
            return {
                "allowed": False,
                "reason": "SPOT_AVAILABLE_BALANCE_UNAVAILABLE",
                "detail": {"asset": str(asset), "error": error},
            }
        if available is None:
            return None
        if required > available:
            return {
                "allowed": False,
                "reason": (
                    "SPOT_AVAILABLE_QUOTE_INSUFFICIENT"
                    if side == "buy"
                    else "SPOT_AVAILABLE_BASE_INSUFFICIENT"
                ),
                "detail": {
                    "asset": str(asset),
                    "required": str(required),
                    "available": str(available),
                    "held_or_reserved_funds_included": True,
                },
            }
        return None

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
        if ok:
            funds_check = self._preflight_available_spot_funds(intent, market)
            if funds_check is not None:
                return funds_check
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
