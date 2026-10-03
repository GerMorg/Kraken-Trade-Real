from __future__ import annotations

import threading
import time

from app.kraken.client import KrakenGateway


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.lock = threading.Lock()

    def request(self, url, *, method="GET", data=None, headers=None):
        with self.lock:
            if data and "nonce=" in data:
                self.calls.append(int(dict(part.split("=", 1) for part in data.split("&"))["nonce"]))
        time.sleep(0.001)
        return {"result": {"ok": True}}


def test_spot_nonce_is_high_resolution_and_monotonic():
    gateway = KrakenGateway("key", "c2VjcmV0")
    values = [gateway._next_nonce() for _ in range(100)]
    assert values == sorted(set(values))
    assert values[-1] > int(time.time() * 1000)


def test_spot_private_requests_are_serialized_and_nonce_increases():
    gateway = KrakenGateway("key", "c2VjcmV0")
    fake = FakeHTTP()
    gateway.http = fake
    threads = [
        threading.Thread(target=gateway.spot_private, args=("Balance",))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fake.calls == sorted(fake.calls)
    assert len(fake.calls) == 8


def test_futures_private_does_not_add_spot_nonce():
    gateway = KrakenGateway("key", "c2VjcmV0", futures_enabled=True, futures_api_key="fkey", futures_api_secret="c2VjcmV0")
    fake = FakeHTTP()
    gateway.http = fake
    gateway.futures_private("accounts")
    assert fake.calls == []
