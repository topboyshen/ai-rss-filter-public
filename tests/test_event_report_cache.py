"""Single-flight TTL cache contract, with no database and no real waiting."""

import threading
import time

import pytest

from services.event_radar_query import EventRadarUnavailableError
from services.event_report_cache import SingleFlightReportCache


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _run(count, target):
    errors, results = [], []

    def work(index):
        try:
            results.append(target(index))
        except Exception as exc:  # noqa: BLE001 - the assertion reads the outcome
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
    [thread.start() for thread in threads]
    [thread.join(timeout=10) for thread in threads]
    return results, errors


def test_identical_concurrent_reads_share_one_build_and_get_private_copies():
    cache = SingleFlightReportCache()
    builds, release = [], threading.Event()

    def build():
        builds.append(1)
        release.wait(5)
        return {"title": "x", "cards": [1, 2]}

    def read(_):
        report = cache.get("k", ttl=30, marker=(1,), build=build)
        report["cards"].append("mutated by this reader")
        return report

    timer = threading.Timer(0.3, release.set)
    timer.start()
    results, errors = _run(6, read)
    timer.cancel()
    assert errors == [] and len(builds) == 1 and len(results) == 6
    assert cache.get("k", ttl=30, marker=(1,), build=build) == {"title": "x", "cards": [1, 2]}
    assert len(builds) == 1


def test_distinct_keys_never_build_in_parallel():
    cache, active, peak, lock = SingleFlightReportCache(), [0], [0], threading.Lock()

    def build():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return {}

    results, errors = _run(5, lambda i: cache.get(i, ttl=30, marker=(), build=build))
    assert errors == [] and len(results) == 5 and peak[0] == 1


def test_entry_expires_by_age_and_by_marker_but_not_before():
    clock, builds = Clock(), []
    cache = SingleFlightReportCache(clock=clock)

    def get(marker):
        return cache.get("k", ttl=30, marker=marker, build=lambda: builds.append(1) or {"n": len(builds)})

    assert get((1,))["n"] == 1
    clock.now += 29.9
    assert get((1,))["n"] == 1
    clock.now += 0.2  # 30.1 seconds after the data was read
    assert get((1,))["n"] == 2
    assert get((2,))["n"] == 3  # new data marker invalidates a young entry
    assert get((1,))["n"] == 4  # and an older marker is not served back
    assert len(builds) == 4


def test_age_counts_from_the_start_of_the_build_not_from_its_end():
    clock = Clock()
    cache = SingleFlightReportCache(clock=clock)

    def slow():
        clock.now += 25
        return {}

    cache.get("k", ttl=30, marker=(), build=slow)
    clock.now += 6
    built = []
    cache.get("k", ttl=30, marker=(), build=lambda: built.append(1) or {})
    assert built == [1]


def test_failures_are_not_cached_and_waiters_fail_closed():
    cache, release, attempts = SingleFlightReportCache(), threading.Event(), []

    def build():
        attempts.append(1)
        release.wait(5)
        raise EventRadarUnavailableError("deadline")

    timer = threading.Timer(0.3, release.set)
    timer.start()
    results, errors = _run(4, lambda _: cache.get("k", ttl=30, marker=(), build=build))
    timer.cancel()
    assert results == [] and len(errors) == 4
    assert all(isinstance(error, EventRadarUnavailableError) for error in errors)
    assert len(cache) == 0
    assert cache.get("k", ttl=30, marker=(), build=lambda: {"ok": True}) == {"ok": True}


def test_waiter_gives_up_with_unavailable_and_leader_still_finishes():
    cache = SingleFlightReportCache(wait_seconds=0.2)
    started, release = threading.Event(), threading.Event()

    def leader_build():
        started.set()
        release.wait(5)
        return {"leader": True}

    leader, outcome = [], []
    thread = threading.Thread(
        target=lambda: leader.append(cache.get("k", ttl=30, marker=(), build=leader_build))
    )
    thread.start()
    assert started.wait(5)
    with pytest.raises(EventRadarUnavailableError):
        cache.get("k", ttl=30, marker=(), build=lambda: outcome.append("second build") or {})
    release.set()
    thread.join(5)
    assert leader == [{"leader": True}] and outcome == []


def test_queued_build_times_out_instead_of_piling_up_and_can_retry():
    cache = SingleFlightReportCache(wait_seconds=0.2)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return {}

    thread = threading.Thread(target=lambda: cache.get("a", ttl=30, marker=(), build=slow))
    thread.start()
    assert started.wait(5)
    ran = []
    with pytest.raises(EventRadarUnavailableError):
        cache.get("b", ttl=30, marker=(), build=lambda: ran.append(1) or {})
    assert ran == []
    release.set()
    thread.join(5)
    assert cache.get("b", ttl=30, marker=(), build=lambda: {"b": 1}) == {"b": 1}


def test_entry_count_is_bounded_and_least_recently_used_goes_first():
    cache, builds = SingleFlightReportCache(max_entries=3), []
    for key in range(10):
        cache.get(key, ttl=30, marker=(), build=lambda key=key: builds.append(key) or {"k": key})
    assert len(cache) == 3
    cache.get(7, ttl=30, marker=(), build=lambda: builds.append("again") or {})
    assert "again" not in builds  # still held
    cache.get(0, ttl=30, marker=(), build=lambda: builds.append("evicted") or {})
    assert builds[-1] == "evicted"
