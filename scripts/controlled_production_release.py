"""Single-attempt host release through actual migration/start/activation APIs.

Inputs are sealed, exact-source, private files. This does not prepare empty
receipt metadata, stop unidentified old producers, replay history, build images
or recreate services. Those prerequisites must be established before execute;
the actual migration inventory and old-container checks enforce them again.
An uncertain stage is retained and stops, never restarted by this entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from scripts import private_release_contract as private
from services import admission_fence as gate, production_migration as migration
from services import production_container_lifecycle as lifecycle
from services.production_activation_control import arm, locked_activation
from services.production_release_lock import retained_release_lock

ROOT = Path(__file__).resolve().parents[1]
MAX_QUERIES = 32


def _file(record):
    gate._require(isinstance(record, dict) and set(record) == {"path", "sha256"}
                  and gate.HEX64.fullmatch(str(record["sha256"])), "exact private file binding required")
    path = Path(record["path"])
    raw, _identity = private.private_file(path)
    gate._require(hashlib.sha256(raw).hexdigest() == record["sha256"], "private deployment file changed")
    return gate._parsed(raw.decode())


def load_inputs(path, *, active=True):
    raw, _identity = private.private_file(Path(path))
    plan = gate._parsed(raw.decode())
    keys = {"schema", "operation_id", "source_sha", "migration", "finalize", "admission_policy", "sender",
            "runtime_root", "state", "lifecycle_root", "migration_root", "evidence_root",
            "lifecycle_operation_id", "activation_operation_id", "runtime_paths", "web_workers", "threads",
            "readiness_seconds", "max_polls", "legacy", "gateway"}
    gate._require(isinstance(plan, dict) and set(plan) == keys and type(plan["schema"]) is int
                  and plan["schema"] == 1, "exact production deployment plan required")
    identifiers = [plan[name] for name in ("operation_id", "lifecycle_operation_id", "activation_operation_id")]
    gate._require(all(isinstance(value, str) and gate.HEX64.fullmatch(value) for value in identifiers)
                  and len(set(identifiers)) == 3, "distinct production operations required")
    for key in ("runtime_root", "state", "lifecycle_root", "migration_root", "evidence_root"):
        value = Path(plan[key])
        gate._require(value.is_absolute() and value.resolve() == value, "canonical deployment roots required")
        if key != "migration_root":
            gate._directory(value)
    roots = [Path(plan[key]) for key in ("runtime_root", "state", "lifecycle_root", "migration_root", "evidence_root")]
    gate._require(len(set(roots)) == 5 and not any(a.is_relative_to(b) for a in roots for b in roots if a != b),
                  "deployment storage roots alias or overlap")
    gate._require(type(plan["readiness_seconds"]) is int and 1 <= plan["readiness_seconds"] <= 180
                  and type(plan["max_polls"]) is int and 1 <= plan["max_polls"] <= 32,
                  "finite readiness budget required")
    gate._require(type(plan["web_workers"]) is int and 1 <= plan["web_workers"] <= 4
                  and type(plan["threads"]) is int and 4 <= plan["threads"] <= 64, "finite topology required")
    for name in ("legacy", "gateway"):
        record = plan[name]
        gate._require(isinstance(record, dict) and set(record) == {"plan", "evidence_root"},
                      "exact external activation binding required")
        _file(record["plan"])
        directory = Path(record["evidence_root"])
        gate._require(directory.is_absolute() and directory.resolve() == directory,
                      "canonical external activation evidence required")
        gate._directory(directory)
    sender = plan["sender"]
    gate._require(isinstance(sender, dict) and set(sender) == {"plan", "endpoint", "token", "evidence_root"},
                  "exact sender driver inputs required")
    permit, final, policy, sender_plan = (_file(plan["migration"]), _file(plan["finalize"]),
                                           _file(plan["admission_policy"]), _file(sender["plan"]))
    from services import production_admission_lifecycle as lifetime
    lifetime.validate_policy(policy)
    if active:
        gate._require(policy["not_before"] <= time.time() < policy["expires_at"], "continuation policy not current")
    gate._require(plan["source_sha"] == private.source_identity(ROOT)["source_sha"]
                  == permit["source"]["revision"] == final["source_sha"], "final clean deployment source differs")
    gate._require(plan["lifecycle_operation_id"] == permit["parent_operation_id"] == final["lifecycle_operation_id"]
                  and plan["lifecycle_root"] == final["lifecycle_root"], "original lifecycle differs")
    gate._require(permit["sender"]["closed_operation_id"] == final["sender_closed_operation_id"]
                  and permit["sender"]["expected_identity"] == sender_plan["postgres_identity"],
                  "sender migration and resume endpoints differ")
    gate._require(permit["sender"].get("postgres_transport") == sender_plan.get("postgres_transport"),
                  "migration and sender transport capabilities differ")
    gate._require(permit["locks"]["release"]["path"] == str(Path(final["registry_root"]) / "release.lock"),
                  "formal registry release lock differs")
    return plan, permit, final, policy, sender_plan


def _sender(plan, permit, sender_plan):
    # Existing native harness and exact-class capability checks use sibling imports.
    prior = list(sys.path)
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from production_sender_driver import NativeSenderActivationTransport, SenderActivationDriver
        from sender_drain_inventory import PostgresReader
    finally:
        sys.path[:] = prior
    config = plan["sender"]
    transport = NativeSenderActivationTransport(config["endpoint"], config["token"],
                                                container_id=sender_plan["binding"]["component"]["id"])
    reader = PostgresReader.from_plan(Path(permit["sender"]["dsn_file"]), sender_plan)
    return SenderActivationDriver(sender_plan, transport, reader, Path(config["evidence_root"]))


def _wait(query, expected, plan):
    deadline = time.monotonic() + plan["readiness_seconds"]
    pending = {"startup_pending", "activation_pending"}
    for attempt in range(plan["max_polls"]):
        gate._require(time.monotonic() < deadline, "production readiness deadline expired")
        result = query()
        gate._require(time.monotonic() < deadline, "production readiness query exceeded deadline")
        if result.get("status") == expected:
            return result
        gate._require(result.get("status") in pending, "production phase uncertain or failed")
        if attempt + 1 < plan["max_polls"]:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise gate.AdmissionUncertain("production readiness query budget exhausted")


def closed_phase(plan, permit, release_lock):
    """Actual M1 and exact created IDs only, with the outer release flock held."""
    with migration.locked_session(plan["migration_root"], permit, release_lock=release_lock) as session:
        session.authorize()
        completed = session.migrate()
        args = (session, plan["runtime_root"], plan["state"], plan["lifecycle_root"])
        kwargs = {"operation_id": plan["lifecycle_operation_id"]}
        created = lifecycle.create(*args, **kwargs, paths=plan["runtime_paths"],
                                   web_workers=plan["web_workers"], threads=plan["threads"])
        started = lifecycle.start(*args, **kwargs)
        if started["status"] != "closed_initialized":
            started = _wait(lambda: lifecycle.query(*args, **kwargs), "closed_initialized", plan)
        armed = arm(session, plan["runtime_root"], plan["state"], operation_id=plan["activation_operation_id"])
        return {"migration": completed, "created": created, "started": started, "armed": armed}


def _watchdog_ready(prepared):
    from scripts import production_admission_watchdog as watchdog
    child = watchdog.start(prepared["plan_path"])
    root = Path(prepared["plan_path"]).parent
    return _actual_child_receipt(child, lambda: watchdog.query_ready(prepared["plan_path"], child.pid), 8,
                                 receipt_path=root / "pulse.json", stop_paths=(root / "STOP.json",))


def _actual_child_receipt(child, query, seconds, *, receipt_path=None, stop_paths=()):
    deadline = time.monotonic() + seconds
    pending = False
    while time.monotonic() < deadline:
        gate._require(child.poll() is None, "bound guardian exited before actual readiness")
        for path in stop_paths:
            path = Path(path)
            gate._require(not gate._present(path) and not gate._present(path.with_suffix(path.suffix + ".pending")),
                          "bound guardian has stopped or stop is uncertain")
        if receipt_path is not None:
            path = Path(receipt_path)
            # A live child's atomic write shows .pending until its rename; a
            # dead writer is caught by poll() above, a stuck one at deadline.
            pending = gate._present(path.with_suffix(path.suffix + ".pending"))
            if pending or not gate._present(path):
                time.sleep(0.05 if pending else 0.1)
                continue
        value = query()
        if time.monotonic() >= deadline:
            raise gate.AdmissionUncertain("bound guardian readiness query exceeded deadline")
        return value
    gate._require(not pending, "bound guardian readiness write is uncertain")
    raise gate.AdmissionUncertain("bound guardian did not prove readiness")


def active_phase(plan, permit, final, policy, sender, release_lock):
    from scripts import controlled_release_finalize as registration
    from scripts import production_admission_supervisor as supervisor, production_admission_watchdog as watchdog
    from scripts.production_gateway_switch import GatewaySwitch
    from services import production_admission_lifecycle as lifetime
    root = Path(plan["evidence_root"])
    prepared = None
    with locked_activation(plan["migration_root"], plan["runtime_root"], plan["state"],
                           operation_id=plan["activation_operation_id"], release_lock=release_lock) as receiver:
        receiver.prepare()
        _wait(receiver.query, "business_prepared", plan)
        receiver.confirm()
        gateway = GatewaySwitch(receiver, _file(plan["gateway"]["plan"]), plan["gateway"]["evidence_root"]).switch()
        prepared = watchdog.prepare(receiver, sender)
        gate._write(root / "watchdog-prepared.json", prepared)
        ready = _watchdog_ready(prepared)
        try:
            opened = receiver.resume()
            verified = lifecycle.verify_active(receiver, plan["lifecycle_root"], operation_id=plan["lifecycle_operation_id"])
            resumed = sender.production_resume(final["sender_closed_operation_id"], final["sender_generation"], receiver)
            authority = lifetime.initialize(receiver, sender, policy)
            ongoing = supervisor.prepare(receiver, sender)
            gate._write(root / "supervisor-prepared.json", ongoing)
            result = registration.finalize(receiver, sender, final, root / "registration")
            return {"receiver": opened, "runtime": verified, "sender": resumed, "gateway": gateway,
                    "admission_authority": authority, "release": result,
                    "watchdog": prepared, "watchdog_ready": ready, "supervisor": ongoing}
        except BaseException:
            try:
                stopped = watchdog.abort(prepared["plan_path"], receiver_session=receiver)
                gate._write(root / "abort-receipt.json", stopped)
            except BaseException as error:
                gate._write(root / "abort-uncertain.json", {"error_type": type(error).__name__,
                            "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(), "stop_confirmed": False})
            raise


def _legacy(plan):
    from scripts.production_legacy_takeover import locked_takeover
    _file(plan["legacy"]["plan"])
    return locked_takeover(plan["legacy"]["plan"]["path"], plan["legacy"]["evidence_root"])


def _handoff(active):
    from scripts import production_admission_watchdog as watchdog, production_admission_supervisor as supervisor
    submitted = watchdog.handoff(active["watchdog"]["plan_path"], active["supervisor"]["plan_path"])
    pid = submitted["pid"]
    class OriginalGuardian:
        def poll(self):
            from services.production_startup import process_identity
            try:
                process_identity(pid)
            except (OSError, gate.AdmissionError):
                return 1
            return None
    root = Path(active["supervisor"]["plan_path"]).parent
    return _actual_child_receipt(OriginalGuardian(), lambda: supervisor.query_started(active["supervisor"]["plan_path"], pid), 45,
                                 receipt_path=root / "supervisor.tick.json",
                                 stop_paths=(root / "STOP.json", Path(active["watchdog"]["plan_path"]).parent / "STOP.json"))


def _stop_retained(root):
    from scripts import production_admission_watchdog as watchdog
    receipt = root / "abort-receipt.json"
    if gate._present(receipt):
        return gate._read(receipt)
    prepared = root / "watchdog-prepared.json"
    if not gate._present(prepared):
        return {"status": "no_guardian_prepared", "production_stop_claimed": False}
    return watchdog.abort(gate._read(prepared)["plan_path"])


def _sealed_call(path, action):
    from scripts import production_admission_supervisor as supervisor
    _plan, permit, _final, _policy, _sender_plan = load_inputs(path, active=action == "execute")
    # Only these separately bounded formal-gate settings cross to the shell
    # checker. They are never inherited by the watchdog or supervisor.
    gate_settings = {key: os.environ[key] for key in ("RELEASE_TEST_ATTESTATION_DIR", "RELEASE_HEALTH_TIMEOUT_SECONDS",
                                                   "RELEASE_HEALTH_POLL_SECONDS") if key in os.environ}
    with supervisor.sealed_configuration(permit):
        os.environ.update(gate_settings)
        return _execute_bound(path) if action == "execute" else _query_bound(path)


def execute(path):
    return _sealed_call(path, "execute")


def _execute_bound(path):
    from core import config
    from scripts import controlled_release_finalize as registration
    plan, permit, final, policy, sender_plan = load_inputs(path)
    root = Path(plan["evidence_root"])
    intent = root / "deployment.intent.json"
    gate._require(not any(gate._present(root / name) for name in
                  ("deployment.intent.json", "deployment.intent.json.pending", "RESULT.json", "STOP.json")),
                  "deployment already attempted; query the original operation only")
    config.CONFIG_PATH = permit["config"]["path"]
    config.config.reload()
    migration.validate_permit(permit, active=True)
    try:
        with retained_release_lock(permit["locks"]["release"]["path"]) as release_lock:
            registration.check_contract(permit, final, release_lock.fd, health=False)
            # Old runtime stoppage is a separately retained original operation.
            # The production release cannot replace its live writer-lock proof
            # with a caller-created ready/result JSON.
            with _legacy(plan) as legacy:
                legacy.check_stopped()
                gate._write(intent, {"schema": 1, "operation_id": plan["operation_id"], "plan_sha256": gate._hash(plan),
                                    "permit_sha256": gate._hash(permit), "finalize_sha256": gate._hash(final)})
                registration_root = root / "registration"
                registration_root.mkdir(mode=0o700)
                gate._sync_directory(registration_root)
                gate._sync_directory(root)
                sender = _sender(plan, permit, sender_plan)
                sender.query_initialize_closed(final["sender_closed_operation_id"])
                closed = closed_phase(plan, permit, release_lock)
                gate._write(root / "closed-phase.json", closed)
                active = active_phase(plan, permit, final, policy, sender, release_lock)
                legacy.check_stopped()
                release_lock.check()
        live = _handoff(active)
        result = {"schema": 1, "operation_id": plan["operation_id"], "plan_sha256": gate._hash(plan),
                  "status": "deployed_controlled", "source": permit["source"], "closed": closed, "active": active,
                  "supervisor_actual_tick": live, "automatic_rollback": False, "historical_raw_replay": False,
                  "natural_acceptance": "not_run", "supervisor_required": True}
        gate._write(root / "RESULT.json", result)
        return result
    except BaseException as error:
        if not gate._present(intent):
            raise
        stop = None
        try:
            stop = _stop_retained(root)
        except BaseException as stop_error:
            stop = {"status": "stop_unconfirmed", "error_type": type(stop_error).__name__}
        if not gate._present(root / "STOP.json"):
            gate._write(root / "STOP.json", {"operation_id": plan["operation_id"], "error_type": type(error).__name__,
                "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(), "restrictive_stop": stop,
                "next_action": "query_original_operation_only", "automatic_rollback": False})
        raise


def query(path):
    return _sealed_call(path, "query")


def _query_bound(path):
    """Inspect only this attempt; no create/start/prepare/resume or new lease."""
    from core import config
    from scripts import controlled_release_finalize as registration
    plan, permit, final, _policy, sender_plan = load_inputs(path, active=False)
    config.CONFIG_PATH = permit["config"]["path"]
    config.config.reload()
    root = Path(plan["evidence_root"])
    intent = gate._read(root / "deployment.intent.json")
    gate._require(intent == {"schema": 1, "operation_id": plan["operation_id"], "plan_sha256": gate._hash(plan),
                  "permit_sha256": gate._hash(permit), "finalize_sha256": gate._hash(final)}, "original deployment differs")
    with retained_release_lock(permit["locks"]["release"]["path"]) as release_lock:
        queries = [root / f"query-{number:02d}.json" for number in range(MAX_QUERIES)]
        unused = next((value for value in queries if not gate._present(value)
                       and not gate._present(value.with_suffix(".json.pending"))), None)
        gate._require(unused is not None, "deployment query budget exhausted")
        gate._write(unused, {"operation_id": plan["operation_id"], "reserved": True})
        register_intent = root / "registration" / "intent.json"
        if not gate._present(register_intent):
            return {"status": "deployment_uncertain", "operation_id": plan["operation_id"],
                    "next_action": "inspect_retained_stage_evidence", "mutation_replayed": False}
        sender = _sender(plan, permit, sender_plan)
        with locked_activation(plan["migration_root"], plan["runtime_root"], plan["state"],
                               operation_id=plan["activation_operation_id"], release_lock=release_lock) as receiver:
            value = registration.query(receiver, sender, final, root / "registration")
            if gate._present(root / "STOP.json"):
                value = {**value, "status": "registered_but_release_stopped", "release_stop": gate._read(root / "STOP.json"),
                         "current_business_live_claimed": False}
            return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("execute", "query"))
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    result = execute(args.plan) if args.action == "execute" else query(args.plan)
    # Full results are private disk evidence. Standard output contains identity
    # and status only, never contract/config/DSN/token/original message bytes.
    print(private.encoded({"status": result["status"], "operation_id": result["operation_id"]}).decode())
    return 0 if result["status"] in {"deployed_controlled", "registered"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
