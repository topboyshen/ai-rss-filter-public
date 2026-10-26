"""Process-local single-flight TTL cache for read-only event reports.

Concurrent identical reads share one computation, distinct computations run
one at a time (the work is pure Python under the GIL, so running them in
parallel only makes every request slower), and nothing is kept longer than a
bounded TTL or beyond a small entry count. Failures are never cached.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

from services.event_radar_query import EventRadarUnavailableError


class _Flight:
    __slots__ = ("done", "value", "failed")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.failed = False


class SingleFlightReportCache:
    def __init__(
        self, *, max_entries: int = 24, wait_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._wait_seconds = wait_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._build_slot = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, Hashable, Any]] = OrderedDict()
        self._flights: dict[Hashable, _Flight] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(
        self, key: Hashable, *, ttl: float, marker: Hashable, build: Callable[[], Any],
    ) -> Any:
        """Return a private copy of the cached report, building it at most once.

        An entry is reused only while younger than ``ttl`` and recorded under
        the same data ``marker``; the marker is read by the caller *before*
        any build, so data that lands mid-build invalidates the next read.
        """
        give_up_at = self._clock() + self._wait_seconds
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._clock() - entry[0] < ttl and entry[1] == marker:
                self._entries.move_to_end(key)
                return copy.deepcopy(entry[2])
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()
        if leader:
            return self._lead(key, flight, marker, build, give_up_at)
        if not flight.done.wait(max(0.0, give_up_at - self._clock())) or flight.failed:
            raise EventRadarUnavailableError("event report is busy or failed")
        return copy.deepcopy(flight.value)

    def _lead(self, key, flight, marker, build, give_up_at):
        try:
            if not self._build_slot.acquire(timeout=max(0.0, give_up_at - self._clock())):
                raise EventRadarUnavailableError("event report is busy")
            try:
                started = self._clock()
                value = build()
            finally:
                self._build_slot.release()
            stored = copy.deepcopy(value)
            with self._lock:
                # Age counts from when the data was read, not when it was stored.
                self._entries[key] = (started, marker, stored)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            flight.value = stored
            return value
        except BaseException:
            flight.failed = True
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
            flight.done.set()
