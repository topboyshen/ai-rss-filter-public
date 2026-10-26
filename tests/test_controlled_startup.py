"""Real SQLite and file/flock startup, never production/model/remote access."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from services import admission_fence as gate
from services import controlled_startup as startup


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    directory = tmp_path / "synthetic-startup"
    directory.mkdir(mode=0o700)
    database_path = directory / "synthetic.db"
    from db import database
    monkeypatch.setattr(database, "DB_PATH", str(database_path))
    monkeypatch.setenv("AI_FILTER_DB_PATH", str(database_path))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("network prohibited"))
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64, "strategy": "synthetic",
               "source_sha256": gate.source_sha256(), "deployment_nonce": "c" * 32}
    now = int(time.time())
    root = directory / "fence"
    gate.provision(root, binding, {"not_before": now - 1, "expires_at": now + 600,
                                   "max_admissions": 10, "max_bytes": 10000})
    fence = gate.Fence(root, binding, lock_seconds=0.05)
    permit = {"schema": 1, "scope": "isolation", "run_id": "d" * 32, "operation_id": "e" * 64,
              "manifest_sha256": gate._hash(fence._manifest()), "binding": binding,
              "runtime_sources_sha256": startup.runtime_sources_sha256(), "db_path": str(database_path),
              "roles": {"web": "1" * 64, "worker": "2" * 64}, "not_before": now - 1, "expires_at": now + 600}
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", json.dumps(binding))
    monkeypatch.delenv(startup.ENV_OPERATION, raising=False)
    yield fence, permit, database
    for key in list(startup._OWNERS):
        if key[0] == str(root):
            fd, _ = startup._OWNERS.pop(key)
            os.close(fd)


def initialized(prepared):
    fence, permit, database = prepared
    startup.authorize(fence, permit)
    receipt = startup.migrate(fence, permit["operation_id"])
    return fence, permit, database, receipt


def test_actual_database_init_once_then_roles_start_but_business_stays_frozen(prepared, monkeypatch):
    fence, permit, database, receipt = initialized(prepared)
    import app
    assert receipt["durable"] and not receipt["business_open"]
    with sqlite3.connect(permit["db_path"]) as conn:
        assert conn.execute("SELECT v FROM app_meta WHERE k='schema_version'").fetchone() == ("7",)
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone() == (0,)
    monkeypatch.setattr(database, "init_db", lambda: pytest.fail("initializer repeated"))
    assert startup.query_migration(fence, permit["operation_id"]) == receipt
    with pytest.raises(gate.AdmissionClosed):
        startup.migrate(fence, permit["operation_id"])
    monkeypatch.setenv(startup.ENV_OPERATION, permit["operation_id"])
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(app, "_init_db", lambda: pytest.fail("API initialized twice"))
    app.initialize_app()
    app.init_db()
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    startup.initialize_runtime_database("worker", lambda: pytest.fail("worker initialized twice"), "worker_database_initialize")
    assert app.app.test_client().get("/unrecognised-producer").status_code == 503
    assert fence.snapshot()["phase"] == "frozen" and fence.snapshot()["intake_count"] == 0
    with pytest.raises(gate.AdmissionClosed):
        with gate.admission("still_closed"):
            pytest.fail("business opened")
    assert fence.open_trial("3" * 64, 0, "separate explicit opening")["phase"] == "open"


@pytest.mark.parametrize("kind", ["wall-backward", "elapsed-expired"])
def test_startup_authorization_window_cannot_reset_after_clock_change(prepared, monkeypatch, kind):
    fence, permit, _, _ = initialized(prepared)
    authority = gate._read(fence.root / "startup/authorization.json")
    if kind == "wall-backward":
        monkeypatch.setattr(startup.time, "time", lambda: authority["clock"]["wall"] - 0.5)
    else:
        monkeypatch.setattr(gate, "_elapsed", lambda: authority["clock"]["elapsed"] + 7201)
    with pytest.raises(gate.AdmissionClosed):
        startup.claim_role(fence, permit["operation_id"], "web")
    assert startup.query_migration(fence, permit["operation_id"])["durable"]
    assert gate._read(fence.root / "startup/authorization.json") == authority


def test_controlled_runtime_role_mismatch_never_consumes_role(prepared, monkeypatch):
    fence, permit, _, _ = initialized(prepared)
    monkeypatch.setenv(startup.ENV_OPERATION, permit["operation_id"])
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    with pytest.raises(gate.AdmissionClosed, match="runtime role"):
        startup.initialize_runtime_database("web", lambda: pytest.fail("called"), "http_database_initialize")
    assert not (fence.root / "startup/web.intent.json").exists()


def test_cli_real_sqlite_roundtrip_and_query_without_reexecution(prepared):
    fence, permit, _ = prepared
    directory = Path(permit["db_path"]).parent
    binding_path, permit_path = directory / "binding.json", directory / "permit.json"
    gate._write(binding_path, fence.binding)
    gate._write(permit_path, permit)
    command = [sys.executable, str(startup.PROJECT / "scripts/run_controlled_startup.py"),
               "--scope", "isolation", "--root", str(fence.root),
               "--binding", str(binding_path), "--permit", str(permit_path)]
    outputs = []
    for action in ("authorize", "migrate", "query"):
        result = subprocess.run([*command[:2], action, *command[2:]], cwd=startup.PROJECT,
                                capture_output=True, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(json.loads(result.stdout))
    assert outputs[1]["result"] == outputs[2]["result"]
    assert not outputs[2]["business_open"] and not outputs[2]["runtime_inspect_verified"]
    assert fence.snapshot()["phase"] == "frozen"

@pytest.mark.parametrize("field,value", [("scope", "production"), ("schema", True), ("run_id", "x"),
                                         ("operation_id", "x"), ("manifest_sha256", "0" * 64),
                                         ("runtime_sources_sha256", "0" * 64), ("expires_at", 1)])
def test_authority_wrong_bindings_fail_before_any_database_write(prepared, field, value):
    fence, permit, _ = prepared
    with pytest.raises(gate.AdmissionClosed):
        startup.authorize(fence, {**permit, field: value})
    assert not Path(permit["db_path"]).exists()
    assert not list((fence.root / "startup").iterdir())


def test_initialized_role_is_not_a_generic_lease_or_callback_permission(prepared):
    fence, permit, _, _ = initialized(prepared)
    with pytest.raises(gate.AdmissionClosed):
        startup.claim_role(fence, permit["operation_id"], "../intake")
    with pytest.raises(gate.AdmissionClosed):
        with fence.frozen_startup():
            with gate.admission("nested_ordinary"):
                pytest.fail("bypass")
    assert not list((fence.root / "leases").iterdir())


def test_unknown_migration_result_never_reruns_even_after_restart(prepared, monkeypatch):
    fence, permit, database = prepared
    startup.authorize(fence, permit)
    real, calls = database.init_db, []
    def committed_then_failed():
        calls.append(True)
        real()
        raise RuntimeError("synthetic crash after some DB commits")
    monkeypatch.setattr(database, "init_db", committed_then_failed)
    with pytest.raises(RuntimeError):
        startup.migrate(fence, permit["operation_id"])
    new = gate.Fence(fence.root, fence.binding)
    with pytest.raises(gate.AdmissionClosed):
        startup.query_migration(new, permit["operation_id"])
    with pytest.raises(gate.AdmissionClosed):
        startup.migrate(new, permit["operation_id"])
    with pytest.raises(gate.AdmissionClosed):
        new.open_trial("3" * 64, 0, "must remain closed")
    assert calls == [True] and Path(permit["db_path"]).exists()


@pytest.mark.parametrize("cutpoint", ["intent_sync", "marker_ack", "receipt_sync", "receipt_ack"])
def test_migration_confirmation_failures_are_preserved_and_only_queried(prepared, monkeypatch, cutpoint):
    fence, permit, database = prepared
    startup.authorize(fence, permit)
    writes, marker, real, calls = gate._write, startup._mark_database, database.init_db, []
    def initialize():
        calls.append(True)
        real()
    monkeypatch.setattr(database, "init_db", initialize)
    def fail_write(path, value):
        writes(path, value)
        if ((cutpoint == "intent_sync" and path.name == "migration.intent.json")
                or (cutpoint == "receipt_ack" and path.name == "migration.json")):
            raise OSError("lost strict confirmation")
    def fail_marker(value):
        marker(value)
        raise OSError("lost SQLite marker acknowledgement")
    with monkeypatch.context() as patch:
        patch.setattr(gate, "_write", fail_write)
        if cutpoint == "marker_ack":
            patch.setattr(startup, "_mark_database", fail_marker)
        if cutpoint == "receipt_sync":
            sync = gate._sync_directory
            def fail_sync(path):
                if path.name == "startup" and (path / "migration.json").exists():
                    raise OSError("directory fsync EIO")
                sync(path)
            patch.setattr(gate, "_sync_directory", fail_sync)
        with pytest.raises(OSError):
            startup.migrate(fence, permit["operation_id"])
    original = (fence.root / "startup/migration.intent.json").read_bytes()
    with pytest.raises(gate.AdmissionClosed):
        startup.migrate(fence, permit["operation_id"])
    if cutpoint == "intent_sync":
        with pytest.raises(gate.AdmissionClosed):
            startup.query_migration(fence, permit["operation_id"])
        assert calls == []
    else:
        assert startup.query_migration(fence, permit["operation_id"])["durable"]
        assert calls == [True]
    assert (fence.root / "startup/migration.intent.json").read_bytes() == original


def test_actual_two_threads_cannot_run_migration_twice(prepared, monkeypatch):
    fence, permit, database = prepared
    startup.authorize(fence, permit)
    real, entered, release, calls = database.init_db, threading.Event(), threading.Event(), []
    def slow():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        real()
    monkeypatch.setattr(database, "init_db", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(startup.migrate, fence, permit["operation_id"])
        try:
            assert entered.wait(5)
            with pytest.raises(gate.AdmissionBusy):
                startup.migrate(gate.Fence(fence.root, fence.binding, lock_seconds=0.01), permit["operation_id"])
            with pytest.raises(gate.AdmissionBusy):
                fence.open_trial("3" * 64, 0, "no concurrent open")
        finally:
            release.set()
        assert future.result(timeout=10)["durable"]
    assert calls == [True]


def test_db_schema_change_cannot_authorize_reinit(prepared):
    fence, permit, _, _ = initialized(prepared)
    with sqlite3.connect(permit["db_path"]) as conn:
        conn.execute("CREATE TABLE unexpected(value)")
    with pytest.raises(gate.AdmissionClosed):
        startup.query_migration(fence, permit["operation_id"])
    with pytest.raises(gate.AdmissionClosed):
        startup.migrate(fence, permit["operation_id"])


def test_role_lost_ack_exact_query_and_different_process_restart_fail_closed(prepared):
    fence, permit, _, _ = initialized(prepared)
    receipt = startup.claim_role(fence, permit["operation_id"], "web")
    assert startup.claim_role(fence, permit["operation_id"], "web") == receipt
    assert startup.query_role(fence, permit["operation_id"], "web")["owner_lease_live"]
    program = """import json, os
from services import admission_fence as g, controlled_startup as s
f=g.Fence(os.environ['AI_FILTER_ADMISSION_ROOT'],json.loads(os.environ['AI_FILTER_ADMISSION_BINDING']))
try: s.claim_role(f,os.environ['SYNTHETIC_OPERATION'],'web')
except g.AdmissionClosed: print('rejected')
else: raise AssertionError('concurrent/restarted process reused role permission')
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=startup.PROJECT,
                            env={**os.environ, "SYNTHETIC_OPERATION": permit["operation_id"]},
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rejected"
    fd, _ = startup._OWNERS.pop((str(fence.root), permit["operation_id"], "web", os.getpid()))
    os.close(fd)
    assert startup.query_role(fence, permit["operation_id"], "web")["status"] == "startup_uncertain"
    with pytest.raises(gate.AdmissionClosed):
        startup.claim_role(fence, permit["operation_id"], "web")


def test_role_durability_failure_never_reports_ready_or_reuses_permission(prepared, monkeypatch):
    fence, permit, _, _ = initialized(prepared)
    real = gate._sync_directory
    def broken(path):
        if (path / "web.json").exists():
            raise OSError("role receipt directory sync failed")
        real(path)
    with monkeypatch.context() as patch:
        patch.setattr(gate, "_sync_directory", broken)
        with pytest.raises(OSError):
            startup.claim_role(fence, permit["operation_id"], "web")
    assert startup.query_role(fence, permit["operation_id"], "web")["status"] == "startup_uncertain"
    with pytest.raises(gate.AdmissionClosed):
        startup.claim_role(fence, permit["operation_id"], "web")
    assert (fence.root / "startup/web.intent.json").exists()


def test_production_cli_rejected_before_reading_any_input(tmp_path):
    from argparse import Namespace
    from scripts.run_controlled_startup import execute
    with pytest.raises(gate.AdmissionClosed, match="production startup"):
        execute(Namespace(scope="production", root=tmp_path, binding=tmp_path / "missing", permit=tmp_path / "missing"))


def run_worker_frozen(monkeypatch):
    """Exercise real worker main; only external Redis/threads are synthetic."""
    from types import SimpleNamespace
    import worker_main
    import routes.shared as shared
    import routes.webhook_routes as webhook
    import routes.dashboard_routes as dashboard
    import services.shared_cache as cache
    import services.worker_health as health
    import services.quant_market_context as quant

    class StopImmediately:
        def wait(self, _timeout):
            return True
        def set(self):
            return None
    live = SimpleNamespace(is_alive=lambda: True)
    observations = []
    monkeypatch.setattr(shared, "init_db", lambda: pytest.fail("worker reran initializer"))
    monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": "synthetic-key"})
    monkeypatch.setattr(cache, "process_role", lambda: "worker")
    monkeypatch.setattr(cache, "redis_enabled", lambda: True)
    monkeypatch.setattr(cache, "redis_ping", lambda: True)
    monkeypatch.setattr(worker_main, "threading", SimpleNamespace(Event=StopImmediately))
    monkeypatch.setattr(worker_main.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(dashboard, "_ensure_quotes_refresher", lambda: live)
    monkeypatch.setattr(dashboard, "stop_quotes_refresher", lambda **_k: None)
    monkeypatch.setattr(webhook, "start_webhook_dispatcher", lambda: live)
    monkeypatch.setattr(webhook, "stop_webhook_dispatcher", lambda **_k: True)
    monkeypatch.setattr(webhook, "webhook_dispatcher_alive", lambda: True)
    monkeypatch.setattr(quant, "start_quant_market_context_poller", lambda: live)
    monkeypatch.setattr(quant, "stop_quant_market_context_poller", lambda **_k: None)
    monkeypatch.setattr(quant, "quant_context_enabled", lambda: False)
    monkeypatch.setattr(health, "write_worker_health", lambda *_a, **_k: observations.append("health-only"))
    monkeypatch.setattr(health, "close_worker_health_resources", lambda: None)
    assert worker_main.main() == 0
    assert observations == ["health-only", "health-only"]
    with pytest.raises(gate.AdmissionClosed):
        worker_main._bootstrap_dashboard_publication()


def test_actual_worker_main_uses_startup_permission_without_business_bypass(prepared, monkeypatch):
    fence, permit, _, _ = initialized(prepared)
    monkeypatch.setenv(startup.ENV_OPERATION, permit["operation_id"])
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    run_worker_frozen(monkeypatch)
    assert startup.query_role(fence, permit["operation_id"], "worker")["receipt"]["health_verified"] is False
    assert fence.snapshot()["phase"] == "frozen"
    assert fence.snapshot()["intake_count"] == 0
    assert not list((fence.root / "leases").iterdir())


@pytest.mark.parametrize("completed_marker", [True, False])
def test_real_process_abrupt_exit_recovers_marker_only_never_reexecutes(prepared, completed_marker):
    fence, permit, _ = prepared
    startup.authorize(fence, permit)
    target = "_result" if completed_marker else "_mark_database"
    program = f"""import json, os
from services import admission_fence as g, controlled_startup as s
f=g.Fence(os.environ['AI_FILTER_ADMISSION_ROOT'],json.loads(os.environ['AI_FILTER_ADMISSION_BINDING']))
s.{target}=lambda *args: os._exit(137)
s.migrate(f,os.environ['SYNTHETIC_OPERATION'])
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=startup.PROJECT,
                            env={**os.environ, "SYNTHETIC_OPERATION": permit["operation_id"]},
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 137, result.stdout + result.stderr
    assert (fence.root / "startup/migration.intent.json").exists()
    assert not (fence.root / "startup/migration.json").exists()
    restarted = gate.Fence(fence.root, fence.binding)
    if completed_marker:
        assert startup.query_migration(restarted, permit["operation_id"])["durable"]
    else:
        with pytest.raises(gate.AdmissionClosed):
            startup.query_migration(restarted, permit["operation_id"])
    with pytest.raises(gate.AdmissionClosed):
        startup.migrate(restarted, permit["operation_id"])
    assert restarted.snapshot()["phase"] == "frozen"


def test_database_replacement_with_identical_schema_and_marker_is_rejected(prepared):
    fence, permit, _, _ = initialized(prepared)
    path = Path(permit["db_path"])
    replacement = path.with_name("replacement.db")
    with sqlite3.connect(path) as source, sqlite3.connect(replacement) as target:
        source.backup(target)
    replacement.chmod(0o600)
    os.replace(replacement, path)
    with pytest.raises(gate.AdmissionClosed, match="marker/schema differs"):
        startup.query_migration(fence, permit["operation_id"])


@pytest.mark.parametrize("damage", ["partial", "conflict", "boolean-schema", "permission"])
def test_partial_or_damaged_intent_is_retained_and_never_reexecuted(prepared, damage):
    fence, permit, database = prepared
    startup.authorize(fence, permit)
    path = fence.root / "startup/migration.intent.json"
    intent = startup._intent(permit)
    if damage == "partial":
        path = path.with_name(path.name + ".pending")
        path.write_bytes(b"{")
        path.chmod(0o600)
    elif damage == "conflict":
        gate._write(path, {**intent, "run_id": "0" * 32})
    elif damage == "boolean-schema":
        gate._write(path, {**intent, "schema": True})
    else:
        gate._write(path, intent)
        path.chmod(0o644)
    original = path.read_bytes()
    with pytest.raises((gate.AdmissionClosed, ValueError, FileNotFoundError)):
        startup.query_migration(fence, permit["operation_id"])
    with pytest.raises(gate.AdmissionClosed):
        startup.migrate(fence, permit["operation_id"])
    assert path.read_bytes() == original
    assert not Path(database.DB_PATH).exists()


def test_expired_permit_allows_exact_query_not_new_role_or_open(prepared):
    fence, permit, _, _ = initialized(prepared)
    # Change only time, never alter persisted authority to obtain a new window.
    from unittest.mock import patch
    with patch.object(startup.time, "time", return_value=permit["expires_at"] + 1):
        assert startup.query_migration(fence, permit["operation_id"])["durable"]
        with pytest.raises(gate.AdmissionClosed):
            startup.claim_role(fence, permit["operation_id"], "web")
        with pytest.raises(gate.AdmissionClosed):
            fence.open_trial("3" * 64, 0, "expired")


def test_ordinary_schema_data_updates_do_not_change_migration_identity(prepared):
    fence, permit, _, receipt = initialized(prepared)
    with sqlite3.connect(permit["db_path"]) as conn:
        conn.execute("INSERT INTO app_meta(k,v) VALUES('synthetic-observation','not-migration')")
    assert startup.query_migration(fence, permit["operation_id"]) == receipt


