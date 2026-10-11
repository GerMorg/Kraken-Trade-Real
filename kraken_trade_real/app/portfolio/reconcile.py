from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_UP
import datetime
import json
import time
from typing import Any

from app.domain.models import Instrument, PortfolioState, quantize_order_quantity
from app.domain.symbols import canonical_asset, resolve_instrument_symbol

D = Decimal

CASH_ASSETS = {
    "EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD", "NZD",
}


def dec(v: Any) -> D:
    try:
        return D(str(v if v not in (None, "") else "0"))
    except (InvalidOperation, ValueError):
        return D(0)


class PortfolioReconciler:
    def __init__(self, gateway: Any, db: Any) -> None:
        self.gateway = gateway
        self.db = db
        self.margin_account: dict[str, Any] | None = None
        self.spot_margin_account: dict[str, Any] | None = None
        self.futures_margin_account: dict[str, Any] | None = None
        # cash_balances stores total wallet inventory; available_cash_balances stores
        # the amount actually spendable after Kraken's open-order holds.
        self.cash_balances: dict[str, D] = {}
        self.available_cash_balances: dict[str, D] = {}
        self.available_balance_read_supported = False
        self.available_balance_read_ok = False
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

    def available_cash_balance(self, asset: str) -> D:
        """Return funds not already held by Kraken orders.

        If this live gateway supports BalanceEx but that read failed, fail closed
        for new spending. Test/legacy adapters without BalanceEx keep the old
        behavior so offline strategy tests remain backwards compatible.
        """
        key = canonical_asset(asset)
        if self.available_balance_read_supported:
            if not self.available_balance_read_ok:
                return D("0")
            return self.available_cash_balances.get(key, D("0"))
        return self.cash_balances.get(key, D("0"))

    def min_cost_eur(self, instrument: Instrument) -> D | None:
        rate = self.quote_to_eur_rate(instrument.quote)
        return instrument.min_cost * rate if rate is not None else None


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
        position_pnl_eur: dict[str, D] = {}
        position_pnl_pct: dict[str, D] = {}
        position_basis_eur: dict[str, D] = {}
        position_quantity: dict[str, D] = {}
        spot_margin_position_symbols: set[str] = set()
        spot_margin_open_order_ids: dict[str, set[str]] = {}
        spot_margin_position_direction_sets: dict[str, set[str]] = {}
        spot_margin_open_lot_count: dict[str, int] = {}
        spot_open_positions_read_ok = False
        spot_wallet_read_ok = False
        wallet_quantity_by_base: dict[str, D] = {}
        wallet_instrument_by_base: dict[str, Instrument] = {}
        spot_cash_position_symbols: set[str] = set()
        self.position_leverages = {}
        self.cash_balances = {}
        self.available_cash_balances = {}
        self.available_balance_read_supported = callable(
            getattr(self.gateway, "spot_balance_extended", None)
        )
        self.available_balance_read_ok = False
        self.spot_margin_account = None
        self.futures_margin_account = None
        self.margin_account = None
        if not self.gateway.api_key:
            return PortfolioState()
        try:
            balances: dict[str, Any]
            if self.available_balance_read_supported:
                try:
                    extended = self.gateway.spot_balance_extended()
                    if not isinstance(extended, dict):
                        raise TypeError("BALANCE_EX_INVALID_RESPONSE")
                    totals: dict[str, str] = {}
                    available: dict[str, D] = {}
                    for asset, item in extended.items():
                        if not isinstance(item, dict):
                            raise TypeError("BALANCE_EX_INVALID_ASSET_ROW")
                        quantity = dec(item.get("balance"))
                        credit = dec(item.get("credit"))
                        credit_used = dec(item.get("credit_used"))
                        held = dec(item.get("hold_trade"))
                        canonical = canonical_asset(asset)
                        totals[str(asset)] = str(quantity)
                        spendable = quantity + credit - credit_used - held
                        available[canonical] = max(D("0"), spendable)
                    balances = totals
                    self.available_cash_balances = available
                    self.available_balance_read_ok = True
                except Exception as exc:
                    # BalanceEx is required to know what is not reserved by open
                    # Spot orders. Balance remains useful for valuation/closures,
                    # but its total must not be mistaken for spendable funds.
                    balances = self.gateway.spot_balance()
                    self.available_balance_read_ok = False
                    self.db.event(
                        "PORTFOLIO_EXTENDED_BALANCE_FAILED",
                        "WARNING",
                        {
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:300],
                            "cash_entries_fail_closed": True,
                        },
                    )
            else:
                # Compatibility for non-live test adapters that only expose Balance.
                balances = self.gateway.spot_balance()
                self.available_balance_read_ok = True
                self.available_cash_balances = {
                    canonical_asset(asset): dec(value)
                    for asset, value in balances.items()
                }

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
                base_key = canonical_asset(instrument.base)
                wallet_quantity_by_base[base_key] = (
                    wallet_quantity_by_base.get(base_key, D("0")) + quantity
                )
                wallet_instrument_by_base[base_key] = instrument
                gross += abs(value_eur)
                net += value_eur

            # The wallet read succeeded independently of the TradeBalance/PnL call.
            # Keep this provenance even if a later margin-account read fails.
            spot_wallet_read_ok = True
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
            margin += used_margin
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
            spot_open_positions_read_ok = isinstance(open_positions, dict)
            known_spot_instruments = self._spot_instruments()
            # Freeze wallet-origin symbols before adding margin rows: a later
            # OpenPositions lot must not be mistaken for an already-counted wallet asset.
            wallet_position_symbols = set(positions)
            for position_id, item in open_positions.items():
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("pair") or item.get("symbol") or "")
                if not symbol:
                    continue
                instrument = resolve_instrument_symbol(symbol, known_spot_instruments)
                position_symbol = instrument.symbol if instrument is not None else symbol
                spot_margin_position_symbols.add(position_symbol)
                exchange_order_id = str(
                    item.get("ordertxid") or item.get("order_id") or position_id or ""
                ).strip()
                if exchange_order_id:
                    spot_margin_open_order_ids.setdefault(position_symbol, set()).add(
                        exchange_order_id
                    )
                position_side = str(item.get("type") or "").strip().lower()
                side_direction = (
                    "SHORT" if position_side == "sell"
                    else "LONG" if position_side == "buy"
                    else "UNKNOWN"
                )
                spot_margin_position_direction_sets.setdefault(position_symbol, set()).add(
                    side_direction
                )
                spot_margin_open_lot_count[position_symbol] = (
                    spot_margin_open_lot_count.get(position_symbol, 0) + 1
                )
                position_leverage = dec(item.get("leverage"))
                if position_leverage > 0:
                    self.position_leverages[position_symbol] = max(
                        self.position_leverages.get(position_symbol, D("0")),
                        position_leverage,
                    )

                # Kraken can expose multiple independent margin lots for one pair.
                # Aggregate their quantity, cost basis and PnL instead of allowing
                # the last lot to overwrite the earlier ones.
                quote_rate = (
                    self.quote_to_eur_rate(instrument.quote)
                    if instrument is not None else None
                )
                reported_net = dec(item.get("net"))
                reported_cost = abs(dec(item.get("cost")))
                if quote_rate is not None and quote_rate > 0:
                    basis_eur = reported_cost * quote_rate
                    pnl_eur = reported_net * quote_rate
                    if basis_eur > 0:
                        position_basis_eur[position_symbol] = (
                            position_basis_eur.get(position_symbol, D("0")) + basis_eur
                        )
                    position_pnl_eur[position_symbol] = (
                        position_pnl_eur.get(position_symbol, D("0")) + pnl_eur
                    )
                    unreal += pnl_eur
                    total_basis = position_basis_eur.get(position_symbol, D("0"))
                    if total_basis > 0:
                        position_pnl_pct[position_symbol] = (
                            position_pnl_eur[position_symbol] / total_basis * D("100")
                        )

                quantity = max(
                    D("0"),
                    dec(item.get("vol")) - dec(item.get("vol_closed")),
                )
                if quantity > 0:
                    position_quantity[position_symbol] = (
                        position_quantity.get(position_symbol, D("0")) + quantity
                    )

                # Preserve the existing wallet valuation policy, but don't let
                # that wallet-key check suppress accounting of a second margin lot.
                if position_symbol in wallet_position_symbols:
                    self.db.event(
                        "PORTFOLIO_MARGIN_POSITION_PNL",
                        "INFO",
                        {
                            "symbol": position_symbol,
                            "pnl_eur": str(position_pnl_eur.get(position_symbol, D("0"))),
                            "pnl_pct": str(position_pnl_pct.get(position_symbol, D("0"))),
                            "basis_eur": str(position_basis_eur.get(position_symbol, D("0"))),
                            "quantity": str(position_quantity.get(position_symbol, D("0"))),
                            "source": "KRAKEN_OPENPOSITIONS_AGGREGATED",
                            "wallet_inventory_present": True,
                        },
                    )
                    continue

                value = dec(item.get("value") or item.get("cost"))
                if quote_rate is not None and quote_rate > 0:
                    value *= quote_rate
                side = str(item.get("type") or "").lower()
                if side == "sell":
                    value = -abs(value)
                elif side == "buy":
                    value = abs(value)
                if value != 0:
                    positions[position_symbol] = (
                        positions.get(position_symbol, D("0")) + value
                    )
                    gross += abs(value)
                    net += value
                self.db.event(
                    "PORTFOLIO_MARGIN_POSITION_PNL",
                    "INFO",
                    {
                        "symbol": position_symbol,
                        "pnl_eur": str(position_pnl_eur.get(position_symbol, D("0"))),
                        "pnl_pct": str(position_pnl_pct.get(position_symbol, D("0"))),
                        "basis_eur": str(position_basis_eur.get(position_symbol, D("0"))),
                        "quantity": str(position_quantity.get(position_symbol, D("0"))),
                        "source": "KRAKEN_OPENPOSITIONS_AGGREGATED",
                        "margin_lot_value_eur": str(value),
                    },
                )
        except Exception as exc:
            spot_open_positions_read_ok = False
            self.db.event(
                "PORTFOLIO_SPOT_POSITIONS_FAILED",
                "WARNING",
                {"error": type(exc).__name__},
            )

        # A wallet asset may be protected as a Normal cash-Spot position only
        # when its entire balance reconciles to the app's FIFO inventory ledger.
        # A deposit, an unexplained quantity delta, a different quote pair, any
        # margin lot, or unknown fee/position provenance fails closed.
        if spot_wallet_read_ok and spot_open_positions_read_ok:
            try:
                known_spot = self._spot_instruments()
                instrument_by_symbol = {item.symbol: item for item in known_spot}
                open_lots = self.db.query(
                    """SELECT lot_id,symbol,direction,entry_order_id,entry_trade_id,
                              opening_decision_id,entry_price,remaining_quantity,
                              quote_asset,detail_json
                       FROM core_inventory_lots
                       WHERE CAST(remaining_quantity AS REAL)>0
                       ORDER BY opened_at,lot_id"""
                )
                lots_by_base: dict[str, list[dict[str, Any]]] = {}
                for lot in open_lots:
                    lot_symbol = str(lot.get("symbol") or "")
                    lot_instrument = instrument_by_symbol.get(lot_symbol)
                    if lot_instrument is None:
                        resolved = resolve_instrument_symbol(lot_symbol, known_spot)
                        lot_instrument = resolved if isinstance(resolved, Instrument) else None
                    if lot_instrument is None or lot_instrument.venue != "spot":
                        continue
                    base_key = canonical_asset(lot_instrument.base)
                    lots_by_base.setdefault(base_key, []).append(lot)

                margin_bases = {
                    canonical_asset(
                        instrument_by_symbol[symbol].base
                    )
                    for symbol in spot_margin_position_symbols
                    if symbol in instrument_by_symbol
                }
                for base_key, wallet_quantity in wallet_quantity_by_base.items():
                    instrument = wallet_instrument_by_base.get(base_key)
                    if (
                        instrument is None
                        or wallet_quantity <= 0
                        or base_key in margin_bases
                        or instrument.symbol in spot_margin_position_symbols
                    ):
                        continue
                    lots = lots_by_base.get(base_key, [])
                    if not lots or any(
                        str(lot.get("symbol") or "") != instrument.symbol
                        for lot in lots
                    ):
                        # Do not use a price from one quote market to manage lots
                        # opened on another market for the same base asset.
                        continue

                    managed_quantity = D("0")
                    basis_eur = D("0")
                    lot_order_ids: set[str] = set()
                    lot_decision_ids: set[str] = set()
                    valid_lots = True
                    for lot in lots:
                        try:
                            direction = str(lot.get("direction") or "").upper()
                            quantity = dec(lot.get("remaining_quantity"))
                            entry_price = dec(lot.get("entry_price"))
                            quote_asset = str(lot.get("quote_asset") or "")
                            order_id = str(lot.get("entry_order_id") or "").strip()
                            decision_id = str(lot.get("opening_decision_id") or "").strip()
                            detail = json.loads(lot.get("detail_json") or "{}")
                            if not isinstance(detail, dict):
                                detail = {}
                            leverage = dec(detail.get("leverage") or "1")
                            is_margin_lot = bool(detail.get("margin", False))
                            quote_rate = self.quote_to_eur_rate(quote_asset)
                            if (
                                direction != "LONG"
                                or quantity <= 0
                                or entry_price <= 0
                                or not order_id
                                or not decision_id
                                or is_margin_lot
                                or leverage != D("1")
                                or quote_rate is None
                                or quote_rate <= 0
                            ):
                                valid_lots = False
                                break
                            managed_quantity += quantity
                            basis_eur += quantity * entry_price * quote_rate
                            lot_order_ids.add(order_id)
                            lot_decision_ids.add(decision_id)
                        except (ArithmeticError, TypeError, ValueError, json.JSONDecodeError):
                            valid_lots = False
                            break
                    tolerance = max(D("0.00000001"), wallet_quantity.copy_abs() * D("0.000001"))
                    if (
                        not valid_lots
                        or managed_quantity <= 0
                        or basis_eur <= 0
                        or abs(wallet_quantity - managed_quantity) > tolerance
                        or not lot_order_ids
                        or not lot_decision_ids
                    ):
                        continue

                    price = self._spot_price(instrument)
                    quote_rate = self.quote_to_eur_rate(instrument.quote)
                    if price <= 0 or quote_rate is None or quote_rate <= 0:
                        continue
                    current_value_eur = wallet_quantity * price * quote_rate
                    pnl_eur = current_value_eur - basis_eur
                    position_basis_eur[instrument.symbol] = basis_eur
                    position_quantity[instrument.symbol] = wallet_quantity
                    position_pnl_eur[instrument.symbol] = pnl_eur
                    position_pnl_pct[instrument.symbol] = pnl_eur / basis_eur * D("100")
                    spot_cash_position_symbols.add(instrument.symbol)
                    self.db.event(
                        "PORTFOLIO_CASH_SPOT_LEDGER_MATCH",
                        "INFO",
                        {
                            "symbol": instrument.symbol,
                            "base_asset": base_key,
                            "wallet_quantity": str(wallet_quantity),
                            "ledger_quantity": str(managed_quantity),
                            "basis_eur": str(basis_eur),
                            "mark_to_market_pnl_eur": str(pnl_eur),
                            "pnl_basis": "FIFO_ENTRY_COST_GROSS_MARK_TO_MARKET",
                            "unique_open_orders": len(lot_order_ids),
                            "unique_opening_decisions": len(lot_decision_ids),
                        },
                    )
            except Exception as exc:
                # Provenance/learning is auxiliary; never fail portfolio reconciliation.
                self.db.event(
                    "PORTFOLIO_CASH_SPOT_LEDGER_MATCH_FAILED",
                    "WARNING",
                    {"error_type": type(exc).__name__, "error": str(exc)[:300]},
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
        # Daily P/L is derived from persisted UTC-day equity snapshots. Kraken's
        # TradeBalance "n" field is not daily realized P/L, and adding it to the
        # aggregated open-position P/L can double-count the same exposure.
        now = time.time()
        day_start = datetime.datetime.fromtimestamp(
            now, datetime.timezone.utc
        ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        baseline = self.db.one(
            "SELECT equity_eur,captured_at FROM portfolio_snapshots "
            "WHERE captured_at < ? AND captured_at >= ? ORDER BY captured_at DESC LIMIT 1",
            (day_start, day_start - 36 * 3600),
        )
        if baseline is None:
            baseline = self.db.one(
                "SELECT equity_eur,captured_at FROM portfolio_snapshots "
                "WHERE captured_at >= ? AND captured_at < ? ORDER BY captured_at ASC LIMIT 1",
                (day_start, now),
            )
        baseline_equity = dec(baseline.get("equity_eur")) if baseline else D(0)
        daily_pnl = equity - baseline_equity if baseline_equity > 0 else D(0)
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
            daily_pnl,
            drawdown,
            0,
            now,
            position_pnl_eur=position_pnl_eur,
            position_pnl_pct=position_pnl_pct,
            position_basis_eur=position_basis_eur,
            position_quantity=position_quantity,
            spot_open_positions_read_ok=spot_open_positions_read_ok,
            spot_wallet_read_ok=spot_wallet_read_ok,
            spot_cash_position_symbols=tuple(sorted(spot_cash_position_symbols)),
            spot_margin_position_symbols=tuple(sorted(spot_margin_position_symbols)),
            spot_margin_open_order_ids={
                symbol: tuple(sorted(order_ids))
                for symbol, order_ids in spot_margin_open_order_ids.items()
            },
            spot_margin_position_directions={
                symbol: (
                    next(iter(directions))
                    if len(directions) == 1 and "UNKNOWN" not in directions
                    else "MIXED"
                )
                for symbol, directions in spot_margin_position_direction_sets.items()
            },
            spot_margin_open_lot_count=spot_margin_open_lot_count,
        )

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

