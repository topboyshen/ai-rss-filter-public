"""Do not turn incomplete identity evidence into a cutover permit."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts/takeover_readonly_preflight.py"
SPEC = importlib.util.spec_from_file_location("takeover_readonly", PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def manifest():
    return {"schema": "takeover-readonly-preflight-v1", "docker_binary": "/usr/bin/docker",
            "docker_sha256": "a" * 64, "containers": ["synthetic-api"],
            "images": ["sha256:" + "b" * 64], "files": []}


def container():
    return {"Name": "/synthetic-api", "Id": "c" * 64, "Image": "sha256:" + "b" * 64,
            "Config": {"Env": ["TOKEN=never-print-this"], "Cmd": ["--password", "never-print-this"],
                       "Labels": {"org.opencontainers.image.revision": "d" * 40}},
            "HostConfig": {"Memory": 768 * 1024**2, "PidsLimit": 256},
            "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z", "OOMKilled": False},
            "RestartCount": 0, "Mounts": [{"Source": "/private-secret-name", "Destination": "/app/data"}]}


def valid_run(argv, timeout):
    assert argv[:3] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
    assert timeout == 5
    if argv[3:5] == ["container", "inspect"]:
        return json.dumps([container()]).encode()
    assert argv[3:5] == ["image", "inspect"]
    return json.dumps([{"Id": "sha256:" + "b" * 64}]).encode()


def test_complete_identity_snapshot_is_never_release_or_inventory_approval():
    rows = []
    assert probe.collect(manifest(), rows.append, run=valid_run, bind=lambda *a: None) == 0
    result = rows[-1]
    assert result["status"] == "bounded_identity_snapshot"
    for key in ("release_authorized", "legacy_originals_proven", "queue_empty_proven",
                "producer_stop_proven", "database_compatibility_proven"):
        assert result[key] is False
    assert "never-print-this" not in json.dumps(rows)
    assert "private-secret-name" not in json.dumps(rows)
    assert rows[2]["evidence"][0]["memory"] == 768 * 1024**2


@pytest.mark.parametrize("reason", ["command_timeout", "command_failed", "command_output_limit"])
def test_failed_later_phase_retains_prior_evidence_and_does_not_retry(reason):
    rows, calls = [], []
    def run(argv, timeout):
        calls.append(argv)
        if len(calls) == 2:
            raise probe.EvidenceFailure(reason)
        return valid_run(argv, timeout)
    assert probe.collect(manifest(), rows.append, run=run, bind=lambda *a: None) == 2
    assert len(calls) == 2
    assert rows[-1]["reason"] == reason
    assert rows[-1]["completed_phases"] == ["executable", "containers"]
    assert not any(row["event"] == "finish" for row in rows)


def test_budget_exhaustion_stops_before_next_command():
    rows, calls = [], []
    now = [0]
    def clock():
        return now[0]
    def run(argv, timeout):
        calls.append(argv)
        now[0] = 20
        return valid_run(argv, timeout)
    assert probe.collect(manifest(), rows.append, run=run, clock=clock, bind=lambda *a: None) == 2
    assert len(calls) == 1
    assert rows[-1]["reason"] == "budget_insufficient"
    assert rows[-1]["phase"] == "retained_images"


def test_manifest_dry_run_never_executes_or_creates_files(tmp_path):
    path = tmp_path / "manifest.json"
    raw = json.dumps(manifest()).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    before = sorted(p.name for p in tmp_path.iterdir())
    result = subprocess.run([sys.executable, "-I", "-B", str(PATH), "--manifest", str(path)],
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "plan_only", "manifest_sha256": probe.digest(raw),
                                        "production_queries": 0, "release_authorized": False}
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("key,value", [
    ("containers", ["--help"]), ("containers", ["api", "api"]),
    ("containers", []), ("containers", ["a"] * 9),
    ("images", ["mutable:latest"]), ("images", []),
    ("docker_binary", "/tmp/custom-command"), ("docker_sha256", "x" * 64),
    ("files", [{"path": "/app/../secret", "kind": "sha256"}]),
    ("files", [{"path": "/path", "kind": "exec"}]),
])
def test_unsafe_or_unbounded_targets_fail_before_execution(key, value):
    data = manifest()
    data[key] = value
    with pytest.raises(probe.EvidenceFailure):
        probe.parse_manifest(json.dumps(data).encode())


def test_duplicate_manifest_keys_rejected():
    raw = json.dumps(manifest())[:-1] + ',"files":[]}'
    with pytest.raises(probe.EvidenceFailure, match="duplicate_manifest_key"):
        probe.parse_manifest(raw.encode())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public_mode"])
def test_manifest_file_unsafe_identity_rejected_without_repair(tmp_path, kind):
    original = tmp_path / "original"
    original.write_text("{}")
    original.chmod(0o600)
    target = tmp_path / "target"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    else:
        target = original
        target.chmod(0o644)
    before = original.stat()
    with pytest.raises((OSError, probe.EvidenceFailure)):
        probe.private_read(target)
    after = original.stat()
    assert (before.st_ino, before.st_mode, before.st_mtime_ns) == (after.st_ino, after.st_mode, after.st_mtime_ns)
    assert original.read_text() == "{}"


def test_wrong_container_or_retained_image_is_not_accepted():
    row = copy.deepcopy(container())
    row["Name"] = "/unreviewed"
    with pytest.raises(probe.EvidenceFailure, match="container_names"):
        probe.container_rows(json.dumps([row]).encode(), ["synthetic-api"])
    with pytest.raises(probe.EvidenceFailure, match="retained_image_identity"):
        probe.image_rows(b'[{"Id":"sha256:wrong"}]', manifest()["images"])


def test_raw_transport_exception_is_sanitized_and_no_retry():
    rows, calls = [], []
    def run(argv, timeout):
        calls.append(argv)
        raise RuntimeError("TOKEN=never-print-this")
    assert probe.collect(manifest(), rows.append, run=run, bind=lambda *a: None) == 2
    assert len(calls) == 1
    assert rows[-1]["reason"] == "evidence_unavailable"
    assert "never-print-this" not in json.dumps(rows)


def test_actual_client_timeout_retains_no_child_output():
    with pytest.raises(probe.EvidenceFailure, match="command_timeout"):
        probe.capture([sys.executable, "-I", "-c", "import time;time.sleep(30)"], 0.05)


def test_actual_client_nonzero_stderr_is_not_returned():
    with pytest.raises(probe.EvidenceFailure, match="command_failed") as caught:
        probe.capture([sys.executable, "-I", "-c", "import sys;sys.stderr.write('secret');sys.exit(3)"], 2)
    assert "secret" not in str(caught.value)


def test_binary_mismatch_stops_before_docker_and_preserves_fixed_reason():
    rows = []
    def bind(*args):
        raise probe.EvidenceFailure("docker_executable_changed")
    def forbidden(*args):
        pytest.fail("must not run an unbound client")
    assert probe.collect(manifest(), rows.append, run=forbidden, bind=bind) == 2
    assert rows[-1]["completed_phases"] == []
    assert rows[-1]["reason"] == "docker_executable_changed"


def test_evidence_hash_allows_read_only_source_without_echoing_content(tmp_path):
    target = tmp_path / "script"
    target.write_text("private source or config content")
    target.chmod(0o400)
    result = probe.file_fact({"path": str(target), "kind": "sha256"})
    assert result["content_sha256"] == probe.digest(target.read_bytes())
    assert "private source" not in json.dumps(result)
    assert result["mode"] == 0o400


def test_symlink_parent_refused_for_metadata_and_hash(tmp_path):
    directory = tmp_path / "original"
    directory.mkdir()
    target = directory / "file"
    target.write_text("evidence")
    target.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    for kind in ("metadata", "sha256"):
        with pytest.raises(probe.EvidenceFailure, match="symlink_parent"):
            probe.file_fact({"path": str(alias / "file"), "kind": kind})


def test_actual_client_output_limit_counts_stderr_without_relaying_it():
    command = [sys.executable, "-I", "-c", "import sys;sys.stderr.buffer.write(b'x'*1100000)"]
    with pytest.raises(probe.EvidenceFailure, match="command_output_limit"):
        probe.capture(command, 2)
