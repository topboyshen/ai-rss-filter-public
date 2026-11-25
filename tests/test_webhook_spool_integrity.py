"""Integrity edges of the durable webhook spool (real files, no dispatcher).

These pin the fail-closed behaviour around the completed
digest ledger, duplicate detection across pending/inflight/dead-letter, and the
"retain, never reinterpret" rule for unreadable or tampered spool files.
"""

from __future__ import annotations

import json
import logging
import os

import pytest


@pytest.fixture
def spool(monkeypatch, tmp_path):
    import routes.webhook_routes as wr

    wr.stop_webhook_dispatcher(timeout=2.0)
    monkeypatch.setattr(wr, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(wr, "_webhook_spool_max_tasks", 128)
    return wr


def _task(n=1):
    return {
        "feed_url": "https://example.com/feed",
        "entries": [{"title": f"t{n}", "url": f"https://example.com/{n}", "content": "c"}],
    }


def _ledger(wr):
    return wr._ensure_spool_dir() / wr._SPOOL_RECEIPTS_FILE


def test_receipt_ledger_skips_malformed_rows(spool):
    good = "a" * 64
    _ledger(spool).write_text(json.dumps([
        [good, 1.5], ["b" * 64], "junk", ["NOT-A-DIGEST", 1], ["c" * 64, "soon"], ["d" * 64, None],
    ]))
    assert dict(spool._load_receipts_unlocked(_ledger(spool).parent)) == {good: 1.5}


def test_receipt_ledger_non_list_is_empty_and_is_trimmed(spool, monkeypatch):
    _ledger(spool).write_text(json.dumps({"a" * 64: 1}))
    assert spool._load_receipts_unlocked(_ledger(spool).parent) == {}
    monkeypatch.setattr(spool, "_webhook_replay_max", 2)
    rows = [[f"{i:x}" * 64, float(i)] for i in range(1, 5)]
    _ledger(spool).write_text(json.dumps(rows))
    assert list(spool._load_receipts_unlocked(_ledger(spool).parent)) == [rows[2][0], rows[3][0]]


def test_completed_digest_is_a_duplicate(spool):
    task = _task()
    _ledger(spool).write_text(json.dumps([[spool._task_digest(task), 1.0]]))
    assert spool._persist_webhook_task(task) == "duplicate"
    assert list(_ledger(spool).parent.glob("*.json")) == []


def test_unreadable_ledger_fails_closed_without_acknowledging(spool, caplog):
    _ledger(spool).write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        spool._persist_webhook_task(_task())
    with caplog.at_level(logging.ERROR, logger=spool.logger.name):
        assert spool._submit_webhook_task(_task()) == "full"
    assert "failed to persist task in durable spool" in caplog.text
    assert list(_ledger(spool).parent.glob("*.json")) == []


@pytest.mark.parametrize("where", ["inflight", "dead-letter"])
def test_task_already_inflight_or_dead_lettered_is_a_duplicate(spool, where):
    task = _task()
    digest = spool._task_digest(task)
    directory = spool._ensure_spool_dir()
    name = f"{digest}.json" if where == "inflight" else f"{digest}.1700000000.json"
    (directory / where / name).write_text("{}")
    assert spool._persist_webhook_task(task) == "duplicate"
    assert not (directory / f"{digest}.json").exists()


# --- tampered / unreadable files are retained, never executed ---------------

def _write_envelope(wr, task, *, digest=None, name=None):
    digest = digest or wr._task_digest(task)
    path = wr._ensure_spool_dir() / (name or f"{digest}.json")
    path.write_bytes(wr._canonical_json(wr._new_spool_envelope(task, digest)))
    return path


def test_tampered_envelope_is_not_loaded_and_is_retained(spool):
    task = _task()
    path = _write_envelope(spool, task)
    envelope = json.loads(path.read_text())
    envelope["task"]["entries"][0]["url"] = "https://changed.example/"
    path.write_text(json.dumps(envelope))
    assert spool._load_spooled_task(path) is None
    assert spool._load_or_migrate_pending_task_unlocked(path.parent, path) is None
    assert path.exists()


def test_envelope_under_wrong_file_name_is_rejected(spool):
    path = _write_envelope(spool, _task(1), name=f"{'e' * 64}.json")
    assert spool._load_spooled_task(path) is None
    assert path.exists()


def test_missing_and_corrupt_spool_files(spool):
    directory = spool._ensure_spool_dir()
    assert spool._load_spooled_task(directory / f"{'f' * 64}.json") is None
    assert spool._load_or_migrate_pending_task_unlocked(directory, directory / "gone.json") is None
    broken = directory / f"{'0' * 64}.json"
    broken.write_text("{truncated")
    assert spool._load_spooled_task(broken) is None
    assert spool._load_or_migrate_pending_task_unlocked(directory, broken) is None
    assert broken.read_text() == "{truncated"


def test_legacy_shape_upgrade_requires_matching_digest(spool):
    directory = spool._ensure_spool_dir()
    task = _task()
    wrong = directory / f"{'1' * 64}.json"
    wrong.write_bytes(spool._canonical_json(task))
    assert spool._load_or_migrate_pending_task_unlocked(directory, wrong) is None
    assert json.loads(wrong.read_text()) == task  # not rewritten

    legacy = directory / f"{spool._task_digest(task)}.json"
    legacy.write_bytes(spool._canonical_json(task))
    os.utime(legacy, (1_700_000_000, 1_700_000_000))
    envelope = spool._load_or_migrate_pending_task_unlocked(directory, legacy)
    assert envelope["task"] == task and envelope["digest"] == legacy.stem
    assert envelope["created_at"] == 1_700_000_000
    assert json.loads(legacy.read_text()) == envelope


def test_dead_letter_trim_keeps_newest(spool, monkeypatch):
    monkeypatch.setattr(spool, "_webhook_dlq_max_tasks", 2)
    dlq = spool._ensure_spool_dir() / spool._SPOOL_DLQ_DIR
    for index in range(4):
        path = dlq / f"{index:064x}.{index}.json"
        path.write_text("{}")
        os.utime(path, (1000 + index, 1000 + index))
    spool._trim_dlq_unlocked(dlq.parent)
    assert sorted(p.name.split(".")[1] for p in dlq.glob("*.json")) == ["2", "3"]
