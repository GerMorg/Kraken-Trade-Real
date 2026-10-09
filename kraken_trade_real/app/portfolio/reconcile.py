from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_UP
import time
from typing import Any

from app.domain.models import Instrument, PortfolioState, quantize_order_quantity
from app.domain.symbols import resolve_instrument_symbol

D = Decimal

CASH_ASSETS = {
    "EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD", "NZD",
}


def dec(v: Any) -> D:
    try:
        return D(str(v if v not in (None, "") else "0"))
    except (InvalidOperation, ValueError):
        return D(0)


def canonical_asset(value: Any) -> str:
    asset = str(value or "").upper().strip()
    aliases = {
        "XBT": "BTC",
        "XXBT": "BTC",
        "XETH": "ETH",
        "XXETH": "ETH",
        "ZUSD": "USD",
        "ZEUR": "EUR",
        "ZGBP": "GBP",
        "ZCHF": "CHF",
        "ZCAD": "CAD",
        "ZJPY": "JPY",
        "ZAUD": "AUD",
        "ZNZD": "NZD",
    }
    if asset in aliases:
        return aliases[asset]
    if asset.startswith("XX") and len(asset) > 2:
        return asset[2:]
    if asset.startswith(("X", "Z")) and len(asset) > 3:
        return asset[1:]
    return asset


class PortfolioReconciler:
    def __init__(self, gateway: Any, db: Any) -> None:
        self.gateway = gateway
        self.db = db
        self.margin_account: dict[str, Any] | None = None
        self.spot_margin_account: dict[str, Any] | None = None
        self.futures_margin_account: dict[str, Any] | None = None
        self.cash_balances: dict[str, D] = {}
        # Exchange-reported Spot Margin/Futures leverage keyed by canonical symbol.
        self.position_leverages: dict[str, D] = {}
        self.instruments: list[Instrument] = []
        self.spot_tickers: dict[str, Any] = {}

    def set_market_context(self, instruments: list[Instrument], spot_tickers: dict[str, Any]) -> None:
        self.instruments = instruments
        self.spot_tickers = spot_tickers

    def _ticker_raw(self, instrument: Instrument) -> dict[str, Any] | None:
        keys = (
            instrument.instrument_id,
            instrument.altname,
            instrument.symbol,
            instrument.symbol.replace("/", ""),
        )
        for key in keys:
            value = self.spot_tickers.get(key)
            if isinstance(value, dict):
                return value
        wanted = instrument.symbol.replace("/", "").upper()
        for key, value in self.spot_tickers.items():
            if str(key).replace("/", "").upper() == wanted and isinstance(value, dict):
                return value
        return None

    def _spot_price(self, instrument: Instrument) -> D:
        raw = self._ticker_raw(instrument)
        if not raw:
            return D(0)
        value = raw.get("c")
        if isinstance(value, list):
            value = value[0] if value else None
        return dec(value)

    def _spot_instruments(self) -> list[Instrument]:
        if self.instruments:
            return [
                instrument
                for instrument in self.instruments
                if instrument.venue == "spot" and instrument.tradeable
            ]
        rows = self.db.query(
            """SELECT symbol, instrument_id, altname, base, quote, status
               FROM instruments WHERE venue='spot'"""
        )
        return [
            Instrument(
                venue="spot",
                product_type=__import__("app.domain.states", fromlist=["ProductType"]).ProductType.SPOT,
                symbol=str(row["symbol"]),
                instrument_id=str(row["instrument_id"]),
                altname=str(row["altname"]),
                base=str(row["base"]),
                quote=str(row["quote"]),
                status=str(row["status"]),
                margin_available=False,
                long_available=True,
                short_available=False,
                leverage_levels=(D("1"),),
                min_order_qty=D(0),
                min_cost=D(0),
                lot_decimals=8,
                price_decimals=8,
                tick_size=D("0.00000001"),
                margin_class="spot",
            )
            for row in rows
        ]

    def quote_to_eur_rate(self, quote: str) -> D | None:
        source = canonical_asset(quote)
        if source == "EUR":
            return D(1)
        instruments = self._spot_instruments()
        direct: dict[str, D] = {}
        graph: dict[str, list[tuple[str, D]]] = {}
        bridge = {
            "EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD", "NZD",
            "USDC", "USDT",
        }
        for instrument in instruments:
            price = self._spot_price(instrument)
            if price <= 0:
                continue
            base = canonical_asset(instrument.base)
            quote_asset = canonical_asset(instrument.quote)
            graph.setdefault(base, []).append((quote_asset, price))
            graph.setdefault(quote_asset, []).append((base, D(1) / price))
            if quote_asset == "EUR" and base == source:
                direct[source] = price
            elif base == "EUR" and quote_asset == source:
                direct[source] = D(1) / price
        if source in direct:
            return direct[source]
        queue: list[tuple[str, D, int]] = [(source, D(1), 0)]
        visited = {source}
        while queue:
            node, rate, depth = queue.pop(0)
            if node == "EUR":
                return rate
            if depth >= 3:
                continue
            for nxt, edge in graph.get(node, []):
                if nxt in visited:
                    continue
                if nxt != "EUR" and nxt not in bridge:
                    continue
                visited.add(nxt)
                queue.append((nxt, rate * edge, depth + 1))
        return None

    def cash_balance(self, asset: str) -> D:
        """Return the current spot cash balance in canonical asset units."""
        return self.cash_balances.get(canonical_asset(asset), D("0"))

    def min_cost_eur(self, instrument: Instrument) -> D | None:
        rate = self.quote_to_eur_rate(instrument.quote)
        return instrument.min_cost * rate if rate is not None else None


def minimum_orderable_spot_quantity(instrument: Instrument, price: D) -> D | None:
    """Return the smallest Spot base quantity satisfying Kraken quantity/cost minimums."""
    if instrument.venue != "spot" or price <= 0:
        return None
    minimum_qty = dec(instrument.min_order_qty)
    minimum_cost = dec(instrument.min_cost)
    cost_qty = minimum_cost / price if minimum_cost > 0 else D("0")
    required = max(minimum_qty, cost_qty)
    if required <= 0:
        return None
    quantity = quantize_order_quantity(instrument, required, rounding=ROUND_UP)
    quantum = D("1").scaleb(-max(0, min(18, int(instrument.lot_decimals))))
    for _ in range(2):
        if quantity >= minimum_qty and (
            minimum_cost <= 0 or quantity * price >= minimum_cost
        ):
            return quantity
        quantity = quantize_order_quantity(
            instrument, quantity + quantum, rounding=ROUND_UP
        )
    if quantity >= minimum_qty and (
        minimum_cost <= 0 or quantity * price >= minimum_cost
    ):
        return quantity
    return None


def resolve_spot_cash_reduction_quantity(
    instrument: Instrument,
    price: D,
    requested_quantity: D | None,
    available_base_quantity: D,
    *,
    flattening: bool,
) -> tuple[D | None, str]:
    """Keep cash-Spot reductions inside holdings and distinguish small deltas from dust.

    A partial rebalance smaller than Kraken's minimum is not a dust holding. A full
    flatten uses the available base-wallet quantity, but only when orderable.
    """
    minimum = minimum_orderable_spot_quantity(instrument, price)
    if minimum is None:
        return requested_quantity, ""
    available = quantize_order_quantity(
        instrument, max(D("0"), dec(available_base_quantity))
    )
    requested = (
        quantize_order_quantity(instrument, max(D("0"), requested_quantity))
        if requested_quantity is not None
        else None
    )

    if flattening and available >= minimum:
        if requested is None or requested != available:
            return available, "FULL_EXIT_BALANCE_RECOVERY"
        return available, ""

    status = ""
    if requested is not None and requested > available:
        requested = available
        status = "BALANCE_CLAMPED"
    if requested is None or requested <= 0 or requested < minimum:
        if available >= minimum:
            return requested, "REBALANCE_DELTA_BELOW_MINIMUM"
        return requested, "DUST_POSITION"
    return requested, status

    def quantity_for_eur(self, instrument: Instrument, notional_eur: D, price: D) -> D | None:
        if price <= 0 or notional_eur <= 0:
            return None
        rate = self.quote_to_eur_rate(instrument.quote)
        if rate is None or rate <= 0:
            return None
        quote_notional = notional_eur / rate
        if instrument.venue != "futures":
            quantity = quantize_order_quantity(instrument, quote_notional / price)
            return quantity if quantity > 0 else None

        contract_size = dec(
            instrument.metadata.get("contractSize")
            if isinstance(instrument.metadata, dict)
            else 0
        )
        if contract_size <= 0:
            return None
        future_type = str(
            instrument.metadata.get("type", "")
            if isinstance(instrument.metadata, dict)
            else ""
        ).lower()
        if "inverse" in future_type:
            return quote_notional / contract_size
        return (quote_notional / price) / contract_size

    def _choose_valuation_instrument(self, base: str) -> Instrument | None:
        target = canonical_asset(base)
        preference = {"EUR": 0, "USD": 1, "USDC": 2, "USDT": 3, "GBP": 4, "CHF": 5}
        candidates = [
            instrument
            for instrument in self._spot_instruments()
            if canonical_asset(instrument.base) == target
        ]
        candidates = [
            instrument
            for instrument in candidates
            if self._spot_price(instrument) > 0 and self.quote_to_eur_rate(instrument.quote) is not None
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda instrument: (
                preference.get(canonical_asset(instrument.quote), 10),
                instrument.symbol,
            ),
        )

    def reconcile(self) -> PortfolioState:
        cash = equity = gross = net = margin = unreal = realized = D(0)
        positions: dict[str, D] = {}
        self.position_leverages = {}
        self.cash_balances = {}
        self.spot_margin_account = None
        self.futures_margin_account = None
        self.margin_account = None
        if not self.gateway.api_key:
            return PortfolioState()
        try:
            balances = self.gateway.spot_balance()
            for asset, raw_value in balances.items():
                quantity = dec(raw_value)
                if quantity == 0:
                    continue
                canonical = canonical_asset(asset)
                self.cash_balances[canonical] = (
                    self.cash_balances.get(canonical, D("0")) + quantity
                )
                if canonical in CASH_ASSETS:
                    rate = self.quote_to_eur_rate(canonical)
                    if rate is None:
                        self.db.event(
                            "PORTFOLIO_CASH_FX_UNAVAILABLE",
                            "WARNING",
                            {"asset": canonical},
                        )
                    else:
                        cash += quantity * rate
                    continue
                instrument = self._choose_valuation_instrument(canonical)
                if instrument is None:
                    self.db.event(
                        "PORTFOLIO_ASSET_UNPRICED",
                        "WARNING",
                        {"asset": canonical, "quantity": str(quantity)},
                    )
                    continue
                price = self._spot_price(instrument)
                rate = self.quote_to_eur_rate(instrument.quote)
                if price <= 0 or rate is None:
                    continue
                value_eur = quantity * price * rate
                positions[instrument.symbol] = value_eur
                gross += abs(value_eur)
                net += value_eur

            tb = self.gateway.spot_private(
                "TradeBalance",
                {"aclass": "currency", "asset": "ZEUR"},
            )
            equity = dec(tb.get("eb") or tb.get("tb") or cash)
            realized = dec(tb.get("n"))
            spot_equity = dec(tb.get("e") or equity)
            used_margin = max(D("0"), dec(tb.get("m")))
            free_margin = dec(tb.get("mf"))
            if free_margin <= 0 and spot_equity > used_margin:
                free_margin = spot_equity - used_margin
            margin_level = dec(tb.get("ml"))
            if margin_level <= 0:
                margin_level = (
                    spot_equity / used_margin * D("100")
                    if used_margin > 0
                    else D("9999")
                )
            self.spot_margin_account = {
                "source": "spot",
                "free_margin": str(max(D("0"), free_margin)),
                "margin_level_pct": str(margin_level),
                "equity_eur": str(spot_equity),
                "used_margin_eur": str(used_margin),
            }
            self.margin_account = self.spot_margin_account
        except Exception as exc:
            self.db.event(
                "PORTFOLIO_SPOT_READ_FAILED",
                "WARNING",
                {"error": type(exc).__name__},
            )

        try:
            open_positions = self.gateway.spot_open_positions()
            for item in open_positions.values():
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("pair") or item.get("symbol") or "")
                if not symbol:
                    continue
                instrument = resolve_instrument_symbol(symbol, self._spot_instruments())
                position_symbol = instrument.symbol if instrument is not None else symbol
                position_leverage = dec(item.get("leverage"))
                if position_leverage > 0:
                    self.position_leverages[position_symbol] = position_leverage
                if position_symbol in positions:
                    continue
                value = dec(item.get("value") or item.get("cost"))
                if instrument is not None:
                    rate = self.quote_to_eur_rate(instrument.quote)
                    if rate is not None:
                        value *= rate
                if value == 0:
                    continue
                if str(item.get("type") or "").lower() == "sell":
                    value = -abs(value)
                # Keep exchange margin positions under the canonical discovered
                # instrument symbol. Kraken's OpenPositions pair can be an
                # altname (e.g. MINAUSD) rather than the app's MINA/USD symbol;
                # otherwise the position is silently omitted from reevaluation.
                positions[position_symbol] = value
                gross += abs(value)
                net += value
        except Exception as exc:
            self.db.event(
                "PORTFOLIO_SPOT_POSITIONS_FAILED",
                "WARNING",
                {"error": type(exc).__name__},
            )

        if getattr(self.gateway, "futures_enabled", False):
            try:
                accounts = self.gateway.futures_accounts().get("accounts", {})
                acct: dict[str, Any] = (
                    next(iter(accounts.values()), {}) if isinstance(accounts, dict) else {}
                )
                self.futures_margin_account = {
                    "source": "futures",
                    "free_margin": str(
                        acct.get("availableMargin") or acct.get("freeMargin") or 0
                    ),
                    "margin_level_pct": str(acct.get("marginLevel") or 9999),
                }
                self.margin_account = self.futures_margin_account
                equity = max(
                    equity,
                    dec(acct.get("portfolioValue") or acct.get("equity") or 0),
                )
                margin += dec(acct.get("initialMargin") or acct.get("marginUsed") or 0)
                unreal += dec(acct.get("unrealizedPnl") or 0)
                rows = self.gateway.futures_open_positions().get("openPositions", [])
                for item in rows if isinstance(rows, list) else []:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol") or "")
                    value = dec(
                        item.get("value") or item.get("size") or item.get("quantity")
                    )
                    if symbol:
                        position = value
                        if str(item.get("side", "buy")).lower() != "buy":
                            position = -abs(value)
                        positions[symbol] = position
                        reported_leverage = dec(item.get("leverage"))
                        if reported_leverage > 0:
                            self.position_leverages[symbol] = reported_leverage
                        gross += abs(position)
                        net += position
            except Exception as exc:
                self.margin_account = None
                self.db.event(
                    "PORTFOLIO_FUTURES_READ_FAILED",
                    "WARNING",
                    {"error": str(exc)[:300]},
                )

        equity = equity if equity > 0 else cash + max(D(0), unreal)
        peak_row = self.db.one(
            "SELECT MAX(CAST(equity_eur AS REAL)) AS peak FROM portfolio_snapshots"
        )
        peak = dec(peak_row.get("peak") if peak_row else equity)
        drawdown = (
            (D(1) - equity / peak) * 100
            if peak > 0 and equity < peak
            else D(0)
        )
        return PortfolioState(
            equity,
            cash,
            positions,
            gross,
            net,
            margin,
            unreal,
            realized,
            unreal + realized,
            drawdown,
            0,
            time.time(),
        )
