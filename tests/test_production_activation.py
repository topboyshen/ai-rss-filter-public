"""Real synthetic SQLite/files/flocks; Docker/process identities are unit doubles."""
from copy import deepcopy
import math
import os
import threading
import time

import pytest

from test_production_migration import prepared  # noqa: F401
from services import admission_fence as gate, controlled_startup as isolation
from services import production_activation as runtime, production_activation_control as control
from services import production_migration as migration, production_startup as startup
from services import production_startup_control as slots


@pytest.fixture
def activation_case(prepared, monkeypatch):  # noqa: F811
    p = prepared
    root, state = p["directory"] / "runtime", p["directory"] / "slots"
    root.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    process = {"pid": 1}
    def identity(pid=None):
        pid = process["pid"] if pid is None else pid
        return {"pid": pid, "ppid": 0 if pid == 1 else 1, "start_ticks": pid + 42,
                "boot_id": gate._boot_identity(), "pid_namespace": 100,
                "command_sha256": "a" * 64, "uid": os.geteuid()}
    monkeypatch.setattr(startup, "process_identity", identity)
    monkeypatch.setattr(startup, "_container_hostname", lambda: ("a" if os.environ.get("AI_FILTER_ROLE") == "web" else "b") * 12)
    for key, value in {startup.ENV_OPERATION: "9" * 64, startup.ENV_ROOT: str(root),
                       "AI_FILTER_ADMISSION_MODE": "enforced", "AI_FILTER_CONFIG_PATH": p["permit"]["config"]["path"],
                       "AI_FILTER_DB_PATH": str(p["db"]), "AI_FILTER_ADMISSION_ROOT": str(p["fence"].root),
                       "AI_FILTER_ADMISSION_BINDING": gate._encoded(p["permit"]["freeze"]["binding"]).decode()}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(isolation.ENV_OPERATION, raising=False)
    monkeypatch.setattr(slots, "verify_container", lambda *a, **kw: slots._container(a[4]))
    monkeypatch.setattr(slots, "_container", lambda cid: {"Id": cid, "State": {"Running": True, "StartedAt": "synthetic"}, "RestartCount": 0})
    monkeypatch.setattr(slots, "_actual_process", lambda _cid, value: value)
    owners, background = {}, []
    try:
        with migration.locked_session(p["root"], p["permit"]) as session:
            session.authorize()
            session.migrate()
            descriptor = slots.authorize(session, root, operation_id="9" * 64, web_workers=1,
                paths={"database": str(p["db"]), "config": p["permit"]["config"]["path"],
                       "freeze": str(p["fence"].root), "state": str(state)})
            for slot, role in descriptor["slots"].items():
                container = ("a" if role == "web" else "b") * 64
                slots._write_grant(root, state, descriptor, slot, 0, container, gate.secrets.token_hex(32), None)
                monkeypatch.setenv("AI_FILTER_ROLE", role)
                process["pid"] = 1
                if role == "web":
                    startup.dispatch(root, descriptor, slot, 0)
                    process["pid"] = 100
                startup.claim(role, slot=slot)
                owners[slot] = dict(startup._OWNERS)
                startup._OWNERS.clear()  # keep both real flock FDs in this synthetic process
            armed = control.arm(session, root, state, operation_id="b" * 64)
        # The sender's integer policy starts at ceil(wall); honor it rather
        # than manufacturing an earlier clock in these fast unit scenarios.
        time.sleep(max(0, math.ceil(descriptor["clock"]["wall"]) - time.time()))
        yield {**p, "runtime_root": root, "state": state, "descriptor": descriptor, "arm": armed,
               "process": process, "owners": owners, "monkeypatch": monkeypatch, "background": background}
    finally:
        for stop, threads in background:
            stop.set()
            for thread in threads:
                thread.join(1)
        startup._OWNERS.clear()
        for values in owners.values():
            for fd, _receipt in values.values():
                os.close(fd)


def prepare_runtime(p):
    stop, threads, active = threading.Event(), [], {}
    p["background"].append((stop, threads))
    for slot, values in p["owners"].items():
        startup._OWNERS.clear()
        startup._OWNERS.update(values)
        p["process"]["pid"] = 1 if slot == "worker-1" else 100
        p["monkeypatch"].setenv("AI_FILTER_ROLE", "worker" if slot == "worker-1" else "web")
        handle = runtime.RuntimeActivation()
        services = {}
        if slot == "worker-1":
            for name in ("quotes", "dispatcher"):
                thread = threading.Thread(target=stop.wait, daemon=True)
                thread.start()
                services[name] = thread
                threads.append(thread)
            services["quant_context"] = None
        handle.prepared(services, {"redis": True} if services else {})
        active[slot] = handle
    return active


def locked(p):
    return control.locked_activation(p["root"], p["runtime_root"], p["state"], operation_id="b" * 64)


def test_same_slots_prepare_confirm_resume_without_initializer_or_grant_replacement(activation_case):
    p = activation_case
    old = {slot: gate._read(p["state"] / f"{slot}.0.json") for slot in p["descriptor"]["slots"]}
    with locked(p) as session:
        assert session.query()["status"] == "activation_pending"
        session.prepare()
        handles = prepare_runtime(p)
        assert session.query()["status"] == "business_prepared"
        assert p["fence"].snapshot()["phase"] == "frozen"
        session.confirm()
        opened = session.resume()
        assert opened["phase"] == "open" and opened["action"] == "resume"
        assert session.storage is None and len(session.shadow.fds) == 3
        proof = session.check_sender_resume()
        assert set(proof["window"]) == {"not_before", "expires_at"}
        assert session.verification_proof()["business_open"] is True
        assert handles["worker-1"].opened()
        handles["worker-1"].pulse()
        lease = p["fence"].reserve_lease("synthetic_new_intake")
        with lease.activate():
            pass
        lease.finish()
    assert all(gate._read(p["state"] / f"{slot}.0.json") == value for slot, value in old.items())


@pytest.mark.parametrize("stage", ["prepare", "confirm", "resume"])
def test_unknown_or_repeated_operation_never_restarts_subsystems(activation_case, stage):
    p = activation_case
    with locked(p) as session:
        session.prepare()
        if stage == "prepare":
            with pytest.raises(gate.AdmissionError, match="already attempted"):
                session.prepare()
            return
        prepare_runtime(p)
        session.confirm()
        if stage == "confirm":
            with pytest.raises(gate.AdmissionError, match="already attempted"):
                session.confirm()
        else:
            session.resume()
            with pytest.raises(gate.AdmissionError, match="query original"):
                session.resume()


def test_forged_readiness_and_dead_subsystem_are_not_confirmation(activation_case):
    p = activation_case
    with locked(p) as session:
        session.prepare()
        with pytest.raises(gate.AdmissionError, match="not ready"):
            session.confirm()


        handles = prepare_runtime(p)
        stop, threads = p["background"][-1]
        stop.set()
        for thread in threads:
            thread.join(1)
        with pytest.raises(gate.AdmissionError, match="automatic restart forbidden"):
            handles["worker-1"].pulse()
        handles["worker-1"].fail(RuntimeError("synthetic failure"))
        assert session.query()["status"] == "activation_uncertain"
        with pytest.raises(gate.AdmissionError, match="not ready"):
            session.confirm()


def test_plain_open_trial_and_untyped_resume_cannot_bypass_capability(activation_case):
    fence = activation_case["fence"]
    with pytest.raises(gate.AdmissionError, match="separate reviewed"):
        fence.open_trial("e" * 64, 3, "not production capability")
    with pytest.raises(gate.AdmissionError, match="live production"):
        fence.resume_production({"ready": True})


@pytest.mark.parametrize("field", ["owner_nonce", "ready_sha256", "elapsed"])
def test_stale_or_changed_runtime_pulse_refuses_sender_resume(activation_case, field):
    p = activation_case
    with locked(p) as session:
        session.prepare()
        prepare_runtime(p)
        files = runtime.paths(p["descriptor"], "worker-1", 0)
        pulse = deepcopy(gate._read(files["pulse"]))
        pulse[field] = 0 if field == "elapsed" else "0" * 64
        gate._write(files["pulse"], pulse)
        with pytest.raises(gate.AdmissionError):
            session.confirm()


@pytest.mark.parametrize("cut", ["intent", "opening_state", "final_state", "receipt"])
def test_lost_resume_ack_only_queries_same_operation_in_fresh_session(activation_case, monkeypatch, cut):
    p = activation_case
    original = gate._write
    def interrupted(path, value):
        original(path, value)
        name = path.name
        selected = ((cut == "intent" and name == "b" * 64 + ".intent.json")
                    or (cut == "opening_state" and name == "state.json" and value.get("pending_operation") == "b" * 64)
                    or (cut == "final_state" and name == "state.json" and value.get("phase") == "open" and not value.get("pending_operation"))
                    or (cut == "receipt" and name == "b" * 64 + ".json"))
        if selected:
            raise OSError("synthetic lost ACK")
    with pytest.raises(OSError, match="lost ACK"):
        with locked(p) as session:
            session.prepare()
            handles = prepare_runtime(p)
            session.confirm()
            monkeypatch.setattr(gate, "_write", interrupted)
            session.resume()
    monkeypatch.setattr(gate, "_write", original)
    # During a two-file resume commit, an exact pending intent is closed
    # progress; it must not poison a healthy retained owner.
    handles["worker-1"].pulse()
    with locked(p) as recovered:
        with pytest.raises(gate.AdmissionError, match="query original"):
            recovered.resume()
        receipt = recovered.query_resume()
        assert receipt["phase"] == "open" and receipt["operation_id"] == "b" * 64
        assert recovered.check_sender_resume()["operation_id"] == "b" * 64
    assert gate._read(p["state"] / "worker-1.0.json")["status"] == "closed_initialized"


def test_completed_query_after_expiry_and_new_queue_data_never_rechecks_empty(activation_case, monkeypatch):
    import time
    p = activation_case
    with locked(p) as session:
        session.prepare()
        prepare_runtime(p)
        session.confirm()
        session.resume()
    # New post-T0 work is not a reason to rerun migration or demand old zero.
    (p["directory"] / "spool" / "new-business-original.json").write_text('{"synthetic":"retained"}')
    future = p["permit"]["expires_at"] + 1
    monkeypatch.setattr(time, "time", lambda: future)
    with locked(p) as recovered:
        result = recovered.query_resume()
        assert result["durable"] is True and result["query_only"] is True
        assert result["current_consumers_ready"] is False
        with pytest.raises(gate.AdmissionError):
            recovered.check_sender_resume()
    assert (p["directory"] / "spool" / "new-business-original.json").exists()


def test_pending_resume_cannot_open_after_permission_expiry(activation_case, monkeypatch):
    import time
    p = activation_case
    original = gate._write
    def interrupted(path, value):
        original(path, value)
        if path.name == "b" * 64 + ".intent.json":
            raise OSError("synthetic intent ACK lost")
    with pytest.raises(OSError):
        with locked(p) as session:
            session.prepare()
            prepare_runtime(p)
            session.confirm()
            monkeypatch.setattr(gate, "_write", interrupted)
            session.resume()
    monkeypatch.setattr(gate, "_write", original)
    monkeypatch.setattr(time, "time", lambda: p["permit"]["expires_at"] + 1)
    with locked(p) as recovered:
        result = recovered.query_resume()
        assert result["status"] == "activation_uncertain" and result["durable"] is False
        assert gate._read(p["fence"].root / "state.json")["phase"] == "frozen"
    assert not (p["fence"].root / "operations" / ("b" * 64 + ".json")).exists()


def test_normal_stop_new_keeps_drain_then_freeze_after_activation(activation_case):
    p = activation_case
    with locked(p) as session:
        session.prepare()
        handles = prepare_runtime(p)
        session.confirm()
        receipt = session.resume()
    p["fence"].stop_new("c" * 64, receipt["generation"], "synthetic later maintenance")
    handles["worker-1"].pulse()
    with pytest.raises(gate.AdmissionClosed):
        p["fence"].reserve_lease("new_intake")
    lease = p["fence"].reserve_lease("retained_drain", kind="drain")
    lease.finish()
    p["fence"].freeze("d" * 64, receipt["generation"] + 1, "synthetic fully drained")
    handles["worker-1"].pulse()
    assert runtime.resume_phase(p["runtime_root"], p["descriptor"]) == "frozen"


@pytest.mark.parametrize("record", ["activation.arm.json", "activation.prepare.json"])
def test_pending_only_permission_never_starts_runtime(activation_case, record):
    p = activation_case
    with locked(p) as session:
        session.prepare()
        path = p["runtime_root"] / record
        path.rename(path.with_name(path.name + ".pending"))
        startup._OWNERS.update(p["owners"]["worker-1"])
        p["process"]["pid"] = 1
        with pytest.raises(gate.AdmissionError, match="not confirmed"):
            runtime.RuntimeActivation()
        # Restore fixture bytes only; the runtime itself never repairs them.
        path.with_name(path.name + ".pending").rename(path)
        assert not runtime.paths(p["descriptor"], "worker-1", 0)["intent"].exists()



@pytest.mark.parametrize("different_owner", [False, True])
def test_real_lock_release_or_takeover_invalidates_live_capability(activation_case, different_owner):
    import fcntl
    p = activation_case
    with locked(p) as session:
        name, fd = session.shadow.fds[0]
        path = session.permit["locks"][name]["path"]
        fcntl.flock(fd, fcntl.LOCK_UN)
        other = os.open(path, os.O_RDWR)
        try:
            if different_owner:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(gate.AdmissionError, match="lock"):
                session.query()
        finally:
            os.close(other)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)



def test_receipt_rename_requires_real_file_and_directory_sync_before_business(activation_case, monkeypatch):
    p = activation_case
    receipt = p["fence"].root / "operations" / ("b" * 64 + ".json")
    sync_directory, confirm_file = gate._sync_directory, gate._confirm_file
    def broken(path):
        if path == receipt.parent and receipt.exists():
            raise OSError("synthetic directory fsync after final rename")
        return sync_directory(path)
    with pytest.raises(OSError, match="directory fsync"):
        with locked(p) as session:
            session.prepare()
            handles = prepare_runtime(p)
            session.confirm()
            monkeypatch.setattr(gate, "_sync_directory", broken)
            session.resume()
    assert receipt.exists() and not receipt.with_suffix(".json.pending").exists()
    original_hash = gate._hash(gate._read(receipt))
    before = sorted((p["fence"].root / "leases").iterdir())
    with pytest.raises(OSError, match="directory fsync"):
        handles["worker-1"].opened()
    with pytest.raises(OSError, match="directory fsync"):
        p["fence"].reserve_lease("synthetic_unconfirmed_intake")
    assert sorted((p["fence"].root / "leases").iterdir()) == before
    confirmed, synced = [], []
    def confirm(path):
        confirmed.append(path)
        return confirm_file(path)
    def sync(path):
        synced.append(path)
        return sync_directory(path)
    monkeypatch.setattr(gate, "_confirm_file", confirm)
    monkeypatch.setattr(gate, "_sync_directory", sync)
    assert handles["worker-1"].opened()
    assert receipt in confirmed and receipt.parent in synced
    lease = p["fence"].reserve_lease("synthetic_confirmed_original_intake")
    lease.finish()
    assert gate._hash(gate._read(receipt)) == original_hash
    # The host's original resume still failed. Only its original query may
    # report the now-confirmed outcome; no start/grant/resume is repeated.
    with locked(p) as recovered:
        result = recovered.query_resume()
        assert result["durable"] is True and result["query_only"] is True



def test_subsystem_death_after_pulse_never_enters_ordinary_restart_lane(activation_case, monkeypatch):
    import worker_main
    from routes import dashboard_routes, webhook_routes
    from services import quant_market_context
    p = activation_case
    with locked(p) as session:
        session.prepare()
        handles = prepare_runtime(p)
        handle = handles["worker-1"]
        original = handle.pulse
        stop, threads = p["background"][-1]
        def race():
            original()
            stop.set()
            for thread in threads:
                thread.join(1)
        monkeypatch.setattr(handle, "pulse", race)
        monkeypatch.setattr(quant_market_context, "quant_context_enabled", lambda: False)
        monkeypatch.setattr(dashboard_routes, "_ensure_quotes_refresher", lambda: pytest.fail("unreviewed quotes restart"))
        monkeypatch.setattr(webhook_routes, "start_webhook_dispatcher", lambda: pytest.fail("unreviewed dispatcher restart"))
        with pytest.raises(RuntimeError, match="restart forbidden"):
            worker_main._worker_subsystems(dict(handle.threads), handle)


def test_atomic_pulse_rotation_is_a_safe_bounded_snapshot(activation_case, monkeypatch):
    p = activation_case
    with locked(p) as session:
        session.prepare()
        prepare_runtime(p)
        path = runtime.paths(p["descriptor"], "worker-1", 0)["pulse"]
        first = gate._read(path)
        reached, replaced = threading.Event(), threading.Event()
        failures = []
        def rotate():
            try:
                assert reached.wait(2)
                gate._write(path, {**first, "wall": time.time(), "elapsed": gate._elapsed()})
            except BaseException as error:
                failures.append(error)
            finally:
                replaced.set()
        writer = threading.Thread(target=rotate)
        writer.start()
        original = gate._read
        observed = []
        def racing_read(candidate):
            value = original(candidate)
            if candidate == path and not reached.is_set():
                observed.append(value)
                reached.set()
                assert replaced.wait(2)
            return value
        monkeypatch.setattr(gate, "_read", racing_read)
        try:
            result = runtime.readiness(p["runtime_root"], p["descriptor"])
            assert result["worker-1"]["pulse"] == first
            assert len(observed) == 1 and original(path) != first and not failures
        finally:
            reached.set()
            writer.join(2)
        assert not writer.is_alive()




