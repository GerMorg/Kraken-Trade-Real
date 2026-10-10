from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .signing import sign_futures, sign_spot


class KrakenError(RuntimeError):
    pass


class KrakenAmbiguous(KrakenError):
    pass


@dataclass
class HTTP:
    timeout: float = 15.0

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        req = Request(
            url,
            data=data.encode("utf-8") if isinstance(data, str) else data,
            headers={
                "Accept": "application/json",
                "User-Agent": "Kraken-Trade-Real/0.1.39",
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urlopen(
                req, timeout=self.timeout if timeout is None else max(0.5, float(timeout))
            ) as response:  # nosec B310
                raw = response.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise KrakenAmbiguous("INVALID_HTTP_RESPONSE") from exc
        except HTTPError as exc:
            try:
                detail = exc.read(512).decode("utf-8", errors="replace").replace("\n", " ")[:400]
            except OSError:
                detail = ""
            suffix = f":{detail}" if detail else ""
            if exc.code >= 500:
                raise KrakenAmbiguous(f"HTTP_{exc.code}{suffix}") from exc
            raise KrakenError(f"HTTP_{exc.code}{suffix}") from exc
        except (TimeoutError, URLError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise KrakenAmbiguous(f"NETWORK:{reason}") from exc
        if not isinstance(payload, dict):
            raise KrakenAmbiguous("INVALID_JSON_ROOT")
        return payload


class KrakenGateway:
    SPOT = "https://api.kraken.com"
    FUTURES = "https://futures.kraken.com/derivatives"
    FUTURES_CHARTS = "https://futures.kraken.com/api/charts/v1"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        timeout: float = 15.0,
        futures_enabled: bool = False,
        futures_api_key: str | None = None,
        futures_api_secret: str | None = None,
        tokenized_assets_enabled: bool = False,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.futures_enabled = futures_enabled
        self.tokenized_assets_enabled = tokenized_assets_enabled
        self.futures_api_key = futures_api_key or ""
        self.futures_api_secret = futures_api_secret or ""
        self.http = HTTP(timeout)
        # Kraken persists the last nonce per API key. Use microseconds so a
        # fresh process is safely above timestamps previously generated in
        # millisecond resolution, while still guaranteeing monotonicity.
        self._nonce = time.time_ns() // 1_000
        self._spot_private_lock = threading.RLock()
        self.last_public_instrument_warnings: list[str] = []
        self.last_public_instrument_stage = "IDLE"
        self.last_public_ticker_warnings: list[str] = []
        self.last_public_ticker_stage = "IDLE"

    def _next_nonce(self) -> int:
        with self._spot_private_lock:
            candidate = time.time_ns() // 1_000
            self._nonce = max(self._nonce + 1, candidate)
            return self._nonce

    def spot_public(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        query = urlencode(params or {}, doseq=True)
        url = f"{self.SPOT}/0/public/{method}" + (f"?{query}" if query else "")
        result = self.http.request(url, timeout=timeout)
        errors = result.get("error")
        if errors:
            detail = ";".join(str(item) for item in errors)[:800]
            raise KrakenError(f"KRAKEN_PUBLIC:{detail}")
        return result.get("result") or {}

    def spot_private(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # Serialize the complete authenticated request. A higher nonce that
        # arrives first can make an older in-flight request invalid at Kraken.
        with self._spot_private_lock:
            path = f"/0/private/{method}"
            body = dict(params or {})
            body["nonce"] = self._next_nonce()
            encoded = urlencode(body, doseq=True)
            result = self.http.request(
                self.SPOT + path,
                method="POST",
                data=encoded,
                headers={
                    "API-Key": self.api_key,
                    "API-Sign": sign_spot(path, body, self.api_secret),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if result.get("error"):
                raise KrakenError("KRAKEN_PRIVATE:" + ";".join(str(x) for x in result.get("error", []))[:700])
            return result.get("result") or {}

    def futures_public(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urlencode(params or {}, doseq=True)
        return self.http.request(
            f"{self.FUTURES}/api/v3/{method}" + (f"?{query}" if query else "")
        )

    def futures_private(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.futures_enabled or not self.futures_api_key or not self.futures_api_secret:
            raise KrakenError("FUTURES_DISABLED_OR_CREDENTIALS_MISSING")
        endpoint = f"/api/v3/{method}"
        body = dict(params or {})
        encoded = urlencode(body, doseq=True)
        result = self.http.request(
            self.FUTURES + endpoint,
            method="POST",
            data=encoded,
            headers={
                "APIKey": self.futures_api_key,
                "Authent": sign_futures(endpoint, encoded, self.futures_api_secret),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if result.get("result") not in (None, "success"):
            raise KrakenError("KRAKEN_FUTURES_PRIVATE")
        return result

    def public_instruments(self):
        # The normal Spot universe is the startup-critical market source.
        # Tokenized xStocks are opt-in because Kraken currently restricts EEA
        # API order-book trading for these assets; they must never block core
        # crypto startup when the endpoint is unavailable.
        self.last_public_instrument_warnings = []
        self.last_public_instrument_stage = "SPOT_ASSETPAIRS"
        spot = self.spot_public("AssetPairs")

        tokenized: dict[str, Any] = {}
        if self.tokenized_assets_enabled:
            self.last_public_instrument_stage = "TOKENIZED_ASSETPAIRS"
            try:
                tokenized_raw = self.spot_public(
                    "AssetPairs",
                    {"aclass_base": "tokenized_asset"},
                    timeout=min(self.http.timeout, 5.0),
                )
                tokenized = {
                    str(key): {**value, "aclass_base": "tokenized_asset"}
                    for key, value in (tokenized_raw or {}).items()
                    if isinstance(value, dict)
                }
            except (KrakenError, KrakenAmbiguous) as exc:
                warning = f"{type(exc).__name__}:{str(exc)[:300]}"
                self.last_public_instrument_warnings.append(warning)

        merged_spot = dict(spot or {})
        merged_spot.update(tokenized)
        if self.futures_enabled:
            self.last_public_instrument_stage = "FUTURES_INSTRUMENTS"
            futures = self.futures_public("instruments")
        else:
            futures = {"instruments": []}
        self.last_public_instrument_stage = "COMPLETE"
        return merged_spot, futures

    def public_tickers(self):
        self.last_public_ticker_warnings = []
        self.last_public_ticker_stage = "SPOT_TICKER"
        spot = self.spot_public("Ticker")
        tokenized: dict[str, Any] = {}
        if self.tokenized_assets_enabled:
            self.last_public_ticker_stage = "TOKENIZED_TICKER"
            try:
                tokenized = self.spot_public(
                    "Ticker",
                    {"asset_class": "tokenized_asset"},
                    timeout=min(self.http.timeout, 5.0),
                )
            except (KrakenError, KrakenAmbiguous) as exc:
                self.last_public_ticker_warnings.append(
                    f"{type(exc).__name__}:{str(exc)[:300]}"
                )
        merged_spot = dict(spot or {})
        merged_spot.update(tokenized or {})
        if self.futures_enabled:
            self.last_public_ticker_stage = "FUTURES_TICKERS"
            futures = self.futures_public("tickers")
        else:
            futures = {"tickers": []}
        self.last_public_ticker_stage = "COMPLETE"
        return merged_spot, futures

    def futures_chart_candles(
        self,
        *,
        symbol: str,
        tick_type: str = "trade",
        resolution: str = "1m",
        count: int = 250,
    ) -> dict[str, Any]:
        if tick_type not in {"spot", "mark", "trade"}:
            raise KrakenError("INVALID_FUTURES_CANDLE_TICK_TYPE")
        if resolution not in {
            "1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"
        }:
            raise KrakenError("INVALID_FUTURES_CANDLE_RESOLUTION")
        params = {"count": max(0, min(int(count), 5000))}
        query = urlencode(params)
        return self.http.request(
            f"{self.FUTURES_CHARTS}/{tick_type}/{symbol}/{resolution}?{query}"
        )

    def public_status(self):
        return self.spot_public("SystemStatus")

    def spot_balance(self):
        return self.spot_private("Balance")

    def spot_open_orders(self):
        return self.spot_private("OpenOrders")

    def spot_open_positions(self):
        return self.spot_private("OpenPositions", {"docalcs": "true"})

    def spot_trades_history(self, params: dict[str, Any] | None = None):
        return self.spot_private("TradesHistory", params)

    def futures_accounts(self):
        return self.futures_private("accounts")

    def futures_open_orders(self):
        return self.futures_private("openorders")

    def futures_open_positions(self):
        return self.futures_private("openpositions")

    def api_permissions(self):
        return self.spot_private("GetApiKeyInfo")

    def websocket_token(self):
        return self.spot_private("GetWebSocketsToken")

    def submit_spot_order(
        self,
        *,
        instrument_id: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        client_order_id: str,
        leverage: Decimal = Decimal("1"),
        margin: bool = False,
        reduce_only: bool = False,
        post_only: bool = False,
        asset_class: str | None = None,
    ):
        side_value = str(side).strip().lower()
        kind = str(order_type).strip().lower()
        if side_value not in {"buy", "sell"}:
            raise KrakenError(f"INVALID_SPOT_ORDER_SIDE:{side_value}")
        if kind not in {"market", "limit"}:
            raise KrakenError(f"UNSUPPORTED_SPOT_ORDER_TYPE:{kind}")
        if quantity <= Decimal("0"):
            raise KrakenError("INVALID_SPOT_ORDER_QUANTITY")
        if kind == "limit" and (price is None or price <= Decimal("0")):
            raise KrakenError("SPOT_LIMIT_ORDER_REQUIRES_POSITIVE_PRICE")
        body = {
            "pair": instrument_id,
            "type": side_value,
            "ordertype": kind,
            "volume": str(quantity),
            "cl_ord_id": client_order_id,
        }
        # Spot market orders don't accept/use a limit-price field.
        if kind == "limit" and price is not None:
            body["price"] = str(price)
        if asset_class == "tokenized_asset":
            body["asset_class"] = "tokenized_asset"
        if margin and leverage > Decimal("1"):
            body["leverage"] = str(leverage)
        elif not margin and leverage > Decimal("1"):
            raise KrakenError(
                "INVALID_MARGIN_ARGUMENT: leverage requires a margin order"
            )
        if reduce_only and margin:
            # Kraken Spot reduce_only is valid only for a genuinely leveraged
            # order. Passing margin=True with leverage=1 omits the leverage
            # field above and Kraken rejects reduce_only as an invalid argument.
            # Do not silently downgrade such a close to a risk-increasing order.
            if leverage <= Decimal("1"):
                raise KrakenError(
                    "INVALID_REDUCE_ONLY_REQUIRES_LEVERAGED_MARGIN_ORDER"
                )
            body["reduce_only"] = "true"
        if post_only:
            if kind != "limit":
                raise KrakenError(
                    "INVALID_POST_ONLY_ARGUMENT: Spot post-only requires ordertype=limit"
                )
            body["oflags"] = "post"
        return self.spot_private("AddOrder", body)

    @staticmethod
    def _normalize_futures_order_type(order_type: str, post_only: bool) -> str:
        raw = str(order_type).strip().lower()
        if post_only:
            if raw not in {"limit", "lmt", "post"}:
                raise KrakenError(
                    "INVALID_FUTURES_ORDER_TYPE: post-only requires a limit order"
                )
            return "post"
        mapping = {
            "limit": "lmt",
            "lmt": "lmt",
            "market": "mkt",
            "mkt": "mkt",
            "ioc": "ioc",
            "fok": "fok",
            "post": "post",
        }
        if raw not in mapping:
            raise KrakenError(f"INVALID_FUTURES_ORDER_TYPE:{raw}")
        return mapping[raw]

    def submit_futures_order(
        self,
        *,
        instrument_id: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        client_order_id: str,
        reduce_only: bool = False,
        post_only: bool = False,
    ):
        side_value = str(side).strip().lower()
        if side_value not in {"buy", "sell"}:
            raise KrakenError(f"INVALID_FUTURES_ORDER_SIDE:{side_value}")
        if quantity <= Decimal("0"):
            raise KrakenError("INVALID_FUTURES_ORDER_QUANTITY")
        kind = self._normalize_futures_order_type(order_type, post_only)
        if kind in {"lmt", "post", "ioc", "fok"} and (price is None or price <= Decimal("0")):
            raise KrakenError(f"FUTURES_{kind.upper()}_ORDER_REQUIRES_POSITIVE_LIMIT_PRICE")
        body = {
            "orderType": kind,
            "symbol": instrument_id,
            "side": side_value,
            "size": str(quantity),
            "cliOrdId": client_order_id,
        }
        # Kraken's mkt order is IOC with price protection; omit limitPrice.
        if kind != "mkt" and price is not None:
            body["limitPrice"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "true"
        return self.futures_private("sendorder", body)

    def lookup_order(self, *, client_order_id: str, instrument: Any, kraken_order_id: str | None = None):
        if getattr(instrument.product_type, "value", "") == "DERIVATIVE":
            result = self.futures_private("ordersstatus", {"cliOrdId": client_order_id})
            return list(result.get("orders") or [])
        # Spot QueryOrders is keyed by Kraken order/transaction id. Kraken's
        # client-order-id support is for order placement/cancel flow, not a
        # reliable historical QueryOrders filter. Prefer the persisted Kraken
        # order id so stale ACKNOWLEDGED orders can be resolved after restart.
        if not kraken_order_id:
            # For a fresh ambiguous submission the exchange order id may not
            # have been returned yet. OpenOrders still exposes cl_ord_id, so it
            # is safe to resolve an actually-open order without guessing.
            open_orders = self.spot_open_orders().get("open", {})
            matches = []
            for txid, order in (open_orders or {}).items():
                if isinstance(order, dict) and str(order.get("cl_ord_id") or "") == client_order_id:
                    matches.append({**order, "txid": str(txid)})
            if matches:
                return matches
            # A closed historical order cannot be safely identified without its
            # Kraken transaction id. Keep the order ambiguous instead of
            # falsely declaring it absent.
            raise KrakenError("ORDER_ID_REQUIRED_FOR_HISTORICAL_RECONCILIATION")
        result = self.spot_private("QueryOrders", {"txid": kraken_order_id})
        # Spot QueryOrders returns an object keyed by Kraken txid. Normalize
        # each order so the reconciler sees status and the Kraken identifier.
        if not isinstance(result, dict):
            return []
        normalized = []
        for txid, order in result.items():
            if isinstance(order, dict):
                normalized.append({**order, "txid": str(txid)})
        return normalized

    def cancel_order(
        self,
        *,
        instrument: Any,
        kraken_order_id: str | None,
        client_order_id: str,
    ):
        if getattr(instrument.product_type, "value", "") == "DERIVATIVE":
            params = {"order_id": kraken_order_id} if kraken_order_id else {"cliOrdId": client_order_id}
            return self.futures_private("cancelorder", params)
        params = {"txid": kraken_order_id} if kraken_order_id else {"cl_ord_id": client_order_id}
        return self.spot_private("CancelOrder", params)
