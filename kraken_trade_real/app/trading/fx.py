from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from app.domain.models import new_client_order_id
from app.domain.states import OrderState

D = Decimal


class FXConversionManager:
    """Convert cash into the quote currency before a dependent spot trade."""

    FX_MAX_WAIT_SECONDS = 30.0
    FX_POLL_SECONDS = 1.0
    DEFAULT_FX_COST_BPS = D("40")

    def __init__(self, config: Any, db: Any, audit: Any, gateway: Any,
                 portfolio: Any, instruments: list[Any] | None = None) -> None:
        self.config = config
        self.db = db
        self.audit = audit
        self.gateway = gateway
        self.portfolio = portfolio
        self.instruments = instruments or []

    def set_instruments(self, instruments: list[Any]) -> None:
        self.instruments = instruments

    def _fx_instrument(self) -> Any | None:
        for i in self.instruments:
            symbol = str(getattr(i, "symbol", "")).upper()
            base = str(getattr(i, "base", "")).upper()
            quote = str(getattr(i, "quote", "")).upper()
            if (
                getattr(i, "venue", "") == "spot"
                and getattr(i, "tradeable", False)
                and (
                    symbol in {"EUR/USD", "EURUSD", "ZEURZUSD"}
                    or (base in {"EUR", "ZEUR"} and quote in {"USD", "ZUSD"})
                )
            ):
                return i
        return None

    def _fx_cost_bps(self) -> D:
        try:
            return max(D("0"), D(str(getattr(
                self.config, "execution_fx_cost_bps", self.DEFAULT_FX_COST_BPS
            ))))
        except Exception:
            return self.DEFAULT_FX_COST_BPS

    def _ask(self, instrument: Any) -> D:
        raw = self.portfolio._ticker_raw(instrument)
        if not isinstance(raw, dict):
            return D("0")
        value = raw.get("a")
        if isinstance(value, list):
            value = value[0] if value else "0"
        try:
            return D(str(value or "0"))
        except Exception:
            return D("0")

    def ensure_quote_funds(
        self, *, quote: str, required_quote: D, cycle_id: str,
        source_preference: str = "EUR"
    ) -> dict[str, Any]:
        target = str(quote).upper().strip()
        if target in {"EUR", "ZEUR"}:
            return {"ready": True, "converted": False, "reason": "EUR_FUNDS_AVAILABLE"}

        available = self.portfolio.cash_balance(target)
        if available >= required_quote:
            return {"ready": True, "converted": False, "reason": "QUOTE_FUNDS_AVAILABLE",
                    "available_quote": str(available)}

        if target not in {"USD", "ZUSD"} or source_preference.upper() != "EUR":
            return {"ready": False, "reason": "FX_ROUTE_UNAVAILABLE",
                    "quote": target, "available_quote": str(available),
                    "required_quote": str(required_quote)}

        fx = self._fx_instrument()
        if fx is None:
            return {"ready": False, "reason": "EUR_USD_INSTRUMENT_UNAVAILABLE"}

        source_available = self.portfolio.cash_balance("EUR")
        missing = max(D("0"), required_quote - available)
        ask = self._ask(fx)
        if ask <= 0:
            return {"ready": False, "reason": "EUR_USD_PRICE_UNAVAILABLE"}

        cost_bps = self._fx_cost_bps()
        target_usd = missing * (D("1") + cost_bps / D("10000"))
        eur_required = target_usd / ask
        if source_available < eur_required:
            return {"ready": False, "reason": "FX_SOURCE_FUNDS_UNAVAILABLE",
                    "available_eur": str(source_available),
                    "required_eur": str(eur_required),
                    "missing_usd": str(missing),
                    "fx_cost_reserve_bps": str(cost_bps)}

        client_order_id = new_client_order_id()
        self.audit.emit(
            "FX_CONVERSION_START", "INFO", cycle_id=cycle_id, symbol=fx.symbol,
            direction="EUR_TO_USD", source_asset="EUR", target_asset="USD",
            source_amount=str(eur_required), target_amount=str(target_usd),
            expected_cost_bps=str(cost_bps), client_order_id=client_order_id,
        )
        try:
            # Kraken's volume for EUR/USD is EUR (the base asset).
            response = self.gateway.submit_spot_order(
                instrument_id=fx.instrument_id, side="buy", order_type="market",
                quantity=eur_required, price=None, client_order_id=client_order_id,
                leverage=D("1"), margin=False, reduce_only=False, post_only=False,
            )
            txids = response.get("txid", []) if isinstance(response, dict) else []
            if not txids:
                return {"ready": False, "reason": "FX_NO_ORDER_ID", "response": response}
            deadline = time.monotonic() + self.FX_MAX_WAIT_SECONDS
            final_state = OrderState.ACKNOWLEDGED
            while time.monotonic() < deadline:
                found = self.gateway.lookup_order(
                    client_order_id=client_order_id, instrument=fx
                )
                if found:
                    raw = found[0]
                    status = str(raw.get("status") or raw.get("state") or "").lower()
                    requested = D(str(raw.get("vol") or "0"))
                    executed = D(str(raw.get("vol_exec") or "0"))
                    if status == "closed" and executed > 0:
                        final_state = (
                            OrderState.PARTIALLY_FILLED
                            if requested > 0 and executed < requested
                            else OrderState.FILLED
                        )
                        break
                    if status in {"canceled", "expired", "rejected"}:
                        final_state = OrderState.REJECTED
                        break
                time.sleep(self.FX_POLL_SECONDS)

            if final_state not in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
                self.audit.emit(
                    "FX_CONVERSION_FAILED", "ERROR", cycle_id=cycle_id,
                    symbol=fx.symbol, client_order_id=client_order_id,
                    state=final_state.value, reason="FX_ORDER_NOT_FILLED",
                )
                return {"ready": False, "reason": "FX_ORDER_NOT_FILLED",
                        "state": final_state.value, "client_order_id": client_order_id}

            self.audit.emit(
                "FX_CONVERSION_FILLED", "INFO", cycle_id=cycle_id,
                symbol=fx.symbol, client_order_id=client_order_id,
                state=final_state.value, expected_cost_bps=str(cost_bps),
            )
            return {"ready": True, "converted": True,
                    "reason": "FX_CONVERSION_FILLED", "state": final_state.value,
                    "client_order_id": client_order_id,
                    "expected_cost_bps": str(cost_bps)}
        except Exception as exc:
            self.audit.emit(
                "FX_CONVERSION_FAILED", "ERROR", cycle_id=cycle_id,
                symbol=fx.symbol, client_order_id=client_order_id,
                error=f"{type(exc).__name__}:{str(exc)[:500]}",
            )
            return {"ready": False, "reason": "FX_CONVERSION_ERROR",
                    "error": f"{type(exc).__name__}:{str(exc)[:500]}"}
