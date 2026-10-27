"""Finite, exact-operation SQLite migrations while reviewed storage stays locked.

This is a separate protocol from the pristine isolation startup runner. An
attempt is never resumed by executing steps again: only its committed journal
may be queried. The enclosing controller still owns first-takeover evidence,
the PostgreSQL control lock, independent storage inventory and opening.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import quote

from db import startup_migration_steps as steps
from services import admission_fence as gate, controlled_startup as isolation

SCHEMA = "production-migration-v1"
JOURNAL = "production_startup_migration_steps"
LOCK_ORDER = ("release", "refresh", "controller", "activity", "spool", "journal")
MAX_SECONDS = 1800
MAX_DB_BYTES = 16 * 1024**3


def file_identity(path, *, directory=False):
    path = Path(path)
    gate._require(path.is_absolute() and path.resolve() == path, "noncanonical migration path")
    for part in (path, *path.parents):
        gate._require(not part.is_symlink(), "symlink in migration path")
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    gate._require(kind(info.st_mode) and info.st_uid == os.geteuid()
                  and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
                  and (directory or info.st_nlink == 1), "unsafe migration storage identity")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino,
            "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}


def database_identity(path):
    path = Path(path)
    result = {"file": file_identity(path), "directory": file_identity(path.parent, directory=True)}
    gate._require(path.stat().st_size <= MAX_DB_BYTES, "migration database exceeds bound")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        result[suffix[1:]] = file_identity(sidecar) if gate._present(sidecar) else None
    gate._require((result["wal"] is None) == (result["shm"] is None), "incomplete SQLite sidecar identity")
    return result


def _same_database(path, expected, *, initial=False):
    actual = database_identity(path)
    fields = actual if initial else ("file", "directory")
    gate._require(all(actual[key] == expected[key] for key in fields), "migration database identity changed")
    return actual


def schema_fingerprint(conn):
    rows = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' AND tbl_name != ? ORDER BY type,name", (JOURNAL,)).fetchmany(2049)
    gate._require(0 < len(rows) <= 2048, "migration schema inventory exceeded or empty")
    return gate._hash([list(row) for row in rows])


def database_fingerprint(conn):
    metadata = []
    if steps.table_exists(conn, "app_meta"):
        metadata = conn.execute("SELECT k,v FROM app_meta ORDER BY k").fetchmany(4097)
        gate._require(len(metadata) <= 4096, "migration metadata inventory exceeded")
    return {"schema_sha256": schema_fingerprint(conn), "app_meta_sha256": gate._hash([list(row) for row in metadata]),
            "retained_outbox": steps.retained_outbox(conn)}


def migration_code_sha256():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for name in ("db", "services"):
        paths = sorted((root / name).glob("*.py")) if name == "db" else [Path(__file__)]
        for path in paths:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _configuration(permit):
    from core.config import config as holder
    record = permit["config"]
    path = Path(record["path"])
    # Runtime config can be the sealed read-only file; never chmod it here.
    info = path.lstat()
    gate._require(path.is_absolute() and path.resolve() == path and not path.is_symlink()
                  and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                  and stat.S_IMODE(info.st_mode) in (0o400, 0o600) and info.st_size <= 4 * 1024**2,
                  "unsafe migration config")
    gate._require(hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
                  and gate._hash(holder.cfg) == record["effective_sha256"], "migration runtime config differs")


def _release_contract(permit, *, active=False):
    from scripts import private_release_contract
    record = permit["config"]
    root = Path(__file__).resolve().parents[1]
    contract, _manifest, digest = private_release_contract.load(record["private_contract_path"], root, require_fresh=active)
    gate._require(digest == permit["source"]["private_contract_sha256"]
                  and contract["source_sha"] == permit["source"]["revision"]
                  and contract["source_tree"] == permit["source"]["tree"]
                  and contract["files"]["runtime.yaml"] == record["sha256"]
                  and Path(record["private_contract_path"]).parent / "runtime.yaml" == Path(record["path"]),
                  "sealed private release contract differs")


def validate_permit(permit, *, active=False):
    keys = {"schema", "operation_id", "parent_operation_id", "approved_by", "not_before", "expires_at",
            "runtime_sources_sha256", "migration_code_sha256", "source", "config", "database", "freeze",
            "locks", "before", "target_schema_sha256", "step_ids", "sender_contract_sha256",
            "inventory_sha256", "compatibility_sha256", "compatibility", "sender", "inventory",
            "minimum_free_bytes", "max_database_bytes"}
    gate._require(isinstance(permit, dict) and set(permit) == keys and permit["schema"] == SCHEMA,
                  "invalid production migration permit")
    for key in ("operation_id", "parent_operation_id", "runtime_sources_sha256", "migration_code_sha256",
                "target_schema_sha256", "sender_contract_sha256", "inventory_sha256", "compatibility_sha256"):
        gate._require(gate.HEX64.fullmatch(str(permit[key])), "invalid migration identity hash")
    gate._require(isinstance(permit["approved_by"], str) and 0 < len(permit["approved_by"]) <= 128,
                  "migration reviewer identity required")
    start, end = permit["not_before"], permit["expires_at"]
    gate._require(type(start) is int and type(end) is int and 0 < start < end and end - start <= MAX_SECONDS,
                  "finite migration window required")
    gate._require(not active or start <= time.time() < end, "migration execution window expired")
    gate._require(permit["runtime_sources_sha256"] == isolation.runtime_sources_sha256()
                  and permit["migration_code_sha256"] == migration_code_sha256()
                  and permit["step_ids"] == list(steps.STEP_IDS), "migration source or ordered steps differ")
    source = permit["source"]
    gate._require(set(source) == {"revision", "tree", "image_id", "private_contract_sha256", "manifest_sha256"}
                  and all(len(source[key]) == 40 and all(c in "0123456789abcdef" for c in source[key])
                          for key in ("revision", "tree"))
                  and source["image_id"].startswith("sha256:") and gate.HEX64.fullmatch(source["image_id"][7:])
                  and all(gate.HEX64.fullmatch(source[key]) for key in ("private_contract_sha256", "manifest_sha256")),
                  "invalid migration source contract")
    gate._require(set(permit["locks"]) == set(LOCK_ORDER), "exact migration lock inventory required")
    gate._require(gate._hash(permit["sender"]) == permit["sender_contract_sha256"], "sender migration contract differs")
    gate._require(type(permit["minimum_free_bytes"]) is int and 64 * 1024**2 <= permit["minimum_free_bytes"] < 2**63,
                  "finite migration free-space floor required")
    gate._require(type(permit["max_database_bytes"]) is int and 64 * 1024**2 <= permit["max_database_bytes"] <= MAX_DB_BYTES,
                  "reviewed migration database size limit required")
    expected_compatibility = {"old_binding": permit["freeze"]["binding"], "candidate_source": source,
        "from_schema_sha256": permit["before"]["schema_sha256"], "to_schema_sha256": permit["target_schema_sha256"],
        "sqlite_policy": "fixed-current-event-core-forward-only", "old_image_execution": "not-proven-no-fallback"}
    gate._require(permit["compatibility"] == expected_compatibility
                  and gate._hash(expected_compatibility) == permit["compatibility_sha256"], "reviewed compatibility endpoints differ")
    _configuration(permit)
    _release_contract(permit, active=active)
    return permit


def _frozen(permit):
    record = permit["freeze"]
    gate._require(set(record) == {"root", "binding", "manifest_sha256", "operation_id", "generation", "receipt_sha256"},
                  "invalid frozen operation binding")
    fence = gate.Fence(record["root"], record["binding"], lock_seconds=0.1)
    with fence._metadata():
        state = gate._state(gate._read(fence.root / "state.json"))
        intent = fence._load_intent(record["operation_id"])
        receipt = gate._read(fence.root / "operations" / (record["operation_id"] + ".json"))
        leases, finishes = fence._leases()
        _intents, pending = fence._operation_inventory()
        gate._require(state["phase"] == "frozen" and state["generation"] == record["generation"]
                      and not state["pending_operation"] and not pending
                      and intent["request"]["action"] == "freeze" and receipt["generation"] == state["generation"]
                      and receipt["request_sha256"] == intent["request_sha256"] and receipt["phase"] == "frozen"
                      and gate._hash(receipt) == record["receipt_sha256"]
                      and gate._hash(fence._manifest()) == record["manifest_sha256"]
                      and set(leases) == set(finishes) and all(row["outcome"] == "finished" for row in finishes.values()),
                      "frozen migration evidence changed or activity remains")
    gate._require(permit["compatibility"]["old_binding"] == fence.binding,
                  "old frozen runtime differs from reviewed compatibility endpoint")
    return fence


class _Connection(sqlite3.Connection):
    controlled_step = False

    def commit(self):
        gate._require(not self.controlled_step, "migration helper attempted an independent commit")
        return super().commit()


def _connection(path, *, readonly=False):
    # In the retained frozen lock context an absent WAL means all committed
    # bytes are in the main file. Immutable RO avoids creating sidecars there.
    suffix = "?mode=ro" if readonly else "?mode=rw"
    if readonly and not Path(str(path) + "-wal").exists():
        suffix += "&immutable=1"
    conn = sqlite3.connect("file:" + quote(str(path), safe="/") + suffix, uri=True,
                           timeout=0.1, isolation_level=None, cached_statements=0, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1024**2)
    conn.execute("PRAGMA cache_size=-16384")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute("PRAGMA busy_timeout=100")
    conn.execute("PRAGMA query_only=ON" if readonly else "PRAGMA synchronous=FULL")
    deadline = time.monotonic() + 30
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    if not readonly:
        gate._require(conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "migration requires existing WAL mode")
    return conn


def _step_authorizer(conn):
    def authorize(action, arg1, arg2, _database, _source):
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if conn.controlled_step and (action == sqlite3.SQLITE_TRANSACTION or
                (action == sqlite3.SQLITE_PRAGMA and arg2 is not None
                 and arg1.lower() in {"synchronous", "journal_mode", "max_page_count"})):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)


def _journal_rows(conn, permit):
    if not steps.table_exists(conn, JOURNAL):
        return []
    rows = conn.execute(f"SELECT operation_id,step_number,record FROM {JOURNAL} ORDER BY step_number").fetchmany(len(steps.STEP_IDS) + 1)
    gate._require(len(rows) <= len(steps.STEP_IDS) and all(row[0] == permit["operation_id"] for row in rows),
                  "migration journal belongs to another operation or exceeds bound")
    result, previous = [], permit["before"]
    for number, row in enumerate(rows):
        record = gate._parsed(row[2])
        gate._require(set(record) == {"permit_sha256", "step_id", "before", "after", "details"}
                      and row[1] == number and record["step_id"] == steps.STEP_IDS[number]
                      and record["permit_sha256"] == gate._hash(permit) and record["before"] == previous,
                      "migration journal chain differs")
        previous = record["after"]
        result.append(record)
    if result:
        gate._require(database_fingerprint(conn) == previous, "migration committed postcondition drift")
    return result


class LockedMigration:
    """A live lock capability, never a caller-supplied `locks_held` boolean."""
    def __init__(self, root, permit):
        self.root, self.permit = Path(root), validate_permit(permit)
        self.pid, self.fds = os.getpid(), []
        self.path = Path(permit["database"]["file"]["path"])
        self.sender = self.inventory = None

    def check(self, *, active=False):
        gate._require(self.pid == os.getpid() and len(self.fds) == 4 and self.sender is not None and self.inventory is not None,
                      "migration lock session is not live")
        validate_permit(self.permit, active=active)
        for (name, fd) in self.fds:
            expected = self.permit["locks"][name]
            actual = file_identity(expected["path"])
            info = os.fstat(fd)
            gate._require(actual == expected and (info.st_dev, info.st_ino) == (actual["device"], actual["inode"]),
                          "migration lock identity changed")
        _same_database(self.path, self.permit["database"])
        gate._require(self.path.stat().st_size <= self.permit["max_database_bytes"], "reviewed migration database size exceeded")
        for path, expected in self.inventory["lock_identities"].items():
            actual = file_identity(path)
            gate._require(all(actual[key] == value for key, value in expected.items()), "inventory lock identity changed")
        self.sender.check()
        _frozen(self.permit)
        if active:
            self._disk_budget()

    def _disk_budget(self):
        # Include retained WAL/SHM: a small main file is not a disk-size proof
        # while a transaction is writing its new pages into WAL.
        total = sum(path.stat().st_size for path in
                    (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")) if gate._present(path))
        gate._require(total <= self.permit["max_database_bytes"], "migration database/WAL byte budget exceeded")
        free = os.statvfs(self.path.parent)
        spool_free = os.statvfs(self.permit["inventory"]["spool"])
        gate._require(min(free.f_bavail * free.f_frsize, spool_free.f_bavail * spool_free.f_frsize)
                      >= self.permit["minimum_free_bytes"], "migration free-space floor not met")

    def _page_budget(self, conn, *, establish=False):
        size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        maximum = self.permit["max_database_bytes"] // size
        gate._require(0 < pages <= maximum, "migration logical database size exceeded")
        if establish:
            # SQLite enforces this during allocation, before an oversized
            # transaction can commit. It does not authorize a larger WAL.
            actual = conn.execute(f"PRAGMA max_page_count={maximum}").fetchone()[0]
            gate._require(actual == maximum, "SQLite refused reviewed page limit")
        self._disk_budget()

    def recheck_inventory(self, *, active=True):
        """Re-read under this session's retained locks; never reacquire them."""
        from scripts import independent_drain_inventory as inventory
        self.check(active=active)
        spec = self.permit["inventory"]
        reader = inventory.Reader(5)
        spool, journal = Path(spec["spool"]), Path(spec["journal"])
        gate._require(inventory.directory_identity(spool) == spec["identities"]["spool"]
                      and inventory.directory_identity(journal) == spec["identities"]["journal"], "inventory directory changed")
        _, locks = inventory._journal(reader, journal)
        expected_locks = {str(spool / ".lock"), str(journal / ".lock"), *map(str, locks)}
        gate._require(expected_locks == set(self.inventory["lock_identities"]), "retained inventory lock set changed")
        states, again = inventory._journal(reader, journal)
        gate._require(sorted(locks) == sorted(again), "journal identity locks changed")
        counts = inventory._queue(reader, spool)
        receiver = inventory.capacity._receiver_locked(spool, spec["identities"]["spool"], reader.deadline, [0], [0])
        publication = inventory._publication(reader, self.path, spec["identities"]["database"])
        reader.tick()
        actual = {**self.inventory, "counts": counts, "journal": states, "receiver": receiver, "publication": publication,
                  "sender": getattr(self.sender, "latest_snapshot", self.sender.snapshot),
                  "files_inspected": reader.files, "bytes_read": reader.bytes,
                  "evidence_sha256": hashlib.sha256(inventory.json.dumps(reader.fingerprints, sort_keys=True).encode()).hexdigest()}
        gate._require(inventory_fingerprint(actual) == self.permit["inventory_sha256"], "retained inventory changed")
        self.sender.check()
        return actual

    def authorize(self):
        self.check(active=True)
        gate._require(not gate._present(self.root), "migration authorization already attempted")
        gate._directory(self.root.parent)
        _same_database(self.path, self.permit["database"], initial=True)
        gate._require(self.permit["database"]["wal"] is not None and self.permit["database"]["shm"] is not None,
                      "production migration requires retained existing WAL and SHM identities")
        conn = _connection(self.path, readonly=True)
        try:
            self._page_budget(conn)
            gate._require(database_fingerprint(conn) == self.permit["before"], "migration source schema or metadata differs")
            preflight = steps.preflight(conn)
        finally:
            conn.close()
        self.root.mkdir(mode=0o700)
        gate._write(self.root / "permit.json", self.permit)
        gate._write(self.root / "authorization.json", {"permit_sha256": gate._hash(self.permit), "preflight": preflight,
                    "boot_id": gate._boot_identity(), "wall": time.time(), "elapsed": gate._elapsed()})
        gate._sync_directory(self.root.parent)
        return {"status": "migration_authorized", "business_open": False, "switch_authorized": False}

    def _authorization(self, *, active=False):
        self.check(active=active)
        gate._directory(self.root)
        gate._require(isolation._read_complete(self.root / "permit.json") == self.permit, "migration permit changed")
        authority = isolation._read_complete(self.root / "authorization.json")
        gate._require(authority["permit_sha256"] == gate._hash(self.permit)
                      and authority["boot_id"] == gate._boot_identity(), "migration authorization identity changed")
        if active:
            elapsed = gate._elapsed() - authority["elapsed"]
            gate._require(time.time() >= authority["wall"] and 0 <= elapsed < self.permit["expires_at"] - authority["wall"],
                          "migration monotonic window expired")
        return authority

    def migrate(self):
        authority = self._authorization(active=True)
        attempt = self.root / "migration.intent.json"
        gate._require(not gate._present(attempt) and not gate._present(attempt.with_name(attempt.name + ".pending")),
                      "migration already attempted; query the original operation only")
        _same_database(self.path, self.permit["database"], initial=True)
        conn = _connection(self.path, readonly=True)
        try:
            gate._require(database_fingerprint(conn) == self.permit["before"]
                          and not steps.table_exists(conn, JOURNAL), "migration source changed or was already attempted")
            steps.preflight(conn)
        finally:
            conn.close()
        gate._write(attempt, {"operation_id": self.permit["operation_id"], "permit_sha256": gate._hash(self.permit)})
        conn = _connection(self.path)
        try:
            self._page_budget(conn, establish=True)
            _step_authorizer(conn)
            for number, step in enumerate(steps.STEP_IDS):
                self._execute_step(conn, number, step, authority)
        finally:
            conn.close()
        return self.query_migration(self.permit["operation_id"])

    def _execute_step(self, conn, number, step, authority):
        self._authorization(active=True)
        deadline = min(time.monotonic() + 120, time.monotonic() + self.permit["expires_at"] - time.time())
        failures = []
        def progress():
            try:
                gate._require(time.monotonic() <= deadline, "migration step deadline exceeded")
                self._disk_budget()
            except Exception as error:
                if not failures:
                    failures.append(error)
                return 1
            return 0
        conn.set_progress_handler(progress, 1000)
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = database_fingerprint(conn)
            if number:
                gate._require(len(_journal_rows(conn, self.permit)) == number, "previous migration step not confirmed")
            else:
                gate._require(before == self.permit["before"], "migration precondition changed before transaction")
                conn.execute(f"CREATE TABLE {JOURNAL}(operation_id TEXT NOT NULL,step_number INTEGER PRIMARY KEY,record TEXT NOT NULL)")
            conn.controlled_step = True
            details = steps.apply_step(conn, step, now=int(authority["wall"]))
            after = database_fingerprint(conn)
            gate._require(after["retained_outbox"] == self.permit["before"]["retained_outbox"], "retained export originals changed")
            if number == len(steps.STEP_IDS) - 1:
                gate._require(after["schema_sha256"] == self.permit["target_schema_sha256"], "target schema postcondition differs")
            record = {"permit_sha256": gate._hash(self.permit), "step_id": step,
                      "before": before, "after": after, "details": details}
            conn.execute(f"INSERT INTO {JOURNAL} VALUES(?,?,?)", (self.permit["operation_id"], number, gate._encoded(record).decode()))
            # A lock/session/config/window failure during the business SQL
            # must roll back this same transaction, including its marker.
            self._authorization(active=True)
            self._page_budget(conn)
            gate._require(time.monotonic() <= deadline, "migration step deadline exceeded")
            conn.controlled_step = False
            conn.execute("COMMIT")
        except BaseException as error:
            conn.controlled_step = False
            conn.set_progress_handler(None, 0)
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if failures:
                raise failures[0] from error
            raise
        # PG and SQLite cannot commit atomically. Loss after SQLite COMMIT
        # leaves a real journal row and an uncertain operation; never rerun it.
        self.check()
        isolation._confirm_database(self.path)

    def query_migration(self, operation_id):
        self._authorization()
        gate._require(operation_id == self.permit["operation_id"], "migration query operation differs")
        attempt = isolation._read_complete(self.root / "migration.intent.json")
        gate._require(attempt == {"operation_id": operation_id, "permit_sha256": gate._hash(self.permit)}, "migration intent differs")
        conn = _connection(self.path, readonly=True)
        try:
            rows = _journal_rows(conn, self.permit)
            complete = len(rows) == len(steps.STEP_IDS)
            if complete:
                steps.verify_final(conn)
                gate._require(rows[-1]["after"]["schema_sha256"] == self.permit["target_schema_sha256"], "target schema differs")
        finally:
            conn.close()
        receipt = {"schema": SCHEMA, "operation_id": operation_id, "permit_sha256": gate._hash(self.permit),
                   "status": "migration_completed" if complete else "migration_uncertain", "completed_steps": [r["step_id"] for r in rows],
                   "database": self.permit["database"]["file"], "postcondition": rows[-1]["after"] if rows else self.permit["before"],
                   "durable": complete, "business_open": False, "switch_authorized": False,
                   "next_allowed_action": "query_original_operation"}
        if complete:
            isolation._confirm_database(self.path)
            gate._write(self.root / "migration.json", receipt)
        return receipt


@contextmanager
def locked_session(root, permit):
    session = LockedMigration(root, permit)
    try:
        seen = set()
        for name in LOCK_ORDER[:4]:
            expected = permit["locks"][name]
            gate._require(file_identity(expected["path"]) == expected, "migration lock differs before acquisition")
            key = (expected["device"], expected["inode"])
            gate._require(key not in seen, "duplicate migration lock identity")
            seen.add(key)
            fd = gate._lock(Path(expected["path"]), True, 0.1)
            session.fds.append((name, fd))
        fence_root = Path(permit["freeze"]["root"])
        gate._require(all(Path(permit["locks"][name]["path"]) == fence_root / "locks" / (name + ".lock")
                          for name in ("controller", "activity")), "migration admission lock mapping differs")
        with ExitStack() as stack:
            # Hold an existing SQLite read handle while the independently
            # verified storage and PostgreSQL locks remain live. We never
            # create a missing DB/WAL/SHM just to make authorization pass.
            _same_database(session.path, permit["database"], initial=not gate._present(session.root))
            anchor = _connection(session.path, readonly=True)
            stack.callback(anchor.close)
            _retain_inventory(session, stack)
            session.check()
            yield session
            session.check()
    finally:
        for _name, fd in reversed(session.fds):
            os.close(fd)
        session.fds.clear()


def _postgres_reader(permit):
    from scripts.sender_drain_inventory import PostgresReader
    sender = permit["sender"]
    gate._require(set(sender) == {"dsn_file", "dsn_sha256", "expected_identity", "closed_operation_id", "manifest"},
                  "invalid sender lock specification")
    path = Path(sender["dsn_file"])
    file_identity(path)
    gate._require(path.stat().st_size <= 16384 and hashlib.sha256(path.read_bytes()).hexdigest() == sender["dsn_sha256"],
                  "sender credentials reference changed")
    return PostgresReader(path, sender["expected_identity"])


def _retain_inventory(session, stack):
    from scripts import independent_drain_inventory as inventory
    permit = session.permit
    spec = permit["inventory"]
    gate._require(spec["database"] == str(session.path)
                  and all(permit["locks"][name] == file_identity(Path(spec[name]) / ".lock")
                          for name in ("spool", "journal")), "inventory storage lock mapping differs")
    reader = _postgres_reader(permit)

    def sample_fixed_sender():
        session.sender = stack.enter_context(reader.locked(permit["sender"]["closed_operation_id"], permit["sender"]["manifest"]))
        return session.sender.snapshot

    session.inventory = stack.enter_context(inventory.locked_inventory(spec, postgres=sample_fixed_sender, seconds=5))
    gate._require(session.inventory["blockers"] == [] and session.inventory["storage_drained"]
                  and inventory_fingerprint(session.inventory) == permit["inventory_sha256"],
                  "independent frozen inventory differs or is not drained")


def inventory_fingerprint(value):
    # Free disk is a current lower-bound check, not an immutable storage
    # identity. A committed migration or an unrelated log must not prevent
    # querying the original operation after an ACK was lost.
    receiver = {key: item for key, item in value["receiver"].items() if key != "physical_free_bytes"}
    from scripts.sender_drain_inventory import stable_inventory
    return gate._hash({**value, "receiver": receiver, "sender": stable_inventory(value["sender"])})
