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
    ) -> dict[str, Any]:
        req = Request(
            url,
            data=data.encode("utf-8") if isinstance(data, str) else data,
            headers={
                "Accept": "application/json",
                "User-Agent": "Kraken-Trade-Real/0.1.3",
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urlopen(req, timeout=self.timeout) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = exc.read(512).decode("utf-8", errors="replace").replace("\n", " ")[:400]
            except OSError:
                detail = ""
            suffix = f":{detail}" if detail else ""
            raise KrakenError(f"HTTP_{exc.code}{suffix}") from exc
        except URLError as exc:
            raise KrakenAmbiguous(f"NETWORK:{exc.reason}") from exc
        if not isinstance(payload, dict):
            raise KrakenError("INVALID_JSON_ROOT")
        return payload


class KrakenGateway:
    SPOT = "https://api.kraken.com"
    FUTURES = "https://futures.kraken.com/derivatives"

    def __init__(self, api_key: str, api_secret: str, timeout: float = 15.0, futures_enabled: bool = False, futures_api_key: str = "", futures_api_secret: str = "") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.futures_enabled = futures_enabled
        self.futures_api_key = futures_api_key
        self.futures_api_secret = futures_api_secret
        self.http = HTTP(timeout)
        self._nonce = int(time.time() * 1000)
        self._lock = threading.Lock()

    def _next_nonce(self) -> int:
        with self._lock:
            self._nonce = max(self._nonce + 1, int(time.time() * 1000))
            return self._nonce

    def spot_public(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urlencode(params or {}, doseq=True)
        url = f"{self.SPOT}/0/public/{method}" + (f"?{query}" if query else "")
        result = self.http.request(url)
        errors = result.get("error")
        if errors:
            detail = ";".join(str(item) for item in errors)[:800]
            raise KrakenError(f"KRAKEN_PUBLIC:{detail}")
        return result.get("result") or {}

    def spot_private(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
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
        body.setdefault("nonce", self._next_nonce())
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
        spot = self.spot_public("AssetPairs")
        futures = self.futures_public("instruments") if self.futures_enabled else {"instruments": []}
        return spot, futures

    def public_tickers(self):
        spot = self.spot_public("Ticker")
        futures = self.futures_public("tickers") if self.futures_enabled else {"tickers": []}
        return spot, futures

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
    ):
        body = {
            "pair": instrument_id,
            "type": side.lower(),
            "ordertype": order_type,
            "volume": str(quantity),
            "cl_ord_id": client_order_id,
        }
        if price is not None:
            body["price"] = str(price)
        if margin:
            body["leverage"] = str(leverage)
        if reduce_only:
            body["reduce_only"] = "true"
        if post_only:
            body["oflags"] = "post"
        return self.spot_private("AddOrder", body)

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
        body = {
            "orderType": order_type,
            "symbol": instrument_id,
            "side": side.lower(),
            "size": str(quantity),
            "cliOrdId": client_order_id,
        }
        if price is not None:
            body["limitPrice"] = str(price)
        if reduce_only:
            body["reduceOnly"] = "true"
        if post_only:
            body["postOnly"] = "true"
        return self.futures_private("sendorder", body)

    def lookup_order(self, *, client_order_id: str, instrument: Any):
        if getattr(instrument.product_type, "value", "") == "DERIVATIVE":
            result = self.futures_private("ordersstatus", {"cliOrdId": client_order_id})
            return list(result.get("orders") or [])
        result = self.spot_private("QueryOrders", {"cl_ord_id": client_order_id})
        return [result] if result else []

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
