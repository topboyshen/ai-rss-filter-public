"""Offline failure attribution only; no native host, Docker, or production proof."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import signal
import time
from types import SimpleNamespace

import pytest

from scripts import production_host_ingress_native as native


@pytest.fixture(autouse=True)
def isolated_diagnostics(monkeypatch):
    monkeypatch.setattr(native, "DIAGNOSTICS", {
        "stage": "initial_validation", "last_command": None, "first_failure": None}, raising=False)
    monkeypatch.setattr(native, "LIVE_DAEMONS", [])
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("no subprocess in diagnostic tests"))


def sample_failure():
    return {"schema": 1, "stage": "network_create", "last_command": {
        "sequence": 7, "operation": "docker_network_create", "state": "failed",
        "elapsed_seconds": 3.0, "returncode": None,
        "error": {"type": "ProofError", "sha256": hashlib.sha256(b"command deadline; outcome unknown").hexdigest(),
                  "literal": "command deadline; outcome unknown"}},
        "error": {"type": "ProofError", "sha256": hashlib.sha256(b"private original error").hexdigest(), "literal": None}}


@pytest.fixture
def fake_main(tmp_path, monkeypatch):
    clock, timers = [100.0], []
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda p, **kw: SimpleNamespace(st_uid=0)
                        if p in {native.ROOT, Path(native.__file__)} else original_stat(p, **kw))

    @contextmanager
    def deadline(seconds):
        assert seconds > 0
        timers.append(seconds)
        yield

    monkeypatch.setattr(native.gateway, "_hard_deadline", deadline)

    @contextmanager
    def active(total_deadline):
        timers.append(total_deadline - native.DIAGNOSTIC_RESERVE_SECONDS - clock[0])
        yield

    monkeypatch.setattr(native, "active_phase", active, raising=False)
    root = tmp_path / "originals"
    argv = ["--repo", str(native.ROOT), "--output", str(root), "--deadline-monotonic", "280"]
    return SimpleNamespace(clock=clock, timers=timers, root=root, argv=argv)


def test_original_budget_reserves_diagnostics_without_reset(fake_main, monkeypatch, capsys):
    s = fake_main

    def timeout(root, deadline):
        assert deadline == 280
        root.mkdir()
        (root / "inner").mkdir()
        s.clock[0] = 275.0
        raise native.gateway.gate.AdmissionUncertain("gateway blocking operation deadline; outcome unknown")

    monkeypatch.setattr(native, "outer_proof", timeout)
    monkeypatch.setattr(native, "evidence_packet", lambda _root: {"complete": False})
    assert native.main(s.argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert s.timers == [180.0, 175.0, 3]
    assert s.clock[0] == 275.0
    assert result["status"] == "unconfirmed" and result["cleanup_confirmed"] is False
    assert "first_inner_failure" not in result  # Simultaneous cutoff may precede child durability.
    assert result["safe_evidence"]["complete"] is False


def test_first_inner_failure_survives_outer_timeout_and_inventory_failure(fake_main, monkeypatch, capsys):
    s, first = fake_main, sample_failure()

    def timeout(root, deadline):
        root.mkdir()
        (root / "inner").mkdir()
        native.durable(root / "inner/FIRST_FAILURE.json", first)
        s.clock[0] = 275.0
        raise native.gateway.gate.AdmissionUncertain("gateway blocking operation deadline; outcome unknown")

    monkeypatch.setattr(native, "outer_proof", timeout)
    monkeypatch.setattr(native, "evidence_packet", lambda _root: (_ for _ in ()).throw(OSError("TOKEN=secret /private/path")))
    assert native.main(s.argv) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["first_inner_failure"] == first
    assert result["error_type"] == "AdmissionUncertain"
    assert result["safe_evidence"]["error_type"] == "OSError"
    assert result["cleanup_confirmed"] is False
    assert "TOKEN" not in output and "/private/path" not in output
    assert json.loads((s.root / "inner/FIRST_FAILURE.json").read_bytes()) == first


def test_success_keeps_original_total_bound_and_complete_evidence(fake_main, monkeypatch, capsys):
    s = fake_main

    def success(root, deadline):
        root.mkdir()
        s.clock[0] = 120.0
        return {"status": "passed", "full_activation_verified": False}

    monkeypatch.setattr(native, "outer_proof", success)
    monkeypatch.setattr(native, "evidence_packet", lambda _root: {"complete": True})
    assert native.main(s.argv) == 0
    assert s.timers == [180.0, 175.0]
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed" and result["safe_evidence"]["complete"] is True


def test_active_deadline_only_tightens_original_window(monkeypatch):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    assert native.bounded_deadline(280) == 280
    assert native.active_deadline(280) == 275
    for value in (100, 104, 105):
        with pytest.raises(native.ProofError, match="active budget"):
            native.active_deadline(value)


def test_active_timeout_rearms_total_deadline_before_blocking_failure_cleanup(monkeypatch):
    monkeypatch.setattr(native, "DIAGNOSTIC_RESERVE_SECONDS", 0.04)
    started, cleanup = time.monotonic(), []
    original_handler = signal.getsignal(signal.SIGALRM)
    with pytest.raises(native.gateway.gate.AdmissionUncertain):
        with native.gateway._hard_deadline(0.12):
            with native.active_phase(started + 0.12):
                try:
                    time.sleep(1)
                finally:
                    cleanup.append(time.monotonic() - started)
                    time.sleep(1)  # Stand-in for a blocked original-evidence fsync.
    assert cleanup and 0.02 <= cleanup[0] < 0.5
    assert time.monotonic() - started < 0.75
    assert signal.getsignal(signal.SIGALRM) == original_handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_active_phase_needs_existing_total_timer():
    with pytest.raises(native.ProofError, match="total deadline"):
        with native.active_phase(time.monotonic() + 180):
            pytest.fail("must not open standalone active budget")


def test_initial_failure_is_not_replaced_by_cleanup(tmp_path):
    native.diagnostic_stage("container_start")
    native.diagnostic_command([*native.DOCKER, "start", "TOKEN=private"], 8)
    original = native.ProofError("first private error")
    native.retain_first_failure(tmp_path, original)
    before = (tmp_path / "FIRST_FAILURE.json").read_bytes()
    native.diagnostic_stage("cleanup")
    native.diagnostic_command([*native.DOCKER, "ps", "--all"], 9)
    native.retain_first_failure(tmp_path, OSError("later cleanup error"))
    assert (tmp_path / "FIRST_FAILURE.json").read_bytes() == before
    assert (tmp_path / "FIRST_FAILURE.json").stat().st_mode & 0o777 == 0o600
    result = native.safe_failure_diagnostic(json.loads(before))
    assert result["stage"] == "container_start" and result["last_command"]["sequence"] == 8
    assert result["error"]["sha256"] == hashlib.sha256(b"first private error").hexdigest()
    assert "TOKEN" not in before.decode() and "first private error" not in before.decode()


def test_first_failure_write_error_keeps_primary_and_memory(monkeypatch, tmp_path):
    error = native.ProofError("first private error")
    monkeypatch.setattr(native, "durable", lambda *_: (_ for _ in ()).throw(OSError("write failed")))
    native.retain_first_failure(tmp_path, error)
    assert native.DIAGNOSTICS["first_failure"]["error"]["sha256"] == hashlib.sha256(str(error).encode()).hexdigest()
    assert error.__notes__ == ["first host-entry failure preservation failed: OSError"]


@pytest.mark.parametrize("returncode", [0, 3])
def test_command_observation_does_not_export_argv_or_change_three_second_budget(tmp_path, monkeypatch, returncode):
    clock = [10.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    process = SimpleNamespace(wait=lambda **_kw: returncode, poll=lambda: returncode)
    monkeypatch.setattr(native.subprocess, "Popen", lambda *_a, **_kw: process)
    commands = native.Commands(tmp_path, 180)

    def capture(_process, stdout, stderr, end, limit):
        assert end == 13.0 and limit == native.OUTPUT_BYTES
        stdout.write_bytes(b"retained private original")
        stderr.write_bytes(b"")
        clock[0] = 11.0

    monkeypatch.setattr(commands, "_capture", capture)
    argv = [*native.DOCKER, "network", "create", "TOKEN=private"]
    if returncode:
        with pytest.raises(native.ProofError, match="command failed"):
            commands.run(argv)
    else:
        assert commands.run(argv) == b"retained private original"
    observed = native.DIAGNOSTICS["last_command"]
    assert observed["operation"] == "docker_network_create" and observed["sequence"] == 1
    assert observed["state"] == ("failed" if returncode else "completed")
    assert observed["returncode"] == returncode and observed["elapsed_seconds"] == 1.0
    assert "TOKEN" not in json.dumps(observed)
    assert json.loads((tmp_path / "command-0001.json").read_bytes())["argv"] == argv


def test_expired_diagnostics_never_reads_originals(monkeypatch, tmp_path):
    monkeypatch.setattr(native.time, "monotonic", lambda: 180)
    monkeypatch.setattr(native, "read_original_json", lambda *_: pytest.fail("read after original deadline"))
    monkeypatch.setattr(native, "evidence_packet", lambda *_: pytest.fail("inventory after original deadline"))
    with pytest.raises(native.ProofError, match="no further original reads"):
        native.failure_evidence(tmp_path, 180, {})


def test_stop_fallback_reports_first_inner_error_before_scenario(tmp_path, monkeypatch):
    (tmp_path / "inner").mkdir()
    first = sample_failure()
    native.durable(tmp_path / "inner/STOP.json", {"status": "unconfirmed", "diagnostics": first})
    monkeypatch.setattr(native, "evidence_packet", lambda *_: {"complete": False})
    result = {}
    native.failure_evidence(tmp_path, native.time.monotonic() + 2, result)
    assert result == {"first_inner_failure": first, "safe_evidence": {"complete": False}, "cleanup_confirmed": False}


@pytest.mark.parametrize("fault", ["schema_bool", "extra_field", "unknown_stage", "secret_literal", "literal_hash",
                                  "bad_hash", "sequence_bool", "unknown_operation", "nan_elapsed", "raw_argv"])
def test_untrusted_first_failure_profiles_rejected(fault):
    value = deepcopy(sample_failure())
    if fault == "schema_bool":
        value["schema"] = True
    elif fault == "extra_field":
        value["raw_message"] = "private"
    elif fault == "unknown_stage":
        value["stage"] = "TOKEN=private"
    elif fault == "secret_literal":
        value["error"]["literal"] = "TOKEN=private"
    elif fault == "literal_hash":
        value["error"]["literal"] = "command deadline; outcome unknown"
    elif fault == "bad_hash":
        value["error"]["sha256"] = "not-a-hash"
    elif fault == "sequence_bool":
        value["last_command"]["sequence"] = True
    elif fault == "unknown_operation":
        value["last_command"]["operation"] = "/private/path"
    elif fault == "nan_elapsed":
        value["last_command"]["elapsed_seconds"] = float("nan")
    else:
        value["last_command"]["argv"] = ["TOKEN=private"]
    with pytest.raises(native.ProofError):
        native.safe_failure_diagnostic(value)


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "hardlink"])
def test_first_failure_never_follows_links_or_accepts_shared_original(tmp_path, monkeypatch, kind):
    target = tmp_path / "target"
    target.mkdir()
    native.durable(target / "FIRST_FAILURE.json", sample_failure())
    original = (target / "FIRST_FAILURE.json").read_bytes()
    root = tmp_path / "root"
    root.mkdir()
    if kind == "directory_link":
        (root / "inner").symlink_to(target, target_is_directory=True)
    else:
        (root / "inner").mkdir()
        path = root / "inner/FIRST_FAILURE.json"
        if kind == "file_link":
            path.symlink_to(target / "FIRST_FAILURE.json")
        else:
            native.os.link(target / "FIRST_FAILURE.json", path)
    monkeypatch.setattr(native, "evidence_packet", lambda *_: pytest.fail("invalid diagnostic must stop export"))
    with pytest.raises((native.ProofError, OSError)):
        native.failure_evidence(root, native.time.monotonic() + 2, {})
    assert (target / "FIRST_FAILURE.json").read_bytes() == original


def test_disabled_diagnostics_leave_shared_full_activation_commands_unobserved(monkeypatch):
    monkeypatch.setattr(native, "DIAGNOSTICS", None)
    native.diagnostic_stage("not-a-stage")
    native.diagnostic_command(["/private/path"], 1)
    assert native.DIAGNOSTICS is None


def test_fixed_daemon_start_diagnostics_export_redacted_text_and_info_receipt(tmp_path, monkeypatch):
    root = tmp_path / "inner"
    root.mkdir()
    first = sample_failure()
    first.update(stage="daemon_start")
    first["last_command"].update(operation="docker_info", sequence=14)
    raw = b'time="now" level=error msg="failed plugin load"\ntoken=private-token /private/daemon/path\n'
    native.durable(root / "daemon.stderr", raw)
    native.durable(root / "command-0014.json", {"argv": [*native.DOCKER, "info", "--format", "{{json .}}"],
        "elapsed": 3.001197, "returncode": None, "client_term_sent": True})
    native.durable(root / "command-0014.stderr", b"password=private-password docker info deadline\n")
    native.durable(root / "STOP.json", {"status": "unconfirmed", "cleanup_confirmed": True, "diagnostics": first})
    originals = {p.name: p.read_bytes() for p in root.iterdir()}
    monkeypatch.setattr(native, "evidence_packet", lambda *_: {"complete": False})
    native.durable(root / "FIRST_FAILURE.json", first)
    result = {"cleanup_confirmed": False}
    native.failure_evidence(tmp_path, time.monotonic() + 2, result)
    rows = result["daemon_start_evidence"]
    assert rows["daemon.stderr"]["profile"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "failed plugin load" in rows["daemon.stderr"]["profile"]["text"]
    assert rows["command-0014.json"]["profile"] == {
        "operation": "docker_info", "returncode": None, "elapsed_seconds": 3.001197,
        "client_term_sent": True, "outcome": "unknown", "execution_proof": False}
    assert rows["STOP.json"]["profile"]["cleanup_claimed"] is True
    assert rows["STOP.json"]["profile"]["cleanup_confirmed"] is False
    assert result["cleanup_confirmed"] is False
    output = json.dumps(result)
    assert "private-token" not in output and "private-password" not in output and "/private/daemon" not in output
    assert all((root / name).read_bytes() == value for name, value in originals.items())


def test_daemon_and_info_text_profiles_reach_actual_safe_packet(tmp_path):
    root = tmp_path / "inner"
    root.mkdir()
    native.durable(root / "daemon.stderr", b"failed to start storage driver\n")
    native.durable(root / "command-0014.json", {"argv": [*native.DOCKER, "info", "--format", "{{json .}}"],
        "elapsed": 3.0, "returncode": None})
    native.durable(root / "command-0014.stderr", b"docker info incomplete\n")
    native.durable(root / "STOP.json", {"status": "unconfirmed", "diagnostics": sample_failure()})
    packet = native.evidence_packet(tmp_path)
    assert packet["complete"] is False
    assert packet["profiles"]["inner/daemon.stderr"]["text"] == "failed to start storage driver\n"
    assert packet["profiles"]["inner/command-0014.stderr"]["text"] == "docker info incomplete\n"
    assert packet["profiles"]["inner/command-0014.json"]["outcome"] == "unknown"
    assert packet["profiles"]["inner/STOP.json"]["diagnostics"] == sample_failure()


def test_text_export_is_bounded_tail_and_never_changes_original(tmp_path):
    path = tmp_path / "daemon.stderr"
    raw = b"old line\n" * 10000 + b"final observed plugin error\n"
    native.durable(path, raw)
    value = native.diagnostic_text_original(path)
    assert len(value["text"].encode()) <= native.DIAGNOSTIC_TEXT_BYTES
    assert value["truncated"] is True and value["source_window"] == "tail"
    assert value["text"].endswith("final observed plugin error\n")
    assert value["bytes"] == len(raw) and value["sha256"] == hashlib.sha256(raw).hexdigest()
    assert path.read_bytes() == raw and value["execution_proof"] is False


@pytest.mark.parametrize("line", [
    "--password SYNTHETIC_SECRET_XYZ", "PRIVATE_KEY=SYNTHETIC_SECRET_XYZ", "Bearer SYNTHETIC_SECRET_XYZ",
    "password=\"SYNTHETIC_SECRET_XYZ with spaces\"", "API_KEY SYNTHETIC_SECRET_XYZ",
    "Authorization: Basic SYNTHETIC_SECRET_XYZ", "--client-secret SYNTHETIC_SECRET_XYZ",
    "cookie SYNTHETIC_SECRET_XYZ", "TOKEN: SYNTHETIC_SECRET_XYZ",
])
def test_unstructured_sensitive_line_is_omitted_instead_of_partial_redaction(tmp_path, line):
    path = tmp_path / "daemon.stderr"
    raw = (line + "\nfailed to start storage driver\n").encode()
    native.durable(path, raw)
    value = native.diagnostic_text_original(path)
    assert value["text"] == "<sensitive-line-redacted>\nfailed to start storage driver\n"
    assert "SYNTHETIC_SECRET_XYZ" not in value["text"]
    assert path.read_bytes() == raw and value["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("newline", [False, True])
def test_tail_start_inside_a_credential_is_never_exported(tmp_path, newline):
    path = tmp_path / "daemon.stderr"
    raw = b"TOKEN=" + b"privatecredential" * 2048
    if newline:
        raw += b"\nobserved storage driver error\n"
    native.durable(path, raw)
    value = native.diagnostic_text_original(path)
    assert "privatecredential" not in value["text"]
    assert value["text"] == ("observed storage driver error\n" if newline else "")
    assert value["truncated"] is True and path.read_bytes() == raw


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "changing"])
def test_diagnostic_text_refuses_unsafe_or_mutating_original(tmp_path, monkeypatch, fault):
    target = tmp_path / "private-target"
    target.write_bytes(b"token=private-original")
    path = tmp_path / "daemon.stderr"
    if fault == "symlink":
        path.symlink_to(target)
    elif fault == "hardlink":
        native.os.link(target, path)
    else:
        native.durable(path, b"first")
        original = native.file_proof
        calls = []
        def changing(*args, **kwargs):
            calls.append(1)
            value = original(*args, **kwargs)
            return value if len(calls) == 1 else {**value, "sha256": "0" * 64}
        monkeypatch.setattr(native, "file_proof", changing)
    with pytest.raises((OSError, native.ProofError)):
        native.diagnostic_text_original(path)
    assert target.read_bytes() == b"token=private-original"


def test_unknown_command_stderr_is_never_exported_as_docker_info(tmp_path):
    native.durable(tmp_path / "command-0014.json", {"argv": [*native.DOCKER, "exec", "private"],
        "elapsed": 1, "returncode": 0})
    native.durable(tmp_path / "command-0014.stderr", b"unclassified private message")
    assert native.profile_for(tmp_path / "command-0014.json") is None
    assert native.profile_for(tmp_path / "command-0014.stderr") is None


@pytest.mark.parametrize("retainer,confirmed", [(0, True), (None, False), (1, False), (True, False)])
def test_cleanup_claim_requires_independent_normal_daemon_and_retainer(tmp_path, monkeypatch, retainer, confirmed):
    (tmp_path / "inner").mkdir()
    profile = {"all_containers_stopped": True, "daemon_returncode": 0,
        "retainer_returncode": retainer, "removed": False, "forced_kill": False}
    monkeypatch.setattr(native, "evidence_packet", lambda *_: {"complete": False,
        "profiles": {"inner/CLEANUP.json": profile}})
    result = {"cleanup_confirmed": False}
    native.failure_evidence(tmp_path, time.monotonic() + 2, result)
    assert result["cleanup_confirmed"] is confirmed
