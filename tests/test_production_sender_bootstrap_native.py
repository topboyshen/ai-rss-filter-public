"""Offline native wiring tests; these do not substitute for actual Docker/PG."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import production_sender_bootstrap_native as native
from services import admission_fence as gate


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
