"""No Docker, network, model calls or deadline expansion: fixed fake clients."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import private_release_contract as private
from scripts import private_release_diagnostics as diag
from scripts import production_native_deadline as native


class Pipe:
    def __init__(self, fd):
        self.fd, self.closed = fd, False

    def fileno(self):
        return self.fd

    def close(self):
        self.closed = True


class Child:
    def __init__(self, *, rc=0, failure=None):
        self.stdout, self.stderr, self.stdin = Pipe(91), Pipe(92), None
        self.pid, self.returncode, self.rc = 321, None, rc
        self.failure, self.wait_calls = failure, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.failure is not None and len(self.wait_calls) == 1:
            raise self.failure
        self.returncode = self.rc
        return self.rc

    def poll(self):
        return self.returncode


class Selector:
    def __init__(self, callback=None):
        self.entries, self.callback = {}, callback

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def register(self, stream, _events, data):
        self.entries[stream.fd] = SimpleNamespace(fileobj=stream, data=data)

    def unregister(self, stream):
        del self.entries[stream.fd]

    def get_map(self):
        return self.entries

    def select(self, timeout):
        if self.callback is not None:
            return self.callback(timeout)
        return [(row, None) for row in list(self.entries.values())]


def install(monkeypatch, *, rc=0, failure=None, stdout=b"result", stderr=b"private stderr", callback=None):
    child = Child(rc=rc, failure=failure)
    spawn = []

    def popen(args, **kwargs):
        spawn.append((args, kwargs))
        return child

    chunks = {91: [stdout, b""], 92: [stderr, b""]}
    monkeypatch.setattr(private.subprocess, "Popen", popen)
    monkeypatch.setattr(private.selectors, "DefaultSelector", lambda: Selector(callback))
    monkeypatch.setattr(private.os, "read", lambda fd, _size: chunks[fd].pop(0))
    kills = []
    monkeypatch.setattr(native.os, "killpg", lambda pid, signal: kills.append((pid, signal)))
    return child, spawn, kills


def at_site(args, *, filename="services/production_startup_control.py", function="_actual_process", env=None):
    namespace = {"private": private, "env": env}
    code = compile(f"def {function}(args):\n    return private.run(args, env=env)\n", str(diag.ROOT / filename), "exec")
    exec(code, namespace)
    return namespace[function](args)


def call(context=None):
    args = ["docker", "exec", "secret-container", "python", "-I", "-c", "SECRET-CODE"]
    if context is None:
        return at_site(args)
    namespace = {"at_site": at_site, "args": args}
    code = compile(f"def {context}():\n    return at_site(args)\n", str(diag.ROOT / "services/production_activation_control.py"), "exec")
    exec(code, namespace)
    return namespace[context]()


def successful_summary(monkeypatch):
    install(monkeypatch)
    with diag.scope() as observation:
        assert call("query") == b"result"
    return observation.summary()


def test_default_off_does_not_inspect_args_stack_or_budget(monkeypatch):
    install(monkeypatch)
    monkeypatch.setattr(diag, "_operation", lambda *_: pytest.fail("diagnostic classification while disabled"))
    monkeypatch.setattr(diag, "_location", lambda *_: pytest.fail("diagnostic stack while disabled"))
    monkeypatch.setattr(diag, "_remaining", lambda *_: pytest.fail("diagnostic budget while disabled"))
    assert diag.STATE.get() is None
    assert call() == b"result"
    assert diag.STATE.get() is None


def test_success_records_real_cap_pipes_and_only_fixed_labels(monkeypatch):
    child, spawn, kills = install(monkeypatch)
    with diag.scope() as observation:
        assert call("query") == b"result"
    value = observation.summary()
    assert value["status"] == "complete" and value["calls"] == 1
    row = value["recent"][0]
    assert (row["operation"], row["callsite"], row["context"]) == ("docker_exec_python", "startup_process", "receiver_query")
    assert row["phase"] == "complete" and row["error"] is None
    assert row["requested_seconds"] == row["clamped_seconds"] == 120
    assert row["returncode"] == 0 and row["stdout_eof"] and row["stderr_eof"]
    assert row["stdout_bytes"] == 6 and row["stderr_bytes"] == 14
    assert row["active_remaining_start"] is None and row["total_remaining_end"] is None
    assert 0 < child.wait_calls[0] <= 120 and kills == []
    assert spawn[0][1]["start_new_session"] is True
    raw = json.dumps(value)
    assert all(secret not in raw for secret in ("secret-container", "SECRET-CODE", "private stderr", str(diag.ROOT)))
    assert diag.safe_summary(value) == value


def test_actual_clamp_and_shared_deadlines_are_only_observed(monkeypatch):
    install(monkeypatch)
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(private.time, "monotonic", lambda: clock.now)
    original = native.clamp
    clamps = []

    def measured(seconds):
        clamps.append(seconds)
        return original(seconds)

    monkeypatch.setattr(native, "clamp", measured)
    with native.scope(15, 25), diag.scope() as observation:
        assert call() == b"result"
    row = observation.summary()["recent"][0]
    assert clamps == [120, 600]  # The existing run clamp and process guard only.
    assert row["clamped_seconds"] == 5
    assert row["active_remaining_start"] == row["active_remaining_end"] == 5
    assert row["total_remaining_start"] == row["total_remaining_end"] == 15


def test_expired_shared_budget_never_spawns_and_preserves_timeout(monkeypatch):
    _child, spawn, _kills = install(monkeypatch)
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(private.time, "monotonic", lambda: clock.now)
    with native.scope(11, 20), diag.scope() as observation:
        clock.now = 12
        with pytest.raises(TimeoutError, match="original shared native deadline expired"):
            call()
    row = observation.summary()["first_failure"]
    assert spawn == [] and row["phase"] == "budget" and row["error"] == "timeout"
    assert row["active_remaining_end"] == -1 and row["clamped_seconds"] is None
    assert row["returncode"] is None


def test_stream_timeout_freezes_before_existing_cleanup(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(private.time, "monotonic", lambda: clock.now)

    def no_data(_timeout):
        clock.now = 131
        return []

    child, _spawn, kills = install(monkeypatch, callback=no_data)
    with diag.scope() as observation:
        with pytest.raises(private.ContractError, match="private release subprocess timed out"):
            call()
        frozen = observation.summary()["first_failure"]
        clock.now = 400
    assert frozen == observation.summary()["first_failure"]
    assert frozen["error"] == "stream_timeout" and frozen["phase"] == "stream"
    assert frozen["elapsed_seconds"] == 121 and frozen["returncode"] is None
    assert not frozen["stdout_eof"] and not frozen["stderr_eof"]
    assert child.returncode == 0 and len(kills) == 1


@pytest.mark.parametrize("failure,code", [
    (subprocess.TimeoutExpired(["secret"], 7, output=b"private", stderr=b"sensitive"), "wait_timeout"),
    (KeyboardInterrupt("private"), "interrupted"),
    (OSError("private"), "os_error"),
])
def test_wait_failure_identity_and_returncode_unknown_survive_cleanup(monkeypatch, failure, code):
    child, _spawn, kills = install(monkeypatch, failure=failure)
    with diag.scope() as observation:
        with pytest.raises(type(failure)) as raised:
            call()
    assert raised.value is failure and len(kills) == 1 and len(child.wait_calls) == 2
    row = observation.summary()["first_failure"]
    assert row["error"] == code and row["phase"] == "wait" and row["returncode"] is None
    assert row["stdout_eof"] and row["stderr_eof"]
    assert "private" not in json.dumps(row) and "sensitive" not in json.dumps(row)


def test_nonzero_rc_is_not_converted_into_success(monkeypatch):
    child, _spawn, kills = install(monkeypatch, rc=7)
    with diag.scope() as observation:
        with pytest.raises(private.ContractError, match="private release subprocess failed"):
            call()
    row = observation.summary()["first_failure"]
    assert row["returncode"] == 7 and row["error"] == "nonzero_rc" and kills == []
    assert len(child.wait_calls) == 2  # Original finish_client still waits for a finished client.


def test_output_limit_still_counts_both_pipes(monkeypatch):
    install(monkeypatch, stdout=b"a" * private.MAX_BYTES, stderr=b"b")
    with diag.scope() as observation:
        with pytest.raises(private.ContractError, match="private release output too large"):
            call()
    row = observation.summary()["first_failure"]
    assert row["error"] == "output_limit" and row["phase"] == "stream"
    assert row["stdout_bytes"] + row["stderr_bytes"] == private.MAX_BYTES + 1


def test_spawn_failure_is_not_masked_or_retried(monkeypatch):
    failure = OSError("private path and credentials")
    starts = []

    def fail(*args, **kwargs):
        starts.append(1)
        raise failure

    monkeypatch.setattr(private.subprocess, "Popen", fail)
    with diag.scope() as observation:
        with pytest.raises(OSError) as raised:
            call()
    assert raised.value is failure and starts == [1]
    row = observation.summary()["first_failure"]
    assert row["phase"] == "spawn" and row["returncode"] is None and row["error"] == "os_error"


def test_observer_internal_failure_does_not_cover_original(monkeypatch):
    failure = subprocess.TimeoutExpired("secret", 7)
    install(monkeypatch, failure=failure)
    monkeypatch.setattr(diag, "_remaining", lambda *_: (_ for _ in ()).throw(RuntimeError("diagnostic fault")))
    with diag.scope() as observation:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            call()
    assert raised.value is failure
    assert observation.summary()["status"] == "evidence_insufficient"


@pytest.mark.parametrize("context,expected", [("prepare", "receiver_prepare"), ("query", "receiver_query"), ("confirm", "receiver_confirm")])
def test_fixed_receiver_contexts(monkeypatch, context, expected):
    install(monkeypatch)
    with diag.scope() as observation:
        call(context)
    assert observation.summary()["recent"][0]["context"] == expected


def test_confirm_context_wins_over_nested_query(monkeypatch):
    install(monkeypatch)
    namespace = {"call": call}
    exec(compile("def confirm():\n    return call('query')\n", str(diag.ROOT / "services/production_activation_control.py"), "exec"), namespace)
    with diag.scope() as observation:
        namespace["confirm"]()
    assert observation.summary()["recent"][0]["context"] == "receiver_confirm"


@pytest.mark.parametrize("args,expected", [
    (["git", "-C", "/private/repo", "status", "--porcelain=v1", "--untracked-files=all"], "git_status"),
    (["git", "-C", "/private/repo", "rev-parse", "HEAD"], "git_revision"),
    (["git", "-C", "/private/repo", "rev-parse", "HEAD^{tree}"], "git_tree"),
    (["docker", "inspect", "private"], "docker_inspect"),
    (["docker", "image", "inspect", "private"], "docker_image_inspect"),
    (["docker", "network", "inspect", "private"], "docker_network_inspect"),
    (["docker", "volume", "inspect", "private"], "docker_volume_inspect"),
    (["docker", "exec", "private", "sha256sum", "/private"], "docker_exec_sha256sum"),
    (["docker", "compose", "--project-name", "private", "-f", "/private", "config", "--format", "json"], "docker_compose_config"),
    (["docker", "exec", "private", "arbitrary"], "unknown"),
    (["docker", "compose", "--arbitrary", "config"], "unknown"),
    (["/private/docker", "inspect", "private"], "unknown"),
    (["unknown-secret", "-C", "/private", "status"], "unknown"),
    (["docker", Path("inspect")], "unknown"),
])
def test_operation_vocabulary_is_fixed(args, expected):
    assert diag._operation(args) == expected
    assert diag._operation(tuple(args)) == expected


def test_unknown_operation_or_site_is_explicit_missing_evidence(monkeypatch):
    install(monkeypatch)
    with diag.scope() as observation:
        assert at_site(["secret-binary"], filename="unknown/private.py", function="secret_function") == b"result"
    value = observation.summary()
    row = value["recent"][0]
    assert value["status"] == "evidence_insufficient" and row["operation"] == row["callsite"] == "unknown"
    assert row["line"] == 0 and "secret" not in json.dumps(value)


def test_ring_cap_retains_first_failure_and_recent_successes(monkeypatch):
    with diag.scope() as observation:
        install(monkeypatch, rc=3)
        with pytest.raises(private.ContractError):
            call()
        frozen = observation.summary()["first_failure"]
        with diag.scope() as nested:
            assert nested is observation
            for _ in range(40):
                install(monkeypatch)
                call()
    value = observation.summary()
    assert value["calls"] == 41 and value["omitted"] == 33 and len(value["recent"]) == 8
    assert value["first_failure"] == frozen and value["recent"][-1]["sequence"] == 41
    assert len(json.dumps(value).encode()) < diag.MAX_BYTES == 8192
    value["first_failure"]["returncode"] = 0
    assert observation.summary()["first_failure"] == frozen  # Snapshot is detached.
    assert diag.STATE.get() is None


@pytest.mark.parametrize("key,value", [
    ("operation", "secret-command"), ("callsite", "/private/path"), ("context", "secret"),
    ("phase", "secret"), ("error", "rawstderr"), ("line", True), ("sequence", True),
    ("returncode", False), ("returncode", 256), ("clamped_seconds", 121), ("clamped_seconds", 0),
    ("elapsed_seconds", float("nan")), ("active_remaining_start", float("inf")),
    ("stdout_bytes", -1), ("stdout_eof", "true"), ("requested_seconds", 121),
    ("stderr_bytes", 4 * 1024 * 1024 + 65537), ("new_key", "secret"),
])
def test_projection_rejects_unknown_or_malformed_fields(monkeypatch, key, value):
    summary = successful_summary(monkeypatch)
    summary["recent"][0][key] = value
    assert diag.safe_summary(summary) is None


def test_projection_rejects_counts_conflicts_and_large_inputs(monkeypatch):
    valid = successful_summary(monkeypatch)
    for change in ({"omitted": 1}, {"calls": True}, {"recent": valid["recent"] * 9},
                   {"raw_stderr": "private"}, {"status": "success"}, {"schema": "private"}):
        assert diag.safe_summary({**valid, **change}) is None
    assert diag.safe_summary(None) is None and diag.safe_summary([valid] * 10000) is None
    assert diag.safe_summary({**valid, "recent": [{"raw": "x" * 10000000}]}) is None
    failed = deepcopy(valid["recent"][0])
    failed.update(state="failed", error="nonzero_rc", returncode=2)
    assert diag.safe_summary({**valid, "first_failure": failed}) is None


def test_projection_never_raises_on_invalid_container_types(monkeypatch):
    valid = successful_summary(monkeypatch)
    for field in ("operation", "callsite", "context", "state", "phase", "error"):
        value = deepcopy(valid)
        value["recent"][0][field] = {"private": "do not stringify"}
        assert diag.safe_summary(value) is None


def test_real_local_subprocess_preserves_stdout_stderr_separation():
    with diag.scope() as observation:
        output = private.run([sys.executable, "-I", "-c", "import sys;print('ordinary');print('private',file=sys.stderr)"])
    assert output == b"ordinary\n"
    value = observation.summary()
    assert value["status"] == "evidence_insufficient"  # Test caller and interpreter are not production allowlist entries.
    row = value["recent"][0]
    assert row["returncode"] == 0 and row["stdout_bytes"] == 9 and row["stderr_bytes"] == 8
    assert "ordinary" not in json.dumps(value) and "private" not in json.dumps(row)


def test_real_local_timeout_does_not_extend_original_shared_deadline():
    started = private.time.monotonic()
    with native.scope(started + 0.05, started + 0.3), diag.scope() as observation:
        with pytest.raises((TimeoutError, private.ContractError)):
            private.run([sys.executable, "-I", "-c", "import time;time.sleep(2)"])
    row = observation.summary()["first_failure"]
    assert row["clamped_seconds"] <= 0.05 and row["error"] in {"stream_timeout", "timeout"}
    assert private.time.monotonic() - started < 1
