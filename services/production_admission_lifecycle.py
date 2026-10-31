"""Retained-identity continuation leases; never reset quotas or reopen a fence.

The host writes this directory below the runtime's read-only grant mount.
Every extension is finite and retains its intent/result. Running out of policy,
evidence, original-receipt or admission capacity requires a real controlled stop.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

from services import admission_fence as gate, production_activation as activation

SCHEMA = "production-admission-continuation-v1"
MAX_AUTHORITY_SECONDS = 30 * 86400
MAX_RENEWALS = 4096
MAX_LEASE_SECONDS = 3600
POLICY_KEYS = {"schema", "operation_id", "approved_by", "not_before", "expires_at", "lease_seconds",
               "renew_before_seconds", "max_renewals", "receiver_stop_records", "receiver_stop_bytes",
               "sender_stop_records", "sender_stop_bytes", "minimum_free_bytes"}


def source_sha256():
    root = Path(__file__).resolve().parents[1]
    names = ("services/production_admission_lifecycle.py", "services/admission_fence.py",
             "services/production_activation.py", "services/production_activation_control.py",
             "scripts/production_sender_driver.py", "scripts/sender_drain_inventory.py",
             "scripts/sender_host_loopback_pg.py",
             "scripts/observe_handoff_capacity.py", "scripts/production_admission_supervisor.py", "scripts/production_admission_watchdog.py", "scripts/sender_namespace_http.py")
    return gate._hash({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})


def validate_policy(policy):
    gate._require(isinstance(policy, dict) and set(policy) == POLICY_KEYS and policy["schema"] == SCHEMA,
                  "exact continuation policy required")
    gate._require(isinstance(policy["approved_by"], str) and 0 < len(policy["approved_by"]) <= 128
                  and isinstance(policy["operation_id"], str) and gate.HEX64.fullmatch(policy["operation_id"]),
                  "reviewed continuation operation required")
    numbers = POLICY_KEYS - {"schema", "operation_id", "approved_by"}
    gate._require(all(type(policy[key]) is int for key in numbers), "integer continuation bounds required")
    gate._require(0 < policy["expires_at"] - policy["not_before"] <= MAX_AUTHORITY_SECONDS
                  and 300 <= policy["lease_seconds"] <= MAX_LEASE_SECONDS
                  and 60 <= policy["renew_before_seconds"] <= policy["lease_seconds"] // 2
                  and 1 <= policy["max_renewals"] <= MAX_RENEWALS, "finite continuation window required")
    gate._require(1 <= policy["receiver_stop_records"] < gate.MAX_ADMISSIONS
                  and 1 <= policy["sender_stop_records"] < 5000
                  and all(1 <= policy[key] <= gate.MAX_BYTES - 1024**2
                          for key in ("receiver_stop_bytes", "sender_stop_bytes"))
                  and policy["minimum_free_bytes"] >= 64 * 1024**2, "conservative original capacity required")
    return policy


def _clock(clock, expires_at, *, active):
    gate._opening_clock(clock)
    if active:
        wall, elapsed = time.time(), gate._elapsed()
        gate._require(clock["wall"] <= wall < expires_at
                      and 0 <= elapsed - clock["elapsed"] < expires_at - clock["wall"]
                      and abs((wall - clock["wall"]) - (elapsed - clock["elapsed"])) <= 30,
                      "continuation clock expired, regressed or changed")


def _clock_now():
    return {"boot_id": gate._boot_identity(), "wall": time.time(), "elapsed": gate._elapsed()}


def _root(session):
    from services.production_activation_control import ReceiverActivationSession
    gate._require(type(session) is ReceiverActivationSession, "live receiver activation capability required")
    session._identity(active=False)
    return session.root / "continuation"


def _sender(sender):
    import sys
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from production_sender_driver import SenderActivationDriver
    gate._require(type(sender) is SenderActivationDriver, "actual sender activation driver required")
    sample = sender.bind()
    original = activation.read_confirmed(sender.control.root / "production_resume.confirmed.json")
    intent = activation.read_confirmed(sender.control.root / "production_resume.intent.json")
    receipt = original["pg_receipt"]
    gate._require(receipt["confirmed"] is True and receipt["durable"] is True
                  and receipt["state"] == "open" and receipt["action"] == "resume"
                  and receipt["operation_id"] == original["operation_id"] == intent["operation_id"]
                  and sample["gate"] == {"manifest": receipt["manifest"], "generation": receipt["generation"],
                                          "phase": "open", "current_operation": receipt["operation_id"]},
                  "actual sender differs from original production resume")
    return sample, original, intent


def _proof(session, sender, *, active=False):
    _root(session)
    proof = session.check_sender_resume(active=active)
    sample, original, intent = _sender(sender)
    gate._require(intent["receiver_activation_proof"] == proof, "sender belongs to another receiver activation")
    gate._require(session.check_sender_resume(active=active) == proof, "receiver changed during sender observation")
    identity = {"receiver": proof, "sender_plan_sha256": sender.plan_sha256,
                "sender_resume_sha256": gate._hash(original), "sender_gate": sample["gate"],
                "postgres_identity": sample["database_identity"],
                "host_targets": {"runtime_root": str(session.root), "migration_root": str(session.migration_root),
                                 "state": str(session.state), "activation_operation": session.operation_id,
                                 "freeze": session.permit["freeze"], "config_path": session.permit["config"]["path"],
                                 "permit_sha256": gate._hash(session.permit)}}
    return identity, sample


def _capacity(session, sample, policy):
    from scripts import observe_handoff_capacity as capacity
    spec = session.permit["inventory"]
    receiver = capacity.sample_receiver(Path(spec["spool"]), spec["identities"]["spool"])
    gate._require(receiver["records"] < policy["receiver_stop_records"]
                  and receiver["reserved_bytes"] < policy["receiver_stop_bytes"]
                  and receiver["physical_free_bytes"] >= policy["minimum_free_bytes"]
                  and sample["rows"] < policy["sender_stop_records"]
                  and sample["charged_bytes"] < policy["sender_stop_bytes"], "original capacity stop threshold reached")
    leases = sample["retained_leases"]
    gate._require(leases["uncertain"] == 0 and leases["records"] <= 9997
                  and sample["retained_operations"] < 9999 and sample["legacy"]["rows"] == 0
                  and sample["states"]["held"] == 0 and sample["expired_unacknowledged"] == 0,
                  "sender uncertainty, held originals or stop budget exhausted")
    fence = gate.Fence(session.permit["freeze"]["root"], session.permit["freeze"]["binding"])
    snap, manifest = fence.snapshot(), fence._manifest()
    gate._require(snap["orphaned"] == snap["uncertain"] == 0 and not snap["pending_operations"]
                  and snap["intake_count"] < manifest["policy"]["max_admissions"]
                  and snap["charged_bytes"] < manifest["policy"]["max_bytes"], "receiver cumulative quota or uncertainty")
    with fence._metadata():
        leases, _finishes = fence._leases()
        intents, pending = fence._operation_inventory()
        gate._require(len(leases) <= gate.MAX_LEASES - 2 and len(intents) <= gate.MAX_OPERATIONS - 2 and not pending,
                      "receiver evidence capacity must retain stop/drain/freeze headroom")
    return {"receiver": receiver, "sender_sha256": gate._hash(sample), "fence": snap}


def authorize(session, sender, policy):
    """Seal one reviewed term while initial activation authority is still current."""
    policy = validate_policy(gate._parsed(gate._encoded(policy)))
    identity, sample = _proof(session, sender, active=True)
    gate._require(policy["not_before"] <= time.time() < policy["expires_at"], "continuation term not current")
    observed = _capacity(session, sample, policy)
    root = _root(session)
    gate._require(not gate._present(root), "continuation authorization attempted; query original only")
    root.mkdir(mode=0o700)
    (root / "leases").mkdir(mode=0o700)
    gate._sync_directory(root / "leases")
    gate._sync_directory(root)
    gate._sync_directory(root.parent)
    value = {"schema": SCHEMA, "policy": policy, "identity": identity,
             "source_sha256": source_sha256(), "clock": _clock_now(), "capacity_sha256": gate._hash(observed)}
    gate._write(root / "authority.intent.json", value)
    gate._write(root / "authority.json", value)
    return {"operation_id": policy["operation_id"], "authority_sha256": gate._hash(value),
            "status": "authorized_no_lease", "business_open_changed": False}


def _authority(root):
    gate._directory(root)
    gate._directory(root / "leases")
    value = activation.read_confirmed(root / "authority.json")
    gate._require(activation.read_confirmed(root / "authority.intent.json") == value
                  and value["schema"] == SCHEMA and value["source_sha256"] == source_sha256(),
                  "continuation authority or source changed")
    validate_policy(value["policy"])
    _clock(value["clock"], value["policy"]["expires_at"], active=False)
    return value


def query_authorize(session, *, operation_id):
    """Durability-confirm only the original authorization; issue no live lease."""
    from services.controlled_startup import _read_complete
    root = _root(session)
    original = _read_complete(root / "authority.intent.json")
    validate_policy(original["policy"])
    gate._require(original["policy"]["operation_id"] == operation_id
                  and original["source_sha256"] == source_sha256(), "query original continuation authority only")
    for name in ("authority.intent.json", "authority.json"):
        path = root / name
        if gate._present(path) or gate._present(path.with_suffix(".json.pending")):
            gate._require(_read_complete(path) == original, "continuation authority conflicts")
        gate._write(path, original)
    return {"operation_id": operation_id, "authority_sha256": gate._hash(_authority(root)),
            "status": "authority_confirmed_no_new_lease", "query_only": True}


def _current(root, authority):
    path = root / "current.json"
    if not gate._present(path) and not gate._present(path.with_suffix(".json.pending")):
        return None
    pointer = activation.read_confirmed(path)
    gate._require(set(pointer) == {"operation_id", "sequence", "receipt_sha256"}
                  and type(pointer["sequence"]) is int and 0 <= pointer["sequence"] < authority["policy"]["max_renewals"]
                  and isinstance(pointer["operation_id"], str) and gate.HEX64.fullmatch(pointer["operation_id"]),
                  "invalid continuation pointer")
    receipt = activation.read_confirmed(root / "leases" / (pointer["operation_id"] + ".json"))
    intent = activation.read_confirmed(root / "leases" / (pointer["operation_id"] + ".intent.json"))
    gate._require(receipt == intent and gate._hash(receipt) == pointer["receipt_sha256"]
                  and receipt["operation_id"] == pointer["operation_id"] and receipt["sequence"] == pointer["sequence"]
                  and receipt["authority_sha256"] == gate._hash(authority), "continuation lease identity differs")
    _receipt(receipt, authority)
    return receipt


def _receipt(value, authority):
    gate._require(set(value) == {"schema", "operation_id", "sequence", "authority_sha256", "previous_sha256",
                                 "clock", "expires_at", "capacity_sha256", "durable"}
                  and value["schema"] == SCHEMA and value["durable"] is True
                  and type(value["sequence"]) is int and 0 <= value["sequence"] < authority["policy"]["max_renewals"]
                  and all(isinstance(value[key], str) and gate.HEX64.fullmatch(value[key])
                          for key in ("operation_id", "authority_sha256", "previous_sha256", "capacity_sha256"))
                  and type(value["expires_at"]) in (int, float), "invalid continuation receipt")
    _clock(value["clock"], value["expires_at"], active=False)


def _window(root, authority, fence, manifest, state, *, active=True):
    gate._require(not gate._present(root / "STOP.json") and not gate._present(root / "STOP.json.pending"),
                  "continuation stopped; no automatic restart")
    identity = authority["identity"]["receiver"]
    gate._require(identity["binding_sha256"] == gate._hash(fence.binding)
                  and identity["manifest_sha256"] == gate._hash(manifest)
                  and state["phase"] == "open" and state["generation"] == identity["generation"]
                  and not state["pending_operation"], "continuation cannot reopen or change generation")
    receipt = _current(root, authority)
    gate._require(receipt is not None, "continuation has no confirmed live lease")
    _clock(authority["clock"], authority["policy"]["expires_at"], active=active)
    _clock(receipt["clock"], receipt["expires_at"], active=active)
    gate._require(receipt["expires_at"] <= authority["policy"]["expires_at"]
                  and 0 < receipt["expires_at"] - receipt["clock"]["wall"] <= authority["policy"]["lease_seconds"],
                  "continuation lease exceeds approved term")
    return receipt


def runtime_window(fence, manifest, state):
    """Called under real admission metadata lock; grant mount stays read-only."""
    from services import production_startup as startup
    if not startup.enabled():
        return None
    import os
    root = startup._path(os.environ.get(startup.ENV_ROOT, ""), directory=True)
    _watchdog_window(root)
    directory = root / "continuation"
    if not gate._present(directory):
        return None
    descriptor = gate._read(root / "descriptor.json")
    authority = _authority(directory)
    gate._require(authority["identity"]["receiver"]["descriptor_sha256"] == gate._hash(descriptor)
                  and descriptor["freeze"]["root"] == str(fence.root)
                  and descriptor["freeze"]["binding"] == fence.binding, "continuation targets another actual fence")
    receipt = _window(directory, authority, fence, manifest, state)
    return {"not_before": receipt["clock"]["wall"], "expires_at": receipt["expires_at"], "clock": receipt["clock"]}


def _prior_window(root, authority, fence):
    current = _current(root, authority)
    manifest, state = fence._manifest(), gate._read(fence.root / "state.json")
    if current is None:
        _clock(state["opening_clock"], manifest["policy"]["expires_at"], active=True)
        gate._require(state["phase"] == "open" and not state["pending_operation"], "initial lease requires open fence")
    else:
        _window(root, authority, fence, manifest, state)
    return current


def _inventory(root, maximum):
    files = list(gate._bounded_paths(root / "leases", maximum * 3 + 1))
    for path in files:
        gate._require(gate.HEX64.fullmatch(path.name[:64])
                      and path.name[64:] in {".intent.json", ".json"}, "continuation partial/unknown evidence")
    return files


def renew(session, sender, *, operation_id, expected_sequence):
    """One new lease, before the prior one expires; never restart an expired term."""
    root = _root(session)
    authority = _authority(root)
    gate._require(isinstance(operation_id, str) and gate.HEX64.fullmatch(operation_id)
                  and type(expected_sequence) is int, "exact continuation operation/CAS required")
    _clock(authority["clock"], authority["policy"]["expires_at"], active=True)
    identity, sample = _proof(session, sender)
    gate._require(identity == authority["identity"], "retained receiver or sender changed")
    observed = _capacity(session, sample, authority["policy"])
    fence = gate.Fence(session.permit["freeze"]["root"], session.permit["freeze"]["binding"])
    with fence._metadata():
        prior = _prior_window(root, authority, fence)
        sequence = prior["sequence"] if prior else -1
        gate._require(sequence == expected_sequence and sequence + 1 < authority["policy"]["max_renewals"],
                      "continuation CAS or retained evidence budget exhausted")
        gate._require(len(_inventory(root, authority["policy"]["max_renewals"])) == 2 * (sequence + 1),
                      "unknown continuation attempt; query original only")
        path = root / "leases" / (operation_id + ".intent.json")
        gate._require(not gate._present(path) and not gate._present(path.with_suffix(".json.pending")),
                      "continuation operation attempted; query only")
        clock = _clock_now()
        gate._require(prior is None or prior["expires_at"] - clock["wall"] <= authority["policy"]["renew_before_seconds"],
                      "continuation renewal is premature")
        receipt = {"schema": SCHEMA, "operation_id": operation_id, "sequence": sequence + 1,
                   "authority_sha256": gate._hash(authority), "previous_sha256": gate._hash(prior),
                   "clock": clock, "expires_at": min(clock["wall"] + authority["policy"]["lease_seconds"], authority["policy"]["expires_at"]),
                   "capacity_sha256": gate._hash(observed), "durable": True}
        gate._require(prior is None or receipt["expires_at"] > prior["expires_at"], "continuation cannot extend within this term")
        gate._write(path, receipt)
        return _publish(root, receipt)


def _publish(root, receipt):
    gate._write(root / "leases" / (receipt["operation_id"] + ".json"), receipt)
    gate._write(root / "current.json", {"operation_id": receipt["operation_id"], "sequence": receipt["sequence"],
                                       "receipt_sha256": gate._hash(receipt)})
    return receipt


def query(session, sender, *, operation_id):
    """Confirm only an existing operation; expired old authority never gains new work."""
    from services.controlled_startup import _read_complete
    root = _root(session)
    authority = _authority(root)
    gate._require(isinstance(operation_id, str) and gate.HEX64.fullmatch(operation_id), "exact continuation query required")
    path = root / "leases" / (operation_id + ".intent.json")
    original = _read_complete(path)
    gate._require(original["operation_id"] == operation_id and original["authority_sha256"] == gate._hash(authority),
                  "continuation query binding differs")
    _receipt(original, authority)
    final = root / "leases" / (operation_id + ".json")
    if gate._present(final) or gate._present(final.with_suffix(".json.pending")):
        gate._require(_read_complete(final) == original, "continuation final conflicts with original intent")
    current_path = root / "current.json"
    pointer = _read_complete(current_path) if gate._present(current_path) or gate._present(current_path.with_suffix(".json.pending")) else None
    fence = gate.Fence(session.permit["freeze"]["root"], session.permit["freeze"]["binding"])
    observed = False
    if pointer is None or pointer["operation_id"] != operation_id:
        identity, sample = _proof(session, sender)
        gate._require(identity == authority["identity"], "continuation identities changed during recovery")
        _capacity(session, sample, authority["policy"])
        observed = True
    with fence._metadata():
        # Query and renewal share the same real CAS lock. Never roll a pointer
        # back when a newer lease commits between the first read and fsync.
        pointer = _read_complete(current_path) if gate._present(current_path) or gate._present(current_path.with_suffix(".json.pending")) else None
        if pointer is None or pointer["operation_id"] != operation_id:
            gate._require(observed, "continuation pointer changed during query; preserve original")
            prior = _prior_window(root, authority, fence)
            gate._require(gate._hash(prior) == original["previous_sha256"], "continuation predecessor changed")
            _clock(original["clock"], original["expires_at"], active=True)
            gate._write(path, original)
            return _publish(root, original)
        gate._require(pointer == {"operation_id": operation_id, "sequence": original["sequence"],
                                  "receipt_sha256": gate._hash(original)}, "continuation result pointer differs")
        gate._write(path, original)
        result = _publish(root, original)
    return {**result, "query_only": True, "expired": time.time() >= result["expires_at"],
            "current_identity_or_readiness_proven": False}


def _safe_error(exc):
    return {"type": type(exc).__name__, "message_sha256": hashlib.sha256(str(exc).encode()).hexdigest()}


def _stop_plan(root, authority, sender):
    """A fresh bounded *restrictive* command, retained once; no new OPEN authority."""
    from services.controlled_startup import _read_complete
    path = root / "shutdown-plan.json"
    if gate._present(path) or gate._present(path.with_suffix(".json.pending")):
        value = _read_complete(path)
    else:
        value = gate._parsed(gate._encoded(sender.plan))
        original = authority["identity"]["sender_gate"]
        value["binding"]["generation"] = original["generation"]
        value["binding"]["initial_operation_id"] = original["current_operation"]
        value["window"] = {"not_before": int(time.time()), "expires_at": int(time.time()) + 120}
    expected = gate._parsed(gate._encoded(sender.plan))
    expected["binding"]["generation"] = authority["identity"]["sender_gate"]["generation"]
    expected["binding"]["initial_operation_id"] = authority["identity"]["sender_gate"]["current_operation"]
    expected["window"] = value["window"]
    gate._require(value == expected and 0 < value["window"]["expires_at"] - value["window"]["not_before"] <= 120,
                  "restrictive shutdown plan changed")
    gate._write(path, value)
    return value


def _stop_sender(root, authority, sender):
    from production_sender_driver import SenderDriver, NativeSenderActivationTransport, NativeSenderTransport
    gate._require(sender.plan_sha256 == authority["identity"]["sender_plan_sha256"], "shutdown sender plan differs")
    transport = sender.transport
    if type(transport) is NativeSenderActivationTransport:
        transport = NativeSenderTransport(f"http://127.0.0.1:{transport.port}", transport.token_file, container_id=transport.container_id,
                                          timeout_seconds=transport.timeout_seconds)
    driver = SenderDriver(_stop_plan(root, authority, sender), transport, sender.postgres, root / "shutdown-sender")
    operation = gate._hash({"authority": gate._hash(authority), "action": "sender_stop_new"})
    path = driver.control.root / "stop_new.intent.json"
    attempted = gate._present(path) or gate._present(path.with_suffix(".json.pending"))
    receipt = (driver.query_stop_new(operation) if attempted else driver.stop_new(operation, "continuation controlled stop"))
    return {"operation_id": operation, "receipt": receipt, "drained": False}


def _stop_receiver(root, authority, fence, session=None):
    operation = gate._hash({"authority": gate._hash(authority), "action": "receiver_stop_new"})
    generation = authority["identity"]["receiver"]["generation"]
    path = fence.root / "operations" / (operation + ".intent.json")
    attempted = gate._present(path) or gate._present(path.with_suffix(".json.pending"))
    if session is None:
        receipt = (fence.query_operation(operation) if attempted else
                   fence.stop_new(operation, generation, "continuation controlled stop"))
    else:
        # Real activation session owns this exact controller lock already.
        session._identity(active=False)
        with fence._metadata():
            intent = (fence._load_intent(operation) if attempted else
                      fence._intent("stop_new", operation, generation, "continuation controlled stop"))
        receipt = fence._advance(intent, 0.1)
    return {"operation_id": operation, "receipt": receipt, "drained": False}


def _stop(root, authority, sender, fence, *, reason_code, session=None, freeze=None):
    gate._require(reason_code in {"operator", "supervisor_failure", "term_complete", "capacity"}, "fixed stop reason required")
    value = {"schema": SCHEMA, "authority_sha256": gate._hash(authority), "reason_code": reason_code,
             "sender_stop_operation": gate._hash({"authority": gate._hash(authority), "action": "sender_stop_new"}),
             "receiver_stop_operation": gate._hash({"authority": gate._hash(authority), "action": "receiver_stop_new"})}
    marker = root / "STOP.json"
    if gate._present(marker) or gate._present(marker.with_suffix(".json.pending")):
        from services.controlled_startup import _read_complete
        old = _read_complete(marker)
        gate._require(all(old[k] == value[k] for k in value if k != "reason_code"), "original stop differs")
        value = old
    result = {"status": "stop_unconfirmed", "queue_checked": False, "drained": False, "errors": {}}
    # Even a disk sync failure must not prevent independent restrictive calls.
    try:
        gate._write(marker, value)
    except Exception as exc:
        result["errors"]["marker"] = _safe_error(exc)
    for name, action in (("sender", lambda: _stop_sender(root, authority, sender)),
                         ("receiver", lambda: _stop_receiver(root, authority, fence or _shutdown_fence(freeze, authority), session))):
        try:
            result[name] = action()
        except Exception as exc:
            result["errors"][name] = _safe_error(exc)
    if not result["errors"]:
        result["status"] = "producers_stopped_drain_pending"
    gate._write(root / "shutdown-result.json", result)
    return result


def stop_new(session, sender, *, reason_code="operator"):
    """Actually stop both producers, retain consumers/originals; never claim drain."""
    root = _root(session)
    authority = _authority(root)
    return _stop(root, authority, sender, None, reason_code=reason_code, session=session, freeze=session.permit["freeze"])


def supervise_step(session, sender):
    """One bounded real identity/capacity check; failure performs restrictive calls."""
    root = _root(session)
    authority = _authority(root)
    try:
        identity, sample = _proof(session, sender)
        gate._require(identity == authority["identity"], "supervisor live identity changed")
        _capacity(session, sample, authority["policy"])
        fence = gate.Fence(session.permit["freeze"]["root"], session.permit["freeze"]["binding"])
        current = _prior_window(root, authority, fence)
        sequence = current["sequence"] if current else -1
        operation = gate._hash({"authority": gate._hash(authority), "sequence": sequence + 1})
        path = root / "leases" / (operation + ".intent.json")
        if gate._present(path) or gate._present(path.with_suffix(".json.pending")):
            current = query(session, sender, operation_id=operation)
        elif current is None or current["expires_at"] - time.time() <= authority["policy"]["renew_before_seconds"]:
            current = renew(session, sender, operation_id=operation, expected_sequence=sequence)
        return {"status": "live", "authority_sha256": gate._hash(authority), "sequence": current["sequence"],
                "receipt_sha256": gate._hash(current), "expires_at": current["expires_at"], "sample_sha256": gate._hash(sample)}
    except Exception as exc:
        result = stop_new(session, sender, reason_code="supervisor_failure")
        result["trigger"] = _safe_error(exc)
        return result


def initialize(session, sender, policy):
    """Explicit reviewed authority plus its first finite lease, or an exception."""
    authorized = authorize(session, sender, policy)
    operation = gate._hash({"authority": authorized["authority_sha256"], "sequence": 0})
    receipt = renew(session, sender, operation_id=operation, expected_sequence=-1)
    return {"authority": authorized, "lease": receipt}


def _shutdown_fence(freeze, authority):
    fence = gate.Fence(freeze["root"], freeze["binding"])
    gate._require(gate._hash(fence._manifest()) == authority["identity"]["receiver"]["manifest_sha256"],
                  "restrictive stop fence manifest differs")
    return fence


def abort_activation(session, sender, *, reason_code="supervisor_failure"):
    """Restrict an already resumed activation even before continuation is durable.

    This original activation evidence can only authorize stop-new, never renew.
    """
    from services.production_activation_control import ReceiverActivationSession
    gate._require(type(session) is ReceiverActivationSession, "real receiver activation capability required")
    session._identity(active=False)
    if not gate._present(sender.control.root / "production_resume.confirmed.json"):
        from scripts import production_admission_watchdog as watchdog
        return watchdog.abort(session.root / "watchdog/plan.json", receiver_session=session)
    original = gate._read(sender.control.root / "production_resume.confirmed.json")
    intent = gate._read(sender.control.root / "production_resume.intent.json")
    proof, receipt = intent["receiver_activation_proof"], original["pg_receipt"]
    gate._require(proof["operation_id"] == session.operation_id
                  and proof["descriptor_sha256"] == gate._hash(session.descriptor)
                  and proof["binding_sha256"] == gate._hash(session.permit["freeze"]["binding"])
                  and receipt["operation_id"] == original["operation_id"] == intent["operation_id"]
                  and receipt["manifest"] == sender.plan["binding"]["manifest"]
                  and receipt["state"] == "open" and receipt["action"] == "resume"
                  and receipt["confirmed"] is True and receipt["durable"] is True, "original resumed activation differs")
    root = session.root / "activation-abort"
    if not gate._present(root):
        root.mkdir(mode=0o700)
        gate._sync_directory(root)
        gate._sync_directory(root.parent)
    value = {"schema": "production-activation-restrictive-only-v1", "identity": {
        "receiver": proof, "sender_plan_sha256": sender.plan_sha256, "sender_resume_sha256": gate._hash(original),
        "sender_gate": {"manifest": receipt["manifest"], "generation": receipt["generation"],
                        "current_operation": receipt["operation_id"], "phase": "open"}}}
    path = root / "targets.json"
    if gate._present(path):
        gate._require(activation.read_confirmed(path) == value, "original abort targets changed")
    evidence_error = None
    try:
        gate._write(path, value)
    except Exception as exc:
        evidence_error = _safe_error(exc)
    result = _stop(root, value, sender, None, reason_code=reason_code, session=session, freeze=session.permit["freeze"])
    if evidence_error:
        result["abort_target_sync_error"] = evidence_error
        result["status"] = "stop_unconfirmed"
    return result


def _watchdog_window(root):
    directory = root / "watchdog"
    if not gate._present(directory):
        return
    gate._directory(directory)
    gate._require(not gate._present(directory / "STOP.json") and not gate._present(directory / "STOP.json.pending"),
                  "pre-open watchdog has stopped")
    plan = activation.read_confirmed(directory / "plan.json")
    pulse = gate._read(directory / "pulse.json")
    gate._require(plan["schema"] == pulse["schema"] == "production-preopen-watchdog-v1"
                  and plan["source_sha256"] == source_sha256()
                  and pulse["plan_sha256"] == gate._hash(plan) and pulse["phase"] in {"preopen", "supervising"}
                  and plan["descriptor_sha256"] == gate._hash(gate._read(root / "descriptor.json")),
                  "watchdog live grant differs")
    _clock(pulse["clock"], pulse["clock"]["wall"] + (8 if pulse["phase"] == "preopen" else 45), active=True)
    if pulse["phase"] == "preopen":
        _clock({k: v for k, v in plan["clock"].items() if k != "expires_at"}, plan["clock"]["expires_at"], active=True)
