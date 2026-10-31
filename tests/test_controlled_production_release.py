"""Private input integrity and single-attempt release stop boundaries."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from scripts import controlled_production_release as deploy
from services import admission_fence as gate


def store(path, value):
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def bound_plan(tmp_path, monkeypatch):
    roots = {key: tmp_path / key for key in
             ("runtime_root", "state", "lifecycle_root", "migration_root", "evidence_root", "registry_root")}
    for key, path in roots.items():
        if key != "migration_root":
            path.mkdir(mode=0o700)
    lock = roots["registry_root"] / "release.lock"
    lock.touch(mode=0o600)
    permit = {"source": {"revision": "a" * 40}, "parent_operation_id": "2" * 64,
              "sender": {"closed_operation_id": "4" * 64, "expected_identity": {"schema_version": 135}},
              "locks": {"release": {"path": str(lock)}}, "config": {"path": str(tmp_path / "runtime.yaml")}}
    final = {"source_sha": "a" * 40, "lifecycle_operation_id": "2" * 64,
             "lifecycle_root": str(roots["lifecycle_root"]), "sender_closed_operation_id": "4" * 64,
             "sender_generation": 0, "registry_root": str(roots["registry_root"])}
    sender = {"postgres_identity": {"schema_version": 135}}
    from services.production_admission_lifecycle import SCHEMA
    now = int(time.time())
    policy = {"schema": SCHEMA, "operation_id": "6" * 64, "approved_by": "synthetic-regression",
              "not_before": now - 1, "expires_at": now + 3600, "lease_seconds": 600,
              "renew_before_seconds": 120, "max_renewals": 10, "receiver_stop_records": 100,
              "receiver_stop_bytes": 2**20, "sender_stop_records": 100, "sender_stop_bytes": 2**20,
              "minimum_free_bytes": 64 * 1024**2}
    external = {}
    for name in ("legacy", "gateway"):
        evidence = tmp_path / (name + "-evidence")
        evidence.mkdir(mode=0o700)
        external[name] = {"plan": store(tmp_path / (name + "-plan.json"), {}), "evidence_root": str(evidence)}
    plan = {"schema": 1, "operation_id": "1" * 64, "source_sha": "a" * 40,
            "lifecycle_operation_id": "2" * 64, "activation_operation_id": "3" * 64,
            "migration": store(tmp_path / "permit.json", permit), "finalize": store(tmp_path / "finalize.json", final),
            "admission_policy": store(tmp_path / "policy.json", policy),
            "sender": {"plan": store(tmp_path / "sender.json", sender), "endpoint": "http://127.0.0.1:12345",
                       "token": str(tmp_path / "token.json"), "evidence_root": str(tmp_path / "sender-evidence")},
            **{key: str(value) for key, value in roots.items() if key != "registry_root"},
            "runtime_paths": {"database": "/app/data/cache.db", "config": "/app/config.yaml",
                              "state": "/app/data/slots", "freeze": "/app/data/fence"},
            "web_workers": 2, "threads": 4, "readiness_seconds": 1, "max_polls": 2, **external}
    path = tmp_path / "plan.json"
    store(path, plan)
    monkeypatch.setattr(deploy.private, "source_identity", lambda _root: {"source_sha": "a" * 40})
    return SimpleNamespace(path=path, plan=plan, permit=permit, final=final, roots=roots)


def test_private_file_change_rejected_before_any_deployment_intent(bound_plan):
    p = bound_plan
    assert deploy.load_inputs(p.path)[0] == p.plan
    Path(p.plan["migration"]["path"]).write_text("{}")
    with pytest.raises(gate.AdmissionError, match="file changed"):
        deploy.load_inputs(p.path)
    assert list(p.roots["evidence_root"].iterdir()) == []


@pytest.mark.parametrize("field,value", [("source_sha", "b" * 40), ("state", "alias"),
                                          ("max_polls", True), ("readiness_seconds", 181),
                                          ("web_workers", 5), ("threads", 3)])
def test_bad_source_paths_and_budgets_close_before_action(bound_plan, field, value):
    p = bound_plan
    plan = deepcopy(p.plan)
    plan[field] = plan["runtime_root"] if value == "alias" else value
    store(p.path, plan)
    with pytest.raises(gate.AdmissionError):
        deploy.load_inputs(p.path)
    assert list(p.roots["evidence_root"].iterdir()) == []


def test_readiness_unknown_is_not_retried_or_treated_as_success():
    calls = []
    def query():
        calls.append(1)
        return {"status": "activation_uncertain"}
    with pytest.raises(gate.AdmissionError, match="uncertain"):
        deploy._wait(query, "business_prepared", {"readiness_seconds": 10, "max_polls": 32})
    assert len(calls) == 1


def test_pending_readiness_has_original_finite_poll_budget(monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
    count = []
    with pytest.raises(gate.AdmissionUncertain, match="budget exhausted"):
        deploy._wait(lambda: count.append(1) or {"status": "startup_pending"},
                     "closed_initialized", {"readiness_seconds": 10, "max_polls": 3})
    assert len(count) == 3


@pytest.fixture
def execute_edges(bound_plan, monkeypatch):
    from core import config
    from scripts import controlled_release_finalize as registration
    p = bound_plan
    calls = []
    from scripts import production_admission_supervisor as supervisor
    @contextmanager
    def synthetic_environment(_permit):
        yield
    monkeypatch.setattr(supervisor, "sealed_configuration", synthetic_environment)
    monkeypatch.setattr(config, "CONFIG_PATH", config.CONFIG_PATH)
    monkeypatch.setattr(config.config, "reload", lambda: None)
    monkeypatch.setattr(deploy.migration, "validate_permit", lambda *_a, **_kw: None)
    def check(*_a, **_kw):
        calls.append("formal_check")
        return {"health_checked": False}
    monkeypatch.setattr(registration, "check_contract", check)
    @contextmanager
    def legacy(_plan):
        yield SimpleNamespace(check_stopped=lambda: calls.append("legacy_stopped"))
    monkeypatch.setattr(deploy, "_legacy", legacy)
    monkeypatch.setattr(deploy, "_handoff", lambda *_a: calls.append("supervisor_handoff") or {"status": "live"})
    monkeypatch.setattr(deploy, "_sender", lambda *_a: SimpleNamespace(
        query_initialize_closed=lambda _op: calls.append("sender_closed")))
    monkeypatch.setattr(deploy, "closed_phase", lambda *_a: calls.append("closed") or {"status": "closed"})
    monkeypatch.setattr(deploy, "active_phase", lambda *_a: calls.append("active") or {"status": "registered"})
    return p, calls, registration


def test_formal_gate_rejection_prevents_intent_migration_and_sender(execute_edges, monkeypatch):
    p, calls, registration = execute_edges
    def reject(*_a, **_kw):
        raise gate.AdmissionError("formal contract negative control")
    monkeypatch.setattr(registration, "check_contract", reject)
    with pytest.raises(gate.AdmissionError, match="negative control"):
        deploy.execute(p.path)
    assert not calls and list(p.roots["evidence_root"].iterdir()) == []


def test_failed_closed_stage_preserves_stop_and_rejects_duplicate_execute(execute_edges, monkeypatch):
    p, calls, _registration = execute_edges
    def lost(*_a):
        calls.append("start_unknown")
        raise TimeoutError("lost actual start acknowledgment")
    monkeypatch.setattr(deploy, "closed_phase", lost)
    with pytest.raises(TimeoutError):
        deploy.execute(p.path)
    root = p.roots["evidence_root"]
    before = {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
              for path in root.rglob("*")}
    assert "deployment.intent.json" in before and "STOP.json" in before and "RESULT.json" not in before
    assert calls == ["formal_check", "legacy_stopped", "sender_closed", "start_unknown"]
    with pytest.raises(gate.AdmissionError, match="already attempted"):
        deploy.execute(p.path)
    assert before == {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
                      for path in root.rglob("*")}
    assert calls == ["formal_check", "legacy_stopped", "sender_closed", "start_unknown"]


def test_final_registration_failure_never_reports_deployed(execute_edges, monkeypatch):
    p, calls, _registration = execute_edges
    def failed(*_a):
        calls.append("registration_failed")
        raise OSError("synthetic final directory fsync")
    monkeypatch.setattr(deploy, "active_phase", failed)
    with pytest.raises(OSError):
        deploy.execute(p.path)
    assert (p.roots["evidence_root"] / "closed-phase.json").is_file()
    assert not (p.roots["evidence_root"] / "RESULT.json").exists()
    assert json.loads((p.roots["evidence_root"] / "STOP.json").read_text())["automatic_rollback"] is False
    assert calls[-1] == "registration_failed"


def test_query_before_registration_does_not_call_sender_or_resume(execute_edges):
    p, calls, _registration = execute_edges
    result = deploy.execute(p.path)
    assert result["status"] == "deployed_controlled"
    assert calls == ["formal_check", "legacy_stopped", "sender_closed", "closed", "active", "legacy_stopped", "supervisor_handoff"]
    calls.clear()
    result = deploy.query(p.path)
    assert result["status"] == "deployment_uncertain" and not result["mutation_replayed"] and not calls
    assert len(list(p.roots["evidence_root"].glob("query-*.json"))) == 1


@pytest.mark.parametrize("change", [{"max_renewals": True}, {"expires_at": 1}, {"lease_seconds": 7201}])
def test_invalid_continuation_policy_stops_before_deployment_attempt(bound_plan, change):
    p = bound_plan
    policy_path = Path(p.plan["admission_policy"]["path"])
    value = json.loads(policy_path.read_bytes())
    value.update(change)
    p.plan["admission_policy"] = store(policy_path, value)
    store(p.path, p.plan)
    with pytest.raises(gate.AdmissionError):
        deploy.load_inputs(p.path)
    assert list(p.roots["evidence_root"].iterdir()) == []


def test_nested_evidence_and_runtime_roots_rejected_before_attempt(bound_plan):
    p = bound_plan
    child = p.roots["runtime_root"] / "nested"
    child.mkdir(mode=0o700)
    p.plan["evidence_root"] = str(child)
    store(p.path, p.plan)
    with pytest.raises(gate.AdmissionError, match="overlap"):
        deploy.load_inputs(p.path)


def test_expired_policy_cannot_execute_but_original_query_can_read_binding(bound_plan):
    p = bound_plan
    path = Path(p.plan["admission_policy"]["path"])
    value = json.loads(path.read_bytes())
    value.update(not_before=1, expires_at=3601)
    p.plan["admission_policy"] = store(path, value)
    store(p.path, p.plan)
    with pytest.raises(gate.AdmissionError, match="not current"):
        deploy.load_inputs(p.path)
    assert deploy.load_inputs(p.path, active=False)[3] == value
    assert list(p.roots["evidence_root"].iterdir()) == []


def test_late_ready_response_cannot_extend_readiness_deadline(monkeypatch):
    clocks = iter([0, 0, 2])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(clocks))
    with pytest.raises(gate.AdmissionError, match="exceeded deadline"):
        deploy._wait(lambda: {"status": "business_prepared"}, "business_prepared",
                     {"readiness_seconds": 1, "max_polls": 1})


def test_guardian_handoff_failure_never_reports_deployed(execute_edges, monkeypatch):
    p, calls, _registration = execute_edges
    def fail(_active):
        calls.append("handoff_lost")
        raise TimeoutError("actual supervisor acknowledgment unknown")
    monkeypatch.setattr(deploy, "_handoff", fail)
    monkeypatch.setattr(deploy, "_stop_retained", lambda _root: {"status": "stop_unconfirmed"})
    with pytest.raises(TimeoutError):
        deploy.execute(p.path)
    root = p.roots["evidence_root"]
    assert not (root / "RESULT.json").exists()
    value = json.loads((root / "STOP.json").read_bytes())
    assert value["restrictive_stop"]["status"] == "stop_unconfirmed"
    assert value["automatic_rollback"] is False
    assert calls[-1] == "handoff_lost"


def test_actual_guardian_readiness_stops_on_exit_and_never_accepts_ready_dict():
    count = []
    child = SimpleNamespace(poll=lambda: 7)
    with pytest.raises(gate.AdmissionError, match="exited"):
        deploy._actual_child_receipt(child, lambda: count.append(1) or {"ready": True}, 1)
    assert count == []


def test_actual_guardian_waits_only_for_not_yet_created_receipt(tmp_path, monkeypatch):
    path, calls = tmp_path / "pulse.json", []
    child = SimpleNamespace(poll=lambda: None)
    def publish(_seconds):
        assert not path.exists() and calls == []
        path.write_text("original child receipt")
    monkeypatch.setattr(deploy.time, "sleep", publish)
    result = deploy._actual_child_receipt(child, lambda: calls.append("verified_original") or {"pid": 123}, 1,
                                         receipt_path=path, stop_paths=(tmp_path / "STOP.json",))
    assert result == {"pid": 123} and calls == ["verified_original"]


def test_inflight_readiness_write_is_retried_then_original_receipt_verified(tmp_path, monkeypatch):
    path, calls = tmp_path / "pulse.json", []
    pending = tmp_path / "pulse.json.pending"
    pending.write_text("child write in flight")

    def rename(_seconds):
        calls.append("waited")
        pending.rename(path)
    monkeypatch.setattr(deploy.time, "sleep", rename)
    result = deploy._actual_child_receipt(SimpleNamespace(poll=lambda: None),
                                         lambda: calls.append("verified_original") or {"pid": 123}, 1,
                                         receipt_path=path, stop_paths=(tmp_path / "STOP.json",))
    assert result == {"pid": 123} and calls == ["waited", "verified_original"]


def test_persistent_readiness_pending_is_uncertain_at_deadline_without_query(tmp_path):
    (tmp_path / "pulse.json").write_text("older original")
    (tmp_path / "pulse.json.pending").write_text("stuck write")
    with pytest.raises(gate.AdmissionError, match="readiness write is uncertain"):
        deploy._actual_child_receipt(SimpleNamespace(poll=lambda: None), lambda: pytest.fail("must not query"), 0.2,
                                    receipt_path=tmp_path / "pulse.json", stop_paths=(tmp_path / "STOP.json",))


@pytest.mark.parametrize("change", ["exit", "stop"])
def test_readiness_pending_never_hides_child_exit_or_stop(tmp_path, monkeypatch, change):
    (tmp_path / "pulse.json.pending").write_text("write in flight")
    state = {"rc": None}

    def fault(_seconds):
        if change == "exit":
            state["rc"] = 1
        else:
            (tmp_path / "STOP.json").write_text("stopped")
    monkeypatch.setattr(deploy.time, "sleep", fault)
    with pytest.raises(gate.AdmissionError, match="exited|stopped"):
        deploy._actual_child_receipt(SimpleNamespace(poll=lambda: state["rc"]), lambda: pytest.fail("must not query"), 1,
                                    receipt_path=tmp_path / "pulse.json", stop_paths=(tmp_path / "STOP.json",))


@pytest.mark.parametrize("marker", ["STOP.json", "STOP.json.pending"])
def test_actual_guardian_never_waits_past_uncertain_write_or_stop(tmp_path, monkeypatch, marker):
    (tmp_path / marker).write_text("retained original")
    monkeypatch.setattr(deploy.time, "sleep", lambda *_a: pytest.fail("must stop before sleeping"))
    with pytest.raises(gate.AdmissionError, match="uncertain|stopped"):
        deploy._actual_child_receipt(SimpleNamespace(poll=lambda: None), lambda: pytest.fail("must not query"), 1,
                                    receipt_path=tmp_path / "pulse.json", stop_paths=(tmp_path / "STOP.json",))


def test_present_invalid_guardian_receipt_is_not_treated_as_startup_delay(tmp_path, monkeypatch):
    path = tmp_path / "pulse.json"
    path.write_text("invalid original")
    monkeypatch.setattr(deploy.time, "sleep", lambda *_a: pytest.fail("must not retry invalid receipt"))
    def invalid():
        raise gate.AdmissionError("activation record is not confirmed")
    with pytest.raises(gate.AdmissionError, match="not confirmed"):
        deploy._actual_child_receipt(SimpleNamespace(poll=lambda: None), invalid, 1, receipt_path=path)


def test_missing_guardian_receipt_has_a_real_finite_deadline(tmp_path):
    with pytest.raises(gate.AdmissionUncertain, match="did not prove"):
        deploy._actual_child_receipt(SimpleNamespace(poll=lambda: None), lambda: pytest.fail("no receipt yet"), 0.01,
                                    receipt_path=tmp_path / "pulse.json")


@pytest.fixture
def activation_edges(bound_plan, monkeypatch):
    """Ordering regressions only; real protocol proofs run in native isolation."""
    from scripts import controlled_release_finalize as registry
    from scripts import production_admission_supervisor as supervisor
    from scripts import production_admission_watchdog as watchdog
    from scripts import production_gateway_switch as gateway
    from services import production_admission_lifecycle as life
    p, calls = bound_plan, []
    def called(name, value=None):
        def invoke(*_a, **_kw):
            calls.append(name)
            return {} if value is None else value
        return invoke
    receiver = SimpleNamespace(prepare=called("prepare"), query=called("prepared", {"status": "business_prepared"}),
                               confirm=called("confirm"), resume=called("receiver_resume"))
    @contextmanager
    def locked(*_a, **_kw):
        yield receiver
    monkeypatch.setattr(deploy, "locked_activation", locked)
    monkeypatch.setattr(gateway, "GatewaySwitch", lambda *_a: SimpleNamespace(switch=called("gateway")))
    prepared = {"plan_path": str(p.roots["runtime_root"] / "watchdog-plan.json")}
    monkeypatch.setattr(watchdog, "prepare", called("watchdog_prepare", prepared))
    monkeypatch.setattr(deploy, "_watchdog_ready", called("watchdog_actual_ready"))
    monkeypatch.setattr(watchdog, "abort", called("restrictive_abort", {"status": "stopped"}))
    monkeypatch.setattr(deploy.lifecycle, "verify_active", called("verify_runtime"))
    monkeypatch.setattr(life, "initialize", called("first_finite_lease"))
    monkeypatch.setattr(supervisor, "prepare", called("supervisor_prepare", {"plan_path": "synthetic-plan"}))
    monkeypatch.setattr(registry, "finalize", called("formal_finalize"))
    sender = SimpleNamespace(production_resume=called("sender_resume"))
    return p, calls, sender, registry


def test_activation_cannot_open_before_gateway_and_actual_watchdog(activation_edges):
    p, calls, sender, _registry = activation_edges
    value = deploy.active_phase(p.plan, p.permit, p.final, {}, sender, object())
    assert calls == ["prepare", "prepared", "confirm", "gateway", "watchdog_prepare", "watchdog_actual_ready",
                     "receiver_resume", "verify_runtime", "sender_resume", "first_finite_lease",
                     "supervisor_prepare", "formal_finalize"]
    assert value["supervisor"]["plan_path"] == "synthetic-plan"
    assert (p.roots["evidence_root"] / "watchdog-prepared.json").exists()


def test_activation_threads_same_legacy_and_gateway_objects_instead_of_their_receipts(activation_edges, monkeypatch):
    from scripts import production_gateway_switch as gateway
    p, _calls, sender, _registry = activation_edges
    legacy = object()
    receipt = {"status": "synthetic_route_proof"}
    adapter = SimpleNamespace(switch=lambda: receipt)
    monkeypatch.setattr(gateway, "GatewaySwitch", lambda *_a: adapter)
    seen = []
    def verify(_receiver, _evidence, **options):
        assert options["legacy"] is legacy
        assert options["gateway"] is adapter and options["gateway"] is not receipt
        seen.append(True)
        return {"status": "synthetic_runtime_proof"}
    monkeypatch.setattr(deploy.lifecycle, "verify_active", verify)
    result = deploy.active_phase(p.plan, p.permit, p.final, {}, sender, object(), legacy)
    assert seen == [True] and result["gateway"] is receipt


def test_closed_lifecycle_retains_the_same_legacy_capability_across_create_start_and_query(bound_plan, monkeypatch):
    p, legacy, seen = bound_plan, object(), []
    session = SimpleNamespace(authorize=lambda: None, migrate=lambda: {"status": "synthetic_migrated"})
    @contextmanager
    def locked(*_args, **_options):
        yield session
    monkeypatch.setattr(deploy.migration, "locked_session", locked)
    def stage(name, result):
        def invoke(actual_session, *_args, **options):
            assert actual_session is session and options["legacy"] is legacy
            seen.append(name)
            return result
        return invoke
    monkeypatch.setattr(deploy.lifecycle, "create", stage("create", {}))
    monkeypatch.setattr(deploy.lifecycle, "start", stage("start", {"status": "initializing"}))
    monkeypatch.setattr(deploy.lifecycle, "query", stage("query", {"status": "closed_initialized"}))
    monkeypatch.setattr(deploy, "arm", lambda *_a, **_kw: {"status": "synthetic_armed"})
    result = deploy.closed_phase(p.plan, p.permit, object(), legacy)
    assert seen == ["create", "start", "query"] and result["started"]["status"] == "closed_initialized"


def test_watchdog_readiness_failure_never_opens_receiver_or_sender(activation_edges, monkeypatch):
    p, calls, sender, _registry = activation_edges
    def failed(_prepared):
        raise TimeoutError("synthetic watchdog did not prove actual process")
    monkeypatch.setattr(deploy, "_watchdog_ready", failed)
    with pytest.raises(TimeoutError):
        deploy.active_phase(p.plan, p.permit, p.final, {}, sender, object())
    assert calls == ["prepare", "prepared", "confirm", "gateway", "watchdog_prepare"]
    assert (p.roots["evidence_root"] / "watchdog-prepared.json").exists()


def test_failure_after_open_invokes_original_guardian_stop(activation_edges, monkeypatch):
    p, calls, sender, registry = activation_edges
    def failed(*_a):
        raise OSError("synthetic final registry sync uncertain")
    monkeypatch.setattr(registry, "finalize", failed)
    with pytest.raises(OSError):
        deploy.active_phase(p.plan, p.permit, p.final, {}, sender, object())
    assert calls[-1] == "restrictive_abort"
    assert "first_finite_lease" in calls
    assert json.loads((p.roots["evidence_root"] / "abort-receipt.json").read_bytes()) == {"status": "stopped"}


def test_parent_unsealed_configuration_cannot_reach_release_intent(bound_plan, monkeypatch):
    from scripts import production_admission_supervisor as supervisor
    p, actions = bound_plan, []
    @contextmanager
    def rejected(_permit):
        raise gate.AdmissionError("synthetic sealed environment binding changed")
        yield
    monkeypatch.setattr(supervisor, "sealed_configuration", rejected)
    monkeypatch.setattr(deploy, "_execute_bound", lambda _path: actions.append("must-not-run"))
    with pytest.raises(gate.AdmissionError, match="sealed environment"):
        deploy.execute(p.path)
    assert actions == [] and not list(p.roots["evidence_root"].iterdir())
