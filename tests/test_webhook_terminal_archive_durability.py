"""A terminal batch keeps its negative receipt until archival is durable."""

import copy
import os
import time
from types import SimpleNamespace

import pytest

from core import processor
from tests.test_natural_score_continuation import (
    FEED, LINK, SUMMARY, TITLE, _record, _rows, pipeline,  # noqa: F401
)
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401
from tests.test_webhook_score_batch_deferral import _clear_owner_memory
from tests.test_webhook_terminal_batch import (
    LATER, _claim, _finish, _terminal, terminal_batch,  # noqa: F401
)


@pytest.fixture
def completed_partial(terminal_batch):  # noqa: F811
    batch = terminal_batch
    original = _terminal(batch.p, state="exhausted")
    assert batch.webhook._persist_webhook_task(batch.task) == "stored"
    path, envelope = _claim(batch.webhook)
    assert batch.webhook._execute_claimed_spooled_task(path, envelope) == batch.webhook._WEBHOOK_PARTIAL
    checkpoint = batch.webhook._load_spooled_task(path)
    assert checkpoint["next_entry_index"] == 2 and len(checkpoint["terminal_unscored"]) == 1
    return SimpleNamespace(
        **vars(batch), path=path, envelope=envelope, checkpoint=checkpoint,
        original=original, paid=len(batch.p.calls), scored=copy.deepcopy(_rows(LATER)),
    )


def _archives(case):
    directory = case.webhook._ensure_spool_dir() / "dead-letter"
    return sorted(directory.glob(case.envelope["digest"] + "*.json"))


def _assert_evidence_and_budget(case):
    paths = ([case.path] if case.path.exists() else []) + _archives(case)
    assert paths, "an unconfirmed archive must retain recoverable terminal evidence"
    for path in paths:
        value = case.webhook._load_spooled_task(path)
        assert value is not None
        assert value["task"] == case.task
        assert value["terminal_unscored"] == case.checkpoint["terminal_unscored"]
        assert value["next_entry_index"] == 2
        assert value["attempts"] == case.checkpoint["attempts"] == 1
        assert value["progress"] == case.checkpoint["progress"]
    assert len(case.p.calls) == case.paid
    assert _rows(LATER) == case.scored and _rows() == ([], [])
    assert _record(case.p.cfg) == case.original
    with case.webhook._locked_spool() as directory:
        assert case.webhook._load_receipts_unlocked(directory) == {}
    assert case.webhook._persist_webhook_task(copy.deepcopy(case.task)) == "duplicate"


def _restart(case):
    _clear_owner_memory(case.webhook)
    case.webhook._archive_retry_at.clear()
    processor._transport_retry_counts.clear()
    processor._transport_pending_writes.clear()
    case.p.scorer.clear_runtime_cache()


def _recover_without_rescoring(case):
    _restart(case)
    assert case.webhook._claim_next_spooled_task(now=time.time() + 300_000) is None
    _assert_evidence_and_budget(case)
    assert not case.path.exists()
    archive, = _archives(case)
    assert archive.name.endswith(".terminal.json")
    assert not list(archive.parent.glob(case.envelope["digest"] + "*.terminal-pending.json"))
    assert case.webhook._load_spooled_task(archive)["last_error"] == case.webhook._WEBHOOK_PARTIAL


def _fail_directory_sync(monkeypatch, directory):
    """Fail the actual directory fsync, including callers that omit strict=True."""
    target = directory.stat()
    original = os.fsync
    observed = []

    def sync(fd):
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) == (target.st_dev, target.st_ino):
            observed.append(fd)
            raise OSError("synthetic archive directory sync failure")
        return original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    return original, observed


def test_archive_directory_sync_failure_is_not_success_and_recovers_without_budget(
    completed_partial, monkeypatch,
):
    case = completed_partial
    directory = case.webhook._ensure_spool_dir() / "dead-letter"
    real_sync, failures = _fail_directory_sync(monkeypatch, directory)
    assert case.webhook._archive_partial_task(
        case.path, case.envelope["digest"], case.envelope["owner"],
    ) is False
    assert failures, "the actual archive directory must be synchronized"
    _assert_evidence_and_budget(case)
    monkeypatch.setattr(os, "fsync", real_sync)
    _recover_without_rescoring(case)


@pytest.mark.parametrize("crash", [False, True])
def test_failure_after_archive_rename_retains_evidence_and_restart_only_finishes_archive(
    completed_partial, monkeypatch, crash,
):
    case = completed_partial
    original = os.replace
    renamed = []

    def replace(source, target):
        original(source, target)
        if os.fspath(target).startswith(os.fspath(case.webhook._ensure_spool_dir() / "dead-letter")):
            renamed.append(os.fspath(target))
            if crash:
                raise SystemExit("worker stopped after archive rename")
            raise OSError("rename completed but archive acknowledgement failed")

    monkeypatch.setattr(os, "replace", replace)
    if crash:
        with pytest.raises(SystemExit, match="after archive rename"):
            case.webhook._archive_partial_task(case.path, case.envelope["digest"], case.envelope["owner"])
    else:
        assert case.webhook._archive_partial_task(
            case.path, case.envelope["digest"], case.envelope["owner"],
        ) is False
    assert renamed
    assert case.path.exists(), "do not delete the source before target-directory durability is confirmed"
    _assert_evidence_and_budget(case)
    monkeypatch.setattr(os, "replace", original)
    _recover_without_rescoring(case)


def test_unconfirmed_archive_does_not_block_unrelated_normal_claim(completed_partial, monkeypatch):
    case = completed_partial
    directory = case.webhook._ensure_spool_dir() / "dead-letter"
    real_sync, failures = _fail_directory_sync(monkeypatch, directory)
    assert case.webhook._archive_partial_task(
        case.path, case.envelope["digest"], case.envelope["owner"],
    ) is False
    assert failures
    normal = {"feed_url": FEED, "entries": [{
        "title": TITLE + " with a different outlook", "url": LINK + "/independent-archive-test",
        "content": SUMMARY,
    }]}
    assert case.webhook._persist_webhook_task(normal) == "stored"
    _restart(case)
    claimed = case.webhook._claim_next_spooled_task(now=time.time() + 300_000)
    assert claimed is not None
    normal_path, normal_envelope = claimed
    assert normal_envelope["digest"] == case.webhook._task_digest(normal)
    _assert_evidence_and_budget(case)
    assert case.webhook._execute_claimed_spooled_task(normal_path, normal_envelope) is True
    _finish(case.webhook, normal_path, normal_envelope, True)
    assert len(case.p.calls) == case.paid + 1
    assert len(_rows(normal["entries"][0]["url"])[1]) == 1
    assert _rows(LATER) == case.scored and _record(case.p.cfg) == case.original
    monkeypatch.setattr(os, "fsync", real_sync)
    _restart(case)
    assert case.webhook._claim_next_spooled_task(now=time.time() + 400_000) is None
    assert not case.path.exists() and len(_archives(case)) == 1
    with case.webhook._locked_spool() as spool:
        assert set(case.webhook._load_receipts_unlocked(spool)) == {normal_envelope["digest"]}


def test_successful_archive_is_idempotent_after_restart_and_duplicate_delivery(completed_partial):
    case = completed_partial
    assert case.webhook._archive_partial_task(
        case.path, case.envelope["digest"], case.envelope["owner"],
    ) is True
    _assert_evidence_and_budget(case)
    original = {path.name: path.read_bytes() for path in _archives(case)}
    _recover_without_rescoring(case)
    assert {path.name: path.read_bytes() for path in _archives(case)} == original


def test_target_write_failure_retains_active_checkpoint_and_restart_only_archives(
    completed_partial, monkeypatch,
):
    case = completed_partial
    original = case.webhook._atomic_write
    writes = []

    def write(directory, target, payload):
        if target.parent.name == "dead-letter":
            writes.append(target)
            raise OSError("target archive could not be written")
        return original(directory, target, payload)

    monkeypatch.setattr(case.webhook, "_atomic_write", write)
    assert case.webhook._archive_partial_task(
        case.path, case.envelope["digest"], case.envelope["owner"],
    ) is False
    assert writes and case.path.exists() and not _archives(case)
    _assert_evidence_and_budget(case)
    monkeypatch.setattr(case.webhook, "_atomic_write", original)
    _recover_without_rescoring(case)


def test_source_unlink_failure_keeps_both_copies_until_recovery(completed_partial, monkeypatch):
    case = completed_partial
    original = os.unlink
    attempts = []

    def unlink(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(case.path):
            attempts.append(path)
            raise OSError("durable archive exists but source unlink failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    assert case.webhook._archive_partial_task(
        case.path, case.envelope["digest"], case.envelope["owner"],
    ) is False
    assert attempts and case.path.exists() and len(_archives(case)) == 1
    _assert_evidence_and_budget(case)
    monkeypatch.setattr(os, "unlink", original)
    _recover_without_rescoring(case)


@pytest.mark.parametrize("crash", [False, True])
def test_source_directory_sync_failure_after_unlink_is_recovered_without_rescore(
    completed_partial, monkeypatch, crash,
):
    case = completed_partial
    parent = case.path.parent.stat()
    original = os.fsync
    failed = []

    def sync(fd):
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) == (parent.st_dev, parent.st_ino) and not case.path.exists():
            failed.append(fd)
            if crash:
                raise SystemExit("worker stopped after source removal")
            raise OSError("source directory sync failed after unlink")
        return original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    if crash:
        with pytest.raises(SystemExit, match="after source removal"):
            case.webhook._archive_partial_task(case.path, case.envelope["digest"], case.envelope["owner"])
    else:
        assert case.webhook._archive_partial_task(
            case.path, case.envelope["digest"], case.envelope["owner"],
        ) is False
    assert failed and not case.path.exists() and len(_archives(case)) == 1
    _assert_evidence_and_budget(case)
    monkeypatch.setattr(os, "fsync", original)
    _recover_without_rescoring(case)


def test_existing_conflicting_archive_is_not_overwritten_or_claimed_for_scoring(
    completed_partial, monkeypatch,
):
    case = completed_partial
    real_sync, failures = _fail_directory_sync(
        monkeypatch, case.webhook._ensure_spool_dir() / "dead-letter",
    )
    assert case.webhook._archive_partial_task(
        case.path, case.envelope["digest"], case.envelope["owner"],
    ) is False
    assert failures and case.path.exists()
    monkeypatch.setattr(os, "fsync", real_sync)
    archive, = _archives(case)
    value = case.webhook._load_spooled_task(archive)
    value["progress"]["passed"] += 1
    with case.webhook._locked_spool() as directory:
        case.webhook._atomic_write(directory, archive, case.webhook._canonical_json(value))
    original = archive.read_bytes()
    _restart(case)
    assert case.webhook._claim_next_spooled_task(now=time.time() + 300_000) is None
    assert case.path.exists() and archive.read_bytes() == original
    active = case.webhook._load_spooled_task(case.path)
    assert active["attempts"] == 1 and active["progress"] == case.checkpoint["progress"]
    assert len(case.p.calls) == case.paid and _rows(LATER) == case.scored
    with case.webhook._locked_spool() as directory:
        assert case.webhook._load_receipts_unlocked(directory) == {}
    assert case.webhook._persist_webhook_task(case.task) == "duplicate"


def test_all_terminal_batch_archive_failure_never_creates_model_or_signal(
    terminal_batch, monkeypatch,  # noqa: F811
):
    batch = terminal_batch
    first, second = _terminal(batch.p), _terminal(batch.p, link=LATER, state="exhausted")
    assert batch.webhook._persist_webhook_task(batch.task) == "stored"
    path, envelope = _claim(batch.webhook)
    assert batch.webhook._execute_claimed_spooled_task(path, envelope) == batch.webhook._WEBHOOK_PARTIAL
    real_sync, failures = _fail_directory_sync(
        monkeypatch, batch.webhook._ensure_spool_dir() / "dead-letter",
    )
    assert batch.webhook._archive_partial_task(path, envelope["digest"], envelope["owner"]) is False
    assert failures and path.exists()
    assert batch.p.calls == [] and _rows() == _rows(LATER) == ([], [])
    monkeypatch.setattr(os, "fsync", real_sync)
    _clear_owner_memory(batch.webhook)
    assert batch.webhook._claim_next_spooled_task(now=time.time() + 300_000) is None
    assert not path.exists()
    archive, = (batch.webhook._ensure_spool_dir() / "dead-letter").glob("*.json")
    value = batch.webhook._load_spooled_task(archive)
    assert value["attempts"] == 1 and value["next_entry_index"] == 2
    assert value["progress"] == {"passed": 0, "filtered": 0, "terminal_unscored": 2}
    assert len(value["terminal_unscored"]) == 2 and batch.p.calls == []
    assert _record(batch.p.cfg) == first and _record(batch.p.cfg, LATER) == second
    assert _rows() == _rows(LATER) == ([], [])
    with batch.webhook._locked_spool() as directory:
        assert batch.webhook._load_receipts_unlocked(directory) == {}


def _interrupt_after_durable_intent(case, monkeypatch):
    """Stop before target I/O, after observing the active intent's strict sync."""
    original_write = case.webhook._atomic_write
    original_sync = case.webhook._fsync_directory
    synchronized = []

    def sync(directory, *, strict=False):
        original_sync(directory, strict=strict)
        synchronized.append((directory, strict))

    def write(directory, target, payload):
        if target.parent.name == "dead-letter":
            assert (case.path.parent, True) in synchronized
            raise SystemExit("worker stopped after durable archive intent")
        return original_write(directory, target, payload)

    with monkeypatch.context() as patch:
        patch.setattr(case.webhook, "_atomic_write", write)
        patch.setattr(case.webhook, "_fsync_directory", sync)
        with pytest.raises(SystemExit, match="after durable archive intent"):
            case.webhook._archive_partial_task(case.path, case.envelope["digest"], None)
    assert case.path.exists() and not _archives(case)
    assert "archive_pending" in case.webhook._load_spooled_task(case.path)
    _assert_evidence_and_budget(case)


def _ordinary_task(suffix):
    return {"feed_url": FEED, "entries": [{
        "title": TITLE + " with an independent update", "url": LINK + suffix, "content": SUMMARY,
    }]}


@pytest.mark.parametrize("field,value", [
    ("source", []), ("source", {}), ("source", None), ("source", True),
    ("source", 1), ("source", "unknown-source"), ("sha256", "0" * 64),
])
def test_invalid_archive_intent_retains_evidence_and_allows_normal_claim(
    completed_partial, monkeypatch, field, value,
):
    case = completed_partial
    _interrupt_after_durable_intent(case, monkeypatch)
    corrupted = case.webhook._load_spooled_task(case.path)
    corrupted["archive_pending"][field] = value
    with case.webhook._locked_spool() as directory:
        case.webhook._atomic_write(directory, case.path, case.webhook._canonical_json(corrupted))
    original = case.path.read_bytes()
    normal = _ordinary_task("/ordinary-after-invalid-intent")
    assert case.webhook._persist_webhook_task(normal) == "stored"
    _restart(case)
    claimed = case.webhook._claim_next_spooled_task(now=time.time() + 300_000)
    assert claimed is not None
    path, envelope = claimed
    assert envelope["digest"] == case.webhook._task_digest(normal)
    assert case.path.read_bytes() == original and not _archives(case)
    _assert_evidence_and_budget(case)
    assert case.webhook._execute_claimed_spooled_task(path, envelope) is True
    _finish(case.webhook, path, envelope, True)
    assert len(case.p.calls) == case.paid + 1
    assert len(_rows(normal["entries"][0]["url"])[1]) == 1
    assert case.path.read_bytes() == original and _rows(LATER) == case.scored
    assert _record(case.p.cfg) == case.original
    with case.webhook._locked_spool() as directory:
        assert set(case.webhook._load_receipts_unlocked(directory)) == {envelope["digest"]}


def test_existing_terminal_target_recovers_when_archive_is_full_without_overwriting_others(
    completed_partial, monkeypatch,
):
    case = completed_partial
    monkeypatch.setattr(case.webhook, "_webhook_dlq_max_tasks", 2)
    real_sync, failures = _fail_directory_sync(
        monkeypatch, case.webhook._ensure_spool_dir() / "dead-letter",
    )
    assert case.webhook._archive_partial_task(case.path, case.envelope["digest"], None) is False
    assert failures and case.path.exists() and len(_archives(case)) == 1
    monkeypatch.setattr(os, "fsync", real_sync)
    other = _ordinary_task("/unrelated-existing-failure-archive")
    assert case.webhook._persist_webhook_task(other) == "stored"
    with case.webhook._locked_spool() as directory:
        other_path = directory / (case.webhook._task_digest(other) + ".json")
        case.webhook._dead_letter_unlocked(
            directory, other_path, case.webhook._load_spooled_task(other_path), "unrelated failure",
        )
    paths = list((directory / "dead-letter").glob("*.json"))
    assert len(paths) == 2
    other_archive, = [path for path in paths if path not in _archives(case)]
    original = other_archive.read_bytes()
    _recover_without_rescoring(case)
    assert len(list((directory / "dead-letter").glob("*.json"))) == 2
    assert other_archive.read_bytes() == original
    assert case.webhook._persist_webhook_task(other) == "duplicate"


@pytest.mark.parametrize("source", ["inflight", "pending"])
def test_durable_intent_crash_recovers_archive_without_claiming_it_before_normal_task(
    completed_partial, monkeypatch, source,
):
    case = completed_partial
    if source == "pending":
        assert case.webhook._retry_or_dead_letter(
            case.path, case.envelope["digest"], case.envelope["owner"], "synthetic worker failure",
        ) == "retry"
        case.path = case.webhook._ensure_spool_dir() / (case.envelope["digest"] + ".json")
        assert case.path.exists()
    _interrupt_after_durable_intent(case, monkeypatch)
    assert case.webhook._load_spooled_task(case.path)["archive_pending"]["source"] == source
    normal = _ordinary_task("/ordinary-after-durable-intent")
    assert case.webhook._persist_webhook_task(normal) == "stored"
    _restart(case)
    claimed = case.webhook._claim_next_spooled_task(now=time.time() + 300_000)
    assert claimed is not None
    path, envelope = claimed
    assert envelope["digest"] == case.webhook._task_digest(normal)
    assert not case.path.exists() and len(_archives(case)) == 1
    _assert_evidence_and_budget(case)
    assert case.webhook._execute_claimed_spooled_task(path, envelope) is True
    _finish(case.webhook, path, envelope, True)
    assert len(case.p.calls) == case.paid + 1
    assert len(_rows(normal["entries"][0]["url"])[1]) == 1
    assert _rows(LATER) == case.scored and _record(case.p.cfg) == case.original
    assert case.webhook._claim_next_spooled_task(now=time.time() + 400_000) is None
    archive, = _archives(case)
    assert case.webhook._load_spooled_task(archive)["attempts"] == 1
    with case.webhook._locked_spool() as directory:
        assert set(case.webhook._load_receipts_unlocked(directory)) == {envelope["digest"]}


@pytest.mark.parametrize("stage", ["after_final_rename", "final_directory_sync"])
@pytest.mark.parametrize("crash", [False, True])
def test_final_archive_rename_failure_never_scores_again_after_restart(
    completed_partial, monkeypatch, stage, crash,
):
    case = completed_partial
    directory = case.webhook._ensure_spool_dir() / "dead-letter"
    identity = directory.stat()
    real_replace, real_sync = os.replace, os.fsync
    failed = []

    def fail():
        failed.append(stage)
        if crash:
            raise SystemExit("worker stopped after final archive rename")
        raise OSError("final archive directory durability unconfirmed")

    def replace(source, target):
        real_replace(source, target)
        if stage == "after_final_rename" and os.fspath(target).endswith(".terminal.json"):
            fail()

    def sync(fd):
        actual = os.fstat(fd)
        if (
            stage == "final_directory_sync"
            and (actual.st_dev, actual.st_ino) == (identity.st_dev, identity.st_ino)
            and list(directory.glob("*.terminal.json"))
        ):
            fail()
        return real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "fsync", sync)
        if crash:
            with pytest.raises(SystemExit, match="after final archive rename"):
                case.webhook._archive_partial_task(case.path, case.envelope["digest"], None)
        else:
            assert case.webhook._archive_partial_task(case.path, case.envelope["digest"], None) is False
    assert failed == [stage] and not case.path.exists()
    completed, = _archives(case)
    assert completed.name.endswith(".terminal.json")
    _assert_evidence_and_budget(case)
    _recover_without_rescoring(case)


@pytest.mark.parametrize("source", ["inflight", "pending"])
def test_reappearing_old_active_intent_only_cleans_up_against_existing_completed_archive(
    completed_partial, monkeypatch, source,
):
    case = completed_partial
    if source == "pending":
        assert case.webhook._retry_or_dead_letter(
            case.path, case.envelope["digest"], case.envelope["owner"], "synthetic worker failure",
        ) == "retry"
        case.path = case.webhook._ensure_spool_dir() / (case.envelope["digest"] + ".json")
    _interrupt_after_durable_intent(case, monkeypatch)
    old_intent = case.path.read_bytes()
    _recover_without_rescoring(case)
    archive, = _archives(case)
    original = archive.read_bytes()
    # Simulate an old active directory entry surviving a crash after unlink;
    # this is a temporary spool only, not a production rollback or queue edit.
    with case.webhook._locked_spool() as directory:
        case.webhook._atomic_write(directory, case.path, old_intent)
    _assert_evidence_and_budget(case)
    _recover_without_rescoring(case)
    assert archive.read_bytes() == original


def test_claim_does_not_read_completed_terminal_archive_bodies(completed_partial, monkeypatch):
    case = completed_partial
    assert case.webhook._archive_partial_task(case.path, case.envelope["digest"], None) is True
    archive, = _archives(case)
    assert archive.name.endswith(".terminal.json")
    normal = _ordinary_task("/claim-does-not-reread-sealed-terminal-archive")
    assert case.webhook._persist_webhook_task(normal) == "stored"
    _restart(case)
    original = case.webhook._load_spooled_task
    reads = []

    def load(path):
        reads.append(path)
        assert path != archive, "ordinary claiming must not deserialize sealed negative receipts"
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(case.webhook, "_load_spooled_task", load)
        claimed = case.webhook._claim_next_spooled_task(now=time.time() + 300_000)
        assert claimed is not None
        path, envelope = claimed
        assert envelope["digest"] == case.webhook._task_digest(normal)
        assert case.webhook._claim_next_spooled_task(now=time.time() + 300_000) is None
    assert reads and archive not in reads
    _assert_evidence_and_budget(case)
    assert case.webhook._execute_claimed_spooled_task(path, envelope) is True
    _finish(case.webhook, path, envelope, True)
    assert len(case.p.calls) == case.paid + 1
    assert _rows(LATER) == case.scored and _record(case.p.cfg) == case.original
