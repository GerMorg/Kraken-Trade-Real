from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from decimal import Decimal
from typing import Any, Iterable

from app.domain.models import Instrument, MarketSnapshot
from app.domain.symbols import canonical_asset


D = Decimal

# Kraken identifies xStocks by an "x" suffix. Keep only known crypto
# false-positives here; all other discovery/eligibility facts come from Kraken.
XSTOCK_SUFFIX_EXCEPTIONS = frozenset({"FLUX"})

FAMILY_ORDER = ("XSTOCK", "STOCK", "DERIVATIVE", "CRYPTO", "OTHER")


class MarketScanner:
    def __init__(self, min_liquidity_eur: float, max_spread_bps: float, freshness_seconds: int) -> None:
        self.min_liquidity = D(str(min_liquidity_eur))
        self.max_spread = D(str(max_spread_bps))
        self.freshness_seconds = freshness_seconds

    FX_CONVERSION_ASSETS = frozenset({
        "EUR", "USD", "GBP", "CHF", "CAD", "AUD", "JPY", "NZD",
        "USDT", "USDC", "DAI",
    })

    def normalize_liquidity_eur(
        self,
        instruments: Iterable[Instrument],
        snapshots: dict[str, MarketSnapshot],
        *,
        conversion_snapshots: dict[str, MarketSnapshot] | None = None,
    ) -> dict[str, Any]:
        """Set each Spot market's 24h quote turnover converted into EUR.

        Kraken's Spot Ticker `v[1]` is base-asset volume, not quote or EUR
        turnover. Futures contract volumes are deliberately left unnormalized
        because their nominal units differ across contracts.
        """
        instrument_list = list(instruments)
        rates_source = conversion_snapshots if conversion_snapshots is not None else snapshots
        graph: dict[str, list[tuple[str, D]]] = {}

        for instrument in instrument_list:
            if (
                instrument.venue != "spot"
                or instrument.product_type.value == "DERIVATIVE"
                or not instrument.tradeable
            ):
                continue
            snap = rates_source.get(instrument.symbol)
            if (
                snap is None
                or snap.age_seconds > self.freshness_seconds
                or snap.price <= 0
            ):
                continue
            base = canonical_asset(instrument.base)
            quote = canonical_asset(instrument.quote)
            if not base or not quote or base == quote:
                continue
            graph.setdefault(base, []).append((quote, snap.price))
            graph.setdefault(quote, []).append((base, D("1") / snap.price))

        def to_eur_rate(asset: str) -> D | None:
            source = canonical_asset(asset)
            if not source:
                return None
            if source == "EUR":
                return D("1")
            # Breadth-first traversal prefers the shortest available conversion.
            # A non-fiat quote may convert directly into a supported currency,
            # but intermediate hops are restricted to currencies/stablecoins.
            queue: list[tuple[str, D]] = [(source, D("1"))]
            visited = {source}
            index = 0
            while index < len(queue):
                current, rate = queue[index]
                index += 1
                for neighbor, multiplier in graph.get(current, ()):
                    if neighbor in visited:
                        continue
                    if neighbor != "EUR" and neighbor not in self.FX_CONVERSION_ASSETS:
                        continue
                    next_rate = rate * multiplier
                    if neighbor == "EUR":
                        return next_rate if next_rate > 0 else None
                    visited.add(neighbor)
                    queue.append((neighbor, next_rate))
            return None

        normalized = 0
        unavailable = 0
        for instrument in instrument_list:
            snap = snapshots.get(instrument.symbol)
            if snap is None:
                continue
            if instrument.venue != "spot" or instrument.product_type.value == "DERIVATIVE":
                snapshots[instrument.symbol] = replace(
                    snap,
                    quote_asset=canonical_asset(instrument.quote),
                    liquidity_eur_24h=None,
                )
                continue

            quote = canonical_asset(instrument.quote)
            turnover_quote = snap.volume_24h_quote
            if turnover_quote is None:
                turnover_quote = snap.volume_24h * snap.price
            rate = to_eur_rate(quote)
            liquidity_eur = (
                turnover_quote * rate
                if rate is not None and turnover_quote >= 0
                else None
            )
            snapshots[instrument.symbol] = replace(
                snap,
                quote_asset=quote,
                volume_24h_quote=turnover_quote,
                liquidity_eur_24h=liquidity_eur,
            )
            if liquidity_eur is None:
                unavailable += 1
            else:
                normalized += 1

        return {
            "normalized_spot_markets": normalized,
            "unavailable_spot_markets": unavailable,
            "fx_conversion_markets": sum(len(edges) for edges in graph.values()) // 2,
        }

    @staticmethod
    def product_family(instrument: Instrument) -> str:
        if instrument.product_type.value == "DERIVATIVE":
            return "DERIVATIVE"

        base = str(instrument.base or "").upper().strip()
        identifiers = {
            base,
            str(getattr(instrument, "altname", "") or "").upper().strip(),
            str(instrument.symbol).split("/", 1)[0].upper().strip(),
        }
        if any(
            value.endswith("X")
            and len(value) >= 4
            and value not in XSTOCK_SUFFIX_EXCEPTIONS
            for value in identifiers
        ):
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
        instrument_list = list(instruments)
        liquidity_diagnostics = self.normalize_liquidity_eur(instrument_list, snapshots)
        candidates: list[Instrument] = []
        total_by_family: dict[str, int] = {}
        excluded_by_reason: dict[str, int] = {}
        family_stats: dict[str, dict[str, Any]] = {}

        def inc(container: dict[str, int], key: str) -> None:
            container[key] = container.get(key, 0) + 1

        for instrument in instrument_list:
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
            elif instrument.product_type.value != "DERIVATIVE" and snap.liquidity_eur_24h is None:
                reason = "LIQUIDITY_EUR_UNAVAILABLE"
            elif (
                snap.liquidity_eur_24h is not None
                and snap.liquidity_eur_24h < self.min_liquidity
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

        # Spot markets are ranked by quote turnover in EUR, never raw base units.
        # Derivatives without a verified EUR notional stay for family exploration.
        candidates.sort(
            key=lambda i: snapshots[i.symbol].liquidity_eur_24h or D("0"),
            reverse=True,
        )
        return candidates, {
            "discovered": total_by_family,
            "excluded_by_reason": excluded_by_reason,
            "families": family_stats,
            "passed": len(candidates),
            "eur_liquidity": liquidity_diagnostics,
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
        # Compatibility wrapper; canonicalisation is shared with portfolio and Tactical.
        return canonical_asset(value)

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
        counted_duplicate_symbols: set[str] = set()
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

        additional_added = 0
        for family in FAMILY_ORDER:
            if family_slots <= 0 or additional_added >= limit:
                break
            added = 0
            for instrument in ranked_list:
                if additional_added >= limit:
                    break
                if instrument.symbol in selected_symbols or self.product_family(instrument) != family:
                    continue
                key = key_for(instrument)
                if key in seen_keys:
                    if instrument.symbol not in counted_duplicate_symbols:
                        duplicates_removed += 1
                        counted_duplicate_symbols.add(instrument.symbol)
                    continue
                selected.append(instrument)
                selected_symbols.add(instrument.symbol)
                seen_keys.add(key)
                additional_added += 1
                added += 1
                if added >= family_slots:
                    break

        for instrument in ranked_list:
            if instrument.symbol in selected_symbols:
                continue
            if additional_added >= limit:
                break
            key = key_for(instrument)
            if key in seen_keys:
                if instrument.symbol not in counted_duplicate_symbols:
                    duplicates_removed += 1
                    counted_duplicate_symbols.add(instrument.symbol)
                continue
            selected.append(instrument)
            selected_symbols.add(instrument.symbol)
            seen_keys.add(key)
            additional_added += 1
        return selected, duplicates_removed

    @classmethod
    def count_by_family(cls, instruments: Iterable[Instrument]) -> dict[str, int]:
        counts = {family: 0 for family in FAMILY_ORDER}
        for instrument in instruments:
            family = cls.product_family(instrument)
            counts[family] = counts.get(family, 0) + 1
        return {key: value for key, value in counts.items() if value}
