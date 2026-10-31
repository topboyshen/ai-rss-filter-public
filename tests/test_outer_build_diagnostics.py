"""Outer candidate build failures expose which fixed command failed, its rc and a redacted stderr tail."""
from __future__ import annotations

import json

import pytest

from scripts import private_release_contract as private
from scripts import production_activation_host_native as native
from scripts import verify_production_startup_native as startup

TOKEN = "ab" * 32
SECRET_LINE = "Authorization: Bearer " + TOKEN + " password=hunter2leak"


def test_archive_command_failure_carries_command_stem_and_returncode(tmp_path):
    with pytest.raises(private.ContractError, match="synthetic command failed; original evidence retained") as caught:
        startup.archive_command(["sh", "-c", "echo boom >&2; exit 7"], tmp_path / "application-build", seconds=10)
    assert caught.value.returncode == 7 and caught.value.command == "application-build"
    assert (tmp_path / "application-build.stderr").read_text() == "boom\n"


def test_failed_build_receipt_records_failed_command_and_returncode(tmp_path, monkeypatch):
    request = {"source": {}, "image": "fixed", "contract_sha256": "a" * 64}
    plan = {"outer_peer": {}, "repo": str(tmp_path), "source_identity": {}, "active_deadline": 600}
    monkeypatch.setattr(native, "checked_build_request", lambda *a: (request, {"sha256": "b" * 64}, {}))
    monkeypatch.setattr(native, "peer_identity", lambda: {})
    monkeypatch.setattr(native.private, "source_identity", lambda *a: {})
    failure = private.ContractError("synthetic command failed; original evidence retained")
    failure.command, failure.returncode = "bounded-image-inspect", 1
    monkeypatch.setattr(native.startup, "build_candidate", lambda *a, **k: (_ for _ in ()).throw(failure))
    saved = {}
    monkeypatch.setattr(native, "record", lambda path, value, **k: saved.__setitem__(path.name, value))
    with pytest.raises(private.ContractError) as caught:
        native.build_requested_candidate(tmp_path, plan, None)
    assert caught.value is failure
    result = saved["BUILD_RESULT.json"]
    assert result["status"] == "failed" and result["failed_command"] == "bounded-image-inspect"
    assert result["returncode"] == 1


def _root(tmp_path):
    root = tmp_path / "host"
    build = root / "outer-build"
    build.mkdir(parents=True)
    noise = "".join(f"step {i}\n" for i in range(4000))
    (build / "application-build.stderr").write_text(noise + SECRET_LINE + "\nERROR: pip install failed\n")
    (build / "application-build.stdout").write_text("ok\n")
    (build / "bounded-image-inspect.stderr").write_text("")
    (root / "BUILD_RESULT.json").write_text(json.dumps({
        "status": "failed", "request_sha256": "c" * 64, "error_type": "ContractError", "error_sha256": "d" * 64,
        "failed_command": "application-build", "returncode": 1}))
    return root


def test_safe_inventory_exports_redacted_tail_and_build_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    packet = native.safe_inventory(_root(tmp_path), 600)
    rows = packet["outer_build_command_stderr"]
    assert [row["path"] for row in rows] == ["outer-build/application-build.stderr"]
    text = rows[0]["text"]
    assert rows[0]["truncated"] is True and text.startswith("[TRUNCATED: last")
    assert text.rstrip().endswith("ERROR: pip install failed")
    serialized = json.dumps(packet)
    assert TOKEN not in serialized and "hunter2leak" not in serialized
    assert packet["profiles"]["BUILD_RESULT.json"] == {"status": "failed", "error_type": "ContractError",
                                                       "failed_command": "application-build", "returncode": 1}


def test_build_profile_rejects_unexpected_values(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "BUILD_RESULT.json").write_text(json.dumps({"status": TOKEN, "error_type": "bad type " + TOKEN,
                                                        "failed_command": "/tmp/" + TOKEN, "returncode": "1"}))
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    packet = native.safe_inventory(root, 600)
    assert packet["profiles"]["BUILD_RESULT.json"] == {"status": None, "error_type": None,
                                                       "failed_command": None, "returncode": None}
    assert TOKEN not in json.dumps(packet)
