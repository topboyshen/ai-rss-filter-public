"""Offline export refusal remains failure; no Docker or root access is used."""

import hashlib
import json
import os
import stat

import pytest

from scripts import production_sender_bootstrap_native as native
from scripts.verify_miniflux_pause_native import save
from services import admission_fence as gate
from test_production_sender_bootstrap_native import _fallback_arguments, failed_command


def snapshot(root):
    return {p.name: (p.read_bytes(), native.export._metadata(p.stat())) for p in root.iterdir()}


def make_failed_original(root):
    save(root / "command-0001.json", {"argv": ["docker", "--host", "unix:///var/run/docker.sock", "run",
         "PRIVATE_ARGV_SECRET"], "rc": 125, "stdin_sha256": hashlib.sha256(b"").hexdigest(),
         "elapsed_seconds": 0.01, "utc": "2026-10-31T00:00:00Z", "stdout": "PRIVATE_STDOUT", "stderr": "PRIVATE_STDERR"})
    failure = {"type": "RuntimeError", "command_sequence": 1}
    native._save(root / "FAILURE.json", failure)
    native._save(root / "RESULT.json", {"status": "unconfirmed", "failure": failure, "errors": []})
    return {"status": "unconfirmed"}


def test_ambient_022_reproduces_strict_command_refusal_without_repair(tmp_path):
    tmp_path.chmod(0o700)
    previous = os.umask(0o022)
    try:
        make_failed_original(tmp_path)
    finally:
        os.umask(previous)
    before = snapshot(tmp_path)
    assert stat.S_IMODE((tmp_path / "command-0001.json").stat().st_mode) == 0o644
    with pytest.raises(gate.AdmissionError, match="unsafe or oversized original") as caught:
        native.safe_export(tmp_path)
    assert snapshot(tmp_path) == before
    diagnostic = caught.value._bootstrap_command_metadata
    assert diagnostic["failed_observed_conditions"] == ["private_mode_0600"]
    assert diagnostic["size"] == (tmp_path / "command-0001.json").stat().st_size
    partial = caught.value._bootstrap_partial_export
    assert partial["complete"] is False and partial["execution_proof"] is False
    assert set(partial["files"]) == {"RESULT.json", "FAILURE.json"}
    for name, record in partial["files"].items():
        assert set(record) == {"sha256", "metadata"}
        assert record["sha256"] == hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()


def test_main_sets_private_umask_before_any_original_and_restores_ambient(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    arguments = _fallback_arguments(tmp_path)
    arguments[arguments.index("--output") + 1] = str(tmp_path)
    monkeypatch.setattr(native.sys, "argv", ["native", *arguments])
    monkeypatch.setattr(native, "execute", lambda *args, **options: make_failed_original(args[4]))
    previous = os.umask(0o022)
    try:
        assert native.main() == 3  # The original failed operation is never success.
        restored = os.umask(0o022)
        assert restored == 0o022
    finally:
        os.umask(previous)
    packet = json.loads(capsys.readouterr().out)
    assert packet["status"] == "unconfirmed" and packet["safe_evidence"]["complete"] is True
    assert packet["safe_evidence"]["command_failures"][0]["execution_proof"] is False
    assert stat.S_IMODE((tmp_path / "command-0001.json").stat().st_mode) == 0o600
    assert "PRIVATE_" not in json.dumps(packet)


def test_argparse_exit_also_restores_callers_umask(monkeypatch):
    monkeypatch.setattr(native.sys, "argv", ["native"])
    previous = os.umask(0o022)
    try:
        with pytest.raises(SystemExit):
            native.main()
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(previous)


@pytest.mark.parametrize("change,condition", [("mode", "private_mode_0600"), ("hardlink", "single_link"),
                                            ("symlink", "regular_file"), ("oversize", "original_file_byte_limit")])
def test_refused_command_metadata_is_nofollow_and_never_reads_content(tmp_path, monkeypatch, change, condition):
    failed_command(monkeypatch, tmp_path)
    path = tmp_path / "command-0001.json"
    if change == "mode":
        path.chmod(0o644)
    elif change == "hardlink":
        os.link(path, tmp_path / "alias")
    elif change == "symlink":
        path.rename(tmp_path / "original")
        path.symlink_to(tmp_path / "original")
    else:
        with path.open("wb") as stream:
            stream.truncate(native.export.EXPORT_FILE_BYTES + 1)
    read_calls = []
    monkeypatch.setattr(native.export.os, "read", lambda *args: read_calls.append(args) or pytest.fail("read refused original"))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((gate.AdmissionError, OSError)) as caught:
            native._command_summary(fd, 1, [0])
    finally:
        os.close(fd)
    diagnostic = caught.value._bootstrap_command_metadata
    assert diagnostic["file_name"] == "command-0001.json"
    assert condition in diagnostic["failed_observed_conditions"]
    assert diagnostic["content_read_for_diagnostic"] is False and read_calls == []
    assert diagnostic["original_failure_condition_confirmed"] is False
    assert "PRIVATE_" not in json.dumps(diagnostic) and str(tmp_path) not in json.dumps(diagnostic)


def test_main_export_failure_retains_small_original_proofs_without_changing_refusal(tmp_path, monkeypatch, capsys):
    failed_command(monkeypatch, tmp_path)
    path = tmp_path / "command-0001.json"
    path.chmod(0o644)
    before = snapshot(tmp_path)
    arguments = _fallback_arguments(tmp_path)
    arguments[arguments.index("--output") + 1] = str(tmp_path)
    monkeypatch.setattr(native.sys, "argv", ["native", *arguments])
    monkeypatch.setattr(native, "execute", lambda *args, **options: {"status": "passed"})
    assert native.main() == 3
    packet = json.loads(capsys.readouterr().out)
    assert packet["status"] == "unconfirmed" and packet["error_type"] == "AdmissionError"
    assert packet["error_sha256"] == hashlib.sha256(b"unsafe or oversized original").hexdigest()
    assert packet["safe_export_complete"] is False and packet["safe_evidence"]["complete"] is False
    assert set(packet["safe_evidence"]["files"]["RESULT.json"]) == {"sha256", "metadata"}
    assert packet["command_export_diagnostic"]["mode"] == 0o644
    assert snapshot(tmp_path) == before and "PRIVATE_" not in json.dumps(packet)


def test_missing_command_metadata_has_no_second_open_or_content(tmp_path, monkeypatch):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(native.os, "open", lambda *args, **options: pytest.fail("metadata diagnostic opened original"))
        diagnostic = native._failed_command_metadata(fd, 1)
    finally:
        os.close(fd)
    assert diagnostic["metadata_state"] == "unavailable" and diagnostic["content_read_for_diagnostic"] is False


@pytest.mark.parametrize("sequence", [0, 10000, True, "1", "../secret"])
def test_unknown_command_sequence_never_reaches_stat(monkeypatch, sequence):
    monkeypatch.setattr(native.os, "stat", lambda *args, **options: pytest.fail("unknown diagnostic path reached stat"))
    with pytest.raises(gate.AdmissionError):
        native._failed_command_metadata(123, sequence)


def test_large_already_validated_result_keeps_only_original_proof(tmp_path, monkeypatch):
    failed_command(monkeypatch, tmp_path)
    path = tmp_path / "RESULT.json"
    value = json.loads(path.read_bytes())
    value["padding"] = "x" * gate.MAX_RECORD_BYTES
    path.write_text(json.dumps(value))
    (tmp_path / "command-0001.json").chmod(0o644)
    with pytest.raises(gate.AdmissionError) as caught:
        native.safe_export(tmp_path)
    partial = caught.value._bootstrap_partial_export
    assert set(partial["files"]["RESULT.json"]) == {"sha256", "metadata"}
    assert partial["files"]["RESULT.json"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert partial["complete"] is False


@pytest.mark.parametrize("name", ["RESULT.json", "FAILURE.json"])
def test_partial_export_never_outputs_original_paths_argv_or_tokens(tmp_path, monkeypatch, name):
    failed_command(monkeypatch, tmp_path)
    path = tmp_path / name
    value = json.loads(path.read_bytes())
    value.update(raw_root="/private/SYNTHETIC_PRIVATE_ROOT", argv=["SYNTHETIC_PRIVATE_ARGV"],
                 token="SYNTHETIC_PRIVATE_TOKEN", message="SYNTHETIC_PRIVATE_MESSAGE")
    path.write_text(json.dumps(value))
    (tmp_path / "command-0001.json").chmod(0o644)
    before = snapshot(tmp_path)
    with pytest.raises(gate.AdmissionError) as caught:
        native.safe_export(tmp_path)
    partial = caught.value._bootstrap_partial_export
    assert partial["complete"] is False and partial["execution_proof"] is False
    assert set(partial["files"][name]) == {"sha256", "metadata"}
    assert partial["files"][name]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "SYNTHETIC_PRIVATE_" not in json.dumps(partial)
    assert "content" not in partial["files"][name] and snapshot(tmp_path) == before
