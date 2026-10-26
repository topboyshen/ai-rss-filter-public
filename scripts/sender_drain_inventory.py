"""Actual read-only PostgreSQL producer-stop inventory; never recreate old bodies."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time

from durable_handoff_cutover import encoded, private_directory, require, sync_directory
from miniflux_handoff_adapter import checked_binding
import observe_handoff_capacity as capacity
from services.sender_drain_contract import (IDENTITY_KEYS, MAX_BODY_BYTES, MAX_MANIFEST_BYTES,
    source_sha256, validate_identity, validate_stop_receipt, validate_entry, validate_manifest)

IDENTITY_SQL = capacity.PG_IDENTITY_SQL + """, 'handoff_state'::regclass::oid::bigint,
 'handoff_operations'::regclass::oid::bigint, 'handoff_leases'::regclass::oid::bigint,
 (SELECT version FROM schema_version), current_user, session_user"""
ROWS_SQL = """SELECT delivery_id,body_sha256,body,target_url,signature,contract,user_id,feed_id,entry_id,state,attempts
 FROM webhook_outbox WHERE state IN ('pending','inflight') ORDER BY delivery_id LIMIT 5001"""


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
    checked_binding(binding, scope="instrumented_sender_only")
    validate_stop_receipt(receipt, binding)
    root = Path(root)
    require(not root.exists() and not root.is_symlink(), "bundle already attempted; preserve original")
    private_directory(root, create=True)
    started = time.monotonic()
    connection = connect(dsn, connect_timeout=3)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL statement_timeout='3000ms'")
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
                require(len(entries) < capacity.SENDER_ROWS and time.monotonic() - started <= 10, "original snapshot budget")
                row, raw = _row(values)
                write_blob(root / row["body_file"], raw, MAX_BODY_BYTES)
                entries.append(row)
            cursor.execute("ROLLBACK")
        value = {"schema": 1, "kind": "miniflux-durable-drain-v1", "sender_binding": binding,
                 "postgres_identity": actual, "control_receipt": receipt, "window": window, "source_sha256": source_sha256(),
                 "inventory_summary": summary, "retained_lease_budget": budget, "entries": entries,
                 "legacy_original_coverage": "registered_paths_since_instrumentation_only", "queue_checked": False, "switch_authorized": False}
        validate_manifest(value)
        require(time.monotonic() - started <= 10, "original snapshot wall budget")
        write_blob(root / "manifest.json", encoded(value), MAX_MANIFEST_BYTES)
        return value
    finally:
        connection.close()
