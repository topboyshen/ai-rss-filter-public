"""Live custody objects survive finalize/recovery; JSON never replaces them.

These are wiring tests with lifecycle as an explicit boundary double. The
actual lock, Docker and routing proofs belong to the lifecycle/native tests.
"""
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import json
import os
from types import SimpleNamespace

import pytest

from scripts import controlled_release_finalize as final
from scripts import production_gateway_switch as real_gateway, retained_gateway_custody as real_custody
from services import admission_fence as gate
from test_controlled_release_finalize import case as final_case  # noqa: F401


@pytest.fixture
def case(request):
    return request.getfixturevalue("final_case")


def bind(case, monkeypatch):
    legacy, calls = object(), []
    gateway = real_gateway.GatewaySwitch.__new__(real_gateway.GatewaySwitch)
    plan = {"operation_id": "f" * 64}
    gateway.session, gateway.plan, gateway.plan_sha = case.receiver, plan, real_custody.digest(plan)
    original = final.lifecycle.verify_active
    held = {"declaration": {"old_container_id": "7" * 64, "old_configuration_proof_sha256": "9" * 64},
            "sha256": "8" * 64, "plan": plan, "proof": {"synthetic_original": True},
            "bindings": [{"HostIp": "127.0.0.1", "HostPort": "5000"}]}
    route = {"status": "gateway_switched", "durable": True, "operation_id": plan["operation_id"],
             "plan_sha256": real_custody.digest(plan), "old_gateway_retained": "7" * 64,
             "old_config": held["proof"], "http": [{"binding": held["bindings"][0]}]}
    receipt = {"old_gateway_id": "7" * 64, "declaration_sha256": "8" * 64,
               "configuration_proof_sha256": "9" * 64, "gateway_receipt_sha256": real_custody.digest(route)}
    state = {"held": False, "post_failure": False, "events": [], "route": route}
    gateway.test_state = state
    lock_path = case.sender.control.root / "gateway-control.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)

    @contextmanager
    def retained_verification(receiver):
        assert receiver is case.receiver and state["held"] is False
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state["held"] = True
            state["events"].append("control_lock_acquired")
            before = deepcopy(state["route"])
            yield deepcopy(before)
            state["events"].append("post_route_check")
            gate._require(not state["post_failure"] and before == state["route"], "synthetic final route postcheck failed")
        finally:
            state["held"] = False
            state["events"].append("control_lock_released")
            os.close(fd)
    gateway.retained_verification = retained_verification

    def verify_records(_held, _review, rows):
        assert state["held"] and rows == [{"synthetic_old": True}]
        return rows[0]

    def verify_legacy(_held, _row, actual, *, private_digest):
        assert state["held"] and private_digest == "d" * 64
        gate._require(actual is legacy, "synthetic real custody capability required")

    helper = SimpleNamespace(verify_gateway=real_custody.verify_gateway, verify_receipt=real_custody.verify_receipt,
                             verify_records=verify_records, verify_legacy=verify_legacy)
    monkeypatch.setattr(final.private, "retained_custody", lambda *_a: (helper, held))
    monkeypatch.setattr(final.private, "inspect_project", lambda _c: [{"synthetic_old": True}])

    def verify(receiver, evidence, **kwargs):
        assert not state["held"], "lifecycle must not reacquire gateway flock during CAS"
        assert receiver is case.receiver and evidence == case.plan["lifecycle_root"]
        calls.append(kwargs)
        gate._require(kwargs.get("legacy") is legacy and kwargs.get("gateway") is gateway,
                      "synthetic real custody capability required")
        return {**original(receiver, evidence, operation_id=kwargs["operation_id"]),
                "retained_gateway_custody": deepcopy(receipt)}

    monkeypatch.setattr(final.lifecycle, "verify_active", verify)
    return legacy, gateway, calls, receipt


def test_old_path_uses_original_lifecycle_call_without_new_keywords(case, monkeypatch):
    original, calls = final.lifecycle.verify_active, []

    def verify(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs == {"operation_id": case.plan["lifecycle_operation_id"]}
        return original(*args, **kwargs)

    monkeypatch.setattr(final.lifecycle, "verify_active", verify)
    assert final.finalize(case.receiver, case.sender, case.plan, case.root)["release_registered"]
    assert len(calls) == 2
    intent = gate._read(case.root / "intent.json")
    assert "retained_gateway_custody" not in intent["actual"]


def test_finalize_passes_identical_live_objects_and_seals_only_verified_proof(case, monkeypatch):
    legacy, gateway, calls, receipt = bind(case, monkeypatch)
    assert final.finalize(case.receiver, case.sender, case.plan, case.root,
                          legacy=legacy, gateway=gateway)["release_registered"]
    assert len(calls) == 2
    assert all(item["legacy"] is legacy and item["gateway"] is gateway for item in calls)
    intent = gate._read(case.root / "intent.json")
    assert intent["actual"]["retained_gateway_custody"] == receipt
    assert "object at" not in json.dumps(intent)


@pytest.mark.parametrize("variant", ["absent", "legacy_missing", "gateway_missing", "json", "other_legacy", "other_gateway"])
def test_missing_forged_or_different_capability_is_not_replaced_by_receipt(case, monkeypatch, variant):
    legacy, gateway, calls, _receipt = bind(case, monkeypatch)
    kwargs = {"legacy": legacy, "gateway": gateway}
    if variant == "absent":
        kwargs = {}
    elif variant == "legacy_missing":
        kwargs.pop("legacy")
    elif variant == "gateway_missing":
        kwargs.pop("gateway")
    elif variant == "json":
        kwargs = {"legacy": {"writer_lock_held": True}, "gateway": {"status": "gateway_switched"}}
    elif variant == "other_legacy":
        kwargs["legacy"] = object()
    else:
        kwargs["gateway"] = object()
    before = (case.registry / "current.state").read_bytes()
    with pytest.raises(gate.AdmissionError, match="real custody capability"):
        final.finalize(case.receiver, case.sender, case.plan, case.root, **kwargs)
    assert len(calls) == 1 and not list(case.root.iterdir())
    assert (case.registry / "current.state").read_bytes() == before


def test_new_gateway_custody_proof_change_is_not_lost_from_stable_comparison(case, monkeypatch):
    legacy, gateway, _calls, receipt = bind(case, monkeypatch)
    original = case.check

    def change_during_check(*args, **kwargs):
        result = original(*args, **kwargs)
        receipt["gateway_receipt_sha256"] = "b" * 64
        return result

    monkeypatch.setattr(final, "_check_process", change_during_check)
    before = (case.registry / "current.state").read_bytes()
    with pytest.raises(gate.AdmissionError, match="original owners changed"):
        final.finalize(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    assert not (case.root / "intent.json").exists()
    assert (case.registry / "current.state").read_bytes() == before


def interrupt(case, monkeypatch, legacy, gateway):
    original = final._replace

    def fail_after_write(f, row):
        original(f, row)
        raise OSError("synthetic custody registration ACK lost")

    monkeypatch.setattr(final, "_replace", fail_after_write)
    with pytest.raises(OSError, match="ACK lost"):
        final.finalize(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    monkeypatch.setattr(final, "_replace", original)
    return (case.root / "intent.json").read_bytes()


def test_original_recovery_passes_same_live_capabilities_without_replaying_business(case, monkeypatch):
    legacy, gateway, calls, _receipt = bind(case, monkeypatch)
    original = interrupt(case, monkeypatch, legacy, gateway)
    first_calls = len(calls)
    result = final.query(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    assert result["release_registered"] and result["query_only"] and result["current_health_rechecked"]
    assert len(calls) == first_calls + 2
    assert all(item["legacy"] is legacy and item["gateway"] is gateway for item in calls)
    assert (case.root / "intent.json").read_bytes() == original
    assert (case.registry / "history.tsv").read_bytes().count(b"controlled-finalize") == 1
    assert all(call[0] != "POST" for call in case.calls)


def test_pending_recovery_without_live_capabilities_cannot_use_sealed_success_json(case, monkeypatch):
    legacy, gateway, calls, _receipt = bind(case, monkeypatch)
    original = interrupt(case, monkeypatch, legacy, gateway)
    before = {path.name: path.read_bytes() for path in case.registry.iterdir() if path.is_file()}
    first_calls = len(calls)
    with pytest.raises(gate.AdmissionError, match="real custody capability"):
        final.query(case.receiver, case.sender, case.plan, case.root)
    assert len(calls) == first_calls + 1
    assert (case.root / "intent.json").read_bytes() == original
    assert {path.name: path.read_bytes() for path in case.registry.iterdir() if path.is_file()} == before
    assert not (case.root / "result.json").exists()


def test_completed_query_remains_historical_receipt_not_new_runtime_success(case, monkeypatch):
    legacy, gateway, calls, _receipt = bind(case, monkeypatch)
    final.finalize(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    first_calls = len(calls)
    result = final.query(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    assert result["release_registered"] and result["query_only"]
    assert result["current_health_rechecked"] is False and len(calls) == first_calls


def test_registry_cas_holds_gateway_lock_without_nested_lifecycle_and_result_waits_for_postcheck(case, monkeypatch):
    legacy, gateway, _calls, _receipt = bind(case, monkeypatch)
    state, original_replace, original_write = gateway.test_state, final._replace, gate._write
    writes = []

    def replace(f, row):
        assert state["held"]
        state["events"].append("registry_cas")
        original_replace(f, row)

    def write(path, value):
        if path == case.root / "intent.json":
            assert state["held"]
        if path == case.root / "result.json":
            assert not state["held"] and state["events"][-2:] == ["post_route_check", "control_lock_released"]
            writes.append("success_after_postcheck")
        return original_write(path, value)

    monkeypatch.setattr(final, "_replace", replace)
    monkeypatch.setattr(gate, "_write", write)
    assert final.finalize(case.receiver, case.sender, case.plan, case.root,
                          legacy=legacy, gateway=gateway)["release_registered"]
    assert state["events"].count("registry_cas") == 11 and writes == ["success_after_postcheck"]


@pytest.mark.parametrize("change", ["post_failure", "route_changed_during_cas"])
def test_postcheck_failure_keeps_original_transition_marker_and_never_success(case, monkeypatch, change):
    legacy, gateway, _calls, _receipt = bind(case, monkeypatch)
    state, replace = gateway.test_state, final._replace
    if change == "post_failure":
        state["post_failure"] = True
    else:
        def changed(f, row):
            replace(f, row)
            state["route"]["synthetic_concurrent_change"] = True
        monkeypatch.setattr(final, "_replace", changed)
    with pytest.raises(gate.AdmissionError, match="postcheck failed"):
        final.finalize(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    intent = (case.root / "intent.json").read_bytes()
    assert (case.registry / final.MARKER).exists() and not (case.root / "result.json").exists()
    assert final._state(case.registry, "current")[0][0] == case.plan["image_ref"]
    assert not state["held"]
    # Recovery may only finish the original transition after fresh real custody.
    monkeypatch.setattr(final, "_replace", replace)
    state["post_failure"] = False
    state["route"].pop("synthetic_concurrent_change", None)
    result = final.query(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    assert result["release_registered"] and result["current_health_rechecked"]
    assert (case.root / "intent.json").read_bytes() == intent
    assert (case.registry / "history.tsv").read_bytes().count(b"controlled-finalize") == 1
    assert not (case.registry / final.MARKER).exists()


@pytest.mark.parametrize("change", ["route", "capability"])
def test_change_after_last_actual_check_blocks_before_intent_or_registry_cas(case, monkeypatch, change):
    legacy, gateway, _calls, _receipt = bind(case, monkeypatch)
    retain, before = final._retain, (case.registry / "current.state").read_bytes()

    def changed(*args, **kwargs):
        result = retain(*args, **kwargs)
        if change == "route":
            gateway.test_state["route"]["synthetic_concurrent_change"] = True
        else:
            gateway.session = object()
        return result

    monkeypatch.setattr(final, "_retain", changed)
    with pytest.raises(gate.AdmissionError, match="gateway changed before registry CAS|capability differs"):
        final.finalize(case.receiver, case.sender, case.plan, case.root, legacy=legacy, gateway=gateway)
    assert not (case.root / "intent.json").exists() and not (case.root / "result.json").exists()
    assert not (case.registry / final.MARKER).exists() and (case.registry / "current.state").read_bytes() == before
