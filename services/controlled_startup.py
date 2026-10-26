"""One-shot, isolation-only initialization while ordinary admission stays frozen.

The SQLite initializer commits in several phases. Therefore an intent without
its exact SQLite completion marker is *unknown*, never permission to rerun it.
No user callback, HTTP endpoint, role lease, or environment variable opens work.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time
from urllib.parse import quote

from services import admission_fence as gate

PROJECT = Path(__file__).resolve().parents[1]
ENV_OPERATION = "AI_FILTER_CONTROLLED_STARTUP_OPERATION"
MAX_SCHEMA_ROWS = 2048
MAX_SOURCE_BYTES = 16 * 1024 * 1024
_OWNERS = {}
_OWNER_LOCK = threading.Lock()


def runtime_sources_sha256():
    """Bind actual runtime Python bytes; image/libraries still need external inspect."""
    paths = []
    for path in PROJECT.glob("*.py"):
        gate._require(len(paths) < 2048, "runtime source inventory exceeded")
        paths.append(path)
    for name in ("core", "db", "routes", "services", "utils", "workers", "modules"):
        for path in (PROJECT / name).rglob("*.py"):
            gate._require(len(paths) < 2048, "runtime source inventory exceeded")
            paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(paths):
        info = path.lstat()
        gate._require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                      and info.st_size <= MAX_SOURCE_BYTES, "unsafe runtime source")
        digest.update(path.relative_to(PROJECT).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _db_path(value):
    gate._require(isinstance(value, str), "explicit database path required")
    path = Path(value)
    gate._require(path.is_absolute() and path.resolve() == path and path.name.endswith(".db"), "canonical database path required")
    gate._directory(path.parent)
    if gate._present(path):
        info = path.lstat()
        gate._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                      and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600,
                      "unsafe startup database")
    return path


def _validate(fence, value, *, active=False):
    keys = {"schema", "scope", "run_id", "operation_id", "manifest_sha256", "binding",
            "runtime_sources_sha256", "db_path", "roles", "not_before", "expires_at"}
    gate._require(isinstance(value, dict) and set(value) == keys and type(value["schema"]) is int
                  and value["schema"] == 1 and value["scope"] == "isolation", "startup authority is isolation-only")
    gate._require(gate.HEX32.fullmatch(str(value["run_id"])) and gate.HEX64.fullmatch(str(value["operation_id"]))
                  and value["binding"] == fence.binding and value["manifest_sha256"] == gate._hash(fence._manifest())
                  and value["runtime_sources_sha256"] == runtime_sources_sha256(), "startup identity/source mismatch")
    roles = value["roles"]
    gate._require(isinstance(roles, dict) and set(roles) == {"web", "worker"}
                  and all(isinstance(v, str) and gate.HEX64.fullmatch(v) for v in roles.values())
                  and len(set(roles.values())) == 2, "exact distinct role permits required")
    start, end = value["not_before"], value["expires_at"]
    gate._require(type(start) is int and type(end) is int and 0 < start < end and end - start <= 7200,
                  "startup finite window required")
    gate._require(not active or start <= time.time() < end, "startup permit expired/not active")
    _db_path(value["db_path"])
    path = Path(value["db_path"])
    temporary_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    gate._require(any(root in path.parents for root in temporary_roots), "production database paths are not supported by isolation runner")
    return value


def _paths(fence):
    directory = fence.root / "startup"
    gate._directory(directory)
    return directory


def _same(actual, expected):
    return gate._encoded(actual) == gate._encoded(expected)


def _inventory(fence):
    allowed = {"permit.json", "authorization.json", "migration.intent.json", "migration.json", "web.intent.json", "web.json",
               "worker.intent.json", "worker.json"}
    for path in gate._bounded_paths(_paths(fence), 16):
        gate._require(path.name in allowed or (path.name.endswith(".pending") and path.name[:-8] in allowed),
                      "unknown startup evidence")


def _read_complete(path):
    """Recover exact complete pending bytes; malformed/conflicting evidence stays held."""
    pending = path.with_name(path.name + ".pending")
    if gate._present(pending):
        value = gate._read(pending)
        if gate._present(path):
            gate._require(_same(gate._read(path), value), "startup pending conflicts with original")
    else:
        value = gate._read(path)
    return value


def _permit(fence, operation_id, *, active=False):
    _inventory(fence)
    value = _validate(fence, _read_complete(_paths(fence) / "permit.json"), active=active)
    gate._require(value["operation_id"] == operation_id, "startup operation differs")
    _authority(fence, value, active=active)
    return value


def _authority(fence, permit, *, active):
    path = _paths(fence) / "authorization.json"
    authority = _read_complete(path)
    gate._require(isinstance(authority, dict) and set(authority) == {"schema", "permit_sha256", "clock"}
                  and type(authority["schema"]) is int and authority["schema"] == 1
                  and authority["permit_sha256"] == gate._hash(permit), "startup authorization clock differs")
    clock = gate._opening_clock(authority["clock"])
    gate._require(permit["not_before"] <= clock["wall"] < permit["expires_at"], "startup clock outside approved window")
    if active:
        elapsed = gate._elapsed() - clock["elapsed"]
        gate._require(time.time() >= clock["wall"] and 0 <= elapsed < permit["expires_at"] - clock["wall"],
                      "startup monotonic window expired or wall clock rolled back")
    gate._write(path, authority)
    return authority


def authorize(fence, permit):
    """Install exactly once. Failed confirmation is queried, not overwritten."""
    with fence.frozen_startup():
        _validate(fence, permit, active=True)
        gate._require(not gate._bounded_paths(_paths(fence), 16), "startup already attempted")
        gate._write(_paths(fence) / "permit.json", permit)
        gate._write(_paths(fence) / "authorization.json", {
            "schema": 1, "permit_sha256": gate._hash(permit),
            "clock": {"boot_id": gate._boot_identity(), "elapsed": gate._elapsed(), "wall": time.time()}})
        return {"status": "startup_authorized", "permit_sha256": gate._hash(permit),
                "scope": "isolation", "business_open": False, "switch_authorized": False}


def _intent(permit):
    return {"schema": 1, "operation_id": permit["operation_id"], "run_id": permit["run_id"],
            "permit_sha256": gate._hash(permit), "kind": "database_initialization"}


def _connection(path, mode):
    conn = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode={mode}", uri=True, timeout=3)
    conn.execute("PRAGMA busy_timeout=3000")
    if mode == "ro":
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def _schema(conn):
    rows = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchmany(MAX_SCHEMA_ROWS + 1)
    gate._require(0 < len(rows) <= MAX_SCHEMA_ROWS, "startup schema missing/exceeded")
    required = {"app_meta", "news_cache", "backtest_signals", "news_processing_logs", "llm_call_logs"}
    gate._require(required <= {row[1] for row in rows if row[0] == "table"}, "initialized schema incomplete")
    return gate._hash(rows)


def _marker_key(permit):
    return "controlled_startup:" + permit["operation_id"]


def _confirm_database(path):
    _db_path(str(path))
    gate._confirm_file(path)
    wal = path.with_name(path.name + "-wal")
    if gate._present(wal):
        info = wal.lstat()
        gate._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid(), "unsafe SQLite WAL")
        gate._confirm_file(wal)
    gate._sync_directory(path.parent)


def _mark_database(permit):
    path = _db_path(permit["db_path"])
    conn = _connection(path, "rw")
    try:
        result = _expected_database_result(permit, conn, path)
        conn.execute("INSERT INTO app_meta(k,v) VALUES(?,?)", (_marker_key(permit), gate._encoded(result).decode()))
        conn.commit()
    finally:
        conn.close()
    _confirm_database(path)
    return result


def _expected_database_result(permit, conn, path):
    info = path.lstat()
    return {**_intent(permit), "status": "initialized", "schema_sha256": _schema(conn), "db_path": str(path),
            "database_identity": {"device": info.st_dev, "inode": info.st_ino}}


def _database_result(permit):
    path = _db_path(permit["db_path"])
    gate._require(gate._present(path), "initialization database absent; do not rerun")
    conn = _connection(path, "ro")
    try:
        row = conn.execute("SELECT v FROM app_meta WHERE k=?", (_marker_key(permit),)).fetchone()
        gate._require(row is not None, "initialization outcome unknown; do not rerun")
        result = gate._parsed(row[0])
        expected = _expected_database_result(permit, conn, path)
        gate._require(_same(result, expected), "startup database marker/schema differs")
    finally:
        conn.close()
    _confirm_database(path)
    return result


def _result(fence, permit):
    path = _paths(fence) / "migration.intent.json"
    gate._require(_same(_read_complete(path), _intent(permit)), "migration intent identity differs")
    result = _database_result(permit)
    receipt = {"schema": 1, "operation_id": permit["operation_id"], "permit_sha256": gate._hash(permit),
               "kind": "database_initialization", "status": "completed", "database_result": result,
               "durable": True, "business_open": False, "switch_authorized": False}
    final = _paths(fence) / "migration.json"
    if gate._present(final) or gate._present(final.with_name(final.name + ".pending")):
        gate._require(_same(_read_complete(final), receipt), "migration receipt conflicts")
    gate._write(_paths(fence) / "permit.json", permit)
    gate._write(path, _intent(permit))
    gate._write(final, receipt)
    return receipt


def migrate(fence, operation_id):
    """Fixed real initializer, one invocation only; no retry or business lease."""
    with fence.frozen_startup():
        permit = _permit(fence, operation_id, active=True)
        path = _paths(fence) / "migration.intent.json"
        gate._require(not gate._present(path) and not gate._present(path.with_name(path.name + ".pending")),
                      "migration already attempted; query exact operation, never rerun")
        from db import database
        gate._require(Path(database.DB_PATH).resolve() == Path(permit["db_path"]), "initializer targets different database")
        gate._write(_paths(fence) / "permit.json", permit)
        gate._write(path, _intent(permit))
        old_umask = os.umask(0o077)
        try:
            database.init_db()
        finally:
            os.umask(old_umask)
        _mark_database(permit)
        return _result(fence, permit)


def query_migration(fence, operation_id):
    with fence.frozen_startup():
        return _result(fence, _permit(fence, operation_id))


def _role_record(permit, role, nonce):
    gate._require(role in permit["roles"], "unknown controlled startup role")
    return {"schema": 1, "operation_id": permit["operation_id"], "permit_sha256": gate._hash(permit),
            "role": role, "role_permit": permit["roles"][role], "process_nonce": nonce,
            "pid": os.getpid(), "host_boot_id": gate._boot_identity(), "status": "startup_initialized",
            "durable": True, "business_open": False, "health_verified": False}


def claim_role(fence, operation_id, role):
    """One process per role; crash/restart must not silently reuse authorization."""
    key = (str(fence.root), operation_id, role, os.getpid())
    with _OWNER_LOCK:
        with fence.frozen_startup():
            permit = _permit(fence, operation_id, active=True)
            _result(fence, permit)
            intent, final = _paths(fence) / f"{role}.intent.json", _paths(fence) / f"{role}.json"
            gate._require(role in {"web", "worker"}, "unknown controlled startup role")
            if key in _OWNERS:
                fd, receipt = _OWNERS[key]
                gate._require(os.fstat(fd).st_ino == intent.lstat().st_ino and _same(_read_complete(final), receipt),
                              "role ownership or receipt changed")
                gate._write(final, receipt)
                return receipt
            gate._require(not gate._present(intent) and not gate._present(intent.with_name(intent.name + ".pending"))
                          and not gate._present(final), "role already attempted; restart requires explicit review")
            receipt = _role_record(permit, role, gate.secrets.token_hex(16))
            gate._write(intent, receipt)
            fd = os.open(intent, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                gate._write(final, receipt)
            except BaseException:
                os.close(fd)
                raise
            _OWNERS[key] = (fd, receipt)
            return receipt


def _confirmed_role(fence, permit, role):
    gate._require(role in permit["roles"], "unknown controlled startup role")
    intent, final = _paths(fence) / f"{role}.intent.json", _paths(fence) / f"{role}.json"
    receipt = _read_complete(final)
    expected = _role_record(permit, role, receipt.get("process_nonce"))
    expected["pid"] = receipt.get("pid")
    gate._require(_same(receipt, expected) and type(receipt["pid"]) is int and receipt["pid"] > 0
                  and gate.HEX32.fullmatch(str(receipt["process_nonce"])) and _same(_read_complete(intent), receipt),
                  "role startup receipt identity differs")
    gate._confirm_file(intent)
    gate._write(final, receipt)
    fd = os.open(intent, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return receipt, True
        return receipt, False
    finally:
        os.close(fd)


def query_role(fence, operation_id, role):
    with fence.frozen_startup():
        permit = _permit(fence, operation_id)
        _result(fence, permit)
        receipt, live = _confirmed_role(fence, permit, role)
        return {"receipt": receipt, "owner_lease_live": live,
                "status": "startup_initialized" if live else "startup_uncertain",
                "health_verified": False, "business_open": False, "switch_authorized": False}


def require_startup_complete(fence):
    """Called inside the existing controller+metadata critical section before open."""
    original = _read_complete(_paths(fence) / "permit.json")
    permit = _permit(fence, original["operation_id"], active=True)
    _result(fence, permit)
    for role in permit["roles"]:
        _, live = _confirmed_role(fence, permit, role)
        gate._require(live, "startup role process is no longer holding its original lease")


def initialize_runtime_database(role, initializer, entrance):
    """Legacy behavior unchanged without an explicit one-shot startup operation."""
    operation_id = os.environ.get(ENV_OPERATION)
    if operation_id is None:
        with gate.admission(entrance, kind="intake"):
            initializer()
        return None
    gate._require(os.environ.get("AI_FILTER_ADMISSION_MODE") == "enforced"
                  and gate.HEX64.fullmatch(operation_id), "controlled startup requires enforced exact operation")
    gate._require(os.environ.get("AI_FILTER_ROLE") == role, "controlled startup runtime role differs")
    return claim_role(gate._configured(), operation_id, role)
