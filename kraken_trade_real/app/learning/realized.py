from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from typing import Any

from app.domain.symbols import canonical_asset


D = Decimal
ZERO = D("0")
BPS = D("10000")


def _decimal(value: Any) -> Decimal:
    result = D(str(value))
    if not result.is_finite():
        raise InvalidOperation("non-finite number")
    return result


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


class CoreSpotRealizedOutcomeLedger:
    """Rebuild FIFO Spot attribution with separately gated verified net outcomes.

    TradesHistory fee amounts are never assumed to be quote-denominated. A net
    label is verified only when both fill fees were attributed to the instrument's
    quote asset, or were explicitly reported as zero, and neither leg involved
    margin/leverage whose financing costs are not represented by spot trade fees.
    Legacy/unresolved rows retain their estimate and never receive a fabricated
    verified PnL value.
    """

    FEE_STATUS = "QUOTE_ESTIMATE_ONLY"

    def __init__(self, db: Any) -> None:
        self.db = db

    def rebuild(self) -> dict[str, Any]:
        rows = self.db.query(
            """SELECT
                 f.order_id AS exchange_order_id, f.trade_id, f.created_at,
                 f.symbol, f.side, f.quantity, f.price, f.fee, f.fee_currency,
                 f.client_order_id, f.decision_id AS fill_decision_id,
                 f.venue, f.quote_asset, f.raw_json,
                 o.direction AS order_direction, o.reduce_only,
                 o.leverage, o.margin, o.decision_id AS order_decision_id
               FROM fills AS f
               JOIN orders AS o
                 ON o.kraken_order_id=f.order_id
                AND o.client_order_id=f.client_order_id
               WHERE lower(f.venue)='spot'
               ORDER BY f.created_at ASC, f.order_id ASC, f.trade_id ASC"""
        )

        lots_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        legs: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        processed = 0

        def anomaly(row: dict[str, Any], reason: str, detail: dict[str, Any]) -> None:
            anomaly_id = _stable_id(
                row.get("exchange_order_id"), row.get("trade_id"), reason, detail
            )
            anomalies.append({
                "anomaly_id": anomaly_id,
                "detected_at": time.time(),
                "symbol": str(row.get("symbol") or ""),
                "order_id": str(row.get("exchange_order_id") or ""),
                "trade_id": str(row.get("trade_id") or ""),
                "reason": reason,
                "detail_json": json.dumps(detail, sort_keys=True, default=str),
            })

        for row in rows:
            processed += 1
            symbol = str(row.get("symbol") or "")
            order_id = str(row.get("exchange_order_id") or "")
            trade_id = str(row.get("trade_id") or "")
            try:
                quantity = _decimal(row.get("quantity"))
                price = _decimal(row.get("price"))
                fee_est = _decimal(row.get("fee") or "0")
                fill_time = float(row.get("created_at") or 0)
                raw = json.loads(row.get("raw_json") or "{}")
            except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
                anomaly(row, "INVALID_FILL_NUMERIC_OR_RAW_DATA", {})
                continue

            direction = str(row.get("order_direction") or "").upper()
            side = str(row.get("side") or "").lower()
            reduce_only = bool(int(row.get("reduce_only") or 0))
            quote_asset = str(row.get("quote_asset") or "")
            opening_decision_id = str(row.get("order_decision_id") or "")
            client_order_id = str(row.get("client_order_id") or "")
            if not isinstance(raw, dict) or not raw.get("ordertxid") or not raw.get("pair"):
                anomaly(row, "LEGACY_FILL_WITHOUT_EXCHANGE_CONTEXT", {
                    "fee_currency": row.get("fee_currency"),
                })
                continue
            if (
                quantity <= 0 or price <= 0 or fee_est < 0 or fill_time <= 0
                or direction not in {"LONG", "SHORT"}
                or side not in {"buy", "sell"}
                or side != ("buy" if direction == "LONG" else "sell")
                or not quote_asset
            ):
                anomaly(row, "INVALID_OR_INCONSISTENT_FILL_DIRECTION", {
                    "direction": direction, "side": side, "quantity": str(quantity),
                    "price": str(price), "quote_asset": quote_asset,
                })
                continue
            fill_decision_id = str(row.get("fill_decision_id") or "")
            fee_attribution = raw.get("_ledger_attribution", {})
            if not isinstance(fee_attribution, dict):
                fee_attribution = {}
            attribution_status = str(fee_attribution.get("status") or "")
            fee_currency = str(row.get("fee_currency") or "")
            margin_ledger_raw = fee_attribution.get("margin_ledger")
            margin_ledger = (
                margin_ledger_raw if isinstance(margin_ledger_raw, dict) else {}
            )
            margin_ledger_verified = (
                attribution_status == "MARGIN_LEDGER_VERIFIED"
                and bool(margin_ledger)
                and canonical_asset(str(margin_ledger.get("asset") or ""))
                    == canonical_asset(quote_asset)
            )
            try:
                margin_ledger_amount = (
                    _decimal(margin_ledger.get("amount"))
                    if margin_ledger_verified else ZERO
                )
                margin_ledger_fee = (
                    _decimal(margin_ledger.get("fee"))
                    if margin_ledger_verified else ZERO
                )
            except (InvalidOperation, TypeError, ValueError):
                margin_ledger_verified = False
                margin_ledger_amount = ZERO
                margin_ledger_fee = ZERO
            quote_fee_verified = bool(
                margin_ledger_verified
                or (fee_est == ZERO and attribution_status == "ZERO_FEE_REPORTED")
                or (
                    fee_est > ZERO
                    and attribution_status == "LEDGER_VERIFIED"
                    and fee_currency not in {"", "UNVERIFIED", "ZERO_FEE"}
                    and canonical_asset(fee_currency) == canonical_asset(quote_asset)
                )
            )
            verified_fee_quote = (
                margin_ledger_fee if margin_ledger_verified
                else fee_est if fee_est > ZERO and quote_fee_verified
                else ZERO
            )
            if not opening_decision_id:
                anomaly(row, "MISSING_LOCAL_DECISION_ID", {})
                continue
            if fill_decision_id != opening_decision_id:
                anomaly(row, "FILL_DECISION_ORDER_DECISION_MISMATCH", {
                    "fill_decision_id": fill_decision_id,
                    "order_decision_id": opening_decision_id,
                })
                continue

            fill_key = _stable_id(order_id, trade_id)
            if not reduce_only:
                opposite = "SHORT" if direction == "LONG" else "LONG"
                opposite_open = sum(
                    (lot["remaining_quantity"] for lot in lots_by_key[(symbol, opposite)]),
                    ZERO,
                )
                if opposite_open > 0:
                    anomaly(row, "OPPOSITE_POSITION_OPEN_DURING_ENTRY", {
                        "order_direction": direction,
                        "opposite_open_quantity": str(opposite_open),
                    })
                    continue
                lots_by_key[(symbol, direction)].append({
                    "lot_id": fill_key,
                    "symbol": symbol,
                    "direction": direction,
                    "opened_at": fill_time,
                    "entry_order_id": order_id,
                    "entry_trade_id": trade_id,
                    "opening_decision_id": opening_decision_id,
                    "client_order_id": client_order_id,
                    "entry_price": price,
                    "remaining_quantity": quantity,
                    "remaining_entry_fee_est_quote": fee_est,
                    "remaining_entry_fee_verified_quote": verified_fee_quote,
                    "entry_fee_verified": quote_fee_verified,
                    "entry_fee_currency": fee_currency,
                    "entry_fee_attribution_status": attribution_status,
                    "margin_ledger_verified": margin_ledger_verified,
                    "margin_ledger_open_fee_quote": margin_ledger_fee,
                    "margin_ledger_open_amount_quote": margin_ledger_amount,
                    "quote_asset": quote_asset,
                    "leverage": str(row.get("leverage") or "1"),
                    "margin": bool(int(row.get("margin") or 0)),
                })
                continue

            closing_direction = "LONG" if direction == "SHORT" else "SHORT"
            position_lots = lots_by_key[(symbol, closing_direction)]
            # Snapshot identity before mutating lots for this fill. Otherwise an
            # early FIFO leg could make a multi-decision position look unambiguous
            # to the later leg of this very same exchange fill.
            opening_decisions_at_fill = {
                str(open_lot.get("opening_decision_id") or "")
                for open_lot in position_lots
                if _decimal(open_lot.get("remaining_quantity") or "0") > 0
            }
            remaining_close_qty = quantity
            for lot in position_lots:
                if remaining_close_qty <= 0:
                    break
                lot_qty_before = lot["remaining_quantity"]
                if lot_qty_before <= 0:
                    continue
                matched_qty = min(lot_qty_before, remaining_close_qty)
                allocated_entry_fee = (
                    lot["remaining_entry_fee_est_quote"] * matched_qty / lot_qty_before
                )
                allocated_exit_fee = fee_est * matched_qty / quantity
                entry_price = lot["entry_price"]
                gross_pnl = (
                    (price - entry_price) * matched_qty
                    if closing_direction == "LONG"
                    else (entry_price - price) * matched_qty
                )
                entry_notional = entry_price * matched_qty
                estimated_net = gross_pnl - allocated_entry_fee - allocated_exit_fee
                allocated_verified_entry_fee = (
                    lot["remaining_entry_fee_verified_quote"] * matched_qty / lot_qty_before
                    if lot["entry_fee_verified"] and lot_qty_before > 0 else ZERO
                )
                allocated_verified_exit_fee = (
                    verified_fee_quote * matched_qty / quantity if quote_fee_verified else ZERO
                )
                try:
                    entry_leverage = _decimal(lot.get("leverage") or "1")
                    exit_leverage = _decimal(row.get("leverage") or "1")
                except (InvalidOperation, TypeError, ValueError):
                    entry_leverage = exit_leverage = D("0")
                no_margin_financing = (
                    not bool(lot["margin"])
                    and not bool(int(row.get("margin") or 0))
                    and entry_leverage == D("1")
                    and exit_leverage == D("1")
                )
                verified_net = None
                verified_fees = None
                verified_source = self.FEE_STATUS

                if (
                    lot["entry_fee_verified"]
                    and quote_fee_verified
                    and no_margin_financing
                    and canonical_asset(lot["quote_asset"]) == canonical_asset(quote_asset)
                ):
                    verified_net = (
                        gross_pnl - allocated_verified_entry_fee - allocated_verified_exit_fee
                    )
                    verified_fees = allocated_verified_entry_fee + allocated_verified_exit_fee
                    verified_source = "LEDGER_VERIFIED_SPOT_NET"

                # Kraken's Margin amount is the authoritative realised P/L before
                # fees. A net margin label is only safe when we can match both
                # entry and exit to one quote-currency Margin ledger record, every
                # closing fill belongs to one opening decision, and the whole
                # holding interval is shorter than the first four-hour rollover.
                close_margin_ledger = margin_ledger
                close_margin_valid = (
                    margin_ledger_verified
                    and bool(int(row.get("margin") or 0))
                    and reduce_only
                    and canonical_asset(lot["quote_asset"]) == canonical_asset(quote_asset)
                )
                same_opening_decision = len(opening_decisions_at_fill) == 1
                held_seconds = fill_time - float(lot.get("opened_at") or fill_time)
                no_rollover_window = 0 <= held_seconds < 4 * 3600
                if (
                    bool(lot.get("margin"))
                    and bool(int(row.get("margin") or 0))
                    and bool(lot.get("margin_ledger_verified"))
                    and close_margin_valid
                    and same_opening_decision
                    and no_rollover_window
                    and canonical_asset(str(close_margin_ledger.get("asset") or ""))
                        == canonical_asset(quote_asset)
                ):
                    try:
                        actual_margin_pnl = _decimal(close_margin_ledger.get("amount"))
                        actual_margin_fee = _decimal(close_margin_ledger.get("fee"))
                        if actual_margin_fee < 0:
                            raise InvalidOperation("negative margin ledger fee")
                        allocated_margin_pnl = actual_margin_pnl * matched_qty / quantity
                        allocated_margin_fee = actual_margin_fee * matched_qty / quantity
                        # Use the real exchange P/L as the realised gross result;
                        # no price-derived or estimated financing value is promoted.
                        gross_pnl = allocated_margin_pnl
                        estimated_net = gross_pnl - allocated_entry_fee - allocated_exit_fee
                        verified_net = (
                            allocated_margin_pnl
                            - allocated_verified_entry_fee
                            - allocated_margin_fee
                        )
                        verified_fees = allocated_verified_entry_fee + allocated_margin_fee
                        verified_source = "LEDGER_VERIFIED_MARGIN_NET"
                    except (InvalidOperation, TypeError, ValueError, ArithmeticError):
                        verified_net = None
                        verified_fees = None
                        verified_source = self.FEE_STATUS
                gross_bps = gross_pnl / entry_notional * BPS if entry_notional > 0 else ZERO
                net_bps = estimated_net / entry_notional * BPS if entry_notional > 0 else ZERO
                verified_net_bps = (
                    verified_net / entry_notional * BPS
                    if verified_net is not None and entry_notional > 0 else None
                )
                leg_id = _stable_id(lot["lot_id"], fill_key, str(matched_qty))
                legs.append({
                    "leg_id": leg_id,
                    "symbol": symbol,
                    "venue": "spot",
                    "direction": closing_direction,
                    "opened_at": lot["opened_at"],
                    "closed_at": fill_time,
                    "entry_order_id": lot["entry_order_id"],
                    "entry_trade_id": lot["entry_trade_id"],
                    "close_order_id": order_id,
                    "close_trade_id": trade_id,
                    "opening_decision_id": lot["opening_decision_id"],
                    "closing_decision_id": opening_decision_id,
                    "quantity": str(matched_qty),
                    "entry_price": str(entry_price),
                    "exit_price": str(price),
                    "entry_notional_quote": str(entry_notional),
                    "gross_pnl_quote": str(gross_pnl),
                    "entry_fee_est_quote": str(allocated_entry_fee),
                    "exit_fee_est_quote": str(allocated_exit_fee),
                    "estimated_net_pnl_quote": str(estimated_net),
                    "quote_asset": lot["quote_asset"],
                    "gross_return_bps": str(gross_bps),
                    "estimated_net_return_bps": str(net_bps),
                    "fee_status": verified_source if verified_net is not None else self.FEE_STATUS,
                    "verified_fees_quote": str(verified_fees) if verified_fees is not None else None,
                    "verified_net_pnl_quote": str(verified_net) if verified_net is not None else None,
                    "verified_net_return_bps": str(verified_net_bps) if verified_net_bps is not None else None,
                    "net_verified": 1 if verified_net is not None else 0,
                    "detail_json": json.dumps({
                        "entry_fee_attribution_status": lot.get("entry_fee_attribution_status", ""),
                        "entry_fee_currency": lot.get("entry_fee_currency", "UNVERIFIED"),
                        "entry_quote_asset": lot.get("quote_asset", ""),
                        "exit_fee_attribution_status": attribution_status,
                        "exit_fee_currency": fee_currency,
                        "exit_quote_asset": quote_asset,
                        "entry_margin_ledger_verified": bool(lot.get("margin_ledger_verified")),
                        "exit_margin_ledger_verified": margin_ledger_verified,
                        "margin_ledger_close_amount": str(margin_ledger_amount) if margin_ledger_verified else "",
                        "margin_ledger_close_fee": str(margin_ledger_fee) if margin_ledger_verified else "",
                        "held_seconds": round(held_seconds, 6),
                        "rollover_free_window": no_rollover_window,
                        "net_verified": verified_net is not None,
                        "quote_asset_matches": canonical_asset(lot["quote_asset"]) == canonical_asset(quote_asset),
                        "entry_leverage": lot["leverage"],
                        "entry_margin": lot["margin"],
                        "exit_leverage": str(row.get("leverage") or "1"),
                        "exit_margin": bool(int(row.get("margin") or 0)),
                        "entry_client_order_id": lot["client_order_id"],
                        "close_client_order_id": client_order_id,
                    }, sort_keys=True, default=str),
                })
                lot["remaining_quantity"] = lot_qty_before - matched_qty
                lot["remaining_entry_fee_est_quote"] -= allocated_entry_fee
                if lot["entry_fee_verified"]:
                    lot["remaining_entry_fee_verified_quote"] -= allocated_verified_entry_fee
                remaining_close_qty -= matched_qty
            if remaining_close_qty > 0:
                anomaly(row, "REDUCE_ONLY_EXCEEDS_LOCAL_INVENTORY", {
                    "close_direction": closing_direction,
                    "fill_quantity": str(quantity),
                    "matched_quantity": str(quantity - remaining_close_qty),
                    "unmatched_quantity": str(remaining_close_qty),
                })
            lots_by_key[(symbol, closing_direction)] = [
                lot for lot in position_lots if lot["remaining_quantity"] > 0
            ]

        outcomes_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for leg in legs:
            key = (
                leg["opening_decision_id"], leg["symbol"], leg["direction"]
            )
            out = outcomes_by_key.setdefault(key, {
                "opening_decision_id": key[0],
                "symbol": key[1],
                "direction": key[2],
                "first_opened_at": float(leg["opened_at"]),
                "last_closed_at": float(leg["closed_at"]),
                "matched_legs": 0,
                "closed_quantity": ZERO,
                "closed_notional_quote": ZERO,
                "gross_pnl_quote": ZERO,
                "fees_est_quote": ZERO,
                "estimated_net_pnl_quote": ZERO,
                "quote_asset": leg["quote_asset"],
                "fee_status": self.FEE_STATUS,
                "net_verified": 1,
                "verified_fees_quote": ZERO,
                "verified_net_pnl_quote": ZERO,
                "verified_statuses": set(),
                "detail": [],
            })
            out["first_opened_at"] = min(out["first_opened_at"], float(leg["opened_at"]))
            out["last_closed_at"] = max(out["last_closed_at"], float(leg["closed_at"]))
            out["matched_legs"] += 1
            out["closed_quantity"] += _decimal(leg["quantity"])
            out["closed_notional_quote"] += _decimal(leg["entry_notional_quote"])
            out["gross_pnl_quote"] += _decimal(leg["gross_pnl_quote"])
            out["fees_est_quote"] += (
                _decimal(leg["entry_fee_est_quote"]) + _decimal(leg["exit_fee_est_quote"])
            )
            out["estimated_net_pnl_quote"] += _decimal(leg["estimated_net_pnl_quote"])
            if int(leg.get("net_verified") or 0) == 1:
                out["verified_fees_quote"] += _decimal(leg["verified_fees_quote"])
                out["verified_net_pnl_quote"] += _decimal(leg["verified_net_pnl_quote"])
                out["verified_statuses"].add(str(leg.get("fee_status") or ""))
            else:
                # A decision-level outcome is verified only when every FIFO leg
                # has quote-currency fee evidence and no unaccounted margin costs.
                out["net_verified"] = 0
            out["detail"].append(leg["leg_id"])

        outcome_rows: list[dict[str, Any]] = []
        for out in outcomes_by_key.values():
            notional = out["closed_notional_quote"]
            gross_bps = out["gross_pnl_quote"] / notional * BPS if notional > 0 else ZERO
            net_bps = out["estimated_net_pnl_quote"] / notional * BPS if notional > 0 else ZERO
            verified_net_bps = (
                out["verified_net_pnl_quote"] / notional * BPS
                if out["net_verified"] and notional > 0 else None
            )
            verified_statuses = out["verified_statuses"]
            if not out["net_verified"]:
                aggregate_fee_status = self.FEE_STATUS
            elif verified_statuses == {"LEDGER_VERIFIED_MARGIN_NET"}:
                aggregate_fee_status = "LEDGER_VERIFIED_MARGIN_NET"
            elif verified_statuses == {"LEDGER_VERIFIED_SPOT_NET"}:
                aggregate_fee_status = "LEDGER_VERIFIED_SPOT_NET"
            else:
                aggregate_fee_status = "LEDGER_VERIFIED_MIXED_NET"

            outcome_rows.append({
                **out,
                "gross_return_bps": str(gross_bps),
                "estimated_net_return_bps": str(net_bps),
                "verified_net_return_bps": str(verified_net_bps) if verified_net_bps is not None else None,
                "verified_fees_quote": str(out["verified_fees_quote"]) if out["net_verified"] else None,
                "verified_net_pnl_quote": str(out["verified_net_pnl_quote"]) if out["net_verified"] else None,
                "fee_status": aggregate_fee_status,
                "closed_quantity": str(out["closed_quantity"]),
                "closed_notional_quote": str(notional),
                "gross_pnl_quote": str(out["gross_pnl_quote"]),
                "fees_est_quote": str(out["fees_est_quote"]),
                "estimated_net_pnl_quote": str(out["estimated_net_pnl_quote"]),
                "detail_json": json.dumps({
                    "leg_ids": out["detail"],
                    "net_verified": bool(out["net_verified"]),
                    "attribution_basis": "app-local-orders-and-Spot-TradesHistory+QueryLedgers",
                    "verified_scope": (
                        "quote-currency verified non-margin Spot; or a single-decision Spot-Margin "
                        "position closed before the first four-hour rollover with linked exchange Margin P/L and fee"
                    ),
                    "verified_fee_statuses": sorted(verified_statuses),
                }, sort_keys=True),
            })

        open_lots: list[dict[str, Any]] = []
        for key_lots in lots_by_key.values():
            for lot in key_lots:
                open_lots.append({
                    "lot_id": lot["lot_id"],
                    "symbol": lot["symbol"],
                    "direction": lot["direction"],
                    "opened_at": lot["opened_at"],
                    "entry_order_id": lot["entry_order_id"],
                    "entry_trade_id": lot["entry_trade_id"],
                    "opening_decision_id": lot["opening_decision_id"],
                    "entry_price": str(lot["entry_price"]),
                    "remaining_quantity": str(lot["remaining_quantity"]),
                    "remaining_entry_fee_est_quote": str(lot["remaining_entry_fee_est_quote"]),
                    "remaining_entry_fee_verified_quote": str(lot["remaining_entry_fee_verified_quote"]),
                    "entry_fee_verified": int(bool(lot["entry_fee_verified"])),
                    "quote_asset": lot["quote_asset"],
                    "detail_json": json.dumps({
                        "leverage": lot["leverage"],
                        "margin": lot["margin"],
                        "client_order_id": lot["client_order_id"],
                        "fee_status": (
                            "MARGIN_LEDGER_VERIFIED"
                            if lot.get("margin_ledger_verified") else self.FEE_STATUS
                        ),
                        "margin_ledger_verified": bool(lot.get("margin_ledger_verified")),
                        "margin_ledger_open_fee_quote": str(lot.get("margin_ledger_open_fee_quote") or ZERO),
                        "entry_fee_currency": lot.get("entry_fee_currency", "UNVERIFIED"),
                        "entry_fee_attribution_status": lot.get("entry_fee_attribution_status", ""),
                    }, sort_keys=True),
                })

        leg_columns = (
            "leg_id,symbol,venue,direction,opened_at,closed_at,entry_order_id,entry_trade_id,"
            "close_order_id,close_trade_id,opening_decision_id,closing_decision_id,quantity,"
            "entry_price,exit_price,entry_notional_quote,gross_pnl_quote,entry_fee_est_quote,"
            "exit_fee_est_quote,estimated_net_pnl_quote,quote_asset,gross_return_bps,"
            "estimated_net_return_bps,fee_status,verified_fees_quote,verified_net_pnl_quote,"
            "verified_net_return_bps,net_verified,detail_json"
        )
        outcome_columns = (
            "opening_decision_id,symbol,direction,first_opened_at,last_closed_at,matched_legs,"
            "closed_quantity,closed_notional_quote,gross_pnl_quote,fees_est_quote,"
            "estimated_net_pnl_quote,gross_return_bps,estimated_net_return_bps,quote_asset,"
            "fee_status,net_verified,verified_fees_quote,verified_net_pnl_quote,"
            "verified_net_return_bps,detail_json"
        )
        lot_columns = (
            "lot_id,symbol,direction,opened_at,entry_order_id,entry_trade_id,opening_decision_id,"
            "entry_price,remaining_quantity,remaining_entry_fee_est_quote,"
            "remaining_entry_fee_verified_quote,entry_fee_verified,quote_asset,detail_json"
        )
        anomaly_columns = (
            "anomaly_id,detected_at,symbol,order_id,trade_id,reason,detail_json"
        )

        def tuples_for(items: list[dict[str, Any]], columns: str) -> list[tuple[Any, ...]]:
            names = columns.split(",")
            return [tuple(item[name] for name in names) for item in items]

        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM core_realized_legs")
                con.execute("DELETE FROM core_realized_outcomes")
                con.execute("DELETE FROM core_inventory_lots")
                con.execute("DELETE FROM core_fill_anomalies")
                if legs:
                    con.executemany(
                        f"INSERT INTO core_realized_legs({leg_columns}) VALUES({','.join('?' for _ in leg_columns.split(','))})",
                        tuples_for(legs, leg_columns),
                    )
                if outcome_rows:
                    con.executemany(
                        f"INSERT INTO core_realized_outcomes({outcome_columns}) VALUES({','.join('?' for _ in outcome_columns.split(','))})",
                        tuples_for(outcome_rows, outcome_columns),
                    )
                if open_lots:
                    con.executemany(
                        f"INSERT INTO core_inventory_lots({lot_columns}) VALUES({','.join('?' for _ in lot_columns.split(','))})",
                        tuples_for(open_lots, lot_columns),
                    )
                if anomalies:
                    con.executemany(
                        f"INSERT INTO core_fill_anomalies({anomaly_columns}) VALUES({','.join('?' for _ in anomaly_columns.split(','))})",
                        tuples_for(anomalies, anomaly_columns),
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

        anomaly_reason_counts: dict[str, int] = defaultdict(int)
        for item in anomalies:
            anomaly_reason_counts[str(item["reason"])] += 1

        # Explain why net outcomes remain estimate-only. These are per-leg
        # blocker counts (not mutually exclusive) so operators can distinguish
        # missing fee evidence from margin/rollover limitations without querying
        # the SQLite database manually.
        unverified_net_reason_counts: dict[str, int] = defaultdict(int)
        for leg in legs:
            if int(leg.get("net_verified") or 0) == 1:
                continue
            try:
                detail = json.loads(leg.get("detail_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            entry_fee_status = str(detail.get("entry_fee_attribution_status") or "")
            entry_fee_currency = str(detail.get("entry_fee_currency") or "")
            entry_quote_asset = str(detail.get("entry_quote_asset") or "")
            if entry_fee_status not in {"LEDGER_VERIFIED", "ZERO_FEE_REPORTED"}:
                unverified_net_reason_counts["ENTRY_FEE_NOT_VERIFIED"] += 1
            elif (
                entry_fee_status == "LEDGER_VERIFIED"
                and entry_fee_currency
                and entry_quote_asset
                and canonical_asset(entry_fee_currency) != canonical_asset(entry_quote_asset)
            ):
                unverified_net_reason_counts["ENTRY_FEE_NOT_QUOTE_DENOMINATED"] += 1

            exit_fee_status = str(detail.get("exit_fee_attribution_status") or "")
            exit_fee_currency = str(detail.get("exit_fee_currency") or "")
            exit_quote_asset = str(detail.get("exit_quote_asset") or "")
            if exit_fee_status not in {"LEDGER_VERIFIED", "ZERO_FEE_REPORTED"}:
                unverified_net_reason_counts["EXIT_FEE_NOT_VERIFIED"] += 1
            elif (
                exit_fee_status == "LEDGER_VERIFIED"
                and exit_fee_currency
                and exit_quote_asset
                and canonical_asset(exit_fee_currency) != canonical_asset(exit_quote_asset)
            ):
                unverified_net_reason_counts["EXIT_FEE_NOT_QUOTE_DENOMINATED"] += 1
            if not bool(detail.get("quote_asset_matches", False)):
                unverified_net_reason_counts["ENTRY_EXIT_QUOTE_ASSET_MISMATCH"] += 1
            leveraged = (
                bool(detail.get("entry_margin"))
                or bool(detail.get("exit_margin"))
                or str(detail.get("entry_leverage") or "1") != "1"
                or str(detail.get("exit_leverage") or "1") != "1"
            )
            if leveraged and not (
                bool(detail.get("entry_margin_ledger_verified"))
                and bool(detail.get("exit_margin_ledger_verified"))
            ):
                unverified_net_reason_counts["MARGIN_LEDGER_NOT_VERIFIED"] += 1
            if leveraged and not bool(detail.get("rollover_free_window", False)):
                unverified_net_reason_counts["MARGIN_ROLLOVER_COST_NOT_VERIFIED"] += 1

        return {
            "status": "REBUILT",
            "processed_fills": processed,
            "realized_legs": len(legs),
            "realized_decisions": len(outcome_rows),
            "open_inventory_lots": len(open_lots),
            "attribution_anomalies": len(anomalies),
            "attribution_anomaly_reason_counts": dict(sorted(anomaly_reason_counts.items())),
            "unverified_net_reason_counts": dict(sorted(unverified_net_reason_counts.items())),
            "fee_status": self.FEE_STATUS,
            "verified_net_outcomes": sum(1 for item in outcome_rows if int(item["net_verified"]) == 1),
            "estimated_only_outcomes": sum(1 for item in outcome_rows if int(item["net_verified"]) != 1),
        }
