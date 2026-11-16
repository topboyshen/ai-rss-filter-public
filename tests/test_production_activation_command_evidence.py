"""Large diagnostic streams keep small, exact execution receipts; no Docker."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
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
def harness(tmp_path):
    value = object.__new__(native.ActivationHarness)
    value.output, value.sequence, value.started = tmp_path, 0, time.monotonic()
    value.phase_deadline, value.env = None, {}
    value.password, value.token, value.auth = "unused-password", "unused-token", "unused-auth"
    value.private_host_context = SimpleNamespace(
        check=lambda **kwargs: None,
        plan={"deadline": time.monotonic() + 60, "active_deadline": time.monotonic() + 40},
        commands=SimpleNamespace(uncertain_mutation=False))
    return value


def response(monkeypatch, *, stdout="synthetic", stderr="", returncode=0):
    seen = []
    def run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    monkeypatch.setattr(common.subprocess, "run", run)
    return seen


@pytest.mark.parametrize("size", [1, 16384, 59591])
def test_command_keeps_large_stdout_but_admission_receipt_stays_small(harness, monkeypatch, size):
    output = "x" * size
    calls = response(monkeypatch, stdout=output)
    assert harness.command(["exec", "synthetic-pg", "pg_dump"], final=True) == output
    original = harness.output / "command-0001.json"
    receipt = original.with_suffix(".result.json")
    retained = json.loads(original.read_bytes())
    assert retained["rc"] == 0 and retained["stdout"] == output
    assert stat.S_IMODE(original.stat().st_mode) == 0o600
    assert receipt.stat().st_size < gate.MAX_RECORD_BYTES == 16384
    value = common.read_command_result(original, 1, json.loads(receipt.read_bytes()))
    assert value["returncode"] == 0
    assert value["original"]["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert value["original"]["size"] == original.stat().st_size
    assert len(calls) == 1
    assert not harness.private_host_context.commands.uncertain_mutation
    if original.stat().st_size > 16384:
        with pytest.raises(gate.AdmissionError, match="unsafe private record"):
            gate._read(original)


@pytest.mark.parametrize("allowed_failure", [False, True])
def test_nonzero_does_not_become_success_and_raw_rc_is_retained(harness, monkeypatch, allowed_failure):
    calls = response(monkeypatch, stdout="retained-output", stderr="retained-stderr", returncode=7)
    if allowed_failure:
        assert harness.command(["inspect", "synthetic"], allowed_failure=True) == "retained-output"
    else:
        with pytest.raises(RuntimeError, match="failed rc=7"):
            harness.command(["inspect", "synthetic"])
    path = harness.output / "command-0001.json"
    assert common.read_command_result(path, 1, json.loads(path.with_suffix(".result.json").read_bytes()))["returncode"] == 7
    assert json.loads(path.read_bytes())["stderr"] == "retained-stderr"
    assert harness.private_host_context.commands.uncertain_mutation is (not allowed_failure)
    assert len(calls) == 1


@pytest.mark.parametrize("allowed_failure", [False, True])
def test_timeout_keeps_partial_evidence_and_sticky_unknown(harness, monkeypatch, allowed_failure):
    calls = []
    def timeout(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"partial", stderr=b"timeout-stderr")
    monkeypatch.setattr(common.subprocess, "run", timeout)
    if allowed_failure:
        assert harness.command(["exec", "synthetic"], allowed_failure=True) == "partial"
    else:
        with pytest.raises(RuntimeError, match="failed rc=None"):
            harness.command(["exec", "synthetic"])
    path = harness.output / "command-0001.json"
    assert common.read_command_result(path, 1, json.loads(path.with_suffix(".result.json").read_bytes()))["returncode"] is None
    assert json.loads(path.read_bytes())["stderr"] == "timeout-stderr"
    assert harness.private_host_context.commands.uncertain_mutation and len(calls) == 1


@pytest.mark.parametrize("stage", ["before", "after"])
def test_context_identity_refusal_still_blocks(harness, monkeypatch, stage):
    calls = response(monkeypatch)
    checks = []
    def check(**kwargs):
        checks.append(kwargs)
        if stage == "before" or len(checks) > 1:
            raise gate.AdmissionError("synthetic identity changed")
    harness.private_host_context.check = check
    with pytest.raises(gate.AdmissionError, match="synthetic identity changed"):
        harness.command(["inspect", "synthetic"])
    assert len(calls) == (0 if stage == "before" else 1)
    if stage == "after":
        assert harness.private_host_context.commands.uncertain_mutation


def original_and_receipt(tmp_path):
    path = tmp_path / "command-0001.json"
    proof = common.save(path, {"rc": 0, "stdout": "synthetic output", "stderr": ""}, retain_proof=True)
    expected = common.retain_command_result(path, 1, 0, proof)
    return path, expected


@pytest.mark.parametrize("change", ["symlink", "hardlink", "permissions", "replace", "same_length"])
def test_retained_original_changes_never_authorize_command_result(tmp_path, change):
    path, expected = original_and_receipt(tmp_path)
    before = path.read_bytes()
    receipt = path.with_suffix(".result.json").read_bytes()
    if change == "symlink":
        held = tmp_path / "held-original"
        path.rename(held)
        path.symlink_to(held)
    elif change == "hardlink":
        os.link(path, tmp_path / "other-link")
    elif change == "permissions":
        path.chmod(0o644)
    elif change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(before)
        replacement.replace(path)
    else:
        old_stat = path.stat()
        changed = before.replace(b"synthetic output", b"synthetic change")
        assert len(changed) == len(before) and changed != before
        path.write_bytes(changed)
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises((gate.AdmissionError, host.ProofError, OSError)):
        common.read_command_result(path, 1, expected)
    assert path.with_suffix(".result.json").read_bytes() == receipt


@pytest.mark.parametrize("fault", ["sequence", "schema_bool", "returncode_bool", "original_hash", "extra_field"])
def test_small_receipt_identity_and_schema_are_not_lenient(tmp_path, fault):
    path, expected = original_and_receipt(tmp_path)
    receipt = path.with_suffix(".result.json")
    value = json.loads(receipt.read_bytes())
    if fault == "sequence":
        value["sequence"] = 2
    elif fault == "schema_bool":
        value["schema"] = True
    elif fault == "returncode_bool":
        value["returncode"] = False
    elif fault == "original_hash":
        value["original"]["sha256"] = "0" * 64
    else:
        value["extra"] = "unreviewed"
    receipt.write_text(json.dumps(value))
    with pytest.raises(gate.AdmissionError):
        common.read_command_result(path, 1, expected)


def test_original_change_during_same_fd_hash_is_rejected(tmp_path, monkeypatch):
    path, expected = original_and_receipt(tmp_path)
    original_read, original_bytes = os.read, path.read_bytes()
    changed = []
    def read(fd, count):
        chunk = original_read(fd, count)
        if chunk and not changed:
            changed.append(True)
            path.write_bytes(original_bytes.replace(b"synthetic output", b"synthetic change"))
        return chunk
    monkeypatch.setattr(host.os, "read", read)
    with pytest.raises(host.ProofError, match="original identity changed"):
        common.read_command_result(path, 1, expected)
    assert changed == [True]


@pytest.mark.parametrize("before,after", [(None, 0), (0, 7), (7, 0)])
def test_valid_returncode_rewrite_cannot_replace_actual_execution(tmp_path, before, after):
    path = tmp_path / "command-0001.json"
    proof = common.save(path, {"rc": before, "stdout": "", "stderr": ""}, retain_proof=True)
    expected = common.retain_command_result(path, 1, before, proof)
    receipt = path.with_suffix(".result.json")
    altered = json.loads(receipt.read_bytes())
    altered["returncode"] = after
    receipt.write_text(json.dumps(altered))
    with pytest.raises(gate.AdmissionError, match="actual execution"):
        common.read_command_result(path, 1, expected)


def test_both_raw_and_receipt_rewrite_cannot_replace_execution_binding(tmp_path):
    path, expected = original_and_receipt(tmp_path)
    raw = json.loads(path.read_bytes())
    raw["rc"] = 7
    path.write_text(json.dumps(raw))
    receipt = path.with_suffix(".result.json")
    forged = json.loads(receipt.read_bytes())
    forged.update(returncode=7, original=common.command_original_proof(path))
    receipt.write_text(json.dumps(forged))
    with pytest.raises(gate.AdmissionError, match="actual execution"):
        common.read_command_result(path, 1, expected)


def test_nested_bool_identity_cannot_equal_integer_metadata(tmp_path):
    path, expected = original_and_receipt(tmp_path)
    receipt = path.with_suffix(".result.json")
    forged = json.loads(receipt.read_bytes())
    # A synthetic size 1 vs True is deliberately equality-equivalent in Python.
    expected["original"]["size"] = 1
    forged["original"]["size"] = True
    receipt.write_text(json.dumps(forged))
    with pytest.raises(gate.AdmissionError, match="actual execution"):
        common.read_command_result(path, 1, expected)


@pytest.mark.parametrize("pending", [False, True])
def test_existing_command_receipt_is_retained_not_overwritten(tmp_path, pending):
    path = tmp_path / "command-0001.json"
    proof = common.save(path, {"rc": 0}, retain_proof=True)
    receipt = path.with_suffix(".result.json")
    held = receipt.with_suffix(".json.pending") if pending else receipt
    held.write_bytes(b"retained earlier evidence")
    before = held.read_bytes()
    with pytest.raises(gate.AdmissionError, match="already exists"):
        common.retain_command_result(path, 1, 0, proof)
    assert held.read_bytes() == before
    assert common.command_original_proof(path) == proof


def test_raw_replaced_after_capture_before_receipt_is_not_adopted(tmp_path):
    path = tmp_path / "command-0001.json"
    proof = common.save(path, {"rc": 0}, retain_proof=True)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    with pytest.raises(gate.AdmissionError, match="changed after capture"):
        common.retain_command_result(path, 1, 0, proof)
    assert not path.with_suffix(".result.json").exists()


@pytest.mark.parametrize("mapping", [True, [], {"stale": 1}])
def test_parent_never_accepts_reused_or_invalid_result_mapping(harness, monkeypatch, mapping):
    calls = response(monkeypatch)
    with pytest.raises(ValueError, match="fresh output mapping"):
        common.Harness.command(harness, ["inspect", "synthetic"], command_result=mapping)
    assert not calls and harness.sequence == 0


def test_original_eight_mib_bound_and_admission_sixteen_kib_bound_remain(tmp_path):
    path = tmp_path / "large-original.json"
    assert common.COMMAND_EVIDENCE_BYTES == 8 * 1024**2
    with path.open("wb") as stream:
        stream.truncate(common.COMMAND_EVIDENCE_BYTES)
    assert common.command_original_proof(path)["size"] == common.COMMAND_EVIDENCE_BYTES
    with path.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(host.ProofError, match="bounded regular original"):
        common.command_original_proof(path)
    assert path.stat().st_size == common.COMMAND_EVIDENCE_BYTES + 1
    assert gate.MAX_RECORD_BYTES == 16384


@pytest.mark.parametrize("failure", ["raw_write", "raw_fsync", "receipt_sync"])
def test_evidence_failure_stays_failure_and_preserves_partial(harness, monkeypatch, failure):
    response(monkeypatch, stdout="x" * 59591)
    if failure == "raw_write":
        original_save = common.save
        def fail_write(path, value, **kwargs):
            original_save(path, value, **kwargs)
            raise OSError("synthetic raw write failed")
        monkeypatch.setattr(common, "save", fail_write)
    elif failure == "raw_fsync":
        monkeypatch.setattr(common.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic raw fsync failed")))
    else:
        original_sync = os.fsync
        count = []
        def fail_receipt_directory(fd):
            count.append(fd)
            if len(count) == 4:
                raise OSError("synthetic receipt sync failed")
            return original_sync(fd)
        monkeypatch.setattr(host.os, "fsync", fail_receipt_directory)
    with pytest.raises(OSError, match="synthetic"):
        harness.command(["exec", "synthetic-pg", "pg_dump"], final=True)
    path = harness.output / "command-0001.json"
    assert path.exists() and json.loads(path.read_bytes())["stdout"] == "x" * 59591
    assert harness.private_host_context.commands.uncertain_mutation


def test_existing_control_record_limit_is_never_used_for_dump(harness, monkeypatch):
    calls = response(monkeypatch, stdout="x" * 59591)
    actual = gate._read
    seen = []
    def read(path):
        seen.append(path.name)
        assert path.name.endswith(".result.json")
        return actual(path)
    monkeypatch.setattr(gate, "_read", read)
    assert harness.command(["exec", "synthetic-pg", "pg_dump"], final=True) == "x" * 59591
    assert seen == ["command-0001.result.json"] and len(calls) == 1


def test_large_successful_dump_does_not_fabricate_final_evidence_failure(tmp_path, harness, monkeypatch):
    output = tmp_path / "execution"
    output.mkdir(mode=0o700)
    harness.output, harness.names = output, {"pg": "synthetic-pg"}
    harness.stage = lambda name: setattr(harness, "current_stage", name)
    harness.compose_parser_evidence = harness.prepare = harness.redis = lambda: None
    harness.preserve = lambda: []
    monkeypatch.setattr(native, "ActivationHarness", lambda *args, **kwargs: harness)
    monkeypatch.setattr(native, "source_binding", lambda *args: {"revision": "a" * 40})
    monkeypatch.setattr(native, "scenario", lambda h, path: h.stage("actual_fixed_network_namespace_transport"))
    monkeypatch.setattr(native.native_deadline, "final_phase", nullcontext)
    calls = response(monkeypatch, stdout="x" * 59591)
    result = native.execute(Path("/synthetic-repo"), tmp_path, "sha256:" + "b" * 64, tmp_path / "binary", output)
    assert result["status"] == "passed" and result["failure"] is None and result["evidence_errors"] == []
    assert len(calls) == 1


def test_timeout_exports_bounded_actual_command_facts(harness, monkeypatch):
    from scripts import production_activation_host_native as host_native
    calls = []
    def timeout(args, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"secret-body", stderr=b"secret-error")
    monkeypatch.setattr(common.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="failed rc=None"):
        harness.command(["run", "--env", "TOKEN=secret-token", "private-image"])
    path = harness.output / "HARNESS_COMMAND_FAILURE.json"
    value = json.loads(path.read_bytes())
    assert value == host_native.safe_harness_failure(value)
    assert value["operation"] == "docker_run" and value["sequence"] == 1
    assert value["outcome"] == "timeout" and value["returncode"] is None
    assert value["timeout_seconds"] == calls[0]
    assert 0 <= value["elapsed_seconds"] < 2
    assert 0 < value["harness_remaining_seconds"] <= 600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(path.read_bytes()) < 1024 and b"secret" not in path.read_bytes()
    first = path.read_bytes()
    with pytest.raises(RuntimeError, match="failed rc=None"):
        harness.command(["inspect", "another-private-name"])
    assert path.read_bytes() == first
    assert harness.private_host_context.commands.uncertain_mutation


def test_diagnostic_write_failure_does_not_mask_original_or_clear_uncertainty(harness, monkeypatch):
    response(monkeypatch, returncode=7)
    def unavailable(*args):
        raise OSError("private diagnostic path")
    monkeypatch.setattr(harness, "_preserve_command_failure", unavailable)
    with pytest.raises(RuntimeError, match="failed rc=7") as caught:
        harness.command(["inspect", "private-container"])
    assert caught.value.__notes__ == ["command failure diagnostic unavailable: OSError"]
    assert harness.private_host_context.commands.uncertain_mutation
    assert not (harness.output / "HARNESS_COMMAND_FAILURE.json").exists()


def test_existing_command_failure_original_is_not_overwritten(harness, monkeypatch):
    path = harness.output / "HARNESS_COMMAND_FAILURE.json"
    path.write_bytes(b"preserve original")
    response(monkeypatch, returncode=9)
    with pytest.raises(RuntimeError, match="failed rc=9") as caught:
        harness.command(["inspect", "private-container"])
    assert path.read_bytes() == b"preserve original"
    assert caught.value.__notes__ == ["command failure diagnostic unavailable: FileExistsError"]


def test_success_does_not_create_command_failure(harness, monkeypatch):
    response(monkeypatch)
    harness.command(["inspect", "private-container"])
    assert not (harness.output / "HARNESS_COMMAND_FAILURE.json").exists()
