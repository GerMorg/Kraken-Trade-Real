from __future__ import annotations

import json
import math
from typing import Any, Callable


class BacktestEngine:
    """Conservative quote/depth-aware simulator for offline strategy evaluation.

    Each observation contains a signal generated at decision_timestamp and an
    executable bid/ask snapshot at a strictly later execution_timestamp. Signal
    values are target positions (-1 short, 0 flat, +1 long). No broker is called.
    Fees, spread, slippage, depth caps, minimum order rules and financing are
    explicit inputs. This simulator is a research baseline, not proof of fills.
    """

    def run_from_database(
        self,
        db: Any,
        symbol: str,
        *,
        since: float,
        until: float,
        signal_provider: Callable[[list[dict[str, Any]]], int],
        min_order_qty: float,
        min_order_cost_quote: float = 0.0,
        **run_kwargs: Any,
    ) -> dict[str, Any]:
        """Backtest one symbol from persisted market_snapshots with next-quote fills.

        The signal callback receives only snapshots at or before decision time.
        Execution uses the next stored bid/ask and recorded book-depth levels.
        Instrument minima must be supplied from the instrument metadata valid for
        the tested date; this method deliberately does not guess historical rules.
        Returns are in the instrument's quote currency, not EUR unless quote=EUR.
        """
        try:
            since_value, until_value = float(since), float(until)
            minimum_quantity = float(min_order_qty)
            minimum_cost = float(min_order_cost_quote)
        except (TypeError, ValueError):
            return {"status": "INVALID_DATASET_PARAMETERS", "fills": [], "metrics": {}}
        if (
            not all(math.isfinite(v) for v in (
                since_value, until_value, minimum_quantity, minimum_cost
            ))
            or since_value >= until_value or minimum_quantity < 0 or minimum_cost < 0
        ):
            return {"status": "INVALID_DATASET_PARAMETERS", "fills": [], "metrics": {}}

        rows = db.query(
            """SELECT captured_at,symbol,price,bid,ask,volume_24h,age_seconds,
                      spread_bps,closes_json,depths_bid_json,depths_ask_json,
                      funding_rate,open_interest,basis_bps,liquidation_pressure,
                      features_json
               FROM market_snapshots
               WHERE symbol=? AND captured_at>=? AND captured_at<=?
               ORDER BY captured_at,id""",
            (str(symbol), since_value, until_value),
        )
        if len(rows) < 2:
            return {
                "status": "INSUFFICIENT_HISTORICAL_QUOTES",
                "fills": [],
                "metrics": {},
                "symbol": symbol,
                "snapshots": len(rows),
            }

        normalized: list[dict[str, Any]] = []
        try:
            for row in rows:
                timestamp = float(row["captured_at"])
                bid, ask = float(row["bid"]), float(row["ask"])
                if not all(math.isfinite(v) for v in (timestamp, bid, ask)):
                    raise ValueError("NON_FINITE_HISTORICAL_QUOTE")
                if timestamp <= 0 or bid <= 0 or ask < bid:
                    raise ValueError("INVALID_HISTORICAL_QUOTE")
                raw_bid_depth = json.loads(row.get("depths_bid_json") or "[]")
                raw_ask_depth = json.loads(row.get("depths_ask_json") or "[]")
                bid_depth = sum(
                    float(level[1]) for level in raw_bid_depth
                    if isinstance(level, (list, tuple)) and len(level) >= 2
                )
                ask_depth = sum(
                    float(level[1]) for level in raw_ask_depth
                    if isinstance(level, (list, tuple)) and len(level) >= 2
                )
                features = json.loads(row.get("features_json") or "{}")
                closes = json.loads(row.get("closes_json") or "[]")
                if (
                    not math.isfinite(bid_depth) or not math.isfinite(ask_depth)
                    or bid_depth < 0 or ask_depth < 0
                    or not isinstance(features, dict) or not isinstance(closes, list)
                ):
                    raise ValueError("INVALID_HISTORICAL_DEPTH_OR_FEATURES")
                normalized.append({
                    **row,
                    "captured_at": timestamp,
                    "bid": bid,
                    "ask": ask,
                    "price": float(row["price"]),
                    "bid_depth_base": bid_depth,
                    "ask_depth_base": ask_depth,
                    "features": features,
                    "closes": closes,
                })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OverflowError):
            return {
                "status": "INVALID_HISTORICAL_SNAPSHOT",
                "fills": [],
                "metrics": {},
                "symbol": symbol,
            }

        observations: list[dict[str, Any]] = []
        try:
            for index in range(len(normalized) - 1):
                history = normalized[: index + 1]
                signal = signal_provider(history)
                decision_row = normalized[index]
                execution_row = normalized[index + 1]
                observations.append({
                    "decision_timestamp": decision_row["captured_at"],
                    "execution_timestamp": execution_row["captured_at"],
                    "signal": signal,
                    "bid": execution_row["bid"],
                    "ask": execution_row["ask"],
                    "bid_depth_base": execution_row["bid_depth_base"],
                    "ask_depth_base": execution_row["ask_depth_base"],
                    "min_order_qty": minimum_quantity,
                    "min_order_cost_quote": minimum_cost,
                })
        except Exception as exc:
            return {
                "status": "SIGNAL_PROVIDER_FAILED",
                "fills": [],
                "metrics": {},
                "symbol": symbol,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            }

        kwargs = dict(run_kwargs)
        kwargs.setdefault("max_quote_age_seconds", 300.0)
        result = self.run(observations, **kwargs)
        result["symbol"] = symbol
        result["snapshot_count"] = len(normalized)
        result["decision_count"] = len(observations)
        result["dataset"] = {
            "source": "market_snapshots",
            "from_timestamp": normalized[0]["captured_at"],
            "to_timestamp": normalized[-1]["captured_at"],
            "instrument": symbol,
            "automatic_promotion": False,
        }
        return result

    def run(
        self,
        observations: list[dict[str, Any]],
        *,
        initial_equity_quote: float = 10_000.0,
        target_notional_quote: float = 1_000.0,
        taker_fee_bps: float = 8.0,
        slippage_bps: float = 3.0,
        impact_coefficient_bps: float = 15.0,
        max_depth_fraction: float = 0.10,
        financing_bps_per_bar: float = 0.0,
        max_quote_age_seconds: float = 30.0,
    ) -> dict[str, Any]:
        params = (
            initial_equity_quote, target_notional_quote, taker_fee_bps,
            slippage_bps, impact_coefficient_bps, max_depth_fraction,
            financing_bps_per_bar, max_quote_age_seconds,
        )
        if any(not math.isfinite(float(v)) for v in params):
            return {"status": "INVALID_PARAMETERS", "fills": [], "metrics": {}}
        if (
            initial_equity_quote <= 0 or target_notional_quote <= 0
            or min(taker_fee_bps, slippage_bps, impact_coefficient_bps, financing_bps_per_bar) < 0
            or not 0 < max_depth_fraction <= 1
            or max_quote_age_seconds <= 0
        ):
            return {"status": "INVALID_PARAMETERS", "fills": [], "metrics": {}}

        rows = list(observations)
        try:
            execution_times = [float(row["execution_timestamp"]) for row in rows]
            decision_times = [float(row["decision_timestamp"]) for row in rows]
        except (KeyError, TypeError, ValueError):
            return {"status": "INVALID_TIMESTAMPS", "fills": [], "metrics": {}}
        if any(not math.isfinite(v) for v in execution_times + decision_times):
            return {"status": "INVALID_TIMESTAMPS", "fills": [], "metrics": {}}
        if any(execution_times[i] <= decision_times[i] for i in range(len(rows))):
            return {"status": "LOOKAHEAD_OR_NONCAUSAL_QUOTE", "fills": [], "metrics": {}}
        if any(execution_times[i] > execution_times[i + 1] for i in range(len(rows) - 1)):
            return {"status": "NON_CHRONOLOGICAL_EXECUTION_DATA", "fills": [], "metrics": {}}

        cash = float(initial_equity_quote)
        position = 0.0
        peak_equity = cash
        equity_curve: list[float] = []
        net_returns: list[float] = []
        fills: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        total_fees = 0.0
        total_slippage_cost = 0.0
        total_financing = 0.0
        previous_equity = cash

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for index, row in enumerate(rows):
            signal = row.get("signal")
            if isinstance(signal, bool) or signal not in (-1, 0, 1):
                skip("INVALID_SIGNAL")
                continue
            try:
                decision_ts = float(row["decision_timestamp"])
                execution_ts = float(row["execution_timestamp"])
                bid = float(row["bid"])
                ask = float(row["ask"])
                bid_depth = float(row["bid_depth_base"])
                ask_depth = float(row["ask_depth_base"])
                min_qty = float(row.get("min_order_qty", 0.0))
                min_cost = float(row.get("min_order_cost_quote", 0.0))
            except (KeyError, TypeError, ValueError):
                skip("MISSING_EXECUTION_FIELDS")
                continue
            quote_age = execution_ts - decision_ts
            if (
                not all(math.isfinite(v) for v in (
                    bid, ask, bid_depth, ask_depth, min_qty, min_cost, quote_age
                ))
                or bid <= 0 or ask < bid or bid_depth < 0 or ask_depth < 0
                or min_qty < 0 or min_cost < 0
            ):
                skip("INVALID_EXECUTION_QUOTE")
                continue
            if quote_age > max_quote_age_seconds:
                skip("STALE_EXECUTION_QUOTE")
                continue

            mid = (bid + ask) / 2.0
            current_equity = cash + position * mid
            if current_equity <= 0 or not math.isfinite(current_equity):
                return {
                    "status": "EQUITY_BREACH", "fills": fills, "metrics": {},
                    "skipped": skipped,
                }
            desired_position = float(signal) * min(
                target_notional_quote, current_equity * 0.95
            ) / mid
            order_delta = desired_position - position
            if abs(order_delta) > 1e-12:
                side = "buy" if order_delta > 0 else "sell"
                available_depth = ask_depth if side == "buy" else bid_depth
                fill_quantity = min(
                    abs(order_delta), available_depth * max_depth_fraction
                )
                if fill_quantity <= 0:
                    skip("NO_EXECUTABLE_DEPTH")
                elif (
                    fill_quantity < min_qty
                    or fill_quantity * (ask if side == "buy" else bid) < min_cost
                ):
                    skip("BELOW_EXCHANGE_MINIMUM")
                else:
                    reference_price = ask if side == "buy" else bid
                    participation = min(1.0, fill_quantity / max(available_depth, 1e-12))
                    impact_bps = impact_coefficient_bps * participation
                    effective_bps = slippage_bps + impact_bps
                    execution_price = reference_price * (
                        1.0 + effective_bps / 10_000.0
                        if side == "buy"
                        else 1.0 - effective_bps / 10_000.0
                    )
                    notional = fill_quantity * execution_price
                    fee = notional * taker_fee_bps / 10_000.0
                    if side == "buy":
                        cash -= notional + fee
                        position += fill_quantity
                    else:
                        cash += notional - fee
                        position -= fill_quantity
                    total_fees += fee
                    total_slippage_cost += abs(execution_price - reference_price) * fill_quantity
                    fills.append({
                        "index": index,
                        "decision_timestamp": decision_ts,
                        "execution_timestamp": execution_ts,
                        "signal": int(signal),
                        "side": side,
                        "quantity": fill_quantity,
                        "reference_quote": reference_price,
                        "execution_price": execution_price,
                        "notional_quote": notional,
                        "fee_quote": fee,
                        "impact_bps": impact_bps,
                        "quote_age_seconds": quote_age,
                        "partial": fill_quantity + 1e-12 < abs(order_delta),
                    })

            # Financing is charged on remaining exposure once per supplied bar.
            if position != 0 and financing_bps_per_bar:
                financing = abs(position * mid) * financing_bps_per_bar / 10_000.0
                cash -= financing
                total_financing += financing
            equity = cash + position * mid
            if not math.isfinite(equity) or equity <= 0:
                return {
                    "status": "EQUITY_BREACH", "fills": fills, "metrics": {},
                    "skipped": skipped,
                }
            equity_curve.append(equity)
            net_returns.append(equity / previous_equity - 1.0)
            previous_equity = equity
            peak_equity = max(peak_equity, equity)

        from app.learning.research import ResearchEngine
        metrics = ResearchEngine.metrics(net_returns)
        metrics.update({
            "initial_equity_quote": initial_equity_quote,
            "final_equity_quote": equity_curve[-1] if equity_curve else initial_equity_quote,
            "fees_quote": total_fees,
            "slippage_and_impact_cost_quote": total_slippage_cost,
            "financing_quote": total_financing,
            "fill_count": float(len(fills)),
            "partial_fill_count": float(sum(bool(fill["partial"]) for fill in fills)),
            "skipped_observations": float(sum(skipped.values())),
            "peak_equity_quote": peak_equity,
        })
        return {
            "status": "OK" if fills else "NO_EXECUTABLE_FILLS",
            "metrics": metrics,
            "equity_curve": equity_curve,
            "fills": fills,
            "skipped": skipped,
            "assumptions": {
                "execution_model": "NEXT_TIMESTAMP_BID_ASK_WITH_DEPTH_CAP",
                "fee_model": "TAKER_RATE_APPLIED_TO_FILLED_NOTIONAL",
                "signal_values": "-1 short, 0 flat, +1 long",
                "automatic_promotion": False,
                "live_orders_placed": False,
            },
        }
