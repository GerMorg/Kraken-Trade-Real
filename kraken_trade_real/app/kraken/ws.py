from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import threading
import time
from decimal import Decimal
from typing import Any, Callable

import websocket


D = Decimal


class SequenceTracker:
    def __init__(self) -> None:
        self.last: int | None = None

    def observe(self, sequence: int | None) -> bool:
        if sequence is None:
            return True
        ok = self.last is None or sequence == self.last + 1
        self.last = sequence
        return ok

    def reset(self) -> None:
        self.last = None


class WebSocketSupervisor:
    """Public Kraken WS v2 market stream used by the tactical strategy.

    The stream is deliberately scoped to a small candidate set. The normal
    five-minute REST cycle remains the source for full-universe discovery.
    """

    def __init__(
        self,
        audit: Any,
        recovery: Any,
        token_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.audit = audit
        self.recovery = recovery
        self.token_provider = token_provider
        self.stop_event = threading.Event()
        self._resubscribe = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._symbols: tuple[str, ...] = ()
        self._subscribed_symbols: tuple[str, ...] = ()
        self._logical_to_wire: dict[str, str] = {}
        self._tickers: dict[str, dict[str, Any]] = {}
        self._books: dict[str, dict[str, dict[D, D]]] = {}
        self._trades: dict[str, deque[tuple[float, D, D, str]]] = {}
        self._prices: dict[str, deque[tuple[float, D]]] = {}
        self.market_sequence = SequenceTracker()
        self.private_sequence = SequenceTracker()
        self.connected = False
        self.last_error = ""

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kraken-ws-market",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._resubscribe.set()
        self.connected = False

    def set_symbols(
        self,
        symbols: list[str] | tuple[str, ...],
        aliases: dict[str, str] | None = None,
    ) -> None:
        normalized = tuple(sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()}))
        with self._lock:
            new_aliases = {
                logical: str((aliases or {}).get(logical, logical))
                for logical in normalized
            }
            if normalized == self._symbols and new_aliases == self._logical_to_wire:
                return
            self._symbols = normalized
            self._logical_to_wire = new_aliases
        self._resubscribe.set()

    def symbols(self) -> tuple[str, ...]:
        with self._lock:
            return self._symbols

    def wire_symbols(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(set(self._logical_to_wire.values())))

    def on_message(self, payload: str, private: bool = False) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self.audit.emit("TACTICAL_WS_INVALID_JSON", "WARNING")
            return

        if not isinstance(data, dict):
            return

        if data.get("method") in {"subscribe", "unsubscribe"}:
            success = data.get("success")
            if success is False:
                self.last_error = str(data.get("error") or "WS_SUBSCRIPTION_FAILED")[:300]
                self.audit.emit(
                    "TACTICAL_WS_SUBSCRIPTION_FAILED",
                    "WARNING",
                    method=str(data.get("method")),
                    error=self.last_error,
                    result=data.get("result") or {},
                )
            elif success is True:
                result = data.get("result") or {}
                self.audit.emit(
                    "TACTICAL_WS_SUBSCRIPTION_ACK",
                    "INFO",
                    method=str(data.get("method")),
                    channel=str(result.get("channel") or ""),
                    symbol=str(result.get("symbol") or ""),
                )
            return

        if private:
            seq = data.get("sequence")
            if not self.private_sequence.observe(int(seq) if seq is not None else None):
                self.audit.emit("WS_SEQUENCE_GAP", "ERROR", private=True, sequence=seq)
                self.recovery.issue("SEQUENCE_GAP", "private=True")
            return

        channel = str(data.get("channel") or "")
        message_type = str(data.get("type") or "")
        rows = data.get("data")
        if not isinstance(rows, list):
            rows = []

        if channel == "ticker":
            for row in rows:
                if isinstance(row, dict):
                    self._on_ticker(row)
        elif channel == "book":
            for row in rows:
                if isinstance(row, dict):
                    self._on_book(row, message_type)
        elif channel == "trade":
            for row in rows:
                if isinstance(row, dict):
                    self._on_trade(row)

    def market_snapshot(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            wire_symbol = self._logical_to_wire.get(symbol, symbol)
            ticker = dict(self._tickers.get(wire_symbol, {}))
            if not ticker:
                return None
            book = self._books.get(wire_symbol, {"bids": {}, "asks": {}})
            prices = tuple(self._prices.get(wire_symbol, ()))
            bids = tuple(
                sorted(
                    ((price, qty) for price, qty in book.get("bids", {}).items() if qty > 0),
                    key=lambda item: item[0],
                    reverse=True,
                )[:10]
            )
            asks = tuple(
                sorted(
                    ((price, qty) for price, qty in book.get("asks", {}).items() if qty > 0),
                    key=lambda item: item[0],
                )[:10]
            )

        bid = self._decimal(ticker.get("bid"))
        ask = self._decimal(ticker.get("ask"))
        last = self._decimal(ticker.get("last"))
        if min(bid, ask, last) <= 0:
            return None

        now = time.time()
        prices = tuple(item for item in prices if now - item[0] <= 300)
        closes = tuple(price for _, price in prices[-120:])
        return {
            "symbol": symbol,
            "price": last,
            "bid": bid,
            "ask": ask,
            "timestamp": self._timestamp(ticker.get("timestamp")),
            "closes": closes,
            "price_points": tuple(prices[-300:]),
            "depths_bid": bids,
            "depths_ask": asks,
            "spread_bps": (ask - bid) / ((ask + bid) / 2) * D("10000"),
        }

    def trade_metrics(self, symbol: str, lookback_seconds: float = 900.0) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            wire_symbol = self._logical_to_wire.get(symbol, symbol)
            rows = list(self._trades.get(wire_symbol, ()))
        rows = [row for row in rows if now - row[0] <= lookback_seconds]
        if not rows:
            return {
                "trade_count": 0,
                "recent_quote": D("0"),
                "baseline_quote_per_minute": D("0"),
                "volume_ratio": D("0"),
                "buy_quote": D("0"),
                "sell_quote": D("0"),
                "signed_flow_ratio": D("0"),
                "age_seconds": D("999999"),
            }

        recent_cut = now - 60.0
        recent = [row for row in rows if row[0] >= recent_cut]
        history = [row for row in rows if row[0] < recent_cut]
        recent_quote = sum((price * qty for _, price, qty, _ in recent), D("0"))
        buy_quote = sum((price * qty for _, price, qty, side in recent if side == "buy"), D("0"))
        sell_quote = sum((price * qty for _, price, qty, side in recent if side == "sell"), D("0"))
        if history:
            oldest = max(history[0][0], now - lookback_seconds)
            elapsed_minutes = max(D("1"), D(str(now - oldest)) / D("60"))
            baseline = sum(
                (price * qty for _, price, qty, _ in history),
                D("0"),
            ) / elapsed_minutes
        else:
            baseline = D("0")
        ratio = recent_quote / baseline if baseline > 0 else D("0")
        total_flow = buy_quote + sell_quote
        flow = (buy_quote - sell_quote) / total_flow if total_flow > 0 else D("0")
        return {
            "trade_count": len(rows),
            "recent_quote": recent_quote,
            "baseline_quote_per_minute": baseline,
            "volume_ratio": ratio,
            "buy_quote": buy_quote,
            "sell_quote": sell_quote,
            "signed_flow_ratio": flow,
            "age_seconds": D(str(max(0.0, now - rows[-1][0]))),
        }

    def _on_ticker(self, row: dict[str, Any]) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return
        last = self._decimal(row.get("last"))
        ts = self._timestamp(row.get("timestamp"))
        with self._lock:
            self._tickers[symbol] = row
            if last > 0:
                prices = self._prices.setdefault(symbol, deque(maxlen=3000))
                if not prices or prices[-1][1] != last or ts > prices[-1][0]:
                    prices.append((ts, last))

    def _on_trade(self, row: dict[str, Any]) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return
        price = self._decimal(row.get("price"))
        qty = self._decimal(row.get("qty"))
        if price <= 0 or qty <= 0:
            return
        ts = self._timestamp(row.get("timestamp"))
        side = str(row.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            side = ""
        with self._lock:
            self._trades.setdefault(symbol, deque(maxlen=20000)).append((ts, price, qty, side))
            self._prices.setdefault(symbol, deque(maxlen=3000)).append((ts, price))

    def _on_book(self, row: dict[str, Any], message_type: str) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return
        bids = row.get("bids") or []
        asks = row.get("asks") or []
        with self._lock:
            book = self._books.setdefault(
                symbol,
                {"bids": {}, "asks": {}},
            )
            if message_type == "snapshot":
                book["bids"].clear()
                book["asks"].clear()
            for side, key in ((bids, "bids"), (asks, "asks")):
                for level in side:
                    if not isinstance(level, dict):
                        continue
                    price = self._decimal(level.get("price"))
                    qty = self._decimal(level.get("qty"))
                    if price <= 0:
                        continue
                    if qty <= 0:
                        book[key].pop(price, None)
                    else:
                        book[key][price] = qty
            for key in ("bids", "asks"):
                if len(book[key]) > 100:
                    ordered = sorted(
                        book[key].items(),
                        key=lambda item: item[0],
                        reverse=(key == "bids"),
                    )[:100]
                    book[key] = dict(ordered)

    @staticmethod
    def _decimal(value: Any) -> D:
        try:
            return D(str(value))
        except Exception:
            return D("0")

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return time.time()
        return time.time()

    def _subscribe(self, ws: Any, symbols: tuple[str, ...]) -> None:
        if not symbols:
            return
        messages = (
            {
                "method": "subscribe",
                "params": {
                    "channel": "ticker",
                    "symbol": list(symbols),
                    "event_trigger": "bbo",
                    "snapshot": True,
                },
            },
            {
                "method": "subscribe",
                "params": {
                    "channel": "book",
                    "symbol": list(symbols),
                    "depth": 10,
                    "snapshot": True,
                },
            },
            {
                "method": "subscribe",
                "params": {
                    "channel": "trade",
                    "symbol": list(symbols),
                    "snapshot": True,
                },
            },
        )
        for message in messages:
            ws.send(json.dumps(message, separators=(",", ":")))

    def _run(self) -> None:
        reconnect_delay = 1.0
        while not self.stop_event.is_set():
            symbols = self.symbols()
            wire_symbols = self.wire_symbols()
            ws = None
            if not symbols:
                self.connected = False
                self._resubscribe.wait(1.0)
                self._resubscribe.clear()
                continue
            try:
                ws = websocket.create_connection("wss://ws.kraken.com/v2", timeout=10)
                ws.settimeout(1.0)
                self._subscribe(ws, wire_symbols)
                self._subscribed_symbols = symbols
                self.connected = True
                self.last_error = ""
                self.audit.emit(
                    "TACTICAL_WS_CONNECTED",
                    "INFO",
                    symbols=len(symbols),
                    channels=["ticker", "book", "trade"],
                )
                reconnect_delay = 1.0
                while not self.stop_event.is_set():
                    current = self.symbols()
                    if current != self._subscribed_symbols or self._resubscribe.is_set():
                        self._resubscribe.clear()
                        try:
                            ws.close()
                        except Exception as exc:
                            self.audit.emit(
                                "TACTICAL_WS_CLOSE_FAILED",
                                "WARNING",
                                error_type=type(exc).__name__,
                            )
                        break
                    try:
                        payload = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}:{str(exc)[:300]}"
                        break
                    if payload:
                        self.on_message(str(payload))
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}:{str(exc)[:300]}"
                self.audit.emit(
                    "TACTICAL_WS_ERROR",
                    "WARNING",
                    error_type=type(exc).__name__,
                    error=self.last_error,
                )
            finally:
                self.connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception as exc:
                        self.audit.emit(
                            "TACTICAL_WS_CLOSE_FAILED",
                            "WARNING",
                            error_type=type(exc).__name__,
                        )
            self.audit.emit(
                "TACTICAL_WS_DISCONNECTED",
                "WARNING",
                reconnect_seconds=round(reconnect_delay, 2),
            )
            self._resubscribe.wait(reconnect_delay)
            self._resubscribe.clear()
            reconnect_delay = min(30.0, reconnect_delay * 2.0)
