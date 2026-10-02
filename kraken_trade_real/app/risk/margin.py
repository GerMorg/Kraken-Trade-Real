from __future__ import annotations

from decimal import Decimal


D=Decimal


class MarginEngine:
    IMMUTABLE_MIN_MARGIN_LEVEL_PCT=D("200")
    IMMUTABLE_MAX_LEVERAGE=D("5")

    def available(self, account: dict, requested_margin: D) -> tuple[bool,str]:
        if requested_margin<=0:
            return True,"NO_MARGIN_REQUIRED"
        free=D(str(account.get("free_margin") or 0))
        level=D(str(account.get("margin_level_pct") or 9999))
        if free < requested_margin:
            return False,"INSUFFICIENT_FREE_MARGIN"
        if level < self.IMMUTABLE_MIN_MARGIN_LEVEL_PCT:
            return False,"MARGIN_LEVEL_TOO_LOW"
        return True,"MARGIN_OK"

    def validate_leverage(self, leverage: D, instrument_max: D, configured_max: D) -> tuple[bool,str]:
        effective=min(instrument_max,configured_max,self.IMMUTABLE_MAX_LEVERAGE)
        if leverage < 1:
            return False,"LEVERAGE_BELOW_1"
        if leverage > effective:
            return False,"LEVERAGE_EXCEEDS_EFFECTIVE_MAX"
        return True,"LEVERAGE_OK"
