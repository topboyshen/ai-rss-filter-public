"""Failure diagnostics expose class names and fixed source literals only, never runtime data."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import private_release_contract as private
from scripts import production_activation_host_native as host_native
from scripts import production_namespace_native as namespace_native
from scripts import verify_production_activation_native as native
from services import admission_fence as gate

REPO = Path(__file__).resolve().parents[1]
LITERAL = "actual fixed network namespace transport proof missing"


def _summary(capsys):
    return json.loads(capsys.readouterr().out.removeprefix("activation_native_failure="))


def test_record_failure_includes_fixed_source_literal(tmp_path, capsys):
    try:
        gate._require(False, LITERAL)
    except gate.AdmissionError as error:
        failure = native.record_failure(error, REPO, tmp_path, "actual_fixed_network_namespace_transport")
    summary = _summary(capsys)
    assert summary["type"] == "AdmissionError" and summary["message_literal"] == LITERAL
    assert failure["message_literal"] == LITERAL
    assert json.loads((tmp_path / "FAILURE.json").read_bytes())["message_literal"] == LITERAL


@pytest.mark.parametrize("message", ["token " + "ab" * 32, "synthetic-secret-token-and-body-never-log",
                                     "postgres://user:pw@db/x"])
def test_record_failure_omits_runtime_or_secret_messages(tmp_path, capsys, message):
    try:
        raise gate.AdmissionError(message)
    except gate.AdmissionError as error:
        native.record_failure(error, REPO, tmp_path, "actual_fixed_network_namespace_transport")
    output = capsys.readouterr().out
    assert message not in output and "message_literal" not in output
    assert message not in (tmp_path / "FAILURE.json").read_text()


def _harness(tmp_path):
    return SimpleNamespace(repo=REPO, owner="0" * 32, manifest="1" * 64, network="n", token="2" * 64,
                           inspect_role=lambda role: {"Id": "3" * 64, "Image": "sha256:" + "4" * 64})


def test_namespace_helper_failure_reports_plain_error_type(tmp_path, capsys, monkeypatch):
    secret = "5" * 64

    def failed(args, path, **kwargs):
        Path(str(path) + ".stdout").write_text(json.dumps({
            "status": "unconfirmed", "error_type": "TimeoutError", "error_sha256": "6" * 64,
            "error_message_literal": "synthetic token required", "leak": secret}))
        raise private.ContractError("synthetic command failed; original evidence retained")

    monkeypatch.setattr(native.startup_native, "archive_command", failed)
    p = {"directory": tmp_path}
    with pytest.raises(private.ContractError) as caught:
        native.actual_namespace_transport(_harness(tmp_path), p, {"sender": {"operation_id": "7" * 64}})
    native.record_failure(caught.value, REPO, tmp_path, "actual_fixed_network_namespace_transport")
    summary = _summary(capsys)
    assert summary["namespace_helper"] == ("status=unconfirmed,error_type=TimeoutError,"
                                           "error_message=synthetic token required")
    assert summary["message_literal"] == "synthetic command failed; original evidence retained"
    assert secret not in json.dumps(summary) and secret not in (tmp_path / "FAILURE.json").read_text()


def test_namespace_helper_unreadable_stdout_never_masks_original_error(tmp_path, monkeypatch):
    def failed(args, path, **kwargs):
        raise private.ContractError("synthetic command deadline exceeded")

    monkeypatch.setattr(native.startup_native, "archive_command", failed)
    with pytest.raises(private.ContractError, match="deadline exceeded") as caught:
        native.actual_namespace_transport(_harness(tmp_path), {"directory": tmp_path}, {"sender": {"operation_id": "7" * 64}})
    assert any(note.startswith("namespace helper: stdout_unreadable=") for note in caught.value.__notes__)


def test_safe_activation_publishes_class_name_and_literal_only():
    value = {"status": "failed_or_evidence_insufficient", "last_stage": "actual_fixed_network_namespace_transport",
             "failure": {"type": "AdmissionError", "message_sha256": "8" * 64, "message_literal": LITERAL,
                         "namespace_helper": "status=unconfirmed,error_type=TimeoutError"}}
    result = host_native.safe_activation(value)
    assert result["failure_type"] == "AdmissionError" and result["failure_message_literal"] == LITERAL
    assert result["failure_namespace_helper"] == "status=unconfirmed,error_type=TimeoutError"
    bad = host_native.safe_activation({"failure": {"type": "bad type!", "message_literal": "x" + "a" * 40,
                                                   "namespace_helper": "\nnewline"}})
    assert bad["failure_type"] is None and bad["failure_message_literal"] is None
    assert bad["failure_namespace_helper"] is None


def test_namespace_helper_report_literal_only(monkeypatch):
    monkeypatch.setattr(namespace_native, "safe_export", lambda options, token: {"complete": True})

    def raises(error):
        def run(options, token):
            raise error
        return run

    monkeypatch.setattr(namespace_native, "execute_owned", raises(gate.AdmissionError("synthetic token required")))
    assert namespace_native.run_report({}, "9" * 64)["error_message_literal"] == "synthetic token required"
    monkeypatch.setattr(namespace_native, "execute_owned", raises(RuntimeError("runtime " + "9" * 64)))
    result = namespace_native.run_report({}, "9" * 64)
    assert result["error_type"] == "RuntimeError" and result["error_message_literal"] is None
