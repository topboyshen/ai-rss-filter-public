"""Synthetic retained watchdog chain; native proof uses actual host namespaces."""
from copy import deepcopy
import os

import pytest

from test_production_admission_lifecycle import case, staged, operation  # noqa: F401
from services import admission_fence as gate, production_admission_lifecycle as life
from scripts import production_admission_watchdog as watchdog


@pytest.fixture
def watched(request, monkeypatch):
    p = request.getfixturevalue("staged")
    permit_sha = gate._hash(p.session.permit)
    descriptor = {**p.session.descriptor, "migration": {"permit_sha256": permit_sha}}
    gate._write(p.root / "descriptor.json", descriptor)
    p.session.descriptor = descriptor
    p.proof["descriptor_sha256"] = gate._hash(descriptor)
    intent_path = p.sender.control.root / "production_resume.intent.json"
    intent = gate._read(intent_path)
    intent["receiver_activation_proof"] = deepcopy(p.proof)
    gate._write(intent_path, intent)
    root = p.root / "watchdog"
    root.mkdir(mode=0o700)
    (root / "lock").touch(mode=0o600)
    identity = {"pid": os.getpid(), "synthetic": True}
    plan = {"schema": watchdog.SCHEMA, "parent": identity, "runtime_root": str(p.root),
            "migration_root": str(p.session.migration_root), "state": str(p.session.state),
            "activation_operation": p.session.operation_id, "freeze": p.session.permit["freeze"],
            "descriptor_sha256": gate._hash(descriptor), "permit_sha256": permit_sha,
            "receiver_generation": 0, "receiver_manifest_sha256": gate._hash(p.fence._manifest()),
            "sender_plan": p.sender.plan, "sender_evidence_root": str(p.sender.root),
            "sender_url": "http://127.0.0.1:12345", "token_file": str(p.root / "token"),
            "dsn_file": str(p.root / "dsn"), "config_path": p.session.permit["config"]["path"],
            "source_sha256": life.source_sha256(), "clock": {**life._clock_now(), "expires_at": p.clock["wall"] + 600},
            "sender_namespace_container_id": None}
    gate._write(root / "plan.json", plan)
    p.watchdog_root, p.watchdog_plan = root, plan
    p.watchdog_path = root / "plan.json"
    monkeypatch.setattr(watchdog, "process_identity", lambda pid=None: identity)
    # These tests isolate stop/handoff semantics; sealed environment behavior
    # has independent real-file/child tests and the full native harness.
    monkeypatch.setattr(watchdog.supervisor, "verify_child_environment", lambda _plan: None)
    watchdog.pulse(plan, root, "preopen")
    return p


def test_runtime_intake_requires_fresh_preopen_watchdog_and_stop_blocks(watched):
    p = watched
    lease = p.fence.reserve_lease("synthetic-watchdog")
    lease.finish()
    p.clock["wall"] += 9
    p.clock["elapsed"] += 9
    with pytest.raises(gate.AdmissionError, match="clock"):
        p.fence.reserve_lease("synthetic-watchdog")
    watchdog.pulse(p.watchdog_plan, p.watchdog_root, "preopen")
    gate._write(p.watchdog_root / "STOP.json", {"stopped": True})
    with pytest.raises(gate.AdmissionError, match="watchdog has stopped"):
        p.fence.reserve_lease("synthetic-watchdog")


def test_query_ready_rejects_changed_parent_or_expired_startup(watched, monkeypatch):
    p = watched
    assert watchdog.query_ready(p.watchdog_path, os.getpid())["phase"] == "preopen"
    monkeypatch.setattr(watchdog, "process_identity", lambda pid=None: {"pid": os.getpid(), "synthetic": False})
    with pytest.raises(gate.AdmissionError, match="actual pre-open"):
        watchdog.query_ready(p.watchdog_path, os.getpid())


def test_parent_loss_runs_restrictive_calls_never_resume(watched, monkeypatch):
    p = watched
    def gone(pid=None):
        raise ProcessLookupError("synthetic original parent gone")
    monkeypatch.setattr(watchdog, "process_identity", gone)
    calls = []
    monkeypatch.setattr(watchdog, "_sender_stop", lambda plan, root: calls.append("sender-stop") or {"state": "closed"})
    result = watchdog.run(p.watchdog_path)
    assert result == 0 and calls == ["sender-stop"]
    assert p.fence.snapshot()["phase"] == "draining"
    assert gate._read(p.watchdog_root / "result.json")["drained"] is False


def test_sender_query_failure_does_not_prevent_actual_receiver_stop(watched, monkeypatch):
    p = watched
    def fail(plan, root):
        raise TimeoutError("synthetic bound sender GET unknown")
    monkeypatch.setattr(watchdog, "_sender_stop", fail)
    result = watchdog.abort(p.watchdog_path)
    assert result["status"] == "stop_unconfirmed" and "sender" in result["errors"]
    assert p.fence.snapshot()["phase"] == "draining"


def test_other_descriptor_or_permit_cannot_authorize_watchdog(watched):
    p = watched
    original = gate._read(p.session.migration_root / "permit.json")
    gate._write(p.session.migration_root / "permit.json", {**original, "changed": True})
    with pytest.raises(gate.AdmissionError, match="targets differ"):
        watchdog.abort(p.watchdog_path)
    assert p.fence.snapshot()["phase"] == "open"


def test_exact_unknown_resume_get_then_stop_no_resume_post(watched, monkeypatch):
    from miniflux_handoff_adapter import MinifluxActivationControl
    p = watched
    p.sender.control = MinifluxActivationControl(p.sender.plan["binding"], p.sender.transport,
        lambda: p.sender.transport.inspect("ignored"), p.sender.root / "watchdog-activation", p.sender.plan["window"])
    proof = deepcopy(p.proof)
    # The fixture represents already OPEN after the exact retained CAS, while
    # the original production_resume response/confirmed file never arrived.
    original = p.sender.control._production_intent(p.sender.plan["binding"]["initial_operation_id"],
                                                  p.sender.plan["binding"]["generation"], proof)
    p.transport.current = original["operation_id"]
    p.transport.generation += 1
    p.transport.action = "resume"
    gate._write(p.sender.control.root / "production_resume.intent.json", original)
    p.sender.control.registration = lambda: None  # ordinary protocol double lacks135 registration only
    monkeypatch.setattr(watchdog.supervisor, "sender_from_plan", lambda plan: p.sender)
    value = watchdog._sender_stop(p.watchdog_plan, p.watchdog_root)
    assert value["receipt"]["state"] == "draining"
    assert [row[1] for row in p.transport.calls if row[0] == "POST"] == ["/stop-new"]
    assert not (p.sender.control.root / "production_resume.confirmed.json").exists()
    again = watchdog._sender_stop(p.watchdog_plan, p.watchdog_root)
    assert again["receipt"]["operation_id"] == value["receipt"]["operation_id"]
    assert [row[1] for row in p.transport.calls if row[0] == "POST"] == ["/stop-new"]


@pytest.mark.parametrize("fault", ["load", "run", "run_status", "handoff", "read_sync", "pulse", "failure_record"])
def test_failed_handoff_uses_original_watchdog_stop_even_if_parent_is_gone(watched, monkeypatch, fault):
    p = watched
    target = {"synthetic_supervisor": True}
    handoff = {"supervisor_plan_path": str(p.root / "continuation/supervisor.plan.json"),
               "supervisor_plan_sha256": gate._hash(target), "watchdog_plan_sha256": gate._hash(p.watchdog_plan),
               "owner": watchdog.process_identity()}
    if fault == "handoff":
        handoff["owner"] = {"pid": 999999, "synthetic": True}
    gate._write(p.watchdog_root / "handoff.json", handoff)
    calls = []
    guardian = watchdog.process_identity()

    def parent_gone(pid=None):
        if pid is not None:
            raise ProcessLookupError("synthetic parent exited after handoff")
        return guardian

    monkeypatch.setattr(watchdog, "process_identity", parent_gone)

    def failure(*_args, **_kwargs):
        raise OSError("synthetic handoff durability or supervisor failure")

    monkeypatch.setattr(watchdog.supervisor, "load", lambda _path: (target, None, None))
    monkeypatch.setattr(watchdog.supervisor, "run", lambda _path: 0)
    if fault == "load":
        monkeypatch.setattr(watchdog.supervisor, "load", failure)
    elif fault in {"run", "failure_record"}:
        monkeypatch.setattr(watchdog.supervisor, "run", failure)
    elif fault == "run_status":
        monkeypatch.setattr(watchdog.supervisor, "run", lambda _path: 1)
    elif fault == "read_sync":
        original = watchdog.runtime.read_confirmed
        def failed_read(path):
            if path == p.watchdog_root / "handoff.json":
                failure()
            return original(path)
        monkeypatch.setattr(watchdog.runtime, "read_confirmed", failed_read)
    elif fault == "pulse":
        monkeypatch.setattr(watchdog, "pulse", failure)
    if fault == "failure_record":
        original = gate._write
        def failed_record(path, value):
            if path == p.watchdog_root / "handoff.failed.json":
                failure()
            return original(path, value)
        monkeypatch.setattr(gate, "_write", failed_record)
    # Keep the actual receiver fence stop; sender is an explicit local spy.
    monkeypatch.setattr(watchdog, "_sender_stop", lambda plan, root:
                        calls.append((plan, root)) or {"state": "draining"})
    assert watchdog.run(p.watchdog_path) == 1
    assert calls == [(p.watchdog_plan, p.watchdog_root)]
    assert p.fence.snapshot()["phase"] == "draining"
    assert gate._read(p.watchdog_root / "result.json")["status"] == "producers_stopped_drain_pending"
    if fault != "failure_record":
        assert gate._read(p.watchdog_root / "handoff.failed.json")["handoff_succeeded"] is False


def test_successful_handoff_does_not_add_another_stop(watched, monkeypatch):
    p = watched
    target = {"synthetic_supervisor": True}
    gate._write(p.watchdog_root / "handoff.json", {
        "supervisor_plan_path": str(p.root / "continuation/supervisor.plan.json"),
        "supervisor_plan_sha256": gate._hash(target), "watchdog_plan_sha256": gate._hash(p.watchdog_plan),
        "owner": watchdog.process_identity()})
    monkeypatch.setattr(watchdog.supervisor, "load", lambda _path: (target, None, None))
    monkeypatch.setattr(watchdog.supervisor, "run", lambda _path: 0)
    calls = []
    monkeypatch.setattr(watchdog, "abort", lambda *_args: calls.append("unexpected-stop"))
    assert watchdog.run(p.watchdog_path) == 0 and not calls
    assert not (p.watchdog_root / "STOP.json").exists()
    assert p.fence.snapshot()["phase"] == "open"


def test_failed_handoff_with_lost_original_binding_cannot_guess_stop_targets(watched, monkeypatch):
    p = watched
    target = {"synthetic_supervisor": True}
    gate._write(p.watchdog_root / "handoff.json", {
        "supervisor_plan_path": str(p.root / "continuation/supervisor.plan.json"),
        "supervisor_plan_sha256": gate._hash(target), "watchdog_plan_sha256": gate._hash(p.watchdog_plan),
        "owner": watchdog.process_identity()})
    monkeypatch.setattr(watchdog.supervisor, "load", lambda _path: (target, None, None))
    def failed_run(_path):
        path = p.session.migration_root / "permit.json"
        gate._write(path, {**gate._read(path), "synthetic_changed": True})
        raise OSError("synthetic handoff failure and original permit loss")
    monkeypatch.setattr(watchdog.supervisor, "run", failed_run)
    calls = []
    monkeypatch.setattr(watchdog, "_sender_stop", lambda *_args: calls.append("must-not-guess"))
    assert watchdog.run(p.watchdog_path) == 1 and calls == []
    assert not (p.watchdog_root / "result.json").exists()
    assert gate._read(p.watchdog_root / "handoff.failed.json")["handoff_succeeded"] is False
    assert p.fence.snapshot()["phase"] == "open"


@pytest.mark.parametrize("change", ["confirmed", "missing", "pending", "wrong_plan", "wrong_marker", "unknown"])
def test_existing_stop_exits_original_guardian_without_handoff_or_mutation(watched, monkeypatch, change):
    p = watched
    calls = []
    monkeypatch.setattr(watchdog, "_sender_stop", lambda *_args:
                        calls.append("sender-stop") or {"state": "already_closed", "queue_checked": False})
    value = watchdog.abort(p.watchdog_path)
    assert value["status"] == "producers_stopped_drain_pending"
    path = p.watchdog_root / "result.json"
    if change == "missing":
        path.rename(path.with_suffix(".retained"))
    elif change == "pending":
        gate._write(path.with_suffix(".json.pending"), value)
    elif change == "wrong_plan":
        gate._write(path, {**value, "watchdog_plan_sha256": "0" * 64})
    elif change == "wrong_marker":
        gate._write(p.watchdog_root / "STOP.json", {"watchdog_plan_sha256": "0" * 64,
                                                 "reason": "parent_or_preopen_window_lost"})
    elif change == "unknown":
        gate._write(path, {**value, "status": "stop_unconfirmed", "errors": {"sender": {"type": "OSError"}}})
    gate._write(p.watchdog_root / "handoff.json", {"must_not_be_used": True})
    monkeypatch.setattr(watchdog.supervisor, "load", lambda *_args: pytest.fail("STOP must preclude handoff"))
    monkeypatch.setattr(watchdog, "pulse", lambda *_args: pytest.fail("STOP must preclude fresh liveness"))
    assert watchdog.run(p.watchdog_path) == (0 if change == "confirmed" else 1)
    assert calls == ["sender-stop"] and p.fence.snapshot()["phase"] == "draining"


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="actual Linux process identity required")
def test_actual_guardian_observes_killed_owned_parent_and_stops_receiver(request, monkeypatch):
    import json
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path
    from services.production_startup import process_identity
    p = request.getfixturevalue("watched")
    # time.time was replaced by the common synthetic fixture. Use the kernel
    # realtime clock here so the independently executed child sees this window.
    monkeypatch.setattr(gate.time, "time", lambda: time.clock_gettime(time.CLOCK_REALTIME))
    monkeypatch.setattr(gate, "_elapsed", lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
    monkeypatch.setattr(gate, "_boot_identity", lambda: "linux:" + Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    monkeypatch.setattr(watchdog, "process_identity", process_identity)
    from production_admission_environment_support import sealed_inputs
    from production_source_fixture_support import isolated_git_source
    source = isolated_git_source(p.root.parent / "actual-child-source", watchdog.ROOT)
    monkeypatch.setattr(watchdog, "ROOT", source)
    monkeypatch.setattr(watchdog, "__file__", str(source / "scripts/production_admission_watchdog.py"))
    monkeypatch.setattr(watchdog.supervisor, "ROOT", source)
    context = sealed_inputs(p.root.parent / "actual-child-environment", source_root=source)
    _sealed, environment_permit = context.__enter__()
    request.addfinalizer(lambda: context.__exit__(None, None, None))
    parent = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child = None
    try:
        now = int(time.time())
        new_root = p.root / "actual-owned-fence"
        gate.provision(new_root, p.fence.binding, {"not_before": now - 1, "expires_at": now + 60,
                                                  "max_admissions": 3, "max_bytes": 300})
        fence = gate.Fence(new_root, p.fence.binding)
        fence.open_trial(operation(701), 0, "synthetic owned parent death test")
        permit = deepcopy(p.session.permit)
        permit.update(environment_permit)
        permit["freeze"]["root"] = str(new_root)
        gate._write(p.session.migration_root / "permit.json", permit)
        descriptor = {"freeze": permit["freeze"], "migration": {"permit_sha256": gate._hash(permit)}}
        gate._write(p.root / "descriptor.json", descriptor)
        plan = deepcopy(p.watchdog_plan)
        plan.update(parent=process_identity(parent.pid), freeze=permit["freeze"], permit_sha256=gate._hash(permit),
                    config_path=permit["config"]["path"],
                    descriptor_sha256=gate._hash(descriptor), receiver_manifest_sha256=gate._hash(fence._manifest()),
                    clock={**life._clock_now(), "expires_at": now + 60})
        plan["sender_plan"]["postgres_identity"]["schema_version"] = 135
        gate._write(Path(plan["token_file"]), {"token": operation(702)})
        gate._write(Path(plan["dsn_file"]), {"dsn": "host=127.0.0.1 port=1 dbname=synthetic connect_timeout=1"})
        # A nonexistent synthetic Docker ID fails before any PostgreSQL access.
        plan["sender_plan"]["binding"]["component"]["id"] = "0" * 64
        gate._write(p.watchdog_path, plan)
        child = watchdog.start(p.watchdog_path)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                watchdog.query_ready(p.watchdog_path, child.pid)
                break
            except (OSError, gate.AdmissionError):
                time.sleep(0.05)
        else:
            pytest.fail("actual guardian did not publish a bound pre-open pulse")
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=3)
        assert child.wait(timeout=15) == 1  # Sender identity deliberately unavailable.
        value = json.loads((p.watchdog_root / "result.json").read_text())
        assert value["status"] == "stop_unconfirmed" and set(value["errors"]) == {"sender"}
        assert fence.snapshot()["phase"] == "draining"
        assert not (p.watchdog_root / "handoff.json").exists()
    finally:
        for owned in (child, parent):
            if owned is not None and owned.poll() is None:
                owned.kill()
                owned.wait(timeout=3)


def test_actual_guardian_source_copy_has_real_git_identity_and_rejects_drift(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from production_admission_environment_support import sealed_inputs
    from production_source_fixture_support import isolated_git_source
    source = isolated_git_source(tmp_path / "source", watchdog.ROOT)
    script = source / "scripts/production_admission_watchdog.py"
    assert script.read_bytes() == Path(watchdog.__file__).read_bytes()
    assert not (source / ".git").is_symlink() and (source / ".git").is_dir()
    assert "__pycache__/" in (source / ".gitignore").read_text()
    subprocess.run(["git", "-C", str(source), "ls-files", "--error-unmatch", ".gitignore"],
                   check=True, capture_output=True, timeout=10)
    with sealed_inputs(tmp_path / "environment", source_root=source) as (_files, permit):
        monkeypatch.setattr(watchdog.supervisor, "ROOT", source)
        environment = watchdog.supervisor._sealed_environment(permit)
        child = subprocess.run([sys.executable, "-I", "-B", str(script), "--help"],
                               cwd=source, env=environment, capture_output=True, timeout=15)
        assert child.returncode == 0, child.stderr.decode()
        assert watchdog.supervisor.private.source_identity(source)["source_sha"] == permit["source"]["revision"]
        script.write_bytes(script.read_bytes() + b"\n# synthetic source drift\n")
        with pytest.raises(watchdog.supervisor.private.ContractError, match="clean source"):
            watchdog.supervisor._sealed_environment(permit)
