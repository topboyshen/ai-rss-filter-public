"""Fixed PostgreSQL readiness uses durable real receipts; no Docker is run."""
from copy import deepcopy
import hashlib
import json
import os
import stat
import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts import production_host_ingress_native as host
from scripts import verify_production_activation_native as native
from scripts import verify_miniflux_pause_native as common
from services import admission_fence as gate


@pytest.fixture(autouse=True)
def private_evidence_mode():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    harness = object.__new__(native.ActivationHarness)
    harness.output, harness.sequence, harness.started = tmp_path, 0, time.monotonic()
    harness.phase_deadline, harness.env = None, {}
    harness.password, harness.token, harness.auth = "unused-password", "unused-token", "unused-auth"
    harness.owner, harness.prefix, harness.network = "a" * 32, "synthetic-owned", "synthetic-network"
    harness.names, harness.images, harness.created = {}, {}, []
    harness.private_host_context = SimpleNamespace(
        check=lambda **kwargs: None,
        plan={"deadline": time.monotonic() + 60, "active_deadline": time.monotonic() + 40},
        commands=SimpleNamespace(uncertain_mutation=False))
    row = {
        "Id": "b" * 64, "Name": "/synthetic-owned-pg", "Image": "sha256:" + "c" * 64,
        "Created": "2026-10-30T00:00:00Z", "RestartCount": 0,
        "Config": {"Labels": {common.LABEL: harness.owner}},
        "State": {"Running": True, "Dead": False, "Restarting": False, "OOMKilled": False,
                  "Pid": 12345, "StartedAt": "2026-10-30T00:00:01Z"},
        "HostConfig": {"NetworkMode": harness.network, "PortBindings": {},
                       "Memory": 384 * 1024**2, "MemorySwap": 384 * 1024**2,
                       "PidsLimit": 64, "NanoCpus": 1_000_000_000,
                       "Privileged": False, "SecurityOpt": ["no-new-privileges"]}}
    value = SimpleNamespace(harness=harness, row=row, calls=[], results=[], inspect_rows=[],
                            outer_rc=0, marker_change=None)

    def run(argv, **kwargs):
        args = argv[len(common.DOCKER):]
        value.calls.append((list(args), kwargs))
        if args[0] == "run":
            return subprocess.CompletedProcess(argv, 0, row["Id"] + "\n", "")
        if args[0] == "inspect":
            observed = value.inspect_rows.pop(0) if value.inspect_rows else row
            return subprocess.CompletedProcess(argv, 0, json.dumps([observed]), "")
        assert args[0] == "exec", "synthetic transport refuses unexpected operations"
        result = value.results.pop(0) if value.results else 0
        if result == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial-not-ready")
        if isinstance(result, BaseException):
            raise result
        if args[2:4] == ["/bin/sh", "-c"]:
            marker = {"schema": 1, "probe": "pg_isready", "nonce": args[-1], "returncode": result}
            stdout = json.dumps(marker, separators=(",", ":")) + "\n"
            if value.marker_change:
                stdout = value.marker_change(marker)
            return subprocess.CompletedProcess(argv, value.outer_rc, stdout, "")
        return subprocess.CompletedProcess(argv, result, "synthetic PostgreSQL status is nonempty\n", "")

    monkeypatch.setattr(common.subprocess, "run", run)
    # Exercise the actual launch binder and its actual original/receipt writes.
    returned = harness.launch("pg", row["Image"], memory=384 * 1024**2, pids=64)
    assert returned == row and harness.pg_launch_identity["Id"] == row["Id"]
    value.calls.clear()
    return value


def probe_records(rig):
    return [json.loads(path.read_bytes()) for path in sorted(rig.harness.output.glob("PG_READINESS_*.json"))]


@pytest.mark.parametrize("rc,expected", [(0, True), (1, False), (2, False)])
def test_only_fixed_probe_classifies_actual_rc_with_nonempty_output(rig, rc, expected):
    rig.results = [rc]
    assert rig.harness.postgres_ready() is expected
    assert not rig.harness.private_host_context.commands.uncertain_mutation
    commands = [args for args, _ in rig.calls]
    assert commands == [["inspect", rig.row["Id"]],
                        ["exec", rig.row["Id"], "/bin/sh", "-c", native.PG_READINESS_PROBE,
                         "pg-readiness", commands[1][-1]],
                        ["inspect", rig.row["Id"]]]
    record, = probe_records(rig)
    assert record["status"] == ("ready" if expected else "not_ready") and record["returncode"] == rc
    original = rig.harness.output / f"command-{record['sequence']:04d}.json"
    receipt = original.with_suffix(".result.json")
    actual = common.read_command_result(original, record["sequence"], json.loads(receipt.read_bytes()))
    assert actual["returncode"] == 0 and json.loads(original.read_bytes())["stdout"]
    assert json.loads(json.loads(original.read_bytes())["stdout"])["returncode"] == rc
    assert len(commands[1][-1]) == 64 and record["transport_returncode"] == 0
    assert record["nonce_sha256"] == hashlib.sha256(commands[1][-1].encode()).hexdigest()
    assert record["command_original_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (original, receipt))
    assert receipt.stat().st_size <= gate.MAX_RECORD_BYTES
    assert rig.harness.pg_readiness_counts == {"ready": int(expected), "not_ready": int(not expected)}


def test_not_ready_then_ready_is_not_sticky_and_keeps_both_receipts(rig):
    rig.results = [2, 0]
    assert rig.harness.postgres_ready() is False
    assert rig.harness.postgres_ready() is True
    assert [row["returncode"] for row in probe_records(rig)] == [2, 0]
    assert rig.harness.pg_readiness_counts == {"ready": 1, "not_ready": 1}
    assert not rig.harness.private_host_context.commands.uncertain_mutation
    assert len({args[-1] for args, _ in rig.calls if args[0] == "exec"}) == 2


@pytest.mark.parametrize("rc", [0, 1, 2])
def test_valid_readiness_never_clears_prior_uncertainty(rig, rc):
    rig.harness.private_host_context.commands.uncertain_mutation = True
    rig.results = [rc]
    assert rig.harness.postgres_ready() is (rc == 0)
    assert rig.harness.private_host_context.commands.uncertain_mutation


@pytest.mark.parametrize("rc", [3, 124, 7, -9, None, True, "timeout"])
def test_unconfirmed_results_remain_sticky_not_not_ready(rig, rc):
    rig.results = [rc]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)
    assert len([args for args, _ in rig.calls if args[0] == "exec"]) == 1
    original = json.loads((rig.harness.output / "command-0004.json").read_bytes())
    assert original["rc"] == (None if rc == "timeout" else 0)


@pytest.mark.parametrize("outer_rc", [1, 2, 3, 124, None, True])
@pytest.mark.parametrize("marker_present", [True, False])
def test_exec_transport_failure_cannot_masquerade_as_inner_readiness(rig, outer_rc, marker_present):
    rig.outer_rc = outer_rc
    rig.results = [1]
    if not marker_present:
        rig.marker_change = lambda marker: ""
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)


@pytest.mark.parametrize("fault", ["nonce", "schema", "probe", "extra", "missing", "not_json", "too_large", "list",
                                    "duplicate", "whitespace", "trailing"])
def test_completion_marker_is_fresh_exact_and_bounded(rig, fault):
    def change(marker):
        if fault == "nonce":
            marker["nonce"] = "0" * 64
        elif fault == "schema":
            marker["schema"] = True
        elif fault == "probe":
            marker["probe"] = "pg_dump"
        elif fault == "extra":
            marker["unchecked"] = True
        elif fault == "missing":
            del marker["returncode"]
        elif fault == "not_json":
            return "accepting connections"
        elif fault == "too_large":
            return json.dumps(marker) + " " * 512
        elif fault == "list":
            return json.dumps([marker])
        elif fault == "duplicate":
            return json.dumps(marker, separators=(",", ":")).replace('"schema":1,', '"schema":1,"schema":1,') + "\n"
        elif fault == "whitespace":
            return json.dumps(marker) + "\n"
        elif fault == "trailing":
            return json.dumps(marker, separators=(",", ":")) + "\ntrailing"

        return json.dumps(marker)
    rig.marker_change = change
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)


def set_nested(row, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        row = row[part]
    row[parts[-1]] = value


@pytest.mark.parametrize("path,value", [
    ("Id", "d" * 64), ("Id", "b" * 12), ("Name", "/another-pg"),
    ("Image", "sha256:" + "d" * 64), ("Created", "2026-10-30T00:00:02Z"),
    ("Config.Labels." + common.LABEL, "another-owner"),
    ("HostConfig.NetworkMode", "host"), ("HostConfig.Memory", 768 * 1024**2),
    ("HostConfig.MemorySwap", -1), ("HostConfig.PidsLimit", 256),
    ("HostConfig.PortBindings", {"5432/tcp": [{"HostPort": "5432"}]}),
    ("HostConfig.Privileged", True), ("State.Running", False), ("State.Pid", 0),
    ("State.Pid", True), ("State.StartedAt", ""), ("State.Dead", True),
    ("State.Restarting", True), ("State.OOMKilled", True), ("RestartCount", True)])
def test_wrong_target_or_resource_identity_is_refused_before_probe(rig, path, value):
    changed = deepcopy(rig.row)
    if path.startswith("Config.Labels."):
        changed["Config"]["Labels"][common.LABEL] = value
    else:
        set_nested(changed, path, value)
    rig.inspect_rows = [changed]
    with pytest.raises((gate.AdmissionError, ValueError)):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not any(args[0] == "exec" for args, _ in rig.calls)
    assert not probe_records(rig)


@pytest.mark.parametrize("path,value", [
    ("Id", "e" * 64), ("Created", "2026-10-30T00:00:02Z"),
    ("RestartCount", 1), ("State.Pid", 12346), ("State.StartedAt", "2026-10-30T00:00:03Z")])
def test_identity_drift_after_probe_is_sticky(rig, path, value):
    changed = deepcopy(rig.row)
    set_nested(changed, path, value)
    rig.inspect_rows = [deepcopy(rig.row), changed]
    with pytest.raises(gate.AdmissionError):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)
    assert len([args for args, _ in rig.calls if args[0] == "exec"]) == 1


@pytest.mark.parametrize("command", [
    ["exec", "synthetic-owned-pg", "pg_isready", "-U", "synthetic", "-d", "synthetic_pause"],
    ["exec", "b" * 64, "pg_dump"], ["exec", "synthetic-redis", "redis-cli", "ping"]])
def test_generic_command_nonzero_retains_original_failure_behavior(rig, command):
    rig.results = [2]
    with pytest.raises(RuntimeError, match="failed rc=2"):
        rig.harness.command(command)
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)


@pytest.mark.parametrize("fault", ["receipt", "raw", "receipt_missing", "same_length"])
def test_probe_requires_unchanged_real_raw_and_receipt(rig, monkeypatch, fault):
    original_read = native.read_command_result
    def damage(path, sequence, expected):
        if sequence == 4:
            receipt = path.with_suffix(".result.json")
            if fault == "receipt":
                data = json.loads(receipt.read_bytes())
                data["returncode"] = 0 if data["returncode"] else 1
                receipt.write_text(json.dumps(data))
            elif fault == "receipt_missing":
                receipt.rename(receipt.with_suffix(".retained"))
            elif fault == "same_length":
                before = path.stat()
                raw = path.read_bytes()
                altered = raw.replace(b"synthetic_pause", b"synthetic_false")
                assert len(altered) == len(raw) and altered != raw
                path.write_bytes(altered)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            else:
                path.write_bytes(path.read_bytes() + b" ")
        return original_read(path, sequence, expected)
    monkeypatch.setattr(native, "read_command_result", damage)
    with pytest.raises((gate.AdmissionError, host.ProofError, OSError)):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)


@pytest.mark.parametrize("point", ["raw_sync", "receipt_sync", "readiness_sync"])
def test_no_readiness_confirmation_when_durability_fails(rig, monkeypatch, point):
    original_save, original_durable = common.save, host.durable
    def save(path, value, **kwargs):
        if point == "raw_sync" and path.name == "command-0004.json":
            original_sync = os.fsync
            with monkeypatch.context() as patch:
                def fail_sync(fd):
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("synthetic original sync refused")
                    return original_sync(fd)
                patch.setattr(os, "fsync", fail_sync)
                return original_save(path, value, **kwargs)
        return original_save(path, value, **kwargs)
    def durable(path, value):
        target = (point == "receipt_sync" and path.name == "command-0004.result.json") or (
            point == "readiness_sync" and path.name.startswith("PG_READINESS_"))
        if target:
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic sync refused")))
                return original_durable(path, value)
        return original_durable(path, value)
    monkeypatch.setattr(common, "save", save)
    monkeypatch.setattr(host, "durable", durable)
    with pytest.raises(OSError, match="sync refused"):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not hasattr(rig.harness, "pg_readiness_counts")


@pytest.mark.parametrize("phase", ["before", "after"])
def test_context_refusal_stays_sticky(rig, phase):
    calls = []
    def check(**kwargs):
        calls.append(kwargs)
        if phase == "before" or len(calls) == 6:
            raise gate.AdmissionError("synthetic context drift")
    rig.harness.private_host_context.check = check
    with pytest.raises(gate.AdmissionError, match="context drift"):
        rig.harness.postgres_ready()
    assert rig.harness.private_host_context.commands.uncertain_mutation
    assert not probe_records(rig)


def test_shared_phase_budget_and_no_internal_retry(rig):
    rig.harness.phase_deadline = time.monotonic() + 0.75
    rig.results = [2]
    assert rig.harness.postgres_ready() is False
    assert len(rig.calls) == 3
    assert all(0 < kwargs["timeout"] <= 0.75 for _, kwargs in rig.calls)


def test_without_private_context_keeps_existing_default_probe(rig):
    rig.harness.private_host_context = None
    rig.results = [2]
    with pytest.raises(RuntimeError, match="failed rc=2"):
        rig.harness.postgres_ready()
    assert [args for args, _ in rig.calls] == [
        ["exec", "synthetic-owned-pg", "pg_isready", "-U", "synthetic", "-d", "synthetic_pause"]]
    assert not probe_records(rig)


@pytest.mark.parametrize("inner_rc", [0, 1, 2, 3, 127])
def test_fixed_shell_probe_reports_actual_inner_result_without_output_leak(tmp_path, inner_rc):
    argv_path = tmp_path / "argv.json"
    # The real shell exercises the unchanged wrapper. A fixed shell function
    # supplies the fake probe without another temporary executable/child shell.
    fake_probe = ('pg_isready() {\n'
                  'printf "%s\\n" "$@" > "$CAPTURE_ARGV"\n'
                  'printf "private synthetic probe stdout\\n"\n'
                  'printf "private synthetic probe stderr\\n" >&2\n'
                  'return "$SYNTHETIC_PG_RC"\n}\n')
    nonce = "7" * 64
    result = subprocess.run(["/bin/sh", "-c", fake_probe + native.PG_READINESS_PROBE, "pg-readiness", nonce],
                            env={"PATH": str(tmp_path), "CAPTURE_ARGV": str(argv_path),
                                 "SYNTHETIC_PG_RC": str(inner_rc)},
                            capture_output=True, text=True, check=False, timeout=3)
    assert result.returncode == 0 and result.stderr == ""
    expected = {"schema": 1, "probe": "pg_isready", "nonce": nonce, "returncode": inner_rc}
    assert result.stdout == json.dumps(expected, separators=(",", ":")) + "\n"
    assert argv_path.read_text().splitlines() == ["-U", "synthetic", "-d", "synthetic_pause"]
    assert "private synthetic" not in result.stdout and len(result.stdout.encode()) < 512


def test_missing_pg_binary_marker_is_unconfirmed_127(tmp_path):
    nonce = "8" * 64
    result = subprocess.run(["/bin/sh", "-c", native.PG_READINESS_PROBE, "pg-readiness", nonce],
                            env={"PATH": str(tmp_path)}, capture_output=True, text=True,
                            check=False, timeout=3)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["returncode"] == 127
