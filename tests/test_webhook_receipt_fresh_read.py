"""Fresh descriptor verification; no production, sleeps or retry-until-pass."""

import errno
import hashlib
import json
import os
import tracemalloc
from types import SimpleNamespace

import pytest

from services import webhook_receipts as receipts
from tests import test_webhook_durable_delivery as delivery_helpers
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401
from tests.test_webhook_durable_delivery import pending, raw_body, signed_post

delivery = delivery_helpers.delivery


def _private_file(tmp_path, data):
    path = tmp_path / "private.json"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _after_first_read(monkeypatch, callback):
    real_fdopen = os.fdopen
    calls = []

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
            calls.append(size)
            data = self.stream.read(size)
            callback()
            return data

        def seek(self, *_args):
            raise AssertionError("verification must not reuse the buffered reader")

    monkeypatch.setattr(os, "fdopen", Reader)
    return calls


@pytest.mark.parametrize("position", ["first", "middle", "last", "chunk-boundary"])
def test_simulated_equal_metadata_valid_json_rewrite_is_detected(tmp_path, monkeypatch, position):
    original = b'{"body":"' + b"x" * (2 * receipts._READ_VERIFY_CHUNK + 13) + b'"}'
    offsets = {"first": 2, "middle": len(original) // 2,
               "last": len(original) - 3, "chunk-boundary": receipts._READ_VERIFY_CHUNK}
    offset = offsets[position]
    changed = original[:offset] + b"z" + original[offset + 1:]
    assert len(original) == len(changed)
    assert json.loads(original) != json.loads(changed)
    path = _private_file(tmp_path, original)
    frozen = path.stat()
    # Simulate timestamp equality explicitly; real filesystem evidence is a
    # separate case below and never assumes a particular timestamp resolution.
    monkeypatch.setattr(os, "fstat", lambda _fd: frozen)
    reads = _after_first_read(monkeypatch, lambda: path.write_bytes(changed))
    with pytest.raises(receipts.ReceiptCorrupt, match="changed during") as caught:
        receipts._read(path)
    assert not isinstance(caught.value, receipts.ReceiptMalformedJSON)
    assert path.read_bytes() == changed
    assert reads == [len(original) + 1]


@pytest.mark.parametrize("rewrite", [False, True], ids=["unchanged", "real-equal-size-rewrite"])
def test_real_filesystem_valid_json_content_is_rechecked(tmp_path, monkeypatch, rewrite):
    original = b'{"body":"original"}'
    changed = b'{"body":"modified"}'
    assert len(original) == len(changed)
    assert json.loads(original) != json.loads(changed)
    path = _private_file(tmp_path, original)

    def mutate_if_requested():
        if rewrite:
            # A real in-place write, with unmodified os.fstat/os.pread. The
            # metadata may or may not change; either way stale bytes must fail.
            path.write_bytes(changed)

    _after_first_read(monkeypatch, mutate_if_requested)
    if rewrite:
        with pytest.raises(receipts.ReceiptCorrupt):
            receipts._read(path)
    else:
        assert receipts._read(path) == original
    assert path.read_bytes() == (changed if rewrite else original)


@pytest.mark.parametrize("size", [0, 1, 1177, receipts._READ_VERIFY_CHUNK + 1, receipts.MAX_RECORD_BYTES])
def test_unchanged_equal_metadata_uses_bounded_fresh_descriptor_reads(tmp_path, monkeypatch, size):
    data = b"x" * size
    path = _private_file(tmp_path, data)
    frozen = path.stat()
    monkeypatch.setattr(os, "fstat", lambda _fd: frozen)
    real_pread = os.pread
    requests = []

    def observed_pread(fd, length, offset):
        requests.append((length, offset))
        return real_pread(fd, length, offset)

    monkeypatch.setattr(os, "pread", observed_pread)
    reads = _after_first_read(monkeypatch, lambda: None)
    assert receipts._read(path) == data
    assert reads == [size + 1]
    expected = [(min(receipts._READ_VERIFY_CHUNK, size - offset), offset)
                for offset in range(0, size, receipts._READ_VERIFY_CHUNK)]
    assert requests == expected + [(1, size)]
    assert all(0 < length <= receipts._READ_VERIFY_CHUNK for length, _ in requests)
    assert sum(length for length, _ in requests) == size + 1


def test_fresh_verification_does_not_allocate_another_full_record(tmp_path):
    data = b"x" * receipts.MAX_RECORD_BYTES
    path = _private_file(tmp_path, data)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        tracemalloc.start()
        receipts._verify_read(fd, data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        os.close(fd)
    assert peak < receipts._READ_VERIFY_CHUNK * 4


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_fresh_pass_checks_size_even_when_metadata_is_equal(tmp_path, monkeypatch, mutation):
    original = b'{"body":"original"}'
    changed = original + b" " if mutation == "grow" else original[:-1]
    path = _private_file(tmp_path, original)
    frozen = path.stat()
    monkeypatch.setattr(os, "fstat", lambda _fd: frozen)
    _after_first_read(monkeypatch, lambda: path.write_bytes(changed))
    with pytest.raises(receipts.ReceiptCorrupt, match="changed during"):
        receipts._read(path)


@pytest.mark.parametrize("field", receipts._READ_IDENTITY)
def test_final_metadata_guard_is_not_replaced_by_content_comparison(tmp_path, monkeypatch, field):
    path = _private_file(tmp_path, b'{"body":"unchanged"}')
    frozen = path.stat()
    calls = []

    def changing_stat(_fd):
        calls.append(None)
        values = {key: getattr(frozen, key) for key in receipts._READ_IDENTITY}
        if len(calls) == 4:
            values[field] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(os, "fstat", changing_stat)
    with pytest.raises(receipts.ReceiptCorrupt, match="changed during"):
        receipts._read(path)
    assert len(calls) == 4


@pytest.mark.parametrize("error", [errno.EIO, errno.ENOENT, errno.ESTALE])
@pytest.mark.parametrize("stage", ["open-stat", "before-stat", "after-stat", "final-stat", "pread", "growth-check"])
def test_post_open_io_failure_is_corrupt_not_absence(tmp_path, monkeypatch, error, stage):
    path = _private_file(tmp_path, b'{"body":"unchanged"}')
    real_fstat, real_pread = os.fstat, os.pread
    stats = []
    reads = []
    opened = set()
    stat_stage = {"open-stat": 1, "before-stat": 2, "after-stat": 3, "final-stat": 4}

    def failing_stat(fd):
        opened.add(fd)
        stats.append(None)
        if len(stats) == stat_stage.get(stage):
            raise OSError(error, "synthetic post-open metadata failure")
        return real_fstat(fd)

    def failing_pread(fd, size, offset):
        reads.append(None)
        if len(reads) == {"pread": 1, "growth-check": 2}.get(stage):
            raise OSError(error, "synthetic fresh-read failure")
        return real_pread(fd, size, offset)

    monkeypatch.setattr(os, "fstat", failing_stat)
    monkeypatch.setattr(os, "pread", failing_pread)
    with pytest.raises(receipts.ReceiptCorrupt) as caught:
        receipts._read(path)
    assert not isinstance(caught.value, receipts.ReceiptMalformedJSON)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == error
    assert opened
    for fd in opened:
        with pytest.raises(OSError) as closed:
            real_fstat(fd)
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("operation", ["fdopen", "first-read", "close"])
def test_stream_io_file_not_found_after_open_cannot_mean_absence(tmp_path, monkeypatch, operation):
    path = _private_file(tmp_path, b'{"body":"unchanged"}')
    real_fdopen = os.fdopen
    opened = []

    class Reader:
        def __init__(self, fd, mode):
            opened.append(fd)
            if operation == "fdopen":
                raise FileNotFoundError(errno.ENOENT, "synthetic stream construction failure")
            self.stream = real_fdopen(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            if operation == "close":
                raise FileNotFoundError(errno.ENOENT, "synthetic stream close failure")

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            if operation == "first-read":
                raise FileNotFoundError(errno.ENOENT, "synthetic stream read failure")
            return self.stream.read(size)

    monkeypatch.setattr(os, "fdopen", Reader)
    with pytest.raises(receipts.ReceiptCorrupt):
        receipts._read(path)
    with pytest.raises(OSError) as caught:
        os.fstat(opened[0])
    assert caught.value.errno == errno.EBADF


def test_initial_missing_open_remains_absence_for_store_callers(tmp_path):
    with pytest.raises(FileNotFoundError):
        receipts._read(tmp_path / "missing")


@pytest.mark.parametrize("stage", ["open-stat", "fdopen"])
@pytest.mark.parametrize("error_type", [MemoryError, RuntimeError])
def test_exception_before_stream_ownership_closes_descriptor(tmp_path, monkeypatch, stage, error_type):
    path = _private_file(tmp_path, b'{"body":"unchanged"}')
    real_open, real_fstat = os.open, os.fstat
    opened = []

    def observed_open(*args):
        fd = real_open(*args)
        opened.append(fd)
        return fd

    def fail(*_args):
        raise error_type("synthetic exception before stream ownership")

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(os, "fstat" if stage == "open-stat" else "fdopen", fail)
    with pytest.raises(error_type):
        receipts._read(path)
    assert len(opened) == 1
    with pytest.raises(OSError) as caught:
        real_fstat(opened[0])
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("path", ["/webhook", "/webhook/receipt"])
@pytest.mark.parametrize("failure", ["mismatch", "io", "missing-after-open"])
def test_unstable_existing_receipt_never_acknowledges_or_isolates_as_partial(
    delivery, monkeypatch, path, failure,
):
    body = raw_body(710)
    assert signed_post(delivery, body).status_code == 202
    directory = delivery.webhook._webhook_spool_dir / "receipts"
    final = directory / (hashlib.sha256(body).hexdigest() + ".json")
    original = final.read_bytes()
    inode = final.stat().st_ino
    real_pread = os.pread

    def unstable_pread(fd, size, offset):
        data = real_pread(fd, size, offset)
        if os.fstat(fd).st_ino == inode:
            if failure == "mismatch":
                return b"!" + data[1:] if data else data
            raise OSError(errno.ENOENT if failure == "missing-after-open" else errno.EIO,
                          "synthetic receipt verification failure")
        return data

    def not_partial(*_args):
        raise AssertionError("I/O or instability is not a proven partial JSON prefix")

    monkeypatch.setattr(os, "pread", unstable_pread)
    monkeypatch.setattr(receipts.ReceiptStore, "_held_header", not_partial)
    assert signed_post(delivery, body, path).status_code == 503
    assert signed_post(delivery, raw_body(711)).status_code == 503
    assert final.read_bytes() == original
    assert len(list(directory.glob("*.json"))) == 1
    assert len(pending(delivery)) == 1
    assert delivery.calls == []
    # Removing the injected I/O fault must expose the same durable receipt,
    # not poison it, create a duplicate task, or spend a scoring attempt.
    monkeypatch.setattr(os, "pread", real_pread)
    queried = signed_post(delivery, body, "/webhook/receipt")
    assert queried.status_code == 200
    duplicate = signed_post(delivery, body)
    assert duplicate.status_code == 202
    assert duplicate.get_json()["status"] == "duplicate"
    assert queried.get_json()["task_digest"] == duplicate.get_json()["task_digest"]
    assert final.read_bytes() == original
    assert len(pending(delivery)) == 1
    assert signed_post(delivery, raw_body(711)).status_code == 202
    assert len(pending(delivery)) == 2
    assert delivery.calls == []


@pytest.mark.parametrize("stage", ["initial", "fresh"])
def test_short_read_is_corrupt_without_rewriting_or_leaking_fd(tmp_path, monkeypatch, stage):
    original = b'{"body":"unchanged"}'
    path = _private_file(tmp_path, original)
    real_fdopen, real_pread = os.fdopen, os.pread
    opened = []

    class Reader:
        def __init__(self, fd, mode):
            opened.append(fd)
            self.stream = real_fdopen(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            return data[:-1] if stage == "initial" else data

    def short_pread(fd, size, offset):
        data = real_pread(fd, size, offset)
        return data[:-1] if stage == "fresh" else data

    monkeypatch.setattr(os, "fdopen", Reader)
    monkeypatch.setattr(os, "pread", short_pread)
    with pytest.raises(receipts.ReceiptCorrupt) as caught:
        receipts._read(path)
    assert not isinstance(caught.value, receipts.ReceiptMalformedJSON)
    assert path.read_bytes() == original
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF
