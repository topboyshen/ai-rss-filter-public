import json
import importlib.util
import logging
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest


@pytest.fixture
def isolated_spool(monkeypatch, tmp_path):
    import routes.webhook_routes as webhook_routes

    webhook_routes.stop_webhook_dispatcher(timeout=2.0)
    monkeypatch.setattr(
        webhook_routes, "_webhook_spool_dir", tmp_path / "webhook-spool"
    )
    monkeypatch.setattr(webhook_routes, "_webhook_spool_max_tasks", 128)
    with webhook_routes._webhook_replay_lock:
        webhook_routes._webhook_replay_seen.clear()
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.clear()
        webhook_routes._webhook_claims.clear()
        webhook_routes._webhook_retry_state.clear()

    yield webhook_routes

    webhook_routes.stop_webhook_dispatcher(timeout=2.0)
    with webhook_routes._webhook_replay_lock:
        webhook_routes._webhook_replay_seen.clear()
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.clear()
        webhook_routes._webhook_claims.clear()
        webhook_routes._webhook_retry_state.clear()


def _task(number=1):
    return {
        "feed_url": "https://example.com/feed",
        "entries": [
            {
                "title": f"entry-{number}",
                "url": f"https://example.com/{number}",
                "content": "content",
            }
        ],
    }


def _pending_files(webhook_routes):
    directory = webhook_routes._ensure_spool_dir()
    return sorted(
        path
        for path in directory.glob("*.json")
        if webhook_routes._is_digest(path.stem)
    )


def test_spooled_task_is_recovered_after_process_memory_loss(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    task = _task()
    assert webhook_routes._persist_webhook_task(task) == "stored"
    claimed = webhook_routes._claim_next_spooled_task(now=100.0)
    assert claimed is not None
    claimed_path, first_envelope = claimed
    assert first_envelope["attempts"] == 1

    # Simulate a crashed owner and an expired durable lease.
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.clear()
        webhook_routes._webhook_claims.clear()
        webhook_routes._webhook_retry_state.clear()
    with webhook_routes._locked_spool() as directory:
        envelope = webhook_routes._load_spooled_task(claimed_path)
        envelope["lease_until"] = 100.0
        webhook_routes._atomic_write(
            directory,
            claimed_path,
            webhook_routes._canonical_json(envelope),
        )

    recovered = webhook_routes._claim_next_spooled_task(now=101.0)
    assert recovered is not None
    recovered_path, recovered_envelope = recovered
    assert recovered_path == claimed_path
    assert recovered_envelope["task"] == task
    assert recovered_envelope["attempts"] == 2


def test_legacy_direct_task_is_atomically_upgraded_and_claimed(isolated_spool):
    webhook_routes = isolated_spool
    task = _task()
    digest = webhook_routes._task_digest(task)
    directory = webhook_routes._ensure_spool_dir()
    legacy_path = directory / f"{digest}.json"
    webhook_routes._atomic_write(
        directory,
        legacy_path,
        webhook_routes._canonical_json(task),
    )

    claimed = webhook_routes._claim_next_spooled_task(now=100.0)

    assert claimed is not None
    claimed_path, envelope = claimed
    assert claimed_path == directory / "inflight" / f"{digest}.json"
    assert not legacy_path.exists()
    assert envelope["task"] == task
    assert envelope["digest"] == digest
    assert envelope["attempts"] == 1
    assert envelope["lease_until"] > 100.0
    assert json.loads(claimed_path.read_text(encoding="utf-8")) == envelope
    assert not list((directory / "dead-letter").glob("*.json"))


def test_success_records_digest_then_deletes_pending_file(isolated_spool):
    webhook_routes = isolated_spool
    task = _task()
    assert webhook_routes._persist_webhook_task(task) == "stored"
    path = _pending_files(webhook_routes)[0]
    digest = path.stem

    assert webhook_routes._webhook_slots.acquire(blocking=False)
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.add(digest)
    completed = Future()
    completed.set_result(True)
    webhook_routes._finish_spooled_task(completed, path, digest)

    assert not path.exists()
    assert webhook_routes._persist_webhook_task(task) == "duplicate"


def test_failed_worker_keeps_pending_file_for_retry(isolated_spool):
    webhook_routes = isolated_spool
    task = _task()
    assert webhook_routes._persist_webhook_task(task) == "stored"
    path = _pending_files(webhook_routes)[0]
    digest = path.stem

    assert webhook_routes._webhook_slots.acquire(blocking=False)
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.add(digest)
    failed = Future()
    failed.set_result(False)
    webhook_routes._finish_spooled_task(failed, path, digest)

    assert path.exists()
    assert digest in webhook_routes._webhook_retry_state


def test_spool_capacity_is_hard_but_duplicates_do_not_consume_slots(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    monkeypatch.setattr(webhook_routes, "_webhook_spool_max_tasks", 2)

    assert webhook_routes._persist_webhook_task(_task(1)) == "stored"
    assert webhook_routes._persist_webhook_task(_task(1)) == "duplicate"
    assert webhook_routes._persist_webhook_task(_task(2)) == "stored"
    assert webhook_routes._persist_webhook_task(_task(3)) == "full"
    assert len(_pending_files(webhook_routes)) == 2


def test_briefing_has_its_own_one_task_quota_and_cannot_fill_scoring_spool(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    monkeypatch.setattr(webhook_routes, "_briefing_spool_max_tasks", 1)
    first = {"task_type": "briefing_refresh", "fingerprint": "a", "request_window": 1}
    second = {"task_type": "briefing_refresh", "fingerprint": "b", "request_window": 1}

    assert webhook_routes._persist_webhook_task(first) == "stored"
    assert webhook_routes._persist_webhook_task(second) == "full"
    assert webhook_routes._persist_webhook_task(_task(99)) == "stored"


def test_refresh_requests_coalesce_while_one_is_pending(isolated_spool):
    webhook_routes = isolated_spool
    first = {"task_type": "refresh", "request_window": 1}
    newer = {"task_type": "refresh", "request_window": 2}

    assert webhook_routes._persist_webhook_task(first) == "stored"
    assert webhook_routes._persist_webhook_task(newer) == "duplicate"
    assert webhook_routes._persist_webhook_task(_task(101)) == "stored"


def test_refresh_lock_busy_requeues_without_consuming_attempts(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    monkeypatch.setattr(webhook_routes, "_refresh_busy_delay_seconds", 1)
    task = {"task_type": "refresh", "request_window": 1}
    assert webhook_routes._persist_webhook_task(task) == "stored"

    for _ in range(webhook_routes._webhook_max_attempts + 2):
        claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 120)
        assert claimed is not None
        path, envelope = claimed
        assert envelope["attempts"] == 1
        assert webhook_routes._webhook_slots.acquire(blocking=False)
        busy = Future()
        busy.set_result("task_busy")
        webhook_routes._finish_spooled_task(
            busy, path, envelope["digest"], envelope["owner"]
        )

        pending = _pending_files(webhook_routes)
        assert len(pending) == 1
        persisted = webhook_routes._load_spooled_task(pending[0])
        assert persisted["attempts"] == 0
        assert persisted["last_error"] == "refresh_lock_busy"

    dlq = Path(webhook_routes._webhook_spool_dir) / "dead-letter"
    assert list(dlq.glob("*.json")) == []


def test_snapshot_input_change_requeues_without_consuming_attempts(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    monkeypatch.setattr(webhook_routes, "_refresh_busy_delay_seconds", 17)
    monkeypatch.setattr(webhook_routes, "_dashboard_snapshot_retry_delay_seconds", 5)
    task = {
        "task_type": "dashboard_snapshot_refresh",
        "snapshot_version": 1,
        "request_window": 1,
    }
    assert webhook_routes._persist_webhook_task(task) == "stored"

    for deferral in range(1, webhook_routes._webhook_max_attempts + 3):
        claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 1000)
        assert claimed is not None
        path, envelope = claimed
        assert envelope["attempts"] == 1
        assert webhook_routes._webhook_slots.acquire(blocking=False)
        changed = Future()
        changed.set_result(webhook_routes._DASHBOARD_SNAPSHOT_INPUTS_CHANGED)
        deferred_at = time.time()
        webhook_routes._finish_spooled_task(
            changed, path, envelope["digest"], envelope["owner"]
        )

        pending = _pending_files(webhook_routes)
        assert len(pending) == 1
        persisted = webhook_routes._load_spooled_task(pending[0])
        assert persisted["attempts"] == 0
        assert persisted["transient_deferrals"] == deferral
        assert (
            persisted["last_error"]
            == webhook_routes._DASHBOARD_SNAPSHOT_INPUTS_CHANGED
        )
        assert persisted["next_attempt_at"] >= deferred_at + 4.5
        expected_delay = min(300.0, 5.0 * (2 ** min(deferral - 1, 6)))
        assert persisted["next_attempt_at"] >= deferred_at + expected_delay - 0.5

    dlq = Path(webhook_routes._webhook_spool_dir) / "dead-letter"
    assert list(dlq.glob("*.json")) == []

    claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 1000)
    assert claimed is not None
    path, envelope = claimed
    assert webhook_routes._webhook_slots.acquire(blocking=False)
    completed = Future()
    completed.set_result(True)
    webhook_routes._finish_spooled_task(
        completed, path, envelope["digest"], envelope["owner"]
    )
    assert _pending_files(webhook_routes) == []
    assert list(dlq.glob("*.json")) == []


def test_snapshot_hard_failure_dlq(isolated_spool):
    webhook_routes = isolated_spool
    task = {
        "task_type": "dashboard_snapshot_refresh",
        "snapshot_version": 1,
        "request_window": 2,
    }
    assert webhook_routes._persist_webhook_task(task) == "stored"

    for expected_attempt in range(1, webhook_routes._webhook_max_attempts + 1):
        claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 120)
        assert claimed is not None
        path, envelope = claimed
        assert envelope["attempts"] == expected_attempt
        assert webhook_routes._webhook_slots.acquire(blocking=False)
        failed = Future()
        failed.set_result(False)
        webhook_routes._finish_spooled_task(
            failed, path, envelope["digest"], envelope["owner"]
        )

    dlq = Path(webhook_routes._webhook_spool_dir) / "dead-letter"
    dead = list(dlq.glob("*.json"))
    assert len(dead) == 1
    envelope = json.loads(dead[0].read_text(encoding="utf-8"))
    assert envelope["attempts"] == webhook_routes._webhook_max_attempts
    assert envelope["last_error"] == "task_failed"


def test_refresh_busy_defer_failure_falls_back_to_normal_retry(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    task = {"task_type": "refresh", "request_window": 1}
    assert webhook_routes._persist_webhook_task(task) == "stored"
    claimed = webhook_routes._claim_next_spooled_task(now=time.time() + 120)
    assert claimed is not None
    path, envelope = claimed
    assert webhook_routes._webhook_slots.acquire(blocking=False)

    retried = []
    monkeypatch.setattr(
        webhook_routes,
        "_defer_spooled_task_without_attempt",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        webhook_routes,
        "_retry_or_dead_letter",
        lambda *args, **_kwargs: retried.append(args) or "retry",
    )
    busy = Future()
    busy.set_result("task_busy")

    webhook_routes._finish_spooled_task(
        busy, path, envelope["digest"], envelope["owner"]
    )

    assert retried
    assert retried[0][3] == "refresh_busy_defer_failed"


def test_realtime_score_is_claimed_before_low_priority_briefing(isolated_spool):
    webhook_routes = isolated_spool
    briefing = {"task_type": "briefing_refresh", "fingerprint": "a"}
    score = _task(42)
    assert webhook_routes._persist_webhook_task(briefing) == "stored"
    assert webhook_routes._persist_webhook_task(score) == "stored"

    claimed = webhook_routes._claim_next_spooled_task(now=100.0)

    assert claimed is not None
    assert claimed[1]["task"] == score


def test_realtime_score_is_claimed_before_full_refresh(isolated_spool):
    webhook_routes = isolated_spool
    refresh = {"task_type": "refresh", "request_window": 1}
    score = _task(43)
    assert webhook_routes._persist_webhook_task(refresh) == "stored"
    assert webhook_routes._persist_webhook_task(score) == "stored"

    claimed = webhook_routes._claim_next_spooled_task(now=100.0)

    assert claimed is not None
    assert claimed[1]["task"] == score


def test_briefing_uses_a_dedicated_single_thread_executor(isolated_spool):
    webhook_routes = isolated_spool
    assert webhook_routes._get_briefing_executor() is not webhook_routes._get_executor()
    assert webhook_routes._get_briefing_executor()._max_workers == 1


def test_executor_backpressure_does_not_reject_durably_accepted_request(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool

    class BusySlots:
        def acquire(self, blocking=False):
            assert blocking is False
            return False

        def release(self):
            raise AssertionError("an unacquired slot must not be released")

    monkeypatch.setattr(webhook_routes, "_webhook_slots", BusySlots())

    import app as app_module
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 1024 * 1024)

    payload = {
        "feed": {"feed_url": "https://example.com/feed"},
        "entries": [{"title": "accepted", "url": "https://example.com/a"}],
    }
    with app_module.app.test_request_context(
        "/webhook",
        method="POST",
        data=json.dumps(payload),
        content_type="application/json",
    ):
        response = webhook_routes.webhook_receiver()

    assert response.status_code == 202
    assert json.loads(response.get_data(as_text=True))["status"] == "received"
    assert len(_pending_files(webhook_routes)) == 1
    assert webhook_routes._dispatch_spooled_tasks_once() == 0
    assert len(_pending_files(webhook_routes)) == 1


def test_webhook_aliases_share_one_durable_task(isolated_spool, monkeypatch):
    webhook_routes = isolated_spool
    runtime = "http://rsshub:1200/wallstreetcn/live"
    original = "http://127.0.0.1:1200/wallstreetcn/live"
    monkeypatch.setattr(
        webhook_routes,
        "_cfg",
        lambda: {
            "feeds": [{"url": runtime}],
            "_runtime_feed_url_aliases": {runtime: [original]},
        },
    )

    import app as app_module
    monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 1024 * 1024)

    def submit(feed_url):
        payload = {
            "feed": {"feed_url": feed_url},
            "entries": [{"title": "same", "url": "https://example.com/a"}],
        }
        with app_module.app.test_request_context(
            "/webhook",
            method="POST",
            data=json.dumps(payload),
            content_type="application/json",
        ):
            return webhook_routes.webhook_receiver()

    assert json.loads(submit(original).get_data(as_text=True))["status"] == "received"
    assert json.loads(submit(runtime).get_data(as_text=True))["status"] == "duplicate"
    pending = _pending_files(webhook_routes)
    assert len(pending) == 1
    assert webhook_routes._load_spooled_task(pending[0])["task"]["feed_url"] == runtime


def test_dispatcher_start_is_idempotent(isolated_spool, monkeypatch):
    webhook_routes = isolated_spool
    scanned = threading.Event()

    def scan_once():
        scanned.set()
        return 0

    monkeypatch.setattr(webhook_routes, "_dispatch_spooled_tasks_once", scan_once)
    first = webhook_routes.start_webhook_dispatcher()
    assert scanned.wait(timeout=2.0)
    second = webhook_routes.start_webhook_dispatcher()
    assert first is second
    assert first.is_alive()


def test_dispatcher_start_creates_spool_directory_and_logs(
    isolated_spool, caplog
):
    webhook_routes = isolated_spool
    directory = Path(webhook_routes._webhook_spool_dir)
    assert not directory.exists()
    caplog.set_level(logging.INFO, logger=webhook_routes.__name__)

    webhook_routes.start_webhook_dispatcher()
    deadline = time.monotonic() + 3.0
    while not directory.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert directory.is_dir()
    assert "durable spool dispatcher started" in caplog.text


def test_existing_spool_is_scanned_and_deleted_after_dispatcher_start(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    assert webhook_routes._persist_webhook_task(_task()) == "stored"
    assert len(_pending_files(webhook_routes)) == 1
    monkeypatch.setattr(webhook_routes, "_score_in_background", lambda _task: True)

    thread = webhook_routes.start_webhook_dispatcher()
    deadline = time.monotonic() + 3.0
    while _pending_files(webhook_routes) and time.monotonic() < deadline:
        time.sleep(0.01)

    assert thread.is_alive()
    assert _pending_files(webhook_routes) == []


def test_process_entry_exception_returns_false_and_keeps_task(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    task = _task()
    assert webhook_routes._persist_webhook_task(task) == "stored"
    path = _pending_files(webhook_routes)[0]
    digest = path.stem

    monkeypatch.setattr(webhook_routes, "Scorer", lambda: object())
    monkeypatch.setattr(
        webhook_routes,
        "_cfg",
        lambda: {"feeds": [{"url": task["feed_url"], "name": "Example"}]},
    )
    monkeypatch.setattr(
        webhook_routes, "resolve_prompt", lambda *_args: ("default", "prompt")
    )
    monkeypatch.setattr(webhook_routes, "_find_notify_threshold", lambda *_args: 9.0)
    monkeypatch.setattr(
        webhook_routes,
        "process_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transient")),
    )

    result = webhook_routes._score_in_background(task)
    assert result is False

    assert webhook_routes._webhook_slots.acquire(blocking=False)
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.add(digest)
    completed = Future()
    completed.set_result(result)
    webhook_routes._finish_spooled_task(completed, path, digest)

    assert path.exists()
    assert digest in webhook_routes._webhook_retry_state


def test_webhook_resolves_registers_and_propagates_feedback_contract(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    task = _task()
    captured = {}
    base_cfg = {"feeds": [{"url": task["feed_url"], "name": "Example"}]}
    resolved_cfg = {
        **base_cfg,
        "scoring_strategy_version": "base:fb-1234",
        "_few_shot_context": "frozen feedback",
        "_scoring_contract_resolved": True,
        "_scoring_contract_registered": True,
    }

    monkeypatch.setattr(webhook_routes, "Scorer", lambda: object())
    monkeypatch.setattr(webhook_routes, "_cfg", lambda: base_cfg)

    def resolve(cfg, *, register_snapshot):
        captured["resolve"] = (cfg, register_snapshot)
        return resolved_cfg

    monkeypatch.setattr(webhook_routes, "resolve_scoring_contract", resolve)
    monkeypatch.setattr(
        webhook_routes, "resolve_prompt", lambda *_args: ("default", "prompt")
    )
    monkeypatch.setattr(
        webhook_routes, "_find_notify_threshold", lambda *_args: 9.0
    )
    monkeypatch.setattr(
        webhook_routes.DB, "get_feed_config", lambda *_args: {"name": "Example"}
    )
    monkeypatch.setattr(webhook_routes.DB, "set_app_meta", lambda *_args: None)
    monkeypatch.setattr(
        webhook_routes.DB, "update_feed_health", lambda **_kwargs: None
    )

    def process_entry(_entry, *_args, **kwargs):
        captured["process_cfg"] = kwargs["cfg"]
        captured["few_shot_context"] = kwargs["few_shot_context"]
        return None, "filtered"

    monkeypatch.setattr(webhook_routes, "process_entry", process_entry)

    assert webhook_routes._score_in_background(task) is True
    assert captured["resolve"] == (base_cfg, True)
    assert captured["process_cfg"] is resolved_cfg
    assert captured["few_shot_context"] == "frozen feedback"


def test_partial_batch_retry_resumes_from_durable_entry_cursor(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    task = {
        "feed_url": "https://example.com/feed",
        "entries": [
            {"title": "first", "url": "https://example.com/1", "content": ""},
            {"title": "second", "url": "https://example.com/2", "content": ""},
        ],
    }
    assert webhook_routes._persist_webhook_task(task) == "stored"
    path, envelope = webhook_routes._claim_next_spooled_task()

    monkeypatch.setattr(webhook_routes, "Scorer", lambda: object())
    monkeypatch.setattr(
        webhook_routes,
        "_cfg",
        lambda: {"feeds": [{"url": task["feed_url"], "name": "Example"}]},
    )
    monkeypatch.setattr(
        webhook_routes.DB, "canonical_feed_url", lambda _cfg, url: url
    )
    monkeypatch.setattr(
        webhook_routes.DB,
        "get_feed_config",
        lambda _cfg, _url: {"name": "Example"},
    )
    monkeypatch.setattr(webhook_routes.DB, "set_app_meta", lambda *_args: None)
    health_updates = []
    monkeypatch.setattr(
        webhook_routes.DB,
        "update_feed_health",
        lambda **kwargs: health_updates.append(kwargs),
    )
    monkeypatch.setattr(
        webhook_routes, "resolve_prompt", lambda *_args: ("default", "prompt")
    )
    monkeypatch.setattr(
        webhook_routes, "_find_notify_threshold", lambda *_args: 9.0
    )

    calls = []
    fail_second = {"value": True}

    def process(entry, *_args, **_kwargs):
        calls.append(entry.title)
        if entry.title == "second" and fail_second["value"]:
            raise RuntimeError("transient")
        return None, "filtered"

    monkeypatch.setattr(webhook_routes, "process_entry", process)

    first_result = webhook_routes._execute_claimed_spooled_task(path, envelope)
    assert first_result is False
    persisted = webhook_routes._load_spooled_task(path)
    assert persisted is not None and persisted["next_entry_index"] == 1
    assert persisted["progress"] == {"passed": 0, "filtered": 1}

    assert webhook_routes._webhook_slots.acquire(blocking=False)
    completed = Future()
    completed.set_result(False)
    webhook_routes._finish_spooled_task(
        completed, path, envelope["digest"], envelope["owner"]
    )

    retry = webhook_routes._claim_next_spooled_task(now=time.time() + 120)
    assert retry is not None
    retry_path, retry_envelope = retry
    fail_second["value"] = False

    assert webhook_routes._execute_claimed_spooled_task(
        retry_path, retry_envelope
    ) is True
    assert calls == ["first", "second", "second"]
    persisted = webhook_routes._load_spooled_task(retry_path)
    assert persisted is not None and persisted["next_entry_index"] == 2
    assert persisted["progress"] == {"passed": 0, "filtered": 2}
    assert health_updates[-1]["articles_total"] == 2
    assert health_updates[-1]["articles_passed"] == 0


def test_successful_webhook_updates_ingestion_timestamp(
    isolated_spool, monkeypatch
):
    webhook_routes = isolated_spool
    task = _task()
    metadata = []

    class ScoredEntry:
        title = "[9.0] [A股] entry"
        summary = "reason"

    monkeypatch.setattr(webhook_routes, "Scorer", lambda: object())
    monkeypatch.setattr(webhook_routes, "_cfg", lambda: {"feeds": []})
    monkeypatch.setattr(
        webhook_routes, "resolve_prompt", lambda *_args: ("default", "prompt")
    )
    monkeypatch.setattr(webhook_routes, "_find_notify_threshold", lambda *_args: 9.0)
    monkeypatch.setattr(
        webhook_routes.DB,
        "get_feed_config",
        lambda _cfg, _url: {"name": "Example Source"},
    )
    monkeypatch.setattr(
        webhook_routes.DB,
        "set_app_meta",
        lambda key, value: metadata.append((key, value)),
    )
    monkeypatch.setattr(webhook_routes.DB, "update_feed_health", lambda **_kwargs: None)
    monkeypatch.setattr(
        webhook_routes,
        "process_entry",
        lambda *_args, **_kwargs: (ScoredEntry(), "ok"),
    )

    assert webhook_routes._score_in_background(task) is True
    assert metadata and metadata[0][0] == "last_ingestion"
    assert all(key != "last_full_refresh" for key, _value in metadata)


def test_gunicorn_web_worker_never_starts_background_dispatcher(monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    import routes.shared as shared_routes
    import routes.webhook_routes as webhook_routes
    import services.shared_cache as shared_cache

    calls = []
    monkeypatch.setattr(shared_routes, "_security_cfg", lambda: {"api_key": "test-key"})
    monkeypatch.setattr(shared_cache, "process_role", lambda: "web")
    monkeypatch.setattr(
        dashboard_routes,
        "_ensure_quotes_refresher",
        lambda: calls.append("quotes"),
    )
    monkeypatch.setattr(
        webhook_routes,
        "start_webhook_dispatcher",
        lambda: calls.append("webhook"),
    )

    config_path = Path(__file__).resolve().parents[1] / "gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("gunicorn_conf_test", config_path)
    assert spec is not None and spec.loader is not None
    gunicorn_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gunicorn_config)

    gunicorn_config.post_worker_init(object())
    assert calls == []


def _requeue_by_lease_recovery(webhook_routes, claimed, envelope):
    with webhook_routes._locked_spool() as directory:
        expired = float(envelope["lease_until"]) + 1
        assert webhook_routes._recover_expired_claims_unlocked(directory, expired) == 1


def _requeue_by_retry(webhook_routes, claimed, envelope):
    assert (
        webhook_routes._retry_or_dead_letter(
            claimed, envelope["digest"], envelope["owner"], "boom"
        )
        == "retry"
    )


def _requeue_by_transient_defer(webhook_routes, claimed, envelope):
    assert webhook_routes._defer_spooled_task_without_attempt(
        claimed, envelope["digest"], envelope["owner"], "busy", delay_seconds=0
    )


@pytest.mark.parametrize(
    "requeue",
    [_requeue_by_lease_recovery, _requeue_by_retry, _requeue_by_transient_defer],
)
def test_requeued_task_goes_behind_already_queued_tasks_despite_tied_mtimes(
    isolated_spool, requeue
):
    """A requeue is a new enqueue: coarse mtimes must not let it jump the queue."""
    webhook_routes = isolated_spool
    first, second, third = _task(1), _task(2), _task(3)
    assert webhook_routes._persist_webhook_task(first) == "stored"
    claimed, envelope = webhook_routes._claim_next_spooled_task()
    assert envelope["digest"] == webhook_routes._task_digest(first)
    assert webhook_routes._persist_webhook_task(second) == "stored"
    assert webhook_routes._persist_webhook_task(third) == "stored"

    requeue(webhook_routes, claimed, envelope)

    directory = webhook_routes._ensure_spool_dir()
    expected = [
        f"{webhook_routes._task_digest(task)}.json" for task in (second, third, first)
    ]
    tied_ns = 1_700_000_000_123_456_789
    for name in expected:
        os.utime(directory / name, ns=(tied_ns, tied_ns))
    pending = webhook_routes._pending_paths_unlocked(directory)
    assert [path.name for path in pending] == expected
    requeued = json.loads((directory / expected[-1]).read_text(encoding="utf-8"))
    assert requeued["created_at"] == envelope["created_at"]
    assert requeued["enqueued_at"] > requeued["created_at"]
