import threading
import time

import pytest

from services import score_continuation_worker as worker


def test_interval_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv(worker._INTERVAL_ENV, raising=False)
    assert worker.interval_seconds() == worker.DEFAULT_INTERVAL_SECONDS

    monkeypatch.setenv(worker._INTERVAL_ENV, "1")
    assert worker.interval_seconds() == worker.MIN_INTERVAL_SECONDS

    monkeypatch.setenv(worker._INTERVAL_ENV, "99999")
    assert worker.interval_seconds() == worker.MAX_INTERVAL_SECONDS

    monkeypatch.setenv(worker._INTERVAL_ENV, "not-a-number")
    assert worker.interval_seconds() == worker.DEFAULT_INTERVAL_SECONDS


def test_initial_delay_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv(worker._INITIAL_DELAY_ENV, raising=False)
    assert worker.initial_delay_seconds() == worker.DEFAULT_INITIAL_DELAY_SECONDS

    monkeypatch.setenv(worker._INITIAL_DELAY_ENV, "-5")
    assert worker.initial_delay_seconds() == 0

    monkeypatch.setenv(worker._INITIAL_DELAY_ENV, "99999")
    assert worker.initial_delay_seconds() == worker.MAX_INTERVAL_SECONDS


def test_run_once_skips_while_a_refresh_owns_the_lane(monkeypatch):
    monkeypatch.setattr(worker, "_refresh_running", lambda: True)
    monkeypatch.setattr(worker, "_resolve_cfg", lambda: pytest.fail("resolved cfg"))
    monkeypatch.setattr(worker, "_run_batch", lambda cfg: pytest.fail("ran batch"))

    assert worker.run_once() == {"skipped": "refresh_running"}


def test_run_once_uses_the_resolved_config(monkeypatch):
    monkeypatch.setattr(worker, "_refresh_running", lambda: False)
    sentinel = object()
    monkeypatch.setattr(worker, "_resolve_cfg", lambda: sentinel)
    seen = []
    monkeypatch.setattr(
        worker,
        "_run_batch",
        lambda cfg: seen.append(cfg) or {"attempted": 1, "remaining": {"due": 0}},
    )

    result = worker.run_once()

    assert seen == [sentinel]
    assert result["attempted"] == 1


def test_run_once_accepts_an_explicit_config(monkeypatch):
    monkeypatch.setattr(worker, "_refresh_running", lambda: False)
    monkeypatch.setattr(worker, "_resolve_cfg", lambda: pytest.fail("resolved cfg"))
    sentinel = object()
    seen = []
    monkeypatch.setattr(worker, "_run_batch", lambda cfg: seen.append(cfg) or {})

    worker.run_once(cfg=sentinel)

    assert seen == [sentinel]


def test_loop_runs_and_stops_cooperatively(monkeypatch):
    monkeypatch.setenv(worker._INITIAL_DELAY_ENV, "0")
    called = threading.Event()
    monkeypatch.setattr(worker, "run_once", lambda: called.set() or {})

    stop = threading.Event()
    thread = worker.start_score_continuation_worker(stop)
    try:
        assert called.wait(2.0), "timer never ran its first batch"
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_loop_survives_a_failed_batch(monkeypatch):
    monkeypatch.setenv(worker._INITIAL_DELAY_ENV, "0")
    attempts = []
    ran = threading.Event()

    def boom():
        attempts.append(1)
        ran.set()
        raise RuntimeError("score backend unavailable")

    monkeypatch.setattr(worker, "run_once", boom)
    stop = threading.Event()
    thread = worker.start_score_continuation_worker(stop)
    try:
        assert ran.wait(2.0)
        time.sleep(0.05)
        assert thread.is_alive()
        assert len(attempts) == 1
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_worker_reports_the_continuation_subsystem():
    import worker_main

    class Alive:
        def is_alive(self):
            return True

    threads = {
        "quotes": Alive(),
        "dispatcher": Alive(),
        "quant_context": None,
        "score_continuations": Alive(),
    }

    subsystems = worker_main._worker_subsystems(threads, None)

    assert subsystems["score_continuations"] is True
