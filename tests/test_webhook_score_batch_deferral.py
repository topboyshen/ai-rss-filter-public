"""Score retries belong to the current durable batch cursor, not older entries."""
from collections import Counter
from concurrent.futures import Future
from types import SimpleNamespace
import sqlite3
import time

import pytest

from tests.test_webhook_durable_spool import _pending_files, _task, isolated_spool  # noqa: F401


def _finish(webhook, path, envelope, outcome):
    future = Future()
    if isinstance(outcome, BaseException):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])


def _claim(webhook):
    claimed = webhook._claim_next_spooled_task(now=time.time() + 100_000)
    assert claimed is not None
    return claimed


def _clear_owner_memory(webhook):
    """Spool files survive a worker restart while process-local ownership does not."""
    with webhook._webhook_inflight_lock:
        webhook._webhook_inflight.clear()
        webhook._webhook_claims.clear()
        webhook._webhook_retry_state.clear()


@pytest.fixture
def batch_env(isolated_spool, monkeypatch):  # noqa: F811
    webhook = isolated_spool
    monkeypatch.setattr(webhook, "Scorer", lambda: object())
    monkeypatch.setattr(webhook, "_cfg", lambda: {})
    monkeypatch.setattr(webhook, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(webhook, "resolve_prompt", lambda *_a: ("p", "prompt"))
    monkeypatch.setattr(webhook, "_find_notify_threshold", lambda *_a: 9)
    monkeypatch.setattr(webhook.DB, "canonical_feed_url", lambda _cfg, url: url)
    monkeypatch.setattr(webhook.DB, "get_feed_config", lambda *_a: {"name": "test source"})
    monkeypatch.setattr(webhook.DB, "set_app_meta", lambda *_a: None)
    monkeypatch.setattr(webhook.DB, "update_feed_health", lambda **_kw: None)
    monkeypatch.setattr(webhook, "_queue_committed_dashboard_inputs", lambda: None)
    return webhook


def test_six_entries_each_defer_twice_then_all_finish_through_durable_spool(
    batch_env, monkeypatch,
):
    """Exercise the actual processor retry decision and persistent queue together."""
    from core import processor

    webhook = batch_env
    monkeypatch.setattr(processor, "_transport_retry_counts", processor.OrderedDict())
    calls, completed = Counter(), []

    def process(entry, **_kw):
        calls[entry.link] += 1
        context = SimpleNamespace(strategy_version="test-batch", quality_repair=False, entry=entry)
        score = processor._EntryScore(
            score_path="llm_safe_neutral_grounding", transport_unverdicted=True,
        )
        try:
            processor._defer_unverdicted_safe_neutral(context, score)
        except processor.ScoringTransportRetryable:
            return None, processor.SCORE_DEFERRED_REASON
        completed.append(entry.link)
        return None, "filtered"

    monkeypatch.setattr(webhook, "process_entry", process)
    task = _task()
    task["entries"] = [
        {"title": f"entry-{n}", "url": f"https://example.com/{n}", "content": "c"}
        for n in range(1, 7)
    ]
    assert webhook._persist_webhook_task(task) == "stored"
    claims, deferred_cursors = 0, []
    while _pending_files(webhook) and claims < 20:
        path, envelope = _claim(webhook)
        outcome = webhook._execute_claimed_spooled_task(path, envelope)
        persisted = webhook._load_spooled_task(path)
        if outcome == webhook._SCORE_TRANSPORT_RETRY:
            deferred_cursors.append(persisted.get("next_entry_index", 0))
        _finish(webhook, path, envelope, outcome)
        claims += 1
    assert claims == 13
    assert deferred_cursors == [n for n in range(6) for _ in range(2)]
    assert calls == {f"https://example.com/{n}": 3 for n in range(1, 7)}
    assert completed == [entry["url"] for entry in task["entries"]]
    assert _pending_files(webhook) == []
    assert list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json")) == []


def test_no_progress_keeps_retry_cap_across_owner_memory_loss(isolated_spool, monkeypatch):  # noqa: F811
    webhook = isolated_spool
    monkeypatch.setattr(webhook, "_webhook_max_attempts", 5)
    assert webhook._persist_webhook_task(_task(71)) == "stored"
    for claim_number in range(1, 10):
        path, envelope = _claim(webhook)
        _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
        _clear_owner_memory(webhook)
        pending = _pending_files(webhook)
        if claim_number < 9:
            persisted = webhook._load_spooled_task(pending[0])
            assert persisted.get("next_entry_index", 0) == 0
            assert persisted["score_transport_entry_index"] == 0
            assert persisted["score_transport_deferrals"] == min(claim_number, 4)
            assert persisted["attempts"] == max(0, claim_number - 4)
        else:
            assert pending == []
    dead = list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json"))
    assert len(dead) == 1
    envelope = webhook._load_spooled_task(dead[0])
    assert envelope["attempts"] == webhook._webhook_max_attempts == 5
    assert envelope["last_error"] == "score_transport_retry_defer_failed"


def test_progress_and_current_entry_retry_budget_survive_restart(isolated_spool):  # noqa: F811
    webhook = isolated_spool
    task = _task(72)
    task["entries"].append(_task(73)["entries"][0])
    assert webhook._persist_webhook_task(task) == "stored"
    for _ in range(2):
        path, envelope = _claim(webhook)
        _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
    path, envelope = _claim(webhook)
    assert webhook._persist_claim_progress(path, envelope["digest"], envelope["owner"], 1, 0, 1)
    _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
    _clear_owner_memory(webhook)
    path, envelope = _claim(webhook)
    assert envelope["next_entry_index"] == 1
    assert envelope["progress"] == {"passed": 0, "filtered": 1}
    assert envelope["score_transport_entry_index"] == 1
    assert envelope["score_transport_deferrals"] == 1
    _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
    persisted = webhook._load_spooled_task(_pending_files(webhook)[0])
    assert persisted["score_transport_entry_index"] == 1
    assert persisted["score_transport_deferrals"] == 2
    assert persisted["attempts"] == 0


def test_stale_progress_does_not_renew_score_or_lock_budget(isolated_spool):  # noqa: F811
    webhook = isolated_spool
    assert webhook._persist_webhook_task(_task(74)) == "stored"
    for outcome in (webhook._SCORE_TRANSPORT_RETRY, sqlite3.OperationalError("database is locked")):
        path, envelope = _claim(webhook)
        _finish(webhook, path, envelope, outcome)
    path, envelope = _claim(webhook)
    assert webhook._persist_claim_progress(path, envelope["digest"], envelope["owner"], 0, 0, 0)
    _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
    persisted = webhook._load_spooled_task(_pending_files(webhook)[0])
    assert persisted["score_transport_deferrals"] == 2
    assert persisted["lock_deferrals"] == 1


def test_new_entry_renews_only_score_budget_not_lock_or_failure_count(isolated_spool):  # noqa: F811
    webhook = isolated_spool
    task = _task(75)
    task["entries"].append(_task(76)["entries"][0])
    assert webhook._persist_webhook_task(task) == "stored"
    outcomes = (False, sqlite3.OperationalError("database is locked"), webhook._SCORE_TRANSPORT_RETRY)
    for outcome in outcomes:
        path, envelope = _claim(webhook)
        _finish(webhook, path, envelope, outcome)
    path, envelope = _claim(webhook)
    assert webhook._persist_claim_progress(path, envelope["digest"], envelope["owner"], 1, 0, 1)
    _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
    persisted = webhook._load_spooled_task(_pending_files(webhook)[0])
    assert persisted["score_transport_entry_index"] == 1
    assert persisted["score_transport_deferrals"] == 1
    assert persisted["lock_deferrals"] == 1
    assert persisted["attempts"] == 1


def test_stale_cursor_counter_cannot_spend_the_current_entry_budget(isolated_spool):  # noqa: F811
    webhook = isolated_spool
    assert webhook._persist_webhook_task(_task(77)) == "stored"
    path, envelope = _claim(webhook)
    envelope.update(next_entry_index=1, score_transport_entry_index=0, score_transport_deferrals=4)
    with webhook._locked_spool() as directory:
        webhook._atomic_write(directory, path, webhook._canonical_json(envelope))
    _finish(webhook, path, envelope, webhook._SCORE_TRANSPORT_RETRY)
    persisted = webhook._load_spooled_task(_pending_files(webhook)[0])
    assert persisted["score_transport_entry_index"] == 1
    assert persisted["score_transport_deferrals"] == 1
    assert persisted["attempts"] == 0
