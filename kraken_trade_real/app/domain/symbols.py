from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def canonical_asset(value: Any) -> str:
    """Normalize Kraken asset aliases to one canonical asset code."""
    asset = str(value or "").upper().strip()
    aliases = {
        "XBT": "BTC", "XXBT": "BTC", "XETH": "ETH", "XXETH": "ETH",
        "ZUSD": "USD", "ZEUR": "EUR", "ZGBP": "GBP", "ZCHF": "CHF",
        "ZCAD": "CAD", "ZJPY": "JPY", "ZAUD": "AUD", "ZNZD": "NZD",
    }
    if asset in aliases:
        return aliases[asset]
    if asset.startswith("XX") and len(asset) > 2:
        return asset[2:]
    if asset.startswith(("X", "Z")) and len(asset) > 3:
        return asset[1:]
    return asset


def normalized_symbol(value: Any) -> str:
    """Normalize exchange symbol aliases without guessing base/quote semantics."""
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def instrument_matches_symbol(instrument: Any, symbol: str) -> bool:
    """Match only explicit Kraken symbol, instrument-id, or altname aliases."""
    aliases = (
        getattr(instrument, "symbol", ""),
        getattr(instrument, "instrument_id", ""),
        getattr(instrument, "altname", ""),
    )
    return str(symbol) in aliases or normalized_symbol(symbol) in {
        normalized_symbol(alias) for alias in aliases if alias
    }


def resolve_instrument_symbol(symbol: str, instruments: Iterable[Any]) -> Any | None:
    """Resolve a Kraken position alias to its discovered canonical instrument."""
    return next(
        (instrument for instrument in instruments if instrument_matches_symbol(instrument, symbol)),
        None,
    )
