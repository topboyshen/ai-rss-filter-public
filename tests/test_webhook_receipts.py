"""Strict receipt store tests; callers serialize via the existing spool flock."""
import base64
import errno
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from services import webhook_receipts as receipts


@pytest.fixture
def store(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    return receipts.ReceiptStore(spool)


@pytest.fixture
def sample():
    return b'{"entries":[{"id":1}]}', {"feed_url": "https://example.test/feed", "entries": []}


def accept(store, sample, **kwargs):
    return store.accept(*sample, strategy="strategy-one", revision="revision-one", accepted_at=123.0, **kwargs)


def test_accept_load_exact_body_and_private_public_scope(store, sample):
    record, created = accept(store, sample)
    assert created and record["schema"] == 1 and record["durable"] is True
    assert base64.b64decode(record["raw_body_b64"]) == sample[0]
    assert record["delivery_id"] == hashlib.sha256(sample[0]).hexdigest()
    assert record["task"] == sample[1]
    assert store.load(record["delivery_id"]) == record
    public = receipts.public_record(record)
    assert not {"raw_body_b64", "task", "_reserved_bytes", "signature", "secret"} & public.keys()


def test_duplicate_keeps_original_acceptance_identity(store, sample):
    original, _ = accept(store, sample)
    repeated, created = store.accept(*sample, strategy="changed", revision="changed", accepted_at=999)
    assert not created and repeated == original


def test_changed_task_same_exact_body_conflicts(store, sample):
    accept(store, sample)
    with pytest.raises(receipts.ReceiptConflict):
        accept(store, (sample[0], {"feed_url": "other", "entries": []}))


def test_different_raw_bodies_same_task_create_alias_no_contract_replacement(store, sample):
    primary, _ = accept(store, sample)
    alias, created = store.accept(b'{"entries": [ {"id": 2} ]}', sample[1], strategy="new", revision="new")
    assert created and alias["delivery_id"] != primary["delivery_id"]
    assert alias["primary_delivery_id"] == primary["delivery_id"]
    assert alias["accepted_strategy"] == primary["accepted_strategy"]
    assert store.find_task(primary["task_digest"]) == primary
    completed = store.update_status(primary["delivery_id"], status="completed", completion={"filtered": 1})
    assert store.find_task(alias["task_digest"]) == completed
    assert store.load(alias["delivery_id"])["status"] == "accepted"


def test_completion_and_terminal_are_immutable(store, sample):
    record, _ = accept(store, sample)
    completed = store.update_status(record["delivery_id"], status="completed", completion={"passed": 1})
    assert store.update_status(record["delivery_id"], status="completed", completion={"passed": 1}) == completed
    for status, completion in (("accepted", None), ("terminal", None), ("completed", {"passed": 2})):
        with pytest.raises(receipts.ReceiptConflict):
            store.update_status(record["delivery_id"], status=status, completion=completion)
    for key in ("delivery_id", "body_sha256", "task_digest", "accepted_at", "accepted_revision", "accepted_strategy", "raw_body_b64", "task"):
        assert completed[key] == record[key]


def test_alias_cannot_mutate_primary_outcome(store, sample):
    primary, _ = accept(store, sample)
    alias, _ = accept(store, (b"different", sample[1]))
    with pytest.raises(receipts.ReceiptConflict):
        store.update_status(alias["delivery_id"], status="terminal")
    assert store.find_task(primary["task_digest"])["status"] == "accepted"


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EIO])
def test_file_sync_failure_not_acknowledged_and_retry_recovers(store, sample, monkeypatch, error):
    original = os.fsync
    def fail_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(error, "synthetic receipt file failure")
        return original(fd)
    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_file)
        with pytest.raises(OSError) as caught:
            accept(store, sample)
        assert caught.value.errno == error
    assert list(store.directory.glob("*.pending"))
    record, created = accept(store, sample)
    assert not created and store.load(record["delivery_id"]) == record


def test_rename_failure_keeps_pending_exact_original(store, sample, monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "synthetic rename")))
        with pytest.raises(OSError):
            accept(store, sample)
    pending, = store.directory.glob("*.pending")
    stored = json.loads(pending.read_bytes())
    assert base64.b64decode(stored["raw_body_b64"]) == sample[0]
    record, _ = accept(store, sample)
    assert record["accepted_at"] == 123


def test_after_rename_directory_sync_failure_requires_rebarrier(store, sample, monkeypatch):
    store._directories(create=True)
    original = receipts._fsync_directory
    with monkeypatch.context() as context:
        context.setattr(receipts, "_fsync_directory", lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "synthetic dir")))
        with pytest.raises(OSError):
            accept(store, sample)
        assert list(store.directory.glob("*.json"))
        with pytest.raises(OSError):
            accept(store, sample)
    calls = []
    monkeypatch.setattr(receipts, "_fsync_directory", lambda path: (calls.append(path), original(path))[1])
    record, created = accept(store, sample)
    assert not created and calls and store.find_task(record["task_digest"]) == record


def test_index_failure_after_primary_record_recovers_on_same_delivery(store, sample, monkeypatch):
    original = store._index
    with monkeypatch.context() as context:
        context.setattr(store, "_index", lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "synthetic index")))
        with pytest.raises(OSError):
            accept(store, sample)
    assert len(list(store.directory.glob("*.json"))) == 1
    record, created = accept(store, sample)
    assert not created and original is not None
    assert store.find_task(record["task_digest"])["delivery_id"] == record["delivery_id"]


def test_unindexed_primary_recovered_before_different_body_alias(store, sample, monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(store, "_index", lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "synthetic index")))
        with pytest.raises(OSError):
            accept(store, sample)
    alias, _ = accept(store, (b"different", sample[1]))
    assert alias["primary_delivery_id"] == hashlib.sha256(sample[0]).hexdigest()


def test_restarted_store_reads_same_receipt_and_index(store, sample):
    record, _ = accept(store, sample)
    replacement = receipts.ReceiptStore(store.spool)
    assert replacement.load(record["delivery_id"]) == record
    assert replacement.find_task(record["task_digest"]) == record


def test_capacity_record_full_refuses_new_but_allows_completion(store, sample):
    store.max_records = 1
    original, _ = accept(store, sample)
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(store, (b"another", sample[1]))
    assert store.update_status(original["delivery_id"], status="terminal", completion={"unscored": 1})["status"] == "terminal"
    assert len(list(store.directory.glob("*.json"))) == 1


def test_exact_reserved_bytes_full_allows_max_bounded_completion(store, sample):
    record, _ = accept(store, sample)
    store.max_bytes = record["_reserved_bytes"]
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(store, (b"different", sample[1]))
    completion = {"reason": "x" * (receipts.MAX_COMPLETION_BYTES - 20)}
    assert store.update_status(record["delivery_id"], status="terminal", completion=completion)["completion"] == completion


def test_large_completion_rejected_without_mutating_original(store, sample):
    record, _ = accept(store, sample)
    with pytest.raises(receipts.ReceiptCapacityError):
        store.update_status(record["delivery_id"], status="completed", completion={"why": "x" * receipts.MAX_COMPLETION_BYTES})
    assert store.load(record["delivery_id"]) == record


def test_completion_sync_failure_then_duplicate_reconfirms(store, sample, monkeypatch):
    record, _ = accept(store, sample)
    original = store._barrier
    def fail_after_completed(path):
        if path.parent == store.directory and json.loads(path.read_bytes()).get("status") == "completed":
            raise OSError(errno.EIO, "synthetic after replacement")
        return original(path)
    with monkeypatch.context() as context:
        context.setattr(store, "_barrier", fail_after_completed)
        with pytest.raises(OSError):
            store.update_status(record["delivery_id"], status="completed", completion={"filtered": 1})
    updated = store.update_status(record["delivery_id"], status="completed", completion={"filtered": 1})
    assert updated["status"] == "completed"


@pytest.mark.parametrize("capacity", [0, -1, True, 1.0, "1", receipts.MAX_RECORDS + 1])
def test_hard_record_capacity_cannot_be_raised(store, capacity):
    with pytest.raises(ValueError):
        receipts.ReceiptStore(store.spool, max_records=capacity)


@pytest.mark.parametrize("capacity", [0, -1, True, 1.0, "1", receipts.MAX_BYTES + 1])
def test_hard_bytes_capacity_cannot_be_raised(store, capacity):
    with pytest.raises(ValueError):
        receipts.ReceiptStore(store.spool, max_bytes=capacity)


@pytest.mark.parametrize("identifier", ["../escape", "/tmp/x", "a" * 63, "A" * 64, None, True])
def test_path_traversal_or_bad_ids_fail_closed(store, identifier):
    with pytest.raises(receipts.ReceiptCorrupt):
        store.load(identifier)
    with pytest.raises(receipts.ReceiptCorrupt):
        store.find_task(identifier)


def test_private_file_and_directory_modes(store, sample):
    record, _ = accept(store, sample)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.tasks.stat().st_mode) == 0o700
    for path in store.directory.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path = store.directory / (record["delivery_id"] + ".json")
    path.chmod(0o644)
    with pytest.raises(receipts.ReceiptCorrupt):
        store.load(record["delivery_id"])


def test_symlink_or_hardlink_receipt_is_rejected(store, sample, tmp_path):
    record, _ = accept(store, sample)
    path = store.directory / (record["delivery_id"] + ".json")
    other = tmp_path / "private-original"
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(OSError):
        store.load(record["delivery_id"])
    path.unlink()
    os.link(other, path)
    with pytest.raises(receipts.ReceiptCorrupt):
        store.load(record["delivery_id"])


def test_task_index_missing_primary_fails_closed(store, sample):
    record, _ = accept(store, sample)
    (store.directory / (record["delivery_id"] + ".json")).unlink()
    with pytest.raises(receipts.ReceiptConflict):
        store.find_task(record["task_digest"])
    with pytest.raises(receipts.ReceiptConflict):
        accept(store, sample)


def test_corrupted_receipt_and_index_are_not_absent(store, sample):
    record, _ = accept(store, sample)
    path = store.directory / (record["delivery_id"] + ".json")
    original = path.read_bytes()
    path.write_bytes(b"{broken")
    with pytest.raises(receipts.ReceiptCorrupt):
        store.load(record["delivery_id"])
    path.write_bytes(original)
    index = store.tasks / (record["task_digest"] + ".json")
    index.write_bytes(b"[]")
    with pytest.raises(receipts.ReceiptCorrupt):
        store.find_task(record["task_digest"])


def test_credentials_not_accepted_as_completion_metadata(store, sample):
    record, _ = accept(store, sample)
    for key in ("secret", "signature", "api_key", "Authorization"):
        with pytest.raises(receipts.ReceiptCorrupt):
            store.update_status(record["delivery_id"], status="completed", completion={"nested": {key: "not-stored"}})


def test_unknown_private_files_fail_new_acceptance_closed(store, sample):
    accept(store, sample)
    unexpected = store.directory / "unexpected"
    unexpected.write_bytes(b"private")
    unexpected.chmod(0o600)
    with pytest.raises(receipts.ReceiptCorrupt):
        accept(store, (b"other", {"entries": [], "feed_url": "other"}))


def test_not_found_returns_none_without_creating_receipt_directory(store):
    assert store.load("0" * 64) is None
    assert store.find_task("0" * 64) is None
    assert not store.directory.exists()


def test_receipt_byte_reservation_includes_index_and_two_write_copies(store, sample):
    record, _ = accept(store, sample)
    path = store.directory / (record["delivery_id"] + ".json")
    index = store.tasks / (record["task_digest"] + ".json")
    assert record["_reserved_bytes"] >= path.stat().st_size * 2 + index.stat().st_size * 2
    assert isinstance(Path(store.directory), Path)


def test_full_pending_restart_uses_original_timestamp_not_retry_clock(store, sample, monkeypatch):
    original = os.fsync
    def fail_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic full pending file")
        return original(fd)
    with monkeypatch.context() as context:
        context.setattr(os, "fsync", fail_file)
        with pytest.raises(OSError):
            accept(store, sample)
    replacement = receipts.ReceiptStore(store.spool)
    recovered, created = replacement.accept(*sample, strategy="new", revision="new", accepted_at=999)
    assert not created and recovered["accepted_at"] == 123
    assert recovered["accepted_strategy"] == "strategy-one"
    assert recovered["accepted_revision"] == "revision-one"


def test_truncated_pending_is_retained_and_never_accepted(store, sample):
    store._directories(create=True)
    delivery_id = hashlib.sha256(sample[0]).hexdigest()
    pending = store.directory / (delivery_id + ".json.pending")
    pending.write_bytes(b'{"_reserved_bytes":')
    pending.chmod(0o600)
    before = pending.read_bytes()
    with pytest.raises(receipts.ReceiptCorrupt):
        accept(store, sample)
    assert pending.read_bytes() == before
    assert not (store.directory / (delivery_id + ".json")).exists()
    # Unknown reservation cannot silently be ignored for another admission.
    with pytest.raises(receipts.ReceiptCorrupt):
        accept(store, (b"other", {"feed_url": "other", "entries": []}))
    assert pending.read_bytes() == before


def test_reservation_tampering_fails_reads_and_new_admissions(store, sample):
    record, _ = accept(store, sample)
    path = store.directory / (record["delivery_id"] + ".json")
    changed = dict(record, _reserved_bytes=record["_reserved_bytes"] - 2)
    path.write_bytes(receipts._encode_record(changed))
    with pytest.raises(receipts.ReceiptCorrupt):
        store.load(record["delivery_id"])
    with pytest.raises(receipts.ReceiptCorrupt):
        accept(store, (b"other", {"feed_url": "other", "entries": []}))


@pytest.mark.parametrize("status", ["completed", "terminal"])
@pytest.mark.parametrize("failure", ["file_fsync", "rename"])
@pytest.mark.parametrize("reader", ["load", "find_task", "accept"])
def test_pending_outcome_recovers_before_exposing_old_accepted(
    store, sample, monkeypatch, status, failure, reader,
):
    accepted, _ = accept(store, sample)
    completion = {"finished_at": 456.0, "entry_count": 1, "terminal_count": int(status == "terminal")}
    real_write, real_sync = store._write, os.fsync
    def interrupted_write(path, payload):
        with monkeypatch.context() as context:
            if failure == "rename":
                context.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "rename")))
            else:
                def fail_file(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError(errno.EIO, "pending file sync")
                    return real_sync(fd)
                context.setattr(os, "fsync", fail_file)
            return real_write(path, payload)
    with monkeypatch.context() as context:
        context.setattr(store, "_write", interrupted_write)
        with pytest.raises(OSError):
            store.update_status(accepted["delivery_id"], status=status, completion=completion)
    final = store.directory / (accepted["delivery_id"] + ".json")
    pending = final.with_name(final.name + ".pending")
    assert json.loads(final.read_bytes())["status"] == "accepted"
    original_intent = json.loads(pending.read_bytes())
    restarted = receipts.ReceiptStore(store.spool)
    monkeypatch.setattr(receipts.time, "time", lambda: 99999.0)
    if reader == "load":
        result = restarted.load(accepted["delivery_id"])
    elif reader == "find_task":
        result = restarted.find_task(accepted["task_digest"])
    else:
        result, created = restarted.accept(*sample, strategy="new", revision="new")
        assert not created
    assert result == original_intent
    assert result["completion"] == completion and result["status"] == status
    assert result["accepted_at"] == accepted["accepted_at"]
    assert not pending.exists()
    assert restarted.load(accepted["delivery_id"]) == original_intent
    with pytest.raises(receipts.ReceiptConflict):
        restarted.update_status(accepted["delivery_id"], status=status, completion={"finished_at": 99999})


@pytest.mark.parametrize("reader", ["load", "find_task", "accept"])
def test_truncated_outcome_pending_never_exposes_existing_accepted(store, sample, reader):
    accepted, _ = accept(store, sample)
    final = store.directory / (accepted["delivery_id"] + ".json")
    pending = final.with_name(final.name + ".pending")
    original = final.read_bytes()
    damaged = receipts._encode_record(dict(accepted, status="terminal", completion={"finished_at": 456}))[:-4]
    pending.write_bytes(damaged)
    pending.chmod(0o600)
    restarted = receipts.ReceiptStore(store.spool)
    with pytest.raises(receipts.ReceiptCorrupt):
        if reader == "load":
            restarted.load(accepted["delivery_id"])
        elif reader == "find_task":
            restarted.find_task(accepted["task_digest"])
        else:
            accept(restarted, sample)
    assert final.read_bytes() == original and pending.read_bytes() == damaged


@pytest.mark.parametrize("change", ["contract", "accepted_metadata", "rollback", "other_terminal"])
def test_pending_write_may_not_rewrite_identity_or_terminal_outcome(store, sample, change):
    original, _ = accept(store, sample)
    if change in {"rollback", "other_terminal"}:
        original = store.update_status(original["delivery_id"], status="completed", completion={"finished_at": 456})
    pending_record = dict(original, status="terminal", completion={"finished_at": 789})
    if change == "contract":
        pending_record["accepted_strategy"] = "strategy-two"
    elif change == "accepted_metadata":
        pending_record["accepted_at"] = 999.0
    elif change == "rollback":
        pending_record["status"] = "accepted"
    final = store.directory / (original["delivery_id"] + ".json")
    before = final.read_bytes()
    pending = final.with_name(final.name + ".pending")
    pending.write_bytes(receipts._encode_record(pending_record))
    pending.chmod(0o600)
    with pytest.raises(receipts.ReceiptError):
        receipts.ReceiptStore(store.spool).load(original["delivery_id"])
    assert final.read_bytes() == before and pending.exists()


def test_small_read_allocates_actual_file_size_not_whole_store_budget(tmp_path, monkeypatch):
    path = tmp_path / "private"
    path.write_bytes(b"x" * 1177)
    path.chmod(0o600)
    real_fdopen = os.fdopen
    requested = []

    class Reader:
        def __init__(self, fd, mode):
            self.stream = real_fdopen(fd, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            requested.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(os, "fdopen", Reader)
    assert receipts._read(path) == b"x" * 1177
    assert requested == [1178]


def test_oversize_sparse_receipt_rejected_before_allocation(tmp_path):
    path = tmp_path / "private"
    with path.open("wb") as out:
        out.truncate(receipts.MAX_RECORD_BYTES + 1)
    path.chmod(0o600)
    with pytest.raises(receipts.ReceiptCorrupt):
        receipts._read(path)


@pytest.mark.parametrize("mutation", ["grow", "shrink", "replace_same_size", "chmod"])
def test_read_mutation_fails_closed(tmp_path, monkeypatch, mutation):
    path = tmp_path / "private"
    path.write_bytes(b"original")
    path.chmod(0o600)
    real_fdopen = os.fdopen

    class Reader:
        def __init__(self, fd, mode):
            self.stream = real_fdopen(fd, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            data = self.stream.read(size)
            if mutation == "chmod":
                path.chmod(0o644)
            else:
                path.write_bytes({"grow": b"original-plus", "shrink": b"x", "replace_same_size": b"changed!"}[mutation])
            return data

    monkeypatch.setattr(os, "fdopen", Reader)
    with pytest.raises(receipts.ReceiptCorrupt, match="changed during"):
        receipts._read(path)


def test_write_safety_limit_retains_original_and_creates_no_pending(store, sample):
    record, _ = accept(store, sample)
    path = store.directory / (record["delivery_id"] + ".json")
    original = path.read_bytes()
    with pytest.raises(receipts.ReceiptCapacityError):
        store._write(path, b"x" * (receipts.MAX_RECORD_BYTES + 1))
    assert path.read_bytes() == original
    assert not path.with_name(path.name + ".pending").exists()


@pytest.mark.parametrize("status", ["completed", "terminal"])
def test_every_byte_prefix_of_real_outcome_keeps_known_identity_held(store, sample, status):
    record, _ = store.accept(*sample, strategy="strategy-one", revision="revision-one", accepted_at=1792630824.1423)
    outcome = dict(record, status=status, completion={
        "finished_at": 1792630999.125, "unicode": "中文证据", "none": None,
        "boolean": True, "negative": -1234.56, "scientific": 1e-10,
        "nested": {"list": [False, None, {"escaped": 'a"b\\c\n'}]},
    })
    payload = receipts._encode_record(outcome)
    for end in range(len(payload)):
        receipts._partial_outcome_identity(payload[:end], record)


@pytest.mark.parametrize("fragment", ['{"completion":bad', '{"completion":[1,]}', '{"completion":{"x":truth'])
def test_invalid_completion_is_not_a_valid_json_prefix(fragment):
    assert not receipts._json_value_prefix(fragment)


@pytest.mark.parametrize("field,value", [
    ("accepted_at", 123456.0), ("accepted_strategy", "different"),
    ("accepted_revision", "different"), ("raw_body_b64", "YWJj"),
])
def test_partial_update_cannot_hide_visible_identity_change(store, sample, field, value):
    record, _ = accept(store, sample)
    outcome = dict(record, status="completed", completion={"finished_at": 456.0})
    outcome[field] = value
    damaged = receipts._encode_record(outcome)[:-2]
    with pytest.raises(receipts.ReceiptError):
        receipts._partial_outcome_identity(damaged, record)


def test_admission_reserves_full_completion_inside_per_file_limit(store, sample, monkeypatch):
    record, _ = accept(store, sample)
    accepted_size = len(receipts._encode_record(record))
    other = receipts.ReceiptStore(store.spool.parent / "other")
    other.spool.mkdir(mode=0o700)
    monkeypatch.setattr(receipts, "MAX_RECORD_BYTES", accepted_size + receipts.MAX_COMPLETION_BYTES + 15)
    with pytest.raises(receipts.ReceiptCapacityError):
        accept(other, sample)
    assert not list(other.directory.glob("*.json"))
