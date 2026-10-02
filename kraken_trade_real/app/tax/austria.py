from __future__ import annotations

from decimal import Decimal
from typing import Iterable


class AustrianTaxLedger:
    """Bookkeeping helper, not tax advice. Keeps realized trade data auditable."""

    def realized_crypto(self, fills: Iterable[dict]) -> Decimal:
        total=Decimal("0")
        for fill in fills:
            if str(fill.get("asset_class","crypto")).lower()!="crypto":
                continue
            pnl=fill.get("realized_pnl_eur")
            if pnl is not None:
                total += Decimal(str(pnl))
        return total

    def export_rows(self, fills: Iterable[dict]) -> list[dict]:
        rows=[]
        for fill in fills:
            rows.append({
                "timestamp":fill.get("timestamp"),
                "symbol":fill.get("symbol"),
                "side":fill.get("side"),
                "quantity":fill.get("quantity"),
                "price":fill.get("price"),
                "fee":fill.get("fee"),
                "realized_pnl_eur":fill.get("realized_pnl_eur"),
                "asset_class":fill.get("asset_class","crypto"),
            })
        return rows
