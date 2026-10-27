"""Bounded native orchestration tests; real Docker proof is the hosted job."""
import json
import io
import os
from pathlib import Path
import sqlite3
import sys
import tarfile

import pytest

from scripts import verify_production_startup_native as native
from scripts import production_startup_fixture as fixture
from scripts import private_release_contract as private


@pytest.fixture
def diagnostic_source(tmp_path, monkeypatch):
    root = tmp_path / "public-source"
    (root / "scripts").mkdir(parents=True)
    path = root / "scripts/public_failure.py"
    path.write_text("def fail():\n    raise RuntimeError('SECRET_MESSAGE_CANARY')\n\nfail()\n")
    monkeypatch.setattr(native, "DIAGNOSTIC_ROOT", root)
    return path


def diagnostic_record(capsys):
    captured = capsys.readouterr()
    assert not captured.out
    assert len(captured.err.encode()) <= native.DIAGNOSTIC_BYTES
    record = json.loads(captured.err)
    assert record["execution_proof"] is False
    assert set(record) == {"schema", "native_command_diagnostic", "execution_proof", "status", "exception_type", "locations"}
    return record


def test_real_child_traceback_only_emits_public_coordinates_after_original_sync(diagnostic_source, tmp_path, capsys, monkeypatch):
    path, synced = tmp_path / "failed", []
    original_sync, original_emit = native.gate._sync_directory, native._emit_archive_diagnostic
    def sync(directory):
        original_sync(directory)
        synced.append(directory)
    def emit(captured, complete):
        assert synced == [path.parent]
        assert all(Path(str(path) + suffix).stat().st_mode & 0o777 == 0o600 for suffix in (".stdout", ".stderr"))
        assert b"SECRET_MESSAGE_CANARY" in Path(str(path) + ".stderr").read_bytes()
        original_emit(captured, complete)
    monkeypatch.setattr(native.gate, "_sync_directory", sync)
    monkeypatch.setattr(native, "_emit_archive_diagnostic", emit)
    with pytest.raises(private.ContractError, match="synthetic command failed; original evidence retained"):
        native.archive_command([sys.executable, "-I", "-B", str(diagnostic_source)], path)
    record = diagnostic_record(capsys)
    assert record["exception_type"] == "RuntimeError" and record["status"] == "locations_available"
    assert record["locations"][-1] == {"file": "public_failure.py", "line": 2, "function": "fail"}
    assert all(value not in json.dumps(record) for value in (str(tmp_path), "SECRET_MESSAGE_CANARY", "raise RuntimeError"))
    original = Path(str(path) + ".stderr").read_bytes()
    assert original.startswith(b"Traceback (most recent call last):\n") and original.endswith(b"RuntimeError: SECRET_MESSAGE_CANARY\n")


@pytest.mark.parametrize("kind", ["fallback", "FAILURE.json", "RESULT.json"])
def test_complete_structured_child_failure_is_only_diagnostic_candidates(diagnostic_source, tmp_path, capsys, kind):
    frame = {"file": diagnostic_source.name, "line": 2, "function": "fail"}
    failure = {"type": "RuntimeError", "locations": [frame], "message": "SECRET_MESSAGE_CANARY"}
    if kind == "fallback":
        packet = {"status": "unconfirmed", "safe_export_complete": False, "error_type": "RuntimeError",
                  "locations": [frame], "error_sha256": "a" * 64, "token": "SECRET_TOKEN_CANARY"}
    else:
        content = failure if kind == "FAILURE.json" else {"failure": failure}
        packet = {"status": "unconfirmed", "safe_evidence": {"files": {kind: {"content": content}}}}
    raw = json.dumps(packet).encode() + b"\n"
    path = tmp_path / "structured"
    with pytest.raises(private.ContractError):
        native.archive_command([sys.executable, "-c", "import sys;sys.stdout.buffer.write(" + repr(raw) + ");sys.exit(3)"], path)
    record = diagnostic_record(capsys)
    assert record["locations"] == [frame] and record["exception_type"] == "RuntimeError"
    assert "SECRET_" not in json.dumps(record) and Path(str(path) + ".stdout").read_bytes() == raw


@pytest.mark.parametrize("change", ["external", "fake_project_path", "function", "line", "truncated", "no_exception", "prefix", "unknown_class"])
def test_fake_or_partial_stderr_never_exports_unverified_text(diagnostic_source, tmp_path, capsys, change):
    file = "/outside/SECRET_PATH/public_failure.py" if change == "external" else str(diagnostic_source)
    if change == "fake_project_path":
        file = str(diagnostic_source.parent.parent / "nonexistent/public_failure.py")
    function, line = ("SECRET_FUNCTION", 2) if change == "function" else ("fail", 999999 if change == "line" else 2)
    kind = "SECRET_CLASS" if change == "unknown_class" else "RuntimeError"
    raw = (f'Traceback (most recent call last):\n  File "{file}", line {line}, in {function}\n'
           f'    raise RuntimeError("SECRET_SOURCE")\n{kind}: SECRET_MESSAGE\n').encode()
    if change == "truncated":
        raw = raw[:-1]
    elif change == "no_exception":
        raw = raw[:raw.rfind(kind.encode())]
    elif change == "prefix":
        raw = b"arbitrary prefix\n" + raw
    path = tmp_path / "untrusted"
    with pytest.raises(private.ContractError):
        native.archive_command([sys.executable, "-c", "import sys;sys.stderr.buffer.write(" + repr(raw) + ");sys.exit(9)"], path)
    record = diagnostic_record(capsys)
    assert "SECRET_" not in json.dumps(record) and str(tmp_path) not in json.dumps(record)
    if change == "unknown_class":
        assert record["exception_type"] == "unknown"
    else:
        assert record["status"] == "no_verified_frames" and not record["locations"]
    assert Path(str(path) + ".stderr").read_bytes() == raw


@pytest.mark.parametrize("change", ["duplicate_json", "truncated_json", "oversized", "ambiguous_source", "source_symlink", "too_many_frames"])
def test_structured_diagnostic_rejects_ambiguity_and_bounds(diagnostic_source, tmp_path, capsys, change):
    frame = {"file": diagnostic_source.name, "line": 2, "function": "fail"}
    packet = {"status": "unconfirmed", "safe_export_complete": False, "error_type": "RuntimeError", "locations": [frame]}
    if change == "too_many_frames":
        packet["locations"] *= 65
    raw = json.dumps(packet).encode() + b"\n"
    if change == "duplicate_json":
        raw = raw.replace(b'"status":', b'"status":"unconfirmed","status":')
    elif change == "truncated_json":
        raw = raw[:-2]
    elif change == "oversized":
        raw += b" " * native.DIAGNOSTIC_INPUT_BYTES[0]
    elif change == "ambiguous_source":
        (diagnostic_source.parent.parent / diagnostic_source.name).write_bytes(diagnostic_source.read_bytes())
    elif change == "source_symlink":
        outside = tmp_path / "outside.py"
        diagnostic_source.rename(outside)
        diagnostic_source.symlink_to(outside)
    path, input_file = tmp_path / "rejected", tmp_path / "child-input"
    input_file.write_bytes(raw)
    with pytest.raises(private.ContractError):
        native.archive_command([sys.executable, "-c", "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes());sys.exit(3)",
                                str(input_file)], path)
    assert diagnostic_record(capsys)["status"] == "no_verified_frames"
    assert Path(str(path) + ".stdout").read_bytes() == raw


def test_diagnostic_write_failure_does_not_replace_original_command_failure(tmp_path, monkeypatch):
    class Unavailable:
        def write(self, _raw):
            raise OSError("SECRET_OUTPUT_FAILURE")
    with monkeypatch.context() as patch:
        patch.setattr(native.sys, "stderr", Unavailable())
        with pytest.raises(private.ContractError, match="synthetic command failed; original evidence retained"):
            native.archive_command([sys.executable, "-c", "raise SystemExit(17)"], tmp_path / "failed")


def test_output_limit_still_fails_without_treating_partial_capture_as_complete(tmp_path, capsys):
    path = tmp_path / "bounded"
    with pytest.raises(private.ContractError, match="evidence byte bound"):
        native.archive_command([sys.executable, "-u", "-c", "import sys;sys.stderr.write('SECRET'*1000)"], path, limit=32)
    record = diagnostic_record(capsys)
    assert record["status"] == "no_verified_frames" and "SECRET" not in json.dumps(record)


def test_diagnostic_frame_and_class_bound_does_not_treat_plain_class_as_exception(diagnostic_source, capsys):
    diagnostic_source.write_text("class PlainPublicClass:\n    pass\n\ndef fail():\n    raise RuntimeError('SECRET')\n")
    packet = {"status": "unconfirmed", "safe_export_complete": False, "error_type": "PlainPublicClass",
              "locations": [{"file": diagnostic_source.name, "line": 5, "function": "fail"}] * 64}
    native._emit_archive_diagnostic([json.dumps(packet).encode(), b""], True)
    value = diagnostic_record(capsys)
    assert len(value["locations"]) == 8 and value["exception_type"] == "unknown"


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


def test_bounded_archive_stdin_is_not_exposed_in_argv_or_evidence(tmp_path):
    path = tmp_path / "command"
    native.archive_command([sys.executable, "-I", "-c",
                            "import sys; x=sys.stdin.buffer.read(); print(len(x)); print('safe',file=sys.stderr)"],
                           path, stdin_bytes=b"synthetic-stdin-only")
    assert Path(str(path) + ".stdout").read_bytes() == b"20\n"
    assert Path(str(path) + ".stderr").read_bytes() == b"safe\n"
    assert all(b"synthetic-stdin-only" not in file.read_bytes() for file in tmp_path.iterdir())


def test_bounded_archive_rejects_large_stdin_before_any_child(tmp_path):
    with pytest.raises(private.ContractError, match="stdin bound"):
        native.archive_command([sys.executable, "-c", "raise Exception('must not run')"],
                               tmp_path / "not-created", stdin_bytes=b"x" * 257)
    assert not list(tmp_path.iterdir())


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


def test_migration_sample_gets_fresh_bounded_reader_after_long_image_build(tmp_path, monkeypatch):
    migration = fixture.migration
    now = migration.time.monotonic
    elapsed = [0]
    monkeypatch.setattr(migration.time, "monotonic", lambda: now() + elapsed[0])
    anchor, _target = fixture.seed_databases(tmp_path)
    path = tmp_path / "fixture.db"
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
    try:
        before = migration.database_fingerprint(anchor)
        identities = [(p.stat().st_dev, p.stat().st_ino) for p in sidecars]
        elapsed[0] = 31  # Actual image build can outlast a SQL read budget.
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            migration.database_fingerprint(anchor)
        assert fixture.database_before(path) == before
        assert [(p.stat().st_dev, p.stat().st_ino) for p in sidecars] == identities
        # The old anchor was neither closed nor given a renewed deadline.
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            migration.database_fingerprint(anchor)
    finally:
        anchor.close()


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
