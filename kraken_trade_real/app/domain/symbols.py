from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def canonical_asset(value: Any) -> str:
    """Normalize Kraken asset aliases to one canonical asset code."""
    asset = str(value or "").upper().strip()
    # Kraken's legacy X/Z prefixes are not a general naming rule. Strip them
    # only through the known Asset Info aliases; a real ticker such as ZBCN
    # or ZETA must remain intact rather than silently becoming BCN/ETA.
    aliases = {
        "XBT": "BTC", "XXBT": "BTC",
        "XETH": "ETH", "XXETH": "ETH",
        "XETC": "ETC", "XLTC": "LTC", "XMLN": "MLN", "XREP": "REP",
        "XXDG": "DOGE", "XDG": "DOGE", "XXLM": "XLM", "XXMR": "XMR",
        "XXRP": "XRP", "XZEC": "ZEC",
        "ZAUD": "AUD", "ZCAD": "CAD", "ZEUR": "EUR", "ZGBP": "GBP",
        "ZJPY": "JPY", "ZUSD": "USD", "ZCHF": "CHF", "ZNZD": "NZD",
    }
    return aliases.get(asset, asset)


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
