"""One activation of retained production slots; never rerun initialization.

Host permissions are read-only in the container. Runtime intents, original
readiness receipts and failures are retained in the separate writable state.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import threading
import time

from services import admission_fence as gate
from services import production_startup as startup

SCHEMA = "production-business-activation-v1"
PULSE_SECONDS = 2
MAX_PULSE_AGE = 8
_MONITOR = None


def read_confirmed(path):
    """Confirm real durability before visible renamed bytes authorize work."""
    path = Path(path)
    gate._require(gate._present(path) and not gate._present(path.with_name(path.name + ".pending")),
                  "activation record is not confirmed")
    value = gate._read(path)
    gate._confirm_file(path)
    gate._sync_directory(path.parent)
    gate._require(not gate._present(path.with_name(path.name + ".pending")) and gate._read(path) == value,
                  "activation record changed during durability confirmation")
    return value


def arm_record(root, descriptor):
    value = read_confirmed(Path(root) / "activation.arm.json")
    keys = {"schema", "operation_id", "descriptor_sha256", "migration_receipt_sha256", "slots", "clock"}
    gate._require(set(value) == keys and value["schema"] == SCHEMA
                  and gate.HEX64.fullmatch(value["operation_id"])
                  and value["operation_id"] not in {descriptor["operation_id"], descriptor["migration"]["operation_id"]}
                  and value["descriptor_sha256"] == gate._hash(descriptor)
                  and value["clock"] == descriptor["clock"]
                  and set(value["slots"]) == set(descriptor["slots"]), "activation arm binding differs")
    for item in value["slots"].values():
        gate._require(set(item) == {"attempt", "receipt_sha256"} and item["attempt"] in (0, 1)
                      and type(item["attempt"]) is int and gate.HEX64.fullmatch(item["receipt_sha256"]),
                      "activation slot binding differs")
    return value


def paths(descriptor, slot, attempt, *, state=None):
    root = Path(state or descriptor["state_path"])
    prefix = f"activation.{slot}.{attempt}"
    return {name: root / f"{prefix}.{name}.json" for name in ("intent", "ready", "pulse", "failed")}


def _live_receipt(state, slot, item):
    prefix = f"{slot}.{item['attempt']}"
    receipt = read_confirmed(state / f"{prefix}.json")
    intent = state / f"{prefix}.intent.json"
    gate._require(gate._hash(receipt) == item["receipt_sha256"] and read_confirmed(intent) == receipt,
                  "activation original owner differs")
    fd = os.open(intent, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return receipt
        raise gate.AdmissionUncertain("activation original owner is not live")
    finally:
        os.close(fd)


def readiness(root, descriptor, *, fresh=True, state=None):
    """Read actual retained leases and bounded pulses; no reported ready flag."""
    arm = arm_record(root, descriptor)
    result = {}
    for slot, item in arm["slots"].items():
        receipt = _live_receipt(Path(state or descriptor["state_path"]), slot, item)
        files = paths(descriptor, slot, item["attempt"], state=state)
        gate._require(not gate._present(files["failed"]) and not gate._present(files["failed"].with_suffix(".json.pending")),
                      "activation failure retained")
        intent = read_confirmed(files["intent"])
        ready = read_confirmed(files["ready"])
        # A pulse is a bounded liveness observation, not a durable commit.
        # Read one safely opened inode; a concurrent atomic replacement must
        # not invalidate the still-fresh observation from that old inode.
        pulse = gate._read(files["pulse"])
        gate._require(intent == {"operation_id": arm["operation_id"], "arm_sha256": gate._hash(arm),
                                 "receipt_sha256": item["receipt_sha256"]}, "activation intent differs")
        gate._require(ready["schema"] == SCHEMA and ready["operation_id"] == arm["operation_id"]
                      and ready["arm_sha256"] == gate._hash(arm) and ready["slot"] == slot
                      and ready["attempt"] == item["attempt"] and ready["receipt_sha256"] == item["receipt_sha256"]
                      and ready["owner"] == receipt["owner"] and ready["owner_nonce"] == receipt["owner_nonce"]
                      and ready["status"] == "business_prepared" and pulse["ready_sha256"] == gate._hash(ready)
                      and pulse["owner_nonce"] == receipt["owner_nonce"], "activation readiness differs")
        if fresh:
            gate._require(0 <= gate._elapsed() - pulse["elapsed"] <= MAX_PULSE_AGE
                          and 0 <= time.time() - pulse["wall"] <= MAX_PULSE_AGE, "activation readiness expired")
        expected = {"quotes", "dispatcher", "quant_context"} if receipt["role"] == "worker" else set()
        gate._require(set(ready["threads"]) == expected
                      and ready["dependencies"] == ({"redis": True} if expected else {}), "activation subsystems differ")
        if expected:
            gate._require(ready["threads"]["quotes"] is not None and ready["threads"]["dispatcher"] is not None,
                          "required business thread absent")
        result[slot] = {"receipt": receipt, "ready": ready, "pulse": pulse}
    return result


def resume_phase(root, descriptor):
    """Distinguish durable resume progress from confirmed business opening."""
    arm = arm_record(root, descriptor)
    confirmation = read_confirmed(Path(root) / "activation.confirm.json")
    gate._require(confirmation["arm_sha256"] == gate._hash(arm)
                  and confirmation["operation_id"] == arm["operation_id"], "activation confirmation differs")
    fence_root = Path(descriptor["freeze"]["root"])
    operation_path = fence_root / "operations" / (arm["operation_id"] + ".json")
    state = gate._read(fence_root / "state.json")
    fence = gate.Fence(fence_root, descriptor["freeze"]["binding"])
    intent = fence._load_intent(arm["operation_id"])
    gate._require(intent["request"]["action"] == "resume"
                  and intent["request"]["activation_sha256"] == gate._hash(confirmation)
                  and intent["before"]["generation"] == descriptor["freeze"]["generation"], "activation resume intent differs")
    final = {**intent["after"], "pending_operation": ""}
    later = state not in (intent["after"], final)
    if later:
        gate._require(gate._present(operation_path), "unconfirmed activation cannot advance")
    if not gate._present(operation_path) or gate._present(operation_path.with_suffix(".json.pending")):
        return "resuming"
    receipt = read_confirmed(operation_path)
    gate._require(receipt["action"] == "resume" and receipt["operation_id"] == arm["operation_id"]
                  and receipt["activation_sha256"] == gate._hash(confirmation)
                  and receipt["manifest_sha256"] == descriptor["freeze"]["manifest_sha256"]
                  and receipt["generation"] == descriptor["freeze"]["generation"] + 1
                  and receipt["phase"] == "open" and receipt["durable"] is True,
                  "activation resume receipt differs")
    if later:
        return _closing_phase(fence, state, final)
    gate._require(state == final, "activation open generation differs")
    return "open"


def _closing_phase(fence, current, before):
    """Keep existing stop_new/drain/freeze usable after this one activation."""
    operations = set()
    for path in gate._bounded_paths(fence.root / "operations", gate.MAX_OPERATIONS * 3):
        if path.name.endswith((".intent.json", ".intent.json.pending")) and gate.HEX64.fullmatch(path.name[:64]):
            operations.add(path.name[:64])
    for action in ("stop_new", "freeze"):
        matches = [fence._load_intent(op) for op in operations]
        matches = [value for value in matches if value["request"]["action"] == action and value["before"] == before]
        gate._require(len(matches) == 1, "activation closing lineage differs")
        intent = matches[0]
        final = {**intent["after"], "phase": "frozen" if action == "freeze" else "draining", "pending_operation": ""}
        path = fence.root / "operations" / (intent["request"]["operation_id"] + ".json")
        if current in (intent["after"], final):
            if not gate._present(path) or gate._present(path.with_suffix(".json.pending")):
                return "closing"
            receipt = read_confirmed(path)
            gate._require(receipt["request_sha256"] == intent["request_sha256"] and receipt["action"] == action
                          and receipt["phase"] == final["phase"] and receipt["generation"] == final["generation"]
                          and receipt["durable"] is True and current == final, "closing receipt differs")
            return final["phase"]
        gate._require(gate._present(path), "closing predecessor unconfirmed")
        receipt = read_confirmed(path)
        gate._require(receipt["request_sha256"] == intent["request_sha256"] and receipt["durable"] is True,
                      "closing predecessor receipt differs")
        before = final
    raise gate.AdmissionError("activation generation advanced outside reviewed closure")


def resumed(root, descriptor):
    gate._require(resume_phase(root, descriptor) in {"open", "draining", "frozen"}, "activation resume is not confirmed")
    arm = arm_record(root, descriptor)
    return read_confirmed(Path(descriptor["freeze"]["root"]) / "operations" / (arm["operation_id"] + ".json"))


def require_business_ready():
    """Additional admission condition for production-startup processes only."""
    if not startup.enabled():
        return
    root = startup._path(os.environ.get(startup.ENV_ROOT, ""), directory=True)
    descriptor = gate._read(root / "descriptor.json")
    resumed(root, descriptor)
    readiness(root, descriptor)


class RuntimeActivation:
    """Same-process, one-shot start intent; a failure never retries callbacks."""
    def __init__(self):
        gate._require(len(startup._OWNERS) == 1, "activation requires exactly one retained owner")
        self.key = next(iter(startup._OWNERS))
        self.receipt = startup.check_owner(self.key)
        self.root, self.descriptor = startup.load()
        self.arm = arm_record(self.root, self.descriptor)
        startup._clock(self.arm["clock"], active=True)
        permission = read_confirmed(self.root / "activation.prepare.json")
        gate._require(permission == {"operation_id": self.arm["operation_id"], "arm_sha256": gate._hash(self.arm)},
                      "activation prepare permission differs")
        slot = self.receipt["slot"]
        item = self.arm["slots"][slot]
        gate._require(item == {"attempt": self.receipt["attempt"], "receipt_sha256": gate._hash(self.receipt)},
                      "activation permission targets another owner")
        self.files = paths(self.descriptor, slot, item["attempt"])
        gate._require(startup._unattempted(self.files.values()), "activation attempted; query original operation")
        gate._write(self.files["intent"], {**permission, "receipt_sha256": item["receipt_sha256"]})
        self.threads, self.ready, self.stop = {}, None, threading.Event()

    def prepared(self, threads, dependencies):
        startup.check_owner(self.key)
        self.threads = dict(threads)
        identities = {}
        for name, thread in self.threads.items():
            gate._require(thread is None or thread.is_alive(), "business thread failed during prepare")
            identities[name] = startup.process_identity(thread.native_id) if thread is not None else None
        receipt = self.receipt
        self.ready = {"schema": SCHEMA, "operation_id": self.arm["operation_id"], "arm_sha256": gate._hash(self.arm),
                      "slot": receipt["slot"], "attempt": receipt["attempt"], "receipt_sha256": gate._hash(receipt),
                      "owner": receipt["owner"], "owner_nonce": receipt["owner_nonce"], "threads": identities,
                      "dependencies": dependencies, "status": "business_prepared"}
        gate._write(self.files["ready"], self.ready)
        self.pulse()

    def pulse(self):
        startup.check_owner(self.key)
        for name, thread in self.threads.items():
            gate._require(thread is None or (thread.is_alive()
                          and startup.process_identity(thread.native_id) == self.ready["threads"][name]),
                          "business subsystem died or changed; automatic restart forbidden")
        gate._write(self.files["pulse"], {"ready_sha256": gate._hash(self.ready), "owner_nonce": self.receipt["owner_nonce"],
                                         "wall": time.time(), "elapsed": gate._elapsed()})

    def fail(self, error):
        if not gate._present(self.files["failed"]):
            gate._write(self.files["failed"], {"operation_id": self.arm["operation_id"], "arm_sha256": gate._hash(self.arm),
                                              "error_type": type(error).__name__, "retry": False})
        self.stop.set()

    def opened(self):
        state = gate._read(Path(self.descriptor["freeze"]["root"]) / "state.json")
        if state["phase"] == "frozen" and state["generation"] == self.descriptor["freeze"]["generation"]:
            startup._clock(self.arm["clock"], active=True)
            return False
        return resume_phase(self.root, self.descriptor) in {"open", "draining", "frozen"}


def permission_present():
    root = Path(os.environ.get(startup.ENV_ROOT, ""))
    return gate._present(root / "activation.prepare.json")


def start_web_monitor():
    global _MONITOR
    gate._require(_MONITOR is None, "web activation monitor already started")
    def observe():
        active = None
        try:
            while not permission_present():
                startup.check_owner(next(iter(startup._OWNERS)))
                time.sleep(PULSE_SECONDS)
            active = RuntimeActivation()
            active.prepared({}, {})
            while not active.stop.wait(PULSE_SECONDS):
                active.pulse()
        except Exception as error:
            if active is not None:
                active.fail(error)
    _MONITOR = threading.Thread(target=observe, name="production-activation", daemon=True)
    _MONITOR.start()
