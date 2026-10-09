from __future__ import annotations

import json

from app.kraken.ws import WebSocketSupervisor


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def emit(self, code: str, level: str = "INFO", **payload) -> None:
        self.events.append((code, level, payload))


class FakeRecovery:
    def issue(self, *args, **kwargs) -> None:
        pass


def test_candidate_rotation_deltas_subscriptions_without_reconnecting():
    audit = FakeAudit()
    supervisor = WebSocketSupervisor(audit, FakeRecovery())
    socket = FakeSocket()

    supervisor._update_subscriptions(
        socket,
        ("BTC/USD", "ETH/USD"),
        ("ETH/USD", "SOL/USD"),
    )

    assert len(socket.messages) == 6
    unsubscribes = [m for m in socket.messages if m["method"] == "unsubscribe"]
    subscribes = [m for m in socket.messages if m["method"] == "subscribe"]
    assert {m["params"]["channel"] for m in unsubscribes} == {"ticker", "book", "trade"}
    assert {m["params"]["channel"] for m in subscribes} == {"ticker", "book", "trade"}
    assert all(m["params"]["symbol"] == ["BTC/USD"] for m in unsubscribes)
    assert all(m["params"]["symbol"] == ["SOL/USD"] for m in subscribes)
    assert all("ETH/USD" not in m["params"]["symbol"] for m in socket.messages)

    assert audit.events == [
        (
            "TACTICAL_WS_SUBSCRIPTION_SET_UPDATED",
            "INFO",
            {
                "added_symbols": ["SOL/USD"],
                "removed_symbols": ["BTC/USD"],
                "subscribed_symbols": 2,
            },
        )
    ]


def test_unchanged_candidate_set_sends_no_subscription_messages():
    audit = FakeAudit()
    supervisor = WebSocketSupervisor(audit, FakeRecovery())
    socket = FakeSocket()

    supervisor._update_subscriptions(
        socket,
        ("BTC/USD", "ETH/USD"),
        ("ETH/USD", "BTC/USD"),
    )

    assert socket.messages == []
    assert audit.events == []
