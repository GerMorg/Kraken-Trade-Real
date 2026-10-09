from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from app.domain.models import OrderIntent, new_client_order_id, quantize_order_quantity
from app.domain.states import Direction, OrderState

D = Decimal


def canonical_asset(value: Any) -> str:
    asset = str(value or "").upper().strip()
    aliases = {"XBT":"BTC","XXBT":"BTC","XETH":"ETH","XXETH":"ETH",
               "ZUSD":"USD","ZEUR":"EUR","ZGBP":"GBP","ZCHF":"CHF",
               "ZCAD":"CAD","ZJPY":"JPY","ZAUD":"AUD","ZNZD":"NZD"}
    return aliases.get(asset, asset[2:] if asset.startswith("XX") else asset[1:] if asset.startswith(("X","Z")) and len(asset)>3 else asset)


class FXConversionManager:
    """Plan and execute dependent funding through TradingAuthority only."""

    FX_MAX_WAIT_SECONDS = 30.0
    DEFAULT_FX_COST_BPS = D("40")

    def __init__(self, config: Any, db: Any, audit: Any, authority: Any,
                 portfolio: Any, instruments: list[Any] | None = None) -> None:
        self.config = config
        self.db = db
        self.audit = audit
        self.authority = authority
        self.portfolio = portfolio
        self.instruments = instruments or []

    def set_instruments(self, instruments: list[Any]) -> None:
        self.instruments = instruments

    def _fx_cost_bps(self) -> D:
        try:
            return max(D("0"), D(str(getattr(self.config, "execution_fx_cost_bps", "40"))))
        except Exception:
            return self.DEFAULT_FX_COST_BPS

    def _position_switch_min_edge_bps(self) -> D:
        try:
            return max(D("0"), D(str(getattr(
                self.config, "execution_fx_position_switch_min_edge_bps", "250"
            ))))
        except Exception:
            return D("250")

    def _fx_pair(self) -> Any | None:
        for instrument in self.instruments:
            if getattr(instrument, "venue", "") != "spot" or not instrument.tradeable:
                continue
            if canonical_asset(instrument.base) == "EUR" and canonical_asset(instrument.quote) == "USD":
                return instrument
        return None

    def _ticker(self, instrument: Any) -> dict[str, Any] | None:
        raw = self.portfolio._ticker_raw(instrument)
        return raw if isinstance(raw, dict) else None

    def _price(self, instrument: Any, side: str) -> D:
        raw = self._ticker(instrument) or {}
        key = "a" if side.lower() == "buy" else "b"
        value = raw.get(key)
        if isinstance(value, list):
            value = value[0] if value else "0"
        try:
            return D(str(value or "0"))
        except Exception:
            return D("0")

    def _daily_count(self) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM orders WHERE submitted_at IS NOT NULL "
            "AND state != 'REJECTED' AND submitted_at>=strftime('%s','now','start of day')"
        )
        return int(row["n"]) if row else 0

    def _make_intent(self, instrument: Any, side: str, quantity: D, decision_id: str,
                     expected_edge_bps: D = D("0"), reduce_only: bool = False) -> OrderIntent:
        quantity = quantize_order_quantity(instrument, quantity)
        return OrderIntent(
            intent_id=f"fund_{new_client_order_id()}",
            client_order_id=new_client_order_id(),
            decision_id=decision_id,
            instrument=instrument,
            direction=Direction.LONG if side.lower() == "buy" else Direction.SHORT,
            side=side.lower(),
            order_type="market",
            quantity=quantity,
            limit_price=None,
            leverage=D("1"),
            margin=False,
            reduce_only=reduce_only,
            expected_edge_bps=expected_edge_bps,
            max_slippage_bps=D(str(getattr(self.config, "execution_max_slippage_bps", "40"))),
            expires_seconds=int(getattr(self.config, "execution_order_timeout_seconds", 45)),
        )

    def _execute(self, intent: OrderIntent, cycle_id: str, purpose: str) -> dict[str, Any]:
        result = self.authority.submit_funding_order(intent, timeout_seconds=self.FX_MAX_WAIT_SECONDS)
        self.audit.emit(
            "FUNDING_ORDER_RESULT",
            "INFO" if result.get("state") in {OrderState.FILLED.value, OrderState.PARTIALLY_FILLED.value} else "WARNING",
            cycle_id=cycle_id, purpose=purpose, symbol=intent.instrument.symbo    def _find_position_source(self, target: str, required: D, cycle_id: str,
                              dependent_edge_bps: D, protected_symbol: str | None) -> dict[str, Any]:
        if not bool(getattr(self.config, "execution_allow_position_funding", True)):
            return {"ready": False, "reason": "POSITION_FUNDING_DISABLED"}
        if dependent_edge_bps < self._position_switch_min_edge_bps():
            return {"ready": False, "reason": "POSITION_FUNDING_EDGE_TOO_LOW",
                    "dependent_net_edge_bps": str(dependent_edge_bps),
                    "required_min_edge_bps": str(self._position_switch_min_edge_bps())}
        balances = self.authority.gateway.spot_balance()
        fx_pair = self._fx_pair()
        fx_bid = self._price(fx_pair, "sell") if fx_pair is not None else D("0")
        fx_ask = self._price(fx_pair, "buy") if fx_pair is not None else D("0")
        fx_cost = self._fx_cost_bps()
        candidates = []
        for asset, raw_qty in (balances or {}).items():
            base = canonical_asset(asset)
            qty = D(str(raw_qty or "0"))
            if qty <= 0 or base in {"EUR", "USD"}:
                continue
            for instrument in self.instruments:
                if getattr(instrument, "venue", "") != "spot" or not instrument.tradeable:
                    continue
                if canonical_asset(instrument.base) != base:
                    continue
                if protected_symbol and instrument.symbol == protected_symbol:
                    continue
                quote = canonical_asset(instrument.quote)
                if quote not in {target, "EUR", "USD"}:
                    continue
                bid = self._price(instrument, "sell")
                if bid <= 0:
                    continue
                if quote == "USD":
                    if fx_ask <= 0:
                        continue
                    value_eur = qty * bid / fx_ask
                else:
                    value_eur = qty * bid
                candidates.append((value_eur, instrument, qty, bid, quote))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            return {"ready": False, "reason": "POSITION_FUNDING_SOURCE_UNAVAILABLE"}

        for _, instrument, balance_qty, bid, quote in candidates:
            if quote == target:
                needed_quote = required
            elif {quote, target} == {"EUR", "USD"}:
                if fx_pair is None or fx_bid <= 0 or fx_ask <= 0:
                    continue
                conversion_target = required * (D("1") + fx_cost / D("10000"))
                needed_quote = (
                    conversion_target / fx_bid
                    if target == "USD" and quote == "EUR"
                    else conversion_target * fx_ask
                )
            else:
                continue

            desired_qty = min(
                balance_qty,
                needed_quote / bid * (D("1") + fx_cost / D("10000")),
            )
            needed_qty = quantize_order_quantity(instrument, desired_qty, rounding=ROUND_DOWN)
            if needed_qty < instrument.min_order_qty or needed_qty * bid < instrument.min_cost:
                continue
            if needed_qty * bid < needed_quote:
                continue
            reserve_orders = 1 if quote == target else 2
            if self._daily_count() >= int(
                getattr(self.config, "execution_max_orders_per_day", 10)
            ) - reserve_orders:
                return {"ready": False, "reason": "DAILY_ORDER_LIMIT_FX_RESERVE"}

            result = self._execute(
                self._make_intent(
                    instrument, "sell", needed_qty, f"fund_{cycle_id}",
                    dependent_edge_bps, True,
                ),
                cycle_id, "POSITION_TO_QUOTE" if quote == target else "POSITION_TO_FX_BRIDGE",
            )
            if result.get("state") not in {OrderState.FILLED.value, OrderState.PARTIALLY_FILLED.value}:
                return {"ready": False, "reason": "POSITION_FUNDING_ORDER_NOT_FILLED", "result": result}
            if quote == target:
                return {"ready": True, "converted": True, "source": instrument.symbol,
                        "source_type": "POSITION", "result": result}
            return self._convert_cash(
                target, required, cycle_id, dependent_edge_bps,
                source_hint=quote, already_funded=True,
            )
        return {"ready": False, "reason": "POSITION_FUNDING_SOURCE_TOO_SMALL"}

int=quote, already_funded=True)
        return {"ready": False, "reason": "POSITION_FUNDING_SOURCE_TOO_SMALL"}

    def _exchange_cash_balance(self, asset: str) -> D | None:
        """Read fresh balances after a just-confirmed position liquidation."""
        try:
            balances = self.authority.gateway.spot_balance()
        except Exception:
            return None
        target = canonical_asset(asset)
        total = D("0")
        for raw_asset, raw_quantity in (balances or {}).items():
            if canonical_asset(raw_asset) == target:
                total += D(str(raw_quantity or "0"))
        return total

    def _convert_cash(self, target: str, required: D, cycle_id: str,
                       dependent_edge_bps: D, source_hint: str | None = None,
                       already_funded: bool = False) -> dict[str, Any]:
        target = canonical_asset(target)
        available = self.portfolio.cash_balance(target)
        if already_funded:
            refreshed = self._exchange_cash_balance(target)
            if refreshed is None:
                return {"ready": False, "reason": "FX_BALANCE_REFRESH_FAILED",
                        "target_asset": target}
            available = refreshed
        if available >= required:
            return {"ready": True, "converted": False, "reason": "QUOTE_FUNDS_AVAILABLE"}

        fx = self._fx_pair()
        if fx is None:
            return {"ready": False, "reason": "EUR_USD_INSTRUMENT_UNAVAILABLE"}

        source = "EUR" if target == "USD" else "USD"
        if source_hint and canonical_asset(source_hint) != source:
            return {"ready": False, "reason": "FX_SOURCE_CURRENCY_MISMATCH",
                    "expected_source": source, "source_hint": canonical_asset(source_hint)}
        if already_funded:
            refreshed_source = self._exchange_cash_balance(source)
            if refreshed_source is None:
                return {"ready": False, "reason": "FX_BALANCE_REFRESH_FAILED",
                        "source_asset": source}
            source_available = refreshed_source
        else:
            source_available = self.portfolio.cash_balance(source)

        missing = max(D("0"), required - available)
        cost_bps = self._fx_cost_bps()
        target_with_reserve = missing * (D("1") + cost_bps / D("10000"))
        if target == "USD":
            # EUR/USD is USD per EUR; to acquire USD, sell the base EUR at bid.
            bid = self._price(fx, "sell")
            if bid <= 0:
                return {"ready": False, "reason": "EUR_USD_PRICE_UNAVAILABLE"}
            quantity = quantize_order_quantity(
                fx, target_with_reserve / bid, rounding=ROUND_UP
            )
            side = "sell"
            source_required = quantity
            reference_price = bid
        else:
            # To acquire EUR, buy the base EUR at ask using USD.
            ask = self._price(fx, "buy")
            if ask <= 0:
                return {"ready": False, "reason": "EUR_USD_PRICE_UNAVAILABLE"}
            quantity = quantize_order_quantity(
                fx, target_with_reserve, rounding=ROUND_UP
            )
            side = "buy"
            source_required = quantity * ask
            reference_price = ask

        if (
            quantity < fx.min_order_qty
            or (fx.min_cost > 0 and quantity * reference_price < fx.min_cost)
        ):
            return {"ready": False, "reason": "FX_ORDER_BELOW_EXCHANGE_MINIMUM",
                    "quantity": str(quantity), "minimum_quantity": str(fx.min_order_qty),
                    "estimated_value": str(quantity * reference_price),
                    "minimum_cost": str(fx.min_cost)}
        if source_available <= 0 or source_available < source_required:
            return {"ready": False, "reason": "FX_SOURCE_FUNDS_UNAVAILABLE",
                    "source_asset": source, "available_source": str(source_available),
                    "required_source": str(source_required)}
        if self._daily_count() >= int(getattr(self.config, "execution_max_orders_per_day", 10)) - 1:
            return {"ready": False, "reason": "DAILY_ORDER_LIMIT_FX_RESERVE"}
        result = self._execute(
            self._make_intent(fx, side, quantity, f"fx_{cycle_id}", dependent_edge_bps),
            cycle_id, "EUR_USD_CONVERSION",
        )
        if result.get("state") not in {OrderState.FILLED.value, OrderState.PARTIALLY_FILLED.value}:
            return {"ready": False, "reason": "FX_ORDER_NOT_FILLED", "result": result}
        return {"ready": True, "converted": True, "reason": "FX_CONVERSION_FILLED",
                "source_asset": source, "target_asset": target,
                "expected_cost_bps": str(cost_bps), "result": result}

    def ensure_quote_funds(self, *, quote: str, required_quote: D, cycle_id: str,
                           source_preference: str = "EUR",
                           dependent_edge_bps: D = D("0"),
                           protected_symbol: str | None = None) -> dict[str, Any]:
        target = canonical_asset(quote)
        available = self.portfolio.cash_balance(target)
        if available >= required_quote:
            return {"ready": True, "converted": False, "reason": "QUOTE_FUNDS_AVAILABLE",
                    "available_quote": str(available)}
        # Funding must leave one order slot for the dependent trade.
        if self._daily_count() >= int(getattr(self.config, "execution_max_orders_per_day", 10)) - 1:
            return {"ready": False, "reason": "DAILY_ORDER_LIMIT_FX_RESERVE"}

        # Direct cash conversion first. This is deliberately preferred to selling a position.
        if target in {"EUR", "USD"}:
            direct_source = "EUR" if target == "USD" else "USD"
            if self.portfolio.cash_balance(direct_source) > 0:
                result = self._convert_cash(target, required_quote, cycle_id, dependent_edge_bps)
                if result.get("ready"):
                    return result

        # If cash is not enough, only a clearly superior dependent trade may unlock
        # a partial liquidation of an existing spot position.
        return self._find_position_source(
            target, required_quote, cycle_id, dependent_edge_bps, protected_symbol
        )
