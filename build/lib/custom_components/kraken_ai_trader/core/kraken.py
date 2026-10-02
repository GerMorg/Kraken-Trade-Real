from __future__ import annotations

import asyncio
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

import aiohttp

from .crypto import futures_signature, spot_signature
from .models import Direction, Instrument, MarketSnapshot, OrderBook, PortfolioSnapshot, Position, ProductType


class KrakenError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class KrakenGateway:
    """Exchange adapter. It exposes data and order primitives; it is not a trading authority."""

    SPOT = "https://api.kraken.com/0"
    FUTURES = "https://futures.kraken.com/derivatives/api/v3"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        futures_key: str = "",
        futures_secret: str = "",
        timeout: float = 15.0,
        account_currency: str = "EUR",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.futures_key = futures_key
        self.futures_secret = futures_secret
        self.timeout = timeout
        self.account_currency = account_currency.upper()
        self.spot_permissions: set[str] = set()
        self.healthy_public = False
        self.healthy_private = False
        self.healthy_portfolio = False
        self.instruments: dict[str, Instrument] = {}
        self._session: aiohttp.ClientSession | None = None
        self._nonce = int(time.time() * 1000)

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _next_nonce(self) -> int:
        self._nonce = max(self._nonce + 1, int(time.time() * 1000))
        return self._nonce

    async def _request(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("gateway not started")
        try:
            async with self._session.request(method, url, data=body, headers=headers, params=params) as response:
                text = await response.text()
                if response.status >= 400:
                    raise KrakenError(f"HTTP {response.status}: {text[:200]}")
                data = await response.json(content_type=None)
            errors = data.get("error", [])
            if errors:
                raise KrakenError(";".join(str(error) for error in errors))
            return data.get("result", data)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise KrakenError(str(exc), ambiguous=method.upper() == "POST") from exc

    async def spot_public(self, endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._request("GET", f"{self.SPOT}/public/{endpoint}", params=params)

    async def spot_private(self, endpoint: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        nonce = self._next_nonce()
        post_data, signature = spot_signature(f"/0/private/{endpoint}", nonce, params or {}, self.api_secret)
        return await self._request(
            "POST",
            f"{self.SPOT}/private/{endpoint}",
            body=post_data,
            headers={
                "API-Key": self.api_key,
                "API-Sign": signature,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    async def futures_public(self, endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._request("GET", f"{self.FUTURES}/{endpoint}", params=params)

    async def futures_private(self, endpoint: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        if not self.futures_key or not self.futures_secret:
            raise KrakenError("futures credentials unavailable")
        nonce = self._next_nonce()
        post_data, signature = futures_signature(f"/api/v3/{endpoint}", params or {}, self.futures_secret, nonce)
        headers = {
            "APIKey": self.futures_key,
            "Authent": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = f"{post_data}&nonce={nonce}" if post_data else f"nonce={nonce}"
        return await self._request("POST", f"{self.FUTURES}/{endpoint}", body=body, headers=headers)

    async def system_status(self) -> str:
        result = await self.spot_public("SystemStatus")
        status = str(result.get("status", "unknown"))
        self.healthy_public = status in {"online", "post_only"}
        return status

    async def discover(self) -> list[Instrument]:
        """Discover current instruments from Kraken metadata; no hard-coded trading universe."""
        spot = await self.spot_public("AssetPairs", {"assetVersion": "1", "info": "info"})
        instruments: list[Instrument] = []
        now = time.time()
        for pair_id, raw in spot.items():
            status = str(raw.get("status", "online"))
            leverage = raw.get("leverage_buy") or raw.get("leverage_sell") or [1]
            lev = tuple(sorted({Decimal(str(x)) for x in leverage if Decimal(str(x)) > 0})) or (Decimal("1"),)
            max_lev = max(lev, default=Decimal("1"))
            symbol = str(raw.get("wsname") or raw.get("altname") or pair_id)
            instruments.append(
                Instrument(
                    venue="kraken",
                    product_type=ProductType.SPOT_MARGIN if max_lev > 1 else ProductType.SPOT,
                    symbol=symbol,
                    instrument_id=str(pair_id),
                    altname=str(raw.get("altname", pair_id)),
                    base=self._clean_asset(str(raw.get("base", ""))),
                    quote=self._clean_asset(str(raw.get("quote", ""))),
                    contract_type=None,
                    status=status,
                    margin=max_lev > 1,
                    long=True,
                    short=max_lev > 1,
                    leverage_levels=lev,
                    max_leverage=max_lev,
                    order_min=Decimal(str(raw.get("ordermin", "0"))),
                    cost_min=Decimal(str(raw.get("costmin", "0"))),
                    lot_precision=int(raw.get("lot_decimals", 8)),
                    price_precision=int(raw.get("pair_decimals", 8)),
                    tick_size=Decimal(str(raw.get("tick_size") or Decimal("1e-8"))),
                    position_limit_long=self._decimal_or_none(raw.get("long_position_limit")),
                    position_limit_short=self._decimal_or_none(raw.get("short_position_limit")),
                    updated_at=now,
                )
            )

        if self.futures_key and self.futures_secret:
            futures = await self.futures_public("instruments")
            for raw in futures.get("instruments", []):
                symbol = str(raw.get("symbol", ""))
                if not symbol:
                    continue
                status = "online" if raw.get("tradeable", True) else "offline"
                max_lev = Decimal(str(raw.get("maxLeverage", raw.get("max_leverage", 1))))
                pair = str(raw.get("pair", symbol))
                base, quote = (pair.split("/", 1) + ["USD"])[:2] if "/" in pair else (pair, "USD")
                instruments.append(
                    Instrument(
                        venue="kraken_futures",
                        product_type=ProductType.FUTURES,
                        symbol=symbol,
                        instrument_id=symbol,
                        altname=symbol,
                        base=self._clean_asset(base),
                        quote=self._clean_asset(quote),
                        contract_type=str(raw.get("type", "perpetual")),
                        status=status,
                        margin=True,
                        long=True,
                        short=True,
                        leverage_levels=(Decimal("1"), max_lev),
                        max_leverage=max_lev,
                        order_min=Decimal(str(raw.get("lotSize", "0.001"))),
                        cost_min=Decimal(str(raw.get("costMin", "0"))),
                        lot_precision=int(raw.get("lotDecimals", 6)),
                        price_precision=int(raw.get("priceDecimals", 6)),
                        tick_size=Decimal(str(raw.get("tickSize", "0.1"))),
                        position_limit_long=self._decimal_or_none(raw.get("maxPositionSize")),
                        position_limit_short=self._decimal_or_none(raw.get("maxPositionSize")),
                        margin_class=str(raw.get("marginClass")) if raw.get("marginClass") else None,
                        collateral=str(raw.get("collateral")) if raw.get("collateral") else None,
                        updated_at=now,
                    )
                )
        self.instruments = {
            instrument.instrument_id: instrument
            for instrument in instruments
            if instrument.status in {"online", "post_only"}
        }
        self.healthy_public = True
        return list(self.instruments.values())

    async def authenticate(self) -> set[str]:
        result = await self.spot_private("GetApiKeyInfo")
        self.spot_permissions = {str(x) for x in result.get("permissions", [])}
        self.healthy_private = True
        if self.futures_key and self.futures_secret:
            await self.futures_private("accounts")
        return self.spot_permissions

    async def fast_scan(self, instruments: list[Instrument]) -> list[tuple[Instrument, Decimal, Decimal, Decimal, Decimal, float]]:
        """Fetch coarse ticker data in batches; depth/OHLC is reserved for the top candidates."""
        now = time.time()
        rows: list[tuple[Instrument, Decimal, Decimal, Decimal, Decimal, float]] = []
        spot = [i for i in instruments if i.product_type != ProductType.FUTURES]
        futures = [i for i in instruments if i.product_type == ProductType.FUTURES]
        if spot:
            for start in range(0, len(spot), 100):
                batch = spot[start:start + 100]
                raw = await self.spot_public("Ticker", {"pair": ",".join(i.instrument_id for i in batch)})
                by_key = {str(key): value for key, value in raw.items()}
                for i in batch:
                    item = by_key.get(i.instrument_id) or by_key.get(i.altname)
                    if not item:
                        continue
                    try:
                        last = Decimal(str(item["c"][0])); bid = Decimal(str(item["b"][0])); ask = Decimal(str(item["a"][0])); volume = Decimal(str(item["v"][1]))
                        if last > 0 and bid > 0 and ask > 0:
                            rows.append((i, last, bid, ask, volume, now))
                    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
                        continue
        if futures:
            raw = await self.futures_public("tickers")
            by_symbol = {str(item.get("symbol")): item for item in raw.get("tickers", []) if item.get("symbol")}
            for i in futures:
                item = by_symbol.get(i.symbol)
                if not item:
                    continue
                try:
                    last = Decimal(str(item.get("last", item.get("lastPrice", "0"))))
                    bid = Decimal(str(item.get("bid", last))); ask = Decimal(str(item.get("ask", last)))
                    volume = Decimal(str(item.get("volume", item.get("volume24h", "0"))))
                    if last > 0 and bid > 0 and ask > 0:
                        rows.append((i, last, bid, ask, volume, now))
                except (TypeError, ValueError, ArithmeticError):
                    continue
        return rows

    async def market_snapshot(self, instrument: Instrument) -> MarketSnapshot:
        if instrument.product_type == ProductType.FUTURES:
            ticker = await self.futures_public("tickers", {"symbol": instrument.symbol})
            tickers = ticker.get("tickers", [])
            raw_t = next((item for item in tickers if item.get("symbol") == instrument.symbol), tickers[0] if tickers else {})
            book_raw = await self.futures_public("orderbook", {"symbol": instrument.symbol})
            raw_bids = book_raw.get("orderBook", {}).get("bids", []) or book_raw.get("bids", [])
            raw_asks = book_raw.get("orderBook", {}).get("asks", []) or book_raw.get("asks", [])
            last = Decimal(str(raw_t.get("last", raw_t.get("lastPrice", "0"))))
            bid = Decimal(str(raw_t.get("bid", last)))
            ask = Decimal(str(raw_t.get("ask", last)))
            volume = Decimal(str(raw_t.get("volume", raw_t.get("volume24h", "0"))))
            funding = self._decimal_or_none(raw_t.get("fundingRate"))
            open_interest = self._decimal_or_none(raw_t.get("openInterest"))
            index_price = self._decimal_or_none(raw_t.get("index", raw_t.get("indexPrice")))
            candles = {}
        else:
            ticker = await self.spot_public("Ticker", {"pair": instrument.instrument_id})
            if not ticker:
                raise KrakenError(f"empty ticker for {instrument.symbol}")
            raw_t = next(iter(ticker.values()))
            last = Decimal(str(raw_t["c"][0]))
            bid = Decimal(str(raw_t["b"][0]))
            ask = Decimal(str(raw_t["a"][0]))
            volume = Decimal(str(raw_t["v"][1]))
            book_raw = await self.spot_public("Depth", {"pair": instrument.instrument_id, "count": "25"})
            if not book_raw:
                raise KrakenError(f"empty orderbook for {instrument.symbol}")
            raw_book = next(iter(book_raw.values()))
            raw_bids = raw_book.get("bids", [])
            raw_asks = raw_book.get("asks", [])
            funding = None
            open_interest = None
            index_price = None
            ohlc = await self.spot_public("OHLC", {"pair": instrument.instrument_id, "interval": "15"})
            candles = {}
            raw_candles = next((value for key, value in ohlc.items() if key != "last"), [])
            if raw_candles:
                candles[15] = tuple(Decimal(str(row[4])) for row in raw_candles[-250:])

        book = OrderBook(
            tuple((Decimal(str(x[0])), Decimal(str(x[1]))) for x in raw_bids),
            tuple((Decimal(str(x[0])), Decimal(str(x[1]))) for x in raw_asks),
            time.time(),
        )
        if last <= 0 or bid <= 0 or ask <= 0:
            raise KrakenError(f"invalid market prices for {instrument.symbol}")
        self.healthy_public = True
        return MarketSnapshot(instrument, last, bid, ask, volume, book, candles, funding, open_interest, time.time(), index_price)

    async def portfolio(self) -> PortfolioSnapshot:
        spot_balance = await self.spot_private("Balance")
        asset_code = self._kraken_currency_code(self.account_currency)
        trade = await self.spot_private("TradeBalance", {"asset": asset_code})
        open_orders = await self.spot_private("OpenOrders")
        spot_positions = await self.spot_private("OpenPositions")

        cash = sum(
            (
                Decimal(str(value))
                for key, value in spot_balance.items()
                if self._clean_asset(str(key)) == self.account_currency
            ),
            Decimal("0"),
        )
        equity = Decimal(str(trade.get("e", cash))) if trade.get("e") is not None else cash
        available_margin = Decimal(str(trade.get("mf", trade.get("e", cash))))
        used_margin = max(Decimal("0"), Decimal(str(trade.get("m", "0"))))
        positions = self._spot_positions(spot_positions)

        if self.futures_key and self.futures_secret:
            try:
                futures_accounts = await self.futures_private("accounts")
                futures_positions = await self.futures_private("openpositions")
                f_equity, f_available, f_used, f_positions = self._futures_portfolio(futures_accounts, futures_positions)
                equity += f_equity
                available_margin += f_available
                used_margin += f_used
                positions.extend(f_positions)
            except KrakenError:
                # Spot remains reconciled; futures health is handled by the caller and no new futures risk is authorized.
                self.healthy_portfolio = False
                raise

        gross = sum((position.notional for position in positions), Decimal("0"))
        net = sum((position.notional if position.direction == Direction.LONG else -position.notional for position in positions), Decimal("0"))
        unrealized = sum((position.unrealized_pnl for position in positions), Decimal("0"))
        realized = Decimal(str(trade.get("n", "0")))
        self.healthy_private = True
        self.healthy_portfolio = True
        return PortfolioSnapshot(
            equity=equity,
            cash=cash,
            available_margin=available_margin,
            used_margin=used_margin,
            gross_exposure=gross,
            net_exposure=net,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            daily_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            positions=tuple(positions),
            open_orders=len(open_orders.get("open", {})),
            orders_today=0,
            timestamp=time.time(),
        )

    def _spot_positions(self, data: dict[str, Any]) -> list[Position]:
        positions: list[Position] = []
        for raw in data.values() if isinstance(data, dict) else []:
            if not isinstance(raw, dict):
                continue
            pair = str(raw.get("pair", ""))
            quantity = Decimal(str(raw.get("vol", "0")))
            if quantity <= 0 or not pair:
                continue
            direction = Direction.LONG if str(raw.get("type", "buy")).lower() == "buy" else Direction.SHORT
            entry = Decimal(str(raw.get("price", "0")))
            cost = Decimal(str(raw.get("value", raw.get("cost", "0"))))
            notional = abs(cost) if cost else abs(quantity * entry)
            pnl = Decimal(str(raw.get("net", "0"))) if raw.get("net") is not None else Decimal("0")
            leverage = Decimal(str(raw.get("leverage", "1"))) if raw.get("leverage") else Decimal("1")
            positions.append(Position(pair, direction, quantity, entry, leverage, pnl, notional))
        return positions

    def _futures_portfolio(self, accounts: dict[str, Any], positions_data: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, list[Position]]:
        account_values = list(accounts.values()) if isinstance(accounts, dict) else []
        raw_account = account_values[0] if account_values and isinstance(account_values[0], dict) else accounts
        equity = self._first_decimal(raw_account, ("equity", "balance", "walletBalance"))
        available = self._first_decimal(raw_account, ("availableMargin", "available", "freeMargin"))
        used = self._first_decimal(raw_account, ("usedMargin", "marginUsed"))
        raw_positions = positions_data.get("openPositions", positions_data.get("positions", [])) if isinstance(positions_data, dict) else []
        positions: list[Position] = []
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol", raw.get("instrument", raw.get("tradeable", ""))))
            quantity = self._first_decimal(raw, ("size", "quantity", "qty"))
            if not symbol or quantity == 0:
                continue
            side = str(raw.get("side", raw.get("direction", raw.get("longShort", "long")))).lower()
            direction = Direction.SHORT if "short" in side or side in {"sell", "-1"} else Direction.LONG
            entry = self._first_decimal(raw, ("price", "entryPrice", "entry"))
            leverage = self._first_decimal(raw, ("leverage",)) or Decimal("1")
            pnl = self._first_decimal(raw, ("unrealizedPnl", "unrealizedPnL", "pnl"))
            notional = abs(self._first_decimal(raw, ("value", "notional"))) or abs(quantity * entry)
            positions.append(Position(symbol, direction, abs(quantity), entry, leverage, pnl, notional))
        return equity, available, used, positions

    async def submit(self, intent) -> tuple[str, dict[str, Any]]:
        if intent.product_type == ProductType.FUTURES:
            side = "buy" if intent.direction == Direction.LONG else "sell"
            if intent.effect in {"CLOSE", "REDUCE"}:
                side = "sell" if intent.direction == Direction.LONG else "buy"
            params: dict[str, object] = {
                "symbol": intent.symbol,
                "side": side,
                "size": str(intent.quantity),
                "orderType": "lmt" if intent.order_type == "limit" else "mkt",
                "cliOrdId": intent.client_order_id,
                "reduceOnly": str(intent.reduce_only).lower(),
            }
            if intent.price is not None:
                params["limitPrice"] = str(intent.price)
            result = await self.futures_private("sendOrder", params)
            status = result.get("sendStatus", {})
            return str(status.get("order_id") or status.get("orderId") or ""), result

        side = "buy" if intent.direction == Direction.LONG else "sell"
        if intent.effect in {"CLOSE", "REDUCE"}:
            side = "sell" if intent.direction == Direction.LONG else "buy"
        volume = "0" if intent.product_type == ProductType.SPOT_MARGIN and intent.effect == "CLOSE" else str(intent.quantity)
        params: dict[str, object] = {
            "ordertype": intent.order_type,
            "type": side,
            "volume": volume,
            "pair": intent.symbol,
            "cl_ord_id": intent.client_order_id,
        }
        if intent.price is not None:
            params["price"] = str(intent.price)
        if intent.leverage > 1:
            params["leverage"] = str(intent.leverage)
        if intent.reduce_only:
            params["reduce_only"] = "true"
        if intent.post_only:
            params["oflags"] = "post"
        result = await self.spot_private("AddOrder", params)
        return str((result.get("txid") or [""])[0]), result

    async def reconcile_order(self, intent, kraken_order_id: str = "") -> dict[str, Any] | None:
        if intent.product_type == ProductType.FUTURES:
            result = await self.futures_private("orders/status", {})
            for order in result.get("orders", []):
                if order.get("cliOrdId") == intent.client_order_id or order.get("orderId") == kraken_order_id:
                    return order
            return None
        if kraken_order_id:
            result = await self.spot_private("QueryOrders", {"txid": kraken_order_id})
            return result.get(kraken_order_id)
        result = await self.spot_private("OpenOrders")
        for order_id, order in result.get("open", {}).items():
            if order.get("cl_ord_id") == intent.client_order_id:
                return {"txid": order_id, **order}
        return None

    @staticmethod
    def quantize(value: Decimal, decimals: int) -> Decimal:
        quantum = Decimal(1).scaleb(-decimals)
        return value.quantize(quantum, rounding=ROUND_DOWN)

    @staticmethod
    def _decimal_or_none(value: object) -> Decimal | None:
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except Exception:  # noqa: BLE001 - metadata parsing should not crash discovery
            return None

    @staticmethod
    def _first_decimal(data: dict[str, Any], keys: tuple[str, ...]) -> Decimal:
        for key in keys:
            if data.get(key) is not None:
                try:
                    return Decimal(str(data[key]))
                except Exception:  # noqa: BLE001 - exchange may emit empty fields
                    continue
        return Decimal("0")

    @staticmethod
    def _clean_asset(value: str) -> str:
        value = value.upper()
        if value.startswith(("X", "Z")) and len(value) == 4:
            return value[1:]
        return value

    @staticmethod
    def _kraken_currency_code(currency: str) -> str:
        mapping = {"EUR": "ZEUR", "USD": "ZUSD", "GBP": "ZGBP", "CAD": "ZCAD", "CHF": "ZCHF"}
        return mapping.get(currency.upper(), currency.upper())
