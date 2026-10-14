from __future__ import annotations

import math
from typing import Any


class BacktestEngine:
    """Conservative quote/depth-aware simulator for offline strategy evaluation.

    Each observation contains a signal generated at decision_timestamp and an
    executable bid/ask snapshot at a strictly later execution_timestamp. Signal
    values are target positions (-1 short, 0 flat, +1 long). No broker is called.
    Fees, spread, slippage, depth caps, minimum order rules and financing are
    explicit inputs. This simulator is a research baseline, not proof of fills.
    """

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
