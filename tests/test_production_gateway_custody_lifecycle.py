"""Lifecycle custody never becomes a generic ignored-ID mechanism."""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from services import admission_fence as gate, production_container_lifecycle as lifecycle
from services.production_activation_control import ReceiverActivationSession
from scripts import production_gateway_switch as gateway
from scripts import retained_gateway_custody as custody
from test_private_release_contract import fixture as private_fixture  # noqa: F401
from test_retained_gateway_custody import custody_case, legacy_capability, load, record  # noqa: F401


def contract(f):
    return {"review": f["review_data"], "app_services": list(f["manifest_data"]["services"])}


def test_retains_all_three_old_ids_and_allows_only_two_exact_new_apps(custody_case, monkeypatch):  # noqa: F811
    f, held = custody_case, load(custody_case)
    c, manifest, cap = contract(f), f["manifest_data"], legacy_capability(f)
    rows = deepcopy(f["rows"])
    monkeypatch.setattr(lifecycle.private, "preflight", lambda *_a: None)
    monkeypatch.setattr(lifecycle.private, "inspect_project", lambda _c: deepcopy(rows))
    roles = {"web": "candidate-api", "worker": "candidate-worker"}
    old = lifecycle._before_create(c, manifest, roles, legacy=cap, private_digest="e" * 64)
    assert {row["Id"] for row in old} == {"1" * 64, "2" * 64, "3" * 64}
    intent = {"previous_containers": lifecycle._old_binding(old), "roles": roles,
              "retained_gateway_custody": held["sha256"]}
    rows += [record(str(n) * 64, name, f["review_data"]["project"], f["review_data"]["project_directory"])
             for n, name in ((4, "candidate-api"), (5, "candidate-worker"))]
    result = lifecycle._project(c, intent, manifest=manifest, legacy=cap, private_digest="e" * 64)
    assert set(result) == {"web", "worker"}
    rows.append(record("6" * 64, "unreviewed", f["review_data"]["project"], f["review_data"]["project_directory"]))
    with pytest.raises(gate.AdmissionError, match="unexpected"):
        lifecycle._project(c, intent, manifest=manifest, legacy=cap, private_digest="e" * 64)


@pytest.mark.parametrize("capability", [None, {}, {"status": "registered_legacy_runtime_stopped"}])
def test_custody_cannot_create_from_missing_or_json_legacy_receipt(custody_case, monkeypatch, capability):  # noqa: F811
    f = custody_case
    monkeypatch.setattr(lifecycle.private, "preflight", lambda *_a: None)
    monkeypatch.setattr(lifecycle.private, "inspect_project", lambda _c: deepcopy(f["rows"]))
    with pytest.raises(gate.AdmissionError, match="live custody"):
        lifecycle._before_create(contract(f), f["manifest_data"], {"web": "candidate-api", "worker": "candidate-worker"},
                                 legacy=capability, private_digest="e" * 64)


def test_sealed_custody_is_not_an_external_ignored_id_argument(custody_case, monkeypatch):  # noqa: F811
    f = custody_case
    monkeypatch.setattr(lifecycle.private, "inspect_project", lambda _c: deepcopy(f["rows"]))
    intent = {"previous_containers": lifecycle._old_binding(f["rows"]),
              "roles": {"web": "candidate-api", "worker": "candidate-worker"},
              "retained_gateway_custody": "0" * 64}
    with pytest.raises(gate.AdmissionError, match="declaration changed"):
        lifecycle._project(contract(f), intent, manifest=f["manifest_data"], legacy=legacy_capability(f),
                           private_digest="e" * 64)


def setup_active(f, monkeypatch, *, post_failure=False):
    session = ReceiverActivationSession.__new__(ReceiverActivationSession)
    session.permit = {"config": {"private_contract_path": str(f["config"].parent / "sealed" / "contract.json")}}
    monkeypatch.setattr(lifecycle.private, "load", lambda *_a, **_kw: (contract(f), f["manifest_data"], "e" * 64))
    held = load(f)
    receipt = {"status": "gateway_switched", "durable": True, "operation_id": f["plan"]["operation_id"],
               "plan_sha256": custody.digest(f["plan"]), "old_gateway_retained": f["rows"][-1]["Id"],
               "old_config": f["proof"], "http": [{"binding": item} for item in held["bindings"]]}
    adapter = gateway.GatewaySwitch.__new__(gateway.GatewaySwitch)
    adapter.session, adapter.plan, adapter.plan_sha = session, f["plan"], custody.digest(f["plan"])
    events = []
    @contextmanager
    def retained_verification(actual):
        assert actual is session
        events.append("control_lock_acquired")
        try:
            yield deepcopy(receipt)
            events.append("post_route_check")
            if post_failure:
                raise gate.AdmissionError("synthetic route changed after yield")
        finally:
            events.append("control_lock_released")
    adapter.retained_verification = retained_verification
    def actual_verify(actual, evidence, *, operation_id, legacy, gateway_receipt, persist):
        assert events == ["control_lock_acquired"] and actual is session
        assert persist is False and gateway_receipt == receipt and legacy is not None
        assert not (evidence / "active-verified.json").exists()
        events.append("actual_runtime_verified")
        return {"status": "actual_runtime_verified", "operation_id": operation_id}
    monkeypatch.setattr(lifecycle, "_verify_active", actual_verify)
    evidence = f["config"].parent / "active-evidence"
    evidence.mkdir(mode=0o700)
    return session, adapter, evidence, events


def test_active_verify_computes_under_real_type_lock_and_persists_only_after_postcheck(custody_case, monkeypatch):  # noqa: F811
    f = custody_case
    session, adapter, evidence, events = setup_active(f, monkeypatch)
    result = lifecycle.verify_active(session, evidence, operation_id="f" * 64, legacy=legacy_capability(f), gateway=adapter)
    assert gate._read(evidence / "active-verified.json") == result
    assert events == ["control_lock_acquired", "actual_runtime_verified", "post_route_check", "control_lock_released"]


def test_post_yield_failure_never_leaves_a_success_receipt(custody_case, monkeypatch):  # noqa: F811
    f = custody_case
    session, adapter, evidence, events = setup_active(f, monkeypatch, post_failure=True)
    with pytest.raises(gate.AdmissionError, match="changed after yield"):
        lifecycle.verify_active(session, evidence, operation_id="f" * 64, legacy=legacy_capability(f), gateway=adapter)
    assert not (evidence / "active-verified.json").exists()
    assert events[-1] == "control_lock_released"


@pytest.mark.parametrize("replacement", ["receipt_dictionary", "other_session"])
def test_active_verify_rejects_receipt_dict_and_same_shape_session(custody_case, monkeypatch, replacement):  # noqa: F811
    f = custody_case
    session, adapter, evidence, events = setup_active(f, monkeypatch)
    if replacement == "receipt_dictionary":
        adapter = {"status": "gateway_switched"}
    else:
        adapter.session = ReceiverActivationSession.__new__(ReceiverActivationSession)
    with pytest.raises(gate.AdmissionError, match="switch capability"):
        lifecycle.verify_active(session, evidence, operation_id="f" * 64, legacy=legacy_capability(f), gateway=adapter)
    assert events == [] and not (evidence / "active-verified.json").exists()
