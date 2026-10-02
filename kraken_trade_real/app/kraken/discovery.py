from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import Instrument
from app.domain.states import ProductType


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _symbol(item: dict[str, Any], derivative: bool = False) -> str:
    for key in ("symbol", "wsname", "altname", "pair"):
        if item.get(key):
            return str(item[key])
    return ""


class InstrumentDiscovery:
    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    def discover(self) -> list[Instrument]:
        spot, futures = self.gateway.public_instruments()
        result = self._spot(spot)
        result.extend(self._futures(futures))
        unique: dict[tuple[str, str], Instrument] = {}
        for instrument in result:
            unique[(instrument.venue, instrument.instrument_id)] = instrument
        return list(unique.values())

    def _spot(self, payload: dict[str, Any]) -> list[Instrument]:
        rows = payload if isinstance(payload, dict) else {}
        found: list[Instrument] = []
        for item_id, raw in rows.items():
            if not isinstance(raw, dict):
                continue
            symbol = _symbol(raw)
            status = str(raw.get("status") or "")
            base = str(raw.get("base") or "")
            quote = str(raw.get("quote") or "")
            if not symbol or not base or not quote:
                continue
            leverage = tuple(
                sorted({_d(x, "1") for x in (raw.get("leverage_buy") or raw.get("leverage_sell") or [1])})
            )
            margin_available = bool(raw.get("leverage_buy") or raw.get("leverage_sell"))
            found.append(
                Instrument(
                    venue="spot",
                    product_type=ProductType.SPOT_MARGIN if margin_available else ProductType.SPOT,
                    symbol=symbol,
                    instrument_id=str(item_id),
                    altname=str(raw.get("altname") or symbol),
                    base=base,
                    quote=quote,
                    status=status,
                    margin_available=margin_available,
                    long_available=True,
                    short_available=margin_available,
                    leverage_levels=leverage or (Decimal("1"),),
                    min_order_qty=_d(raw.get("ordermin")),
                    min_cost=_d(raw.get("costmin")),
                    lot_decimals=int(raw.get("lot_decimals") or 8),
                    price_decimals=int(raw.get("pair_decimals") or 8),
                    tick_size=Decimal("1").scaleb(-int(raw.get("pair_decimals") or 8)),
                    margin_class="spot-margin" if margin_available else "spot",
                    metadata=dict(raw),
                )
            )
        return found

    def _futures(self, payload: dict[str, Any]) -> list[Instrument]:
        rows = payload.get("instruments") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []
        found: list[Instrument] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            symbol = _symbol(raw, derivative=True)
            status = str(raw.get("tradeable") or raw.get("status") or "active")
            if not symbol:
                continue
            long_ok = bool(raw.get("type") in (None, "futures", "perpetual", "flexible"))
            short_ok = bool(raw.get("type") in (None, "futures", "perpetual", "flexible"))
            max_lev = _d(raw.get("maxLeverage") or raw.get("max_leverage"), "1")
            lev_levels = tuple(
                Decimal(i) for i in range(1, int(max(1, min(20, max_lev))) + 1)
            )
            base = str(raw.get("underlying") or raw.get("base") or "")
            quote = str(raw.get("quoteCurrency") or raw.get("quote") or "USD")
            found.append(
                Instrument(
                    venue="futures",
                    product_type=ProductType.DERIVATIVE,
                    symbol=symbol,
                    instrument_id=str(raw.get("symbol") or symbol),
                    altname=str(raw.get("symbol") or symbol),
                    base=base,
                    quote=quote,
                    status=status,
                    margin_available=True,
                    long_available=long_ok,
                    short_available=short_ok,
                    leverage_levels=lev_levels,
                    min_order_qty=_d(raw.get("contractSize") or raw.get("minOrderSize")),
                    min_cost=_d(raw.get("minOrderSize") or "0"),
                    lot_decimals=int(raw.get("contractSize") and 8 or 8),
                    price_decimals=int(raw.get("tickSize") and 8 or 8),
                    tick_size=_d(raw.get("tickSize"), "0.00000001"),
                    margin_class=str(raw.get("marginCurrency") or quote),
                    metadata=dict(raw),
                )
            )
        return found
