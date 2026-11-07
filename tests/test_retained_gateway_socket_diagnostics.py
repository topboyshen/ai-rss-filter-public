import copy
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from scripts import retained_gateway_configuration as subject
from scripts import retained_gateway_diagnostics as diagnostic
from services import admission_fence as gate

HEADER = b"Num RefCount Protocol Flags Type St Inode Path\n"
LISTENER = b"00000000: 00000002 00000000 00010000 0001 01 123 /run/docker.sock\n"
PENDING = b"00000001: 00000003 00000000 00000000 0001 02 0 /run/docker.sock\n"
ACCEPTED = b"00000002: 00000003 00000000 00000000 0001 03 124 /run/docker.sock\n"
UNRELATED = b"SECRET-KERNEL-POINTER: 00000001 00000000 00000000 0001 03 777 /SECRET-SOCKET-PATH\n"
VALID, BAD = HEADER + LISTENER + ACCEPTED, HEADER + LISTENER + PENDING + UNRELATED


@pytest.fixture
def private_store(monkeypatch, tmp_path):
    parent = tmp_path / "private-outside-upload"
    parent.mkdir(mode=0o700)
    real = tempfile.mkdtemp
    def create(*, prefix, dir):
        assert dir == "/tmp"
        return real(prefix=prefix, dir=parent)
    monkeypatch.setattr(diagnostic.tempfile, "mkdtemp", create)
    return parent


def authority(seconds=2):
    return SimpleNamespace(deadline=time.monotonic() + seconds, pid=456, listener=123, namespace=(1, 2))


def reject(monkeypatch, rows=(BAD, VALID), *, seconds=2):
    values = iter((row, (1, 2, 3)) for row in rows)
    monkeypatch.setattr(subject, "_unix_table", lambda: next(values))
    with diagnostic.scope(enabled=True), diagnostic.phase("peer_confirm", authority(seconds), (456, 0, 0)):
        with pytest.raises(gate.AdmissionError) as caught:
            subject._current_listener()
    return caught.value


def test_original_tables_private_and_safe_public_projection(monkeypatch, private_store):
    error = reject(monkeypatch)
    event = error.gateway_socket_diagnostic
    directory = event["private_directory"]
    assert directory.parent == private_store and stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name, raw in (("first.raw", BAD), ("second.raw", VALID)):
        path = directory / name
        assert path.read_bytes() == raw and stat.S_IMODE(path.stat().st_mode) == 0o600
    value = diagnostic.error_summary(error)
    assert value["retention"] == "retained_privately" and value["failed_at"] == "first_parse"
    assert value["stage"] == "peer_confirm" and value["execution_proof"] is False
    assert value["full_raw_archive_verified"] is False
    assert value["reads"][0]["sha256"] == diagnostic.sha(BAD)
    assert value["reads"][1]["sha256"] == diagnostic.sha(VALID)
    assert value["reads"][0]["conditions"]["reason"] == "inode"
    assert value["reads"][0]["conditions"]["pending_zero"] == 1
    raw = diagnostic.encoded(value)
    assert len(raw) <= diagnostic.PUBLIC_BYTES
    assert all(token not in raw for token in (b"SECRET", b"/tmp", b"/run/docker", str(directory).encode(), b"456"))
    context = json.loads((directory / "CONTEXT.json").read_text())
    assert context["identity"]["peer"] == [456, 0, 0]
    assert context["identity"]["scope"] == "anchor_snapshot_not_current_proof"


@pytest.mark.parametrize("first,second,failed_at", [(BAD, VALID, "first_parse"), (VALID, BAD, "second_parse")])
def test_both_complete_reads_are_retained_even_when_first_parse_fails(monkeypatch, private_store, first, second, failed_at):
    error = reject(monkeypatch, (first, second))
    value = diagnostic.error_summary(error)
    assert value["failed_at"] == failed_at
    assert [x["state"] for x in value["reads"]] == ["complete", "complete"]


def test_no_automatic_retry_and_later_read_does_not_erase_first_rejection(monkeypatch, private_store):
    rows, calls = iter([BAD, BAD, VALID, VALID]), []
    def read():
        calls.append(1)
        return next(rows), (1,)
    monkeypatch.setattr(subject, "_unix_table", read)
    with diagnostic.scope(enabled=True), diagnostic.phase("peer_confirm", authority()):
        with pytest.raises(gate.AdmissionError) as caught:
            subject._current_listener()
        assert len(calls) == 2
        before = copy.deepcopy(diagnostic.error_summary(caught.value))
        assert subject._current_listener()["inode"] == 123
        assert diagnostic.error_summary(caught.value) == before
        assert (caught.value.gateway_socket_diagnostic["private_directory"] / "first.raw").read_bytes() == BAD


def test_separate_archive_failures_do_not_hide_later_actual_failure(monkeypatch, private_store):
    error1 = reject(monkeypatch)
    duplicate = HEADER + LISTENER + ACCEPTED.replace(b" 124 ", b" 123 ")
    error2 = reject(monkeypatch, (duplicate, VALID))
    assert diagnostic.error_summary(error1)["reads"][0]["conditions"]["reason"] == "inode"
    assert diagnostic.error_summary(error2)["reads"][0]["conditions"]["reason"] == "duplicate"
    assert error1.gateway_socket_diagnostic["private_directory"] != error2.gateway_socket_diagnostic["private_directory"]


@pytest.mark.parametrize("side", [0, 1])
def test_failed_read_is_partial_and_unstarted_side_is_not_fabricated(monkeypatch, private_store, side):
    original = OSError("SECRET-READ-ERROR")
    calls = []
    def read():
        index = len(calls)
        calls.append(index)
        if index == side:
            diagnostic.read_failed(original, b"incomplete SECRET bytes", eof=False)
            raise original
        return VALID, (1,)
    monkeypatch.setattr(subject, "_unix_table", read)
    with diagnostic.scope(enabled=True), diagnostic.phase("peer_confirm", authority()):
        with pytest.raises(OSError) as caught:
            subject._current_listener()
    assert caught.value is original
    value = diagnostic.error_summary(original)
    assert value["reads"][side]["state"] == "partial" and value["reads"][side]["eof"] is False
    assert value["reads"][1 - side]["state"] == ("not_started" if side == 0 else "complete")
    assert b"SECRET" not in diagnostic.encoded(value)


def test_unavailable_read_does_not_claim_empty_complete_original(monkeypatch, private_store):
    original = PermissionError("SECRET-path")
    monkeypatch.setattr(subject, "_unix_table", lambda: (_ for _ in ()).throw(original))
    with diagnostic.scope(enabled=True), diagnostic.phase("initial_listener", authority()):
        with pytest.raises(PermissionError) as caught:
            subject._current_listener()
    assert caught.value is original
    value = diagnostic.error_summary(original)
    assert [r["state"] for r in value["reads"]] == ["unavailable", "not_started"]
    assert all(r["sha256"] is None for r in value["reads"])


def test_real_read_error_captures_bounded_partial_bytes(monkeypatch):
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_uid=0, st_gid=0, st_nlink=1, st_size=0, st_dev=1, st_ino=2)
    original = OSError("read failure")
    values = iter([b"partial", original])
    def read(*_args):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(subject.os, "lstat", lambda _p: info)
    monkeypatch.setattr(subject.os, "fstat", lambda _p: info)
    monkeypatch.setattr(subject.os, "open", lambda *_a: 99)
    monkeypatch.setattr(subject.os, "read", read)
    monkeypatch.setattr(subject.os, "close", lambda _fd: None)
    monkeypatch.setattr(subject, "_procfs", lambda _fd: None)
    with diagnostic.scope(enabled=True):
        with pytest.raises(OSError) as caught:
            subject._unix_table()
    assert caught.value is original
    assert original.gateway_socket_read == {"raw": b"partial", "state": "partial", "eof": False, "truncated": False}


def test_partial_byte_cap_is_truthful(monkeypatch, private_store):
    original = gate.AdmissionError("capacity")
    with diagnostic.scope(enabled=True), diagnostic.phase("peer_confirm", authority()):
        diagnostic.read_failed(original, b"x" * (diagnostic.READ_BYTES + 1))
        diagnostic.capture(original, None, None, [None, None], "first_read")
    value = diagnostic.error_summary(original)["reads"][0]
    assert value["bytes"] == diagnostic.READ_BYTES and value["truncated"] is True and value["state"] == "partial"


@pytest.mark.parametrize("failure_point", ["mkdir", "write", "metadata"])
def test_optional_write_failure_never_replaces_original(monkeypatch, private_store, failure_point):
    def fail(*_a, **_k):
        raise OSError("SECRET-WRITE-ERROR")
    if failure_point == "mkdir":
        monkeypatch.setattr(diagnostic.tempfile, "mkdtemp", fail)
    elif failure_point == "write":
        monkeypatch.setattr(diagnostic.os, "write", fail)
    else:
        actual = diagnostic._private_write
        def write(directory, name, raw, deadline):
            return fail() if name == "CONTEXT.json" else actual(directory, name, raw, deadline)
        monkeypatch.setattr(diagnostic, "_private_write", write)
    error = reject(monkeypatch)
    assert str(error) == "Docker archive socket table identity ambiguous"
    value = diagnostic.error_summary(error)
    assert value["retention"] == "write_failed" and b"SECRET" not in diagnostic.encoded(value)


def test_blocking_write_is_bounded_by_original_deadline(monkeypatch, private_store):
    monkeypatch.setattr(diagnostic.os, "write", lambda *_a: time.sleep(1))
    started = time.monotonic()
    error = reject(monkeypatch, seconds=0.03)
    assert time.monotonic() - started < 0.4
    assert str(error) == "Docker archive socket table identity ambiguous"
    assert diagnostic.error_summary(error) is None
    assert error.gateway_socket_diagnostic["retention"] == "deadline_exhausted"


@pytest.mark.parametrize("limit", ["PRIVATE_BYTES", "PRIVATE_EVENTS"])
def test_private_capacity_failure_does_not_grant_success(monkeypatch, private_store, limit):
    monkeypatch.setattr(diagnostic, limit, 0)
    error = reject(monkeypatch)
    assert diagnostic.error_summary(error)["retention"] == "capacity_exhausted"
    assert list(private_store.iterdir()) == []


def test_expired_deadline_never_creates_files(monkeypatch, private_store):
    monkeypatch.setattr(diagnostic, "conditions", lambda _raw: pytest.fail("no projection after deadline"))
    monkeypatch.setattr(diagnostic, "sha", lambda _raw: pytest.fail("no hashing after deadline"))
    error = reject(monkeypatch, seconds=-1)
    assert diagnostic.error_summary(error) is None
    assert error.gateway_socket_diagnostic["retention"] == "deadline_exhausted"
    assert error.gateway_socket_diagnostic["reads"][0]["raw"] == BAD
    assert list(private_store.iterdir()) == []


def test_slow_projection_is_bounded_before_any_file_write(monkeypatch, private_store):
    monkeypatch.setattr(diagnostic, "conditions", lambda _raw: time.sleep(1))
    started = time.monotonic()
    error = reject(monkeypatch, seconds=0.03)
    assert time.monotonic() - started < 0.4
    assert str(error) == "Docker archive socket table identity ambiguous"
    assert diagnostic.error_summary(error) is None
    assert error.gateway_socket_diagnostic["retention"] == "deadline_exhausted"
    assert list(private_store.iterdir()) == []


def test_earlier_outer_alarm_is_never_touched_or_consumed(monkeypatch, private_store):
    calls = []
    monkeypatch.setattr(diagnostic.signal, "getsignal", lambda _sig: lambda *_a: None)
    monkeypatch.setattr(diagnostic.signal, "getitimer", lambda _which: (0.01, 0.0))
    monkeypatch.setattr(diagnostic.signal, "signal", lambda *_a: calls.append("handler"))
    monkeypatch.setattr(diagnostic.signal, "setitimer", lambda *_a: calls.append("timer"))
    monkeypatch.setattr(diagnostic, "conditions", lambda _raw: pytest.fail("no projection before earlier cancellation"))
    error = reject(monkeypatch)
    assert calls == [] and diagnostic.error_summary(error) is None
    assert error.gateway_socket_diagnostic["retention"] == "deadline_exhausted"
    assert list(private_store.iterdir()) == []


def test_later_outer_alarm_handler_and_remaining_time_are_restored(monkeypatch, private_store):
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    assert previous_timer[0] == 0
    calls = []
    def handler(*_a):
        calls.append("outer")
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    started = time.monotonic()
    try:
        monkeypatch.setattr(diagnostic, "conditions", lambda _raw: time.sleep(0.5))
        error = reject(monkeypatch, seconds=0.03)
        elapsed = time.monotonic() - started
        remaining = signal.getitimer(signal.ITIMER_REAL)[0]
        assert diagnostic.error_summary(error) is None and calls == []
        assert signal.getsignal(signal.SIGALRM) is handler
        assert 0 < remaining <= 1.0 - elapsed + 0.005
        assert elapsed < 0.4
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def test_default_production_path_has_no_collector_or_writes(monkeypatch, private_store):
    monkeypatch.setattr(subject, "_unix_table", lambda: (BAD, (1,)))
    with pytest.raises(gate.AdmissionError) as caught:
        subject._current_listener()
    assert not hasattr(caught.value, "gateway_socket_diagnostic")
    assert list(private_store.iterdir()) == []


@pytest.mark.parametrize("change", ["extra", "raw", "proof", "archive", "phase", "hash", "bounds", "condition", "state"])
def test_public_export_rejects_hostile_or_inconsistent_schema(monkeypatch, private_store, change):
    value = diagnostic.error_summary(reject(monkeypatch))
    if change == "extra":
        value["SECRET"] = "path"
    elif change == "raw":
        value["reads"][0]["raw"] = "SECRET"
    elif change == "proof":
        value["execution_proof"] = True
    elif change == "archive":
        value["full_raw_archive_verified"] = True
    elif change == "phase":
        value["stage"] = "SECRET"
    elif change == "hash":
        value["identity_sha256"] = "SECRET"
    elif change == "bounds":
        value["reads"][0]["bytes"] = diagnostic.READ_BYTES + 1
    elif change == "condition":
        value["reads"][0]["conditions"]["columns_eight"] = "SECRET"
    else:
        value["reads"][0]["state"] = "not_started"
    assert diagnostic.safe_summary(value) is None


def test_public_byte_bound_and_export_failure_are_optional(monkeypatch, private_store):
    error = reject(monkeypatch)
    monkeypatch.setattr(diagnostic, "PUBLIC_BYTES", 1)
    assert diagnostic.error_summary(error) is None
    monkeypatch.setattr(diagnostic, "encoded", lambda _v: (_ for _ in ()).throw(OSError("SECRET")))
    assert diagnostic.error_summary(error) is None


def test_native_failure_and_safe_profile_export_no_private_bytes(monkeypatch, private_store, tmp_path):
    from scripts import verify_production_activation_native as native
    from scripts import production_activation_host_native as host
    error = reject(monkeypatch)
    output = tmp_path / "public-fixture-root"
    output.mkdir()
    failure = native.record_failure(error, Path(__file__).resolve().parents[1], output, "actual_fixed_network_namespace_transport")
    public = host.safe_activation({"status": "failed_or_evidence_insufficient", "failure": failure})
    assert public["failure_socket_listener"]["execution_proof"] is False
    assert b"SECRET" not in diagnostic.encoded(public)
    assert not error.gateway_socket_diagnostic["private_directory"].is_relative_to(output)
    assert not any(p.suffix == ".raw" for p in output.rglob("*"))


@pytest.mark.parametrize("bad", [b" 0 ", b" -1 ", b" 0124 "])
def test_invalid_accepted_inode_remains_rejected(monkeypatch, private_store, bad):
    row = HEADER + LISTENER + ACCEPTED.replace(b" 124 ", bad)
    error = reject(monkeypatch, (row, VALID))
    assert diagnostic.error_summary(error)["reads"][0]["conditions"]["bad_inode"] == 1


@pytest.mark.parametrize("row,reason", [
    (ACCEPTED.replace(b" 124 ", b" 123 "), "duplicate"),
    (ACCEPTED.rstrip() + b" SECRET\n", "columns"),
    (ACCEPTED.replace(b"0001 03", b"0002 03"), "shape"),
])
def test_known_pending_does_not_hide_later_conflicting_rows_in_diagnostics(monkeypatch, private_store, row, reason):
    error = reject(monkeypatch, (HEADER + LISTENER + PENDING + row, VALID))
    conditions = diagnostic.error_summary(error)["reads"][0]["conditions"]
    assert conditions["reason"] == "inode"
    assert conditions[{"duplicate": "duplicate_positive_inode", "columns": "bad_columns", "shape": "unknown_shape"}[reason]] >= 1


def test_outer_job_log_exports_safe_conditions_without_raw_bytes(monkeypatch, private_store, capsys):
    from scripts import verify_production_startup_native as startup
    value = diagnostic.error_summary(reject(monkeypatch))
    packet = {"status": "failed_or_evidence_insufficient", "activation": {
        "status": "failed_or_evidence_insufficient", "failure_type": "AdmissionError",
        "failure_locations": [{"file": "retained_gateway_configuration.py", "line": subject._listener.__code__.co_firstlineno + 1}],
        "failure_socket_listener": value}}
    raw = diagnostic.encoded(packet)
    startup._emit_archive_diagnostic([bytearray(raw), bytearray()], True)
    emitted = capsys.readouterr().err
    result = json.loads(emitted)
    assert result["socket_listener"] == value and result["execution_proof"] is False
    assert result["status"] == "locations_available" and len(emitted.encode()) <= startup.DIAGNOSTIC_BYTES
    assert "SECRET" not in emitted and "private_directory" not in emitted
    base = dict(result)
    del base["socket_listener"]
    monkeypatch.setattr(startup, "DIAGNOSTIC_BYTES", len(diagnostic.encoded(base)) + 1)
    startup._emit_archive_diagnostic([bytearray(raw), bytearray()], True)
    dropped = json.loads(capsys.readouterr().err)
    assert dropped == base


def test_outer_socket_schema_rejects_duplicate_keys_and_incomplete_capture(monkeypatch, private_store, capsys):
    from scripts import verify_production_startup_native as startup
    value = diagnostic.error_summary(reject(monkeypatch))
    packet = {"status": "failed_or_evidence_insufficient", "activation": {
        "status": "failed_or_evidence_insufficient", "failure_socket_listener": value}}
    raw = diagnostic.encoded(packet)
    duplicate = raw.replace(b'"stage":"peer_confirm"', b'"stage":"peer_confirm","stage":"peer_confirm"')
    startup._emit_archive_diagnostic([bytearray(duplicate), bytearray()], True)
    assert "socket_listener" not in json.loads(capsys.readouterr().err)
    startup._emit_archive_diagnostic([bytearray(raw), bytearray()], False)
    assert "socket_listener" not in json.loads(capsys.readouterr().err)


def test_peer_drift_is_still_rejected_without_retry(monkeypatch, private_store):
    current = subject._ArchiveAuthority(time.monotonic() + 1)
    current.pid = 456
    monkeypatch.setattr(current, "confirm", lambda: pytest.fail("wrong peer must not reach confirm"))
    with diagnostic.scope(enabled=True):
        with pytest.raises(gate.AdmissionError, match="peer not recognized"):
            current.peer((999, 0, 0))
    assert list(private_store.iterdir()) == []


@pytest.mark.parametrize("changed", ["namespace", "held_namespace", "listener"])
def test_confirm_identity_drift_still_rejects_with_diagnostics_enabled(monkeypatch, private_store, changed):
    info = SimpleNamespace(st_dev=3, st_ino=2, st_mode=stat.S_IFSOCK | 0o660,
                           st_uid=0, st_gid=0, st_nlink=1, st_size=0, st_mtime_ns=0, st_ctime_ns=0)
    current = subject._ArchiveAuthority(time.monotonic() + 1)
    current.socket, current.pid_raw, current.pid_metadata = info, b"456", ()
    current.namespace_fd, current.namespace = 19, (3, 2)
    current.listener_row = {"inode": 123}
    monkeypatch.setattr(subject.os, "lstat", lambda _p: info)
    monkeypatch.setattr(subject, "_small", lambda *_a: (b"456", ()))
    drift = SimpleNamespace(st_dev=3, st_ino=99)
    monkeypatch.setattr(subject.os, "stat", lambda _p: drift if changed == "namespace" else info)
    monkeypatch.setattr(subject.os, "fstat", lambda _fd: drift if changed == "held_namespace" else info)
    calls = []
    def listener():
        calls.append(1)
        return {"inode": 999}
    monkeypatch.setattr(subject, "_current_listener", listener)
    with diagnostic.scope(enabled=True), diagnostic.phase("peer_confirm", current):
        with pytest.raises(gate.AdmissionError, match="listener/namespace changed"):
            current.confirm()
    assert len(calls) == (1 if changed == "listener" else 0)
    assert list(private_store.iterdir()) == []


# Rootless native Linux mechanism, not a Docker/socket-authority substitute.
# The dedicated child cannot accept until the parent explicitly grants it.
_ORDER_SERVER = r'''
import json, os, socket, struct, sys, time
fd, path, deadline = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
control = socket.socket(fileno=fd)
def left():
    value = deadline - time.monotonic()
    assert value > 0
    return value
def emit(value):
    control.settimeout(left())
    raw = json.dumps(value).encode()
    assert control.send(raw) == len(raw)
def command(expected):
    control.settimeout(left())
    raw = control.recv(4097)
    assert 0 < len(raw) <= 4096
    value = json.loads(raw)
    if value == {"command": "stop"}:
        raise SystemExit(0)
    assert value == {"command": expected}
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(2)
    emit({"stage": "listening", "pid": os.getpid(), "listener_inode": os.fstat(listener.fileno()).st_ino})
    command("accept")
    listener.settimeout(left())
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(left())
        assert connection.recv(1) == b"Q"
        credentials = struct.pack("3i", os.getpid(), os.getuid(), os.getgid())
        assert connection.sendmsg([b"R"], [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, credentials)]) == 1
        emit({"stage": "response_sent", "accepted_inode": os.fstat(connection.fileno()).st_ino,
              "listener_inode": os.fstat(listener.fileno()).st_ino})
        command("finish")
    emit({"stage": "finished"})
control.close()
'''


class _LinuxSocketOrdering:
    def __init__(self, path):
        self.path = str(path)
        self.deadline = time.monotonic() + 3.0
        # Cleanup is inside the same fixed deadline, not an extra allowance.
        self.work_deadline = self.deadline - 0.25
        self.connections = []
        self.finished = False
        self.control, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", _ORDER_SERVER, str(child.fileno()),
                 self.path, repr(self.work_deadline)], pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            child.close()

    def left(self):
        value = self.work_deadline - time.monotonic()
        assert value > 0, "fixed three-second native ordering deadline exhausted"
        return value

    def receive(self):
        self.control.settimeout(self.left())
        raw = self.control.recv(4097)
        assert 0 < len(raw) <= 4096
        return json.loads(raw)

    def send(self, name):
        self.control.settimeout(self.left())
        raw = json.dumps({"command": name}).encode()
        assert self.control.send(raw) == len(raw)

    def identity(self):
        info = os.lstat(self.path)
        assert stat.S_ISSOCK(info.st_mode)
        return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)

    def table(self):
        # Never export another process's row, pathname or kernel pointer.
        with open("/proc/net/unix", "rb") as stream:
            raw = stream.read(512 * 1024 + 1)
        assert len(raw) <= 512 * 1024
        lines = raw.decode("ascii", "strict").splitlines()
        assert 0 < len(lines) <= 4096 and lines[0].split() == HEADER.decode().split()
        selected = []
        for line in lines[1:]:
            row = line.split()
            if len(row) >= 8 and row[7] == self.path:
                assert len(row) == 8
                row[0] = "00000000:"  # Pointer is irrelevant to the parser.
                selected.append(" ".join(row))
        return HEADER + ("\n".join(selected) + "\n").encode("ascii")

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connections.append(connection)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        connection.settimeout(self.left())
        connection.connect(self.path)
        expected = self.process.pid, os.getuid(), os.getgid()
        assert struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)) == expected
        return connection

    def serve(self, connection):
        self.send("accept")
        connection.settimeout(self.left())
        connection.sendall(b"Q")
        raw, controls, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(12))
        assert raw == b"R" and flags == 0
        assert len(controls) == 1 and controls[0][:2] == (socket.SOL_SOCKET, socket.SCM_CREDENTIALS)
        assert len(controls[0][2]) == 12
        assert struct.unpack("3i", controls[0][2]) == (self.process.pid, os.getuid(), os.getgid())
        sent = self.receive()
        assert sent["stage"] == "response_sent" and sent["accepted_inode"] > 0
        return sent

    def finish(self):
        self.send("finish")
        assert self.receive() == {"stage": "finished"}
        assert self.process.wait(timeout=self.left()) == 0
        self.finished = True

    def cleanup(self):
        try:
            if not self.finished:
                graceful_failure = None
                try:
                    self.send("stop")
                    assert self.process.wait(timeout=self.left()) == 0
                except (AssertionError, OSError, subprocess.TimeoutExpired) as exc:
                    graceful_failure = exc
                if self.process.poll() is None:
                    # Own synthetic child only; TERM is never success evidence.
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=max(0.0, self.deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as exc:
                        raise AssertionError("own synthetic child exit unconfirmed within fixed deadline") from exc
                    raise AssertionError("own synthetic child required TERM; normal exit unconfirmed") from graceful_failure
                if graceful_failure is not None:
                    raise graceful_failure
                assert self.process.returncode == 0
            else:
                assert self.process.poll() == 0
        finally:
            for connection in self.connections:
                connection.close()
            self.control.close()


@pytest.mark.parametrize("case", ["normal", "already_finished", "terminated", "exit_unconfirmed"])
def test_native_ordering_cleanup_reaps_within_original_deadline_and_never_hides_failure(monkeypatch, case):
    native = object.__new__(_LinuxSocketOrdering)
    native.deadline = 10.0
    native.work_deadline = 9.75
    native.finished = case == "already_finished"
    events = []
    native.connections = [SimpleNamespace(close=lambda: events.append("connection_closed"))]
    native.control = SimpleNamespace(close=lambda: events.append("control_closed"))
    monkeypatch.setattr(time, "monotonic", lambda: 9.9)

    class Process:
        returncode = 0 if native.finished else None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("term")

        def wait(self, *, timeout):
            events.append(("wait", timeout))
            if case == "exit_unconfirmed":
                raise subprocess.TimeoutExpired("own synthetic child", timeout)
            self.returncode = -signal.SIGTERM if "term" in events else 0
            return self.returncode

    native.process = Process()

    def stop(name):
        assert name == "stop"
        if case in {"terminated", "exit_unconfirmed"}:
            raise AssertionError("work deadline exhausted")

    native.send = stop
    native.left = lambda: 0.05
    if case in {"normal", "already_finished"}:
        native.cleanup()
        assert "term" not in events and native.process.returncode == 0
    else:
        expected = "required TERM" if case == "terminated" else "exit unconfirmed within fixed deadline"
        with pytest.raises(AssertionError, match=expected):
            native.cleanup()
        assert "term" in events
        waits = [
            event[1] for event in events if isinstance(event, tuple) and event[0] == "wait"
        ]
        assert waits == [pytest.approx(0.1)]
    assert events[-2:] == ["connection_closed", "control_closed"]


@pytest.mark.skipif(sys.platform != "linux", reason="requires actual Linux AF_UNIX lifecycle/procfs")
@pytest.mark.parametrize("case", ["accepted", "other_pending", "second_listener"])
def test_real_linux_connect_accept_order_and_conflicts_remain_strict(monkeypatch, case):
    # A short private path also fits sockaddr_un in containerized CI.
    with tempfile.TemporaryDirectory(prefix="socket-order-", dir="/tmp") as directory:
        native = _LinuxSocketOrdering(Path(directory) / "own.sock")
        monkeypatch.setattr(subject, "SOCKET_PATH", native.path)
        extra_listener = None
        try:
            ready = native.receive()
            assert ready["stage"] == "listening" and ready["pid"] == native.process.pid
            initial_identity = native.identity()
            initial = subject._listener(native.table())
            assert initial["inode"] == ready["listener_inode"]
            if case == "second_listener":
                # Both listener FDs retain the same pathname in procfs. This
                # only unlinks our temporary socket; no host socket is used.
                os.unlink(native.path)
                extra_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                extra_listener.bind(native.path)
                extra_listener.listen(1)
                assert native.identity() != initial_identity
                with pytest.raises(gate.AdmissionError, match="listener is not unique"):
                    subject._listener(native.table())
                return
            connection = native.connect()
            pending = native.table()
            rows = [line.split() for line in pending.decode().splitlines()[1:]]
            assert sum(row[2:7] == ["00000000", "00000000", "0001", "02", "0"] for row in rows) == 1
            assert native.identity() == initial_identity
            with pytest.raises(gate.AdmissionError, match="identity ambiguous"):
                subject._listener(pending)
            sent = native.serve(connection)
            accepted = native.table()
            assert subject._listener(accepted) == initial
            rows = [line.split() for line in accepted.decode().splitlines()[1:]]
            assert sum(row[2:7] == ["00000000", "00000000", "0001", "03", str(sent["accepted_inode"])] for row in rows) == 1
            assert sent["listener_inode"] == initial["inode"] and native.identity() == initial_identity
            if case == "other_pending":
                # Our authenticated response cannot excuse another connection
                # still awaiting accept. No later retry may hide that row.
                native.connect()
                with pytest.raises(gate.AdmissionError, match="identity ambiguous"):
                    subject._listener(native.table())
            native.finish()
            assert native.finished and native.process.returncode == 0
        finally:
            if extra_listener is not None:
                extra_listener.close()
            native.cleanup()
