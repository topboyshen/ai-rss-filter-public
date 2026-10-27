"""Actual read-only PostgreSQL producer-stop inventory; never recreate old bodies."""
from __future__ import annotations

import hashlib
import os
import math
from pathlib import Path
import time

from scripts.durable_handoff_cutover import encoded, private_directory, require, sync_directory
from scripts import observe_handoff_capacity as capacity
from services.sender_drain_contract import (IDENTITY_KEYS, MAX_BODY_BYTES, MAX_MANIFEST_BYTES,
    source_sha256, validate_identity, validate_stop_receipt, validate_entry, validate_manifest, validate_binding)

# Miniflux stores version as text. Normalize only the exact supported ASCII
# marker, without trim/cast coercion of malformed or future versions.
IDENTITY_SQL = capacity.PG_IDENTITY_SQL + """, 'handoff_state'::regclass::oid::bigint,
 'handoff_operations'::regclass::oid::bigint, 'handoff_leases'::regclass::oid::bigint,
 (SELECT CASE WHEN version COLLATE "C" = '134' THEN 134 ELSE NULL END FROM schema_version), current_user, session_user"""
ROWS_SQL = """SELECT delivery_id,body_sha256,body,target_url,signature,contract,user_id,feed_id,entry_id,state,attempts
 FROM webhook_outbox WHERE state IN ('pending','inflight') ORDER BY delivery_id LIMIT 5001"""


def stable_inventory(value):
    """Identity projection only; retain the actual age diagnostic in reports.

    ACKs can cross seven days while every protocol lock remains held. This
    diagnostic never grants cleanup credit or permission to remove originals.
    All other keys, including bytes, states, held rows and credit, stay bound.
    """
    return {key: item for key, item in value.items() if key != "acked_at_least_7_days"}



class _TimedCursor:
    """Every SQL command shares the original wall deadline and <=3s SQL cap."""
    def __init__(self, cursor, deadline):
        self.cursor, self.deadline = cursor, deadline

    def remaining(self):
        left = self.deadline - time.monotonic()
        require(left > 0, "sender PostgreSQL overall deadline exceeded")
        return left

    def execute(self, sql, params=None):
        milliseconds = max(1, min(3000, int(self.remaining() * 1000)))
        self.cursor.execute("SELECT set_config('statement_timeout',%s,false)", (str(milliseconds),))
        self.remaining()
        result = self.cursor.execute(sql) if params is None else self.cursor.execute(sql, params)
        self.remaining()
        return result

    def fetchone(self):
        self.remaining()
        value = self.cursor.fetchone()
        self.remaining()
        return value

    def fetchall(self):
        self.remaining()
        value = self.cursor.fetchall()
        self.remaining()
        return value

    def __iter__(self):
        for value in self.cursor:
            self.remaining()
            yield value


def _deadline(value=None):
    return min(time.monotonic() + 10, value) if value is not None else time.monotonic() + 10


def _connect(connect, dsn, deadline):
    remaining = deadline - time.monotonic()
    # libpq treats connect_timeout below 2s as 2s. Do not start another
    # connection when that minimum no longer fits the shared remaining bound.
    require(remaining >= 2, "insufficient remaining PostgreSQL connect budget")
    connection = connect(dsn, connect_timeout=min(3, math.floor(remaining)))
    if time.monotonic() >= deadline:
        connection.close()
        require(False, "PostgreSQL connection exceeded overall deadline")
    return connection

def write_blob(path, raw, maximum):
    require(len(raw) <= maximum, "private original size limit")
    private_directory(path.parent)
    require(not path.exists() and not path.is_symlink(), "original already sealed; load exact bundle only")
    pending = path.with_name(path.name + ".pending")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(pending, path)
    sync_directory(path.parent)


def _read_gate(cursor, receipt):
    cursor.execute("SELECT manifest,generation,phase,current_operation FROM handoff_state WHERE id=1")
    require(cursor.fetchone() == (receipt["manifest"], receipt["generation"], "draining", receipt["operation_id"]),
            "actual PostgreSQL gate differs from confirmed stop-new")
    cursor.execute("""SELECT manifest,action,previous_generation,generation,status FROM handoff_operations
 WHERE operation_id=%s""", (receipt["operation_id"],))
    require(cursor.fetchone() == (receipt["manifest"], "stop_new", receipt["previous_generation"], receipt["generation"], "confirmed"),
            "actual PostgreSQL producer-stop marker differs")
    cursor.execute("""SELECT count(*),count(*) FILTER(WHERE status='active' AND source NOT IN ('outbox_pass','outbox_sender')),
 count(*) FILTER(WHERE status='uncertain' AND source NOT IN ('outbox_pass','outbox_sender')) FROM handoff_leases""")
    records, active, uncertain = cursor.fetchone()
    require(type(records) is int and 0 <= records <= 10000 and active == uncertain == 0, "producer or retained lease budget differs")
    return {"records": records, "limit": 10000, "remaining": 10000 - records}


def _row(value):
    delivery, body_sha, body, target, signature, contract, user, feed, entry, state, attempts = value
    raw = bytes(body)
    row = {"delivery_id": delivery, "body_sha256": body_sha, "body_size": len(raw), "body_file": delivery + ".body",
           "target_url": target, "signature": signature, "contract": contract, "user_id": user, "feed_id": feed,
           "entry_id": entry, "state": state, "attempts": attempts}
    validate_entry(row)
    require(hashlib.sha256(raw).hexdigest() == delivery, "PostgreSQL body SHA differs")
    return row, raw


def freeze_bundle(connect, dsn, expected, binding, receipt, window, root):
    """Actual bounded read-only PG snapshot. Never resample a partially written bundle."""
    validate_identity(expected)
    validate_binding(binding)
    validate_stop_receipt(receipt, binding)
    root = Path(root)
    require(not root.exists() and not root.is_symlink(), "bundle already attempted; preserve original")
    private_directory(root, create=True)
    deadline = _deadline()
    connection = _connect(connect, dsn, deadline)
    try:
        connection.autocommit = True
        with connection.cursor() as raw_cursor:
            cursor = _TimedCursor(raw_cursor, deadline)
            cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL lock_timeout='3000ms'")
            cursor.execute("SHOW transaction_read_only")
            require(cursor.fetchone()[0] == "on", "original snapshot not read-only")
            cursor.execute(IDENTITY_SQL)
            actual = dict(zip(IDENTITY_KEYS, cursor.fetchone()))
            require(actual == expected, "original PostgreSQL identity changed")
            budget = _read_gate(cursor, receipt)
            cursor.execute(capacity.PG_SAMPLE_SQL)
            summary = {**capacity._postgres_counts(cursor.fetchall()), "database_identity": actual, "read_only_transaction": True}
            cursor.execute(ROWS_SQL)
            entries = []
            for values in cursor:
                require(len(entries) < capacity.SENDER_ROWS and time.monotonic() < deadline, "original snapshot budget")
                row, raw = _row(values)
                write_blob(root / row["body_file"], raw, MAX_BODY_BYTES)
                entries.append(row)
            cursor.execute("ROLLBACK")
        value = {"schema": 1, "kind": "miniflux-durable-drain-v1", "sender_binding": binding,
                 "postgres_identity": actual, "control_receipt": receipt, "window": window, "source_sha256": source_sha256(),
                 "inventory_summary": summary, "retained_lease_budget": budget, "entries": entries,
                 "legacy_original_coverage": "registered_paths_since_instrumentation_only", "queue_checked": False, "switch_authorized": False}
        validate_manifest(value)
        require(time.monotonic() < deadline, "original snapshot wall budget")
        write_blob(root / "manifest.json", encoded(value), MAX_MANIFEST_BYTES)
        return value
    finally:
        connection.close()


def observe(connect, dsn, expected, *, deadline=None):
    """Fixed read-only SQL; returns counts plus actual gate, never a permit."""
    validate_identity(expected)
    deadline = _deadline(deadline)
    connection = _connect(connect, dsn, deadline)
    try:
        connection.autocommit = True
        with connection.cursor() as raw_cursor:
            cursor = _TimedCursor(raw_cursor, deadline)
            cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL lock_timeout='3000ms'")
            cursor.execute("SHOW transaction_read_only")
            require(cursor.fetchone()[0] == "on", "sender observation not read-only")
            cursor.execute(IDENTITY_SQL)
            actual = dict(zip(IDENTITY_KEYS, cursor.fetchone()))
            require(actual == expected, "sender PostgreSQL identity changed")
            cursor.execute(capacity.PG_SAMPLE_SQL)
            sample = capacity._postgres_counts(cursor.fetchall())
            cursor.execute("SELECT manifest,generation,phase,current_operation FROM handoff_state WHERE id=1")
            row = cursor.fetchone()
            require(row is not None and len(row) == 4, "actual sender gate missing")
            gate = dict(zip(("manifest", "generation", "phase", "current_operation"), row))
            cursor.execute("""SELECT count(*),count(*) FILTER(WHERE status='active'),count(*) FILTER(WHERE status='uncertain'),
 count(*) FILTER(WHERE status='active' AND source NOT IN ('outbox_pass','outbox_sender')),
 count(*) FILTER(WHERE status='uncertain' AND source NOT IN ('outbox_pass','outbox_sender')) FROM handoff_leases""")
            leases = dict(zip(("records", "active", "uncertain", "active_producers", "uncertain_producers"), cursor.fetchone()))
            require(all(type(v) is int and 0 <= v <= 10000 for v in leases.values()), "invalid retained lease state")
            cursor.execute("SELECT count(*) FROM handoff_operations")
            operations = cursor.fetchone()[0]
            require(type(operations) is int and 0 <= operations <= 10000, "operation capacity invalid")
            cursor.execute("ROLLBACK")
        require(time.monotonic() < deadline, "sender observation wall budget")
        return {**sample, "database_identity": actual, "read_only_transaction": True, "gate": gate,
                "retained_leases": leases, "retained_operations": operations}
    finally:
        connection.close()


class PostgresReader:
    """Explicit private DSN with fixed psycopg read-only queries; no zero callback."""
    def __init__(self, dsn_file, expected_identity):
        from scripts.durable_handoff_cutover import read_private
        self.path = Path(dsn_file)
        require(self.path.is_absolute(), "absolute private sender DSN reference required")
        self.expected = validate_identity(dict(expected_identity))
        self._read_private = read_private
        self._credential_sha256 = hashlib.sha256(encoded(self._credential())).hexdigest()

    def _credential(self):
        value = self._read_private(self.path)
        require(isinstance(value, dict) and set(value) == {"dsn"} and isinstance(value["dsn"], str)
                and 0 < len(value["dsn"]) <= 8192, "explicit sender DSN required")
        return value

    def _dsn(self):
        value = self._credential()
        require(hashlib.sha256(encoded(value)).hexdigest() == self._credential_sha256, "sender database credential changed")
        return value["dsn"]

    def snapshot(self, *, deadline=None):
        import psycopg
        return observe(psycopg.connect, self._dsn(), self.expected, deadline=deadline)

    def freeze(self, binding, receipt, window, root):
        import psycopg
        return freeze_bundle(psycopg.connect, self._dsn(), self.expected, binding, receipt, window, root)

    def locked(self, closed_operation_id, manifest):
        return FrozenPostgres(self, closed_operation_id, manifest)


class FrozenPostgres:
    """Live session locks, not a serializable authorization receipt."""
    def __init__(self, reader, operation, manifest):
        from services.sender_drain_contract import HEX64
        require(all(isinstance(v, str) and HEX64.fullmatch(v) for v in (operation, manifest)), "exact closed gate identity required")
        self.reader, self.operation, self.manifest = reader, operation, manifest
        self.connection = None
        self.snapshot = self.latest_snapshot = self.lock_identity = None

    def __enter__(self):
        import psycopg
        deadline = _deadline()
        self.connection = _connect(psycopg.connect, self.reader._dsn(), deadline)
        try:
            self.connection.autocommit = True
            with self.connection.cursor() as raw_cursor:
                cursor = _TimedCursor(raw_cursor, deadline)
                cursor.execute("SET lock_timeout='3000ms'")
                cursor.execute(IDENTITY_SQL)
                require(dict(zip(IDENTITY_KEYS, cursor.fetchone())) == self.reader.expected, "frozen PG identity differs")
                oid = self.reader.expected["state_table_oid"]
                for namespace in (20261024, 20261023):
                    cursor.execute("SELECT pg_try_advisory_lock(%s,%s)", (namespace, oid))
                    require(cursor.fetchone()[0] is True, "sender activity/control lock busy")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            self.lock_identity = {"backend_pid":pid,"state_table_oid":oid,"activity_namespace":20261024,"metadata_namespace":20261023,
                                  "database_identity":self.reader.expected}
            self.snapshot = self.reader.snapshot(deadline=deadline)
            self._closed(self.snapshot)
            self.check(deadline=deadline)
            return self
        except BaseException:
            self.connection.close()
            self.connection = None
            raise

    def _closed(self, value):
        require(value["gate"] == {"manifest":self.manifest,"phase":"closed","generation":value["gate"]["generation"],
                                  "current_operation":self.operation}, "exact sender full pause not current")
        leases = value["retained_leases"]
        require(leases["active"] == leases["uncertain"] == 0, "sender active/unknown lease retained")
        require(not any(value["states"][k] for k in ("pending","inflight","held"))
                and value["legacy"]["rows"] == 0 and value["expired_unacknowledged"] == 0,
                "sender retained originals are not independently drained")

    def _confirmed_operation(self, cursor):
        cursor.execute("""SELECT manifest,action,previous_generation,generation,status,confirmed_at IS NOT NULL
 FROM handoff_operations WHERE operation_id=%s""", (self.operation,))
        generation = self.snapshot["gate"]["generation"]
        require(cursor.fetchone() == (self.manifest, "pause", generation - 1, generation, "confirmed", True),
                "actual sender pause operation is missing, pending, or mismatched")

    def check(self, *, deadline=None):
        deadline = _deadline(deadline)
        require(self.connection is not None, "sender PG locks no longer held")
        with self.connection.cursor() as raw_cursor:
            cursor = _TimedCursor(raw_cursor, deadline)
            cursor.execute(IDENTITY_SQL)
            require(dict(zip(IDENTITY_KEYS, cursor.fetchone())) == self.reader.expected, "locked PG identity changed")
            self._confirmed_operation(cursor)
            cursor.execute("""SELECT pg_backend_pid(),count(*) FROM pg_locks WHERE pid=pg_backend_pid()
 AND locktype='advisory' AND classid IN (20261024::oid,20261023::oid) AND objid=%s::oid
 AND objsubid=2 AND mode='ExclusiveLock' AND granted""", (self.reader.expected["state_table_oid"],))
            require(cursor.fetchone() == (self.lock_identity["backend_pid"],2), "sender PG lock ownership lost")
        current = self.reader.snapshot(deadline=deadline)
        self._closed(current)
        require(stable_inventory(current) == stable_inventory(self.snapshot), "frozen sender inventory changed under locks")
        self.latest_snapshot = current
        return self.lock_identity

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.check()
        finally:
            self.connection.close()
            self.connection = None
