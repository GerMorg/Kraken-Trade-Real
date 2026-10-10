from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from typing import Any


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
    """Rebuild an auditable FIFO attribution ledger from app-managed Spot fills.

    The reported TradesHistory fee is retained as an estimated quote-currency
    commission. Outcomes are explicitly marked as not fee-verified and therefore
    must not be used as verified net-PnL training labels.
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
            if not opening_decision_id:
                anomaly(row, "MISSING_LOCAL_DECISION_ID", {})
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
                    "quote_asset": quote_asset,
                    "leverage": str(row.get("leverage") or "1"),
                    "margin": bool(int(row.get("margin") or 0)),
                })
                continue

            closing_direction = "LONG" if direction == "SHORT" else "SHORT"
            open_lots = lots_by_key[(symbol, closing_direction)]
            remaining_close_qty = quantity
            exit_fee_remaining = fee_est
            for lot in open_lots:
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
                gross_bps = gross_pnl / entry_notional * BPS if entry_notional > 0 else ZERO
                net_bps = estimated_net / entry_notional * BPS if entry_notional > 0 else ZERO
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
                    "fee_status": self.FEE_STATUS,
                    "detail_json": json.dumps({
                        "entry_fee_currency_field": "UNVERIFIED",
                        "exit_fee_currency_field": row.get("fee_currency"),
                        "net_verified": False,
                        "quote_asset_matches": lot["quote_asset"] == quote_asset,
                        "entry_leverage": lot["leverage"],
                        "entry_margin": lot["margin"],
                        "entry_client_order_id": lot["client_order_id"],
                        "close_client_order_id": client_order_id,
                    }, sort_keys=True, default=str),
                })
                lot["remaining_quantity"] = lot_qty_before - matched_qty
                lot["remaining_entry_fee_est_quote"] -= allocated_entry_fee
                remaining_close_qty -= matched_qty
                exit_fee_remaining -= allocated_exit_fee
            if remaining_close_qty > 0:
                anomaly(row, "REDUCE_ONLY_EXCEEDS_LOCAL_INVENTORY", {
                    "close_direction": closing_direction,
                    "fill_quantity": str(quantity),
                    "matched_quantity": str(quantity - remaining_close_qty),
                    "unmatched_quantity": str(remaining_close_qty),
                })
            lots_by_key[(symbol, closing_direction)] = [
                lot for lot in open_lots if lot["remaining_quantity"] > 0
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
                "net_verified": 0,
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
            out["detail"].append(leg["leg_id"])

        outcome_rows: list[dict[str, Any]] = []
        for out in outcomes_by_key.values():
            notional = out["closed_notional_quote"]
            gross_bps = out["gross_pnl_quote"] / notional * BPS if notional > 0 else ZERO
            net_bps = out["estimated_net_pnl_quote"] / notional * BPS if notional > 0 else ZERO
            outcome_rows.append({
                **out,
                "gross_return_bps": str(gross_bps),
                "estimated_net_return_bps": str(net_bps),
                "closed_quantity": str(out["closed_quantity"]),
                "closed_notional_quote": str(notional),
                "gross_pnl_quote": str(out["gross_pnl_quote"]),
                "fees_est_quote": str(out["fees_est_quote"]),
                "estimated_net_pnl_quote": str(out["estimated_net_pnl_quote"]),
                "detail_json": json.dumps({
                    "leg_ids": out["detail"],
                    "net_verified": False,
                    "attribution_basis": "app-local-orders-and-Spot-TradesHistory",
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
                    "quote_asset": lot["quote_asset"],
                    "detail_json": json.dumps({
                        "leverage": lot["leverage"],
                        "margin": lot["margin"],
                        "client_order_id": lot["client_order_id"],
                        "fee_status": self.FEE_STATUS,
                    }, sort_keys=True),
                })

        leg_columns = (
            "leg_id,symbol,venue,direction,opened_at,closed_at,entry_order_id,entry_trade_id,"
            "close_order_id,close_trade_id,opening_decision_id,closing_decision_id,quantity,"
            "entry_price,exit_price,entry_notional_quote,gross_pnl_quote,entry_fee_est_quote,"
            "exit_fee_est_quote,estimated_net_pnl_quote,quote_asset,gross_return_bps,"
            "estimated_net_return_bps,fee_status,detail_json"
        )
        outcome_columns = (
            "opening_decision_id,symbol,direction,first_opened_at,last_closed_at,matched_legs,"
            "closed_quantity,closed_notional_quote,gross_pnl_quote,fees_est_quote,"
            "estimated_net_pnl_quote,gross_return_bps,estimated_net_return_bps,quote_asset,"
            "fee_status,net_verified,detail_json"
        )
        lot_columns = (
            "lot_id,symbol,direction,opened_at,entry_order_id,entry_trade_id,opening_decision_id,"
            "entry_price,remaining_quantity,remaining_entry_fee_est_quote,quote_asset,detail_json"
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

        return {
            "status": "REBUILT",
            "processed_fills": processed,
            "realized_legs": len(legs),
            "realized_decisions": len(outcome_rows),
            "open_inventory_lots": len(open_lots),
            "attribution_anomalies": len(anomalies),
            "fee_status": self.FEE_STATUS,
            "verified_net_outcomes": 0,
        }
