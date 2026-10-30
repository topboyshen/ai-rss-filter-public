"""Command diagnosis survives first-failure export without completing a failed run."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from scripts import private_release_diagnostics as diagnostics
from scripts import production_activation_host_native as host_native
from scripts import verify_production_activation_native as native

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_mode():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def empty_summary():
    with diagnostics.scope() as observed:
        return observed.summary()


def test_failure_contains_safe_commands_before_final_preservation(tmp_path, capsys):
    evidence = empty_summary()
    error = native.private.ContractError("private release subprocess timed out")
    native.record_failure(error, REPO, tmp_path, "actual_consumer_prepare_while_closed",
                          private_commands=evidence)
    original = json.loads((tmp_path / "FAILURE.json").read_bytes())
    printed = json.loads(capsys.readouterr().out.removeprefix("activation_native_failure="))
    assert original["private_commands"] == evidence == printed["private_commands"]
    assert original["message_literal"] == "private release subprocess timed out"
    projected = host_native.safe_activation({"status": "failed_or_evidence_insufficient",
                                            "failure": original, "private_commands": evidence})
    assert projected["private_commands"] == evidence
    assert projected["status"] == "failed_or_evidence_insufficient"


@pytest.mark.parametrize("bad", [None, [], "token-secret", {"argv": "token-secret"},
                                 {"schema": 1, "stdout": "token-secret"}])
def test_unknown_diagnostic_shape_never_reaches_public_profile(tmp_path, capsys, bad):
    native.record_failure(ValueError("secret-raw-error"), REPO, tmp_path, "actual_consumer_prepare_while_closed",
                          private_commands=bad)
    assert "private_commands" not in json.loads((tmp_path / "FAILURE.json").read_bytes())
    assert "token-secret" not in capsys.readouterr().out
    projected = host_native.safe_activation({"private_commands": bad})
    assert projected["private_commands"] is None
    assert "token-secret" not in json.dumps(projected)


def test_first_failure_export_does_not_substitute_missing_completion(tmp_path, monkeypatch):
    root = tmp_path / "inner" / "activation"
    root.mkdir(parents=True)
    evidence = empty_summary()
    native.record_failure(ValueError("not exported raw"), REPO, root,
                          "actual_consumer_prepare_while_closed", private_commands=evidence)
    monkeypatch.setattr(host_native, "remaining", lambda *a, **k: 10)
    packet = host_native.safe_inventory(tmp_path, 600)
    assert packet["complete"] is False
    assert packet["full_raw_archive_verified"] is False
    failure = packet["profiles"]["inner/activation/FAILURE.json"]
    assert failure["private_commands"] == evidence
    assert failure["status"] == "failed_or_evidence_insufficient"
    assert "not exported raw" not in json.dumps(packet)
    assert "inner/activation/RESULT.json" not in packet["profiles"]


def fake_execution(monkeypatch, *, fail_cleanup=False):
    class Harness:
        def __init__(self, *args, **kwargs):
            self.names, self.started, self.current_stage = {}, time.monotonic(), "initial"
        def stage(self, name):
            self.current_stage = name
        def compose_parser_evidence(self):
            pass
        def prepare(self):
            pass
        def redis(self):
            pass
        def preserve(self):
            if fail_cleanup:
                raise TimeoutError("cleanup deadline")
            return []
    def scenario(harness, output):
        harness.stage("actual_consumer_prepare_while_closed")
        raise native.private.ContractError("private release subprocess timed out")
    monkeypatch.setattr(native, "source_binding", lambda *a: {"revision": "a" * 40})
    monkeypatch.setattr(native, "ActivationHarness", Harness)
    monkeypatch.setattr(native, "scenario", scenario)


def test_default_execution_does_not_enable_command_capture(tmp_path, monkeypatch):
    fake_execution(monkeypatch)
    monkeypatch.setattr(diagnostics, "scope", lambda: pytest.fail("default capture must stay disabled"))
    result = native.execute(REPO, REPO, "image", REPO, tmp_path)
    assert result["status"] == "failed_or_evidence_insufficient"
    assert result["private_commands"] is None


def test_isolated_execution_captures_first_failure_even_if_cleanup_raises(tmp_path, monkeypatch):
    fake_execution(monkeypatch, fail_cleanup=True)
    evidence = empty_summary()
    seen = []
    @contextmanager
    def observed():
        seen.append("enter")
        try:
            yield SimpleNamespace(summary=lambda: evidence)
        finally:
            seen.append("exit")
    monkeypatch.setattr(diagnostics, "scope", observed)
    with pytest.raises(TimeoutError, match="cleanup deadline"):
        native.execute(REPO, REPO, "image", REPO, tmp_path, private_host_context=object())
    assert seen == ["enter", "exit"]
    original = json.loads((tmp_path / "FAILURE.json").read_bytes())
    assert original["message_literal"] == "private release subprocess timed out"
    assert original["private_commands"] == evidence
    assert not (tmp_path / "RESULT.json").exists()


def harness_failure():
    return {"schema": 1, "sequence": 175, "operation": "docker_run", "timeout_seconds": 15,
            "harness_remaining_seconds": 21, "elapsed_seconds": 15.01,
            "returncode": None, "outcome": "timeout"}


@pytest.mark.parametrize("key,bad", [
    ("schema", True), ("sequence", False), ("sequence", 0), ("sequence", 1000001),
    ("operation", "secret-image"), ("operation", []), ("timeout_seconds", True),
    ("timeout_seconds", -1), ("timeout_seconds", 601), ("elapsed_seconds", float("nan")),
    ("harness_remaining_seconds", float("inf")), ("returncode", False),
    ("returncode", 1), ("outcome", "returned"), ("outcome", "secret"),
    ("argv", "secret-token")])
def test_harness_failure_profile_rejects_unbounded_or_unknown_fields(key, bad):
    value = harness_failure()
    value[key] = bad
    assert host_native.safe_harness_failure(value) is None


def test_harness_failure_export_does_not_claim_complete_inventory(tmp_path, monkeypatch):
    root = tmp_path / "inner" / "activation"
    root.mkdir(parents=True)
    value = harness_failure()
    (root / "HARNESS_COMMAND_FAILURE.json").write_text(json.dumps(value))
    monkeypatch.setattr(host_native, "remaining", lambda *a, **k: 10)
    packet = host_native.safe_inventory(tmp_path, 600)
    assert packet["profiles"]["inner/activation/HARNESS_COMMAND_FAILURE.json"] == value
    assert packet["complete"] is False and packet["full_raw_archive_verified"] is False


@pytest.mark.parametrize("value", [{"ready": 1, "not_ready": 2}, {"ready": 0, "not_ready": 0}])
@pytest.mark.parametrize("key", ["pg_readiness", "sender_readiness"])
def test_only_fixed_readiness_counts_are_exported(value, key):
    assert host_native.safe_activation({key: value})[key] == value


@pytest.mark.parametrize("value", [{"ready": True, "not_ready": 0}, {"ready": -1, "not_ready": 0},
    {"ready": 1, "not_ready": 10001}, {"ready": 1, "not_ready": 0, "raw": "secret"}, None])
@pytest.mark.parametrize("key", ["pg_readiness", "sender_readiness"])
def test_unsafe_readiness_counts_are_not_exported(value, key):
    assert host_native.safe_activation({key: value})[key] is None


def test_drain_observation_alone_never_completes_native_proof(tmp_path, monkeypatch):
    root = tmp_path / "inner"
    root.mkdir()
    value = {"schema": 1, "running_count": 0, "uncertain_mutation": False}
    (root / "DRAIN_OBSERVATION.json").write_text(json.dumps(value))
    monkeypatch.setattr(host_native, "remaining", lambda *a, **k: 10)
    packet = host_native.safe_inventory(tmp_path, 600)
    assert packet["profiles"]["inner/DRAIN_OBSERVATION.json"] == value
    assert packet["complete"] is False and packet["full_raw_archive_verified"] is False
    assert "inner/DAEMON_STOP.json" not in packet["profiles"]
