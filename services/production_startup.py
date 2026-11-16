"""Closed runtime slots. Host-only source/PG/seal checks never run here.

The descriptor/grants mount is read-only. Only the separate slot directory is
writable. A dispatched or claimed slot is never erased or silently reused.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from urllib.parse import quote

from services import admission_fence as gate, controlled_startup as isolation

ENV_OPERATION = "AI_FILTER_PRODUCTION_STARTUP_OPERATION"
ENV_ROOT = "AI_FILTER_PRODUCTION_STARTUP_ROOT"
SCHEMA = "production-closed-startup-v1"
JOURNAL = "production_startup_migration_steps"
_OWNERS = {}
_OWNER_LOCK = threading.Lock()


def enabled():
    return ENV_OPERATION in os.environ or ENV_ROOT in os.environ


def process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    gate._require(type(pid) is int and pid > 0, "invalid startup process")
    root = Path("/proc") / str(pid)
    raw = (root / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    gate._require(len(fields) > 20, "incomplete process identity")
    return {"pid": pid, "ppid": int(fields[1]), "start_ticks": int(fields[19]), "boot_id": gate._boot_identity(),
            "pid_namespace": (root / "ns/pid").stat().st_ino,
            "command_sha256": hashlib.sha256((root / "cmdline").read_bytes()).hexdigest(),
            "uid": root.stat().st_uid}


def _path(value, *, directory=False):
    path = Path(value)
    gate._require(path.is_absolute() and path.resolve() == path
                  and all(not p.is_symlink() for p in (path, *path.parents)), "unsafe startup path")
    if directory:
        gate._directory(path)
    return path


def _clock(value, *, active):
    gate._require(set(value) == {"boot_id", "wall", "elapsed", "expires_at"}
                  and value["boot_id"] == gate._boot_identity()
                  and 0 < value["expires_at"] - value["wall"] <= 1800, "startup finite clock differs")
    if active:
        elapsed = gate._elapsed() - value["elapsed"]
        gate._require(value["wall"] <= time.time() < value["expires_at"]
                      and 0 <= elapsed < value["expires_at"] - value["wall"], "startup window expired")


def _frozen(descriptor):
    record = descriptor["freeze"]
    root = _path(record["root"], directory=True)
    if record.get("kind") == "initial_closed":
        from services.production_initial_fence import read_receipt
        read_receipt(root, record)
        return
    state = gate._read(root / "state.json")
    manifest = gate._read(root / "manifest.json")
    receipt = isolation._read_complete(root / "operations" / (record["operation_id"] + ".json"))
    gate._require(state["phase"] == "frozen" and state["generation"] == record["generation"]
                  and not state["pending_operation"] and manifest["binding"] == record["binding"] and gate._hash(manifest) == record["manifest_sha256"]
                  and gate._hash(receipt) == record["receipt_sha256"], "closed startup frozen state differs")



def _sidecars(path, expected):
    gate._require(set(expected) == {"wal", "shm"}, "startup sidecar inventory differs")
    for name in ("wal", "shm"):
        item = Path(str(path) + "-" + name)
        gate._require(gate._present(item), "startup sidecar missing; read-only SQLite connection refused")
        info = item.lstat()
        gate._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not item.is_symlink()
                      and {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
                           "mode": stat.S_IMODE(info.st_mode)} == expected[name], "startup sidecar identity differs")


def _database(descriptor):
    from db import database, startup_migration_steps as steps
    path = _path(descriptor["database"]["path"])
    info = path.lstat()
    expected = descriptor["database"]["identity"]
    gate._require(str(Path(database.DB_PATH).resolve()) == str(path)
                  and {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid} == expected
                  and info.st_nlink == 1 and info.st_mode & 0o777 == 0o600, "startup DB identity differs")
    _sidecars(path, descriptor["database"]["sidecars"])
    conn = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=0.1)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA cache_size=-16384")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024**2)
        deadline = time.monotonic() + 15
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        rows = conn.execute(f"SELECT operation_id,step_number,record FROM {JOURNAL} ORDER BY step_number").fetchmany(15)
        gate._require(len(rows) == len(steps.STEP_IDS) and gate._hash([list(r) for r in rows])
                      == descriptor["migration"]["journal_sha256"], "completed migration journal differs")
        records = [gate._parsed(row[2]) for row in rows]
        gate._require(all(row[0] == descriptor["migration"]["operation_id"] and row[1] == index
                          and records[index]["step_id"] == steps.STEP_IDS[index]
                          and records[index]["permit_sha256"] == descriptor["migration"]["permit_sha256"]
                          for index, row in enumerate(rows)), "migration operation or fixed steps differ")
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                              "AND tbl_name != ? ORDER BY type,name", (JOURNAL,)).fetchmany(2049)
        gate._require(0 < len(schema) <= 2048 and gate._hash([list(row) for row in schema])
                      == descriptor["migration"]["schema_sha256"], "completed migration schema differs")
        metadata = conn.execute("SELECT k,v FROM app_meta ORDER BY k").fetchmany(4097)
        actual = {"schema_sha256": descriptor["migration"]["schema_sha256"],
                  "app_meta_sha256": gate._hash([list(row) for row in metadata]), "retained_outbox": steps.retained_outbox(conn)}
        gate._require(len(metadata) <= 4096 and actual == records[-1]["after"], "migration completion postcondition differs")
        steps.verify_final(conn)
        _sidecars(path, descriptor["database"]["sidecars"])
    finally:
        conn.close()


def load(*, active=True, verify_database=True, allow_activated=False):
    operation = os.environ.get(ENV_OPERATION)
    gate._require(isinstance(operation, str) and gate.HEX64.fullmatch(operation)
                  and isolation.ENV_OPERATION not in os.environ
                  and os.environ.get("AI_FILTER_ADMISSION_MODE") == "enforced", "exact production startup operation required")
    root = _path(os.environ.get(ENV_ROOT, ""), directory=True)
    descriptor = gate._read(root / "descriptor.json")
    keys = {"schema", "operation_id", "runtime_sources_sha256", "config", "database", "migration", "freeze",
            "clock", "state_path", "source", "slots", "threads"}
    gate._require(set(descriptor) == keys and descriptor["schema"] == SCHEMA
                  and descriptor["operation_id"] == operation
                  and descriptor["runtime_sources_sha256"] == isolation.runtime_sources_sha256(), "startup descriptor/source differs")
    slots = descriptor["slots"]
    web = [name for name in slots if name.startswith("web-")]
    gate._require(1 <= len(web) <= 4 and set(slots) == {"worker-1", *[f"web-{i}" for i in range(1, len(web) + 1)]}
                  and all(slots[name] == ("web" if name.startswith("web-") else "worker") for name in slots)
                  and type(descriptor["threads"]) is int and 4 <= descriptor["threads"] <= 64, "invalid finite startup slots")
    activated = False
    if allow_activated:
        state = gate._read(Path(descriptor["freeze"]["root"]) / "state.json")
        if state["phase"] != "frozen" or state["generation"] != descriptor["freeze"]["generation"]:
            from services.production_activation import resume_phase
            activated = resume_phase(root, descriptor) != "resuming"
            if not activated:
                # The exact resume intent still blocks every admission. Do
                # not poison live owners during the durable two-file commit.
                _clock(descriptor["clock"], active=active)
                activated = "resuming"
    _clock(descriptor["clock"], active=active and activated is not True)
    _path(descriptor["state_path"], directory=True)
    from core.config import config as holder
    config = descriptor["config"]
    path = _path(config["path"])
    gate._require(os.environ.get("AI_FILTER_CONFIG_PATH") == str(path) and path.stat().st_size <= 4 * 1024**2
                  and hashlib.sha256(path.read_bytes()).hexdigest() == config["sha256"]
                  and gate._hash(holder.cfg) == config["effective_sha256"], "startup config differs")
    gate._require(os.environ.get("AI_FILTER_ADMISSION_ROOT") == descriptor["freeze"]["root"]
                  and gate._parsed(os.environ.get("AI_FILTER_ADMISSION_BINDING", "null")) == descriptor["freeze"]["binding"]
                  and os.environ.get("AI_FILTER_DB_PATH") == descriptor["database"]["path"], "actual admission or database environment differs")
    if not activated:
        _frozen(descriptor)
    configured = gate._configured()
    gate._require(str(configured.root) == descriptor["freeze"]["root"]
                  and configured.binding == descriptor["freeze"]["binding"], "actual application fence differs")
    if verify_database:
        _database(descriptor)
    return root, descriptor


def _container_hostname():
    return Path("/etc/hostname").read_text().strip()


def _grant(root, descriptor, slot, attempt):
    gate._require(slot in descriptor["slots"] and type(attempt) is int and attempt in (0, 1), "unknown startup slot/attempt")
    grant = gate._read(root / f"{slot}.{attempt}.grant.json")
    keys = {"schema", "operation_id", "descriptor_sha256", "slot", "attempt", "role", "container_id", "image_id",
            "attempt_operation_id", "previous_receipt_sha256", "process_nonce", "slot_lock"}
    gate._require(set(grant) == keys and grant["schema"] == SCHEMA and grant["operation_id"] == descriptor["operation_id"]
                  and grant["descriptor_sha256"] == gate._hash(descriptor) and grant["slot"] == slot and grant["attempt"] == attempt
                  and grant["role"] == descriptor["slots"][slot] and grant["image_id"] == descriptor["source"]["image_id"]
                  and gate.HEX64.fullmatch(grant["container_id"]) and gate.HEX64.fullmatch(grant["attempt_operation_id"])
                  and gate.HEX32.fullmatch(grant["process_nonce"]), "startup grant differs")
    hostname = _container_hostname()
    gate._require(hostname == grant["container_id"][:12], "startup container identity differs")
    if attempt:
        old = isolation._read_complete(Path(descriptor["state_path"]) / f"{slot}.0.json")
        gate._require(gate._hash(old) == grant["previous_receipt_sha256"]
                      and old["attempt_operation_id"] != grant["attempt_operation_id"], "replacement original receipt differs")
    else:
        gate._require(grant["previous_receipt_sha256"] is None, "initial slot cannot replace an owner")
    return grant


def _state_paths(descriptor, slot, attempt):
    directory = _path(descriptor["state_path"], directory=True)
    prefix = f"{slot}.{attempt}"
    return directory / (prefix + ".dispatch.json"), directory / (prefix + ".intent.json"), directory / (prefix + ".json")


def _unattempted(paths):
    return all(not gate._present(p) and not gate._present(p.with_name(p.name + ".pending")) for p in paths)


@contextmanager
def _slot_lock(descriptor, grant):
    path = Path(descriptor["state_path"]) / f"{grant['slot']}.{grant['attempt']}.lock"
    fd = gate._lock(path, True, 0.1)
    try:
        info = os.fstat(fd)
        gate._require({"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid} == grant["slot_lock"],
                      "startup slot lock identity differs")
        yield
    finally:
        os.close(fd)


def dispatch(root, descriptor, slot, attempt):
    grant = _grant(root, descriptor, slot, attempt)
    dispatch_path, intent, final = _state_paths(descriptor, slot, attempt)
    with _slot_lock(descriptor, grant):
        gate._require(_unattempted((dispatch_path, intent, final)), "slot dispatched; query original attempt")
        master = process_identity()
        gate._require(master["pid"] == 1, "startup dispatch requires the actual container main process")
        record = {"grant_sha256": gate._hash(grant), "master": master}
        gate._write(dispatch_path, record)
    return grant


def claim(role, *, slot=None, attempt=0):
    root, descriptor = load()
    gate._require(os.environ.get("AI_FILTER_ROLE") == role and role in {"web", "worker"}, "production startup role differs")
    slot = "worker-1" if role == "worker" and slot is None else slot
    existing = [key for key, (_fd, receipt) in _OWNERS.items() if key[0] == os.getpid() and receipt["slot"] == slot]
    if existing:
        gate._require(len(existing) == 1, "ambiguous current startup owner")
        return check_owner(existing[0])
    gate._require(not _OWNERS, "one process cannot own multiple startup slots")
    if role == "worker":
        available = [n for n in (0, 1) if gate._present(root / f"worker-1.{n}.grant.json")
                     and _unattempted(_state_paths(descriptor, "worker-1", n))]
        gate._require(len(available) == 1, "worker startup attempt absent or ambiguous")
        attempt = available[0]
    grant = _grant(root, descriptor, slot, attempt)
    gate._require(grant["role"] == role, "slot role differs")
    dispatch_path, intent, final = _state_paths(descriptor, slot, attempt)
    key = (os.getpid(), str(intent))
    with _OWNER_LOCK:
        if key in _OWNERS:
            return check_owner(key)
        if role == "worker":
            dispatch(root, descriptor, slot, attempt)
        original_dispatch = isolation._read_complete(dispatch_path)
        gate._require(original_dispatch["grant_sha256"] == gate._hash(grant), "slot dispatch differs")
        owner, master = process_identity(), original_dispatch["master"]
        gate._require(master["pid"] == 1 and ((role == "worker" and owner == master)
                      or (role == "web" and owner["pid"] != 1 and owner["ppid"] == 1
                          and process_identity(owner["ppid"]) == master)), "startup owner is not the dispatched container child")
        with _slot_lock(descriptor, grant):
            gate._require(_unattempted((intent, final)), "slot already attempted; query original operation")
            receipt = {**grant, "owner": owner, "master": master, "owner_nonce": gate.secrets.token_hex(16),
                       "status": "closed_initialized", "business_open": False, "health_verified": False}
            gate._write(intent, receipt)
            fd = os.open(intent, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                gate._write(final, receipt)
            except BaseException:
                os.close(fd)
                raise
            _OWNERS[key] = (fd, receipt)
            return receipt


def check_owner(key):
    fd, receipt = _OWNERS[key]
    _root, descriptor = load(verify_database=False, allow_activated=True)
    _dispatch, intent, final = _state_paths(descriptor, receipt["slot"], receipt["attempt"])
    info = os.fstat(fd)
    gate._require(receipt["owner"] == process_identity() and isolation._read_complete(final) == receipt
                  and isolation._read_complete(intent) == receipt
                  and (info.st_dev, info.st_ino) == (intent.stat().st_dev, intent.stat().st_ino), "startup owner changed")
    return receipt


def _after_fork():
    global _OWNER_LOCK
    for fd, _receipt in _OWNERS.values():
        os.close(fd)
    _OWNERS.clear()
    _OWNER_LOCK = threading.Lock()
    import sys
    activation = sys.modules.get("services.production_activation")
    if activation is not None:
        activation._MONITOR = None


os.register_at_fork(after_in_child=_after_fork)


def install_gunicorn(server):
    """Bound each real fork; an absent grant leaves a hole, not a restart storm."""
    root, descriptor = load()
    web_slots = [s for s, role in descriptor["slots"].items() if role == "web"]
    gate._require(server.cfg.workers == len(web_slots) and server.cfg.threads == descriptor["threads"]
                  and not server.cfg.preload_app, "Gunicorn startup topology differs")
    original = server.spawn_worker
    def spawn():
        _clock(descriptor["clock"], active=True)
        _frozen(descriptor)
        for slot in sorted(web_slots):
            for attempt in (0, 1):
                grant_path = root / f"{slot}.{attempt}.grant.json"
                if gate._present(grant_path) and _unattempted(_state_paths(descriptor, slot, attempt)):
                    dispatch(root, descriptor, slot, attempt)
                    server._production_slot = (slot, attempt)
                    try:
                        return original()
                    finally:
                        server._production_slot = None
        return None
    server.spawn_worker = spawn


def pre_fork(server, worker):
    gate._require(getattr(server, "_production_slot", None) is not None, "unreviewed Gunicorn fork refused")
    worker.production_slot = server._production_slot


def closed_worker_loop():
    """Stay closed until the exact retained owner receives one prepare grant."""
    import signal
    from services import production_activation as activation
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(1):
        gate._require(len(_OWNERS) == 1, "closed worker owner absent")
        check_owner(next(iter(_OWNERS)))
        if activation.permission_present():
            from worker_main import run_business
            permission = activation.RuntimeActivation()
            try:
                return run_business(activation=permission)
            except Exception as error:
                permission.fail(error)
                raise
    return 0
