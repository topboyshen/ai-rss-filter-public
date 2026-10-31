"""Fixed CLI and retained-target stop tests; real host/native proof is separate."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_production_admission_lifecycle import case, staged, operation  # noqa: F401
from services import admission_fence as gate, production_admission_lifecycle as life
from scripts import production_admission_supervisor as supervisor
from production_sender_driver import NativeSenderActivationTransport, PostgresReader


@pytest.fixture
def prepared(request, monkeypatch):
    from production_admission_environment_support import sealed_inputs
    p = request.getfixturevalue("staged")
    context = sealed_inputs(p.root.parent / "child-environment")
    f, environment_permit = context.__enter__()
    request.addfinalizer(lambda: context.__exit__(None, None, None))
    monkeypatch.setattr(supervisor, "ROOT", f["root"])
    p.session.permit.update(environment_permit)
    gate._write(p.session.migration_root / "permit.json", p.session.permit)
    life.initialize(p.session, p.sender, p.policy)
    # The supervisor refuses arbitrary transports. This synthetic preparation
    # uses exact objects; its bound observation still uses the protocol fixture.
    token = p.root / "token.json"
    gate._write(token, {"token": operation(99)})
    original = p.sender.transport, p.sender.postgres
    p.sender.transport = NativeSenderActivationTransport("http://127.0.0.1:12345", token)
    reader = PostgresReader.__new__(PostgresReader)
    reader.path = p.root / "dsn.json"
    p.sender.postgres = reader
    result = supervisor.prepare(p.session, p.sender)
    p.sender.transport, p.sender.postgres = original
    p.plan_path = Path(result["plan_path"])
    return p


def test_sealed_plan_targets_and_authority_are_exact(prepared):
    p = prepared
    plan, root, authority = supervisor.load(p.plan_path)
    assert root == p.root / "continuation" and plan["authority_sha256"] == gate._hash(authority)
    assert (root / "supervisor.lock").stat().st_mode & 0o777 == 0o600
    bad = {**plan, "freeze": {**plan["freeze"], "root": str(p.root / "other")}}
    gate._write(p.plan_path, bad)
    with pytest.raises(gate.AdmissionError, match="host paths"):
        supervisor.load(p.plan_path)


def test_dead_activation_session_does_not_prevent_independent_real_stop(prepared):
    p = prepared
    p.session.live = False
    result = supervisor.restrictive_stop(p.plan_path, sender=p.sender)
    assert result["status"] == "producers_stopped_drain_pending"
    assert p.transport.state == p.fence.snapshot()["phase"] == "draining"


def test_sender_constructor_failure_still_stops_local_receiver(prepared, monkeypatch):
    p = prepared
    def fail(_):
        raise OSError("synthetic credential unavailable")
    monkeypatch.setattr(supervisor, "sender_from_plan", fail)
    result = supervisor.restrictive_stop(p.plan_path)
    assert result["status"] == "stop_unconfirmed" and "sender_construction_error" in result
    assert p.fence.snapshot()["phase"] == "draining" and p.transport.state == "open"


def test_receiver_manifest_change_does_not_skip_actual_sender_stop(prepared):
    p = prepared
    (p.fence.root / "manifest.json").rename(p.fence.root / "original-manifest-preserved.json")
    result = supervisor.restrictive_stop(p.plan_path, sender=p.sender)
    assert result["status"] == "stop_unconfirmed" and "receiver" in result["errors"]
    assert p.transport.state == "draining"


def test_tick_context_failure_uses_stop_after_lock_release(prepared, monkeypatch):
    from contextlib import contextmanager
    p = prepared
    lock = {"held": False}
    @contextmanager
    def failed(*args, **kwargs):
        lock["held"] = True
        try:
            raise gate.AdmissionError("synthetic dead owner")
            yield
        finally:
            lock["held"] = False
    original = supervisor.restrictive_stop
    def stop(*args, **kwargs):
        assert not lock["held"]
        return original(*args, **kwargs)
    monkeypatch.setattr(supervisor, "locked_activation", failed)
    monkeypatch.setattr(supervisor, "restrictive_stop", stop)
    monkeypatch.setattr(supervisor, "sender_from_plan", lambda plan: p.sender)
    monkeypatch.setattr(supervisor, "process_identity", lambda: {"pid": 123, "synthetic": True})
    value = supervisor.tick(p.plan_path)
    assert value["result"]["status"] == "producers_stopped_drain_pending"
    assert gate._read(p.root / "continuation/supervisor.tick.json") == value


def test_stale_or_other_process_tick_cannot_claim_supervisor_ready(prepared, monkeypatch):
    p = prepared
    plan, root, authority = supervisor.load(p.plan_path)
    receipt = life._current(root, authority)
    owner = {"pid": 123, "synthetic": True}
    monkeypatch.setattr(supervisor, "process_identity", lambda pid: owner)
    value = {"schema": supervisor.SCHEMA, "plan_sha256": gate._hash(plan), "owner": owner,
             "clock": life._clock_now(), "result": {"status": "live", "receipt_sha256": gate._hash(receipt)}}
    gate._write(root / "supervisor.tick.json", value)
    assert supervisor.query_started(p.plan_path, 123) == value
    bad = deepcopy(value)
    bad["owner"]["pid"] = 999
    gate._write(root / "supervisor.tick.json", bad)
    with pytest.raises(gate.AdmissionError, match="actual live"):
        supervisor.query_started(p.plan_path, 123)
    gate._write(root / "supervisor.tick.json", value)
    p.clock["wall"] += 46
    p.clock["elapsed"] += 46
    with pytest.raises(gate.AdmissionError, match="clock"):
        supervisor.query_started(p.plan_path, 123)


def test_fixed_launch_does_not_forward_interpreter_injection(prepared, monkeypatch, tmp_path):
    p = prepared
    calls = []
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("PYTHONSTARTUP", str(tmp_path / "evil.py"))
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    original = supervisor.subprocess.Popen
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda argv, **kw:
                        (calls.append((argv, kw)) or SimpleNamespace(pid=123)) if argv[0] == sys.executable
                        else original(argv, **kw))
    supervisor.start(p.plan_path)
    argv, options = calls[0]
    assert argv[:3] == [sys.executable, "-I", "-B"]
    assert set(options["env"]) == {"PATH", "LANG", "AI_FILTER_CONFIG_PATH", "AI_FILTER_API_KEY", "LLM_API_KEY"}
    assert options["env"]["AI_FILTER_API_KEY"] == "synthetic-child-secret-0123456789"
    assert options["close_fds"] and options["start_new_session"] and "shell" not in options


def test_actual_isolated_cli_ignores_malicious_pythonpath(prepared, monkeypatch, tmp_path):
    p = prepared
    marker = tmp_path / "executed"
    (tmp_path / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('bad')")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    child = supervisor.start(p.plan_path)
    assert child.wait(timeout=10) != 0  # No synthetic boot/PID doubles in child.
    assert not marker.exists()
    launch = gate._read(p.root / "continuation/supervisor.launch.json")
    assert launch["status"] == "started_not_yet_proven"


def _busy_then_lock(p, monkeypatch, *, operator_first):
    """First attempt: an operator holds the controller lock (optionally mid-STOP)."""
    from contextlib import contextmanager
    state = {"held": False, "attempts": 0, "unlocked_stops": 0}
    @contextmanager
    def locked(*args, **kwargs):
        state["attempts"] += 1
        if state["attempts"] == 1:
            state["held"] = True
            if operator_first:
                life.stop_new(p.session, p.sender, reason_code="operator")
            raise gate.AdmissionBusy("active work/lock did not drain within bound")
        state["held"] = False
        yield p.session
    original = supervisor.restrictive_stop
    def stop(*args, **kwargs):
        state["unlocked_stops"] += 1
        assert not state["held"], "unlocked stop raced the operator-held controller lock"
        return original(*args, **kwargs)
    monkeypatch.setattr(supervisor, "locked_activation", locked)
    monkeypatch.setattr(supervisor, "restrictive_stop", stop)
    monkeypatch.setattr(supervisor, "sender_from_plan", lambda plan: p.sender)
    monkeypatch.setattr(supervisor, "process_identity", lambda: {"pid": 123, "synthetic": True})
    return state


def test_operator_stop_holding_lock_is_not_raced_by_unlocked_supervisor_stop(prepared, monkeypatch):
    p = prepared
    state = _busy_then_lock(p, monkeypatch, operator_first=True)
    value = supervisor.tick(p.plan_path)
    assert state["attempts"] == 2 and state["unlocked_stops"] == 0
    assert value["result"]["status"] == "producers_stopped_drain_pending"
    assert value["result"]["drained"] is False
    assert p.transport.state == p.fence.snapshot()["phase"] == "draining"


def test_busy_without_exact_operator_stop_still_fails_closed_to_restrictive_stop(prepared, monkeypatch):
    p = prepared
    real_stop = supervisor.restrictive_stop
    state = _busy_then_lock(p, monkeypatch, operator_first=False)
    calls = []
    monkeypatch.setattr(supervisor, "restrictive_stop", lambda *a, **k: calls.append(1) or real_stop(*a, **k))
    value = supervisor.tick(p.plan_path)
    assert state["attempts"] == 1 and calls == [1]
    assert value["result"]["trigger"]["type"] == "AdmissionBusy"
    assert value["result"]["status"] == "producers_stopped_drain_pending"


def test_non_operator_stop_marker_does_not_authorize_retry(prepared, monkeypatch):
    p = prepared
    plan, root, authority = supervisor.load(p.plan_path)
    life.stop_new(p.session, p.sender, reason_code="supervisor_failure")
    assert not supervisor.operator_stop_marked(root, authority)
    gate._write(root / "STOP.json", {**gate._read(root / "STOP.json"), "authority_sha256": "0" * 64})
    assert not supervisor.operator_stop_marked(root, authority)
