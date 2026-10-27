"""Bounded native orchestration tests; real Docker proof is the hosted job."""
import json
import io
import os
from pathlib import Path
import sys
import tarfile

import pytest

from scripts import verify_production_startup_native as native
from scripts import production_startup_fixture as fixture
from scripts import private_release_contract as private


def test_bounded_archive_retains_both_original_streams(tmp_path):
    path = tmp_path / "command"
    native.archive_command([sys.executable, "-c", "import sys;print('synthetic stdout');print('synthetic stderr',file=sys.stderr)"], path)
    assert Path(str(path) + ".stdout").read_bytes() == b"synthetic stdout\n"
    assert Path(str(path) + ".stderr").read_bytes() == b"synthetic stderr\n"
    assert Path(str(path) + ".stdout").stat().st_mode & 0o777 == 0o600


def test_failed_command_keeps_original_failure_bytes(tmp_path):
    path = tmp_path / "command"
    with pytest.raises(private.ContractError):
        native.archive_command([sys.executable, "-c", "import sys;print('failure-original',file=sys.stderr);sys.exit(19)"], path)
    assert Path(str(path) + ".stderr").read_bytes() == b"failure-original\n"


def test_public_archive_modes_survive_private_outer_umask(tmp_path):
    archive = tmp_path / "public.tar"
    with tarfile.open(archive, "w") as output:
        directory = tarfile.TarInfo("core")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o775
        output.addfile(directory)
        source = tarfile.TarInfo("core/config.py")
        data = b"synthetic_public_source = True\n"
        source.size, source.mode = len(data), 0o664
        output.addfile(source, io.BytesIO(data))
        executable = tarfile.TarInfo("start.sh")
        executable.size, executable.mode = 0, 0o775
        output.addfile(executable, io.BytesIO())
    archive.chmod(0o600)
    original, repaired = tmp_path / "original", tmp_path / "repaired"
    original.mkdir(mode=0o700)
    repaired.mkdir(mode=0o700)
    previous = os.umask(0o077)
    try:
        native.archive_command(["tar", "-xf", str(archive), "--no-same-permissions", "-C", str(original)], tmp_path / "original-extract")
        native.extract_public_source(archive, repaired, tmp_path / "fixed-extract")
        current = os.umask(0o077)
        assert current == 0o077  # Only the extraction child changes umask.
    finally:
        os.umask(previous)
    assert (original / "core/config.py").stat().st_mode & 0o777 == 0o600
    assert (original / "core").stat().st_mode & 0o777 == 0o700
    assert (repaired / "core/config.py").stat().st_mode & 0o777 == 0o644
    assert (repaired / "core").stat().st_mode & 0o777 == 0o755
    assert (repaired / "start.sh").stat().st_mode & 0o777 == 0o755
    assert (repaired / "core/config.py").read_bytes() == data
    assert repaired.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "fixed-extract.stderr").stat().st_mode & 0o777 == 0o600


def test_image_probe_failure_retains_original_stderr_with_private_bound(tmp_path, monkeypatch):
    def probe(contract, directory, digest, *, runner):
        runner([sys.executable, "-c", "import sys;print('synthetic-probe-failure',file=sys.stderr);sys.exit(17)"])
    monkeypatch.setattr(private, "image_probe", probe)
    with pytest.raises(private.ContractError):
        native.probe_candidate({}, tmp_path, "sha256:" + "a" * 64, tmp_path)
    assert (tmp_path / "image-probe-0.stderr").read_bytes() == b"synthetic-probe-failure\n"
    assert (tmp_path / "image-probe-0.stderr").stat().st_mode & 0o777 == 0o600


def test_image_probe_transport_preserves_original_time_and_byte_limits(tmp_path, monkeypatch):
    calls = []
    def archive(args, path, **kwargs):
        calls.append(kwargs)
        Path(str(path) + ".stdout").write_bytes(b"synthetic-output")
    def probe(contract, directory, digest, *, runner):
        assert runner(["synthetic-command"]) == b"synthetic-output"
    monkeypatch.setattr(private, "image_probe", probe)
    monkeypatch.setattr(native, "archive_command", archive)
    native.probe_candidate({}, tmp_path, "sha256:" + "a" * 64, tmp_path)
    assert calls == [{"seconds": 120, "limit": private.MAX_BYTES}]


def test_command_deadline_is_bounded_and_keeps_early_bytes(tmp_path):
    path = tmp_path / "command"
    with pytest.raises(private.ContractError, match="deadline"):
        native.archive_command([sys.executable, "-u", "-c", "import time;print('before-stop');time.sleep(5)"], path, seconds=0.1)
    assert Path(str(path) + ".stdout").read_bytes() == b"before-stop\n"


def test_archive_failure_never_removes_unpreserved_container(tmp_path, monkeypatch):
    directory = tmp_path / "native"
    directory.mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr(native.private, "run", lambda args: b"a" * 12 + b"\n")
    def archive(args, path, **kwargs):
        calls.append(args)
        raise OSError("synthetic evidence disk full")
    monkeypatch.setattr(native, "archive_command", archive)
    with pytest.raises(OSError):
        native.preserve_scenario({"directory": directory, "project": "synthetic"})
    assert calls == [["docker", "inspect", "a" * 12]]


def test_synthetic_manifest_exact_real_commands_and_closed_limits(tmp_path):
    paths = {key: tmp_path / key for key in ("data", "fence", "grants", "slots")}
    manifest = fixture.manifest_input(paths, "synthetic", "a" * 64, 4, {"synthetic": True})
    services = manifest["services"]
    assert services["ai-filter-api"]["command"] == ["/app/start.sh"]
    assert services["ai-filter-worker"]["command"] == ["python", "worker_main.py"]
    for service in services.values():
        assert service["mem_limit"] == service["memswap_limit"] == "768m" and service["pids_limit"] == 256
        assert service["network_mode"] == "none" and service["cap_drop"] == ["ALL"] and service["cpus"] == 1
        assert service["restart"] == "no" and service["user"] == f"{os.geteuid()}:{os.getegid()}"
        assert all(mount["target"].startswith("/app/data/") or mount["target"] == "/app/config.yaml" for mount in service["volumes"])
        assert service["environment"]["AI_FILTER_ADMISSION_MODE"] == "enforced"
        assert "postgres" not in json.dumps(service).lower()



def test_slot_wait_requires_all_successes_in_the_same_fresh_round(tmp_path, monkeypatch):
    replies = iter(["closed_initialized", "startup_uncertain", "startup_uncertain", "closed_initialized",
                    "closed_initialized", "closed_initialized"])
    calls = []
    monkeypatch.setattr(native.gate, "_present", lambda _: True)
    monkeypatch.setattr(native.isolation, "_read_complete", lambda _: {"attempt_operation_id": "a" * 64})
    monkeypatch.setattr(native.time, "sleep", lambda _: None)
    def query(*args, **kwargs):
        calls.append(kwargs["slot"])
        return {"status": next(replies), "round_query": len(calls)}
    monkeypatch.setattr(native.control, "query", query)
    result = native.wait_for_slots(object(), {"paths": {"slots": tmp_path, "grants": tmp_path}}, ["web-1", "worker-1"])
    assert len(calls) == 6 and result["web-1"]["round_query"] == 5 and result["worker-1"]["round_query"] == 6



def test_review_strategy_uses_actual_loader_derived_version(tmp_path, monkeypatch):
    from core import config
    path = tmp_path / "synthetic.yaml"
    path.write_text("scoring_strategy_version: synthetic-startup\nfeeds: []\n")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    actual = config.load_config()["scoring_strategy_version"]
    assert actual.startswith("synthetic-startup:") and actual != "synthetic-startup"
    paths = {key: tmp_path / key for key in ("data", "fence", "grants", "slots", "project")}
    (tmp_path / "Dockerfile").write_text("FROM python@sha256:" + "a" * 64 + "\n")
    manifest = fixture.manifest_input(paths, "synthetic", "a" * 64, 1, {"synthetic": True})
    manifest_path, environment = tmp_path / "manifest.json", tmp_path / "environment"
    manifest_path.write_text(json.dumps(manifest))
    environment.write_text("AI_FILTER_API_KEY=synthetic\n")
    monkeypatch.setenv("AI_FILTER_API_KEY", "synthetic")
    monkeypatch.setattr(private, "run", lambda *a, **k: b"")
    monkeypatch.setattr(private, "inspect_project", lambda _: [])
    observed = []
    def seal(*args):
        observed.append(json.loads(args[4].read_text())["strategy"])
        return args[5] / "contract.json"
    monkeypatch.setattr(private, "seal", seal)
    fixture.seal_inputs(tmp_path, {"source": {}, "manifest": manifest, "config": path, "directory": tmp_path,
        "paths": paths, "project": "synthetic", "manifest_path": manifest_path, "environment": environment, "strategy": actual})
    assert observed == [actual]
