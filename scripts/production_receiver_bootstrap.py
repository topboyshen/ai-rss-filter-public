"""One-time receipt metadata preparation after independently proved legacy stop.

This never drains work, creates receipts, repairs partial directories, migrates
SQLite, or claims historical delivery coverage. M1 must reacquire and resample.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from scripts import private_release_contract as private  # noqa: E402
from scripts import independent_drain_inventory as inventory  # noqa: E402
from scripts import durable_handoff_cutover as records  # noqa: E402
from scripts.production_legacy_takeover import LegacyTakeover, locked_takeover  # noqa: E402
from services import admission_fence as gate, production_migration as migration  # noqa: E402
from services.production_release_lock import RetainedReleaseLock, retained_release_lock  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
try:
    from production_sender_driver import SenderActivationDriver, NativeSenderActivationTransport, PostgresReader
    from sender_drain_inventory import FrozenPostgres, stable_inventory
finally:
    sys.path.remove(str(ROOT / "scripts"))

SCHEMA = "production-receiver-metadata-bootstrap-v1"
PLAN_KEYS = {"schema", "operation_id", "revision", "sources", "approved_by", "not_before", "expires_at",
             "host_boot_id", "legacy_operation_id", "legacy_plan_sha256", "release_lock", "inventory",
             "database_identity", "database_fingerprint", "sender"}


def sources():
    names = ("scripts/production_receiver_bootstrap.py", "scripts/production_legacy_takeover.py",
             "scripts/independent_drain_inventory.py", "scripts/observe_handoff_capacity.py",
             "scripts/production_sender_driver.py", "scripts/sender_drain_inventory.py",
             "services/production_release_lock.py", "services/production_migration.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def _file(record):
    gate._require(isinstance(record, dict) and set(record) == {"path", "sha256"}, "exact private bootstrap reference required")
    raw, _identity = private.private_file(record["path"])
    gate._require(hashlib.sha256(raw).hexdigest() == record["sha256"], "bootstrap input changed")
    return gate._parsed(raw.decode())


def _read(path):
    """Only an actual final file can be reconfirmed; pending originals stay held."""
    private.safe_path(path)
    gate._require(gate._present(path) and not gate._present(path.with_name(path.name + ".pending")),
                  "bootstrap record outcome unknown; inspect original only")
    value = records.read_private(path)
    gate._confirm_file(path)
    gate._sync_directory(path.parent)
    gate._require(records.read_private(path) == value, "bootstrap original changed during confirmation")
    return value


def _driver(plan):
    spec = plan["sender"]
    sender_plan = _file(spec["plan"])
    _file(spec["dsn"])
    _file(spec["token"])
    identifier = sender_plan["binding"]["component"]["id"]
    gate._require(isinstance(identifier, str) and gate.HEX64.fullmatch(identifier), "exact sender namespace required")
    transport = NativeSenderActivationTransport(spec["url"], spec["token"]["path"], container_id=identifier)
    return SenderActivationDriver(sender_plan, transport,
        PostgresReader(spec["dsn"]["path"], sender_plan["postgres_identity"]), spec["evidence_root"])


def _validate(plan, root, legacy, release, driver, *, active):
    gate._require(isinstance(plan, dict) and set(plan) == PLAN_KEYS and plan["schema"] == SCHEMA,
                  "exact receiver bootstrap plan required")
    gate._require(type(legacy) is LegacyTakeover and type(release) is RetainedReleaseLock
                  and type(driver) is SenderActivationDriver and type(driver.transport) is NativeSenderActivationTransport
                  and type(driver.postgres) is PostgresReader, "actual retained capabilities required")
    gate._require(isinstance(plan["operation_id"], str) and gate.HEX64.fullmatch(plan["operation_id"])
                  and plan["sources"] == sources() and plan["revision"] == private.source_identity(ROOT)["source_sha"],
                  "bootstrap clean source binding differs")
    gate._require(isinstance(plan["approved_by"], str) and 0 < len(plan["approved_by"]) <= 128
                  and all(type(plan[key]) is int for key in ("not_before", "expires_at"))
                  and 0 < plan["expires_at"] - plan["not_before"] <= 600, "finite reviewed bootstrap required")
    gate._require(not active or plan["not_before"] <= time.time() < plan["expires_at"], "bootstrap window expired")
    release.check()
    gate._require(plan["release_lock"] == release.identity and plan["host_boot_id"] == legacy.plan["host_boot_id"],
                  "bootstrap host/release lock differs")
    legacy.check(active=active)
    stopped = legacy.check_stopped()
    gate._require(stopped["operation_id"] == plan["legacy_operation_id"]
                  and stopped["plan_sha256"] == plan["legacy_plan_sha256"], "original legacy stop differs")
    spec = plan["sender"]
    gate._require(set(spec) == {"plan", "dsn", "token", "url", "evidence_root", "closed_operation_id"},
                  "exact original sender references required")
    gate._require(driver.plan == _file(spec["plan"]) and driver.postgres.path == Path(spec["dsn"]["path"])
                  and driver.transport.token_file == Path(spec["token"]["path"])
                  and driver.root == Path(spec["evidence_root"]) and driver.postgres.expected == driver.plan["postgres_identity"]
                  and spec["url"].rstrip("/") == f"http://127.0.0.1:{driver.transport.port}"
                  and driver.transport.container_id == driver.plan["binding"]["component"]["id"], "sender binding differs")
    _file(spec["dsn"])
    _file(spec["token"])
    gate._require(driver.transport.container_id not in {row["id"] for row in legacy.plan["containers"].values()},
                  "new sender aliases old runtime")
    gate._directory(private.safe_path(root))
    return stopped


def _layout(plan, legacy, root):
    spec = plan["inventory"]
    gate._require(set(spec) == {"spool", "journal", "database", "identities"}
                  and set(spec["identities"]) == {"spool", "journal", "database"}, "exact original inventory required")
    bases = []
    for role in ("api", "worker"):
        expected = legacy.plan["containers"][role]
        row = legacy.transport.inspect(legacy.plan["docker"]["path"], expected["id"])
        from scripts.production_legacy_takeover import container_identity
        gate._require(container_identity(row) == expected, "old data container identity changed")
        mounts = [mount for mount in row["Mounts"] if mount.get("Destination") == "/app/data"]
        gate._require(len(mounts) == 1 and mounts[0].get("Type") == "bind" and mounts[0].get("RW") is True,
                      "single actual retained data bind required")
        base = private.safe_path(mounts[0]["Source"])
        env = private.runtime_environment(row["Config"].get("Env"))
        expected = {"spool": env.get("WEBHOOK_SPOOL_DIR", "/app/data/webhook_spool"),
                    "database": env.get("AI_FILTER_DB_PATH", "/app/data/cache.db")}
        for key, value in expected.items():
            target = Path(value)
            gate._require(target.is_absolute() and str(target) == str(Path("/app/data") / target.relative_to("/app/data"))
                          and ".." not in target.parts and target != Path("/app/data")
                          and Path(spec[key]) == base / target.relative_to("/app/data"), "old actual data path differs")
        gate._require(Path(spec["journal"]) == Path(spec["database"]).parent / "natural_score_continuations",
                      "actual old continuation journal required")
        bases.append(base)
    gate._require(bases[0] == bases[1], "old app roles use different data roots")
    paths = [private.safe_path(spec[key]) for key in ("spool", "journal", "database")]
    gate._require(len(set(paths)) == 3 and all(path != bases[0] and path.is_relative_to(bases[0]) for path in paths)
                  and not paths[0].is_relative_to(paths[1]) and not paths[1].is_relative_to(paths[0])
                  and not any(paths[2].is_relative_to(path) for path in paths[:2]), "original data paths overlap or escape")
    gate._require(not root.is_relative_to(bases[0]) and not bases[0].is_relative_to(root), "bootstrap evidence aliases old data")
    for key in ("spool", "journal"):
        gate._require(inventory.directory_identity(spec[key]) == spec["identities"][key], "original storage identity changed")
    # database_identity rejects one-sided sidecars; a fully checkpointed closed
    # database may legitimately retain neither. Never manufacture an anchor.
    gate._require(migration.database_identity(paths[2]) == plan["database_identity"],
                  "existing SQLite identity/WAL/SHM differs")
    gate._require(inventory._identity(paths[2].lstat()) == spec["identities"]["database"], "original database differs")
    return paths


def _publication(connection, reader):
    sql_deadline = min(reader.deadline, time.monotonic() + 3)
    connection.set_progress_handler(lambda: int(time.monotonic() > sql_deadline), 1000)
    rows = connection.execute("SELECT k,v FROM app_meta WHERE k IN (?,?,?)", inventory.META_KEYS).fetchmany(4)
    gate._require(len(rows) == 3 and {row[0] for row in rows} == set(inventory.META_KEYS), "publication metadata missing")
    values = {}
    for key, value in rows:
        gate._require(isinstance(value, str) and value.isascii() and value.isdecimal(), "invalid publication counter")
        values[key] = int(value)
    return {"input": values[inventory.META_KEYS[0]], "published": values[inventory.META_KEYS[1]],
            "published_at": values[inventory.META_KEYS[2]], "read_only": True}


def _database(plan, reader):
    path = Path(plan["inventory"]["database"])
    gate._require(migration.database_identity(path) == plan["database_identity"], "SQLite sidecar identity changed")
    suffix = "?mode=ro"
    if plan["database_identity"]["wal"] is None:
        suffix += "&immutable=1"  # Retained stopped writers + no WAL: do not create sidecars.
    connection = sqlite3.connect(path.as_uri() + suffix, timeout=0, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        sql_deadline = min(reader.deadline, time.monotonic() + 3)
        connection.set_progress_handler(lambda: int(time.monotonic() > sql_deadline), 1000)
        connection.execute("BEGIN")
        value = migration.database_fingerprint(connection)
        gate._require(value == plan["database_fingerprint"], "old database schema/metadata differs")
        publication = _publication(connection, reader)
        gate._require(publication["input"] == publication["published"], "old publication remains unfinished")
        return {"identity": plan["database_identity"], "fingerprint": value, "publication": publication}
    finally:
        connection.close()
        gate._require(migration.database_identity(path) == plan["database_identity"], "SQLite identity changed during read")


def _snapshot(plan, locks):
    reader = inventory.Reader(5)
    spool, journal = (Path(plan["inventory"][key]) for key in ("spool", "journal"))
    counts = inventory._queue(reader, spool)
    states, found = inventory._journal(reader, journal)
    gate._require({str(path) for path in found} == {key for key in locks if key not in {str(spool / ".lock"), str(journal / ".lock")}},
                  "journal lock inventory changed")
    for path in locks:
        gate._require(reader.read(Path(path)) == b"", "nonempty retained storage lock")
    gate._require(not any(value for key, value in counts.items() if key != "retained_dead_letters")
                  and not any(states[key] for key in inventory.continuations.ACTIVE_STATES), "old known work is not drained")
    dirs = [spool, journal, spool / "inflight", spool / "dead-letter"]
    if gate._present(journal / ".locks"):
        dirs.append(journal / ".locks")
    value = {"counts": counts, "journal": states, "database": _database(plan, reader),
             "directories": {str(path): migration.file_identity(path, directory=True) for path in dirs},
             "files": {path: {"sha256": digest, "identity": migration.file_identity(path)}
                       for path, digest in sorted(reader.fingerprints.items())}}
    gate._require(len(records.encoded(value)) <= records.MAX_BYTES, "retained metadata evidence budget exceeded")
    return value


@contextmanager
def _locked(plan, legacy, release, driver, root, *, active):
    stopped = _validate(plan, root, legacy, release, driver, active=active)
    paths = _layout(plan, legacy, root)
    operation = plan["sender"]["closed_operation_id"]
    receipt = driver.query_initialize_closed(operation)
    gate._require(receipt["operation_id"] == operation and receipt["state"] == "closed", "original sender CLOSED unconfirmed")
    with ExitStack() as stack:
        frozen = stack.enter_context(driver.postgres.locked(operation, driver.plan["binding"]["manifest"]))
        gate._require(type(frozen) is FrozenPostgres and frozen.reader is driver.postgres, "actual retained sender locks required")
        locks = {}
        def retain(path):
            fd = stack.enter_context(inventory.existing_lock(path))
            locks[str(path)] = (fd, migration.file_identity(path))
        for path in (paths[0] / ".lock", paths[1] / ".lock"):
            retain(path)
        _, found = inventory._journal(inventory.Reader(5), paths[1])
        for path in sorted(found):
            retain(path)
        state = {"legacy": stopped, "sender": stable_inventory(frozen.snapshot), "locks": locks, "frozen": frozen}
        _recheck(plan, legacy, release, driver, root, state, active=active)
        yield state
        _recheck(plan, legacy, release, driver, root, state, active=active)


def _recheck(plan, legacy, release, driver, root, state, *, active):
    gate._require(_validate(plan, root, legacy, release, driver, active=active) == state["legacy"], "retained old stop changed")
    _layout(plan, legacy, root)
    from services.production_release_lock import _identity
    for path, (fd, expected) in state["locks"].items():
        gate._require(_identity(path, fd) == expected, "retained storage lock changed")
    state["frozen"].check()
    gate._require(stable_inventory(state["frozen"].latest_snapshot) == state["sender"], "retained sender inventory changed")


def _metadata(spool):
    directory, tasks = spool / "receipts", spool / "receipts/tasks"
    result = {str(path): migration.file_identity(path, directory=True) for path in (directory, tasks)}
    gate._require({path.name for path in directory.iterdir()} == {"tasks"} and not list(tasks.iterdir()),
                  "receipt bootstrap contains unknown or delivery originals")
    return result


def _stop(root, plan, error):
    try:
        if not gate._present(root / "STOP.json"):
            records.durable_write(root / "STOP.json", {"operation_id": plan["operation_id"], "error_type": type(error).__name__,
                "message_sha256": hashlib.sha256(str(error).encode()).hexdigest(), "next_action": "query_original_only"})
    except BaseException as secondary:
        error.add_note("Receiver bootstrap STOP preservation failed: " + type(secondary).__name__)


def _execute(legacy, release, driver, plan, evidence_root):
    root = Path(evidence_root)
    with _locked(plan, legacy, release, driver, root, active=True) as state:
        gate._require(not list(root.iterdir()), "receiver bootstrap already attempted; query original only")
        spool = Path(plan["inventory"]["spool"])
        gate._require(not gate._present(spool / "receipts"), "existing receipt metadata requires separate review")
        before = _snapshot(plan, state["locks"])
        intent = {"schema": SCHEMA, "operation_id": plan["operation_id"], "plan_sha256": gate._hash(plan),
                  "before_sha256": gate._hash(before), "legacy": state["legacy"], "sender": state["sender"]}
        try:
            records.durable_write(root / "before.json", before)
            records.durable_write(root / "intent.json", intent)
            _recheck(plan, legacy, release, driver, root, state, active=True)
            for path in (spool / "receipts", spool / "receipts/tasks"):
                path.mkdir(mode=0o700)
                gate._sync_directory(path)
                gate._sync_directory(path.parent)
            gate._sync_directory(spool.parent)
            metadata = _metadata(spool)
            records.durable_write(root / "created.json", metadata)
            after = _snapshot(plan, state["locks"])
            gate._require(after == before, "retained originals changed during metadata preparation")
            records.durable_write(root / "after.json", after)
            _recheck(plan, legacy, release, driver, root, state, active=True)
            result = {"schema": SCHEMA, "operation_id": plan["operation_id"], "intent_sha256": gate._hash(intent),
                      "before_sha256": gate._hash(before), "after_sha256": gate._hash(after), "metadata": metadata,
                      "status": "new_metadata_prepared", "historical_raw_coverage": "unknown_not_replayed",
                      "protocol_T0_established": False, "migration_authorized": False, "business_open": False}
            records.durable_write(root / "final.json", result)
            return result
        except BaseException as error:
            _stop(root, plan, error)
            raise


def execute(legacy, release, driver, plan, evidence_root):
    try:
        return _execute(legacy, release, driver, plan, evidence_root)
    except BaseException as error:
        root = Path(evidence_root)
        if gate._present(root / "intent.json") or gate._present(root / "intent.json.pending"):
            _stop(root, plan, error)
        raise


def query(legacy, release, driver, plan, evidence_root):
    root = Path(evidence_root)
    with _locked(plan, legacy, release, driver, root, active=False) as state:
        intent = _read(root / "intent.json")
        gate._require(intent["operation_id"] == plan["operation_id"] and intent["plan_sha256"] == gate._hash(plan),
                      "query original receiver bootstrap only")
        gate._require(intent["legacy"] == state["legacy"] and intent["sender"] == state["sender"], "original frozen context differs")
        before = _read(root / "before.json")
        gate._require(gate._hash(before) == intent["before_sha256"], "original retained inventory differs")
        gate._require(_snapshot(plan, state["locks"]) == before, "retained originals changed after metadata attempt")
        if not gate._present(root / "final.json") or gate._present(root / "final.json.pending"):
            return {"status": "bootstrap_uncertain", "operation_id": plan["operation_id"],
                    "next_action": "inspect_original_only", "metadata_recreated": False, "protocol_T0_established": False}
        result = _read(root / "final.json")
        created, after = _read(root / "created.json"), _read(root / "after.json")
        gate._require(result["operation_id"] == plan["operation_id"] and result["intent_sha256"] == gate._hash(intent)
                      and result["before_sha256"] == result["after_sha256"] == gate._hash(before)
                      and after == before and result["metadata"] == created == _metadata(Path(plan["inventory"]["spool"])),
                      "original completed metadata receipt differs")
        return {**result, "query_only": True, "prior_stop_preserved": gate._present(root / "STOP.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("execute", "query"))
    for name in ("plan", "legacy-plan", "legacy-evidence", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    raw, _identity = private.private_file(args.plan)
    plan = gate._parsed(raw.decode())
    driver = _driver(plan)
    with retained_release_lock(plan["release_lock"]["path"]) as release:
        with locked_takeover(args.legacy_plan, args.legacy_evidence) as legacy:
            result = (execute if args.action == "execute" else query)(legacy, release, driver, plan, args.evidence)
    print(json.dumps({key: result[key] for key in ("status", "operation_id", "protocol_T0_established")}, sort_keys=True))


if __name__ == "__main__":
    main()
