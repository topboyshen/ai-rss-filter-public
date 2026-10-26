"""Private cross-process application admission fence; disabled unless explicit.

This is not a deployment controller or a queue-empty proof. Intake is stopped
before existing durable consumers drain; freeze then waits for every activity
lease. Uncertain/crashed leases are retained, never automatically cleared.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import fcntl
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time

MAX_RECORD_BYTES = 16384
MAX_LEASES = 8192
MAX_OPERATIONS = 64
MAX_WAIT_SECONDS = 30.0
MAX_ADMISSIONS = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_WINDOW_SECONDS = 7200
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_LOCAL = threading.local()
_CACHE_LOCK = threading.Lock()
_CACHE = {}


class AdmissionClosed(RuntimeError):
    """Admission is closed; this is not authorization to retry a model."""


class AdmissionError(AdmissionClosed):
    """Invalid configuration/identity or unsafe persistent state."""


class AdmissionBusy(AdmissionClosed):
    """Bounded lock wait expired; existing work was not killed."""


class AdmissionUncertain(AdmissionClosed):
    """Original evidence is incomplete; no recovery is assumed."""


def _require(condition, message):
    if not condition:
        raise AdmissionError(message)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _boot_identity():
    if sys.platform.startswith("linux"):
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        _require(re.fullmatch(r"[0-9a-f-]{36}", value), "invalid host boot identity")
        return "linux:" + value
    if sys.platform == "darwin":
        class Timeval(ctypes.Structure):
            _fields_ = [("seconds", ctypes.c_long), ("microseconds", ctypes.c_int)]
        value, size = Timeval(), ctypes.c_size_t(ctypes.sizeof(Timeval))
        result = ctypes.CDLL(None, use_errno=True).sysctlbyname(b"kern.boottime", ctypes.byref(value), ctypes.byref(size), None, 0)
        _require(result == 0 and size.value == ctypes.sizeof(value) and value.seconds > 0, "host boot identity unavailable")
        return f"darwin:{value.seconds}:{value.microseconds}"
    raise AdmissionError("host boot identity unsupported")


def _elapsed():
    # Linux BOOTTIME includes suspend and is shared across processes/containers.
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def _opening_clock(value):
    _require(isinstance(value, dict) and set(value) == {"boot_id", "elapsed", "wall"}
             and value["boot_id"] == _boot_identity()
             and all(type(value[k]) in (int, float) and math.isfinite(value[k]) and value[k] > 0
                     for k in ("elapsed", "wall")), "invalid opening clock")
    return value


def _parsed(raw):
    def pairs(items):
        value = {}
        for key, entry in items:
            _require(key not in value, "duplicate JSON key")
            value[key] = entry
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(AdmissionError("nonfinite JSON")))


def _directory(path):
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o700, "unsafe private directory")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600
                 and 0 < before.st_size <= MAX_RECORD_BYTES, "unsafe private record")
        raw = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
        _require(len(raw) == before.st_size and before.st_size == after.st_size
                 and before.st_mtime_ns == after.st_mtime_ns, "record changed while reading")
    return _parsed(raw)


def _confirm_file(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, value):
    """Strict file/rename/directory durability; no cleanup on partial failure."""
    _directory(path.parent)
    raw = _encoded(value)
    _require(len(raw) <= MAX_RECORD_BYTES, "record byte limit")
    pending = path.with_name(path.name + ".pending")
    if _present(pending):
        _require(_read(pending) == value, "unconfirmed partial or conflicting write")
        fd = os.open(pending, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(pending, path)
    _sync_directory(path.parent)


def _bounded_paths(path, limit):
    _directory(path)
    values = []
    with os.scandir(path) as entries:
        for entry in entries:
            _require(len(values) < limit, "persistent record capacity exhausted")
            values.append(Path(entry.path))
    return values


def _wait(value):
    _require(type(value) in (int, float) and math.isfinite(value)
             and 0 <= value <= MAX_WAIT_SECONDS, "invalid lock wait")
    return value


def _lock(path, exclusive, seconds):
    _wait(seconds)
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                 and stat.S_IMODE(info.st_mode) == 0o600, "unsafe lock")
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise AdmissionBusy("active work/lock did not drain within bound") from error
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        current = path.lstat()
        _require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), "lock identity changed")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _locked(path, exclusive, seconds):
    fd = _lock(path, exclusive, seconds)
    try:
        yield
    finally:
        os.close(fd)


def _binding(value):
    keys = {"revision", "image_id", "strategy", "source_sha256", "deployment_nonce"}
    _require(isinstance(value, dict) and set(value) == keys, "exact binding fields required")
    _require(re.fullmatch(r"[0-9a-f]{40}", str(value["revision"]))
             and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["image_id"]))
             and isinstance(value["strategy"], str) and 0 < len(value["strategy"]) <= 128
             and value["source_sha256"] == source_sha256()
             and HEX32.fullmatch(str(value["deployment_nonce"])), "binding/source identity invalid")
    return value


def _policy(value):
    keys = {"not_before", "expires_at", "max_admissions", "max_bytes"}
    _require(isinstance(value, dict) and set(value) == keys and all(type(v) is int for v in value.values()), "exact integer policy required")
    _require(0 < value["not_before"] < value["expires_at"] and value["expires_at"] - value["not_before"] <= MAX_WINDOW_SECONDS
             and 0 < value["max_admissions"] <= MAX_ADMISSIONS and 0 < value["max_bytes"] <= MAX_BYTES, "finite policy limit")
    return value


def _state(value):
    _require(isinstance(value, dict) and set(value) == {"schema", "generation", "phase", "pending_operation", "opening_clock"}
             and type(value["schema"]) is int and value["schema"] == 1
             and type(value["generation"]) is int and 0 <= value["generation"] < 2**63
             and value["phase"] in {"open", "draining", "freezing", "frozen"}
             and (value["pending_operation"] == "" or HEX64.fullmatch(str(value["pending_operation"]))), "invalid fence state")
    if value["generation"] == 0:
        _require(value["opening_clock"] is None, "unopened clock changed")
    else:
        _opening_clock(value["opening_clock"])
    return value


def provision(root, binding, policy):
    """Explicit install only; never called by runtime and never opens intake."""
    root = Path(root)
    _binding(binding)
    _policy(policy)
    _require(not _present(root), "provision requires a new directory")
    root.mkdir(mode=0o700)
    _directory(root)
    for name in ("locks", "leases", "finishes", "operations", "startup"):
        (root / name).mkdir(mode=0o700)
        _sync_directory(root / name)
    for name in ("metadata", "activity", "controller"):
        fd = os.open(root / "locks" / (name + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _sync_directory(root / "locks")
    manifest = {"schema": 1, "binding": binding, "policy": policy, "host_boot_id": _boot_identity()}
    _write(root / "manifest.json", manifest)
    _write(root / "state.json", {"schema": 1, "generation": 0, "phase": "frozen", "pending_operation": "", "opening_clock": None})
    _sync_directory(root)
    _sync_directory(root.parent)
    return {"status": "provisioned_frozen", "manifest_sha256": _hash(manifest), "switch_authorized": False}


class Fence:
    def __init__(self, root, binding, lock_seconds=1.0):
        self.root, self.binding = Path(root), _binding(binding)
        self.lock_seconds = _wait(lock_seconds)
        self.pid, self.process_nonce = os.getpid(), secrets.token_hex(16)
        self._manifest()

    def _manifest(self):
        _require(self.pid == os.getpid(), "inherited fence after fork requires fresh binding")
        _directory(self.root)
        value = _read(self.root / "manifest.json")
        _require(set(value) == {"schema", "binding", "policy", "host_boot_id"} and type(value["schema"]) is int
                 and value["schema"] == 1 and value["binding"] == self.binding
                 and value["host_boot_id"] == _boot_identity(), "manifest/host boot binding changed")
        _binding(value["binding"])
        _policy(value["policy"])
        return value

    def _metadata(self):
        return _locked(self.root / "locks/metadata.lock", True, self.lock_seconds)

    @contextmanager
    def frozen_startup(self):
        """Exclusive initial-frozen maintenance, never an ordinary work lease.

        This grants no thread-local intake/drain exemption. The fixed startup
        runner separately requires a one-time, source-bound private permit.
        """
        _require(getattr(_LOCAL, "lease", None) is None, "startup cannot inherit business admission")
        with _locked(self.root / "locks/controller.lock", True, self.lock_seconds):
            with _locked(self.root / "locks/activity.lock", True, self.lock_seconds):
                with self._metadata():
                    self._manifest()
                    state = _state(_read(self.root / "state.json"))
                    intents, pending = self._operation_inventory()
                    leases, finishes = self._leases()
                    _require(state["phase"] == "frozen" and state["generation"] == 0
                             and not state["pending_operation"] and not intents and not pending
                             and not leases and not finishes, "startup requires pristine frozen fence")
                    _directory(self.root / "startup")
                yield

    def _operation_inventory(self):
        intents, receipts = set(), set()
        for path in _bounded_paths(self.root / "operations", MAX_OPERATIONS * 2 + 1):
            if re.fullmatch(r"[0-9a-f]{64}\.intent\.json", path.name):
                intents.add(path.name[:64])
            elif re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                receipts.add(path.name[:64])
            else:
                raise AdmissionUncertain("operation has partial/unknown evidence")
        _require(receipts <= intents, "operation receipt has no intent")
        return intents, intents - receipts

    def _leases(self):
        leases, finishes = {}, {}
        for folder, values in (("leases", leases), ("finishes", finishes)):
            for path in _bounded_paths(self.root / folder, MAX_LEASES):
                if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                    raise AdmissionUncertain("lease has partial/unknown evidence")
                row = _read(path)
                _require(row.get("lease_id") == path.stem and row.get("binding_sha256") == _hash(self.binding), "lease binding differs")
                values[path.stem] = row
        _require(set(finishes) <= set(leases), "finish has no original lease")
        for key, row in leases.items():
            _require(row.get("kind") in {"intake", "drain"} and type(row.get("charge_bytes")) is int
                     and 0 <= row["charge_bytes"] <= MAX_BYTES and type(row.get("created_at")) in (int, float), "invalid original lease")
            if key in finishes:
                _require(finishes[key].get("lease_sha256") == _hash(row)
                         and finishes[key].get("outcome") in {"finished", "uncertain"}, "finish identity differs")
                # A prior caller may have lost the final directory-sync result.
                # Visible completion bytes alone are never durability proof.
                _confirm_file(self.root / "leases" / (key + ".json"))
                _confirm_file(self.root / "finishes" / (key + ".json"))
        _sync_directory(self.root / "leases")
        _sync_directory(self.root / "finishes")
        return leases, finishes

    def _orphaned(self, leases, finishes):
        """An immutable lease inode has its own owner lock, independent of PID reuse."""
        orphaned = []
        for key in set(leases) - set(finishes):
            path = self.root / "leases" / (key + ".json")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                orphaned.append(key)
            finally:
                os.close(fd)
        return orphaned

    def snapshot(self):
        with self._metadata():
            manifest = self._manifest()
            state = _state(_read(self.root / "state.json"))
            _, pending = self._operation_inventory()
            leases, finishes = self._leases()
            return {**state, "manifest_sha256": _hash(manifest), "binding": self.binding,
                    "intake_count": sum(r["kind"] == "intake" for r in leases.values()),
                    "charged_bytes": sum(r["charge_bytes"] for r in leases.values() if r["kind"] == "intake"),
                    "active": len(set(leases) - set(finishes)), "orphaned": len(self._orphaned(leases, finishes)),
                    "uncertain": sum(r["outcome"] == "uncertain" for r in finishes.values()),
                    "pending_operations": sorted(pending), "queue_checked": False, "switch_authorized": False}

    def reserve_lease(self, entrance, kind="intake", charge_bytes=0):
        _require(kind in {"intake", "drain"} and isinstance(entrance, str)
                 and re.fullmatch(r"[a-z][a-z0-9_.:-]{0,95}", entrance), "invalid lease entrance/kind")
        _require(type(charge_bytes) is int and 0 <= charge_bytes <= MAX_BYTES, "invalid charge")
        self._manifest()
        fd = _lock(self.root / "locks/activity.lock", False, self.lock_seconds)
        try:
            with self._metadata():
                manifest, now = self._manifest(), time.time()
                state = _state(_read(self.root / "state.json"))
                _, pending = self._operation_inventory()
                if pending or state["pending_operation"] or state["phase"] not in {"open", "draining"}:
                    raise AdmissionClosed("fence is not admitting activities")
                if kind == "intake" and state["phase"] != "open":
                    raise AdmissionClosed("new intake is stopped")
                leases, finishes = self._leases()
                if self._orphaned(leases, finishes) or any(row["outcome"] == "uncertain" for row in finishes.values()):
                    raise AdmissionUncertain("uncertain lease requires explicit review")
                _require(len(leases) < MAX_LEASES, "lease evidence capacity exhausted")
                policy = manifest["policy"]
                if kind == "intake":
                    accepted = [r for r in leases.values() if r["kind"] == "intake"]
                    clock = state["opening_clock"]
                    elapsed = _elapsed() - clock["elapsed"]
                    if (not policy["not_before"] <= now < policy["expires_at"]
                            or not 0 <= elapsed < policy["expires_at"] - clock["wall"]
                            or any(now < r["created_at"] for r in accepted)):
                        raise AdmissionClosed("intake time window invalid/expired")
                    if len(accepted) >= policy["max_admissions"] or sum(r["charge_bytes"] for r in accepted) + charge_bytes > policy["max_bytes"]:
                        raise AdmissionClosed("intake policy capacity exhausted")
                row = {"schema": 1, "lease_id": secrets.token_hex(16), "binding_sha256": _hash(self.binding),
                       "generation": state["generation"], "kind": kind, "entrance": entrance,
                       "charge_bytes": charge_bytes if kind == "intake" else 0, "created_at": now,
                       "pid": self.pid, "process_nonce": self.process_nonce}
                _write(self.root / "leases" / (row["lease_id"] + ".json"), row)
                owner_fd = os.open(self.root / "leases" / (row["lease_id"] + ".json"), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    os.close(owner_fd)
                    raise
            return Lease(self, row, fd, owner_fd)
        except BaseException:
            os.close(fd)
            raise

    def _intent(self, action, operation_id, expected_generation, reason):
        _require(isinstance(operation_id, str) and HEX64.fullmatch(operation_id)
                 and type(expected_generation) is int and 0 <= expected_generation < 2**63 - 1
                 and isinstance(reason, str) and 0 < len(reason) <= 256, "explicit operation/CAS/reason required")
        state = _state(_read(self.root / "state.json"))
        intent_path = self.root / "operations" / (operation_id + ".intent.json")
        request = {"action": action, "operation_id": operation_id, "expected_generation": expected_generation,
                   "reason": reason, "manifest_sha256": _hash(self._manifest())}
        if _present(intent_path) or _present(intent_path.with_name(intent_path.name + ".pending")):
            raise AdmissionUncertain("operation already attempted; query exact operation only")
        intents, pending = self._operation_inventory()
        _require(not pending and not state["pending_operation"] and len(intents) < MAX_OPERATIONS, "unconfirmed operation or capacity limit")
        expected = {"open": "frozen", "stop_new": "open", "freeze": "draining"}[action]
        _require(state["phase"] == expected and state["generation"] == expected_generation, "phase/generation CAS differs")
        _require(action != "open" or expected_generation == 0, "resume/new policy requires separate reviewed provisioning")
        if action == "open" and _present(self.root / "startup") and _bounded_paths(self.root / "startup", 16):
            from services.controlled_startup import require_startup_complete
            require_startup_complete(self)
        policy = self._manifest()["policy"]
        _require(action != "open" or policy["not_before"] <= time.time() < policy["expires_at"], "approved opening window not current")
        after = {"schema": 1, "generation": expected_generation + 1,
                 "phase": {"open": "open", "stop_new": "draining", "freeze": "freezing"}[action],
                 "pending_operation": operation_id,
                 "opening_clock": {"boot_id": _boot_identity(), "elapsed": _elapsed(), "wall": time.time()} if action == "open" else state["opening_clock"]}
        intent = {"schema": 1, "request": request, "request_sha256": _hash(request), "before": state, "after": after}
        _write(intent_path, intent)
        return intent

    def _load_intent(self, operation_id):
        _require(isinstance(operation_id, str) and HEX64.fullmatch(operation_id), "bad operation id")
        path = self.root / "operations" / (operation_id + ".intent.json")
        pending = path.with_name(path.name + ".pending")
        value = _read(pending if _present(pending) else path)
        request = value["request"]
        _require(type(value["schema"]) is int and value["schema"] == 1 and request["operation_id"] == operation_id
                 and request["manifest_sha256"] == _hash(self._manifest())
                 and value["request_sha256"] == _hash(request), "operation binding differs")
        before, after = _state(value["before"]), _state(value["after"])
        _require(request["action"] in {"open", "stop_new", "freeze"}
                 and type(request["expected_generation"]) is int
                 and before["generation"] == request["expected_generation"]
                 and after["generation"] == before["generation"] + 1
                 and after["pending_operation"] == operation_id, "operation state identity differs")
        action = request["action"]
        _require(before["phase"] == {"open": "frozen", "stop_new": "open", "freeze": "draining"}[action]
                 and after["phase"] == {"open": "open", "stop_new": "draining", "freeze": "freezing"}[action]
                 and not before["pending_operation"] and (action != "open" or before["generation"] == 0), "operation phase identity differs")
        _require(action == "open" or after["opening_clock"] == before["opening_clock"], "opening deadline cannot reset")
        return value

    def _advance(self, intent, wait_seconds):
        request, after = intent["request"], intent["after"]
        operation_id = request["operation_id"]
        path = self.root / "operations" / (operation_id + ".json")
        with self._metadata():
            current = _state(_read(self.root / "state.json"))
            final = {**after, "phase": "frozen" if request["action"] == "freeze" else after["phase"], "pending_operation": ""}
            _require(current in (intent["before"], after, final), "operation state no longer matches")
            if _present(path):
                receipt = _read(path)
                expected = {"schema": 1, **request, "request_sha256": intent["request_sha256"],
                            "generation": final["generation"], "phase": final["phase"], "durable": True,
                            "queue_checked": False, "switch_authorized": False}
                _require(current == final and set(receipt) == set(expected) | {"activity_drained"}
                         and type(receipt["activity_drained"]) is bool
                         and all(type(receipt[k]) is type(v) and receipt[k] == v for k, v in expected.items()), "existing receipt identity differs")
                if request["action"] == "freeze":
                    leases, finishes = self._leases()
                    _require(set(leases) == set(finishes) and all(r["outcome"] == "finished" for r in finishes.values()), "frozen lease evidence changed")
                _write(self.root / "operations" / (operation_id + ".intent.json"), intent)
                _write(self.root / "state.json", final)
                _write(path, receipt)
                return receipt
            _write(self.root / "operations" / (operation_id + ".intent.json"), intent)
            _write(self.root / "state.json", after)
        if request["action"] == "freeze":
            with _locked(self.root / "locks/activity.lock", True, wait_seconds):
                return self._complete(intent, final, path)
        return self._complete(intent, final, path)

    def _complete(self, intent, final, path):
        with self._metadata():
            self._manifest()
            _require(_state(_read(self.root / "state.json")) == intent["after"], "transition CAS changed")
            leases, finishes = self._leases()
            drained = set(leases) == set(finishes) and all(r["outcome"] == "finished" for r in finishes.values())
            if intent["request"]["action"] == "freeze" and not drained:
                raise AdmissionUncertain("crashed/uncertain leases remain; freeze not confirmed")
            receipt = {"schema": 1, **intent["request"], "request_sha256": intent["request_sha256"],
                       "generation": final["generation"], "phase": final["phase"], "durable": True,
                       "activity_drained": drained, "queue_checked": False, "switch_authorized": False}
            _write(self.root / "state.json", final)
            _write(path, receipt)
            return receipt

    def _operate(self, action, operation_id, expected_generation, reason, wait_seconds=1.0):
        _wait(wait_seconds)
        self._manifest()
        with _locked(self.root / "locks/controller.lock", True, self.lock_seconds):
            with self._metadata():
                intent = self._intent(action, operation_id, expected_generation, reason)
            return self._advance(intent, wait_seconds)

    def open_trial(self, operation_id, expected_generation, reason):
        return self._operate("open", operation_id, expected_generation, reason)

    def stop_new(self, operation_id, expected_generation, reason):
        return self._operate("stop_new", operation_id, expected_generation, reason)

    def freeze(self, operation_id, expected_generation, reason, wait_seconds=1.0):
        return self._operate("freeze", operation_id, expected_generation, reason, wait_seconds)

    def query_operation(self, operation_id, wait_seconds=1.0):
        _wait(wait_seconds)
        self._manifest()
        with _locked(self.root / "locks/controller.lock", True, self.lock_seconds):
            with self._metadata():
                intent = self._load_intent(operation_id)
            return self._advance(intent, wait_seconds)


class Lease:
    """Explicit SH ownership transferable between serial threads, never forks."""
    def __init__(self, fence, row, fd, owner_fd):
        self.fence, self.row, self.fd, self.owner_fd = fence, row, fd, owner_fd
        self.kind, self.pid = row["kind"], os.getpid()
        self.activation = threading.Lock()
        self.outcome = None

    @contextmanager
    def activate(self):
        _require(self.pid == os.getpid() and self.fd is not None, "lease ended or inherited after fork")
        previous = getattr(_LOCAL, "lease", None)
        if previous is self:
            yield self
            return
        _require(previous is None, "different active lease cannot be replaced")
        _require(self.activation.acquire(blocking=False), "lease already active in another thread")
        try:
            # finish() can win between the fast check and this lock acquisition.
            _require(self.pid == os.getpid() and self.fd is not None and self.outcome is None,
                     "lease ended or inherited after fork")
            _LOCAL.lease = self
            try:
                yield self
            finally:
                _LOCAL.lease = previous
        finally:
            self.activation.release()

    def finish(self, success=True):
        _require(type(success) is bool and self.pid == os.getpid(), "invalid lease finish")
        if self.fd is None:
            _require(self.outcome == success, "unconfirmed/conflicting duplicate finish")
            return
        _require(self.activation.acquire(blocking=False), "cannot finish active work")
        try:
            # Another finisher may have completed after the unlocked fast path.
            if self.fd is None:
                _require(self.outcome == success, "unconfirmed/conflicting duplicate finish")
                return
            _require(self.pid == os.getpid() and self.outcome is None, "invalid active lease finish")
            try:
                with self.fence._metadata():
                    self.fence._manifest()
                    path = self.fence.root / "leases" / (self.row["lease_id"] + ".json")
                    _require(_read(path) == self.row, "original lease changed")
                    value = {"schema": 1, "lease_id": self.row["lease_id"], "binding_sha256": self.row["binding_sha256"],
                             "lease_sha256": _hash(self.row), "outcome": "finished" if success else "uncertain"}
                    _write(self.fence.root / "finishes" / path.name, value)
                    self.outcome = success
            finally:
                try:
                    os.close(self.owner_fd)
                finally:
                    self.owner_fd = None
                    os.close(self.fd)
                    self.fd = None
        finally:
            self.activation.release()


class _DisabledLease:
    def __init__(self, kind):
        self.kind, self.pid = kind, os.getpid()

    @contextmanager
    def activate(self):
        previous = getattr(_LOCAL, "lease", None)
        _LOCAL.lease = self
        try:
            yield self
        finally:
            _LOCAL.lease = previous

    def finish(self, success=True):
        return None


def current_kind(default="intake"):
    _require(default in {"intake", "drain"}, "invalid inherited default")
    lease = getattr(_LOCAL, "lease", None)
    if lease is not None:
        _require(lease.pid == os.getpid(), "inherited context after fork")
        return lease.kind
    return default


def _configured():
    mode = os.environ.get("AI_FILTER_ADMISSION_MODE", "disabled")
    if mode == "disabled":
        return None
    _require(mode == "enforced", "unknown admission mode")
    root = os.environ.get("AI_FILTER_ADMISSION_ROOT", "/app/data/admission_fence")
    try:
        binding = _binding(_parsed(os.environ.get("AI_FILTER_ADMISSION_BINDING", "")))
    except (ValueError, TypeError, KeyError) as error:
        raise AdmissionError("invalid explicit admission binding") from error
    key = (os.getpid(), root, _hash(binding))
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = Fence(root, binding)
        return _CACHE[key]


def reserve_lease(entrance, kind="drain", charge_bytes=0):
    kind = current_kind() if kind == "inherit" else kind
    _require(kind in {"intake", "drain"}, "invalid lease kind")
    fence = _configured()
    if fence is None:
        return _DisabledLease(kind)
    _require(getattr(_LOCAL, "lease", None) is None, "explicit lease reserve inside existing operation forbidden")
    return fence.reserve_lease(entrance, kind, charge_bytes)


@contextmanager
def admission(entrance, kind="intake", charge_bytes=0):
    kind = current_kind() if kind == "inherit" else kind
    _require(kind in {"intake", "drain"}, "invalid admission kind")
    outer = getattr(_LOCAL, "lease", None)
    mode = os.environ.get("AI_FILTER_ADMISSION_MODE", "disabled")
    _require(mode in {"disabled", "enforced"}, "unknown admission mode")
    if mode == "disabled" and outer is not None:
        yield outer
        return
    if outer is not None:
        configured = _configured()
        _require(isinstance(outer, Lease) and outer.pid == os.getpid()
                 and outer.fence.root == configured.root and outer.fence.binding == configured.binding
                 and not (outer.kind == "drain" and kind == "intake"), "nested admission cannot change identity or create intake from drain")
        yield outer
        return
    lease = reserve_lease(entrance, kind, charge_bytes)
    try:
        with lease.activate():
            yield lease
    except BaseException:
        lease.finish(False)
        raise
    else:
        lease.finish(True)


def guarded(entrance, kind="intake"):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with admission(entrance, kind):
                return function(*args, **kwargs)
        return wrapped
    return decorate
