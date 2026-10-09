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
            buy_levels = tuple(_d(x, "1") for x in (raw.get("leverage_buy") or []))
            sell_levels = tuple(_d(x, "1") for x in (raw.get("leverage_sell") or []))
            leverage = tuple(sorted(set(buy_levels + sell_levels)))
            margin_available = bool(buy_levels or sell_levels)
            pair_decimals_raw = raw.get("pair_decimals")
            pair_decimals = max(
                0, min(18, int(pair_decimals_raw if pair_decimals_raw is not None else 8))
            )
            lot_decimals_raw = raw.get("lot_decimals")
            lot_decimals = max(
                0, min(18, int(lot_decimals_raw if lot_decimals_raw is not None else 8))
            )
            metadata = dict(raw)
            if metadata.get("aclass_base") == "tokenized_asset":
                metadata["asset_class"] = "tokenized_asset"
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
                    short_available=bool(sell_levels),
                    leverage_levels=leverage or (Decimal("1"),),
                    min_order_qty=_d(raw.get("ordermin")),
                    min_cost=_d(raw.get("costmin")),
                    lot_decimals=lot_decimals,
                    price_decimals=pair_decimals,
                    tick_size=Decimal("1").scaleb(-pair_decimals),
                    margin_class="spot-margin" if margin_available else "spot",
                    metadata=metadata,
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
            tradeable_raw = raw.get("tradeable")
            if isinstance(tradeable_raw, bool):
                status = "online" if tradeable_raw else "offline"
            else:
                status = str(tradeable_raw or raw.get("status") or "active")
            if not symbol:
                continue
            future_type = str(raw.get("type") or "").lower()
            derivative_type = (
                "futures" in future_type
                or future_type in {"perpetual", "flexible", "futures"}
            )
            long_ok = bool(raw.get("tradeable")) and derivative_type
            short_ok = bool(raw.get("tradeable")) and derivative_type
            max_lev = _d(raw.get("maxLeverage") or raw.get("max_leverage"), "0")
            if max_lev <= 1:
                margin_levels = raw.get("retailMarginLevels") or raw.get("marginLevels") or []
                first_level = (
                    margin_levels[0]
                    if isinstance(margin_levels, list)
                    and margin_levels
                    and isinstance(margin_levels[0], dict)
                    else {}
                )
                initial_margin = _d(first_level.get("initialMargin"), "0")
                if initial_margin > 0:
                    max_lev = Decimal("1") / initial_margin
            max_lev_int = max(1, min(20, int(max_lev)))
            lev_levels = tuple(Decimal(i) for i in range(1, max_lev_int + 1))
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
                    long_available=bool(long_ok),
                    short_available=bool(short_ok),
                    leverage_levels=lev_levels,
                    min_order_qty=_d(raw.get("minOrderSize") or raw.get("contractSize")),
                    min_cost=_d(raw.get("minOrderSize") or "0"),
                    lot_decimals=8,
                    price_decimals=8,
                    tick_size=_d(raw.get("tickSize"), "0.00000001"),
                    margin_class=str(raw.get("marginCurrency") or quote),
                    metadata=dict(raw),
                )
            )
        return found
