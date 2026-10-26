"""Failure reporting tests without invoking Docker or reading private inputs."""

import json
from pathlib import Path

import pytest

from scripts import verify_private_release_native as native


def test_native_failure_retains_previous_stages_without_private_error_text(tmp_path):
    path = tmp_path / "report.json"
    report = {"status": "started", "checks": ["earlier-proof"]}
    native.advance_stage(path, report, "native_architecture")
    assert json.loads(path.read_text())["events"][0]["state"] == "started"
    native.advance_stage(path, report, "pull_previous_fixed_image")
    secret = "private-path-and-environment-value-must-not-leak"
    try:
        native.private.require(False, secret)
    except native.private.ContractError as error:
        native.fail_stage(path, report, error)

    saved = json.loads(path.read_text())
    assert saved["checks"] == ["earlier-proof"]
    assert saved["status"] == "failed"
    assert [(event["stage"], event["state"]) for event in saved["events"]] == [
        ("native_architecture", "started"), ("native_architecture", "completed"),
        ("pull_previous_fixed_image", "started"), ("pull_previous_fixed_image", "failed"),
    ]
    assert saved["failure"]["kind"] == "contract_refusal"
    assert saved["failure"]["message_sha256"] == native.private.sha(secret.encode())
    assert saved["failure"]["locations"] == [{
        "file": "private_release_contract.py", "function": "require",
        "line": native.private.require.__code__.co_firstlineno + 2,
    }]
    assert secret not in path.read_text()
    assert str(tmp_path) not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_unexpected_failure_has_no_exception_message_or_arbitrary_frame_path():
    try:
        raise OSError("/private/credentials: SECRET=synthetic-sensitive-value")
    except OSError as error:
        details = native.failure_details(error)
    assert details["kind"] == "unexpected_error"
    assert details["locations"] == []
    assert "SECRET" not in json.dumps(details)


def test_fixed_image_pull_failure_is_separate_from_start_and_preserved(tmp_path, monkeypatch):
    root = tmp_path / "public"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python@sha256:" + "a" * 64 + "\n")
    monkeypatch.setattr(native.private, "source_identity", lambda root: {"source_sha": "b" * 40})
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "info"]:
            return b"amd64\n"
        if args[:2] == ["docker", "pull"]:
            raise native.private.ContractError("private release subprocess failed")
        return b"fixture-strategy\n"

    monkeypatch.setattr(native.private, "run", fake_run)
    monkeypatch.setattr(native, "cleanup", lambda *args: None)
    output = tmp_path / "evidence"
    with pytest.raises(native.private.ContractError):
        native.execute(root, output)
    saved = json.loads((output / "report.json").read_text())
    assert saved["stage"] == "pull_previous_fixed_image"
    assert saved["events"][-1]["state"] == "failed"
    assert any(event["stage"] == "create_synthetic_resources" and event["state"] == "completed"
               for event in saved["events"])
    assert not any(args[:2] == ["docker", "compose"] for args in calls)
    assert saved["failure"]["locations"][0]["file"] == Path(native.__file__).name



def test_reader_identity_proof_keeps_capabilities_dropped_and_file_private(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_bytes(b"synthetic: true\n")
    config.chmod(0o400)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return b"" if len(calls) == 1 else native.private.sha(config.read_bytes()).encode() + b"\n"

    monkeypatch.setattr(native.private, "run", run)
    native.verify_config_reader_identity("python@sha256:" + "a" * 64, config, "1001:1001")
    assert [args[args.index("--user") + 1] for args in calls] == ["0:0", "1001:1001"]
    assert all(args[args.index("--cap-drop") + 1] == "ALL" and "--cap-add" not in args for args in calls)
    assert "except PermissionError" in calls[0][-1]
    assert config.stat().st_mode & 0o777 == 0o400
