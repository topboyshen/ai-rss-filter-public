"""Create, grant and start exact candidate containers under real migration locks.

Old containers remain stopped and retained. Neither retries nor Compose up,
recreation, removal, rollback or opening admission are part of this protocol.
"""
from __future__ import annotations

import os
from pathlib import Path

from services import admission_fence as gate, production_startup_control as startup
from scripts import private_release_contract as private


# This narrower entry point creates only two reviewed application services.
# Compose create has no --no-deps flag. Reject dependency/namespace references,
# providers, hooks and future unreviewed service fields before any create intent.
CREATE_SERVICE_FIELDS = frozenset({
    "image", "command", "entrypoint", "environment", "working_dir", "user", "group_add",
    "cap_add", "cap_drop", "security_opt", "mem_limit", "memswap_limit", "pids_limit", "cpus",
    "volumes", "ports", "networks", "network_mode", "secrets", "configs", "healthcheck",
    "labels", "logging", "restart", "read_only", "tmpfs", "stop_signal", "stop_grace_period",
    "platform", "deploy", "scale", "profiles",
})


def _create_scope(manifest):
    """Reject service expansion; existing private validation still runs first."""
    for service in manifest["services"].values():
        gate._require(isinstance(service, dict) and set(service) <= CREATE_SERVICE_FIELDS,
                      "unsupported controlled create service field")
        deployment = service.get("deploy", {})
        gate._require(isinstance(deployment, dict) and set(deployment) <= {"replicas"},
                      "controlled create deployment indirection forbidden")
        for value in (service.get("scale", 1), deployment.get("replicas", 1)):
            gate._require(type(value) is int and value == 1, "controlled create requires one exact replica")
        gate._require(service.get("profiles", []) == []
                      and isinstance(service.get("profiles", []), list)
                      and service.get("network_mode", "none") == "none",
                      "controlled create profiles or namespace references forbidden")


def _save(path, value):
    if gate._present(path):
        gate._require(gate._read(path) == value, "lifecycle evidence conflicts")
    gate._write(path, value)
    return value


def _inputs(session, runtime_root, state, evidence, operation_id, *, active=True):
    startup._session(session, active=active)
    paths = tuple(Path(value) for value in (runtime_root, state, evidence))
    for path in paths:
        gate._directory(path)
    gate._require(len(set(paths)) == 3 and all(path.is_absolute() and str(path) == os.path.normpath(path) for path in paths)
                  and not any(a.is_relative_to(b) for a in paths for b in paths if a != b),
                  "distinct absolute lifecycle paths required")
    gate._require(isinstance(operation_id, str) and gate.HEX64.fullmatch(operation_id)
                  and operation_id != session.permit["operation_id"], "invalid lifecycle operation")
    contract_path = Path(session.permit["config"]["private_contract_path"])
    contract, manifest, digest = private.load(contract_path, Path(__file__).resolve().parents[1], require_fresh=active)
    return paths, contract_path.parent, contract, manifest, digest


def _roles(contract, manifest):
    roles = {}
    for name in contract["app_services"]:
        service = manifest["services"][name]
        role = service.get("environment", {}).get("AI_FILTER_ROLE")
        gate._require(role in {"web", "worker"} and role not in roles
                      and not service.get("profiles") and service.get("deploy", {}).get("replicas", 1) == 1,
                      "one explicit candidate service per application role required")
        roles[role] = name
    gate._require(set(roles) == {"web", "worker"}, "both application roles required")
    return roles


def _service(record):
    return record["Config"]["Labels"].get("com.docker.compose.service")


def _old_binding(records):
    return {row["Id"]: {"image": row["Image"], "service": _service(row),
            "started_at": row["State"]["StartedAt"], "finished_at": row["State"].get("FinishedAt"),
            "restart_count": row["RestartCount"], "running": row["State"]["Running"]}
            for row in records}


def _custody(contract, manifest, records, *, legacy, private_digest):
    reviewed = private.retained_custody(contract, manifest)
    if reviewed is None:
        return None
    helper, held = reviewed
    try:
        row = helper.verify_records(held, contract["review"], records)
        helper.verify_legacy(held, row, legacy, private_digest=private_digest)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise gate.AdmissionError("retained gateway live custody differs") from None
    return helper, held


def _before_create(contract, manifest, roles, *, legacy=None, private_digest=None):
    private.preflight(contract, manifest)
    records = private.inspect_project(contract)
    gate._require(private.runtime_topology(records) == contract["review"]["existing_topology"],
                  "previous project changed before create")
    _custody(contract, manifest, records, legacy=legacy, private_digest=private_digest)
    names = set(roles.values())
    gate._require(not names.intersection(_service(row) for row in records),
                  "candidate service names must not replace retained previous containers")
    mapped = {old for name in names for old in contract["review"]["service_mapping"][name]}
    previous = [row for row in records if _service(row) in mapped]
    gate._require(previous and mapped == {_service(row) for row in previous}, "previous application mapping absent")
    for row in previous:
        state = row["State"]
        gate._require(not state["Running"] and not state.get("Restarting") and not state.get("Paused")
                      and not state.get("Dead") and state.get("Status") in {"exited", "created"},
                      "previous application must already be safely stopped; this protocol never stops it")
    return records


def _project(contract, intent, *, legacy=None, private_digest=None, manifest=None):
    records = private.inspect_project(contract)
    old_ids = set(intent["previous_containers"])
    old = [row for row in records if row["Id"] in old_ids]
    gate._require(len(old) == len(old_ids) and _old_binding(old) == intent["previous_containers"]
                  and private.runtime_topology(old) == contract["review"]["existing_topology"],
                  "retained previous runtime changed")
    custody = _custody(contract, manifest, records, legacy=legacy, private_digest=private_digest)
    gate._require((custody is None and "retained_gateway_custody" not in intent)
                  or (custody is not None and intent.get("retained_gateway_custody") == custody[1]["sha256"]),
                  "original retained gateway declaration changed")
    new = [row for row in records if row["Id"] not in old_ids]
    names = set(intent["roles"].values())
    gate._require(len(new) == 2 and {_service(row) for row in new} == names,
                  "unexpected, missing or duplicate candidate container")
    by_role = {role: next(row for row in new if _service(row) == name)
               for role, name in intent["roles"].items()}
    gate._require(len({row["Id"] for row in by_role.values()}) == 2, "candidate role identity aliases")
    return by_role


def _intent_binding(session, paths, operation_id, private_digest, roles, previous, workers, threads, runtime_paths,
                    *, custody=None):
    result = {"schema": 1, "operation_id": operation_id, "migration_permit_sha256": gate._hash(session.permit),
            "private_contract_sha256": private_digest, "source": session.permit["source"],
            "runtime_root": str(paths[0]), "state": str(paths[1]), "roles": roles,
            "previous_containers": _old_binding(previous), "web_workers": workers, "threads": threads,
            "runtime_paths": runtime_paths, "business_open": False}
    if custody is not None:
        result["retained_gateway_custody"] = custody[1]["sha256"]
    return result


def _new_operation(evidence):
    for name in ("create.intent.json", "created.json", "start.intent.json", "closed.json"):
        path = evidence / name
        gate._require(not gate._present(path) and not gate._present(path.with_name(path.name + ".pending")),
                      "lifecycle already attempted; query original operation only")


def create(session, runtime_root, state, evidence, *, operation_id, paths, web_workers, threads=16, legacy=None):
    """One create command; old candidates and ambiguous ACKs are never reused."""
    roots, directory, contract, manifest, private_digest = _inputs(
        session, runtime_root, state, evidence, operation_id)
    runtime_root, state, evidence = roots
    gate._require(type(web_workers) is int and 1 <= web_workers <= 4 and type(threads) is int
                  and 4 <= threads <= 64, "invalid finite application topology")
    gate._require(isinstance(paths, dict) and set(paths) == {"database", "config", "freeze", "state"}
                  and all(isinstance(v, str) and Path(v).is_absolute() and v == os.path.normpath(v) for v in paths.values()),
                  "exact normalized runtime path mapping required")
    _create_scope(manifest)
    _new_operation(evidence)
    roles = _roles(contract, manifest)
    previous = _before_create(contract, manifest, roles, legacy=legacy, private_digest=private_digest)
    custody = _custody(contract, manifest, previous, legacy=legacy, private_digest=private_digest)
    intent = _intent_binding(session, roots, operation_id, private_digest, roles, previous,
                             web_workers, threads, paths, custody=custody)
    _save(evidence / "create.intent.json", intent)
    session.check(active=True)
    private.compose(contract, directory, session.permit["source"]["image_id"],
                    ["-f", str(directory / "compose.json"), "create", "--no-recreate", "--no-build",
                     "--pull", "never", *sorted(roles.values())])
    session.check(active=True)
    candidates = _project(contract, intent, legacy=legacy, private_digest=private_digest, manifest=manifest)
    descriptor = startup.authorize(session, runtime_root, operation_id=operation_id,
                                   paths=paths, web_workers=web_workers, threads=threads)
    grants = {}
    for slot, role in descriptor["slots"].items():
        grants[slot] = startup.grant(session, runtime_root, state, slot=slot,
            container_id=candidates[role]["Id"],
            attempt_operation_id=gate._hash({"lifecycle": gate._hash(intent), "slot": slot, "attempt": 0}))
    session.recheck_inventory()
    return _save(evidence / "created.json", {"schema": 1, "status": "created_granted",
        "operation_id": operation_id, "intent_sha256": gate._hash(intent),
        "descriptor_sha256": gate._hash(descriptor), "containers": {role: row["Id"] for role, row in candidates.items()},
        "grants": grants, "business_open": False, "durable": True})


def _load(session, runtime_root, state, evidence, operation_id, *, active, legacy=None):
    roots, _directory, contract, manifest, digest = _inputs(
        session, runtime_root, state, evidence, operation_id, active=active)
    runtime_root, state, evidence = roots
    intent = gate._read(evidence / "create.intent.json")
    gate._require(intent["operation_id"] == operation_id and intent["migration_permit_sha256"] == gate._hash(session.permit)
                  and intent["private_contract_sha256"] == digest and intent["runtime_root"] == str(runtime_root)
                  and intent["state"] == str(state), "lifecycle original binding differs")
    created = gate._read(evidence / "created.json")
    descriptor = gate._read(runtime_root / "descriptor.json")
    gate._require(created["status"] == "created_granted" and created["operation_id"] == operation_id
                  and created["intent_sha256"] == gate._hash(intent)
                  and created["descriptor_sha256"] == gate._hash(descriptor)
                  and descriptor["operation_id"] == operation_id, "created/grant evidence differs")
    candidates = _project(contract, intent, legacy=legacy, private_digest=digest, manifest=manifest)
    gate._require(created["containers"] == {role: row["Id"] for role, row in candidates.items()}
                  and set(created["grants"]) == set(descriptor["slots"]), "candidate container identity changed")
    for slot, grant in created["grants"].items():
        gate._require(gate._read(runtime_root / f"{slot}.0.grant.json") == grant
                      and grant["container_id"] == created["containers"][descriptor["slots"][slot]],
                      "original candidate grant changed")
    return evidence, descriptor, candidates, created


def start(session, runtime_root, state, evidence, *, operation_id, legacy=None):
    """Start the exact granted IDs once; an unknown result allows query only."""
    evidence, descriptor, candidates, created = _load(
        session, runtime_root, state, evidence, operation_id, active=True, legacy=legacy)
    intent = evidence / "start.intent.json"
    gate._require(not gate._present(intent) and not gate._present(intent.with_name(intent.name + ".pending")),
                  "candidate start already attempted; query original operation only")
    for role, row in candidates.items():
        startup.verify_container(session, Path(runtime_root), Path(state), descriptor, row["Id"], role, running=False)
        gate._require(row["State"]["Status"] == "created", "candidate was previously started")
    _save(intent, {"schema": 1, "operation_id": operation_id, "created_sha256": gate._hash(created),
                   "container_ids": sorted(created["containers"].values())})
    session.check(active=True)
    private.run(["docker", "start", *sorted(created["containers"].values())])
    return query(session, runtime_root, state, evidence, operation_id=operation_id, legacy=legacy)


def query(session, runtime_root, state, evidence, *, operation_id, legacy=None):
    """Query this operation only; may durably confirm its completed receipt."""
    evidence, descriptor, candidates, created = _load(
        session, runtime_root, state, evidence, operation_id, active=False, legacy=legacy)
    attempted = gate._read(evidence / "start.intent.json")
    gate._require(attempted == {"schema": 1, "operation_id": operation_id,
                  "created_sha256": gate._hash(created), "container_ids": sorted(created["containers"].values())},
                  "start intent differs")
    slots, pending = {}, []
    for slot, grant in created["grants"].items():
        if not gate._present(Path(state) / f"{slot}.0.json"):
            pending.append(slot)
            continue
        slots[slot] = startup.query(session, Path(runtime_root), Path(state), slot=slot, attempt=0,
                                    operation_id=grant["attempt_operation_id"])
        gate._require(slots[slot]["status"] == "closed_initialized", "candidate slot is dead or not confirmed")
    for role, row in candidates.items():
        if row["State"]["Running"]:
            startup.verify_container(session, Path(runtime_root), Path(state), descriptor, row["Id"], role, running=True)
        else:
            gate._require(row["State"]["Status"] == "created" and row["RestartCount"] == 0,
                          "candidate stopped or restarted; no automatic replacement")
    result = {"schema": 1, "status": "closed_initialized" if not pending else "startup_pending",
              "operation_id": operation_id, "created_sha256": gate._hash(created),
              "containers": created["containers"], "slots": slots, "pending_slots": pending,
              "business_open": False, "durable": not pending}
    if not pending:
        _save(evidence / "closed.json", result)
    return result


def verify_active(session, evidence, *, operation_id, legacy=None, gateway=None):
    from services.production_activation_control import ReceiverActivationSession
    gate._require(type(session) is ReceiverActivationSession, "real activation lock capability required")
    contract_path = Path(session.permit["config"]["private_contract_path"])
    loaded = private.load(contract_path, Path(__file__).resolve().parents[1])
    contract, manifest, _digest = loaded
    custody = private.retained_custody(contract, manifest)
    if custody is None:
        return _verify_active(session, evidence, operation_id=operation_id, legacy=legacy, loaded=loaded)
    helper, held = custody
    try:
        helper.verify_gateway(held, gateway, session)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise gate.AdmissionError("real retained gateway switch capability differs") from None
    # The exact GatewaySwitch retains its actual control.lock, verifies the real
    # original route before yield and rechecks that route after our receipt.
    with gateway.retained_verification(session) as receipt:
        try:
            helper.verify_receipt(held, receipt)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise gate.AdmissionError("retained gateway actual route differs") from None
        result = _verify_active(session, evidence, operation_id=operation_id, legacy=legacy,
                                gateway_receipt=receipt, persist=False)
    # A failing post-yield route/control check must leave no success receipt.
    return _save(Path(evidence) / "active-verified.json", result)


def _verify_active(session, evidence, *, operation_id, legacy=None, gateway_receipt=None, persist=True, loaded=None):
    """Independent actual verification under the retained activation capability.

    Only exact retained previous application IDs are excluded from the normal
    runtime verifier. This does not update release registration or resume sender.
    """
    from services.production_activation_control import ReceiverActivationSession
    gate._require(type(session) is ReceiverActivationSession, "real activation lock capability required")
    proof = session.verification_proof(active=False)
    evidence = Path(evidence)
    gate._directory(evidence)
    intent = gate._read(evidence / "create.intent.json")
    created = gate._read(evidence / "created.json")
    closed = gate._read(evidence / "closed.json")
    contract_path = Path(session.permit["config"]["private_contract_path"])
    contract, manifest, digest = loaded or private.load(contract_path, Path(__file__).resolve().parents[1])
    gate._require(intent["operation_id"] == operation_id == created["operation_id"] == closed["operation_id"]
                  and intent["migration_permit_sha256"] == proof["migration_permit_sha256"]
                  and intent["source"] == proof["source"] and intent["private_contract_sha256"] == digest
                  and intent["runtime_root"] == str(session.root) and intent["state"] == str(session.state)
                  and created["descriptor_sha256"] == proof["descriptor_sha256"]
                  and created["intent_sha256"] == gate._hash(intent)
                  and closed["created_sha256"] == gate._hash(created)
                  and closed["status"] == "closed_initialized" and closed["durable"] is True
                  and not closed["pending_slots"] and closed["containers"] == created["containers"],
                  "active runtime differs from original exact create/grant/start")
    candidates = _project(contract, intent, legacy=legacy, private_digest=digest, manifest=manifest)
    gate._require(created["containers"] == {role: row["Id"] for role, row in candidates.items()}
                  and all(item["attempt"] == 0 for item in session.arm["slots"].values()),
                  "active candidate identity or startup attempt changed")
    gate._require(set(created["grants"]) == set(session.descriptor["slots"]), "original slot set changed")
    for slot, grant in created["grants"].items():
        receipt, live = startup._receipt(session.state, session.descriptor, slot, 0)
        gate._require(live and gate._read(session.root / f"{slot}.0.grant.json") == grant
                      and all(receipt.get(key) == value for key, value in grant.items())
                      and receipt["container_id"] == created["containers"][session.descriptor["slots"][slot]],
                      "active slot differs from original exact container grant")
    retained = {old for name in contract["app_services"] for old in contract["review"]["service_mapping"][name]}
    records = private.inspect_project(contract)
    custody = _custody(contract, manifest, records, legacy=legacy, private_digest=digest)
    ignored = [row for row in records if row["Id"] in intent["previous_containers"] and _service(row) in retained]
    gate._require(ignored and all(not row["State"]["Running"] and not row["State"].get("Paused")
                  and not row["State"].get("Restarting") and not row["State"].get("Dead")
                  and row["State"]["Status"] in {"created", "exited"} for row in ignored),
                  "previous application is not retained stopped")
    ignored_ids = {row["Id"] for row in ignored}
    custody_result = None
    if custody is not None:
        helper, held = custody
        gate._require(gateway_receipt is not None, "retained gateway verification lock required")
        ignored_ids.add(held["declaration"]["old_container_id"])
        custody_result = {"old_gateway_id": held["declaration"]["old_container_id"],
            "declaration_sha256": held["sha256"], "configuration_proof_sha256": held["declaration"]["old_configuration_proof_sha256"],
            "gateway_receipt_sha256": helper.verify_receipt(held, gateway_receipt)}
    active = [row for row in records if row["Id"] not in ignored_ids]
    private._verify_runtime_records(contract, manifest, contract_path.parent, proof["source"]["image_id"], active)
    gate._require(session.verification_proof(active=False) == proof, "activation changed during independent verify")
    _project(contract, intent, legacy=legacy, private_digest=digest, manifest=manifest)
    result = {"schema": 1, "status": "actual_runtime_verified", "operation_id": operation_id,
        "activation_sha256": gate._hash(proof), "containers": created["containers"],
        "retained_previous_ids": sorted(row["Id"] for row in ignored), "business_open": True,
        "release_registered": False, "sender_resumed": False, "durable": True}
    if custody_result is not None:
        result["retained_gateway_custody"] = custody_result
    return _save(evidence / "active-verified.json", result) if persist else result
