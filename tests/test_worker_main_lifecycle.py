"""Lifecycle contract of ``worker_main.main``: preflight, supervision, shutdown.

Every collaborator that would start a thread, touch Redis or write the health
file is replaced with a recorder, so these tests assert *what the worker does*
and in which order, without sockets, model calls or real background threads.
"""

from __future__ import annotations

import pytest

import worker_main


class _Thread:
    def __init__(self, alive: bool = True):
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class _ContinuationThread(_Thread):
    def join(self, timeout):
        self.alive = False


class _Harness:
    """Records calls and lets a test decide when the supervision loop stops."""

    def __init__(self, monkeypatch, *, ticks: int = 2, quant_enabled: bool = False):
        import routes.dashboard_routes as dashboard
        import routes.shared as shared
        import routes.webhook_routes as webhook
        import services.quant_market_context as quant
        import services.score_continuation_worker as continuation
        import services.shared_cache as cache
        import services.worker_health as health

        self.calls: list[tuple] = []
        self.health_writes: list[dict] = []
        self.handlers: dict[int, object] = {}
        self.role = "worker"
        self.api_key = "k"
        self.redis_enabled = True
        self.redis_ok = True
        self.drained = True
        self.health_error_on: set[int] = set()
        self.ticks = ticks
        self.quant_enabled = quant_enabled
        self.quotes = [_Thread()]
        self.dispatchers = [_Thread()]
        self.quant = [_Thread()]

        monkeypatch.setattr(continuation, "start_score_continuation_worker", lambda stop: _ContinuationThread())
        monkeypatch.setattr(worker_main.production_startup, "enabled", lambda: False)
        monkeypatch.setattr(worker_main, "initialize_runtime_database", lambda role, initialize, phase: initialize())
        record = self.calls.append
        monkeypatch.setattr(cache, "process_role", lambda: self.role)
        monkeypatch.setattr(cache, "redis_enabled", lambda: self.redis_enabled)
        monkeypatch.setattr(cache, "redis_ping", lambda: self.redis_ok)
        monkeypatch.setattr(shared, "init_db", lambda: record(("init_db",)))
        monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": self.api_key})

        def next_thread(seq):
            # Hand threads out in order; the last one is reused afterwards.
            return seq.pop(0) if len(seq) > 1 else seq[0]

        def start_dispatcher():
            record(("start_dispatcher",))
            return next_thread(self.dispatchers)

        def ensure_quotes():
            record(("ensure_quotes",))
            return next_thread(self.quotes)

        def start_quant():
            record(("start_quant",))
            return next_thread(self.quant) if self.quant_enabled else None

        monkeypatch.setattr(webhook, "start_webhook_dispatcher", start_dispatcher)
        monkeypatch.setattr(
            webhook, "stop_webhook_dispatcher",
            lambda timeout: record(("stop_dispatcher", timeout)) or self.drained,
        )
        monkeypatch.setattr(webhook, "webhook_dispatcher_alive", lambda: True)
        monkeypatch.setattr(dashboard, "_ensure_quotes_refresher", ensure_quotes)
        monkeypatch.setattr(
            dashboard, "stop_quotes_refresher",
            lambda timeout: record(("stop_quotes", timeout)),
        )
        monkeypatch.setattr(quant, "quant_context_enabled", lambda: self.quant_enabled)
        monkeypatch.setattr(quant, "start_quant_market_context_poller", start_quant)
        monkeypatch.setattr(
            quant, "stop_quant_market_context_poller",
            lambda timeout: record(("stop_quant", timeout)),
        )
        monkeypatch.setattr(health, "HEALTH_WRITE_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(
            health, "close_worker_health_resources", lambda: record(("close_health",))
        )
        monkeypatch.setattr(health, "write_worker_health", self._write_health)
        monkeypatch.setattr(
            worker_main, "_bootstrap_dashboard_publication",
            lambda: record(("bootstrap",)),
        )
        monkeypatch.setattr(
            worker_main, "_retry_pending_dashboard_publication",
            lambda: record(("retry_publication",)),
        )
        monkeypatch.setattr(
            worker_main.signal, "signal",
            lambda signum, handler: self.handlers.__setitem__(signum, handler),
        )

    def _write_health(self, alive, *, subsystems=None, running=True,
                      force_dependency_probe=False):
        index = len(self.health_writes)
        self.health_writes.append({
            "alive": alive, "subsystems": dict(subsystems or {}),
            "running": running, "force": force_dependency_probe,
        })
        self.calls.append(("health", running))
        if running and index >= self.ticks:
            # Deliver SIGTERM through the handler main() registered.
            self.handlers[worker_main.signal.SIGTERM](worker_main.signal.SIGTERM, None)
        if index in self.health_error_on:
            raise OSError("health file unavailable")

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def harness(monkeypatch):
    return _Harness(monkeypatch)


@pytest.mark.parametrize("role", ["web", "api", ""])
def test_refuses_non_worker_role_before_touching_the_database(harness, role):
    harness.role = role
    assert worker_main.main() == 2
    assert harness.calls == []


def test_refuses_to_start_without_an_effective_api_key(harness):
    harness.api_key = "   "
    assert worker_main.main() == 2
    assert harness.names() == ["init_db"]


@pytest.mark.parametrize("enabled,reachable", [(False, True), (True, False)])
def test_refuses_to_start_without_reachable_redis(harness, enabled, reachable):
    harness.redis_enabled, harness.redis_ok = enabled, reachable
    assert worker_main.main() == 2
    assert "start_dispatcher" not in harness.names()


def test_all_role_is_accepted(harness):
    harness.role = "all"
    assert worker_main.main() == 0


def test_happy_path_starts_supervises_and_drains_in_order(harness):
    assert worker_main.main() == 0
    names = harness.names()
    # Subsystems start once, before bootstrap and the first health write.
    assert names[:5] == ["init_db", "ensure_quotes", "start_dispatcher", "start_quant", "bootstrap"]
    assert set(harness.handlers) == {worker_main.signal.SIGTERM, worker_main.signal.SIGINT}
    first = harness.health_writes[0]
    assert first == {
        "alive": True, "subsystems": {"quotes": True, "dispatcher": True, "score_continuations": True},
        "running": True, "force": True,
    }
    # Each periodic tick retries pending publication before writing health,
    # and periodic writes do not force the expensive dependency probe.
    periodic = harness.health_writes[1:-1]
    assert periodic and all(not write["force"] for write in periodic)
    assert names.count("retry_publication") == len(periodic)
    # Shutdown: final not-running snapshot, then stop in a fixed order.
    assert harness.health_writes[-1]["running"] is False
    tail = [call for call in harness.calls if call[0] != "health"][-4:]
    assert tail == [
        ("stop_quotes", 15.0), ("stop_quant", 12.0), ("stop_dispatcher", 60.0), ("close_health",),
    ]


def test_dead_subsystems_are_restarted_before_health_is_reported(harness):
    harness.ticks = 1
    harness.quotes = [_Thread(alive=False), _Thread()]
    harness.dispatchers = [_Thread(alive=False), _Thread()]
    assert worker_main.main() == 0
    names = harness.names()
    assert names.count("ensure_quotes") == 2
    assert names.count("start_dispatcher") == 2
    # The dead quotes refresher is stopped without waiting before restart.
    assert ("stop_quotes", 0.0) in harness.calls
    assert names.index("ensure_quotes", 1) < names.index("health")
    assert harness.health_writes[0]["subsystems"] == {"quotes": True, "dispatcher": True, "score_continuations": True}


def test_subsystem_that_cannot_be_restarted_is_reported_dead(harness):
    harness.ticks = 1
    harness.dispatchers = [_Thread(alive=False)]
    assert worker_main.main() == 0
    assert all(
        write["subsystems"].get("dispatcher") is False
        for write in harness.health_writes if write["running"]
    )
    # One start at boot plus one restart attempt per running health write.
    running_writes = sum(1 for write in harness.health_writes if write["running"])
    assert harness.names().count("start_dispatcher") == 1 + running_writes


def test_quant_poller_is_supervised_only_when_enabled(monkeypatch):
    harness = _Harness(monkeypatch, ticks=1, quant_enabled=True)
    harness.quant = [_Thread(alive=False), _Thread()]
    assert worker_main.main() == 0
    assert harness.names().count("start_quant") == 2
    assert ("stop_quant", 0.0) in harness.calls
    assert harness.health_writes[0]["subsystems"] == {
        "quotes": True, "dispatcher": True, "quant_context": True, "score_continuations": True,
    }


def test_disabled_quant_poller_is_not_a_health_subsystem(harness):
    assert worker_main.main() == 0
    assert harness.names().count("start_quant") == 1
    assert all("quant_context" not in write["subsystems"] for write in harness.health_writes)


def test_health_write_failures_never_stop_the_worker(harness, caplog):
    # Initial, one periodic, and the shutdown snapshot all fail.
    harness.ticks = 1
    harness.health_error_on = {0, 1, 2}
    with caplog.at_level("ERROR", logger="worker_main"):
        assert worker_main.main() == 0
    messages = [record.getMessage() for record in caplog.records]
    assert "[Worker] failed to write initial health snapshot" in messages
    assert "[Worker] failed to write health snapshot" in messages
    assert "[Worker] failed to write shutdown health snapshot" in messages
    # Shutdown still drains every subsystem after the failed writes.
    assert harness.calls[-1] == ("close_health",)


def test_undrained_dispatcher_is_logged_but_exit_is_clean(harness, caplog):
    harness.drained = False
    with caplog.at_level("ERROR", logger="worker_main"):
        assert worker_main.main() == 0
    assert "[Worker] shutdown deadline reached; claimed tasks remain leased" in [
        record.getMessage() for record in caplog.records
    ]


def test_sigint_also_stops_the_loop(harness, monkeypatch):
    import services.worker_health as health

    harness.ticks = 10**6  # never self-stop via SIGTERM
    original = harness._write_health

    def write_then_interrupt(*args, **kwargs):
        original(*args, **kwargs)
        if len(harness.health_writes) == 2:
            harness.handlers[worker_main.signal.SIGINT](worker_main.signal.SIGINT, None)

    monkeypatch.setattr(health, "write_worker_health", write_then_interrupt)
    assert worker_main.main() == 0
    assert len(harness.health_writes) == 3
    assert harness.health_writes[-1]["running"] is False


# ---------------------------------------------------------------- bootstrap


def test_bootstrap_submits_one_snapshot_refresh_task(monkeypatch):
    import routes.webhook_routes as webhook
    import services.dashboard_snapshots as snapshots
    import services.release_info as release_info

    submitted, retried = [], []
    monkeypatch.setattr(snapshots, "snapshots_enabled", lambda: True)
    monkeypatch.setattr(release_info, "release_version", lambda: "my-ai-filter:test")
    monkeypatch.setattr(webhook, "_submit_background_task", lambda task: submitted.append(task) or "queued")
    monkeypatch.setattr(worker_main.time, "time", lambda: 1_700_000_123.0)
    monkeypatch.setattr(worker_main, "_retry_pending_dashboard_publication", lambda: retried.append(1))
    worker_main._bootstrap_dashboard_publication()
    assert submitted == [{
        "task_type": "dashboard_snapshot_refresh",
        "snapshot_version": 1,
        "release_version": "my-ai-filter:test",
        "request_window": 1_700_000_123 // 300,
    }]
    assert retried == [1]


def test_bootstrap_skips_disabled_snapshots_but_still_retries_publication(monkeypatch):
    import routes.webhook_routes as webhook
    import services.dashboard_snapshots as snapshots

    submitted, retried = [], []
    monkeypatch.setattr(snapshots, "snapshots_enabled", lambda: False)
    monkeypatch.setattr(webhook, "_submit_background_task", lambda task: submitted.append(task))
    monkeypatch.setattr(worker_main, "_retry_pending_dashboard_publication", lambda: retried.append(1))
    worker_main._bootstrap_dashboard_publication()
    assert submitted == [] and retried == [1]


def test_bootstrap_failure_is_logged_and_does_not_skip_retry(monkeypatch, caplog):
    import services.dashboard_snapshots as snapshots

    retried = []

    def boom():
        raise RuntimeError("snapshot store unavailable")

    monkeypatch.setattr(snapshots, "snapshots_enabled", boom)
    monkeypatch.setattr(worker_main, "_retry_pending_dashboard_publication", lambda: retried.append(1))
    with caplog.at_level("ERROR", logger="worker_main"):
        worker_main._bootstrap_dashboard_publication()
    assert retried == [1]
    assert "[Worker] dashboard snapshot bootstrap failed" in [r.getMessage() for r in caplog.records]


def test_pending_publication_warns_only_when_still_pending(monkeypatch, caplog):
    import services.dashboard_snapshot_updates as updates

    outcomes = iter(["full", "unavailable", "queued", "coalesced"])
    monkeypatch.setattr(updates, "enqueue_pending_realtime_snapshot", lambda: next(outcomes))
    with caplog.at_level("WARNING", logger="worker_main"):
        for _ in range(4):
            worker_main._queue_pending_dashboard_publication()
    assert [r.getMessage() for r in caplog.records] == [
        "[Worker] dashboard publication remains pending: full",
        "[Worker] dashboard publication remains pending: unavailable",
    ]


def test_retry_is_skipped_while_another_caller_holds_the_lock(monkeypatch):
    started = []
    monkeypatch.setattr(worker_main, "_dashboard_retry_thread", None)
    monkeypatch.setattr(
        worker_main.threading, "Thread",
        lambda **kwargs: started.append(kwargs) or _Thread(),
    )
    assert worker_main._dashboard_retry_lock.acquire(blocking=False)
    try:
        worker_main._retry_pending_dashboard_publication()
    finally:
        worker_main._dashboard_retry_lock.release()
    assert started == []
    assert worker_main._dashboard_retry_thread is None
