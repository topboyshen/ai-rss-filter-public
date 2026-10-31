#!/usr/bin/env python3
"""Fixed host supervisor for an already activated release; no shell or reopen.

The process survives its launching controller. A killed/unreachable supervisor
cannot make Go OPEN expire: receiver admission expires, originals stay bounded,
and an operator must recover this same plan or perform a reviewed stopped drain.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from services import admission_fence as gate  # noqa: E402
from services import production_activation as runtime  # noqa: E402
from services import production_admission_lifecycle as life  # noqa: E402
from services.production_activation_control import locked_activation  # noqa: E402
from services.production_startup import process_identity  # noqa: E402
from production_sender_driver import (  # noqa: E402
    NativeSenderActivationTransport, SenderActivationDriver, PostgresReader,
)
from scripts import private_release_contract as private  # noqa: E402

SCHEMA = "production-admission-supervisor-v1"
KEYS = {"schema", "authority_sha256", "runtime_root", "migration_root", "state", "activation_operation",
        "freeze", "sender_plan", "sender_evidence_root", "sender_url", "token_file", "dsn_file",
        "config_path", "source_sha256", "interval_seconds", "sender_namespace_container_id"}


def _sealed_environment(permit, *, source_root=None):
    """No ambient interpolation or executable-loader settings enter this check."""
    fixed = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
             "AI_FILTER_CONFIG_PATH": permit["config"]["path"]}
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(fixed)
        path = Path(permit["config"]["private_contract_path"])
        contract, _manifest, digest = private.load(path, source_root or ROOT, require_fresh=False)
        source = permit["source"]
        gate._require(digest == source["private_contract_sha256"]
                      and contract["source_sha"] == source["revision"] and contract["source_tree"] == source["tree"]
                      and contract["files"]["compose.json"] == source["manifest_sha256"]
                      and contract["files"]["runtime.yaml"] == permit["config"]["sha256"]
                      and Path(permit["config"]["path"]) == path.parent / "runtime.yaml",
                      "sealed host configuration source differs")
        raw, _metadata = private.private_file(path.parent / "environment", sealed=True)
        gate._require(private.sha(raw) == contract["files"]["environment"], "sealed child environment changed")
        values = private.environment_values(raw)
        return {**values, **fixed}
    finally:
        os.environ.clear()
        os.environ.update(previous)


def sealed_child_environment(plan):
    permit = runtime.read_confirmed(Path(plan["migration_root"]) / "permit.json")
    gate._require(plan["config_path"] == permit["config"]["path"] and plan["freeze"] == permit["freeze"]
                  and plan["source_sha256"] == life.source_sha256(), "child original permit/config differs")
    return _sealed_environment(permit)


@contextmanager
def sealed_configuration(permit, *, source_root=None):
    """Host main-thread scope for exactly the original sealed configuration."""
    from core import config
    expected = _sealed_environment(permit, source_root=source_root)
    previous_env = dict(os.environ)
    previous = (config.CONFIG_PATH, config.config._cfg, config.config._blacklist_re)
    try:
        os.environ.clear()
        os.environ.update(expected)
        config.CONFIG_PATH = expected["AI_FILTER_CONFIG_PATH"]
        config.config.reload()
        gate._require(gate._hash(config.config.cfg) == permit["config"]["effective_sha256"],
                      "sealed effective host configuration differs")
        yield config.config.cfg
    finally:
        os.environ.clear()
        os.environ.update(previous_env)
        config.CONFIG_PATH, config.config._cfg, config.config._blacklist_re = previous


def verify_child_environment(plan):
    from core import config
    expected = sealed_child_environment(plan)
    gate._require(dict(os.environ) == expected, "child environment differs from original sealed values")
    permit = runtime.read_confirmed(Path(plan["migration_root"]) / "permit.json")
    config.CONFIG_PATH = expected["AI_FILTER_CONFIG_PATH"]
    config.config.reload()
    gate._require(gate._hash(config.config.cfg) == permit["config"]["effective_sha256"],
                  "child effective configuration differs")


def prepare(session, sender):
    root = life._root(session)
    authority = life._authority(root)
    identity, sample = life._proof(session, sender)
    gate._require(identity == authority["identity"], "supervisor preparation identity differs")
    life._capacity(session, sample, authority["policy"])
    gate._require(type(sender.transport) is NativeSenderActivationTransport and type(sender.postgres) is PostgresReader,
                  "fixed native sender transport/reader required")
    plan = {"schema": SCHEMA, "authority_sha256": gate._hash(authority), "runtime_root": str(session.root),
            "migration_root": str(session.migration_root), "state": str(session.state),
            "activation_operation": session.operation_id, "freeze": session.permit["freeze"],
            "sender_plan": sender.plan, "sender_evidence_root": str(sender.root),
            "sender_url": f"http://127.0.0.1:{sender.transport.port}", "token_file": str(sender.transport.token_file),
            "dsn_file": str(sender.postgres.path), "sender_namespace_container_id": sender.transport.container_id, "config_path": session.permit["config"]["path"],
            "source_sha256": life.source_sha256(), "interval_seconds": 15, **bridge_timeout_field(sender)}
    path = root / "supervisor.plan.json"
    if gate._present(path):
        gate._require(runtime.read_confirmed(path) == plan, "supervisor plan changed")
    lock = root / "supervisor.lock"
    if not gate._present(lock):
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        gate._sync_directory(root)
    gate._write(path, plan)
    return {"plan_path": str(path), "plan_sha256": gate._hash(plan)}


def load(path):
    path = Path(path)
    plan = runtime.read_confirmed(path)
    gate._require(plan_keys_valid(plan, KEYS) and plan["schema"] == SCHEMA and plan["interval_seconds"] == 15
                  and plan["source_sha256"] == life.source_sha256(), "fixed supervisor plan/source required")
    root = Path(plan["runtime_root"]) / "continuation"
    gate._require(path == root / "supervisor.plan.json", "supervisor canonical plan path differs")
    authority = life._authority(root)
    gate._require(gate._hash(authority) == plan["authority_sha256"]
                  and gate._hash(plan["sender_plan"]) == authority["identity"]["sender_plan_sha256"]
                  and gate._hash(plan["freeze"]["binding"]) == authority["identity"]["receiver"]["binding_sha256"],
                  "supervisor sealed targets differ")
    targets = authority["identity"]["host_targets"]
    gate._require(all(plan[key] == targets[key] for key in targets if key != "permit_sha256")
                  and gate._hash(runtime.read_confirmed(Path(plan["migration_root"]) / "permit.json")) == targets["permit_sha256"],
                  "supervisor original host paths/permit differ")
    return plan, root, authority


TIMEOUT_KEY = "sender_timeout_seconds"  # CI native bridge only; absent => transport default (2 s)


def bridge_timeout_field(sender):
    """Plan field only when the transport carries a non-default (CI bridge) timeout."""
    value = getattr(sender.transport, "timeout_seconds", NativeSenderActivationTransport.DEFAULT_TIMEOUT_SECONDS)
    if value == NativeSenderActivationTransport.DEFAULT_TIMEOUT_SECONDS:
        return {}
    gate._require(sender.transport.container_id is None, "longer sender timeout is CI bridge only")
    return {TIMEOUT_KEY: value}


def plan_keys_valid(plan, keys):
    extra = set(plan) - keys
    return set(plan) >= keys and extra <= {TIMEOUT_KEY} and (not extra or (
        plan["sender_namespace_container_id"] is None and type(plan[TIMEOUT_KEY]) in (int, float)
        and 0 < plan[TIMEOUT_KEY] <= NativeSenderActivationTransport.MAX_TIMEOUT_SECONDS))


def sender_from_plan(plan):
    return SenderActivationDriver(plan["sender_plan"], NativeSenderActivationTransport(plan["sender_url"], plan["token_file"], container_id=plan["sender_namespace_container_id"],
                                  timeout_seconds=plan.get(TIMEOUT_KEY)),
                                  PostgresReader(plan["dsn_file"], plan["sender_plan"]["postgres_identity"]),
                                  Path(plan["sender_evidence_root"]))


def restrictive_stop(plan_path, *, sender=None):
    """No worker/session readiness prerequisite; exact sealed targets only."""
    plan, root, authority = load(plan_path)
    sender_error = None
    if sender is None:
        try:
            sender = sender_from_plan(plan)
        except Exception as exc:
            sender_error = life._safe_error(exc)
    result = life._stop(root, authority, sender, None, reason_code="supervisor_failure", freeze=plan["freeze"])
    if sender_error:
        result["sender_construction_error"] = sender_error
    return result


def operator_stop_marked(root, authority):
    """Exact durable operator STOP for this authority; anything else is unconfirmed."""
    marker = root / "STOP.json"
    if not (gate._present(marker) or gate._present(marker.with_suffix(".json.pending"))):
        return False
    try:
        from services.controlled_startup import _read_complete
        old = _read_complete(marker)
    except Exception:
        return False
    digest = gate._hash(authority)
    expected = {"schema": life.SCHEMA, "authority_sha256": digest, "reason_code": "operator",
                "sender_stop_operation": gate._hash({"authority": digest, "action": "sender_stop_new"}),
                "receiver_stop_operation": gate._hash({"authority": digest, "action": "receiver_stop_new"})}
    return isinstance(old, dict) and old == expected


def _locked_step(plan, sender):
    with locked_activation(plan["migration_root"], plan["runtime_root"], plan["state"],
                           operation_id=plan["activation_operation"]) as session:
        return life.supervise_step(session, sender)


def tick(plan_path):
    plan, root, authority = load(plan_path)
    sender = None
    try:
        sender = sender_from_plan(plan)
        try:
            result = _locked_step(plan, sender)
        except gate.AdmissionBusy:
            # An exact operator STOP holds the controller lock: never race it with
            # an unlocked competing stop. Wait once more under the same existing
            # lock bound, then re-check and stop through the normal locked path.
            if not operator_stop_marked(root, authority):
                raise
            result = _locked_step(plan, sender)
    except Exception as exc:
        # Context unwinding releases the controller lock before independent stop.
        result = restrictive_stop(plan_path, sender=sender)
        result["trigger"] = life._safe_error(exc)
    value = {"schema": SCHEMA, "plan_sha256": gate._hash(plan), "owner": process_identity(),
             "clock": life._clock_now(), "result": result}
    gate._write(root / "supervisor.tick.json", value)
    return value


def query_started(plan_path, pid):
    plan, root, authority = load(plan_path)
    value = runtime.read_confirmed(root / "supervisor.tick.json")
    gate._require(value["schema"] == SCHEMA and value["plan_sha256"] == gate._hash(plan)
                  and value["owner"] == process_identity(pid) and value["result"]["status"] == "live",
                  "actual live supervisor tick required")
    life._clock(value["clock"], value["clock"]["wall"] + 45, active=True)
    fence = gate.Fence(plan["freeze"]["root"], plan["freeze"]["binding"])
    with fence._metadata():
        receipt = life._window(root, authority, fence, fence._manifest(), gate._read(fence.root / "state.json"))
    gate._require(value["result"]["receipt_sha256"] == gate._hash(receipt), "supervisor observed another current lease")
    return value


def start(plan_path):
    plan, root, _authority = load(plan_path)
    # No shell, no inherited controller locks, no credential/body stdout.
    process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--plan", str(Path(plan_path))],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               close_fds=True, start_new_session=True, cwd=str(ROOT),
                               env=sealed_child_environment(plan))
    gate._write(root / "supervisor.launch.json", {"pid": process.pid, "plan_sha256": gate._hash(plan),
                                                "status": "started_not_yet_proven"})
    return process


def run(plan_path):
    plan, root, _authority = load(plan_path)
    verify_child_environment(plan)
    with gate._locked(root / "supervisor.lock", True, 0.1):
        while True:
            try:
                value = tick(plan_path)
                watchdog_path = Path(plan["runtime_root"]) / "watchdog/plan.json"
                if gate._present(watchdog_path):
                    from scripts import production_admission_watchdog as watchdog
                    watchdog_plan, watchdog_root = watchdog.load(watchdog_path)
                    watchdog.pulse(watchdog_plan, watchdog_root, "supervising")
            except Exception as exc:
                # Preserve a bounded sanitized terminal result; never claim closed.
                gate._write(root / "supervisor.failed.json", {"error": life._safe_error(exc), "stop_confirmed": False})
                return 1
            if value["result"]["status"] != "live":
                return 0 if value["result"]["status"] == "producers_stopped_drain_pending" else 1
            time.sleep(plan["interval_seconds"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.plan)


if __name__ == "__main__":
    raise SystemExit(main())
