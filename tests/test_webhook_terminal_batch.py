"""A terminal item is durably audited without starving later webhook items."""

import copy
import time
from collections import OrderedDict
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from core import processor
from services import natural_score_continuations as store
from tests.test_natural_score_continuation import (
    FEED, LINK, PROMPT, SUMMARY, TITLE, _make_due, _record, _rows,
    pipeline,  # noqa: F401
)
from tests.test_terminal_unscored_consumers import _processing_rows
from tests.test_webhook_durable_spool import _pending_files, isolated_spool  # noqa: F401
from tests.test_webhook_score_batch_deferral import _clear_owner_memory

LATER = LINK + "/later-in-same-batch"


def _terminal(p, link=LINK, state="expired"):
    strategy = p.cfg["scoring_strategy_version"]
    assert store.enqueue(strategy, link, {
        "title": TITLE, "link": link, "summary": SUMMARY, "prompt": PROMPT,
        "feed_url": FEED, "few_shot_context": "",
    }, initial_attempts=store.MAX_ATTEMPTS) == "stored"
    with store.locked_record(strategy, link) as record:
        data = record.data
        data.update(state=state, updated_at=time.time())
        record.save(data)
    return _record(p.cfg, link)


@pytest.fixture
def terminal_batch(pipeline, isolated_spool, monkeypatch):  # noqa: F811
    p, webhook = pipeline, isolated_spool
    monkeypatch.setattr(webhook, "Scorer", lambda: p.scorer)
    monkeypatch.setattr(webhook, "_cfg", lambda: p.cfg)
    monkeypatch.setattr(webhook, "resolve_scoring_contract", lambda cfg, **_kw: cfg)
    monkeypatch.setattr(webhook, "resolve_prompt", lambda *_a: ("p", PROMPT))
    monkeypatch.setattr(webhook, "_queue_committed_dashboard_inputs", lambda: None)
    p.control["mode"] = "recovered"
    task = {
        "feed_url": FEED,
        "entries": [
            {"title": TITLE, "url": link, "content": SUMMARY}
            for link in (LINK, LATER)
        ],
    }
    return SimpleNamespace(p=p, webhook=webhook, task=task)


def _claim(webhook):
    claimed = webhook._claim_next_spooled_task(now=time.time() + 100_000)
    assert claimed is not None
    return claimed


def _finish(webhook, path, envelope, outcome):
    future = Future()
    if isinstance(outcome, BaseException):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)
    assert webhook._webhook_slots.acquire(blocking=False)
    webhook._finish_spooled_task(future, path, envelope["digest"], envelope["owner"])


def _restart_claim(webhook, path):
    _clear_owner_memory(webhook)
    with webhook._locked_spool() as directory:
        envelope = webhook._load_spooled_task(path)
        envelope["lease_until"] = 0
        webhook._atomic_write(directory, path, webhook._canonical_json(envelope))
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()


def _archived_partial(webhook, task, *, passed=1, filtered=0, terminals=1):
    assert _pending_files(webhook) == []
    directory = webhook._ensure_spool_dir()
    assert list((directory / "inflight").glob("*.json")) == []
    paths = list((directory / "dead-letter").glob("*.json"))
    assert len(paths) == 1
    archived = webhook._load_spooled_task(paths[0])
    assert archived["last_error"] == webhook._WEBHOOK_PARTIAL
    assert archived["next_entry_index"] == len(task["entries"])
    assert archived["progress"] == {
        "passed": passed, "filtered": filtered, "terminal_unscored": terminals,
    }
    assert len(archived["terminal_unscored"]) == terminals
    assert archived["task"] == task
    with webhook._locked_spool() as directory:
        assert webhook._load_receipts_unlocked(directory) == {}
    return archived


def _assert_terminal_row(row, p, *, index=0, link=LINK):
    assert row["entry_index"] == index
    assert row["article_key"] == store.identity(p.cfg["scoring_strategy_version"], link)[2]
    assert row["strategy_version"] == p.cfg["scoring_strategy_version"]
    assert row["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
    assert isinstance(row["recorded_at"], (float, int)) and row["recorded_at"] > 0
    assert "title" not in row and "link" not in row and "content" not in row


@pytest.mark.parametrize("terminal", ["expired", "exhausted"])
def test_terminal_first_article_does_not_starve_later_score_or_claim_full_success(terminal_batch, terminal):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p, state=terminal)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL and outcome is not True
    persisted = webhook._load_spooled_task(path)
    assert persisted["next_entry_index"] == 2
    _assert_terminal_row(persisted["terminal_unscored"][0], p)
    assert _rows() == ([], []) and _record(p.cfg) == before
    cache, signals = _rows(LATER)
    assert len(cache) == len(signals) == 1
    assert signals[0]["score"] == 8.0 and signals[0]["selected"] == 1
    assert len(p.calls) == 1
    _finish(webhook, path, envelope, outcome)
    archived = _archived_partial(webhook, task)
    assert archived["attempts"] == 1
    assert archived.get("score_transport_deferrals", 0) == 0
    diagnostics = _processing_rows()
    assert len(diagnostics) == 2
    assert diagnostics[0]["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
    assert diagnostics[0]["state"] == "error" and diagnostics[0]["signal_key"] is None
    assert diagnostics[1]["state"] == "completed" and diagnostics[1]["signal_key"] is not None
    _clear_owner_memory(webhook)
    p.scorer.clear_runtime_cache()
    assert webhook._persist_webhook_task(copy.deepcopy(task)) == "duplicate"
    assert webhook._claim_next_spooled_task(now=time.time() + 100_000) is None
    assert len(p.calls) == 1 and _record(p.cfg) == before
    assert _rows(LATER) == (cache, signals)


@pytest.mark.parametrize("after_checkpoint", [False, True])
def test_crash_around_terminal_checkpoint_preserves_cursor_and_later_entry(
    terminal_batch, monkeypatch, after_checkpoint,
):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p, state="exhausted")
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    real_persist = webhook._persist_claim_progress

    def crash(*args, **kwargs):
        if kwargs.get("terminal_outcome"):
            if after_checkpoint:
                assert real_persist(*args, **kwargs)
            raise SystemExit("simulated worker crash at terminal checkpoint")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(webhook, "_persist_claim_progress", crash)
    with pytest.raises(SystemExit, match="terminal checkpoint"):
        webhook._execute_claimed_spooled_task(path, envelope)
    persisted = webhook._load_spooled_task(path)
    assert persisted.get("next_entry_index", 0) == int(after_checkpoint)
    assert len(persisted.get("terminal_unscored", [])) == int(after_checkpoint)
    assert p.calls == [] and _rows() == _rows(LATER) == ([], [])
    monkeypatch.setattr(webhook, "_persist_claim_progress", real_persist)
    _restart_claim(webhook, path)
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    archived = _archived_partial(webhook, task)
    assert archived["attempts"] == 2
    assert len(p.calls) == 1 and len(_rows(LATER)[1]) == 1
    assert _rows() == ([], []) and _record(p.cfg) == before
    terminal_rows = [row for row in _processing_rows() if row["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON]
    assert len(terminal_rows) == (1 if after_checkpoint else 2)


def test_failed_terminal_checkpoint_cannot_skip_article_or_invoke_later_model(terminal_batch, monkeypatch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    real_write = webhook._atomic_write

    def disk_failure(*_args, **_kwargs):
        raise OSError("simulated journal disk failure")

    monkeypatch.setattr(webhook, "_atomic_write", disk_failure)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome is False
    persisted = webhook._load_spooled_task(path)
    assert persisted.get("next_entry_index", 0) == 0
    assert persisted.get("terminal_unscored", []) == []
    assert persisted.get("progress", {"passed": 0, "filtered": 0}) == {"passed": 0, "filtered": 0}
    assert p.calls == [] and _rows() == _rows(LATER) == ([], [])
    assert _record(p.cfg) == before
    monkeypatch.setattr(webhook, "_atomic_write", real_write)
    _finish(webhook, path, envelope, outcome)
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, task)
    assert len(p.calls) == 1 and len(_rows(LATER)[1]) == 1
    assert _rows() == ([], []) and _record(p.cfg) == before


@pytest.mark.parametrize("fallback_recovers", [False, True])
def test_terminal_checkpoint_survives_later_transient_retry_without_renewing_budget(
    terminal_batch, fallback_recovers,
):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p, state="exhausted")
    p.control["mode"] = "unverdicted"
    assert p.run(link=LATER)[1] == processor.SCORE_DEFERRED_REASON
    assert _record(p.cfg, LATER)["attempts"] == 1 and len(p.calls) == 2
    _make_due(p.cfg, LATER)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._SCORE_TRANSPORT_RETRY
    _finish(webhook, path, envelope, outcome)
    pending, = _pending_files(webhook)
    persisted = webhook._load_spooled_task(pending)
    assert persisted["next_entry_index"] == 1
    assert len(persisted["terminal_unscored"]) == 1
    assert persisted["score_transport_entry_index"] == 1
    assert persisted["score_transport_deferrals"] == 1 and persisted["attempts"] == 0
    assert persisted["progress"] == {"passed": 0, "filtered": 0, "terminal_unscored": 1}
    assert _record(p.cfg, LATER)["attempts"] == 2 and len(p.calls) == 4
    _clear_owner_memory(webhook)
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    p.scorer.clear_runtime_cache()
    _make_due(p.cfg, LATER)
    if fallback_recovers:
        p.control["mode"] = "fallback_recovered"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, task, passed=int(fallback_recovers), filtered=int(not fallback_recovers))
    assert _record(p.cfg, LATER)["state"] == "completed"
    assert _record(p.cfg, LATER)["attempts"] == store.MAX_ATTEMPTS
    assert len(p.calls) == 6 and _record(p.cfg) == before and _rows() == ([], [])
    cache, signals = _rows(LATER)
    assert len(cache) == len(signals) == 1
    assert signals[0]["score"] == (8.0 if fallback_recovers else 4.0)
    assert signals[0]["selected"] == int(fallback_recovers)


def test_all_terminal_batch_is_archived_partial_without_models_or_success_receipt(terminal_batch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    first, second = _terminal(p), _terminal(p, link=LATER, state="exhausted")
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    archived = _archived_partial(webhook, task, passed=0, terminals=2)
    for index, link in enumerate((LINK, LATER)):
        _assert_terminal_row(archived["terminal_unscored"][index], p, index=index, link=link)
    assert p.calls == [] and _rows() == _rows(LATER) == ([], [])
    assert _record(p.cfg) == first and _record(p.cfg, LATER) == second
    assert archived["attempts"] == 1


def test_failed_partial_archive_retains_evidence_and_never_reexecutes_completed_scores(
    terminal_batch, monkeypatch,
):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    paid, scored = len(p.calls), _rows(LATER)
    persisted = webhook._load_spooled_task(path)
    assert persisted["next_entry_index"] == 2 and len(persisted["terminal_unscored"]) == 1
    real_write = webhook._atomic_write

    def archive_failure(directory, target, payload):
        if target.parent.name == "dead-letter":
            raise OSError("simulated partial-archive target write failure")
        return real_write(directory, target, payload)

    monkeypatch.setattr(webhook, "_atomic_write", archive_failure)
    _finish(webhook, path, envelope, outcome)
    with webhook._locked_spool() as directory:
        assert webhook._load_receipts_unlocked(directory) == {}
    assert list((directory / "dead-letter").glob("*.json")) == []
    retained = list((directory / "inflight").glob("*.json")) + _pending_files(webhook)
    assert len(retained) == 1
    checkpoint = webhook._load_spooled_task(retained[0])
    assert checkpoint["next_entry_index"] == 2
    assert checkpoint["terminal_unscored"] == persisted["terminal_unscored"]
    monkeypatch.setattr(webhook, "_atomic_write", real_write)
    _clear_owner_memory(webhook)
    webhook._archive_retry_at.clear()
    assert webhook._claim_next_spooled_task(now=time.time() + 100_000) is None
    archived = _archived_partial(webhook, task)
    assert archived["attempts"] == persisted["attempts"]
    assert len(p.calls) == paid and _rows(LATER) == scored
    assert _rows() == ([], []) and _record(p.cfg) == before


def test_terminal_without_durable_context_cannot_be_skipped(terminal_batch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p)
    assert not hasattr(webhook._task_execution_context, "progress_callback")
    assert webhook._score_in_background(task) is False
    assert p.calls == [] and _rows() == _rows(LATER) == ([], [])
    assert _record(p.cfg) == before
    assert _pending_files(webhook) == []
    assert list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json")) == []
    with webhook._locked_spool() as directory:
        assert webhook._load_receipts_unlocked(directory) == {}


def test_ordinary_successful_batch_keeps_existing_success_receipt(terminal_batch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    task["entries"][1]["title"] = TITLE + " with a fresh update"
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    assert webhook._execute_claimed_spooled_task(path, envelope) is True
    current = webhook._load_spooled_task(path)
    assert current["progress"] == {"passed": 2, "filtered": 0}
    assert "terminal_unscored" not in current
    assert len(p.calls) == 2 and len(_rows()[1]) == len(_rows(LATER)[1]) == 1
    _finish(webhook, path, envelope, True)
    assert _pending_files(webhook) == []
    assert list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json")) == []
    with webhook._locked_spool() as directory:
        assert set(webhook._load_receipts_unlocked(directory)) == {envelope["digest"]}
    assert webhook._persist_webhook_task(task) == "duplicate"


@pytest.mark.parametrize("all_terminal", [False, True])
def test_terminal_batch_feed_health_reports_partial_not_success(terminal_batch, monkeypatch, all_terminal):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    _terminal(p)
    if all_terminal:
        _terminal(p, link=LATER, state="exhausted")
    recorded = []
    real_update = webhook.DB.update_feed_health

    def record_health(**kwargs):
        recorded.append(copy.deepcopy(kwargs))
        return real_update(**kwargs)

    monkeypatch.setattr(webhook.DB, "update_feed_health", record_health)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    health, = recorded
    assert health["success"] is False
    assert health["articles_total"] == 2 and health["articles_passed"] == int(not all_terminal)
    assert health["error"] == f"终止未评分 {1 + int(all_terminal)} 条"
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, task, passed=int(not all_terminal), terminals=1 + int(all_terminal))


@pytest.mark.parametrize("partial", [False, True])
def test_full_protected_archive_retains_failed_task_without_blocking_other_claims(
    terminal_batch, monkeypatch, partial,
):
    p, webhook = terminal_batch.p, terminal_batch.webhook
    before = _terminal(p)
    monkeypatch.setattr(webhook, "_webhook_dlq_max_tasks", 1)
    monkeypatch.setattr(webhook, "_webhook_max_attempts", 1)
    protected = {"feed_url": FEED, "entries": terminal_batch.task["entries"][:1]}
    assert webhook._persist_webhook_task(protected) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, protected, passed=0)
    archive_path, = (webhook._ensure_spool_dir() / "dead-letter").glob("*.json")
    archive_before = archive_path.read_bytes()

    blocked = copy.deepcopy(terminal_batch.task)
    if not partial:
        blocked["entries"] = blocked["entries"][1:]
    assert webhook._persist_webhook_task(blocked) == "stored"
    path, envelope = _claim(webhook)
    if partial:
        outcome = webhook._execute_claimed_spooled_task(path, envelope)
        assert outcome == webhook._WEBHOOK_PARTIAL
    else:
        outcome = False  # A normal worker failure has exhausted its task budget.
    _finish(webhook, path, envelope, outcome)
    assert path.exists()
    retained = webhook._load_spooled_task(path)
    assert retained["attempts"] == 1
    assert retained.get("next_entry_index", 0) == (2 if partial else 0)
    assert archive_path.read_bytes() == archive_before
    assert webhook._persist_webhook_task(blocked) == "duplicate"
    assert len(_rows(LATER)[1]) == int(partial) and len(p.calls) == int(partial)

    normal_link = LINK + "/unrelated-to-full-archive"
    normal = {"feed_url": FEED, "entries": [
        {"title": TITLE + " with a fresh update", "url": normal_link, "content": SUMMARY},
    ]}
    assert webhook._persist_webhook_task(normal) == "stored"
    _restart_claim(webhook, path)
    normal_path, normal_envelope = _claim(webhook)
    assert normal_envelope["digest"] == webhook._task_digest(normal)
    assert webhook._execute_claimed_spooled_task(normal_path, normal_envelope) is True
    _finish(webhook, normal_path, normal_envelope, True)
    assert len(_rows(normal_link)[1]) == 1
    assert len(p.calls) == 1 + int(partial)
    assert path.exists() and archive_path.read_bytes() == archive_before
    assert webhook._persist_webhook_task(blocked) == "duplicate"
    assert _record(p.cfg) == before and _rows() == ([], [])
    with webhook._locked_spool() as directory:
        assert set(webhook._load_receipts_unlocked(directory)) == {normal_envelope["digest"]}


def test_terminal_directory_sync_failure_stops_before_later_article(terminal_batch, monkeypatch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    real_sync = webhook._fsync_directory
    strict_calls = []

    def fail_strict(directory, *, strict=False):
        if strict:
            strict_calls.append(directory)
            raise OSError("simulated durable directory sync failure")
        return real_sync(directory, strict=strict)

    monkeypatch.setattr(webhook, "_fsync_directory", fail_strict)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome is False and strict_calls == [path.parent]
    assert p.calls == [] and _rows() == _rows(LATER) == ([], [])
    assert _record(p.cfg) == before
    # Rename may be visible in this process even though durable sync failed.
    # No later item may run on that unacknowledged checkpoint in this attempt.
    current = webhook._load_spooled_task(path)
    assert current["next_entry_index"] == 1 and len(current["terminal_unscored"]) == 1
    _finish(webhook, path, envelope, outcome)
    _clear_owner_memory(webhook)
    path, envelope = _claim(webhook)
    with pytest.raises(OSError, match="directory sync failure") as error:
        webhook._execute_claimed_spooled_task(path, envelope)
    assert strict_calls == [path.parent, path.parent]
    assert p.calls == [] and _rows() == _rows(LATER) == ([], [])
    assert not hasattr(webhook._task_execution_context, "progress_callback")
    _finish(webhook, path, envelope, error.value)
    monkeypatch.setattr(webhook, "_fsync_directory", real_sync)
    _clear_owner_memory(webhook)
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, task)
    assert len(p.calls) == 1 and len(_rows(LATER)[1]) == 1
    assert _rows() == ([], []) and _record(p.cfg) == before


def test_later_score_committed_before_cursor_failure_is_reused_after_restart(terminal_batch, monkeypatch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    real_persist = webhook._persist_claim_progress

    def fail_later_cursor(*args, **kwargs):
        if args[3] == 2 and not kwargs.get("terminal_outcome"):
            return False
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(webhook, "_persist_claim_progress", fail_later_cursor)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome is False
    current = webhook._load_spooled_task(path)
    assert current["next_entry_index"] == 1
    assert current["progress"] == {"passed": 0, "filtered": 0, "terminal_unscored": 1}
    scored, paid = _rows(LATER), len(p.calls)
    assert len(scored[0]) == len(scored[1]) == paid == 1
    assert _rows() == ([], []) and _record(p.cfg) == before
    monkeypatch.setattr(webhook, "_persist_claim_progress", real_persist)
    _finish(webhook, path, envelope, outcome)
    _clear_owner_memory(webhook)
    p.scorer.clear_runtime_cache()
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, task)
    assert len(p.calls) == paid and _rows(LATER) == scored
    assert _rows() == ([], []) and _record(p.cfg) == before


@pytest.mark.parametrize("after_checkpoint", [False, True])
def test_stale_owner_cannot_checkpoint_archive_or_complete_terminal_batch(
    terminal_batch, after_checkpoint,
):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    _terminal(p)
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    if after_checkpoint:
        assert webhook._execute_claimed_spooled_task(path, envelope) == webhook._WEBHOOK_PARTIAL
    with webhook._locked_spool() as directory:
        replaced = webhook._load_spooled_task(path)
        replaced["owner"] = "replacement-worker-owner"
        webhook._atomic_write(directory, path, webhook._canonical_json(replaced))
    before, paid = path.read_bytes(), len(p.calls)
    kwargs = {} if after_checkpoint else {"terminal_outcome": {
        "entry_index": 0,
        "article_key": store.identity(p.cfg["scoring_strategy_version"], LINK)[2],
        "strategy_version": p.cfg["scoring_strategy_version"],
        "outcome": processor.SCORE_TERMINAL_UNSCORED_REASON,
        "recorded_at": time.time(),
    }}
    assert not webhook._persist_claim_progress(
        path, envelope["digest"], envelope["owner"],
        2 if after_checkpoint else 1, int(after_checkpoint), 0, **kwargs,
    )
    assert not webhook._archive_partial_task(path, envelope["digest"], envelope["owner"])
    assert not webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"])
    assert path.read_bytes() == before and len(p.calls) == paid
    assert list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json")) == []
    with webhook._locked_spool() as directory:
        assert webhook._load_receipts_unlocked(directory) == {}


def test_full_archive_retry_waits_sixty_seconds_without_blocking_normal_claims(terminal_batch, monkeypatch):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    monkeypatch.setattr(webhook, "_webhook_max_attempts", 1)
    monkeypatch.setattr(webhook, "_archive_retry_at", OrderedDict())
    clock = {"now": 100.0}
    monkeypatch.setattr(webhook, "time", SimpleNamespace(
        time=time.time, time_ns=time.time_ns, monotonic=lambda: clock["now"],
    ))
    assert webhook._persist_webhook_task(task) == "stored"
    blocked_path, blocked = _claim(webhook)
    _restart_claim(webhook, blocked_path)
    original = webhook._load_spooled_task(blocked_path)
    real_archive, tries = webhook._dead_letter_unlocked, []

    def archive_full(directory, path, envelope, error):
        if path == blocked_path:
            tries.append(clock["now"])
            raise OSError("simulated full protected archive")
        return real_archive(directory, path, envelope, error)

    monkeypatch.setattr(webhook, "_dead_letter_unlocked", archive_full)
    normal = {"feed_url": FEED, "entries": [
        {"title": TITLE, "url": LINK + "/normal-during-backoff", "content": SUMMARY},
    ]}
    assert webhook._persist_webhook_task(normal) == "stored"
    path, envelope = _claim(webhook)
    assert envelope["digest"] == webhook._task_digest(normal) and tries == [100.0]
    assert webhook._execute_claimed_spooled_task(path, envelope) is True
    _finish(webhook, path, envelope, True)
    for point in (100.0, 101.0, 130.0, 159.999):
        clock["now"] = point
        assert webhook._claim_next_spooled_task(now=time.time() + 100_000) is None
        assert tries == [100.0]
        assert webhook._load_spooled_task(blocked_path) == original
    clock["now"] = 160.0
    assert webhook._claim_next_spooled_task(now=time.time() + 100_000) is None
    assert tries == [100.0, 160.0]
    assert webhook._archive_retry_at[str(blocked_path)] == 220.0
    assert webhook._load_spooled_task(blocked_path) == original
    assert webhook._persist_webhook_task(task) == "duplicate"
    assert len(p.calls) == 1 and len(_rows(normal["entries"][0]["url"])[1]) == 1
    assert original["attempts"] == blocked["attempts"] == 1


def test_archive_backoff_cache_is_bounded_and_restart_does_not_renew_task_budget(
    terminal_batch, monkeypatch, tmp_path,
):
    webhook, task = terminal_batch.webhook, terminal_batch.task
    monkeypatch.setattr(webhook, "_webhook_max_attempts", 1)
    monkeypatch.setattr(webhook, "_archive_retry_at", OrderedDict())
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    _restart_claim(webhook, path)
    before = webhook._load_spooled_task(path)

    def unavailable(*_args):
        raise OSError("simulated full archive")

    monkeypatch.setattr(webhook, "_dead_letter_unlocked", unavailable)
    with webhook._locked_spool() as directory:
        for index in range(130):
            assert not webhook._try_dead_letter_unlocked(directory, tmp_path / f"task-{index}.json", None, "full")
        assert len(webhook._archive_retry_at) == 128
        assert not webhook._try_dead_letter_unlocked(directory, path, envelope, "full")
        assert len(webhook._archive_retry_at) == 128
    webhook._archive_retry_at.clear()  # Process-local backoff is not a persisted model budget.
    _clear_owner_memory(webhook)
    assert webhook._claim_next_spooled_task(now=time.time() + 100_000) is None
    assert webhook._load_spooled_task(path) == before
    assert before["attempts"] == 1 and webhook._persist_webhook_task(task) == "duplicate"


@pytest.mark.parametrize("title", ["", "<b> </b>"])
@pytest.mark.parametrize("terminal", ["expired", "exhausted"])
def test_terminal_empty_title_cannot_bypass_audit_or_starve_later_article(
    terminal_batch, title, terminal,
):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    before = _terminal(p, state=terminal)
    task["entries"][0]["title"] = title
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome == webhook._WEBHOOK_PARTIAL and outcome is not True
    current = webhook._load_spooled_task(path)
    assert current["progress"] == {"passed": 1, "filtered": 0, "terminal_unscored": 1}
    _assert_terminal_row(current["terminal_unscored"][0], p)
    _finish(webhook, path, envelope, outcome)
    _archived_partial(webhook, task)
    assert len(p.calls) == 1 and _record(p.cfg) == before and _rows() == ([], [])
    cache, signals = _rows(LATER)
    assert len(cache) == len(signals) == 1 and signals[0]["score"] == 8.0
    first, second = _processing_rows()
    assert first["state"] == "error" and first["outcome"] == processor.SCORE_TERMINAL_UNSCORED_REASON
    assert first["model_attempts"] == 0 and first["signal_key"] is None
    assert second["state"] == "completed" and second["signal_key"] is not None


@pytest.mark.parametrize("title", ["", "<b> </b>"])
def test_unjournaled_empty_title_keeps_input_filter_without_model_call(terminal_batch, title):
    p, webhook, task = terminal_batch.p, terminal_batch.webhook, terminal_batch.task
    task["entries"] = [{"title": title, "url": LINK, "content": SUMMARY}]
    assert webhook._persist_webhook_task(task) == "stored"
    path, envelope = _claim(webhook)
    outcome = webhook._execute_claimed_spooled_task(path, envelope)
    assert outcome is True
    current = webhook._load_spooled_task(path)
    assert current["progress"] == {"passed": 0, "filtered": 1}
    assert current.get("terminal_unscored", []) == []
    _finish(webhook, path, envelope, outcome)
    assert p.calls == [] and _rows() == ([], []) and _record(p.cfg) is None
    diagnostic, = _processing_rows()
    assert diagnostic["state"] == "completed" and diagnostic["outcome"] == "empty_title"
    assert diagnostic["model_attempts"] == 0 and diagnostic["signal_key"] is None
    assert _pending_files(webhook) == []
    assert list((webhook._ensure_spool_dir() / "dead-letter").glob("*.json")) == []
    with webhook._locked_spool() as directory:
        assert set(webhook._load_receipts_unlocked(directory)) == {envelope["digest"]}
