"""Offline native wiring tests; these do not substitute for actual Docker/PG."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import production_sender_bootstrap_native as native
from services import admission_fence as gate


def _fallback_arguments(tmp_path):
    return ["--repo", str(tmp_path / "not-the-source"), "--source", str(tmp_path / "upstream"),
            "--binary", str(tmp_path / "binary"), "--output", str(tmp_path / "output"),
            "--build-evidence", str(tmp_path / "build"), "--image-id", "sha256:" + "a" * 64]


def test_real_bootstrap_fallback_exit3_retains_only_public_locations(tmp_path):
    result = subprocess.run([sys.executable, "-I", "-B", native.__file__, *_fallback_arguments(tmp_path)],
                            capture_output=True, timeout=20, check=False)
    assert result.returncode == 3 and not result.stderr
    value = json.loads(result.stdout)
    assert set(value) == {"status", "error_type", "error_sha256", "safe_export_complete", "locations"}
    assert value["status"] == "unconfirmed" and value["safe_export_complete"] is False
    assert value["error_type"] == "AdmissionError" and len(value["error_sha256"]) == 64
    assert 0 < len(value["locations"]) <= 8
    assert all(set(frame) == {"file", "line", "function"} and "/" not in frame["file"] for frame in value["locations"])
    assert any(frame["file"] == "production_sender_bootstrap_native.py" and frame["function"] == "execute" for frame in value["locations"])
    assert str(tmp_path).encode() not in result.stdout and not list(tmp_path.iterdir())


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_fallback_message_and_args_stay_private_even_if_locator_fails(tmp_path, monkeypatch, capsys, diagnostic_failure):
    from scripts import verify_production_startup_native as startup
    monkeypatch.setattr(native.sys, "argv", ["native", *_fallback_arguments(tmp_path)])
    def fail(*_args, **_kwargs):
        raise RuntimeError("SECRET_MESSAGE_AND_ARGS")
    monkeypatch.setattr(native, "execute", fail)
    if diagnostic_failure:
        monkeypatch.setattr(startup, "public_exception_locations", fail)
    assert native.main() == 3
    captured = capsys.readouterr()
    value = json.loads(captured.out)
    assert not captured.err and "SECRET_" not in captured.out and str(tmp_path) not in captured.out
    assert value["error_type"] == "RuntimeError"
    assert value["error_sha256"] == hashlib.sha256(b"SECRET_MESSAGE_AND_ARGS").hexdigest()
    if diagnostic_failure:
        assert value["locations"] == []
    else:
        assert len(value["locations"]) == 1
        assert value["locations"][0]["file"] == "production_sender_bootstrap_native.py"
        assert value["locations"][0]["function"] == "main" and type(value["locations"][0]["line"]) is int


def test_actual_fallback_archive_emits_diagnostic_but_still_fails(tmp_path, capsys):
    from scripts import verify_production_startup_native as startup
    from scripts import private_release_contract as private
    path = tmp_path / "archived"
    with pytest.raises(private.ContractError, match="synthetic command failed; original evidence retained"):
        startup.archive_command([sys.executable, "-I", "-B", native.__file__, *_fallback_arguments(tmp_path)], path)
    captured = capsys.readouterr()
    record = json.loads(captured.err)
    assert not captured.out and len(captured.err.encode()) <= 4096
    assert record["execution_proof"] is False and record["exception_type"] == "AdmissionError"
    assert record["status"] == "locations_available"
    assert any(frame["function"] == "execute" for frame in record["locations"])
    original = json.loads(Path(str(path) + ".stdout").read_bytes())
    assert original["status"] == "unconfirmed" and original["safe_export_complete"] is False
    assert original["locations"] == record["locations"]
    assert str(tmp_path) not in captured.err and not Path(str(path) + ".stderr").read_bytes()


def test_nonroot_rejected_before_source_or_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(native.os, "geteuid", lambda: 1001)
    with pytest.raises(gate.AdmissionError, match="Linux root"):
        native.execute(tmp_path, tmp_path, "sha256:" + "a" * 64, tmp_path / "binary", tmp_path / "out", tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def records(tmp_path):
    root = tmp_path.resolve() / "build"
    root.mkdir(mode=0o700)
    revision, image = "a" * 40, "sha256:" + "b" * 64
    original = {"BUILD.log": b"go version go1.26.4 linux/amd64\n+ go build -p 1 -trimpath ...\n",
        "IMAGE_BUILD.log": b"actual synthetic image build output\n",
        "rawimage.json": json.dumps([{"Id": image, "Config": {"Labels": {"org.opencontainers.image.revision": revision}}}]).encode()}
    for name, raw in original.items():
        native._save(root / name, raw, 0o400)
    return SimpleNamespace(root=root, revision=revision, image=image, original=original)


def test_build_package_preserves_actual_bytes_hash_and_image(records):
    p = records
    value = native.build_records(p.root, p.image, p.revision)
    assert value["revision"] == p.revision and value["image_id"] == p.image
    for name, raw in p.original.items():
        assert value["files"][name] == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                                        "raw_base64": base64.b64encode(raw).decode()}
        assert (p.root / name).read_bytes() == raw


@pytest.mark.parametrize("change", ["wrong_image", "wrong_revision", "wrong_go", "missing", "symlink", "hardlink", "mode", "oversize"])
def test_unknown_build_is_not_fabricated(records, change):
    p = records
    path = p.root / "BUILD.log"
    if change == "wrong_go":
        path.chmod(0o600)
        path.write_bytes(b"go version go1.26.5 linux/amd64")
        path.chmod(0o400)
    elif change == "missing":
        path.unlink()
    elif change == "symlink":
        path.rename(p.root / "original")
        path.symlink_to(p.root / "original")
    elif change == "hardlink":
        os.link(path, p.root / "another-name")
    elif change == "mode":
        path.chmod(0o600)
    elif change == "oversize":
        path.chmod(0o600)
        with path.open("wb") as stream:
            stream.truncate(1024**2 + 1)
        path.chmod(0o400)
    with pytest.raises((gate.AdmissionError, OSError)):
        native.build_records(p.root, "sha256:" + "c" * 64 if change == "wrong_image" else p.image,
                             "c" * 40 if change == "wrong_revision" else p.revision)


def test_file_cron_command_does_not_touch_system_cron(tmp_path):
    root = tmp_path.resolve()
    current = root / "current"
    original, stopped = b"# synthetic\n* * * * * /synthetic/refresh\n", b"# stopped original\n"
    current.write_bytes(original)
    script = root / "crontab-fixture"
    script.write_bytes(native.cron_command(current))
    after = root / "after"
    after.write_bytes(stopped)
    assert subprocess.check_output([sys.executable, "-I", "-B", str(script), "-u", "root", "-l"], timeout=3) == original
    subprocess.run([sys.executable, "-I", "-B", str(script), "-u", "root", str(after)], check=True, timeout=3)
    assert current.read_bytes() == stopped and after.read_bytes() == stopped
    assert set(path.name for path in root.iterdir()) == {"current", "crontab-fixture", "after"}


def test_seed_uses_only_exact_locked_original_sql(tmp_path):
    repo, source = tmp_path / "repo", tmp_path / "source"
    (repo / "ops/miniflux").mkdir(parents=True)
    (source / "internal/database").mkdir(parents=True)
    path = source / "internal/database/webhook_outbox.go"
    raw = b'func migration(){ _, err := tx.Exec(`CREATE TABLE original(x int);`) }'
    path.write_bytes(raw)
    lock = repo / "ops/miniflux/pause-source-lock.json"
    lock.write_text(json.dumps({"patched_files_sha256": {"internal/database/webhook_outbox.go": hashlib.sha256(raw).hexdigest()}}))
    assert native._sql132(repo, source) == ("CREATE TABLE original(x int);", hashlib.sha256(raw).hexdigest())
    path.write_bytes(raw.replace(b"original", b"unreviewed"))
    with pytest.raises(gate.AdmissionError, match="source hash"):
        native._sql132(repo, source)


@pytest.fixture
def seed_case(tmp_path):
    repo, source, output = tmp_path / "repo", tmp_path / "upstream", tmp_path / "evidence"
    (repo / "ops/miniflux").mkdir(parents=True)
    (source / "internal/database").mkdir(parents=True)
    output.mkdir(mode=0o700)
    (repo / "ops/miniflux/Dockerfile").write_bytes((native.ROOT / "ops/miniflux/Dockerfile").read_bytes())
    sql = "CREATE TABLE synthetic_exact132(x int);"
    raw = ('func migration(){ _, err := tx.Exec(`' + sql + '`) }').encode()
    (source / "internal/database/webhook_outbox.go").write_bytes(raw)
    (repo / "ops/miniflux/pause-source-lock.json").write_text(json.dumps({"patched_files_sha256": {
        "internal/database/webhook_outbox.go": hashlib.sha256(raw).hexdigest()}}))
    reference = native._base_image(repo)
    image = {"Id": "sha256:" + "a" * 64, "Architecture": "amd64", "Os": "linux", "RepoDigests": [reference],
             "Config": {"Entrypoint": None, "Cmd": ["/usr/bin/miniflux"]}}
    case = SimpleNamespace(repo=repo, source=source, output=output, image=image, reference=reference, sql=sql, events=[], failure=None)
    def command(args, **options):
        case.events.append(("command", args, options))
        if args[0] == "pull":
            if case.failure == "pull":
                raise RuntimeError("synthetic pull failure")
            return "fixed dependency prepared"
        if args[:2] == ["image", "inspect"]:
            if case.failure == "inspect":
                raise RuntimeError("synthetic inspect failure")
            return json.dumps([image])
        if args[0] == "wait":
            return "0"
        assert args == ["inspect", "owned-seed131"]
        return json.dumps([{"Id": "b" * 64}])
    versions = iter(("131", "132"))
    def query(statement):
        case.events.append(("sql", statement))
        return next(versions) if statement == "SELECT version FROM schema_version;" else ""
    def launch(*args):
        case.events.append(("launch", args))
    case.harness = SimpleNamespace(repo=repo, source=source, output=output, password="synthetic", names={"seed131": "owned-seed131"},
                                   command=command, sql=query, launch=launch)
    return case


def test_seed_prepares_actual_from_digest_once_then_checks_image_before_exact132_transaction(seed_case):
    p = seed_case
    native.seed132(p.harness)
    assert p.events[:2] == [("command", ["pull", "--platform=linux/amd64", p.reference], {}),
                            ("command", ["image", "inspect", p.reference], {})]
    assert p.events[2][0] == "launch" and p.events[2][1][0:2] == ("seed131", p.image["Id"])
    assert p.events[2][1][-1] == ["/usr/bin/miniflux", "-migrate"]
    assert [event[1] for event in p.events if event[0] == "sql"] == ["SELECT version FROM schema_version;",
        "BEGIN; SET LOCAL statement_timeout='3000ms'; " + p.sql
        + " UPDATE schema_version SET version='132' WHERE version='131'; COMMIT;", "SELECT version FROM schema_version;"]
    assert len([e for e in p.events if e[:1] == ("command",) and e[1][0] == "pull"]) == 1
    assert native.MAX_SECONDS == 180  # No final/timeout override bypasses command's shared deadline.
    record = json.loads((p.output / "SEED132.json").read_bytes())
    assert record["upstream_reference"] == p.reference and record["upstream_image"] == p.image["Id"]
    assert record["prior_schema"] == 131 and record["schema"] == 132 and record["marker_and_sql_same_transaction"] is True
    assert record["production_compatibility_proven"] is False
    assert record["upstream_entrypoint"] is None and record["upstream_cmd"] == ["/usr/bin/miniflux"]


@pytest.mark.parametrize("config", [None, {}, {"Cmd": ["/usr/bin/miniflux"]},
    {"Entrypoint": [], "Cmd": ["/usr/bin/miniflux"]},
    {"Entrypoint": ["/usr/bin/miniflux"], "Cmd": ["/usr/bin/miniflux"]},
    {"Entrypoint": None, "Cmd": ["/unreviewed/binary"]},
    {"Entrypoint": None, "Cmd": ["/usr/bin/miniflux", "-migrate"]}])
def test_seed_rejects_changed_or_missing_actual_executable_config_before_launch(seed_case, config):
    p = seed_case
    p.image["Config"] = config
    with pytest.raises(gate.AdmissionError, match="executable configuration"):
        native.seed132(p.harness)
    assert [event[0] for event in p.events] == ["command", "command"]
    assert not list(p.output.iterdir())


@pytest.mark.parametrize("failure", ["pull", "inspect", "architecture", "os", "digest", "image_id"])
def test_seed_dependency_failure_has_no_retry_launch_or_migration(seed_case, failure):
    p = seed_case
    p.failure = failure
    if failure == "architecture":
        p.image["Architecture"] = "arm64"
    elif failure == "os":
        p.image["Os"] = "other"
    elif failure == "digest":
        p.image["RepoDigests"] = ["miniflux/miniflux@sha256:" + "f" * 64]
    elif failure == "image_id":
        p.image["Id"] = "truncated"
    with pytest.raises((RuntimeError, ValueError)):
        native.seed132(p.harness)
    expected = [("command", ["pull", "--platform=linux/amd64", p.reference], {})]
    if failure != "pull":
        expected.append(("command", ["image", "inspect", p.reference], {}))
    assert p.events == expected and not list(p.output.iterdir())


@pytest.mark.parametrize("source", ["FROM miniflux/miniflux:latest\n", "FROM miniflux/miniflux@sha256:abc\n", ""])
def test_seed_refuses_unfixed_source_before_dependency_command(seed_case, source):
    p = seed_case
    (p.repo / "ops/miniflux/Dockerfile").write_text(source)
    with pytest.raises(gate.AdmissionError, match="fixed upstream seed image"):
        native.seed132(p.harness)
    assert not p.events and not list(p.output.iterdir())


def test_bootstrap_plan_consumes_real_build_and_no_dummy_mount(records, monkeypatch):
    p = records
    directory = p.root.parent / "inputs"
    directory.mkdir(mode=0o700)
    network = {"Name": "owned-internal", "Id": "c" * 64, "Driver": "bridge", "Scope": "local",
               "Internal": True, "Options": {}, "IPAM": {}}
    pg = {"NetworkSettings": {"Networks": {network["Name"]: {"IPAddress": "172.31.0.2"}}}}
    harness = SimpleNamespace(owner="d" * 32, password="synthetic", token="e" * 64, image_id=p.image,
        network=network["Name"], names={"pg": "owned-pg"}, repo=native.ROOT)
    harness.command = lambda args: json.dumps([network if args[0] == "network" else pg])
    monkeypatch.setattr(native, "postgres_before", lambda dsn: {"schema_version": 132, "dsn_hash": hashlib.sha256(dsn.encode()).hexdigest()})
    binding = {"revision": p.revision, "upstream_revision": "f" * 40, "binary_sha256": "a" * 64}
    actual = native.build_records(p.root, p.image, p.revision)
    plan = native.prepare_bootstrap(harness, directory, {"operation_id": "b" * 64, "plan_sha256": "c" * 64}, binding, actual)
    assert set(plan) == native.bootstrap.PLAN_KEYS
    assert json.loads(native.bootstrap.private.private_file(Path(plan["build_evidence"]["path"]), sealed=True)[0]) == actual
    spec = json.loads(Path(plan["compose"]["path"]).read_text())["services"][plan["service"]]
    assert spec["volumes"] == [] and spec["ports"] == [] and spec["read_only"] is True
    assert spec["mem_limit"] == spec["memswap_limit"] == 768 * 1024**2 and spec["pids_limit"] == 256
    assert "CREATE_ADMIN=0\n" in Path(plan["environment"]["path"]).read_text()
    assert plan["postgres_before"]["schema_version"] == 132 and plan["window"]["expires_at"] - plan["window"]["not_before"] == 121


def test_repeat_action_refusal_is_not_a_success_fallback():
    assert native.rejected_original(lambda: (_ for _ in ()).throw(gate.AdmissionError("already attempted"))) is True
    with pytest.raises(AssertionError, match="accepted"):
        native.rejected_original(lambda: {"okay": True})
    with pytest.raises(OSError):
        native.rejected_original(lambda: (_ for _ in ()).throw(OSError("unknown")))


def test_safe_export_selects_only_public_proof_and_keeps_root_originals(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    native._save(root / "RESULT.json", {"status": "unconfirmed"})
    native._save(root / "PROOF.json", {"business_http_status": 503, "namespace_transport": True})
    native._save(root / "secret.json", {"token": "not-for-export"})
    before = {p.name: (p.read_bytes(), export_metadata(p)) for p in root.iterdir()}
    packet = native.safe_export(root)
    assert packet["complete"] is True and packet["originals_modified"] is False
    assert set(packet["files"]) == {"RESULT.json", "PROOF.json"}
    assert "not-for-export" not in json.dumps(packet)
    for name, value in before.items():
        assert ((root / name).read_bytes(), export_metadata(root / name)) == value


def export_metadata(path):
    return native.export._metadata(path.stat())


def failed_command(monkeypatch, tmp_path, *, rc=125, stderr=None):
    root = tmp_path.resolve()
    root.chmod(0o700)
    harness = object.__new__(native.Harness)
    harness.output, harness.sequence, harness.started = root, 0, time.monotonic()
    harness.phase_deadline, harness.env = None, {}
    harness.password, harness.token, harness.auth = "SYNTHETIC_PASSWORD", "SYNTHETIC_TOKEN", "SYNTHETIC_AUTH"
    calls = []
    stderr = stderr if stderr is not None else ('docker: Error response from daemon: failed to create task: '
        'OCI runtime create failed: exec: "-migrate": executable file not found in $PATH: unknown.\n')
    def command(args, **options):
        calls.append((args, options))
        if rc is None:
            raise subprocess.TimeoutExpired(args, 0.1, output="PRIVATE_STDOUT", stderr=stderr)
        return subprocess.CompletedProcess(args, rc, "PRIVATE_STDOUT", stderr)
    monkeypatch.setattr(native.subprocess, "run", command)
    previous = os.umask(0o077)
    try:
        with pytest.raises(RuntimeError) as caught:
            harness.command(["run", "--env", "DATABASE_URL=postgres://PRIVATE_USER:PRIVATE_PASS@private-host/db",
                             "--mount", "type=bind,src=/private/SECRET_PATH,dst=/app/data", "sha256:" + "a" * 64], timeout=0.1)
    finally:
        os.umask(previous)
    reference = native._command_reference(harness, caught.value)
    assert reference == {"command_sequence": 1} and len(calls) == 1
    failure = {"type": "RuntimeError", **reference}
    native._save(root / "FAILURE.json", failure)
    native._save(root / "RESULT.json", {"status": "unconfirmed", "failure": failure, "errors": []})
    return harness, caught.value, calls


@pytest.mark.parametrize("rc,category", [(125, "container_executable_not_found"), (None, "result_unknown"), (-15, "container_executable_not_found")])
def test_actual_command_failure_reference_exports_only_bounded_derivative(monkeypatch, tmp_path, rc, category):
    harness, error, calls = failed_command(monkeypatch, tmp_path, rc=rc)
    before = {p.name: (p.read_bytes(), export_metadata(p)) for p in tmp_path.iterdir()}
    packet = native.safe_export(tmp_path.resolve())
    assert packet["complete"] is True and packet["originals_modified"] is False
    summary, = packet["command_failures"]
    assert summary["command_sequence"] == 1 and summary["operation"] == "run"
    assert summary["rc"] == rc and summary["error_class"] == category and summary["execution_proof"] is False
    assert summary["original_record_sha256"] == hashlib.sha256(before["command-0001.json"][0]).hexdigest()
    original = json.loads(before["command-0001.json"][0])
    assert summary["retained_stderr_sha256"] == hashlib.sha256(original["stderr"].encode()).hexdigest()
    assert "PRIVATE_" not in json.dumps(packet) and "SECRET_PATH" not in json.dumps(packet)
    assert "/private/" not in json.dumps(packet) and "postgres://" not in json.dumps(packet)
    assert len(calls) == 1 and type(error) is RuntimeError and harness.sequence == 1
    assert all((p.read_bytes(), export_metadata(p)) == before[p.name] for p in tmp_path.iterdir())


def test_spoofed_exception_and_other_harness_cannot_reference_command(monkeypatch, tmp_path):
    harness, error, _ = failed_command(monkeypatch, tmp_path)
    assert native._command_reference(SimpleNamespace(sequence=1, output=tmp_path), error) == {}
    try:
        raise RuntimeError("isolated command 1 failed rc=125")
    except RuntimeError as spoof:
        assert native._command_reference(harness, spoof) == {}


@pytest.mark.parametrize("export_fails", [False, True])
def test_main_command_diagnostic_never_turns_original_failure_into_success(monkeypatch, tmp_path, capsys, export_fails):
    failed_command(monkeypatch, tmp_path)
    original = (tmp_path / "command-0001.json").read_bytes()
    if export_fails:
        os.link(tmp_path / "command-0001.json", tmp_path / "private-alias")
    arguments = _fallback_arguments(tmp_path)
    arguments[arguments.index("--output") + 1] = str(tmp_path)
    monkeypatch.setattr(native.sys, "argv", ["native", *arguments])
    monkeypatch.setattr(native, "execute", lambda *_args: {"status": "unconfirmed"})
    assert native.main() == 3
    captured = capsys.readouterr()
    assert not captured.err and "PRIVATE_" not in captured.out and "/private/" not in captured.out
    packet = json.loads(captured.out)
    assert packet["status"] == "unconfirmed"
    if export_fails:
        assert packet["safe_export_complete"] is False
    else:
        assert packet["safe_evidence"]["command_failures"][0]["execution_proof"] is False
    assert (tmp_path / "command-0001.json").read_bytes() == original


@pytest.mark.parametrize("change", ["symlink", "hardlink", "mode", "oversize", "zero_rc", "bool_rc", "nan_elapsed", "duplicate", "bad_prefix"])
def test_unsafe_or_ambiguous_retained_command_fails_export_without_following(monkeypatch, tmp_path, change):
    failed_command(monkeypatch, tmp_path)
    path = tmp_path / "command-0001.json"
    raw = path.read_bytes()
    if change == "symlink":
        path.rename(tmp_path / "private-original")
        path.symlink_to(tmp_path / "private-original")
    elif change == "hardlink":
        os.link(path, tmp_path / "private-alias")
    elif change == "mode":
        path.chmod(0o644)
    elif change == "oversize":
        with path.open("wb") as stream:
            stream.truncate(native.export.EXPORT_FILE_BYTES + 1)
    elif change == "duplicate":
        path.write_bytes(raw.rstrip()[:-1] + b',"rc":0}')
    else:
        value = json.loads(raw)
        if change == "bad_prefix":
            value["argv"][0] = "PRIVATE_ARBITRARY_COMMAND"
        elif change == "nan_elapsed":
            value["elapsed_seconds"] = float("nan")
        else:
            value["rc"] = 0 if change == "zero_rc" else True
        path.write_text(json.dumps(value))
    with pytest.raises((gate.AdmissionError, OSError)):
        native.safe_export(tmp_path.resolve())
    assert path.exists() and (tmp_path / "RESULT.json").exists()


def test_command_diagnostics_share_existing_input_budget_and_bound_count(monkeypatch, tmp_path):
    failed_command(monkeypatch, tmp_path)
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    with monkeypatch.context() as scope:
        scope.setattr(native.export, "EXPORT_INPUT_BYTES", total - 1)
        with pytest.raises(gate.AdmissionError, match="byte budget"):
            native.safe_export(tmp_path.resolve())
    result = tmp_path / "RESULT.json"
    result.write_text(json.dumps({"status": "unconfirmed", "errors": [{"command_sequence": n} for n in range(1, 10)]}))
    with pytest.raises(gate.AdmissionError, match="count bound"):
        native.safe_export(tmp_path.resolve())


@pytest.mark.parametrize("stderr", ["PRIVATE_MESSAGE exec: \"-migrate\": executable file not found in $PATH",
    "PRIVATE_TOKEN\nCannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    "docker: Error response from daemon: PRIVATE_MESSAGE /private/SECRET_PATH https://user:SECRET@host/",
    "docker: Error response from daemon: " + "x" * 65536])
def test_unrecognized_sensitive_stderr_stays_hash_only(monkeypatch, tmp_path, stderr):
    failed_command(monkeypatch, tmp_path, stderr=stderr)
    summary, = native.safe_export(tmp_path.resolve())["command_failures"]
    assert summary["error_class"] == "unclassified" and summary["execution_proof"] is False
    assert "PRIVATE_" not in json.dumps(summary) and "SECRET" not in json.dumps(summary)
    assert "/private/" not in json.dumps(summary) and "https://" not in json.dumps(summary)


@pytest.mark.parametrize("change", ["symlink", "hardlink", "mode", "oversize", "missing_result"])
def test_safe_export_fails_closed_on_ambiguous_original(tmp_path, change):
    root = tmp_path.resolve()
    root.chmod(0o700)
    path = root / "RESULT.json"
    native._save(path, {"status": "passed"})
    if change == "symlink":
        path.rename(root / "outside")
        path.symlink_to(root / "outside")
    elif change == "hardlink":
        os.link(path, root / "alias")
    elif change == "mode":
        path.chmod(0o644)
    elif change == "missing_result":
        path.unlink()
    else:
        with path.open("wb") as stream:
            stream.truncate(1024**2 + 1)
    with pytest.raises((gate.AdmissionError, OSError)):
        native.safe_export(root)


def test_ready_uses_real_namespace_class_and_only_get(monkeypatch):
    calls = []
    plan = {"control_url": "http://127.0.0.1:8787", "token_file": "/private/synthetic-token"}
    monkeypatch.setattr(native.bootstrap, "_postgres", lambda plan, **options: {"schema_version": 135})
    class Transport:
        def __init__(self, url, token, *, container_id):
            calls.append((url, token, container_id))
        def request(self, method, route):
            calls.append((method, route))
            return 200, {"database_schema": 135}
    monkeypatch.setattr(native.bootstrap, "NativeSenderActivationTransport", Transport)
    assert native._ready(plan, {"container_id": "a" * 64}) == {"schema_version": 135}
    assert calls[-1] == ("GET", "/activation-registration")
    assert calls[0][-1] == "a" * 64


def test_actual_helper_never_replaces_legacy_capability_or_production_driver():
    # Guard the integration entrypoint: transport doubles are confined to this
    # offline test module; the hosted helper itself uses the real capabilities.
    source = Path(native.__file__).read_text()
    assert "with legacy.locked_takeover(path, old) as cap:" in source
    assert "bootstrap.initialize(cap, plan, evidence)" in source
    assert "LegacyTakeover.__new__" not in source and "monkeypatch" not in source
    assert "--signal\", \"TERM" in source and '"rm"' not in source and '"prune"' not in source
    assert native.bootstrap.NativeSenderActivationTransport.__module__ == "production_sender_driver"


def test_failed_owned_log_capture_does_not_skip_term_only_cleanup(monkeypatch, tmp_path):
    row = {"Id": "a" * 64, "State": {"Running": True}, "Config": {"Labels": {"ai-filter.synthetic-miniflux-pause": "b" * 32}}}
    calls = []
    def command(args, **options):
        calls.append((args, options))
        if args[0] == "inspect":
            return json.dumps([row])
        if args[0] == "logs":
            raise TimeoutError("synthetic logs unavailable")
        return row["Id"] if args[0] == "kill" else "0"
    harness = SimpleNamespace(owner="b" * 32, names={"pg": "owned-pg"}, output=tmp_path,
                              command=command, safe_inspect=lambda value: value)
    assert native.preserve_owned(harness, "pg") == [{"stage": "pg:observation", "type": "TimeoutError"}]
    assert [item[0][0] for item in calls] == ["inspect", "logs", "kill", "wait"]
    assert calls[-2][0] == ["kill", "--signal", "TERM", row["Id"]]
    assert calls[-1][1]["timeout"] == 8


@pytest.mark.parametrize("status,leases,accepted", [("503", "0", True), ("401", "0", False), ("200", "0", False), ("503", "1", False)])
def test_closed_request_uses_gate_before_login_not_anonymous_api_auth(status, leases, accepted):
    commands, queries = [], []
    harness = SimpleNamespace(network="synthetic", names={"fixture": "owned-fixture"})
    harness.command = lambda args: commands.append(args) or status
    harness.sql = lambda sql: queries.append(sql) or leases
    actual = {"NetworkSettings": {"Networks": {"synthetic": {"IPAddress": "172.31.0.8"}}}}
    if accepted:
        assert native.closed_http(harness, actual)["business_leases_after"] == 0
        assert queries == ["SELECT count(*) FROM handoff_leases;"]
    else:
        with pytest.raises(gate.AdmissionError):
            native.closed_http(harness, actual)
    program = commands[0][-1]
    assert ":8080/login" in program and "method='POST'" in program and "username=bad&password=bad" in program
    assert "/v1/" not in program


def test_old_synthetic_process_has_explicit_term_handler_and_exits_without_kill():
    import signal
    import selectors
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", native.OLD_PROCESS],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Readiness in real native is /proc/1/status SigCgt; this portable test
        # only verifies the exact synthetic program's explicit normal handler.
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=3)
            assert process.stdout.readline() == b"synthetic_TERM_ready\n"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == 0
        assert "SigCgt" in native.TERM_READY and "signal.SIGTERM-1" in native.TERM_READY
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()


def test_legacy_mounts_same_retained_receiver_data_only_for_application_roles(tmp_path, monkeypatch):
    from test_production_legacy_takeover import container
    monkeypatch.setattr(native.legacy, "ROOT_UID", os.geteuid())  # Only the offline fixture owner.
    root = tmp_path.resolve()
    root.chmod(0o700)
    launches = {}
    harness = SimpleNamespace(owner="a" * 32, python_id="sha256:" + "b" * 64, names={})
    def launch(role, image, options, command):
        launches[role] = (image, options, command)
        harness.names[role] = role
    harness.launch = launch
    harness.wait = lambda check, **_options: check()
    def command(argv):
        if argv[0] == "exec":
            return "1"
        role = argv[1].removeprefix("old-")
        return json.dumps([container(list(native.legacy.ROLES).index(role) + 1, role)])
    harness.command = command
    record = native.legacy._record
    def binaries(path, **options):
        if str(path) in {"/usr/bin/flock", "/usr/bin/docker"}:
            return {"path": str(path), "sha256": "c" * 64}, b"synthetic binary double"
        return record(path, **options)
    monkeypatch.setattr(native.legacy, "_record", binaries)
    monkeypatch.setattr(native.legacy, "_process", lambda pid: {"pid": pid})
    monkeypatch.setattr(native.legacy.NativeTransport, "host_boot", lambda _self: "synthetic-boot")
    data = root / "original-data"
    data.mkdir(mode=0o700)
    native.prepare_legacy(harness, root, SimpleNamespace(pid=123), data)
    wanted = "type=bind,src=" + str(data) + ",dst=/app/data"
    assert wanted in launches["old-api"][1] and wanted in launches["old-worker"][1]
    assert "--mount" not in launches["old-sender"][1]
    assert wanted not in launches["old-gateway"][1]
    assert all(item[2][-1] == native.OLD_PROCESS for item in launches.values())
