from __future__ import annotations

from hashlib import sha256
import json
from decimal import Decimal
from typing import Any, Iterable

from app.domain.models import Instrument, MarketSnapshot


D = Decimal

# Current Kraken xStocks universe. This is used only for classification/coverage
# diagnostics; actual API availability remains determined by instrument discovery.
KNOWN_XSTOCKS = frozenset({
    "AAPLX","ABBVX","ABTX","ACNX","ADBEX","AMATX","AMBRX","AMDX","AMZNX","ANETX",
    "APLDX","APPX","ASMLX","ASTSX","AVG0X","AVGOX","AZNX","BACX","BMNRX","BRK.BX",
    "BSPX","BTBTX","BTGOX","CEGX","CLSKX","CMCSAX","COINX","COPXX","CORZX","CRCLX",
    "CRMX","CRWDX","CSCOX","CVXX","DELLX","DFDVX","DHRX","ETNX","FGDLX","FLBLX",
    "FLQMX","FSMLX","GEVX","GLDX","GLXYX","GMEX","GOOGLX","GSX","HDX","HONX",
    "HOODX","HUTX","IBMX","IEMGX","IJRX","INTCX","IQMX","ITAX","IWMX","JNJX",
    "JPMX","KLACX","KOX","KRAQX","LITEX","LINX","LLYX","LRCXX","MARAX","MAX",
    "MCDX","MDTX","METAX","MOOX","MRKX","MRVLX","MSFTX","MSTRX","MUX","NFLXX",
    "NVDAX","NVOX","OKLOX","OPENX","ORCLX","PALLX","PANWX","PEPX","PFEX","PGX",
    "PLTRX","PLX","PPLTX","PWRX","PYPLX","QQQX","RBLXX","RIOTX","SBETX","SCHFX",
    "SGOVX","SKHYX","SLVX","SMCIX","SMHX","SMRX","SNDKX","SPCEX","SPCX","SPYX",
    "STRCX","TBLLX","TERX","TMOX","TMUSX","TONXX","TQQQX","TSLAX","TSMX","UBERX",
    "UNHX","URAX","USARX","USPXX","UUUUX","VCXX","VGKX","VRTX","VTIX","VTX","VUGX",
    "VXUSX","WBDX","WMTX","WULFX","XLEX","XOMX","XOPX","YLDEx".upper(),
})

FAMILY_ORDER = ("XSTOCK", "STOCK", "DERIVATIVE", "CRYPTO", "OTHER")


class MarketScanner:
    def __init__(self, min_liquidity_eur: float, max_spread_bps: float, freshness_seconds: int) -> None:
        self.min_liquidity = D(str(min_liquidity_eur))
        self.max_spread = D(str(max_spread_bps))
        self.freshness_seconds = freshness_seconds

    @staticmethod
    def product_family(instrument: Instrument) -> str:
        if instrument.product_type.value == "DERIVATIVE":
            return "DERIVATIVE"

        base = str(instrument.base or "").upper().strip()
        if base in KNOWN_XSTOCKS:
            return "XSTOCK"

        # Kraken's AssetPairs payload is allowed to carry asset-class/product
        # hints. Use those when present so newly listed tokenized equities do
        # not require an application release before entering diagnostics.
        try:
            blob = json.dumps(instrument.metadata or {}, sort_keys=True, default=str).lower()
        except (TypeError, ValueError):
            blob = str(instrument.metadata or {}).lower()
        if any(token in blob for token in ("xstock", "tokenized stock", "tokenized equity", "tokenised stock", "tokenised equity")):
            return "XSTOCK"
        if any(token in blob for token in ("etf", "equity", "stock")):
            return "STOCK"

        # Unknown newly introduced x-suffix assets are tracked separately from
        # normal crypto only when they look like an equity ticker. Short crypto
        # symbols such as STX remain CRYPTO through the exclusion list.
        crypto_x_suffix = {"STX", "FLUX", "ZRX", "XCN", "XDC"}
        if base.endswith("X") and len(base) >= 4 and base not in crypto_x_suffix:
            return "XSTOCK"
        if instrument.product_type.value in {"SPOT", "SPOT_MARGIN"}:
            return "CRYPTO"
        return "OTHER"

    def fast_filter(
        self,
        instruments: Iterable[Instrument],
        snapshots: dict[str, MarketSnapshot],
        *,
        require_history: bool = True,
    ) -> list[Instrument]:
        return self.fast_filter_with_diagnostics(
            instruments, snapshots, require_history=require_history
        )[0]

    def fast_filter_with_diagnostics(
        self,
        instruments: Iterable[Instrument],
        snapshots: dict[str, MarketSnapshot],
        *,
        require_history: bool = True,
    ) -> tuple[list[Instrument], dict[str, Any]]:
        candidates: list[Instrument] = []
        total_by_family: dict[str, int] = {}
        excluded_by_reason: dict[str, int] = {}
        family_stats: dict[str, dict[str, Any]] = {}

        def inc(container: dict[str, int], key: str) -> None:
            container[key] = container.get(key, 0) + 1

        for instrument in instruments:
            family = self.product_family(instrument)
            inc(total_by_family, family)
            family_stats.setdefault(
                family,
                {"discovered": 0, "passed": 0, "excluded": {}},
            )["discovered"] += 1

            snap = snapshots.get(instrument.symbol)
            reason = ""
            if not snap:
                reason = "NO_TICKER"
            elif not instrument.tradeable:
                reason = "NOT_TRADEABLE"
            elif snap.age_seconds > self.freshness_seconds:
                reason = "STALE_TICKER"
            elif snap.spread_bps > self.max_spread:
                reason = "SPREAD_TOO_WIDE"
            elif (
                snap.volume_24h < self.min_liquidity
                and instrument.product_type.value != "DERIVATIVE"
            ):
                reason = "LIQUIDITY_TOO_LOW"
            elif require_history and len(snap.closes) < 30:
                reason = "HISTORY_INSUFFICIENT"

            if reason:
                inc(excluded_by_reason, reason)
                inc(family_stats[family]["excluded"], reason)
                continue

            candidates.append(instrument)
            family_stats[family]["passed"] += 1

        candidates.sort(key=lambda i: snapshots[i.symbol].volume_24h, reverse=True)
        return candidates, {
            "discovered": total_by_family,
            "excluded_by_reason": excluded_by_reason,
            "families": family_stats,
            "passed": len(candidates),
        }

    def build_history_candidates(
        self,
        prefiltered: Iterable[Instrument],
        *,
        core_limit: int = 200,
        exploration_limit: int = 80,
        exploration_slots_per_family: int = 2,
        cycle_key: str = "",
        preserve_symbols: set[str] | frozenset[str] = frozenset(),
    ) -> list[Instrument]:
        ranked = list(prefiltered)
        core_limit = max(0, int(core_limit))
        exploration_limit = max(0, int(exploration_limit))
        family_slots = max(0, int(exploration_slots_per_family))

        selected: list[Instrument] = []
        selected_symbols: set[str] = set()

        for instrument in ranked[:core_limit]:
            if instrument.symbol not in selected_symbols:
                selected.append(instrument)
                selected_symbols.add(instrument.symbol)

        remaining = [i for i in ranked if i.symbol not in selected_symbols]
        if not remaining or exploration_limit <= 0:
            for instrument in ranked:
                if instrument.symbol in preserve_symbols and instrument.symbol not in selected_symbols:
                    selected.append(instrument)
                    selected_symbols.add(instrument.symbol)
            return selected

        family_remaining: dict[str, list[Instrument]] = {family: [] for family in FAMILY_ORDER}
        for instrument in remaining:
            family_remaining.setdefault(self.product_family(instrument), []).append(instrument)

        exploration: list[Instrument] = []
        for family in FAMILY_ORDER:
            for instrument in family_remaining.get(family, [])[:family_slots]:
                if len(exploration) >= exploration_limit:
                    break
                if instrument.symbol not in selected_symbols:
                    exploration.append(instrument)
                    selected_symbols.add(instrument.symbol)
            if len(exploration) >= exploration_limit:
                break

        if len(exploration) < exploration_limit:
            unclaimed = [i for i in remaining if i.symbol not in selected_symbols]
            rotated = sorted(
                unclaimed,
                key=lambda i: sha256(
                    f"{cycle_key}:{i.symbol}".encode("utf-8")
                ).hexdigest(),
            )
            for instrument in rotated[: exploration_limit - len(exploration)]:
                exploration.append(instrument)
                selected_symbols.add(instrument.symbol)

        selected.extend(exploration)

        for instrument in ranked:
            if instrument.symbol in preserve_symbols and instrument.symbol not in selected_symbols:
                selected.append(instrument)
                selected_symbols.add(instrument.symbol)
        return selected

    def rank(
        self,
        candidates: Iterable[Instrument],
        snapshots: dict[str, MarketSnapshot],
        features: dict[str, dict[str, Decimal]],
    ) -> list[Instrument]:
        def score(inst: Instrument) -> Decimal:
            f = features[inst.symbol]
            trend = abs(f.get("trend", D("0")))
            momentum = abs(f.get("return_5", D("0")))
            liquidity = (f.get("liquidity", D("0")) + D("1")).ln()
            spread = max(D("0.1"), f.get("spread_bps", D("9999")))
            return trend * D("5") + momentum * D("2") + liquidity - spread / D("100")

        return sorted(candidates, key=score, reverse=True)

    @staticmethod
    def _canonical_asset(value: str) -> str:
        asset = str(value or "").upper().strip()
        aliases = {"XBT": "BTC", "XXBT": "BTC", "XETH": "ETH", "XXETH": "ETH"}
        if asset in aliases:
            return aliases[asset]
        if asset.startswith("XX") and len(asset) > 2:
            return asset[2:]
        if asset.startswith(("X", "Z")) and len(asset) > 3:
            return asset[1:]
        return asset

    def select_for_cycle(
        self,
        ranked: Iterable[Instrument],
        *,
        limit: int = 20,
        preserve_symbols: set[str] | frozenset[str] = frozenset(),
        family_slots: int = 2,
    ) -> tuple[list[Instrument], int]:
        limit = max(0, int(limit))
        family_slots = max(0, int(family_slots))
        selected: list[Instrument] = []
        selected_symbols: set[str] = set()
        seen_keys: set[tuple[str, str]] = set()
        duplicates_removed = 0

        def key_for(instrument: Instrument) -> tuple[str, str]:
            if instrument.venue == "spot" and instrument.quote.upper() in {"EUR", "USD", "ZEUR", "ZUSD"}:
                return ("SPOT_BASE", self._canonical_asset(instrument.base))
            return ("INSTRUMENT", instrument.symbol)

        ranked_list = list(ranked)

        for instrument in ranked_list:
            if instrument.symbol not in preserve_symbols or instrument.symbol in selected_symbols:
                continue
            selected.append(instrument)
            selected_symbols.add(instrument.symbol)
            seen_keys.add(key_for(instrument))

        for family in FAMILY_ORDER:
            if family_slots <= 0:
                break
            added = 0
            for instrument in ranked_list:
                if instrument.symbol in selected_symbols or self.product_family(instrument) != family:
                    continue
                key = key_for(instrument)
                if key in seen_keys:
                    duplicates_removed += 1
                    continue
                selected.append(instrument)
                selected_symbols.add(instrument.symbol)
                seen_keys.add(key)
                added += 1
                if added >= family_slots:
                    break

        slots = 0
        for instrument in ranked_list:
            if instrument.symbol in selected_symbols:
                continue
            if slots >= limit:
                break
            key = key_for(instrument)
            if key in seen_keys:
                duplicates_removed += 1
                continue
            selected.append(instrument)
            selected_symbols.add(instrument.symbol)
            seen_keys.add(key)
            slots += 1
        return selected, duplicates_removed

    @classmethod
    def count_by_family(cls, instruments: Iterable[Instrument]) -> dict[str, int]:
        counts = {family: 0 for family in FAMILY_ORDER}
        for instrument in instruments:
            family = cls.product_family(instrument)
            counts[family] = counts.get(family, 0) + 1
        return {key: value for key, value in counts.items() if value}
