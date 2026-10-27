"""Real synthetic SQLite/slot flock; Docker identities are unit doubles here."""
from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from test_production_migration import prepared  # noqa: F401
from services import admission_fence as gate, controlled_startup as isolation
from services import production_migration as migration, production_startup as runtime
from services import production_startup_control as control


@pytest.fixture
def started(prepared, monkeypatch):  # noqa: F811
    p = prepared
    root, state = p["directory"] / "runtime", p["directory"] / "slots"
    root.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    process = {"pid": 100}
    def identity(pid=None):
        pid = process["pid"] if pid is None else pid
        return {"pid": pid, "ppid": 0 if pid == 1 else 1, "start_ticks": 42, "boot_id": gate._boot_identity(),
                "pid_namespace": 100, "command_sha256": "a" * 64, "uid": os.geteuid()}
    monkeypatch.setattr(runtime, "process_identity", identity)
    original_dispatch = runtime.dispatch
    def dispatch(*args):
        previous = process["pid"]
        process["pid"] = 1
        try:
            return original_dispatch(*args)
        finally:
            process["pid"] = previous
    monkeypatch.setattr(runtime, "dispatch", dispatch)
    monkeypatch.setattr(runtime, "_container_hostname", lambda: "a" * 12)
    monkeypatch.setenv(runtime.ENV_OPERATION, "9" * 64)
    monkeypatch.setenv(runtime.ENV_ROOT, str(root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_CONFIG_PATH", p["permit"]["config"]["path"])
    monkeypatch.setenv("AI_FILTER_DB_PATH", str(p["db"]))
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(p["fence"].root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", gate._encoded(p["permit"]["freeze"]["binding"]).decode())
    monkeypatch.delenv(isolation.ENV_OPERATION, raising=False)
    with migration.locked_session(p["root"], p["permit"]) as session:
        session.authorize()
        session.migrate()
        descriptor = control.authorize(session, root, operation_id="9" * 64,
            paths={"database": str(p["db"]), "config": p["permit"]["config"]["path"],
                   "freeze": str(p["fence"].root), "state": str(state)}, web_workers=4)
        for slot in descriptor["slots"]:
            control._write_grant(root, state, descriptor, slot, 0, "a" * 64, gate.secrets.token_hex(32), None)
        try:
            yield {**p, "session": session, "runtime_root": root, "state": state, "descriptor": descriptor, "process": process}
        finally:
            runtime._after_fork()


def test_current_migration_closes_real_initializer_and_claims_independent_slots(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    results = []
    for number, slot in enumerate(("web-1", "web-2", "web-3", "web-4")):
        runtime._after_fork()
        p["process"]["pid"] = 100 + number
        runtime.dispatch(p["runtime_root"], p["descriptor"], slot, 0)
        result = isolation.initialize_runtime_database("web", lambda: pytest.fail("initializer ran"), "test", slot=slot)
        assert result["slot"] == slot and result["status"] == "closed_initialized"
        assert result["business_open"] is False
        results.append(result)
    assert len({value["owner_nonce"] for value in results}) == 4
    assert len({value["owner"]["pid"] for value in results}) == 4
    runtime._after_fork()
    p["process"]["pid"] = 1
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    result = isolation.initialize_runtime_database("worker", lambda: pytest.fail("initializer ran"), "test")
    assert result["slot"] == "worker-1"


@pytest.mark.parametrize("field", ["journal", "schema", "config", "inode", "frozen", "runtime", "operation"])
def test_runtime_refuses_drift_without_reinitialization(started, monkeypatch, field):
    p = started
    descriptor = deepcopy(p["descriptor"])
    if field == "journal":
        p["anchor"].execute(f"DELETE FROM {migration.JOURNAL} WHERE step_number=13")
    elif field == "schema":
        p["anchor"].execute("CREATE TABLE unexpected(value TEXT)")
    elif field == "config":
        descriptor["config"]["sha256"] = "0" * 64
    elif field == "inode":
        descriptor["database"]["identity"]["inode"] += 1
    elif field == "frozen":
        descriptor["freeze"]["generation"] += 1
    elif field == "runtime":
        descriptor["runtime_sources_sha256"] = "0" * 64
    else:
        monkeypatch.setenv(runtime.ENV_OPERATION, "0" * 64)
    gate._write(p["runtime_root"] / "descriptor.json", descriptor)
    with pytest.raises((gate.AdmissionError, RuntimeError)):
        runtime.load()


def test_dispatch_unknown_and_dead_owner_are_never_reused(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    runtime.dispatch(p["runtime_root"], p["descriptor"], "web-1", 0)
    with pytest.raises(gate.AdmissionError, match="dispatched"):
        runtime.dispatch(p["runtime_root"], p["descriptor"], "web-1", 0)
    original = runtime.claim("web", slot="web-1")
    receipt, live = control._receipt(p["state"], p["descriptor"], "web-1", 0)
    assert receipt == original and live
    runtime._after_fork()
    assert not control._receipt(p["state"], p["descriptor"], "web-1", 0)[1]
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        runtime.claim("web", slot="web-1")
    assert gate._read(p["state"] / "web-1.0.json") == original


def test_gunicorn_no_implicit_respawn_preserves_healthy_slots(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    spawned = []
    server = SimpleNamespace(cfg=SimpleNamespace(workers=4, threads=16, preload_app=False))
    def spawn():
        worker = SimpleNamespace()
        runtime.pre_fork(server, worker)
        spawned.append(worker.production_slot)
        runtime._after_fork()
        p["process"]["pid"] = 100 + len(spawned)
        runtime.claim("web", slot=worker.production_slot[0], attempt=worker.production_slot[1])
        return len(spawned)
    server.spawn_worker = spawn
    runtime.install_gunicorn(server)
    for _ in range(4):
        server.spawn_worker()
    assert len(spawned) == 4
    originals = [gate._read(p["state"] / f"web-{i}.0.json") for i in range(1, 5)]
    for _ in range(5):
        assert server.spawn_worker() is None
    assert [gate._read(p["state"] / f"web-{i}.0.json") for i in range(1, 5)] == originals
    assert server.cfg.workers == 4


def test_replacement_requires_dead_owner_and_exact_original_then_only_once(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    runtime.dispatch(p["runtime_root"], p["descriptor"], "web-1", 0)
    old = runtime.claim("web", slot="web-1")
    with pytest.raises(gate.AdmissionError, match="unresolved"):
        control.replace(p["session"], p["runtime_root"], p["state"], slot="web-1", operation_id="d" * 64,
                        previous_receipt_sha256=gate._hash(old), container_id="a" * 64)
    runtime._after_fork()
    actual = {"RestartCount": 0, "Image": p["descriptor"]["source"]["image_id"], "State": {"Running": True}}
    monkeypatch.setattr(control, "_container", lambda _id: actual)
    monkeypatch.setattr(control, "verify_container", lambda *a, **kw: actual)
    monkeypatch.setattr(control, "_actual_process", lambda *a: old["owner"])
    with pytest.raises(gate.AdmissionError, match="remains alive"):
        control.replace(p["session"], p["runtime_root"], p["state"], slot="web-1", operation_id="d" * 64,
                        previous_receipt_sha256=gate._hash(old), container_id="a" * 64)
    monkeypatch.setattr(control, "_actual_process", lambda *a: None)
    replacement = control.replace(p["session"], p["runtime_root"], p["state"], slot="web-1", operation_id="d" * 64,
                                  previous_receipt_sha256=gate._hash(old), container_id="a" * 64)
    runtime.dispatch(p["runtime_root"], p["descriptor"], "web-1", 1)
    p["process"]["pid"] = 101
    result = runtime.claim("web", slot="web-1", attempt=1)
    assert result["process_nonce"] == replacement["process_nonce"] and result["owner_nonce"] != old["owner_nonce"]
    assert gate._read(p["state"] / "web-1.0.json") == old
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        control.replace(p["session"], p["runtime_root"], p["state"], slot="web-1", operation_id="e" * 64,
                        previous_receipt_sha256=gate._hash(old), container_id="a" * 64)


def test_worker_entry_returns_closed_without_starting_business(started, monkeypatch):
    import worker_main
    started["process"]["pid"] = 1
    from routes import shared
    from services import shared_cache
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": "synthetic"})
    monkeypatch.setattr(shared_cache, "redis_ping", lambda: pytest.fail("closed worker touched Redis"))
    monkeypatch.setattr(worker_main, "_bootstrap_dashboard_publication", lambda: pytest.fail("closed worker bootstrapped"))
    monkeypatch.setattr(worker_main, "_retry_pending_dashboard_publication", lambda: pytest.fail("closed worker retried"))
    monkeypatch.setattr(runtime, "closed_worker_loop", lambda: 0)
    assert worker_main.main() == 0


def test_live_session_rechecks_new_inventory_without_acquiring_same_locks(started):
    p = started
    assert p["session"].recheck_inventory()["storage_drained"]
    (p["directory"] / "spool" / "unknown.json").write_text("{}")
    with pytest.raises(Exception):
        p["session"].recheck_inventory()
    # Synthetic corruption retained for the test; session exit checks identity,
    # not a new zero proof. No action succeeds after failed inventory checking.


@pytest.mark.parametrize("field", ["AI_FILTER_ADMISSION_ROOT", "AI_FILTER_ADMISSION_BINDING", "AI_FILTER_DB_PATH"])
def test_actual_environment_cannot_point_away_from_frozen_descriptor(started, monkeypatch, field):
    value = "{}" if field.endswith("BINDING") else "/synthetic/other-open-resource"
    monkeypatch.setenv(field, value)
    with pytest.raises(gate.AdmissionError, match="environment differs"):
        runtime.load()


def test_same_process_cannot_consume_multiple_web_slots(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    for slot in ("web-1", "web-2"):
        runtime.dispatch(p["runtime_root"], p["descriptor"], slot, 0)
    runtime.claim("web", slot="web-1")
    with pytest.raises(gate.AdmissionError, match="multiple startup slots"):
        runtime.claim("web", slot="web-2")


def test_unrelated_exec_process_cannot_claim_dispatched_web_slot(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    runtime.dispatch(p["runtime_root"], p["descriptor"], "web-1", 0)
    original = runtime.process_identity
    monkeypatch.setattr(runtime, "process_identity", lambda pid=None: {**original(pid), "ppid": 88} if pid is None else original(pid))
    with pytest.raises(gate.AdmissionError, match="dispatched container child"):
        runtime.claim("web", slot="web-1")
    assert not (p["state"] / "web-1.0.intent.json").exists()


def test_other_open_fence_cannot_substitute_for_descriptor_frozen_fence(started, monkeypatch):
    import time
    p = started
    other = p["directory"] / "other-open-fence"
    binding = {**p["permit"]["freeze"]["binding"], "deployment_nonce": "e" * 32}
    gate.provision(other, binding, {"not_before": int(time.time()) - 1, "expires_at": int(time.time()) + 120,
                                    "max_admissions": 5, "max_bytes": 4096})
    fence = gate.Fence(other, binding)
    fence.open_trial("f" * 64, 0, "synthetic other open fence")
    assert gate._read(other / "state.json")["phase"] == "open"
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(other))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", gate._encoded(binding).decode())
    from flask import Flask
    from services.admission_http import install_http_admission
    app = Flask("synthetic-open-fence-substitution")
    app.add_url_rule("/protected", "protected", lambda: "synthetic handler reached")
    install_http_admission(app)
    assert app.test_client().get("/protected").status_code == 200
    with pytest.raises(gate.AdmissionError, match="environment differs"):
        runtime.load()


@pytest.mark.parametrize("alias", ["same", "state_ancestor", "state_descendant", "other_rw_ancestor", "config_ancestor"])
def test_readonly_grants_and_config_cannot_have_writable_alias(tmp_path, alias):
    root, state, config = tmp_path / "grants", tmp_path / "slots", tmp_path / "sealed" / "runtime.yaml"
    source = state
    if alias == "same":
        state = root
    elif alias == "state_ancestor":
        state = tmp_path
    elif alias == "state_descendant":
        state = root / "nested"
    elif alias == "other_rw_ancestor":
        source = tmp_path
    else:
        source = config.parent
    record = {"Mounts": [{"Type": "bind", "Source": str(source), "RW": True}]}
    with pytest.raises(gate.AdmissionError):
        control._protect_readonly_inputs(record, root, state, config)


def test_separate_readonly_grants_and_config_with_writable_state_are_supported(tmp_path):
    record = {"Mounts": [{"Type": "bind", "Source": str(tmp_path / "slots"), "RW": True}]}
    control._protect_readonly_inputs(record, tmp_path / "grants", tmp_path / "slots", tmp_path / "sealed/runtime.yaml")



def test_expired_original_slot_query_is_readonly_and_cannot_grant_again(started, monkeypatch):
    p = started
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    runtime.dispatch(p["runtime_root"], p["descriptor"], "web-1", 0)
    receipt = runtime.claim("web", slot="web-1")
    runtime._after_fork()
    monkeypatch.setattr(control, "_container", lambda _: {"Id": "a" * 64, "State": {
        "Running": False, "Status": "exited", "StartedAt": "synthetic-start"}, "RestartCount": 0})
    monkeypatch.setattr(runtime.time, "time", lambda: p["permit"]["expires_at"] + 1)
    result = control.query(p["session"], p["runtime_root"], p["state"], slot="web-1", attempt=0,
                           operation_id=receipt["attempt_operation_id"])
    assert result["status"] == "startup_uncertain" and not result["owner_lease_live"]
    assert not result["business_open"]
    with pytest.raises(gate.AdmissionError, match="window"):
        control.grant(p["session"], p["runtime_root"], p["state"], slot="web-2", container_id="a" * 64,
                      attempt_operation_id="f" * 64)
    assert gate._read(p["state"] / "web-1.0.json") == receipt


@pytest.mark.parametrize("suffix", ["wal", "shm"])
def test_missing_sidecar_refuses_before_sqlite_can_create_it(started, monkeypatch, suffix):
    p = started
    sidecar = p["db"].with_name(p["db"].name + "-" + suffix)
    retained = sidecar.with_name(sidecar.name + ".retained-original")
    sidecar.rename(retained)
    monkeypatch.setattr(runtime.sqlite3, "connect", lambda *a, **kw: pytest.fail("SQLite connected without retained sidecars"))
    try:
        with pytest.raises(gate.AdmissionError, match="sidecar missing"):
            runtime._database(p["descriptor"])
        assert not sidecar.exists()
    finally:
        retained.rename(sidecar)
