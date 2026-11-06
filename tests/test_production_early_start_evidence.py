"""Synthetic local originals only; no Docker, process probing or production."""
import hashlib
import json
import os

import pytest

from scripts import production_early_start_evidence as early


def write(path, value, mode=0o600):
    raw = value if type(value) is bytes else json.dumps(value).encode()
    if path.exists():
        path.chmod(0o600)  # Construct adversarial fixtures, never production originals.
    path.write_bytes(raw)
    path.chmod(mode)
    return raw


@pytest.fixture
def originals(tmp_path, monkeypatch):
    # pytest's real temporary tree is private; readers still check every path.
    root = tmp_path / "proof"
    root.mkdir(mode=0o700)
    inner = root / "inner"
    inner.mkdir(mode=0o700)
    monkeypatch.setattr(early.time, "monotonic", lambda: 120.0)
    capture = {"schema": 1, "execution_proof": False, "operation": "docker_info", "sequence": 11,
        "budget_seconds": 3.0, "elapsed_seconds": 3.001, "failure_phase": "capture",
        "capture": {"started": True, "stdout_bytes": 0, "stderr_bytes": 0, "stdout_eof": False,
                    "stderr_eof": False, "loop_completed": False, "failure_stage": "deadline",
                    "finalization_stage": None, "finalization_completed": True},
        "client": {"popen_returned": True, "returncode": None, "term_sent": True}}
    observation = {"operation": "docker_info", "sequence": 11, "budget_seconds": 3.0,
                   "elapsed_seconds": 3.001, "returncode": None, "error": "ProofError", "execution_proof": False}
    result = {"status": "failed_or_evidence_insufficient", "bootstrap_result": None,
              "daemon_stop": None, "command_failure": observation, "command_capture": capture,
              "error_type": "ProofError", "error_sha256": "a" * 64}
    receipt = {"argv": [*early.host.DOCKER, "info", "--format", "{{json .}}"],
               "started_monotonic": 110.0, "deadline_monotonic": 113.0, "elapsed": 3.001,
               "returncode": None, "transfer": False, "capture_diagnostic": capture, "error": "SECRET_ERROR_CANARY"}
    write(root / "PLAN.json", {"bootstrap_mode": "private-host-loopback131-v1", "started": 100.0,
                              "deadline": 280.0}, 0o400)
    write(inner / "INNER_RESULT.json", result, 0o400)
    write(inner / "command-0011.json", receipt)
    write(inner / "command-0011.stdout", b"SECRET_INFO_CANARY")
    write(inner / "command-0011.stderr", b"")
    write(inner / "daemon.stderr", b'level=info msg="Daemon has completed initialization" SECRET_DAEMON_CANARY\n')
    return root, inner, result, receipt


def run(originals):
    root, _, result, _ = originals
    return early.export(root, result, 280.0)


def test_early_failure_before_bootstrap_exports_only_fixed_bound_facts(originals):
    root, inner, result, _ = originals
    before = {p: p.read_bytes() for p in [root / "PLAN.json", *inner.iterdir()]}
    value = run(originals)
    assert not (inner / "bootstrap/RESULT.json").exists()
    assert value["status"] == "diagnostic_only"
    assert value["execution_proof"] is value["complete"] is value["full_raw_archive_verified"] is False
    assert value["command"]["capture"] == result["command_capture"]
    assert value["command"]["capture_scope"] == "pipe_read_observations_not_durable_bytes"
    assert value["command"]["stdout"]["original"]["sha256"] == hashlib.sha256(b"SECRET_INFO_CANARY").hexdigest()
    assert value["files"]["daemon.stderr"]["markers"]["counts"]["initialization_completed"] == 1
    assert value["files"]["DAEMON_STOP.json"]["status"] == "not_present_unknown"
    assert value["files"]["RESOURCES_FINAL.json"]["status"] == "not_present_unknown"
    assert "SECRET_" not in json.dumps(value) and "argv" not in json.dumps(value)
    assert {p: p.read_bytes() for p in before} == before
    assert early.MAX_INPUT + early.reader.EXPORT_INPUT_BYTES == 8 * 1024**2
    assert len(early.gate._encoded(value)) < early.MAX_PACKET


@pytest.mark.parametrize("target", ["PLAN.json", "INNER_RESULT.json", "command-0011.json", "daemon.stderr"])
def test_symlink_original_is_never_followed(originals, tmp_path, target):
    root, inner, _, _ = originals
    path = (root if target == "PLAN.json" else inner) / target
    path.unlink()
    outside = tmp_path / "secret"
    write(outside, b"SECRET_CANARY")
    path.symlink_to(outside)
    value = run(originals)
    assert value["status"] == "evidence_insufficient"
    assert "SECRET_CANARY" not in json.dumps(value)


@pytest.mark.parametrize("change", ["hardlink", "mode", "owner", "oversize"])
def test_original_identity_and_size_guards(originals, monkeypatch, change):
    _, inner, _, _ = originals
    path = inner / "command-0011.json"
    if change == "hardlink":
        os.link(path, inner / "other")
    elif change == "mode":
        path.chmod(0o644)
    elif change == "owner":
        actual = early.os.geteuid()
        monkeypatch.setattr(early.os, "geteuid", lambda: actual + 1)
    else:
        write(path, b"x" * (early.MAX_JSON + 1))
    value = run(originals)
    assert value["status"] == "evidence_insufficient"


@pytest.mark.parametrize("change", ["argv", "rc", "elapsed", "budget", "transfer", "missing_capture", "capture_budget",
                                   "capture_elapsed", "capture_rc", "capture_sequence"])
def test_receipt_and_sealed_observation_must_agree(originals, change):
    _, inner, result, receipt = originals
    if change == "argv":
        receipt["argv"] = [*early.host.DOCKER, "ps"]
    elif change == "rc":
        receipt["returncode"] = 0
    elif change == "elapsed":
        receipt["elapsed"] = 1.0
    elif change == "budget":
        receipt["deadline_monotonic"] = 114.0
    elif change == "transfer":
        receipt["transfer"] = True
    elif change == "missing_capture":
        receipt.pop("capture_diagnostic")
    else:
        key = {"capture_budget": "budget_seconds", "capture_elapsed": "elapsed_seconds",
               "capture_sequence": "sequence"}.get(change)
        if key:
            result["command_capture"][key] += 1
        else:
            result["command_capture"]["client"]["returncode"] = 0
        write(inner / "INNER_RESULT.json", result, 0o400)
    write(inner / "command-0011.json", receipt)
    value = run(originals)
    assert value["command"]["status"] == value["status"] == "evidence_insufficient"


@pytest.mark.parametrize("target", ["PLAN.json", "INNER_RESULT.json", "command-0011.json"])
def test_malformed_or_duplicate_json_is_not_a_proof(originals, target):
    root, inner, _, _ = originals
    write((root if target == "PLAN.json" else inner) / target, b'{"x":1,"x":2}',
          0o400 if target != "command-0011.json" else 0o600)
    assert run(originals)["status"] == "evidence_insufficient"


def test_sealed_result_must_be_identical_to_observed_result(originals):
    _, _, result, _ = originals
    result["new"] = "only_in_memory"
    assert run(originals)["error"]["reason_code"] == "sealed_result_differs_from_observation"


def test_no_read_after_original_deadline(originals, monkeypatch):
    monkeypatch.setattr(early.time, "monotonic", lambda: 280.0)
    monkeypatch.setattr(early.os, "open", lambda *a, **kw: pytest.fail("expired export opened a file"))
    value = run(originals)
    assert value["error"]["reason_code"] == "original_deadline_exhausted"
    assert value["original_bytes_read"] == 0


def test_bootstrap_started_diagnostics_are_not_repurposed_as_early_proof(originals, monkeypatch):
    _, _, result, _ = originals
    result["bootstrap_result"] = {"status": "failed"}
    monkeypatch.setattr(early.os, "open", lambda *a, **kw: pytest.fail("late result must use original bootstrap exporter"))
    assert run(originals)["status"] == "not_applicable_bootstrap_started"


def test_changed_log_snapshot_is_insufficient_and_not_a_readiness_verdict(originals, monkeypatch):
    _, inner, _, _ = originals
    actual_read = early.os.read
    changed = False
    def mutate(fd, size):
        nonlocal changed
        raw = actual_read(fd, size)
        if b"SECRET_DAEMON_CANARY" in raw and not changed:
            changed = True
            with (inner / "daemon.stderr").open("ab") as stream:
                stream.write(b"later\n")
        return raw
    monkeypatch.setattr(early.os, "read", mutate)
    value = run(originals)
    row = value["files"]["daemon.stderr"]
    assert row["status"] == "evidence_insufficient" and "markers" not in row
    assert row["reason_code"] == "original_changed_during_read"


def test_total_input_is_bounded_without_omitting_a_failure(originals):
    _, inner, _, _ = originals
    write(inner / "daemon.stdout", b"x" * early.MAX_FILE)
    write(inner / "daemon.stderr", b"x" * early.MAX_FILE)
    value = run(originals)
    assert value["status"] == "evidence_insufficient"
    assert value["original_bytes_read"] <= early.MAX_INPUT
    assert value["files"]["daemon.stderr"]["reason_code"] == "original_input_budget_exhausted"


@pytest.mark.parametrize("raw, expected, partial", [
    (b"", 0, False), (b"API listen on secret", 0, True), (b"API listen on secret\n", 1, False),
    (b"x" * 9000 + b" API listen on secret\n", 0, True),
    (b"x" * 9000 + b"\nAPI listen on secret\n", 1, True),
])
def test_only_complete_tail_lines_count_and_absence_remains_unknown(raw, expected, partial):
    value = early._markers(raw)
    assert value["counts"]["api_listen"] == expected
    assert value["window_partial"] is partial
    assert value["absence_is_unknown"] and value["writer_flush_and_eof_unconfirmed"]
    assert "secret" not in json.dumps(value)


def test_resources_are_fixed_counters_and_keep_the_limited_scope(originals):
    _, inner, _, _ = originals
    write(inner / "RESOURCES_FINAL.json", {**early.isolated.SUPERVISOR, "memory.peak": "1234",
          "memory.events": {"oom": "0", "oom_kill": "0"}, "scope": early.isolated.RESOURCE_SCOPE})
    value = run(originals)["files"]["RESOURCES_FINAL.json"]
    assert value["profile"]["memory.peak"] == 1234
    assert value["profile"]["scope"] == "supervisor_and_private_daemon_only_not_all_application_containers"


def test_unrecognized_error_text_is_hash_only():
    value = early.failure(early.EvidenceError("SECRET_FUTURE_ERROR"))
    assert value["reason_code"] == "read_or_projection_failure"
    assert "SECRET" not in json.dumps(value)


def test_parent_symlink_is_not_followed(originals, tmp_path):
    root, _, result, _ = originals
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert early.export(alias, result, 280.0)["status"] == "evidence_insufficient"


def test_pipe_bytes_do_not_claim_durable_file_length(originals):
    _, inner, result, receipt = originals
    result["command_capture"]["capture"]["stdout_bytes"] = 4096
    write(inner / "INNER_RESULT.json", result, 0o400)
    write(inner / "command-0011.json", receipt)
    value = run(originals)
    assert value["command"]["status"] == "available"
    assert value["command"]["capture"]["capture"]["stdout_bytes"] != value["command"]["stdout"]["original"]["metadata"]["size"]


@pytest.mark.parametrize("suffix", ["stdout", "stderr"])
def test_command_stream_gap_marks_overall_diagnostic_insufficient(originals, tmp_path, suffix):
    _, inner, _, _ = originals
    outside = tmp_path / "outside"
    write(outside, b"SECRET_STREAM")
    path = inner / ("command-0011." + suffix)
    path.unlink()
    path.symlink_to(outside)
    value = run(originals)
    assert value["command"]["status"] == "available"  # Only receipt identity is available.
    assert value["command"][suffix]["status"] == value["status"] == "evidence_insufficient"
    assert "SECRET_STREAM" not in json.dumps(value)


def test_replaced_open_original_is_rejected(originals, monkeypatch):
    _, inner, _, _ = originals
    actual_read = early.os.read
    replaced = False
    def replace(fd, size):
        nonlocal replaced
        raw = actual_read(fd, size)
        if raw == b"SECRET_INFO_CANARY" and not replaced:
            replaced = True
            replacement = inner / "new-stdout"
            write(replacement, raw)
            replacement.replace(inner / "command-0011.stdout")
        return raw
    monkeypatch.setattr(early.os, "read", replace)
    value = run(originals)
    assert value["command"]["stdout"]["reason_code"] == "original_changed_during_read"
    assert value["status"] == "evidence_insufficient"
