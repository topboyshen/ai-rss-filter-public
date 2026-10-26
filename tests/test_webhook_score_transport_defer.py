"""A deferred score (backup without verdict) requeues without an attempt, capped."""
import time
from concurrent.futures import Future

from tests.test_webhook_durable_spool import _pending_files, _task, isolated_spool  # noqa: F401


def _finish(webhook_routes, result):
    claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 10_000)
    assert claimed is not None
    path, envelope = claimed
    assert envelope["attempts"] >= 1
    assert webhook_routes._webhook_slots.acquire(blocking=False)
    future = Future()
    future.set_result(result)
    started = time.time()
    webhook_routes._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])
    pending = _pending_files(webhook_routes)
    assert len(pending) == 1
    return webhook_routes._load_spooled_task(pending[0]), started


def test_score_transport_retry_defers_without_attempt_until_cap(isolated_spool):  # noqa: F811
    webhook_routes = isolated_spool
    assert webhook_routes._persist_webhook_task(_task(42)) == "stored"
    for deferral in range(1, webhook_routes._SCORE_TRANSPORT_DEFERRAL_MAX + 1):
        persisted, started = _finish(webhook_routes, webhook_routes._SCORE_TRANSPORT_RETRY)
        assert persisted["attempts"] == 0
        assert persisted["score_transport_deferrals"] == deferral
        assert persisted["last_error"] == webhook_routes._SCORE_TRANSPORT_RETRY
        expected = min(300.0, 60.0 * 2 ** (deferral - 1))
        assert persisted["next_attempt_at"] >= started + expected - 0.5
    persisted, _ = _finish(webhook_routes, webhook_routes._SCORE_TRANSPORT_RETRY)
    assert persisted["attempts"] == 1  # cap reached: an ordinary counted retry
    assert persisted["last_error"] == "score_transport_retry_defer_failed"


def test_score_in_background_returns_marker_and_keeps_cursor(isolated_spool, monkeypatch):  # noqa: F811
    webhook_routes = isolated_spool
    from core.processor import SCORE_DEFERRED_REASON

    seen = []

    def fake_process_entry(wrapper, **_kw):
        seen.append(wrapper.link)
        if wrapper.link.endswith("/2"):
            return None, SCORE_DEFERRED_REASON
        return None, "filtered"

    monkeypatch.setattr(webhook_routes, "process_entry", fake_process_entry)
    monkeypatch.setattr(webhook_routes, "resolve_scoring_contract", lambda cfg, **_k: cfg)
    monkeypatch.setattr(webhook_routes, "resolve_prompt", lambda *_a: ("p", "prompt"))
    monkeypatch.setattr(webhook_routes, "_queue_committed_dashboard_inputs", lambda: None)
    progress = []
    webhook_routes._task_execution_context.start_entry_index = 0
    webhook_routes._task_execution_context.progress_callback = (
        lambda i, p, f: progress.append(i)
    )
    webhook_routes._task_execution_context.deadline = time.monotonic() + 60
    task = {
        "feed_url": "https://example.com/feed",
        "entries": [
            {"title": f"entry-{n}", "url": f"https://example.com/{n}", "content": "c"}
            for n in (1, 2, 3)
        ],
    }
    try:
        assert webhook_routes._score_in_background(task) == webhook_routes._SCORE_TRANSPORT_RETRY
    finally:
        for name in ("start_entry_index", "progress_callback", "deadline"):
            if hasattr(webhook_routes._task_execution_context, name):
                delattr(webhook_routes._task_execution_context, name)
    assert seen == ["https://example.com/1", "https://example.com/2"]
    assert progress == [1]  # entry 2 stays at the cursor; entry 3 not attempted


def _finish_outcome(webhook_routes, outcome):
    """Claim the single task and finish it with a result or an exception."""
    claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 10_000)
    assert claimed is not None
    path, envelope = claimed
    assert webhook_routes._webhook_slots.acquire(blocking=False)
    future = Future()
    if isinstance(outcome, BaseException):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)
    started = time.time()
    webhook_routes._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])
    pending = _pending_files(webhook_routes)
    assert len(pending) == 1
    return webhook_routes._load_spooled_task(pending[0]), started


def test_lock_and_transport_deferrals_keep_separate_counters(isolated_spool):  # noqa: F811
    import sqlite3

    webhook_routes = isolated_spool
    transport = webhook_routes._SCORE_TRANSPORT_RETRY
    locked = sqlite3.OperationalError("database is locked")
    assert webhook_routes._persist_webhook_task(_task(77)) == "stored"

    # Interleave both kinds on the same task: each counter moves only for its
    # own kind, each uses its own back-off, and neither consumes an attempt.
    expected_lock = expected_transport = 0
    for outcome in (transport, locked, transport, locked):
        persisted, started = _finish_outcome(webhook_routes, outcome)
        if outcome is locked:
            expected_lock += 1
            delay = 5.0 * 2 ** (expected_lock - 1)
            assert persisted["last_error"] == webhook_routes._SQLITE_LOCKED
        else:
            expected_transport += 1
            delay = 60.0 * 2 ** (expected_transport - 1)
            assert persisted["last_error"] == transport
        assert persisted["attempts"] == 0
        assert persisted.get("lock_deferrals", 0) == expected_lock
        assert persisted.get("score_transport_deferrals", 0) == expected_transport
        assert started + delay - 0.5 <= persisted["next_attempt_at"] <= time.time() + delay + 0.5

    # Exhaust the lock cap: transport progress is untouched, and the lock
    # fallback is an ordinary counted failure.
    while expected_lock < webhook_routes._SQLITE_LOCK_DEFERRAL_MAX:
        persisted, _ = _finish_outcome(webhook_routes, locked)
        expected_lock += 1
        assert persisted["attempts"] == 0
    persisted, _ = _finish_outcome(webhook_routes, locked)
    assert persisted["attempts"] == 1
    assert persisted["lock_deferrals"] == webhook_routes._SQLITE_LOCK_DEFERRAL_MAX
    assert persisted["score_transport_deferrals"] == 2

    # Transport deferral still works after the lock cap and still refunds its
    # own claim; its remaining budget is the original cap minus its own uses.
    for n in (3, 4):
        persisted, started = _finish_outcome(webhook_routes, transport)
        assert persisted["attempts"] == 1
        assert persisted["score_transport_deferrals"] == n
        assert persisted["lock_deferrals"] == webhook_routes._SQLITE_LOCK_DEFERRAL_MAX
        assert persisted["next_attempt_at"] >= started + min(300.0, 60.0 * 2 ** (n - 1)) - 0.5
    persisted, _ = _finish_outcome(webhook_routes, transport)
    assert persisted["attempts"] == 2
    assert persisted["last_error"] == "score_transport_retry_defer_failed"


def test_transport_cap_does_not_consume_lock_budget(isolated_spool):  # noqa: F811
    import sqlite3

    webhook_routes = isolated_spool
    assert webhook_routes._persist_webhook_task(_task(78)) == "stored"
    for _ in range(webhook_routes._SCORE_TRANSPORT_DEFERRAL_MAX + 1):
        persisted, _ = _finish_outcome(webhook_routes, webhook_routes._SCORE_TRANSPORT_RETRY)
    assert persisted["attempts"] == 1
    assert "lock_deferrals" not in persisted
    persisted, started = _finish_outcome(
        webhook_routes, sqlite3.OperationalError("database is locked")
    )
    assert persisted["attempts"] == 1  # lock deferral refunds its own claim
    assert persisted["lock_deferrals"] == 1
    assert persisted["score_transport_deferrals"] == webhook_routes._SCORE_TRANSPORT_DEFERRAL_MAX
    assert persisted["next_attempt_at"] >= started + 5.0 - 0.5
