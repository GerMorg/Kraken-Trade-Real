from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp


class WebsocketSupervisor:
    """Exchange event supervisor; it observes and requests recovery but cannot submit orders."""

    SPOT_PUBLIC = "wss://ws.kraken.com/v2"
    SPOT_PRIVATE = "wss://ws-auth.kraken.com/v2"
    FUTURES_PUBLIC = "wss://futures.kraken.com/ws/v1"

    def __init__(self, gateway, on_event: Callable[[dict[str, Any]], Awaitable[None]], on_gap: Callable[[], Awaitable[None]], reconnect_base: float = 1.0, reconnect_max: float = 60.0) -> None:
        self.gateway = gateway
        self.on_event = on_event
        self.on_gap = on_gap
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self.running = False
        self.public_healthy = False
        self.private_healthy = False
        self.futures_healthy = False
        self._tasks: list[asyncio.Task] = []
        self._last_sequence: dict[str, int] = {}

    async def start(self, symbols: list[str], futures_symbols: list[str] | None = None) -> None:
        if self.running:
            return
        self.running = True
        self._tasks = [asyncio.create_task(self._loop_public(symbols), name="kraken_public_ws")]
        if self.gateway.api_key:
            self._tasks.append(asyncio.create_task(self._loop_private(), name="kraken_private_ws"))
        if futures_symbols:
            self._tasks.append(asyncio.create_task(self._loop_futures(futures_symbols), name="kraken_futures_ws"))

    async def stop(self) -> None:
        self.running = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop_public(self, symbols: list[str]) -> None:
        await self._loop_with_reconnect(self._public_session, symbols, "PUBLIC_WS_DISCONNECTED")

    async def _public_session(self, session: aiohttp.ClientSession, symbols: list[str]) -> None:
        async with session.ws_connect(self.SPOT_PUBLIC, heartbeat=20) as ws:
            for start in range(0, len(symbols), 100):
                await ws.send_json({"method": "subscribe", "params": {"channel": "ticker", "symbol": symbols[start:start + 100], "event_trigger": "bbo"}})
            for start in range(0, len(symbols), 50):
                await ws.send_json({"method": "subscribe", "params": {"channel": "book", "symbol": symbols[start:start + 50], "depth": 10, "snapshot": True, "level3": False}})
            self.public_healthy = True
            await self.on_event({"event_code": "PUBLIC_WS_CONNECTED"})
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(msg.data), private=False)
                elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    raise ConnectionError("public websocket closed")

    async def _loop_private(self) -> None:
        backoff = self.reconnect_base
        while self.running:
            try:
                token_result = await self.gateway.spot_private("GetWebSocketsToken")
                token = str(token_result.get("token", ""))
                if not token:
                    raise ConnectionError("missing websocket token")
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.SPOT_PRIVATE, heartbeat=20) as ws:
                        await ws.send_json({"method": "subscribe", "params": {"channel": "executions", "snap_orders": True, "token": token}})
                        self.private_healthy = True
                        backoff = self.reconnect_base
                        await self.on_event({"event_code": "PRIVATE_WS_CONNECTED"})
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(json.loads(msg.data), private=True)
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                raise ConnectionError("private websocket closed")
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, json.JSONDecodeError):
                self.private_healthy = False
                await self.on_event({"event_code": "PRIVATE_WS_DISCONNECTED"})
                await asyncio.sleep(backoff)
                backoff = min(self.reconnect_max, backoff * 2)

    async def _loop_futures(self, symbols: list[str]) -> None:
        backoff = self.reconnect_base
        while self.running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.FUTURES_PUBLIC, heartbeat=20) as ws:
                        for start in range(0, len(symbols), 100):
                            await ws.send_json({"event": "subscribe", "feed": "ticker", "product_ids": symbols[start:start + 100]})
                        self.futures_healthy = True
                        backoff = self.reconnect_base
                        await self.on_event({"event_code": "FUTURES_WS_CONNECTED"})
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self.on_event(json.loads(msg.data))
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                raise ConnectionError("futures websocket closed")
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, json.JSONDecodeError):
                self.futures_healthy = False
                await self.on_event({"event_code": "FUTURES_WS_DISCONNECTED"})
                await asyncio.sleep(backoff)
                backoff = min(self.reconnect_max, backoff * 2)

    async def _loop_with_reconnect(self, callback, arg, disconnected_code: str) -> None:
        backoff = self.reconnect_base
        while self.running:
            try:
                async with aiohttp.ClientSession() as session:
                    await callback(session, arg)
                if self.running:
                    raise ConnectionError("websocket ended without stop request")
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, json.JSONDecodeError):
                self.public_healthy = False
                await self.on_event({"event_code": disconnected_code})
                await asyncio.sleep(backoff)
                backoff = min(self.reconnect_max, backoff * 2)

    async def _handle_message(self, message: dict[str, Any], private: bool) -> None:
        if message.get("sequence") is not None:
            channel = str(message.get("channel", "unknown"))
            sequence = int(message["sequence"])
            previous = self._last_sequence.get(channel)
            if previous is not None and sequence > previous + 1:
                await self.on_gap()
                await self.on_event({"event_code": "PRIVATE_SEQUENCE_GAP" if private else "PUBLIC_SEQUENCE_GAP", "required": previous + 1, "actual": sequence})
            self._last_sequence[channel] = sequence
        await self.on_event(message)
