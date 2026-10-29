#!/usr/bin/env python3
"""Pre-open guardian. It observes a fixed parent, then hands off or stops only."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from services import admission_fence as gate, production_activation as runtime  # noqa: E402
from services import production_admission_lifecycle as life  # noqa: E402
from services.production_activation_control import ReceiverActivationSession  # noqa: E402
from services.production_startup import process_identity  # noqa: E402
from production_sender_driver import NativeSenderActivationTransport, PostgresReader, SenderActivationDriver  # noqa: E402
from scripts import production_admission_supervisor as supervisor  # noqa: E402

SCHEMA = "production-preopen-watchdog-v1"
KEYS = {"schema", "parent", "runtime_root", "migration_root", "state", "activation_operation", "freeze",
        "descriptor_sha256", "permit_sha256", "receiver_generation", "receiver_manifest_sha256", "sender_plan",
        "sender_evidence_root", "sender_url", "token_file", "dsn_file", "config_path", "source_sha256", "clock", "sender_namespace_container_id"}


FROZEN_EVIDENCE = "frozen_pg_snapshot_and_recorded_receipt"
LIVE_EVIDENCE = "live_go_query"


def _closed_evidence(session, sender):
    """Explicit branch: a frozen session's PG locks block Go metadata queries."""
    from scripts.sender_drain_inventory import FrozenPostgres
    operation = sender.plan["binding"]["initial_operation_id"]
    if not session.opened and session.storage is not None:
        frozen = getattr(session.shadow, "sender", None)
        # check_receiver_resume just re-ran FrozenPostgres.check under both locks.
        gate._require(type(frozen) is FrozenPostgres and frozen.connection is not None
                      and frozen.latest_snapshot is not None, "frozen sender PG lock evidence missing")
        sample = frozen.latest_snapshot
        return sample, sender.recorded_initialize_closed(operation, sample), FROZEN_EVIDENCE
    sample = sender.bind()
    return sample, sender.query_initialize_closed(operation), LIVE_EVIDENCE


def prepare(session, sender):
    gate._require(type(session) is ReceiverActivationSession and type(sender) is SenderActivationDriver,
                  "actual closed activation capabilities required")
    session._identity(active=True)
    proof = session.check_receiver_resume()
    sample, receipt, source = _closed_evidence(session, sender)
    gate._require(not session.opened and receipt["state"] == "closed" and receipt["generation"] == 0
                  and sample["gate"] == {"manifest": receipt["manifest"], "generation": 0,
                                         "phase": "closed", "current_operation": receipt["operation_id"]},
                  "watchdog requires actual initial closed sender/receiver")
    gate._require(type(sender.transport) is NativeSenderActivationTransport and type(sender.postgres) is PostgresReader,
                  "native sender connection required for watchdog")
    fence = gate.Fence(session.permit["freeze"]["root"], session.permit["freeze"]["binding"])
    snap = fence.snapshot()
    gate._require(snap["phase"] == "frozen" and snap["generation"] == proof["generation"]
                  and not snap["pending_operations"], "watchdog receiver is not actually frozen")
    value = {"schema": SCHEMA, "parent": process_identity(), "runtime_root": str(session.root),
             "migration_root": str(session.migration_root), "state": str(session.state),
             "activation_operation": session.operation_id, "freeze": session.permit["freeze"],
             "descriptor_sha256": gate._hash(session.descriptor), "permit_sha256": gate._hash(session.permit),
             "receiver_generation": proof["generation"], "receiver_manifest_sha256": gate._hash(fence._manifest()),
             "sender_plan": sender.plan, "sender_evidence_root": str(sender.root),
             "sender_url": f"http://127.0.0.1:{sender.transport.port}", "token_file": str(sender.transport.token_file),
             "dsn_file": str(sender.postgres.path), "sender_namespace_container_id": sender.transport.container_id, "config_path": session.permit["config"]["path"],
             "source_sha256": life.source_sha256(), "clock": session.arm["clock"],
             **supervisor.bridge_timeout_field(sender)}
    root = session.root / "watchdog"
    gate._require(not gate._present(root), "watchdog already prepared; no replacement authority")
    root.mkdir(mode=0o700)
    fd = os.open(root / "lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    gate._write(root / "plan.json", value)
    gate._sync_directory(root.parent)
    return {"plan_path": str(root / "plan.json"), "plan_sha256": gate._hash(value), "closed_evidence_source": source}


def load(path):
    path = Path(path)
    value = runtime.read_confirmed(path)
    gate._require(supervisor.plan_keys_valid(value, KEYS) and value["schema"] == SCHEMA and value["source_sha256"] == life.source_sha256()
                  and path == Path(value["runtime_root"]) / "watchdog/plan.json", "fixed watchdog plan/source required")
    permit = runtime.read_confirmed(Path(value["migration_root"]) / "permit.json")
    descriptor = runtime.read_confirmed(Path(value["runtime_root"]) / "descriptor.json")
    gate._require(gate._hash(permit) == value["permit_sha256"] and permit["freeze"] == value["freeze"]
                  and permit["config"]["path"] == value["config_path"]
                  and gate._hash(descriptor) == value["descriptor_sha256"]
                  and descriptor["migration"]["permit_sha256"] == value["permit_sha256"], "watchdog retained targets differ")
    return value, path.parent


def pulse(plan, root, phase):
    gate._require(phase in {"preopen", "supervising"}, "invalid watchdog phase")
    value = {"schema": SCHEMA, "plan_sha256": gate._hash(plan), "owner": process_identity(),
             "clock": life._clock_now(), "phase": phase}
    gate._write(root / "pulse.json", value)
    return value


def query_ready(path, pid):
    plan, root = load(path)
    value = runtime.read_confirmed(root / "pulse.json")
    gate._require(value["plan_sha256"] == gate._hash(plan) and value["owner"] == process_identity(pid)
                  and value["phase"] == "preopen" and process_identity(plan["parent"]["pid"]) == plan["parent"],
                  "actual pre-open watchdog and parent required")
    life._clock(value["clock"], value["clock"]["wall"] + 8, active=True)
    life._clock({k: v for k, v in plan["clock"].items() if k != "expires_at"}, plan["clock"]["expires_at"], active=True)
    gate._require(not gate._present(root / "STOP.json"), "watchdog has stopped")
    return value


def start(path):
    plan, root = load(path)
    child = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--plan", str(Path(path))],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True, cwd=str(ROOT),
                             env=supervisor.sealed_child_environment(plan))
    gate._write(root / "launch.json", {"pid": child.pid, "plan_sha256": gate._hash(plan), "ready": False})
    return child


def handoff(path, supervisor_path):
    plan, root = load(path)
    launched = runtime.read_confirmed(root / "launch.json")
    query_ready(path, launched["pid"])
    target, _directory, _authority = supervisor.load(supervisor_path)
    gate._require(all(target[key] == plan[key] for key in ("runtime_root", "migration_root", "state", "activation_operation",
                  "freeze", "sender_plan", "sender_evidence_root", "sender_url", "token_file", "dsn_file", "config_path", "sender_namespace_container_id")),
                  "watchdog handoff targets differ")
    value = {"supervisor_plan_path": str(Path(supervisor_path)), "supervisor_plan_sha256": gate._hash(target),
             "watchdog_plan_sha256": gate._hash(plan), "owner": process_identity(launched["pid"])}
    gate._require(not gate._present(root / "handoff.json"), "handoff already attempted; query actual tick")
    gate._write(root / "handoff.json", value)
    return {"pid": launched["pid"], "handoff": "submitted_not_yet_proven"}


def _sender_stop(plan, root):
    """Read exact old resume intent, GET only, then stop the bound OPEN result."""
    from services.controlled_startup import _read_complete
    from miniflux_handoff_adapter import _query_slot
    from handoff_host_cron import locked
    sender = supervisor.sender_from_plan(plan)
    old = sender.plan["binding"]
    retained = root / "restrictive-targets.json"
    if gate._present(retained):
        value = runtime.read_confirmed(retained)
        proof = value["identity"]["receiver"]
        gate._require(value["schema"] == "watchdog-restrictive-only-v1"
                      and value["identity"]["sender_plan_sha256"] == sender.plan_sha256
                      and proof["descriptor_sha256"] == plan["descriptor_sha256"]
                      and proof["operation_id"] == plan["activation_operation"], "retained watchdog target differs")
        return life._stop_sender(root, value, sender)
    sample = sender.bind()
    if sample["gate"] == {"manifest": old["manifest"], "generation": old["generation"],
                          "phase": "closed", "current_operation": old["initial_operation_id"]}:
        return {"state": "already_closed", "queue_checked": False}
    intent = _read_complete(sender.control.root / "production_resume.intent.json")
    proof = intent["receiver_activation_proof"]
    gate._require(intent == sender.control._production_intent(intent["closed_operation_id"], intent["closed_generation"], proof)
                  and intent["closed_operation_id"] == old["initial_operation_id"] and intent["closed_generation"] == old["generation"]
                  and proof["descriptor_sha256"] == plan["descriptor_sha256"]
                  and proof["operation_id"] == plan["activation_operation"]
                  and proof["manifest_sha256"] == plan["receiver_manifest_sha256"]
                  and proof["binding_sha256"] == gate._hash(plan["freeze"]["binding"])
                  and proof["generation"] == plan["receiver_generation"] + 1, "watchdog query original resume only")
    with locked(sender.control.root / "control.lock", True):
        sender.control.registration()
        with _query_slot(sender.control.root) as name:
            status, raw = sender.control._exchange("GET", "/operations/" + intent["operation_id"])
            sender.control._save_exchange(name, status, raw)
        receipt = sender.control._confirmed(status, raw, intent["operation_id"], intent["closed_generation"] + 1,
                                            "resume", "open", allow_work=True)
    value = {"schema": "watchdog-restrictive-only-v1", "identity": {"receiver": proof,
             "sender_plan_sha256": sender.plan_sha256, "sender_resume_sha256": gate._hash(intent),
             "sender_gate": {"manifest": receipt["manifest"], "generation": receipt["generation"],
                             "current_operation": receipt["operation_id"], "phase": "open"}}}
    path = root / "restrictive-targets.json"
    if gate._present(path):
        gate._require(runtime.read_confirmed(path) == value, "watchdog original stop targets differ")
    gate._write(path, value)
    return life._stop_sender(root, value, sender)


def _receiver_stop(plan, root, session=None):
    fence = gate.Fence(plan["freeze"]["root"], plan["freeze"]["binding"])
    gate._require(gate._hash(fence._manifest()) == plan["receiver_manifest_sha256"], "watchdog receiver manifest differs")
    with fence._metadata():
        state = gate._read(fence.root / "state.json")
    if state["phase"] == "frozen" and state["generation"] == plan["receiver_generation"] and not state["pending_operation"]:
        return {"state": "already_closed", "queue_checked": False}
    authority = {"identity": {"receiver": {"generation": plan["receiver_generation"] + 1}},
                 "watchdog_plan_sha256": gate._hash(plan)}
    return life._stop_receiver(root, authority, fence, session)


def abort(path, *, receiver_session=None):
    plan, root = load(path)
    result = {"status": "stop_unconfirmed", "queue_checked": False, "drained": False, "errors": {},
              "watchdog_plan_sha256": gate._hash(plan)}
    try:
        gate._write(root / "STOP.json", {"watchdog_plan_sha256": gate._hash(plan), "reason": "parent_or_preopen_window_lost"})
    except Exception as exc:
        result["errors"]["marker"] = life._safe_error(exc)
    for key, action in (("sender", _sender_stop), ("receiver", lambda plan, root: _receiver_stop(plan, root, receiver_session))):
        try:
            result[key] = action(plan, root)
        except Exception as exc:
            result["errors"][key] = life._safe_error(exc)
    if not result["errors"]:
        result["status"] = "producers_stopped_drain_pending"
    gate._write(root / "result.json", result)
    return result


def run(path):
    plan, root = load(path)
    supervisor.verify_child_environment(plan)
    with gate._locked(root / "lock", True, 0.1):
        while True:
            if gate._present(root / "STOP.json") or gate._present(root / "STOP.json.pending"):
                # An external abort may have completed while this process was
                # still observing its parent. Never enter handoff after STOP.
                try:
                    marker = runtime.read_confirmed(root / "STOP.json")
                    result = runtime.read_confirmed(root / "result.json")
                    gate._require(marker == {"watchdog_plan_sha256": gate._hash(plan),
                                            "reason": "parent_or_preopen_window_lost"}
                                  and result["watchdog_plan_sha256"] == gate._hash(plan),
                                  "watchdog original stop result differs")
                    return 0 if (result["status"] == "producers_stopped_drain_pending" and not result["errors"]
                                 and "sender" in result and "receiver" in result
                                 and result["queue_checked"] is False and result["drained"] is False) else 1
                except Exception:
                    return 1  # Missing/uncertain original is not a fresh stop.
            if gate._present(root / "handoff.json"):
                try:
                    handoff_value = runtime.read_confirmed(root / "handoff.json")
                    target, _directory, _authority = supervisor.load(handoff_value["supervisor_plan_path"])
                    gate._require(handoff_value == {
                        "supervisor_plan_path": str(Path(plan["runtime_root"]) / "continuation/supervisor.plan.json"),
                        "supervisor_plan_sha256": gate._hash(target), "watchdog_plan_sha256": gate._hash(plan),
                        "owner": process_identity()}, "watchdog handoff differs")
                    pulse(plan, root, "supervising")
                    code = supervisor.run(handoff_value["supervisor_plan_path"])
                    gate._require(type(code) is int and code == 0, "bound supervisor terminated unsuccessfully")
                    return 0
                except Exception as error:
                    # Go OPEN does not expire. A failed handoff cannot retire
                    # this guardian before it attempts the original sealed stop.
                    try:
                        failure = root / "handoff.failed.json"
                        if not gate._present(failure):
                            gate._write(failure, {"watchdog_plan_sha256": gate._hash(plan),
                                "error": life._safe_error(error), "handoff_succeeded": False})
                    except Exception:
                        pass  # Evidence I/O failure must not skip both stops.
                    try:
                        abort(path)
                    except Exception:
                        pass  # Original binding/I/O loss remains unconfirmed.
                    return 1  # Preserve the failed handoff even if stop worked.
            try:
                gate._require(process_identity(plan["parent"]["pid"]) == plan["parent"], "original parent exited or changed")
                life._clock({k: v for k, v in plan["clock"].items() if k != "expires_at"}, plan["clock"]["expires_at"], active=True)
                pulse(plan, root, "preopen")
            except Exception:
                return 0 if abort(path)["status"] == "producers_stopped_drain_pending" else 1
            time.sleep(2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    return run(parser.parse_args().plan)


if __name__ == "__main__":
    raise SystemExit(main())
