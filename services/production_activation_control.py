"""Host-only retained-owner activation and one same-fence production resume.

Migration exits under its original frozen rules. This separate session takes
real locks again and never uses a historical empty sample as a live capability.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import fcntl
import math
import os
from pathlib import Path

from services import admission_fence as gate
from services import production_activation as runtime, production_migration as migration
from services import production_startup_control as startup


def _owned_lock(path, fd):
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise gate.AdmissionError("activation lock no longer held")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise gate.AdmissionError("activation lock has another owner") from error
    finally:
        os.close(probe)


def arm(session, runtime_root, state, *, operation_id, attempts=None):
    completed = startup._session(session)
    root, state = Path(runtime_root), Path(state)
    descriptor = startup._descriptor(session, root)
    gate._require(gate.HEX64.fullmatch(operation_id)
                  and operation_id not in {descriptor["operation_id"], session.permit["operation_id"]},
                  "distinct finite activation operation required")
    attempts = {slot: (attempts or {}).get(slot, 0) for slot in descriptor["slots"]}
    rows = {}
    for slot, attempt in attempts.items():
        receipt, live = startup._receipt(state, descriptor, slot, attempt)
        result = startup.query(session, root, state, slot=slot, attempt=attempt,
                               operation_id=receipt["attempt_operation_id"])
        gate._require(live and result["status"] == "closed_initialized", "all retained startup owners must be live")
        rows[slot] = {"attempt": attempt, "receipt_sha256": gate._hash(receipt)}
    path = root / "activation.arm.json"
    gate._require(runtime.read_confirmed(session.root / "migration.json") == completed
                  and not gate._present(path) and not gate._present(path.with_suffix(".json.pending")),
                  "activation already attempted or migration completion differs")
    value = {"schema": runtime.SCHEMA, "operation_id": operation_id, "descriptor_sha256": gate._hash(descriptor),
             "migration_receipt_sha256": gate._hash(completed), "slots": rows, "clock": descriptor["clock"]}
    gate._write(path, value)
    return value


class ReceiverActivationSession:
    """Live release/controller locks and verified owners, not a serialized flag."""
    def __init__(self, migration_root, runtime_root, state, *, operation_id):
        self.migration_root, self.root, self.state = map(Path, (migration_root, runtime_root, state))
        self.permit = runtime.read_confirmed(self.migration_root / "permit.json")
        self.shadow = migration.LockedMigration(self.migration_root, self.permit)
        self.descriptor = startup._descriptor(self.shadow, self.root, active=False)
        self.arm = runtime.arm_record(self.root, self.descriptor)
        gate._require(self.arm["operation_id"] == operation_id, "query original activation operation only")
        self.operation_id, self.pid = operation_id, os.getpid()
        self.live, self.opened, self.storage = False, False, None
        self.anchor = None
        operation = Path(self.permit["freeze"]["root"]) / "operations" / (operation_id + ".intent.json")
        self.recovering = gate._present(operation) or gate._present(operation.with_suffix(".json.pending"))
        if self.recovering:
            final = operation.with_name(operation_id + ".json")
            self.opened = gate._present(final) and not gate._present(final.with_suffix(".json.pending"))

    def _identity(self, *, active=False):
        gate._require(self.live and self.pid == os.getpid(), "activation session is not live")
        migration.validate_permit(self.permit, active=active)
        runtime.startup._clock(self.arm["clock"], active=active)
        gate._require(runtime.arm_record(self.root, self.descriptor) == self.arm
                      and gate._read(self.root / "descriptor.json") == self.descriptor
                      and gate._hash(runtime.read_confirmed(self.migration_root / "migration.json"))
                      == self.arm["migration_receipt_sha256"], "activation frozen completion changed")
        expected_count = 3 if self.opened else 4
        gate._require(len(self.shadow.fds) == expected_count, "activation lock ownership differs")
        for name, fd in self.shadow.fds:
            expected = self.permit["locks"][name]
            actual, info = migration.file_identity(expected["path"]), os.fstat(fd)
            gate._require(actual == expected and (info.st_dev, info.st_ino) == (expected["device"], expected["inode"]),
                          "activation retained lock changed")
            _owned_lock(expected["path"], fd)
        actual = migration.file_identity(self.shadow.path)
        expected = self.permit["database"]["file"]
        gate._require(all(actual[k] == expected[k] for k in ("device", "inode", "uid", "mode")),
                      "activation database file changed")

    def _owners(self):
        result = {}
        for slot, item in self.arm["slots"].items():
            receipt, live = startup._receipt(self.state, self.descriptor, slot, item["attempt"])
            gate._require(live and gate._hash(receipt) == item["receipt_sha256"], "activation owner died or changed")
            startup._unique_owner(self.state, self.descriptor, receipt)
            startup.verify_container(self.shadow, self.root, self.state, self.descriptor,
                                     receipt["container_id"], receipt["role"], running=True)
            for name in ("owner", "master"):
                gate._require(startup._actual_process(receipt["container_id"], receipt[name]) == receipt[name],
                              "activation actual process differs")
            result[slot] = receipt
        return result

    def _ready(self):
        records = runtime.readiness(self.root, self.descriptor, state=self.state)
        for value in records.values():
            receipt, ready = value["receipt"], value["ready"]
            for identity in ready["threads"].values():
                if identity is not None:
                    gate._require(startup._actual_process(receipt["container_id"], identity) == identity,
                                  "actual prepared subsystem thread differs")
        return {slot: gate._hash(value["ready"]) for slot, value in records.items()}

    def prepare(self):
        gate._require(not self.recovering and not self.opened, "resume attempted; query original operation only")
        self._identity(active=True)
        self.shadow.recheck_inventory(active=True)
        self._owners()
        path = self.root / "activation.prepare.json"
        gate._require(not gate._present(path) and not gate._present(path.with_suffix(".json.pending")),
                      "prepare already attempted; query original activation only")
        value = {"operation_id": self.operation_id, "arm_sha256": gate._hash(self.arm)}
        gate._write(path, value)
        return value

    def query(self):
        self._identity()
        owners = self._owners()
        records, pending, failed = {}, [], []
        for slot, item in self.arm["slots"].items():
            files = runtime.paths(self.descriptor, slot, item["attempt"], state=self.state)
            if gate._present(files["failed"]) or gate._present(files["failed"].with_suffix(".json.pending")):
                failed.append(slot)
            elif not all(gate._present(files[key]) for key in ("intent", "ready", "pulse")):
                pending.append(slot)
        if not pending and not failed:
            records = self._ready()
        return {"schema": runtime.SCHEMA, "operation_id": self.operation_id,
                "status": "activation_uncertain" if failed else "business_prepared" if records else "activation_pending",
                "slots": records, "pending_slots": pending, "failed_slots": failed,
                "owner_receipts": {name: gate._hash(value) for name, value in owners.items()},
                "business_open": self.opened, "sender_resumed": False}

    def confirm(self):
        gate._require(not self.recovering and not self.opened, "resume attempted; query original operation only")
        self._identity(active=True)
        self.shadow.recheck_inventory(active=True)
        result = self.query()
        gate._require(result["status"] == "business_prepared", "actual business subsystems are not ready")
        path = self.root / "activation.confirm.json"
        gate._require(not gate._present(path) and not gate._present(path.with_suffix(".json.pending")),
                      "confirm already attempted; query original activation only")
        value = {"operation_id": self.operation_id, "arm_sha256": gate._hash(self.arm),
                 "slots": result["slots"], "slots_sha256": gate._hash(result["slots"])}
        gate._write(path, value)
        return value

    def check_receiver_resume(self, *, active=True, recovery=False):
        self._identity(active=active)
        self._owners()
        ready = self._ready()
        confirmation = runtime.read_confirmed(self.root / "activation.confirm.json")
        gate._require(confirmation == {"operation_id": self.operation_id, "arm_sha256": gate._hash(self.arm),
                                      "slots": ready, "slots_sha256": gate._hash(ready)}, "activation confirmation changed")
        fence = gate.Fence(self.permit["freeze"]["root"], self.permit["freeze"]["binding"], lock_seconds=0.1)
        gate._require(gate._hash(fence._manifest()) == self.permit["freeze"]["manifest_sha256"], "activation fence manifest changed")
        if not recovery:
            self.shadow.recheck_inventory(active=active)
        elif not self.opened:
            self.shadow.sender.check()
        return {"operation_id": self.operation_id, "generation": self.permit["freeze"]["generation"],
                "activation_sha256": gate._hash(confirmation), "root": str(fence.root), "binding": fence.binding}

    def _release_frozen_storage(self):
        # Sender Go resume takes these same PG locks through another connection.
        # Release storage/PG before activity, retaining the controller boundary.
        self.storage.close()
        self.storage = None
        retained = []
        for name, fd in self.shadow.fds:
            if name == "activity":
                os.close(fd)
            else:
                retained.append((name, fd))
        self.shadow.fds = retained
        self.opened = True

    def resume(self):
        gate._require(not self.recovering and not self.opened, "resume attempted; query original operation only")
        self.check_receiver_resume()
        fence = gate.Fence(self.permit["freeze"]["root"], self.permit["freeze"]["binding"], lock_seconds=0.1)
        result = fence.resume_production(self)
        self._release_frozen_storage()
        return result

    def query_resume(self):
        self._identity()
        fence = gate.Fence(self.permit["freeze"]["root"], self.permit["freeze"]["binding"], lock_seconds=0.1)
        intent = fence._load_intent(self.operation_id)
        confirmation = runtime.read_confirmed(self.root / "activation.confirm.json")
        gate._require(intent["request"]["action"] == "resume"
                      and intent["request"]["activation_sha256"] == gate._hash(confirmation), "query original resume only")
        path = fence.root / "operations" / (self.operation_id + ".json")
        if gate._present(path) and not gate._present(path.with_suffix(".json.pending")):
            receipt = runtime.read_confirmed(path)
            final = {**intent["after"], "pending_operation": ""}
            gate._require(receipt["request_sha256"] == intent["request_sha256"] and receipt["durable"] is True
                          and receipt["phase"] == "open" and receipt["generation"] == final["generation"],
                          "original resume receipt differs")
            # Confirm existing bytes only. An expired or dead owner never gains
            # a new start/open permission from this historical outcome query.
            gate._confirm_file(path)
            gate._sync_directory(path.parent)
            if not self.opened and gate._read(fence.root / "state.json") == final:
                self._release_frozen_storage()
            try:
                self._owners()
                self._ready()
                ready = True
            except (gate.AdmissionClosed, OSError):
                ready = False
            return {**receipt, "current_consumers_ready": ready, "query_only": True}
        try:
            # A pending opening cannot become new business after its finite
            # permission expired or its original consumers became uncertain.
            self.check_receiver_resume(active=True, recovery=True)
        except (gate.AdmissionClosed, OSError):
            return {"operation_id": self.operation_id, "status": "activation_uncertain", "durable": False,
                    "query_only": True, "next_allowed_action": "query_original_operation"}
        result = fence.query_production_resume(self)
        if not self.opened:
            self._release_frozen_storage()
        return result

    def check_sender_resume(self, *, active=True):
        self._identity(active=active)
        gate._require(self.opened and self.storage is None, "receiver open/PG lock release not confirmed")
        self._owners()
        ready = self._ready()
        confirmation = runtime.read_confirmed(self.root / "activation.confirm.json")
        gate._require(confirmation["slots"] == ready and confirmation["slots_sha256"] == gate._hash(ready),
                      "confirmed ready owners differ")
        freeze = self.permit["freeze"]
        receipt = runtime.read_confirmed(Path(freeze["root"]) / "operations" / (self.operation_id + ".json"))
        state = gate._read(Path(freeze["root"]) / "state.json")
        gate._require(receipt["action"] == "resume" and receipt["activation_sha256"] == gate._hash(confirmation)
                      and receipt["generation"] == freeze["generation"] + 1 and receipt["phase"] == "open"
                      and receipt["durable"] is True and state["generation"] == receipt["generation"]
                      and state["phase"] == "open" and not state["pending_operation"], "receiver exact resume not current")
        window = {"not_before": math.ceil(self.arm["clock"]["wall"]), "expires_at": self.arm["clock"]["expires_at"]}
        if active:
            import time
            gate._require(window["not_before"] <= time.time() < window["expires_at"], "sender resume window is not current")
        return {"operation_id": self.operation_id, "descriptor_sha256": gate._hash(self.descriptor),
                "manifest_sha256": freeze["manifest_sha256"], "binding_sha256": gate._hash(freeze["binding"]),
                "generation": receipt["generation"], "slots_sha256": gate._hash(ready),
                "window": window}

    def verification_proof(self, *, active=True):
        summary = self.check_sender_resume(active=active)
        receipt = runtime.read_confirmed(Path(self.permit["freeze"]["root"]) / "operations" / (self.operation_id + ".json"))
        confirmation = runtime.read_confirmed(self.root / "activation.confirm.json")
        return {**summary, "migration_permit_sha256": gate._hash(self.permit), "source": self.permit["source"],
                "resume_operation_id": self.operation_id, "resume_receipt_sha256": gate._hash(receipt),
                "business_open": True, "slots": confirmation["slots"]}


def _close_session(session):
    """Release every owned resource even when final PG verification fails."""
    session.live = False
    actions = []
    if session.storage is not None:
        actions.append(session.storage.close)
    if session.anchor is not None:
        actions.append(session.anchor.close)
    actions.extend(lambda fd=fd: os.close(fd) for _name, fd in reversed(session.shadow.fds))
    failures = []
    for close in actions:
        try:
            close()
        except BaseException as error:
            failures.append(error)
    session.shadow.fds.clear()
    if len(failures) > 1:
        raise failures[0] from BaseExceptionGroup("additional activation cleanup failures", failures[1:])
    if failures:
        raise failures[0]


@contextmanager
def locked_activation(migration_root, runtime_root, state, *, operation_id):
    session = ReceiverActivationSession(migration_root, runtime_root, state, operation_id=operation_id)
    try:
        for name in migration.LOCK_ORDER[:4]:
            if session.opened and name == "activity":
                continue
            expected = session.permit["locks"][name]
            gate._require(migration.file_identity(expected["path"]) == expected, "activation lock changed before acquisition")
            session.shadow.fds.append((name, gate._lock(Path(expected["path"]), True, 0.1)))
        if not session.opened:
            migration._same_database(session.shadow.path, session.permit["database"])
            for suffix in ("-wal", "-shm"):
                gate._require(gate._present(Path(str(session.shadow.path) + suffix)), "activation existing sidecar absent")
            session.anchor = migration._connection(session.shadow.path, readonly=True)
            session.storage = ExitStack()
            migration._retain_inventory(session.shadow, session.storage)
            if not session.recovering:
                session.shadow.check()
                gate._require(gate._hash(session.shadow.query_migration(session.permit["operation_id"]))
                              == session.arm["migration_receipt_sha256"], "activation completed migration differs")
        session.live = True
        session._identity()
        yield session
        session._identity()
        if not session.opened and not session.recovering:
            session.shadow.check()
    finally:
        _close_session(session)
