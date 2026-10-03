from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class WatchdogSnapshot:
    cycle_id: str
    stage: str
    timeout_seconds: float
    armed_at: float
    heartbeat_at: float


class RuntimeWatchdog:
    """Independent daemon watchdog for cycle/startup stages.

    It deliberately runs outside the trading call path. When the watched
    stage stops heartbeating beyond its deadline, the owner callback is
    invoked so a wedged process can fail closed instead of remaining stuck.
    """

    def __init__(
        self,
        on_timeout: Callable[[WatchdogSnapshot], None],
        poll_seconds: float = 0.5,
    ) -> None:
        self._on_timeout = on_timeout
        self._poll_seconds = max(0.05, float(poll_seconds))
        self._lock = threading.RLock()
        self._snapshot: WatchdogSnapshot | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="kraken-runtime-watchdog",
            daemon=True,
        )
        self._thread.start()

    def arm(self, cycle_id: str, stage: str, timeout_seconds: float) -> None:
        now = time.monotonic()
        snapshot = WatchdogSnapshot(
            str(cycle_id),
            str(stage),
            max(0.1, float(timeout_seconds)),
            now,
            now,
        )
        with self._lock:
            self._snapshot = snapshot

    def heartbeat(self, cycle_id: str, stage: str) -> None:
        with self._lock:
            current = self._snapshot
            if current is None:
                return
            if current.cycle_id != str(cycle_id) or current.stage != str(stage):
                return
            self._snapshot = replace(current, heartbeat_at=time.monotonic())

    def clear(self, cycle_id: str) -> None:
        with self._lock:
            current = self._snapshot
            if current is not None and current.cycle_id == str(cycle_id):
                self._snapshot = None

    def snapshot(self) -> WatchdogSnapshot | None:
        with self._lock:
            return self._snapshot

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            snapshot = self.snapshot()
            if snapshot is None:
                continue
            if time.monotonic() - snapshot.heartbeat_at <= snapshot.timeout_seconds:
                continue
            with self._lock:
                current = self._snapshot
                if current != snapshot:
                    continue
                self._snapshot = None
            try:
                self._on_timeout(snapshot)
            except Exception:
                # The runtime callback is responsible for the hard fail-safe.
                # Keep the watchdog thread itself alive if a test/dry callback
                # raises unexpectedly.
                continue
