"""Local pipes/subprocesses only; no Docker daemon or production access."""
import builtins
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import production_host_ingress_native as native


INFO = [*native.DOCKER, "info", "--format", "{{json .}}"]


def observation():
    return {"started": False, "stdout_bytes": 0, "stderr_bytes": 0, "stdout_eof": False,
            "stderr_eof": False, "loop_completed": False, "failure_stage": None,
            "finalization_stage": None, "finalization_completed": False}


def valid():
    return {"schema": 1, "execution_proof": False, "operation": "docker_info", "sequence": 11,
            "budget_seconds": 0.1, "elapsed_seconds": 0.101, "failure_phase": "capture",
            "capture": observation(), "client": {"popen_returned": True, "returncode": None, "term_sent": True}}


@pytest.mark.parametrize("path,value", [
    (("schema",), True), (("schema",), 2), (("execution_proof",), True), (("operation",), "docker_other"),
    (("sequence",), True), (("sequence",), 0), (("budget_seconds",), float("nan")),
    (("elapsed_seconds",), float("inf")), (("elapsed_seconds",), 10**400),
    (("failure_phase",), "SECRET"), (("extra",), "SECRET"),
    (("capture", "stdout_bytes"), True), (("capture", "stderr_bytes"), -1),
    (("capture", "stdout_bytes"), native.OUTPUT_BYTES + 65537),
    (("capture", "stdout_eof"), 1), (("capture", "failure_stage"), "SECRET"),
    (("capture", "finalization_stage"), "SECRET"), (("capture", "stdout"), "SECRET"),
    (("client", "returncode"), True), (("client", "returncode"), 256),
    (("client", "term_sent"), 1), (("client", "argv"), "SECRET"),
])
def test_capture_projection_rejects_extra_unsafe_or_unbounded_values(path, value):
    row = valid()
    target = row
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert native.safe_capture_diagnostic(row) is None


def test_projection_copies_and_preserves_unknown_without_claiming_readiness():
    row = valid()
    result = native.safe_capture_diagnostic(row)
    assert result == row and result["execution_proof"] is False
    row["capture"]["stdout_bytes"] = 1
    assert result["capture"]["stdout_bytes"] == 0
    row = valid()
    row["capture"].update(stdout_bytes=native.OUTPUT_BYTES, stderr_bytes=65537)
    assert native.safe_capture_diagnostic(row) is None


def pipes():
    pairs = [os.pipe(), os.pipe()]
    process = SimpleNamespace(stdout=os.fdopen(pairs[0][0], "rb"), stderr=os.fdopen(pairs[1][0], "rb"))
    return process, [pair[1] for pair in pairs]


def close(process, writers):
    for stream in (process.stdout, process.stderr):
        stream.close()
    for fd in writers:
        if fd is not None:
            os.close(fd)


def test_pipe_capture_distinguishes_each_stream_and_complete_eof(tmp_path):
    process, writers = pipes()
    seen = observation()
    try:
        os.write(writers[0], b"stdout secret")
        os.write(writers[1], b"err")
        for index, fd in enumerate(writers):
            os.close(fd)
            writers[index] = None
        native.Commands._capture(process, tmp_path / "out", tmp_path / "err", time.monotonic() + 1,
                                 native.OUTPUT_BYTES, observation=seen)
    finally:
        close(process, writers)
    assert seen == {"started": True, "stdout_bytes": 13, "stderr_bytes": 3, "stdout_eof": True,
                    "stderr_eof": True, "loop_completed": True, "failure_stage": None,
                    "finalization_stage": "close_stderr", "finalization_completed": True}
    assert (tmp_path / "out").read_bytes() == b"stdout secret"


def test_partial_pipe_timeout_retains_primary_unknown_and_original_limits(tmp_path, monkeypatch):
    process, writers = pipes()
    calls = []
    os.write(writers[0], b"partial SECRET")
    os.close(writers[0])
    writers[0] = None
    process.poll = lambda: calls.append("poll") or None
    process.terminate = lambda: calls.append("term")
    process.wait = lambda **_kw: pytest.fail("must not wait after unknown capture")
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_kw: calls.append("popen") or process)
    commands = native.Commands(tmp_path, time.monotonic() + 0.05)
    commands.peer = lambda: calls.append("peer")
    try:
        with pytest.raises(native.ProofError, match="command deadline; outcome unknown") as caught:
            commands.run(INFO)
    finally:
        close(process, writers)
    row = caught.value.native_host_capture
    assert row["capture"] == {"started": True, "stdout_bytes": 14, "stderr_bytes": 0, "stdout_eof": True,
        "stderr_eof": False, "loop_completed": False, "failure_stage": "deadline",
        "finalization_stage": "close_stderr", "finalization_completed": True}
    assert row["failure_phase"] == "capture" and row["client"]["returncode"] is None
    assert 0 < row["budget_seconds"] <= 0.05
    assert calls == ["peer", "popen", "poll", "term", "poll"]
    assert commands.uncertain_mutation is False
    receipt = json.loads((tmp_path / "command-0001.json").read_bytes())
    assert receipt["capture_diagnostic"] == row and receipt["deadline_monotonic"] == commands.deadline
    assert (tmp_path / "command-0001.stdout").read_bytes() == b"partial SECRET"
    assert "SECRET" not in json.dumps(row)
    assert set(caught.value.native_host_command) == {
        "execution_proof", "operation", "sequence", "returncode", "elapsed_seconds", "budget_seconds", "error"}


def local_info(monkeypatch, script):
    actual_popen, children = subprocess.Popen, []

    def launch(args, **kwargs):
        assert args == INFO
        process = actual_popen([sys.executable, "-I", "-B", "-c", script], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(native.subprocess, "Popen", launch)
    return children


def test_real_subprocess_partial_output_then_hang_remains_unknown(tmp_path, monkeypatch):
    children = local_info(monkeypatch, "import os,time;os.write(1,b'PARTIAL_SECRET');os.write(2,b'ERR');time.sleep(10)")
    commands = native.Commands(tmp_path, time.monotonic() + 0.3)
    try:
        with pytest.raises(native.ProofError, match="outcome unknown") as caught:
            commands.run(INFO)
        row = caught.value.native_host_capture
        assert row["capture"]["stdout_bytes"] == 14 and row["capture"]["stderr_bytes"] == 3
        assert row["capture"]["stdout_eof"] is False and row["capture"]["stderr_eof"] is False
        assert row["capture"]["loop_completed"] is False and row["client"]["term_sent"] is True
        assert row["failure_phase"] == "capture" and row["execution_proof"] is False
        assert "SECRET" not in json.dumps(row)
    finally:
        for child in children:
            child.wait(timeout=2)  # Reap only this local test child after the unchanged production TERM.


def test_real_eof_does_not_prove_client_exit(tmp_path, monkeypatch):
    children = local_info(monkeypatch, "import os,time;os.close(1);os.close(2);time.sleep(10)")
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            native.Commands(tmp_path, time.monotonic() + 0.2).run(INFO)
        row = caught.value.native_host_capture
        assert row["failure_phase"] == "wait"
        assert row["capture"]["stdout_eof"] is True and row["capture"]["stderr_eof"] is True
        assert row["capture"]["loop_completed"] is True
        assert row["client"]["term_sent"] is True and row["execution_proof"] is False
    finally:
        for child in children:
            child.wait(timeout=2)


@pytest.mark.parametrize("number,expected", [(1, "sync_stdout"), (2, "sync_stderr")])
def test_capture_sync_failure_never_looks_finalized(tmp_path, monkeypatch, number, expected):
    process, writers = pipes()
    for index, fd in enumerate(writers):
        os.close(fd)
        writers[index] = None
    seen, counter = observation(), []
    original = OSError("private sync error")

    def sync(_fd):
        counter.append(1)
        if len(counter) == number:
            raise original

    monkeypatch.setattr(native.os, "fsync", sync)
    try:
        with pytest.raises(OSError) as caught:
            native.Commands._capture(process, tmp_path / "out", tmp_path / "err", time.monotonic() + 1,
                                     native.OUTPUT_BYTES, observation=seen)
    finally:
        close(process, writers)
    assert caught.value is original
    assert seen["loop_completed"] is True and seen["finalization_completed"] is False
    assert seen["finalization_stage"] == expected


def test_open_failure_is_identified_without_reading_an_existing_file(tmp_path):
    process, writers = pipes()
    (tmp_path / "out").write_bytes(b"retained")
    seen = observation()
    try:
        with pytest.raises(FileExistsError):
            native.Commands._capture(process, tmp_path / "out", tmp_path / "err", time.monotonic() + 1,
                                     native.OUTPUT_BYTES, observation=seen)
    finally:
        close(process, writers)
    assert seen["failure_stage"] == "open" and seen["finalization_completed"] is False
    assert (tmp_path / "out").read_bytes() == b"retained"


def test_output_limit_stays_closed_and_read_count_is_not_durable_size(tmp_path):
    process, writers = pipes()
    seen = observation()
    os.write(writers[0], b"too many bytes")
    for index, fd in enumerate(writers):
        os.close(fd)
        writers[index] = None
    try:
        with pytest.raises(native.ProofError, match="command output limit; partial retained"):
            native.Commands._capture(process, tmp_path / "out", tmp_path / "err", time.monotonic() + 1,
                                     4, observation=seen)
    finally:
        close(process, writers)
    assert seen["stdout_bytes"] == 14 and (tmp_path / "out").read_bytes() == b""
    assert seen["failure_stage"] == "output_bound" and seen["loop_completed"] is False
    assert seen["finalization_completed"] is True


def test_flush_failure_is_not_reported_as_sync_or_complete(tmp_path, monkeypatch):
    process, writers = pipes()
    for index, fd in enumerate(writers):
        os.close(fd)
        writers[index] = None
    seen, original_open = observation(), builtins.open
    original = OSError("private flush failure")

    class Output:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            return self.stream.write(data)

        def fileno(self):
            return self.stream.fileno()

        def flush(self):
            raise original

    def open_output(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return Output(stream) if path == tmp_path / "out" else stream

    monkeypatch.setattr(builtins, "open", open_output)
    monkeypatch.setattr(native.os, "fsync", lambda _fd: pytest.fail("flush must precede sync"))
    try:
        with pytest.raises(OSError) as caught:
            native.Commands._capture(process, tmp_path / "out", tmp_path / "err", time.monotonic() + 1,
                                     native.OUTPUT_BYTES, observation=seen)
    finally:
        close(process, writers)
    assert caught.value is original and seen["loop_completed"] is True
    assert seen["finalization_stage"] == "flush_stdout" and seen["finalization_completed"] is False


def test_nonfixed_command_has_no_capture_projection(tmp_path):
    with pytest.raises(native.ProofError) as caught:
        native.Commands(tmp_path, time.monotonic() + 2).run([sys.executable, "-c", "raise SystemExit(7)"])
    assert not hasattr(caught.value, "native_host_capture")
    assert "capture_diagnostic" not in json.loads((tmp_path / "command-0001.json").read_bytes())


def test_attachment_error_cannot_replace_primary_refusal(tmp_path, monkeypatch):
    original = OSError("SECRET primary popen failure")
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_kw: (_ for _ in ()).throw(original))
    monkeypatch.setattr(native, "safe_capture_diagnostic", lambda *_a: (_ for _ in ()).throw(RuntimeError("diagnostic failure")))
    with pytest.raises(OSError) as caught:
        native.Commands(tmp_path, time.monotonic() + 1).run(INFO)
    assert caught.value is original and not hasattr(original, "native_host_capture")


def test_receipt_failure_has_distinct_phase_and_does_not_invent_exit(tmp_path, monkeypatch):
    original = OSError("receipt sync failure")
    local_info(monkeypatch, "pass")
    monkeypatch.setattr(native, "durable", lambda *_a: (_ for _ in ()).throw(original))
    with pytest.raises(OSError) as caught:
        native.Commands(tmp_path, time.monotonic() + 2).run(INFO)
    row = caught.value.native_host_capture
    assert caught.value is original and row["failure_phase"] == "receipt"
    assert row["client"]["returncode"] == 0 and row["client"]["term_sent"] is False
    assert row["execution_proof"] is False and not (tmp_path / "command-0001.json").exists()
