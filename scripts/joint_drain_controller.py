"""Bounded instrumented sender/receiver stop, original transfer and zero freeze.

No deploy, migrate, reopen, arbitrary command or caller-supplied empty proof.
The final inventory is historical evidence only: migration must take its full
lock set and independently sample again before any schema/startup operation.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import fcntl
import os
from pathlib import Path
import stat

from services import admission_fence as gate
from services import receiver_drain_storage as disk
from services import receiver_drain_transfer as transfer
from services import sender_drain_contract as originals
from durable_handoff_cutover import encoded, require
from production_receiver_driver import ReceiverDriver
from production_sender_driver import SenderDriver
from receiver_drain_driver import ReceiverDrainDriver
import independent_drain_inventory as inventory

OPERATIONS = {"sender_stop", "receiver_stop", "transfer", "sender_freeze", "receiver_freeze"}
POLICY = {"endpoint", "contract", "config_sha256", "body_limit", "max_batches", "max_replays", "source_sha256"}
MAX_ATTEMPTS = 32


class ReceiverLocks:
    """Live descriptors for the actual receiver locks, never a boolean proof."""
    def __init__(self, fence, stack):
        self.fence, self.pid, self.fds = fence, os.getpid(), {}
        for name in ("controller", "activity"):
            path = fence.root / "locks" / (name + ".lock")
            fd = gate._lock(path, True, 1)
            stack.callback(os.close, fd)
            self.fds[str(path)] = fd

    def check(self, fence):
        require(self.pid == os.getpid() and self.fence.root == fence.root and len(self.fds) == 2, "receiver lock session differs")
        for path, fd in self.fds.items():
            info, current = os.fstat(fd), Path(path).lstat()
            require((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), "receiver lock replaced")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                raise gate.AdmissionError("receiver lock ownership missing")
            finally:
                os.close(probe)


def sources():
    names = ("joint_drain_controller.py", "receiver_drain_driver.py", "production_receiver_driver.py",
             "production_sender_driver.py", "sender_drain_inventory.py", "independent_drain_inventory.py",
             "observe_handoff_capacity.py", "handoff_host_cron.py", "handoff_runtime_adapter.py", "durable_handoff_cutover.py")
    return {**{name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in names},
            **transfer.sources()}


def _file_identity(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, "canonical existing joint lock required")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600, "unsafe joint file")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": 0o600}


def _sqlite_boundary(path):
    """Do not let a nominal read-only WAL observation create missing sidecars."""
    _file_identity(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        header = os.read(fd, 100)
    finally:
        os.close(fd)
    require(header[:16] == b"SQLite format 3\x00" and header[18:20] in (b"\x01\x01", b"\x02\x02"), "unsupported SQLite journal identity")
    return {str(Path(str(path) + suffix)): _file_identity(str(path) + suffix)
            for suffix in ("-wal", "-shm")} if header[18] == 2 else {}


def _same_stop(first, sealed, binding):
    originals.validate_stop_receipt(first, binding)
    originals.validate_stop_receipt(sealed, binding)
    counters = {"active_leases", "uncertain_leases"}
    require({k: v for k, v in first.items() if k not in counters} == {k: v for k, v in sealed.items() if k not in counters},
            "sealed sender stop identity differs")


class JointDrainController:
    def __init__(self, sender, receiver, plan, evidence_root):
        require(isinstance(sender, SenderDriver) and isinstance(receiver, ReceiverDriver), "actual instrumented host drivers required")
        self.sender, self.receiver = sender, receiver
        self.plan = gate._parsed(encoded(plan))
        self.root = disk.directory(evidence_root, create=True)
        require(set(plan) == {"schema", "scope", "sender_plan_sha256", "receiver_plan_sha256", "operations",
                              "transfer_policy", "inventory", "locks", "source_sha256"}
                and plan["schema"] == 1 and type(plan["schema"]) is int
                and plan["scope"] == "instrumented_joint_drain_only"
                and plan["sender_plan_sha256"] == sender.plan_sha256
                and plan["receiver_plan_sha256"] == receiver.plan_sha256
                and plan["source_sha256"] == sources(), "exact source-bound joint plan required")
        ops, policy = plan["operations"], plan["transfer_policy"]
        require(isinstance(ops, dict) and set(ops) == OPERATIONS and len(set(ops.values())) == len(OPERATIONS)
                and all(isinstance(v, str) and gate.HEX64.fullmatch(v) for v in ops.values()), "distinct exact joint operations required")
        require(isinstance(policy, dict) and set(policy) == POLICY and policy["source_sha256"] == transfer.sources(), "fixed transfer policy required")
        transfer._endpoint(policy["endpoint"])
        require(plan["inventory"]["spool"] == receiver.plan["spool"]
                and plan["inventory"]["identities"]["spool"] == receiver.plan["spool_identity"], "joint inventory receiver differs")
        require(set(plan["locks"]) == {"release", "refresh"}
                and all(_file_identity(row["path"]) == row for row in plan["locks"].values()), "bound release/refresh locks required")
        fence = Path(receiver.plan["host_fence_root"])
        require(sender.root.is_relative_to(fence) and sender.root != fence, "sender evidence must use inspected shared fence mount")
        disk.create_lock(self.root / ".lock")
        disk.once(self.root / "plan.json", self.plan)

    def _check(self, *, window=True):
        require(self.plan == disk.confirm(self.root / "plan.json") and self.plan["source_sha256"] == sources(), "joint plan/source changed")
        self.sender.bind()
        self.receiver.bind()
        if window:
            self.sender._window()
            self.receiver._window()

    def _attempt(self, action):
        paths = list(self.root.glob("attempt-*.json*"))
        require(len(paths) < MAX_ATTEMPTS, "joint control attempt budget exhausted")
        disk.once(self.root / f"attempt-{len(paths):02}.json", {"action": action, "plan_sha256": gate._hash(self.plan)})

    def _step(self, name, write, query):
        """Private fixed callsites only; no shell or caller-provided operation."""
        path = self.root / (name + ".json")
        if gate._present(path):
            return disk.confirm(path)
        intent = self.root / (name + ".intent.json")
        value = {"operation_id": self.plan["operations"][name], "plan_sha256": gate._hash(self.plan)}
        attempted = gate._present(intent) or gate._present(intent.with_name(intent.name + ".pending"))
        disk.once(intent, value)
        result = query() if attempted else write()
        disk.once(path, result)
        return result

    def _stop_new(self):
        ops = self.plan["operations"]
        sender = self._step("sender_stop", lambda: self.sender.stop_new(ops["sender_stop"], "joint bounded stop-new"),
                            lambda: self.sender.query_stop_new(ops["sender_stop"]))
        receiver = self._step("receiver_stop", lambda: self.receiver.operate("stop_new", ops["receiver_stop"], "joint bounded stop-new"),
                              lambda: self.receiver.query(ops["receiver_stop"]))
        return {"sender": sender, "receiver": receiver, "queue_checked": False, "switch_authorized": False}

    def stop_new(self):
        with gate._locked(self.root / ".lock", True, 1):
            self._attempt("stop_new")
            self._check()
            return self._stop_new()

    def _bundle(self, stopped):
        path = self.root / "bundle.json"
        bundle = self.sender.root / "originals"
        intent = self.root / "bundle.intent.json"
        if not gate._present(path):
            attempted = gate._present(intent) or gate._present(intent.with_name(intent.name + ".pending"))
            disk.once(intent, {"sender_stop_sha256": gate._hash(stopped["sender"]), "plan_sha256": gate._hash(self.plan)})
            if not attempted:
                result = self.sender.seal_originals(self.plan["operations"]["sender_stop"])
            else:
                # A complete same snapshot may be recovered; partial/no
                # manifest is unknown and is never replaced by a new snapshot.
                _, sha = originals.load_bundle(bundle)
                result = {"bundle": str(bundle), "manifest_sha256": sha, "queue_checked": False, "switch_authorized": False}
            disk.once(path, result)
        result = disk.confirm(path)
        manifest, sha = originals.load_bundle(bundle)
        require(result == {"bundle": str(bundle), "manifest_sha256": sha, "queue_checked": False, "switch_authorized": False}
                and manifest["sender_binding"] == self.sender.plan["binding"]
                and manifest["postgres_identity"] == self.sender.plan["postgres_identity"], "sealed sender provenance differs")
        _same_stop(stopped["sender"], manifest["control_receipt"], self.sender.plan["binding"])
        return result

    def _transfer_driver(self, stopped, bundle):
        relative = Path(bundle["bundle"]).relative_to(self.receiver.plan["host_fence_root"])
        request = {"schema": 1, "operation_id": self.plan["operations"]["transfer"],
            "stop_operation_id": self.plan["operations"]["receiver_stop"], "generation": stopped["receiver"]["generation"],
            "receiver_binding": self.receiver.plan["registration"]["binding"],
            "receiver_manifest_sha256": self.receiver.plan["registration"]["manifest_sha256"],
            "spool": self.receiver.plan["container_spool_root"], "spool_identity": self.receiver.plan["spool_identity"],
            "sender_bundle": str(Path(self.receiver.plan["container_fence_root"]) / relative),
            "sender_manifest_sha256": bundle["manifest_sha256"], "sender_binding": self.sender.plan["binding"],
            "postgres_identity": self.sender.plan["postgres_identity"], **self.plan["transfer_policy"]}
        return ReceiverDrainDriver(self.receiver, request, self.root / "transfer")

    def transfer_originals(self):
        with gate._locked(self.root / ".lock", True, 1):
            self._attempt("transfer_originals")
            self._check()
            stopped = self._stop_new()
            adapter = self._transfer_driver(stopped, self._bundle(stopped))
            intent = adapter.root / "intent.json"
            return adapter.query() if gate._present(intent) or gate._present(intent.with_name(intent.name + ".pending")) else adapter.prepare()

    def freeze_sender(self):
        with gate._locked(self.root / ".lock", True, 1):
            self._attempt("freeze_sender")
            intent = self.root / "sender_freeze.intent.json"
            attempted = gate._present(intent) or gate._present(intent.with_name(intent.name + ".pending"))
            self._check(window=not attempted)
            ops = self.plan["operations"]
            if attempted:
                value = self.sender.query_freeze(ops["sender_freeze"])
                disk.once(self.root / "sender_freeze.json", value)
                return value
            stopped = {"sender": disk.confirm(self.root / "sender_stop.json"), "receiver": disk.confirm(self.root / "receiver_stop.json")}
            adapter = self._transfer_driver(stopped, disk.confirm(self.root / "bundle.json"))
            require(adapter.query()["state"] == "complete", "original transfer incomplete")
            require(not self.sender.inventory(ops["sender_stop"])["blockers"], "sender still draining; no freeze intent written")
            return self._step("sender_freeze", lambda: self.sender.freeze(ops["sender_freeze"], ops["sender_stop"], "joint independently drained sender"),
                              lambda: self.sender.query_freeze(ops["sender_freeze"]))

    def query_transfer(self):
        with gate._locked(self.root / ".lock", True, 1):
            self._attempt("query_transfer")
            self._check(window=False)
            stopped = {"sender": disk.confirm(self.root / "sender_stop.json"), "receiver": disk.confirm(self.root / "receiver_stop.json")}
            adapter = self._transfer_driver(stopped, disk.confirm(self.root / "bundle.json"))
            return adapter.query()

    def confirm_existing_boundaries(self, transfer_adapter):
        """Adopt only independently queried operations owned by the two drivers.

        This supports a staged host restart before final zero freeze. It does
        not import a bundle, claim original coverage, or trust caller receipts.
        """
        with gate._locked(self.root / ".lock", True, 1):
            self._attempt("confirm_existing_boundaries")
            self._check(window=False)
            ops = self.plan["operations"]
            self._existing_transfer_chain(transfer_adapter)
            paused = self.sender.query_freeze(ops["sender_freeze"])
            stopped = self.receiver.query(ops["receiver_stop"])
            require(paused["receipt"]["state"] == "closed" and not paused["inventory"]["blockers"]
                    and stopped["phase"] == "draining", "exact original stop/pause boundaries absent")
            disk.once(self.root / "sender_freeze.json", paused)
            disk.once(self.root / "receiver_stop.json", stopped)
            return {"sender": paused, "receiver": stopped, "queue_checked": False, "switch_authorized": False}

    def _existing_transfer_chain(self, adapter):
        ops, binding = self.plan["operations"], self.sender.plan["binding"]
        require(isinstance(adapter, ReceiverDrainDriver) and adapter.receiver.plan_sha256 == self.receiver.plan_sha256,
                "actual bound transfer driver required")
        request = adapter.request
        require(request["operation_id"] == ops["transfer"] and request["stop_operation_id"] == ops["receiver_stop"]
                and request["sender_binding"] == binding and request["postgres_identity"] == self.sender.plan["postgres_identity"]
                and all(request[k] == v for k, v in self.plan["transfer_policy"].items()), "original transfer operation differs")
        relative = Path(request["sender_bundle"]).relative_to(self.receiver.plan["container_fence_root"])
        bundle = Path(self.receiver.plan["host_fence_root"]) / relative
        require(bundle == self.sender.root / "originals", "original transfer bundle path differs")
        manifest, sha = originals.load_bundle(bundle, check_window=False)
        require(sha == request["sender_manifest_sha256"] and manifest["sender_binding"] == binding
                and manifest["postgres_identity"] == self.sender.plan["postgres_identity"], "original sender bundle identity differs")
        # Go can query only its current operation after full pause. Preserve
        # the earlier exact local stop confirmation and link the live pause
        # intent back to it; never query/reissue an obsolete stop operation.
        stop = self.sender.control._read_stage("stop_new", ops["sender_stop"])
        pause = self.sender.control._read_stage("pause", ops["sender_freeze"])
        saved = disk.confirm(self.sender.control.root / "stop_new.confirmed.json")
        require(saved["intent_sha256"] == hashlib.sha256(encoded(stop)).hexdigest()
                and pause["prior_operation_id"] == ops["sender_stop"]
                and saved["queue_checked"] is saved["legacy_originals_proven"] is saved["switch_authorized"] is False,
                "original stop/pause control chain differs")
        _same_stop(saved["pg_receipt"], manifest["control_receipt"], binding)
        result = adapter.query()
        require(result["state"] == "complete" and result["sender_manifest_sha256"] == sha, "exact original transfer incomplete")
        disk.once(self.root / "original-transfer-chain.json", {"sender_stop": saved, "sealed_stop": manifest["control_receipt"],
                    "pause_intent_sha256": hashlib.sha256(encoded(pause)).hexdigest(), "transfer": result,
                    "sender_manifest_sha256": sha, "queue_checked": False, "switch_authorized": False})

    def freeze_receiver(self):
        with gate._locked(self.root / ".lock", True, 1):
            self._attempt("freeze_receiver")
            operation = self.plan["operations"]["receiver_freeze"]
            attempted = gate._present(self.root / "receiver_freeze.intent.json") or gate._present(self.root / "receiver_freeze.intent.json.pending")
            self._check(window=not attempted)
            require(disk.confirm(self.root / "sender_freeze.json")["receipt"]["operation_id"] == self.plan["operations"]["sender_freeze"],
                    "original sender full-pause receipt missing")
            fence = gate.Fence(self.receiver.plan["host_fence_root"], self.receiver.plan["registration"]["binding"])
            with ExitStack() as stack:
                for name in ("release", "refresh"):
                    row = self.plan["locks"][name]
                    require(_file_identity(row["path"]) == row, "joint outer lock identity changed")
                    stack.enter_context(inventory.existing_lock(Path(row["path"])))
                held = ReceiverLocks(fence, stack)
                return self._freeze_inventory(stack, fence, operation, attempted, held)

    def _freeze_inventory(self, stack, fence, operation, attempted, held):
        pg = None
        def sample_sender():
            nonlocal pg
            pg = stack.enter_context(self.sender.postgres.locked(self.plan["operations"]["sender_freeze"], self.sender.plan["binding"]["manifest"]))
            return pg.snapshot
        sidecars = _sqlite_boundary(self.plan["inventory"]["database"])
        with inventory.locked_inventory(self.plan["inventory"], postgres=sample_sender) as observed:
            require(observed["storage_drained"] and not observed["blockers"], "receiver storage not independently drained; consumers remain enabled")
            pg.check()
            self._check(window=not attempted)
            for path, identity in sidecars.items():
                require(_file_identity(path) == identity, "SQLite sidecar changed during independent inventory")
            stop = disk.confirm(self.root / "receiver_stop.json")
            if not attempted:
                transfer._producer_boundary(fence, {"receiver_manifest_sha256": self.receiver.plan["registration"]["manifest_sha256"],
                                                    "generation": stop["generation"]})
                with fence._metadata():
                    leases, finishes = fence._leases()
                    require(set(leases) == set(finishes) and all(row["outcome"] == "finished" for row in finishes.values()),
                            "receiver activity not independently finished; leave consumers enabled")
                disk.once(self.root / "receiver_freeze.intent.json", {"operation_id": operation, "plan_sha256": gate._hash(self.plan)})
                with fence._metadata():
                    intent = fence._intent("freeze", operation, stop["generation"], "joint independent storage zero")
            else:
                with fence._metadata():
                    intent = fence._load_intent(operation)
                require(intent["request"]["action"] == "freeze" and intent["request"]["expected_generation"] == stop["generation"],
                        "original joint freeze identity differs")
            receipt = _finish_freeze_held(fence, intent, held)
            pg.check()
            self._check(window=False)
            result = {"schema": 1, "receipt": receipt, "inventory": observed, "inventory_sha256": gate._hash(observed),
                      "sender_lock_identity": pg.lock_identity, "queue_checked": True, "switch_authorized": False,
                      "legacy_originals_proven": False, "external_ingress_proven": False}
            # Inventory free-space observations can change; preserve every
            # recovery sample instead of replacing the first proof.
            path = self.root / ("receiver_freeze.json" if not gate._present(self.root / "receiver_freeze.json") else
                                f"receiver_freeze-query-{len(list(self.root.glob('receiver_freeze-query-*.json*'))):02}.json")
            disk.once(path, result)
            return result


def _finish_freeze_held(fence, intent, held):
    """Caller owns the actual controller AND activity EX locks throughout."""
    require(isinstance(held, ReceiverLocks), "live receiver lock session required")
    held.check(fence)
    path = fence.root / "operations" / (intent["request"]["operation_id"] + ".json")
    final = {**intent["after"], "phase": "frozen", "pending_operation": ""}
    with fence._metadata():
        current = gate._state(gate._read(fence.root / "state.json"))
        require(current in (intent["before"], intent["after"], final), "joint freeze state changed")
    if gate._present(path):
        # Existing receipt branch of _advance does not reacquire activity.
        return fence._advance(intent, 0)
    with fence._metadata():
        gate._write(fence.root / "operations" / (intent["request"]["operation_id"] + ".intent.json"), intent)
        gate._write(fence.root / "state.json", intent["after"])
    return fence._complete(intent, final, path)
