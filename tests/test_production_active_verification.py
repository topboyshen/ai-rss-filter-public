"""Real activation lock capability with a narrow Docker inspection double."""
from copy import deepcopy

import pytest

from test_production_activation import activation_case, locked, prepare_runtime  # noqa: F401
from test_production_migration import prepared  # noqa: F401
from test_production_container_lifecycle import container
from services import admission_fence as gate, production_container_lifecycle as lifecycle


@pytest.fixture
def active_case(activation_case, monkeypatch):  # noqa: F811
    p = activation_case
    evidence = p["directory"] / "lifecycle"
    evidence.mkdir(mode=0o700)
    old = [container("1" * 64, "previous-api", "web", "sha256:" + "c" * 64),
           container("2" * 64, "previous-worker", "worker", "sha256:" + "c" * 64)]
    new = [container("a" * 64, "api", "web", p["permit"]["source"]["image_id"]),
           container("b" * 64, "worker", "worker", p["permit"]["source"]["image_id"])]
    for row in new:
        row["State"].update(Status="running", Running=True)
    rows = old + new
    contract = {"app_services": ["api", "worker"], "review": {
        "existing_topology": lifecycle.private.runtime_topology(old),
        "service_mapping": {"api": ["previous-api"], "worker": ["previous-worker"]}}}
    intent = {"operation_id": "9" * 64, "migration_permit_sha256": gate._hash(p["permit"]),
        "private_contract_sha256": "e" * 64, "source": p["permit"]["source"],
        "runtime_root": str(p["runtime_root"]), "state": str(p["state"]),
        "roles": {"web": "api", "worker": "worker"}, "previous_containers": lifecycle._old_binding(old)}
    created = {"operation_id": "9" * 64, "intent_sha256": gate._hash(intent),
        "descriptor_sha256": gate._hash(p["descriptor"]), "containers": {"web": "a" * 64, "worker": "b" * 64},
        "grants": {slot: gate._read(p["runtime_root"] / f"{slot}.0.grant.json") for slot in p["descriptor"]["slots"]}}
    closed = {"operation_id": "9" * 64, "created_sha256": gate._hash(created), "status": "closed_initialized",
              "durable": True, "pending_slots": [], "containers": created["containers"]}
    for name, value in (("create.intent", intent), ("created", created), ("closed", closed)):
        gate._write(evidence / (name + ".json"), value)
    monkeypatch.setattr(lifecycle.private, "load", lambda *a, **kw: (contract, {"services": {}}, "e" * 64))
    monkeypatch.setattr(lifecycle.private, "inspect_project", lambda *a: deepcopy(rows))
    verified = []
    monkeypatch.setattr(lifecycle.private, "_verify_runtime_records", lambda *args: verified.append(args[-1]))
    with locked(p) as session:
        session.prepare()
        prepare_runtime(p)
        session.confirm()
        session.resume()
        yield {**p, "session": session, "evidence": evidence, "rows": rows, "verified": verified}


def test_active_verify_requires_real_capability_and_retains_previous(active_case):
    p = active_case
    with pytest.raises(gate.AdmissionError, match="real activation"):
        lifecycle.verify_active({"business_open": True}, p["evidence"], operation_id="9" * 64)
    result = lifecycle.verify_active(p["session"], p["evidence"], operation_id="9" * 64)
    assert result["status"] == "actual_runtime_verified" and result["business_open"]
    assert result["release_registered"] is False and result["sender_resumed"] is False
    assert result["retained_previous_ids"] == ["1" * 64, "2" * 64]
    assert [row["Id"] for row in p["verified"][0]] == ["a" * 64, "b" * 64]


@pytest.mark.parametrize("drift", ["old_running", "new_id", "extra", "descriptor", "old_image"])
def test_active_verify_rejects_drift_before_runtime_probe(active_case, drift):
    p = active_case
    if drift == "old_running":
        p["rows"][0]["State"]["Running"] = True
    elif drift == "new_id":
        p["rows"][2]["Id"] = "d" * 64
    elif drift == "extra":
        p["rows"].append(container("d" * 64, "foreign", "web", "sha256:" + "c" * 64))
    elif drift == "old_image":
        p["rows"][0]["Image"] = "sha256:" + "d" * 64
    else:
        path = p["evidence"] / "created.json"
        value = gate._read(path)
        gate._write(path, {**value, "descriptor_sha256": "d" * 64})
    with pytest.raises(gate.AdmissionError):
        lifecycle.verify_active(p["session"], p["evidence"], operation_id="9" * 64)
    assert not p["verified"] and not (p["evidence"] / "active-verified.json").exists()
