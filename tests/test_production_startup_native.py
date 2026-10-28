"""Bounded native orchestration tests; real Docker proof is the hosted job."""
import json
import io
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time

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


def cancel_controller_after_recorded_chunk(tmp_path, stream):
    controller = tmp_path / "controller.py"
    controller.write_text('''from pathlib import Path
import builtins,json,os,signal,sys
sys.path.insert(0,sys.argv[1])
from scripts import verify_production_startup_native as native
root=Path(sys.argv[2]); path=root/'command'; marker=root/'READ_BOUNDARY.json'
real_read,real_fsync,real_open=os.read,os.fsync,builtins.open
state={'read_chunk':False,'synced':[],'files':{}}
def opened(name,*args,**kwargs):
 stream=real_open(name,*args,**kwargs)
 if str(name) in (str(path)+'.stdout',str(path)+'.stderr'):state['files'][stream.fileno()]=str(name)
 return stream
def synced(fd):
 real_fsync(fd)
 if fd in state['files']:state['synced'].append(Path(state['files'][fd]).name)
def read(fd,size):
 if state['read_chunk']:
  packet={'stdout':Path(str(path)+'.stdout').read_text(),'stderr':Path(str(path)+'.stderr').read_text(),'synced':state['synced']}
  raw=json.dumps(packet,sort_keys=True).encode();pending=marker.with_suffix('.pending');out=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  try:os.write(out,raw);real_fsync(out)
  finally:os.close(out)
  os.rename(pending,marker)
  signal.pause()
 data=real_read(fd,size)
 if data:state['read_chunk']=True
 return data
native.open=opened;native.os.read=read;native.os.fsync=synced
native.archive_command([sys.executable,'-I','-B','-c',"import sys;print('fixed synthetic phase',file=sys."+sys.argv[3]+",flush=True)"],path,seconds=5,limit=128)
''')
    root = Path(native.__file__).resolve().parents[1]
    process = subprocess.Popen([sys.executable, "-I", "-B", str(controller), str(root), str(tmp_path), stream],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "READ_BOUNDARY.json").exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.01)
        assert (tmp_path / "READ_BOUNDARY.json").exists(), "controller did not reach a second read"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == -signal.SIGTERM and stdout == stderr == b""
        return json.loads((tmp_path / "READ_BOUNDARY.json").read_bytes())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_controller_cancellation_keeps_chunk_synced_before_next_read_without_claiming_success(tmp_path, stream):
    result = cancel_controller_after_recorded_chunk(tmp_path, stream)
    assert result[stream] == "fixed synthetic phase\n"
    assert result["stderr" if stream == "stdout" else "stdout"] == ""
    assert result["synced"] == ["command." + stream]
    assert Path(str(tmp_path / "command") + "." + stream).read_bytes() == b"fixed synthetic phase\n"


@pytest.mark.parametrize("failure", ["short", "zero", "none"])
def test_archive_short_write_is_a_failure_and_never_retried(tmp_path, monkeypatch, failure):
    actual = open
    writes = []
    class ShortWriter:
        def __init__(self, stream):
            self.stream, self.name = stream, stream.name
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, raw):
            writes.append(bytes(raw))
            if failure == "none":
                return None
            return self.stream.write(raw[:-1] if failure == "short" else b"")
    def opened(name, *args, **kwargs):
        assert kwargs["buffering"] == 0
        return ShortWriter(actual(name, *args, **kwargs))
    monkeypatch.setattr(native, "open", opened, raising=False)
    path = tmp_path / "short"
    with pytest.raises(private.ContractError, match="evidence short write"):
        native.archive_command([sys.executable, "-c", "print('fixed original',flush=True)"], path)
    assert writes == [b"fixed original\n"]
    assert Path(str(path) + ".stdout").read_bytes() == (b"fixed original" if failure == "short" else b"")


@pytest.mark.parametrize("failure", ["chunk_sync", "final_sync", "directory_sync", "close"])
def test_archive_preservation_failure_never_returns_success(tmp_path, monkeypatch, failure):
    actual_open, actual_sync = open, native.os.fsync
    calls, closed = [], []
    class CloseFailure:
        def __init__(self, stream):
            self.stream, self.name = stream, stream.name
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def close(self):
            self.stream.close()
            closed.append(self.name)
            raise OSError("synthetic output close failure")
    if failure == "close":
        monkeypatch.setattr(native, "open", lambda *a, **k: CloseFailure(actual_open(*a, **k)), raising=False)
    def sync(fd):
        calls.append(fd)
        if failure == "chunk_sync" or failure == "final_sync" and len(calls) > 1:
            raise OSError("synthetic output synchronization failure")
        return actual_sync(fd)
    monkeypatch.setattr(native.os, "fsync", sync)
    if failure == "directory_sync":
        monkeypatch.setattr(native.gate, "_sync_directory", lambda *a: (_ for _ in ()).throw(OSError("synthetic directory failure")))
    with pytest.raises(OSError, match="synthetic"):
        native.archive_command([sys.executable, "-c", "print('fixed original',flush=True)"], tmp_path / "sync")
    assert Path(str(tmp_path / "sync") + ".stdout").read_bytes() == b"fixed original\n"
    if failure == "close":
        assert len(closed) == 2


def test_real_nonzero_exit_remains_primary_when_all_output_cleanup_also_fails(tmp_path, monkeypatch):
    calls = []
    def failed_sync(_fd):
        calls.append("file")
        raise OSError("synthetic cleanup synchronization failure")
    def failed_directory(_root):
        calls.append("directory")
        raise OSError("synthetic cleanup directory failure")
    monkeypatch.setattr(native.os, "fsync", failed_sync)
    monkeypatch.setattr(native.gate, "_sync_directory", failed_directory)
    monkeypatch.setattr(native, "_emit_archive_diagnostic", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(private.ContractError, match="synthetic command failed; original evidence retained") as raised:
        native.archive_command([sys.executable, "-c", "raise SystemExit(19)"], tmp_path / "failed")
    assert calls == ["file", "file", "directory"]
    assert len(raised.value.__notes__) == 4
    assert all("synthetic cleanup" not in note for note in raised.value.__notes__)


@pytest.mark.parametrize("limit", [21, 22])
def test_per_chunk_sync_retains_exact_original_byte_limit(tmp_path, limit):
    path = tmp_path / "exact"
    command = [sys.executable, "-c", "print('fixed synthetic phase',flush=True)"]
    if limit == 22:
        native.archive_command(command, path, limit=limit)
        assert Path(str(path) + ".stdout").read_bytes() == b"fixed synthetic phase\n"
    else:
        with pytest.raises(private.ContractError, match="evidence byte bound"):
            native.archive_command(command, path, limit=limit)
        assert Path(str(path) + ".stdout").read_bytes() == b""


def test_chunk_sync_time_is_charged_to_original_deadline(tmp_path, monkeypatch):
    clock, synced = {"now": 0}, []
    original_sync = native.os.fsync
    monkeypatch.setattr(native.time, "monotonic", lambda: clock["now"])
    def sync(fd):
        original_sync(fd)
        synced.append(fd)
        clock["now"] = 2
    monkeypatch.setattr(native.os, "fsync", sync)
    path = tmp_path / "deadline"
    with pytest.raises(private.ContractError, match="command deadline exceeded"):
        native.archive_command([sys.executable, "-c", "print('fixed synthetic phase',flush=True)"], path, seconds=1)
    assert synced and Path(str(path) + ".stdout").read_bytes() == b"fixed synthetic phase\n"


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


@pytest.fixture(scope="module")
def public_repository(tmp_path_factory):
    import subprocess
    repo = tmp_path_factory.mktemp("shared-public-git")
    (repo / "core").mkdir()
    for name, raw in {"config.yaml": b"feeds: []\n", "docker-compose.yml": b"services: {}\n",
                      "Dockerfile": b"FROM synthetic@sha256:" + b"a" * 64 + b"\n",
                      "core/public.py": b"public = True\n", "start.sh": b"#!/bin/sh\nexit 0\n"}.items():
        (repo / name).write_bytes(raw)
    (repo / "start.sh").chmod(0o755)
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                                               "commit", "-qm", "synthetic public source")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo, private.source_identity(repo)


@pytest.fixture
def shared_source(public_repository, tmp_path):
    repo, source = public_repository
    shared = native.SharedPublicSource(repo, tmp_path / "s", source)
    try:
        yield repo, source, shared
    finally:
        shared.close()


def synthetic_contract(number):
    return {"files": {"compose.json": str(number) * 64, "runtime.yaml": str(number + 4) * 64}}


def fake_candidate_docker(monkeypatch):
    """Only Docker is doubled; Git archive, extraction and complete-tree IO are real."""
    original_archive, original_run = native.archive_command, private.run
    builds, inspected = [], []
    def archive(args, path, **options):
        if args[:2] != ["docker", "build"]:
            return original_archive(args, path, **options)
        builds.append((args, options))
        native.save(Path(str(path) + ".stdout"), b"synthetic build stdout\n")
        native.save(Path(str(path) + ".stderr"), b"synthetic build stderr\n")
    def run(args, **kwargs):
        if args[:3] != ["docker", "image", "inspect"]:
            return original_run(args, **kwargs)
        inspected.append(args[-1])
        build_args = builds[-1][0]
        values = dict(build_args[index + 1].split("=", 1) for index, value in enumerate(build_args) if value == "--build-arg")
        labels = {"io.ai-filter.private-release.sha256": values["AI_FILTER_PRIVATE_RELEASE_SHA"],
                  "io.ai-filter.runtime-config.sha256": values["AI_FILTER_RUNTIME_CONFIG_SHA"],
                  "io.ai-filter.config.sha256": values["AI_FILTER_BUILD_CONFIG_SHA"],
                  "org.opencontainers.image.revision": values["AI_FILTER_BUILD_GIT_SHA"]}
        return json.dumps([{"Id": "sha256:" + str(len(builds)) * 64, "Config": {"Labels": labels}}]).encode()
    monkeypatch.setattr(native, "archive_command", archive)
    monkeypatch.setattr(private, "run", run)
    return builds, inspected


def test_four_independent_builds_share_exact_public_bytes_without_mode_changes(shared_source, tmp_path, monkeypatch):
    repo, source, shared = shared_source
    builds, inspected = fake_candidate_docker(monkeypatch)
    baseline = native._source_tree(shared.fds[2])
    for number in range(1, 5):
        workspace = tmp_path / f"n{number}"
        workspace.mkdir(mode=0o700)
        digest = native.build_candidate(repo, workspace, source, f"synthetic:{number}", synthetic_contract(number),
                                        "c" * 64, shared_source=shared)
        assert digest == "sha256:" + str(number) * 64
        assert json.loads((workspace / "shared-source-before.json").read_bytes()) == shared.reference
        assert json.loads((workspace / "shared-source-after.json").read_bytes()) == shared.reference
        assert (workspace / "image-inspect.json").is_file()
        assert (workspace / "application-build.stdout").read_bytes() == b"synthetic build stdout\n"
        assert (workspace / "application-build.stderr").read_bytes() == b"synthetic build stderr\n"
        assert not (workspace / "context").exists() and not (workspace / "source.tar").exists()
    assert len(builds) == len(inspected) == 4
    assert [item[0][item[0].index("--tag") + 1] for item in builds] == [f"synthetic:{n}" for n in range(1, 5)]
    for number, (args, options) in enumerate(builds, 1):
        assert args[-1] == str(shared.context) and options == {"seconds": 600}
        assert f"AI_FILTER_BUILD_MANIFEST_SHA={str(number) * 64}" in args
        assert f"AI_FILTER_RUNTIME_CONFIG_SHA={str(number + 4) * 64}" in args
    assert native._source_tree(shared.fds[2]) == baseline
    assert (shared.context / "core/public.py").stat().st_mode & 0o777 == 0o644
    assert (shared.context / "start.sh").stat().st_mode & 0o777 == 0o755
    assert (shared.context / "core").stat().st_mode & 0o777 == 0o755
    assert shared.context.stat().st_mode & 0o777 == 0o700
    assert len(list(tmp_path.rglob("source.tar"))) == 1
    assert {path.name for path in shared.directory.iterdir()} == {
        "source.tar", "context", "SOURCE.json", "git-archive.stdout", "git-archive.stderr", "archive-extract.stdout", "archive-extract.stderr"}


@pytest.mark.parametrize("change", ["bytes", "same_bytes_replacement", "add", "delete", "mode", "symlink", "hardlink",
                                   "directory", "context", "archive_bytes", "archive_replacement", "archive_link", "parent"])
def test_shared_public_source_rejects_actual_tree_or_archive_drift_before_build(shared_source, tmp_path, monkeypatch, change):
    repo, source, shared = shared_source
    builds, inspected = fake_candidate_docker(monkeypatch)
    target = shared.context / "core/public.py"
    original = target.read_bytes()
    if change == "bytes":
        target.write_bytes(b"tampered True\n")
    elif change == "same_bytes_replacement":
        target.unlink()
        target.write_bytes(original)
    elif change == "add":
        (shared.context / "new").write_bytes(b"added")
    elif change == "delete":
        target.unlink()
    elif change == "mode":
        target.chmod(0o600)
    elif change in ("symlink", "hardlink"):
        target.unlink()
        external = tmp_path / "external-original"
        external.write_bytes(original)
        getattr(os, "symlink" if change == "symlink" else "link")(external, target)
    elif change in ("directory", "context", "parent"):
        selected = {"directory": shared.context / "core", "context": shared.context, "parent": shared.directory}[change]
        selected.rename(tmp_path / "retained-original")
        selected.mkdir(mode=0o700)
    elif change == "archive_bytes":
        with (shared.directory / "source.tar").open("r+b") as handle:
            handle.write(b"changed")
    else:
        archive = shared.directory / "source.tar"
        original_archive = archive.read_bytes()
        archive.rename(tmp_path / "retained-archive")
        if change == "archive_link":
            archive.symlink_to(tmp_path / "retained-archive")
        else:
            archive.write_bytes(original_archive)
            archive.chmod(0o600)
    workspace = tmp_path / "n1"
    workspace.mkdir(mode=0o700)
    with pytest.raises((private.ContractError, FileNotFoundError)):
        native.build_candidate(repo, workspace, source, "synthetic:1", synthetic_contract(1), "c" * 64, shared_source=shared)
    assert builds == inspected == [] and not (workspace / "shared-source-after.json").exists()
    assert (repo / "core/public.py").read_bytes() == original


@pytest.mark.parametrize("original_failure", [False, True])
def test_shared_source_post_build_drift_stops_before_inspect_and_keeps_original_failure(shared_source, tmp_path, monkeypatch, original_failure):
    repo, source, shared = shared_source
    builds, inspected = fake_candidate_docker(monkeypatch)
    original_archive = native.archive_command
    error = RuntimeError("original synthetic Docker failure")
    def archive(args, path, **kwargs):
        original_archive(args, path, **kwargs)
        (shared.context / "core/public.py").write_bytes(b"changed during build")
        if original_failure:
            raise error
    monkeypatch.setattr(native, "archive_command", archive)
    workspace = tmp_path / "n1"
    workspace.mkdir(mode=0o700)
    with pytest.raises(RuntimeError if original_failure else private.ContractError) as raised:
        native.build_candidate(repo, workspace, source, "synthetic:1", synthetic_contract(1), "c" * 64, shared_source=shared)
    if original_failure:
        assert raised.value is error and isinstance(error.__cause__, private.ContractError)
    assert len(builds) == 1 and inspected == []
    assert (workspace / "application-build.stdout").read_bytes() == b"synthetic build stdout\n"
    assert (workspace / "application-build.stderr").read_bytes() == b"synthetic build stderr\n"
    assert not (workspace / "shared-source-after.json").exists()
    assert (shared.directory / "source.tar").is_file() and (shared.context / "core/public.py").is_file()


def test_shared_source_does_not_trust_rewritten_inventory_json(shared_source):
    repo, source, shared = shared_source
    (shared.context / "core/public.py").write_bytes(b"not Git public bytes")
    native.save(shared.directory / "SOURCE.json", {"source": source, "tree": native._source_tree(shared.fds[2])})
    with pytest.raises(private.ContractError, match="content changed"):
        shared.check(repo, source)


def test_shared_source_rejects_wrong_source_and_dirty_original_tree(shared_source):
    repo, source, shared = shared_source
    with pytest.raises(private.ContractError, match="identity differs"):
        shared.check(repo, {**source, "source_sha": "f" * 40})
    original = (repo / "core/public.py").read_bytes()
    try:
        (repo / "core/public.py").write_bytes(b"dirty working source")
        with pytest.raises(private.ContractError, match="clean source"):
            shared.check(repo, source)
    finally:
        (repo / "core/public.py").write_bytes(original)
    assert private.source_identity(repo) == source


@pytest.mark.parametrize("change", ["bytes", "missing", "extra", "link"])
def test_extraction_is_matched_against_actual_complete_archive(public_repository, tmp_path, monkeypatch, change):
    repo, source = public_repository
    extract = native.extract_public_source
    def changed(archive, context, evidence):
        extract(archive, context, evidence)
        path = context / "core/public.py"
        if change == "bytes":
            path.write_bytes(b"wrong extracted bytes")
        elif change == "missing":
            path.unlink()
        elif change == "extra":
            (context / "extra").write_bytes(b"extra")
        else:
            path.unlink()
            path.symlink_to(repo / "core/public.py")
    monkeypatch.setattr(native, "extract_public_source", changed)
    with pytest.raises(private.ContractError):
        native.SharedPublicSource(repo, tmp_path / "s", source)
    assert (tmp_path / "s/source.tar").is_file() and (tmp_path / "s/archive-extract.stdout").is_file()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "traversal", "duplicate", "fifo"])
def test_shared_source_rejects_unsafe_archive_before_any_extraction(tmp_path, kind):
    archive_path = tmp_path / "bad.tar"
    with tarfile.open(archive_path, "w") as archive:
        item = tarfile.TarInfo("../outside" if kind == "traversal" else "public.py")
        if kind in ("symlink", "hardlink"):
            item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            item.linkname = "/outside"
        elif kind == "fifo":
            item.type = tarfile.FIFOTYPE
        archive.addfile(item)
        if kind == "duplicate":
            archive.addfile(item)
    with archive_path.open("rb") as handle, pytest.raises(private.ContractError):
        native._archive_content(handle.fileno())
    assert not (tmp_path / "outside").exists()


def test_existing_oneoff_builder_keeps_its_own_context_and_original_modes(public_repository, tmp_path, monkeypatch):
    repo, source = public_repository
    builds, inspected = fake_candidate_docker(monkeypatch)
    workspace = tmp_path / "oneoff"
    workspace.mkdir(mode=0o700)
    native.build_candidate(repo, workspace, source, "synthetic:1", synthetic_contract(1), "c" * 64)
    assert len(builds) == len(inspected) == 1
    assert builds[0][0][-1] == str(workspace / "context")
    assert (workspace / "source.tar").is_file() and not (workspace / "shared-source-before.json").exists()
    assert (workspace / "context/start.sh").stat().st_mode & 0o777 == 0o755


def test_execute_preserves_four_worker_scenarios_and_one_checked_source(public_repository, tmp_path, monkeypatch):
    repo, source = public_repository
    original_run, original_archive = private.run, native.archive_command
    monkeypatch.setattr(native.sys, "platform", "linux")
    def run(args, **kwargs):
        return b"amd64\n" if args[:2] == ["docker", "info"] else original_run(args, **kwargs)
    def archive(args, path, **kwargs):
        if args[:2] == ["docker", "pull"]:
            assert kwargs == {"seconds": 180}
            return
        return original_archive(args, path, **kwargs)
    calls = []
    def scenario(root, workspace, workers, dsn, miniflux, stage, report, shared):
        calls.append((workers, shared, shared.check(root, source)))
    monkeypatch.setattr(private, "run", run)
    monkeypatch.setattr(native, "archive_command", archive)
    monkeypatch.setattr(native, "scenario", scenario)
    dsn = tmp_path / "dsn.json"
    native.save(dsn, {"dsn": "synthetic-no-connection"})
    native.execute(repo, tmp_path / "output", dsn, tmp_path / "unused-miniflux")
    assert [row[0] for row in calls] == [1, 2, 3, 4]
    assert len({id(row[1]) for row in calls}) == 1 and all(row[2] == calls[0][2] for row in calls)
    assert calls[0][1].fds == []
    report = json.loads((tmp_path / "output/report.json").read_bytes())
    assert report["status"] == "passed" and report["shared_public_source"] == calls[0][2]


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_shared_source_scan_bounds_fail_closed_without_deleting_originals(shared_source, monkeypatch, limit):
    repo, source, shared = shared_source
    original = (shared.directory / "source.tar").read_bytes()
    monkeypatch.setattr(native, "SOURCE_ENTRIES" if limit == "entries" else "SOURCE_BYTES", 2)
    with pytest.raises(private.ContractError, match="bound exceeded"):
        shared.check(repo, source)
    assert (shared.directory / "source.tar").read_bytes() == original
    assert (shared.context / "core/public.py").read_bytes() == b"public = True\n"


def test_source_scan_detects_same_file_mutation_while_reading(shared_source, monkeypatch):
    _repo, _source, shared = shared_source
    target = shared.context / "core/public.py"
    identity = target.stat().st_ino
    original_read = os.read
    changed = []
    def read(fd, count):
        value = original_read(fd, count)
        if value and os.fstat(fd).st_ino == identity and not changed:
            changed.append(True)
            target.write_bytes(b"public = False\n")
        return value
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(private.ContractError, match="changed during read"):
        native._source_tree(shared.fds[2])
    assert changed == [True] and target.read_bytes() == b"public = False\n"


def test_shared_source_requires_private_direct_parent_before_archive(public_repository, tmp_path, monkeypatch):
    repo, source = public_repository
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    calls = []
    monkeypatch.setattr(native, "archive_command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(private.ContractError, match="parent must be private"):
        native.SharedPublicSource(repo, parent / "s", source)
    assert calls == [] and list(parent.iterdir()) == []
    parent.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    with pytest.raises(private.ContractError, match="parent must be private"):
        native.SharedPublicSource(repo, alias / "s", source)
    assert calls == [] and list(parent.iterdir()) == []


def test_failed_build_with_valid_context_retains_error_and_checked_after_record(shared_source, tmp_path, monkeypatch):
    repo, source, shared = shared_source
    builds, inspected = fake_candidate_docker(monkeypatch)
    original_archive = native.archive_command
    error = RuntimeError("exact synthetic Docker error")
    def archive(args, path, **kwargs):
        original_archive(args, path, **kwargs)
        raise error
    monkeypatch.setattr(native, "archive_command", archive)
    workspace = tmp_path / "n1"
    workspace.mkdir(mode=0o700)
    with pytest.raises(RuntimeError) as raised:
        native.build_candidate(repo, workspace, source, "synthetic:1", synthetic_contract(1), "c" * 64, shared_source=shared)
    assert raised.value is error and error.__cause__ is None
    assert len(builds) == 1 and inspected == []
    assert json.loads((workspace / "shared-source-after.json").read_bytes()) == shared.reference
    assert (workspace / "application-build.stderr").read_bytes() == b"synthetic build stderr\n"


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_shared_source_rejects_outer_parent_replacement_without_changing_original_tree(public_repository, tmp_path, replacement):
    repo, source = public_repository
    parent = tmp_path / "workspace"
    parent.mkdir(mode=0o700)
    shared = native.SharedPublicSource(repo, parent / "s", source)
    original = native._source_tree(shared.fds[2])
    retained = tmp_path / "retained-workspace"
    parent.rename(retained)
    if replacement == "symlink":
        parent.symlink_to(retained, target_is_directory=True)
    else:
        parent.mkdir(mode=0o700)
    try:
        with pytest.raises(private.ContractError, match="parent"):
            shared.check(repo, source)
        assert native._source_tree(shared.fds[2]) == original
        assert (retained / "s/source.tar").is_file()
    finally:
        shared.close()


def test_tree_entry_bound_stops_streaming_scandir_before_consuming_remaining_entries(shared_source, monkeypatch):
    _repo, _source, shared = shared_source
    original = os.scandir
    consumed = []
    class BoundedEntries:
        def __init__(self, fd):
            self.iterator = original(fd)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.iterator.close()
        def __iter__(self):
            return self
        def __next__(self):
            assert len(consumed) < 2, "scanner consumed entries beyond its first over-bound entry"
            entry = next(self.iterator)
            consumed.append(entry.name)
            return entry
    monkeypatch.setattr(os, "scandir", BoundedEntries)
    monkeypatch.setattr(native, "SOURCE_ENTRIES", 2)
    with pytest.raises(private.ContractError, match="entry bound"):
        native._source_tree(shared.fds[2])
    assert len(consumed) == 2
    assert (shared.context / "core/public.py").read_bytes() == b"public = True\n"


@pytest.mark.parametrize("depth", [0, 1])
def test_tree_count_bound_includes_root_and_nested_directories(tmp_path, monkeypatch, depth):
    assert native.SOURCE_ENTRIES == 4096
    root = tmp_path / "tree"
    root.mkdir(mode=0o700)
    leaf = root
    if depth:
        leaf = root / "child"
        leaf.mkdir(mode=0o700)
    for name in ("one", "two"):
        (leaf / name).write_bytes(b"retained public bytes")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        original = native._source_tree(fd)
        assert len(original) == 3 + depth
        monkeypatch.setattr(native, "SOURCE_ENTRIES", len(original))
        assert native._source_tree(fd) == original
        monkeypatch.setattr(native, "SOURCE_ENTRIES", len(original) - 1)
        with pytest.raises(private.ContractError, match="entry bound"):
            native._source_tree(fd)
        assert all((leaf / name).read_bytes() == b"retained public bytes" for name in ("one", "two"))
    finally:
        os.close(fd)
