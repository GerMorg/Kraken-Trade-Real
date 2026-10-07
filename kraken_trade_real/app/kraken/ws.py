from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation
import binascii
import json
import threading
import time
from datetime import datetime
from typing import Any

import websocket


D = Decimal


def _decimal(value: Any) -> D:
    try:
        return D(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return D("0")


def _epoch(value: Any) -> float:
    if value is None:
        return time.time()
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return time.time()


def _checksum_number(value: D) -> str:
    text = format(value, "f")
    text = text.replace(".", "").lstrip("0")
    return text or "0"


def _book_checksum(
    bids: dict[D, D],
    asks: dict[D, D],
) -> int:
    ask_rows = sorted(
        ((price, qty) for price, qty in asks.items() if qty > 0),
        key=lambda item: item[0],
    )[:10]
    bid_rows = sorted(
        ((price, qty) for price, qty in bids.items() if qty > 0),
        key=lambda item: item[0],
        reverse=True,
    )[:10]
    text = "".join(
        _checksum_number(price) + _checksum_number(qty)
        for price, qty in ask_rows + bid_rows
    )
    return binascii.crc32(text.encode("utf-8")) & 0xFFFFFFFF


class WebSocketSupervisor:
    """Maintain bounded, checksum-validated Kraken Spot WS v2 market state."""

    def __init__(
        self,
        audit: Any,
        recovery: Any,
        token_provider: Any = None,
    ) -> None:
        self.audit = audit
        self.recovery = recovery
        self.token_provider = token_provider
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._ws: Any = None
        self._symbols: tuple[str, ...] = ()
        self._generation = 0
        self._connected = False
        self._last_message_at = 0.0
        self._last_error = ""
        self._state: dict[str, dict[str, Any]] = {}
        self._book_bids: dict[str, dict[D, D]] = {}
        self._book_asks: dict[str, dict[D, D]] = {}
        self._book_checksums: dict[str, int] = {}
        self._book_ready: dict[str, bool] = {}
        self._book_desync = False
        self._book_depth = 10

    def set_symbols(self, symbols: list[str]) -> None:
        clean = tuple(dict.fromkeys(
            symbol.strip() for symbol in symbols if isinstance(symbol, str) and symbol.strip()
        ))
        with self._lock:
            if clean == self._symbols:
                return
            self._symbols = clean
            self._generation += 1
            self._state = {symbol: self._state.get(symbol, {}) for symbol in clean}
            self._book_bids = {
                symbol: self._book_bids.get(symbol, {}) for symbol in clean
            }
            self._book_asks = {
                symbol: self._book_asks.get(symbol, {}) for symbol in clean
            }
            self._book_checksums = {
                symbol: self._book_checksums.get(symbol, 0) for symbol in clean
            }
            self._book_ready = {symbol: False for symbol in clean}
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def symbols(self) -> tuple[str, ...]:
        with self._lock:
            return self._symbols

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "symbols": len(self._symbols),
                "last_message_at": self._last_message_at,
                "last_error": self._last_error,
                "state_symbols": len(self._state),
            }

    def snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            result: dict[str, dict[str, Any]] = {}
            for symbol in self._symbols:
                source = self._state.get(symbol, {})
                bids = self._sorted_book(
                    self._book_bids.get(symbol, {}),
                    reverse=True,
                )
                asks = self._sorted_book(
                    self._book_asks.get(symbol, {}),
                    reverse=False,
                )
                ready = bool(self._book_ready.get(symbol, False))
                result[symbol] = {
                    **source,
                    "bids": bids[: self._book_depth] if ready else [],
                    "asks": asks[: self._book_depth] if ready else [],
                    "book_ready": ready,
                    "trades": list(source.get("trades", ())),
                }
            return result

    @staticmethod
    def _sorted_book(
        side: dict[D, D],
        *,
        reverse: bool,
    ) -> list[tuple[D, D]]:
        return sorted(
            ((price, qty) for price, qty in side.items() if price > 0 and qty > 0),
            key=lambda item: item[0],
            reverse=reverse,
        )

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="kraken-ws-v2",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def on_message(self, payload: str, private: bool = False) -> None:
        try:
            data = json.loads(payload, parse_float=Decimal)
        except json.JSONDecodeError:
            self._record_error("WS_INVALID_JSON")
            self.recovery.issue("DATA_STALE", "WS_INVALID_JSON")
            return
        self._process(data)

    def _process(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        channel = str(data.get("channel") or "")
        if channel in {"status", "heartbeat"}:
            return
        if data.get("success") is False:
            error = str(data.get("error") or "SUBSCRIPTION_FAILED")
            self._record_error(error)
            self.audit.emit(
                "PUBLIC_WS_SUBSCRIPTION_FAILED",
                "WARNING",
                error=error,
            )
            return
        rows = data.get("data")
        if not isinstance(rows, list):
            return
        message_type = str(data.get("type") or "")
        self._last_message()
        if channel == "ticker":
            for row in rows:
                if isinstance(row, dict):
                    self._process_ticker(row)
        elif channel == "trade":
            for row in rows:
                if isinstance(row, dict):
                    self._process_trade(row)
        elif channel == "book":
            for row in rows:
                if isinstance(row, dict):
                    self._process_book(row, message_type)

    def _last_message(self) -> None:
        with self._lock:
            self._last_message_at = time.time()
            self._last_error = ""

    def _record_error(self, error: str) -> None:
        with self._lock:
            self._last_error = error[:400]

    def _ensure_state(self, symbol: str) -> dict[str, Any]:
        state = self._state.setdefault(symbol, {})
        state.setdefault("trades", deque(maxlen=1000))
        return state

    def _process_ticker(self, row: dict[str, Any]) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return
        with self._lock:
            state = self._ensure_state(symbol)
            state.update(
                {
                    "bid": _decimal(row.get("bid")),
                    "ask": _decimal(row.get("ask")),
                    "last": _decimal(row.get("last")),
                    "volume_24h": _decimal(row.get("volume")),
                    "change_pct": _decimal(row.get("change_pct")),
                    "high": _decimal(row.get("high")),
                    "low": _decimal(row.get("low")),
                    "timestamp": _epoch(row.get("timestamp")),
                }
            )

    def _process_trade(self, row: dict[str, Any]) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return
        event = {
            "trade_id": str(row.get("trade_id") or ""),
            "side": str(row.get("side") or "").lower(),
            "qty": str(row.get("qty") or "0"),
            "price": str(row.get("price") or "0"),
            "timestamp_epoch": _epoch(row.get("timestamp")),
            "ord_type": str(row.get("ord_type") or ""),
        }
        with self._lock:
            state = self._ensure_state(symbol)
            trades = state["trades"]
            if trades and event["trade_id"] and trades[-1].get("trade_id") == event["trade_id"]:
                return
            trades.append(event)
            state["last_trade_at"] = event["timestamp_epoch"]

    def _process_book(self, row: dict[str, Any], message_type: str) -> None:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            return
        with self._lock:
            bids = self._book_bids.setdefault(symbol, {})
            asks = self._book_asks.setdefault(symbol, {})
            if message_type == "snapshot":
                bids.clear()
                asks.clear()
            self._apply_levels(bids, row.get("bids"), descending=True)
            self._apply_levels(asks, row.get("asks"), descending=False)
            self._truncate(bids, descending=True)
            self._truncate(asks, descending=False)
            checksum = row.get("checksum")
            if checksum is not None:
                expected = int(checksum)
                actual = _book_checksum(bids, asks)
                self._book_checksums[symbol] = actual
                if actual != expected:
                    self._book_ready[symbol] = False
                    self.audit.emit(
                        "PUBLIC_WS_BOOK_CHECKSUM_MISMATCH",
                        "ERROR",
                        symbol=symbol,
                        expected=expected,
                        actual=actual,
                    )
                    bids.clear()
                    asks.clear()
                    self.recovery.issue("SEQUENCE_GAP", f"book_checksum:{symbol}")
                    self._book_desync = True
                    return
                self._book_ready[symbol] = True

    def _apply_levels(
        self,
        side: dict[D, D],
        rows: Any,
        *,
        descending: bool,
    ) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = _decimal(row.get("price"))
            qty = _decimal(row.get("qty"))
            if price <= 0:
                continue
            if qty <= 0:
                side.pop(price, None)
            else:
                side[price] = qty

    def _truncate(self, side: dict[D, D], *, descending: bool) -> None:
        ordered = sorted(side, reverse=descending)
        for price in ordered[self._book_depth :]:
            side.pop(price, None)

    def _run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                with self._lock:
                    symbols = list(self._symbols)
                    generation = self._generation
                if not symbols:
                    self.stop_event.wait(1.0)
                    continue
                ws = websocket.create_connection(
                    "wss://ws.kraken.com/v2",
                    timeout=5,
                    enable_multithread=True,
                )
                with self._lock:
                    self._ws = ws
                    self._connected = True
                    self._last_error = ""
                    self._book_desync = False
                subscriptions = (
                    {
                        "method": "subscribe",
                        "params": {
                            "channel": "ticker",
                            "symbol": symbols,
                            "event_trigger": "bbo",
                            "snapshot": True,
                        },
                    },
                    {
                        "method": "subscribe",
                        "params": {
                            "channel": "trade",
                            "symbol": symbols,
                            "snapshot": True,
                        },
                    },
                    {
                        "method": "subscribe",
                        "params": {
                            "channel": "book",
                            "symbol": symbols,
                            "depth": self._book_depth,
                            "snapshot": True,
                        },
                    },
                )
                for request in subscriptions:
                    ws.send(json.dumps(request))
                self.audit.emit(
                    "PUBLIC_WS_CONNECTED",
                    "INFO",
                    symbols=len(symbols),
                    channels=["ticker", "trade", "book"],
                )
                delay = 1.0
                while not self.stop_event.is_set():
                    with self._lock:
                        changed = generation != self._generation
                        desync = self._book_desync
                    if changed or desync:
                        break
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception as exc:
                        self._record_error(f"{type(exc).__name__}:{str(exc)[:250]}")
                        break
                    if message:
                        self.on_message(message)
                try:
                    ws.close()
                except Exception:
                    pass
                with self._lock:
                    if self._ws is ws:
                        self._ws = None
                    self._connected = False
            except Exception as exc:
                self._record_error(f"{type(exc).__name__}:{str(exc)[:250]}")
                self.audit.emit(
                    "PUBLIC_WS_RECONNECT",
                    "WARNING",
                    error_type=type(exc).__name__,
                )
                self.recovery.issue(
                    "KRAKEN_UNAVAILABLE",
                    f"WS:{type(exc).__name__}",
                )
            self.stop_event.wait(delay)
            delay = min(60.0, delay * 2)
