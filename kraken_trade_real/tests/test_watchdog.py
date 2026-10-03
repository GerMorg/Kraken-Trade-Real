from __future__ import annotations

import threading
import time

from app.runtime.watchdog import RuntimeWatchdog


def test_watchdog_timeout_calls_callback_once():
    received=[]
    event=threading.Event()

    def on_timeout(snapshot):
        received.append(snapshot)
        event.set()

    watchdog=RuntimeWatchdog(on_timeout,poll_seconds=0.01)
    try:
        watchdog.arm("cycle-test","TEST_STAGE",0.05)
        assert event.wait(0.5)
        assert len(received) == 1
        assert received[0].cycle_id == "cycle-test"
        assert received[0].stage == "TEST_STAGE"
    finally:
        watchdog.close()


def test_watchdog_heartbeat_prevents_timeout():
    received=[]
    event=threading.Event()

    def on_timeout(snapshot):
        received.append(snapshot)
        event.set()

    watchdog=RuntimeWatchdog(on_timeout,poll_seconds=0.01)
    try:
        watchdog.arm("cycle-test","TEST_STAGE",0.08)
        deadline=time.monotonic()+0.25
        while time.monotonic()<deadline:
            watchdog.heartbeat("cycle-test","TEST_STAGE")
            time.sleep(0.02)
        assert not event.is_set()
        assert received == []
    finally:
        watchdog.close()
