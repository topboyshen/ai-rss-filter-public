"""Isolation-only orchestration: stop first, retain locks through frozen startup.

There is deliberately no deploy/resume/rollback CLI. This controller connects
the actual runtime adapter, independent raw-storage reader and a separately
bound frozen-start driver. It never turns a released zero-count snapshot into
a switch permit. A lost acknowledgement is queried, never blindly replayed.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = _module("cutover_inventory", ROOT / "scripts/independent_drain_inventory.py")
journal = _module("cutover_journal", ROOT / "scripts/durable_handoff_cutover.py")
fence_module = _module("cutover_fence", ROOT / "services/admission_fence.py")


def sha(value):
    return hashlib.sha256(journal.encoded(value)).hexdigest()


def validate_plan(plan):
    journal.require(isinstance(plan, dict) and set(plan) == {
        "schema", "scope", "run_id", "operation_id", "not_before", "expires_at", "source_sha256",
        "release_lock", "refresh_lock", "inventory", "old_binding", "new_binding", "startup_operation_id"},
        "exact combined plan required")
    journal.require(type(plan["schema"]) is int and plan["schema"] == 1 and plan["scope"] == "isolation",
                    "production execution is not authorized")
    for key, length in (("run_id", 32), ("operation_id", 64), ("startup_operation_id", 64)):
        journal.require(isinstance(plan[key], str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", plan[key]),
                        "invalid combined operation identity")
    for key in ("not_before", "expires_at"):
        journal.require(type(plan[key]) is int, "invalid combined clock")
    journal.require(0 < plan["expires_at"] - plan["not_before"] <= 7200, "combined window budget")
    for key in ("release_lock", "refresh_lock"):
        journal.require(Path(plan[key]).is_absolute(), "absolute existing host locks required")
    journal.require(plan["release_lock"] != plan["refresh_lock"], "host locks alias")
    expected = {"controller": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "inventory": hashlib.sha256((ROOT / "scripts/independent_drain_inventory.py").read_bytes()).hexdigest()}
    journal.require(plan["source_sha256"] == expected, "combined source changed")
    return plan


class FrozenCutover:
    """No live configuration discovery, source mutation or automatic opening."""
    def __init__(self, plan, runtime, old_fence, startup, evidence, *, postgres):
        self.plan = validate_plan(json.loads(json.dumps(plan)))
        self.runtime, self.fence, self.startup, self.postgres = runtime, old_fence, startup, postgres
        self.root = Path(evidence)
        journal.private_directory(self.root, create=True)
        lock = self.root / ".controller.lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            journal.sync_directory(self.root)
        journal.require(self.fence.binding == self.plan["old_binding"], "wrong old fence binding")
        journal.require(startup.binding == self.plan["new_binding"]
                        and startup.operation_id == self.plan["startup_operation_id"], "wrong startup binding")
        self.intent = self.root / (plan["operation_id"] + ".intent.json")
        self.final = self.root / (plan["operation_id"] + ".json")

    def _active(self):
        journal.require(self.plan["not_before"] <= time.time() <= self.plan["expires_at"], "combined window expired")

    def _stopped(self, value):
        journal.require(isinstance(value, dict) and value.get("status") == "listed_producers_stopped"
                        and value.get("remaining_controls") == [] and value.get("scope") == "isolation"
                        and value.get("receipts") and all(row.get("durable") is True
                                                        for row in value["receipts"].values()),
                        "all registered producers must be actually stopped")
        return value

    def _frozen(self):
        value = self.fence.snapshot()
        journal.require(value["binding"] == self.plan["old_binding"] and value["phase"] == "frozen"
                        and all(value[k] == 0 for k in ("active", "orphaned", "uncertain"))
                        and value["pending_operations"] == [], "application activity not safely frozen")
        return value

    def _validated_result(self, intent, actual):
        journal.require(isinstance(actual, dict) and actual.get("status") == "completed_frozen"
                        and actual.get("binding") == self.plan["new_binding"]
                        and actual.get("operation_id") == self.plan["startup_operation_id"]
                        and actual.get("intent_sha256") == sha(intent)
                        and actual.get("durable") is True and actual.get("business_open") is False,
                        "frozen startup not confirmed; query original operation, do not replay")
        result = {"schema": 1, "scope": "isolation", "status": "completed_frozen", "run_id": self.plan["run_id"],
                  "operation_id": self.plan["operation_id"], "plan_sha256": sha(self.plan),
                  "startup": actual, "durable": True, "business_open": False,
                  "switch_authorized": False, "production_verified": False}
        return result

    def _save_result(self, result):
        if self.final.exists():
            journal.require(journal.read_private(self.final) == result, "combined receipt conflicts")
        journal.durable_write(self.final, result)
        return result

    @contextmanager
    def _drained(self):
        with ExitStack() as stack:
            paths = [Path(self.plan[key]) for key in ("release_lock", "refresh_lock")]
            paths.append(self.fence.root / "locks/activity.lock")
            identities = {}
            for path in paths:
                fd = stack.enter_context(storage.existing_lock(path))
                identities[str(path)] = storage._identity(os.fstat(fd))
            frozen = self._frozen()
            rebound = self.runtime.inspect_and_bind()
            inventory = stack.enter_context(storage.locked_inventory(self.plan["inventory"], postgres=self.postgres))
            journal.require(inventory["storage_drained"], "independent drain not complete: " + ",".join(inventory["blockers"]))
            yield frozen, rebound, inventory, identities

    def run(self):
        with storage.existing_lock(self.root / ".controller.lock"):
            return self._run_locked()

    def _run_locked(self):
        self._active()
        journal.require(not self.intent.exists() and not self.intent.with_suffix(".json.pending").exists(),
                        "combined operation already attempted; query only")
        stopped = self._stopped(self.runtime.stop_producers(self.plan["operation_id"], "approved isolated cutover"))
        # Producers and old cron children are stopped BEFORE waiting for locks
        # they may need to finish. Do not acquire release first and deadlock cron.
        with self._drained() as (frozen, rebound, inventory, identities):
            self._active()
            intent = {"schema": 1, "operation_id": self.plan["operation_id"], "plan_sha256": sha(self.plan),
                      "stopped_sha256": sha(stopped), "frozen_sha256": sha(frozen),
                      "runtime_sha256": sha(rebound), "inventory": inventory,
                      "lock_identities": identities,
                      "startup_operation_id": self.plan["startup_operation_id"]}
            journal.durable_write(self.intent, intent)
            # Exactly one invocation after durable intent. An exception or ACK
            # loss leaves intent, and run() can never issue this action again.
            actual = self.startup.start_frozen(sha(intent))
            self._active()
            result = self._validated_result(intent, actual)
        # All context-exit identity checks must succeed before a durable final.
        # Producers and business remain closed after these locks are released.
        return self._save_result(result)

    def query(self):
        """Confirm original startup only. Never re-enter stop, migration or start."""
        with storage.existing_lock(self.root / ".controller.lock"):
            intent = journal.read_private(self.intent)
            journal.require(intent.get("plan_sha256") == sha(self.plan)
                            and intent.get("operation_id") == self.plan["operation_id"], "original cutover intent differs")
            with self._drained() as (_, _, inventory, identities):
                journal.require(identities == intent.get("lock_identities")
                                and inventory["lock_identities"] == intent["inventory"].get("lock_identities"),
                                "original cutover lock identity differs")
                result = self._validated_result(intent, self.startup.query_frozen(sha(intent)))
            return self._save_result(result)


if __name__ == "__main__":
    raise SystemExit("No production CLI: separately reviewed bound runtime drivers are required")
