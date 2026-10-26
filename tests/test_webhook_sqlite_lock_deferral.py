"""A held SQLite write lock defers spooled work without consuming attempts."""
import sqlite3
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from db import database
from services import dashboard_snapshot_updates as updates


@pytest.fixture
def lock_env(monkeypatch, tmp_path):
    import routes.webhook_routes as webhook

    webhook.stop_webhook_dispatcher(timeout=2)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(webhook, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(webhook, "_webhook_spool_max_tasks", 32)
    monkeypatch.setattr(updates, "time", SimpleNamespace(time=lambda: 1000))
    database.init_db()
    # Keep the real busy handler but make a held lock fail fast in tests.
    monkeypatch.setattr(database, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(database, "_SQLITE_BUSY_TIMEOUT_PRAGMA", "PRAGMA busy_timeout=50")
    with webhook._webhook_inflight_lock:
        webhook._webhook_inflight.clear()
        webhook._webhook_claims.clear()
        webhook._webhook_retry_state.clear()
    yield webhook
    webhook.stop_webhook_dispatcher(timeout=2)


def _dirty_input():
    with database.get_connection() as conn:
        updates.mark_realtime_inputs_dirty(conn)
        conn.commit()
    return updates.realtime_revision_status()


def _run(webhook, path, envelope):
    future = Future()
    try:
        future.set_result(webhook._execute_claimed_spooled_task(path, envelope))
    except Exception as exc:  # noqa: BLE001 - mirrors the executor
        future.set_exception(exc)
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])


def _only_pending(webhook):
    directory = webhook._ensure_spool_dir()
    paths = webhook._pending_paths_unlocked(directory)
    assert len(paths) == 1
    return webhook._load_spooled_task(paths[0])


def _dead_letters(webhook):
    return list((Path(webhook._webhook_spool_dir) / "dead-letter").glob("*.json"))


def test_held_write_lock_defers_ack_without_attempt_or_rerender(lock_env, monkeypatch):
    webhook = lock_env
    import services.dashboard_snapshots as snapshots

    renders = []

    def render():
        renders.append(1)
        return {"ok": sorted(updates.REALTIME_CARDS), "failed": [], "deferred": []}

    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", render)
    state = _dirty_input()
    assert state["input"] > state["published"]
    task = {
        "task_type": "dashboard_realtime_refresh", "snapshot_version": 1,
        "realtime_input_revision": state["input"], "request_window": 1,
    }
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = webhook._claim_next_spooled_task(now=time.time())
    assert envelope["attempts"] == 1

    locker = sqlite3.connect(database.DB_PATH, isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")  # a real competing writer
        before = time.time()
        _run(webhook, path, envelope)
    finally:
        locker.rollback()
        locker.close()

    persisted = _only_pending(webhook)
    assert persisted["attempts"] == 0  # the claim's attempt was handed back
    assert persisted["lock_deferrals"] == 1
    assert persisted["last_error"] == webhook._SQLITE_LOCKED
    assert persisted["pending_ack"]["revision"] == state["input"]
    assert persisted["next_attempt_at"] >= before + webhook._SQLITE_LOCK_BASE_DELAY_SECONDS - 0.5
    assert _dead_letters(webhook) == []
    assert updates.realtime_revision_status()["published"] == state["published"]
    assert renders == [1]

    # Lock released: the retry acknowledges the already published render only.
    path, envelope = webhook._claim_next_spooled_task(now=time.time() + 1000)
    _run(webhook, path, envelope)
    assert renders == [1]
    assert updates.realtime_revision_status()["published"] == state["input"]
    assert webhook._pending_paths_unlocked(webhook._ensure_spool_dir()) == []
    assert _dead_letters(webhook) == []


def test_lock_deferrals_are_capped_then_attempts_resume(lock_env):
    webhook = lock_env
    task = {"task_type": "dashboard_snapshot_refresh", "snapshot_version": 1,
            "request_window": 7}
    assert webhook._persist_webhook_task(task) == "stored"
    locked = sqlite3.OperationalError("database is locked")

    for expected in range(1, webhook._SQLITE_LOCK_DEFERRAL_MAX + 1):
        path, envelope = webhook._claim_next_spooled_task(now=time.time() + 10_000)
        future = Future()
        future.set_exception(locked)
        assert webhook._webhook_slots.acquire(blocking=False)
        webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])
        persisted = _only_pending(webhook)
        assert persisted["attempts"] == 0
        assert persisted["lock_deferrals"] == expected
        assert "pending_ack" not in persisted

    path, envelope = webhook._claim_next_spooled_task(now=time.time() + 10_000)
    future = Future()
    future.set_exception(locked)
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])
    persisted = _only_pending(webhook)
    assert persisted["attempts"] == 1  # cap reached: counted as a normal failure
    assert persisted["lock_deferrals"] == webhook._SQLITE_LOCK_DEFERRAL_MAX


def test_other_operational_errors_still_consume_attempts(lock_env):
    webhook = lock_env
    task = {"task_type": "dashboard_snapshot_refresh", "snapshot_version": 1,
            "request_window": 8}
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = webhook._claim_next_spooled_task(now=time.time())
    future = Future()
    future.set_exception(sqlite3.OperationalError("no such table: app_meta"))
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])
    persisted = _only_pending(webhook)
    assert persisted["attempts"] == 1
    assert "lock_deferrals" not in persisted


def test_malformed_pending_ack_falls_back_to_full_render(lock_env, monkeypatch):
    webhook = lock_env
    import services.dashboard_snapshots as snapshots

    renders = []
    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: (
        renders.append(1) or {"ok": sorted(updates.REALTIME_CARDS), "failed": [], "deferred": []}
    ))
    task = {"task_type": "dashboard_realtime_refresh", "snapshot_version": 1,
            "request_window": 9}
    for bad in ({"revision": True}, {"revision": 1, "realtime_only": False,
                "ok": [], "failed": [], "deferred": []}, "x"):
        assert webhook._valid_pending_ack(task, bad) is None
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = webhook._claim_next_spooled_task(now=time.time())
    envelope["pending_ack"] = {"revision": "1"}
    assert webhook._execute_claimed_spooled_task(path, envelope) is True
    assert renders == [1]


@pytest.mark.parametrize("opener", ["rw", "rw_unconfigured", "ro"])
def test_connection_helpers_share_one_busy_timeout(opener):
    db = database
    db.init_db()
    expected = int(db.SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
    if opener == "rw":
        ctx = db.get_connection()
    elif opener == "rw_unconfigured":
        ctx = db.get_connection(configure_storage=False)
    else:
        ctx = db.get_read_only_connection()
    with ctx as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == expected
