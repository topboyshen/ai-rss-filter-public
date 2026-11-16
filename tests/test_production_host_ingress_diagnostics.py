"""Offline failure attribution only; no native host, Docker, or production proof."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import production_host_ingress_native as native

REAL_POPEN = subprocess.Popen


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
    first["last_command"].update(operation="docker_info", sequence=14, elapsed_seconds=3.001197)
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
        "client_term_sent": True, "outcome": "unknown", "execution_proof": False,
        "capture_status": "not_present_unknown"}
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


def fixed_info_receipt():
    capture = {"schema": 1, "execution_proof": False, "operation": "docker_info", "sequence": 14,
        "budget_seconds": 3.0, "elapsed_seconds": 3.001197, "failure_phase": "capture",
        "capture": {**native.capture_observation(), "started": True, "failure_stage": "deadline",
                    "finalization_stage": "close_stderr", "finalization_completed": True},
        "client": {"popen_returned": True, "returncode": None, "term_sent": True}}
    return {"argv": [*native.DOCKER, "info", "--format", "{{json .}}"], "returncode": None,
        "elapsed": 3.001197, "started_monotonic": 100.0, "deadline_monotonic": 103.0,
        "transfer": False, "client_term_sent": True, "capture_diagnostic": capture}


def test_fixed_info_capture_exports_bound_original_not_stream_contents(tmp_path):
    value = fixed_info_receipt()
    path = tmp_path / "command-0014.json"
    native.durable(path, value)
    raw = path.read_bytes()
    profile = native.info_command_profile(path)
    assert profile["capture_diagnostic"] == value["capture_diagnostic"]
    assert profile["capture_status"] == "available" and profile["outcome"] == "unknown"
    assert profile["capture_scope"] == "pipe_read_observations_not_durable_bytes"
    assert profile["execution_proof"] is False and "argv" not in profile
    assert path.read_bytes() == raw


@pytest.mark.parametrize("fault", ["sequence", "budget", "elapsed", "returncode", "term", "transfer",
    "nan", "infinity", "negative_budget", "over_budget", "boolean_rc", "term_string", "negative_clock",
    "raw_extra", "unknown_phase", "bytes_limit"])
def test_fixed_info_capture_conflicts_fail_without_replacing_original(tmp_path, fault):
    value = fixed_info_receipt()
    capture = value["capture_diagnostic"]
    if fault in {"sequence", "budget", "elapsed"}:
        capture[{"sequence": "sequence", "budget": "budget_seconds", "elapsed": "elapsed_seconds"}[fault]] += 1
    elif fault == "returncode":
        capture["client"]["returncode"] = 0
    elif fault == "term":
        capture["client"]["term_sent"] = False
    elif fault == "transfer":
        value["transfer"] = True
    elif fault in {"nan", "infinity"}:
        value["started_monotonic"] = float("nan" if fault == "nan" else "inf")
    elif fault in {"negative_budget", "over_budget"}:
        delta = -1 if fault == "negative_budget" else 4
        value["deadline_monotonic"] = 100 + delta
        capture["budget_seconds"] = delta
    elif fault == "boolean_rc":
        value["returncode"] = False
    elif fault == "term_string":
        value["client_term_sent"] = "false"
        capture["client"]["term_sent"] = False
    elif fault == "negative_clock":
        value.update(started_monotonic=-4.0, deadline_monotonic=-1.0)
    elif fault == "raw_extra":
        capture["stdout"] = "private-message"
    elif fault == "unknown_phase":
        capture["failure_phase"] = "private-message"
    else:
        capture["capture"]["stdout_bytes"] = native.OUTPUT_BYTES + 65537
    path = tmp_path / "command-0014.json"
    raw = json.dumps(value).encode()
    native.durable(path, raw)
    with pytest.raises(native.ProofError):
        native.info_command_profile(path)
    assert path.read_bytes() == raw


def resource_values():
    return {"memory.max": str(native.MEMORY), "memory.swap.max": "0", "pids.max": str(native.PIDS),
        "cpu.max": native.CPU, "memory.current": "100", "memory.peak": "200",
        "memory.events": "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1",
        "pids.current": "6", "cpu.stat": "usage_usec 12\nuser_usec 10\nsystem_usec 2",
        "cgroup.procs": ""}


def private_resource_fixture(tmp_path, monkeypatch):
    parent = tmp_path / "cgroup"
    parent.mkdir(mode=0o700)
    plan = {"owner": "a" * 32, "cgroup": "/ai-filter-host-" + "a" * 32,
            "deadline": time.monotonic() + 20}
    root = parent / plan["cgroup"].lstrip("/")
    root.mkdir(mode=0o700)
    for key, value in resource_values().items():
        (root / key).write_text(value + "\n")
    monkeypatch.setattr(native, "CGROUP", parent)
    return plan, root


def test_failure_resource_observation_preserves_oom_and_projects_only_counters(tmp_path, monkeypatch):
    plan, _root = private_resource_fixture(tmp_path, monkeypatch)
    raw = native.startup_resources(plan, plan["deadline"])
    assert raw == resource_values() and raw["memory.events"].endswith("oom_kill 1")
    raw["cgroup.procs"] = "987654321\n123456789"
    projected = native.startup_resource_profile(raw)
    assert projected["memory.events"]["oom"] == projected["memory.events"]["oom_kill"] == 1
    assert projected["cgroup.procs"] == {"count": 2, "nonempty": True}
    assert "987654321" not in json.dumps(projected) and "123456789" not in json.dumps(projected)


def observed_kernel_resource_values():
    # Exact fixed-kernel fields from successful run 42214141017; no live reads.
    return {**resource_values(), "memory.current": "325013504", "memory.peak": "466944000",
        "pids.current": "3", "cpu.stat": "usage_usec 2587124\nuser_usec 1430855\nsystem_usec 1156268\n"
        "nice_usec 0\ncore_sched.force_idle_usec 0\nnr_periods 87\nnr_throttled 11\n"
        "throttled_usec 498645\nnr_bursts 0\nburst_usec 0",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\nsock_throttled 0"}


def test_actual_kernel_counter_names_survive_fixed_read_and_projection(tmp_path, monkeypatch):
    plan, directory = private_resource_fixture(tmp_path, monkeypatch)
    values = observed_kernel_resource_values()
    for key, value in values.items():
        (directory / key).write_text(value + "\n")
    raw = native.startup_resources(plan, plan["deadline"])
    assert raw == values
    projected = native.startup_resource_profile(raw)
    assert projected["cpu.stat"] == {"usage_usec": 2587124, "user_usec": 1430855,
        "system_usec": 1156268, "nice_usec": 0, "core_sched.force_idle_usec": 0,
        "nr_periods": 87, "nr_throttled": 11, "throttled_usec": 498645,
        "nr_bursts": 0, "burst_usec": 0}
    assert projected["memory.events"]["sock_throttled"] == 0
    assert projected["cgroup.procs"] == {"count": 0, "nonempty": False}
    assert {key: (directory / key).read_text().strip() for key in values} == values


@pytest.mark.parametrize("field,line", [
    ("cpu.stat", "other.force_idle_usec 0"), ("cpu.stat", "core_sched.force_idle_usec.extra 0"),
    ("cpu.stat", "nice_usec 1"), ("cpu.stat", "core_sched.force_idle_usec 1"),
    ("memory.events", "sock_throttled 1"), ("memory.events", "socket_throttled 0"),
    ("cpu.stat", "nice_usec -1"), ("cpu.stat", "core_sched.force_idle_usec nan"),
    ("memory.events", "sock_throttled 1e2"),
])
def test_observed_counter_extensions_still_reject_unknown_duplicate_or_malformed(field, line):
    values = observed_kernel_resource_values()
    values[field] += "\n" + line
    with pytest.raises(native.ProofError):
        native.startup_resource_profile(values)


@pytest.mark.parametrize("field,key,bad", [
    ("cpu.stat", "nice_usec", "-1"), ("cpu.stat", "core_sched.force_idle_usec", "nan"),
    ("memory.events", "sock_throttled", "01"), ("memory.events", "sock_throttled", "1e2"),
])
def test_observed_counter_value_is_canonical_without_duplicate(field, key, bad):
    values = observed_kernel_resource_values()
    values[field] = values[field].replace(key + " 0", key + " " + bad)
    with pytest.raises(native.ProofError):
        native.startup_resource_profile(values)


def test_dot_byte_is_not_accepted_in_unrelated_resource_field(tmp_path, monkeypatch):
    plan, directory = private_resource_fixture(tmp_path, monkeypatch)
    (directory / "memory.current").write_text("1.0\n")
    with pytest.raises(native.ProofError, match="capacity/shape"):
        native.startup_resources(plan, plan["deadline"])


@pytest.mark.parametrize("fault", ["owner", "path", "directory_link", "field_link", "hardlink", "writable", "over_limit", "expired"])
def test_failure_resource_observation_never_follows_or_widens(tmp_path, monkeypatch, fault):
    plan, root = private_resource_fixture(tmp_path, monkeypatch)
    private = tmp_path / "private-original"
    private.write_text("secret_token_do_not_read")
    if fault == "owner":
        plan["owner"] = "../../private-original"
    elif fault == "path":
        plan["cgroup"] = "/different"
    elif fault == "directory_link":
        root.rename(tmp_path / "original-resource-dir")
        root.symlink_to(tmp_path / "original-resource-dir", target_is_directory=True)
    elif fault == "field_link":
        (root / "memory.max").unlink()
        (root / "memory.max").symlink_to(private)
    elif fault == "hardlink":
        native.os.link(root / "memory.max", tmp_path / "field-alias")
    elif fault == "writable":
        (root / "memory.max").chmod(0o666)
    elif fault == "over_limit":
        (root / "memory.max").write_text("1" * 1025)
    else:
        plan["deadline"] = time.monotonic() - 1
    with pytest.raises((OSError, native.ProofError)):
        native.startup_resources(plan, plan["deadline"])
    assert private.read_text() == "secret_token_do_not_read"


@pytest.mark.parametrize("key,text", [("memory.max", "secret_token"), ("memory.current", "max"),
    ("pids.max", "01"), ("cpu.max", "1 2 3"), ("memory.events", "oom 0\noom_kill 0\nprivate 2"),
    ("memory.events", "oom 0\noom 1\noom_kill 0"), ("cpu.stat", "secret 1"),
    ("cgroup.procs", "private"), ("cgroup.procs", "1\n1")])
def test_startup_resource_public_profile_rejects_unknown_or_private_text(key, text):
    values = resource_values()
    values[key] = text
    with pytest.raises(native.ProofError):
        native.startup_resource_profile(values)


def test_live_daemon_observation_shares_existing_reader_without_flush_or_wait(tmp_path, monkeypatch):
    observation = {**native.capture_observation(), "started": True, "stderr_bytes": 14000}
    logs = SimpleNamespace(observation=observation, thread=SimpleNamespace(is_alive=lambda: True), error=None)
    process = SimpleNamespace(poll=lambda: None)
    plan = {"owner": "a" * 32}
    monkeypatch.setattr(native, "LIVE_DAEMONS", [(process, logs, tmp_path, plan)])
    result = native.startup_stream_state(tmp_path, plan)
    assert result["capture"] == observation and result["capture"] is not observation
    assert result["thread_alive"] is True and result["daemon_returncode"] is None
    assert result["execution_proof"] is False and result["capture"]["finalization_completed"] is False
    assert native.startup_stream_state(tmp_path, dict(plan)) == {"status": "not_present_unknown"}


def test_daemon_reader_passes_same_observation_without_extra_reader_or_flush(tmp_path, monkeypatch):
    logs = object.__new__(native.DaemonLogs)
    logs.process, logs.error, logs.observation = object(), None, native.capture_observation()
    calls = []
    def capture(process, stdout, stderr, end, limit, *, close_pipes, observation):
        calls.append((process, stdout, stderr, end, limit, close_pipes))
        assert observation is logs.observation
        observation["started"] = True
    monkeypatch.setattr(native.Commands, "_capture", capture)
    logs._read(tmp_path)
    assert len(calls) == 1 and calls[0][-1] is False and logs.observation["started"] is True


@pytest.mark.parametrize("fault", [None, "resource", "write", "timer", "expired"])
def test_early_start_snapshot_never_changes_primary_or_resets_deadline(tmp_path, monkeypatch, fault):
    native.DIAGNOSTICS["stage"] = "daemon_start"
    clock, budgets, calls = [100.0], [], []
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    plan = {"deadline": 100.4}
    primary = native.ProofError("primary-info-unknown")
    @contextmanager
    def bound(seconds):
        budgets.append(seconds)
        yield
    monkeypatch.setattr(native.gateway, "_hard_deadline", bound)
    def resources(_plan, end):
        assert end == 100.4
        calls.append("resources")
        if fault == "timer":
            raise native.gateway.gate.AdmissionUncertain("gateway blocking operation deadline; outcome unknown")
        if fault == "resource":
            raise PermissionError("private resource unavailable")
        return resource_values()
    monkeypatch.setattr(native, "startup_resources", resources)
    monkeypatch.setattr(native, "startup_stream_state", lambda *_: calls.append("streams") or {"status": "not_present_unknown"})
    if fault == "write":
        monkeypatch.setattr(native, "durable", lambda *_: (_ for _ in ()).throw(OSError("write failed")))
    if fault == "expired":
        clock[0] = 101.0
    native.retain_startup_observation(tmp_path, plan, primary)
    assert str(primary) == "primary-info-unknown"
    if fault in {"timer", "expired", "write"}:
        assert hasattr(primary, "native_startup_observation_error")
        assert not (tmp_path / "START_OBSERVATION.json").exists()
    else:
        value = json.loads((tmp_path / "START_OBSERVATION.json").read_bytes())
        profile = native.startup_observation_profile(value)
        assert profile["execution_proof"] is False
        assert profile["status"] == ("evidence_insufficient" if fault == "resource" else "diagnostic_only")
    assert budgets == ([] if fault == "expired" else [pytest.approx(0.4)])
    if fault in {"timer", "expired"}:
        assert "streams" not in calls


def test_inner_start_failure_records_observation_but_never_enters_cleanup_or_next_stage(tmp_path, monkeypatch):
    plan = {"owner": "a" * 32, "cgroup": "/ai-filter-host-" + "a" * 32,
            "deadline": time.monotonic() + 20, "parent": {"netns": 1, "mntns": 1}}
    primary, calls = native.ProofError("command deadline; outcome unknown"), []
    monkeypatch.setattr(native, "Commands", lambda *_: SimpleNamespace())
    monkeypatch.setattr(native, "proc_identity", lambda _: {"netns": 2, "mntns": 2,
                        "cgroup": plan["cgroup"] + "/supervisor"})
    monkeypatch.setattr(native, "restrict_private_ipv6", lambda *_: None)
    def start(*_):
        calls.append("start")
        raise primary
    monkeypatch.setattr(native, "start_daemon", start)
    monkeypatch.setattr(native, "startup_resources", lambda *_: resource_values())
    monkeypatch.setattr(native, "stop_owned", lambda *_: pytest.fail("unknown daemon cannot be cleaned up"))
    with pytest.raises(native.ProofError) as caught:
        native.inner_proof(tmp_path, plan)
    assert caught.value is primary and calls == ["start"]
    first = json.loads((tmp_path / "FIRST_FAILURE.json").read_bytes())
    assert first["stage"] == "daemon_start" and first["error"]["literal"] == str(primary)
    value = json.loads((tmp_path / "START_OBSERVATION.json").read_bytes())
    assert native.startup_observation_profile(value)["resources"]["value"]["memory.events"]["oom_kill"] == 1
    assert not (tmp_path / "CLEANUP.json").exists() and not (tmp_path / "RESULT.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requires SIGALRM hard deadline")
@pytest.mark.parametrize("blocked", ["reader", "durable"])
def test_real_alarm_bounds_startup_projection_and_keeps_original_error(tmp_path, monkeypatch, blocked):
    monkeypatch.setattr(native.subprocess, "Popen", REAL_POPEN)
    code = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import production_host_ingress_native as h
root, blocked = Path(sys.argv[2]), sys.argv[3]
h.DIAGNOSTICS = {"stage": "daemon_start"}
calls = []
primary = h.ProofError("primary-info-unknown")
def resources(*args):
    calls.append("resources")
    if blocked == "reader":
        time.sleep(2)
    return {}
def streams(*args):
    calls.append("streams")
    return {}
def write(*args):
    calls.append("durable")
    time.sleep(2)
h.startup_resources, h.startup_stream_state, h.durable = resources, streams, write
start = time.monotonic()
try:
    with h.gateway._hard_deadline(0.1):
        h.retain_startup_observation(root, {"deadline": start + 1}, primary)
except h.gateway.gate.AdmissionUncertain:
    pass  # Original outer timer is never disabled/reset by the inner snapshot.
print(json.dumps({"calls": calls, "elapsed": time.monotonic() - start, "primary": str(primary),
                  "diagnostic_error": getattr(primary, "native_startup_observation_error", None)}))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
        str(Path(native.__file__).resolve().parents[1]), str(tmp_path), blocked],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=False)
    assert result.returncode == 0, result.stderr.decode()
    observed = json.loads(result.stdout)
    assert observed["calls"] == (["resources"] if blocked == "reader" else ["resources", "streams", "durable"])
    assert 0.02 <= observed["elapsed"] < 0.75
    assert observed["primary"] == "primary-info-unknown"
    assert observed["diagnostic_error"]["type"] == "AdmissionUncertain"
    assert list(tmp_path.iterdir()) == []
