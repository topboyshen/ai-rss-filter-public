"""Synthetic old receiver storage for the actual retained sender bootstrap run.

Only this module creates synthetic fixture data. The metadata action itself is
the production receiver bootstrap, with real legacy/release/PG capabilities.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import time

from services import admission_fence as gate
from scripts import production_receiver_bootstrap as receiver

ROOT = Path(__file__).resolve().parents[1]
MAX_ORIGINAL = 1024**2


def sources():
    names = ("scripts/production_receiver_bootstrap_native.py", "scripts/production_receiver_bootstrap.py",
             "scripts/production_sender_bootstrap_native.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def write(path, value):
    raw = value if isinstance(value, bytes) else gate._encoded(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    gate._sync_directory(path.parent)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def original(path):
    """A small synthetic regular file, read without altering the original."""
    expected = receiver.migration.file_identity(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        gate._require(info.st_size <= MAX_ORIGINAL and info.st_ino == expected["inode"], "synthetic original changed or oversized")
        chunks, count = [], 0
        while count <= info.st_size:
            block = os.read(fd, min(65536, info.st_size + 1 - count))
            if not block:
                break
            chunks.append(block)
            count += len(block)
        raw = b"".join(chunks)
        gate._require(len(raw) == info.st_size and receiver.migration.file_identity(path) == expected
                      and (os.fstat(fd).st_mtime_ns, os.fstat(fd).st_ctime_ns) == (info.st_mtime_ns, info.st_ctime_ns),
                      "synthetic original changed during read")
    finally:
        os.close(fd)
    return {"identity": expected, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, raw


def prepare(root):
    data = root / "receiver-originals"
    data.mkdir(mode=0o700)
    spool, journal, database = data / "webhook_spool", data / "natural_score_continuations", data / "cache.db"
    for path in (spool, journal, spool / "inflight", spool / "dead-letter", journal / ".locks"):
        path.mkdir(mode=0o700)
    release = root / "receiver-release.lock"
    for path in (spool / ".lock", journal / ".lock", release, database):
        write(path, b"")
    task = {"type": "refresh", "entries": []}
    digest = gate._hash(task)
    completed, terminal = spool / ".completed-digests", spool / "dead-letter" / (digest + ".1.terminal.json")
    write(completed, {digest: 123})
    write(terminal, {"version": 1, "digest": digest, "task": task, "attempts": 0})
    strategy, link, key = receiver.inventory.continuations.identity("synthetic-receiver-metadata", "https://example.invalid/retained")
    entry = journal / (key + ".json")
    write(entry, {"schema": 1, "article_key": key, "link": link, "strategy_version": strategy,
                  "state": "exhausted", "attempts": 3, "created_at": 100, "updated_at": 101, "next_attempt_at": 102})
    identity_lock = journal / ".locks" / (key + ".lock")
    write(identity_lock, b"")
    anchor = sqlite3.connect(database)
    try:
        anchor.execute("PRAGMA journal_mode=WAL")
        anchor.execute("PRAGMA synchronous=FULL")
        anchor.execute("CREATE TABLE app_meta(k TEXT PRIMARY KEY,v TEXT)")
        anchor.executemany("INSERT INTO app_meta VALUES(?,?)", zip(receiver.inventory.META_KEYS, ("1", "1", "100")))
        anchor.commit()
        for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
            path.chmod(0o600)  # Only these newly created synthetic files.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        gate._sync_directory(data)
        files = (spool / ".lock", journal / ".lock", completed, terminal, entry, identity_lock,
                 database, Path(str(database) + "-wal"))
        originals = {str(path): original(path)[0] for path in files}
        p = {"data": data, "spool": spool, "journal": journal, "database": database, "anchor": anchor,
             "release_lock": release, "originals": originals, "database_fingerprint": receiver.migration.database_fingerprint(anchor)}
        write(root / "RECEIVER_OLD_STORAGE.json", {"sources": sources(), "originals": originals,
            "receipt_metadata_absent": not (spool / "receipts").exists(), "synthetic_sqlite_schema": "publication_metadata_only",
            "database_identity": receiver.migration.database_identity(database), "historical_coverage_proven": False})
        return p
    except BaseException:
        anchor.close()
        raise


def preserve(root, p):
    """Copy only the tiny synthetic SQLite originals before fixture close."""
    try:
        directory = root / "receiver-sqlite-original-copies"
        directory.mkdir(mode=0o700)
        copied = {}
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(p["database"]) + suffix)
            identity, raw = original(source)
            target = directory / ("cache.db" + suffix)
            copied[str(source)] = {"original": identity, "copy": write(target, raw)}
        write(root / "RECEIVER_SQLITE_PRESERVED.json", {"copies": copied,
            "copied_before_synthetic_anchor_close": True, "production_database_access": False,
            "note": "Closing the fixture anchor may checkpoint/remove sidecars; exact original copies are retained."})
    finally:
        p["anchor"].close()


@contextmanager
def prepared(root):
    p, primary = prepare(root), None
    try:
        yield p
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            preserve(root, p)
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note("Synthetic receiver SQLite preservation failed: " + type(error).__name__)


def reference(path):
    raw, _identity = receiver.private.private_file(path)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def plan(root, p, cap, release, driver, revision):
    now = int(time.time())
    spec = {"spool": str(p["spool"]), "journal": str(p["journal"]), "database": str(p["database"]),
        "identities": {"spool": receiver.inventory.directory_identity(p["spool"]),
                       "journal": receiver.inventory.directory_identity(p["journal"]),
                       "database": receiver.inventory._identity(p["database"].lstat())}}
    return {"schema": receiver.SCHEMA, "operation_id": gate._hash({"receiver-bootstrap": cap.plan["operation_id"]}),
        "revision": revision, "sources": receiver.sources(), "approved_by": "synthetic-native-no-production",
        "not_before": now - 1, "expires_at": min(now + 90, driver.plan["window"]["expires_at"]),
        "host_boot_id": cap.plan["host_boot_id"], "legacy_operation_id": cap.plan["operation_id"],
        "legacy_plan_sha256": gate._hash(cap.plan), "release_lock": release.identity, "inventory": spec,
        "database_identity": receiver.migration.database_identity(p["database"]),
        "database_fingerprint": p["database_fingerprint"],
        "sender": {"plan": reference(driver.root.parent / "driver-plan.json"), "dsn": reference(driver.postgres.path),
                   "token": reference(driver.transport.token_file), "url": f"http://127.0.0.1:{driver.transport.port}",
                   "evidence_root": str(driver.root), "closed_operation_id": driver.plan["binding"]["initial_operation_id"]}}


def locked_precheck(value, cap, release, driver, evidence):
    """Observe the same real lock implementation in a separate RO precheck."""
    with receiver._locked(value, cap, release, driver, evidence, active=True) as state:
        refusals = []
        for path in state["locks"]:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    refusals.append(path)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    raise gate.AdmissionError("actual receiver storage lock was not exclusive")
            finally:
                os.close(descriptor)
        release.check()
        state["frozen"].check()
        return {"phase": "separate_readonly_precheck_same_production_lock_implementation",
                "exclusive_storage_paths": sorted(refusals), "release_identity": release.identity,
                "sender_locked_inventory_sha256": gate._hash(state["sender"]),
                "postgres_reader_same_instance": state["frozen"].reader is driver.postgres}


def exercise(root, p, cap, release, driver, revision):
    evidence = root / "receiver-bootstrap"
    evidence.mkdir(mode=0o700)
    value = plan(root, p, cap, release, driver, revision)
    write(root / "receiver-bootstrap-plan.json", value)
    gate._require(not (p["spool"] / "receipts").exists(), "synthetic old receipt metadata already exists")
    locks = locked_precheck(value, cap, release, driver, evidence)
    result = receiver.execute(cap, release, driver, value, evidence)
    queried = receiver.query(cap, release, driver, value, evidence)
    gate._require(queried["query_only"] is True and all(queried[key] == item for key, item in result.items()),
                  "actual receiver original-operation query differs")
    try:
        receiver.execute(cap, release, driver, value, evidence)
    except gate.AdmissionError as error:
        gate._require("already attempted" in str(error), "unexpected repeated receiver action refusal")
    else:
        raise gate.AdmissionError("receiver metadata initialization repeated")
    duplicate_stop = receiver._read(evidence / "STOP.json")
    gate._require(duplicate_stop["operation_id"] == value["operation_id"], "duplicate attempt STOP differs")
    for path, before in p["originals"].items():
        gate._require(original(Path(path))[0] == before, "receiver bootstrap changed a retained original")
    gate._require(receiver.migration.database_fingerprint(p["anchor"]) == p["database_fingerprint"], "synthetic SQLite contents changed")
    for path in (p["spool"] / "receipts", p["spool"] / "receipts/tasks"):
        gate._require(stat.S_IMODE(path.stat().st_mode) == 0o700, "receipt metadata directory mode differs")
    gate._require({path.name for path in (p["spool"] / "receipts").iterdir()} == {"tasks"}
                  and not list((p["spool"] / "receipts/tasks").iterdir()), "metadata bootstrap created a delivery original")
    before, after = receiver._read(evidence / "before.json"), receiver._read(evidence / "after.json")
    gate._require(before == after, "production independent old inventory changed")
    proof = {"status": "passed", "sources": sources(), "result": result, "query": queried, "lock_precheck": locks,
        "originals": p["originals"], "before_sha256": gate._hash(before), "after_sha256": gate._hash(after),
        "legacy_operation_id": cap.plan["operation_id"], "sender_plan_sha256": driver.plan_sha256,
        "sender_postgres_identity": driver.postgres.expected, "duplicate_execute_rejected": True,
        "deliberate_duplicate_stop_sha256": gate._hash(duplicate_stop),
        "real_fsync_returned": True, "power_loss_proven": False, "production_access": False, "real_models": 0,
        "protocol_T0_established": False, "migration_authorized": False, "historical_raw_coverage": "unknown_not_replayed"}
    write(root / "RECEIVER_BOOTSTRAP_PROOF.json", proof)
    return proof
