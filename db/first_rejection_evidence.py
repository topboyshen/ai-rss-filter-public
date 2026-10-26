"""Append-only storage for replayable first-rejection evidence.

One row binds the exact input a validator saw for the first (rejected) attempt of
a scoring stage, that attempt's raw output and parsed tuple, the rejection
reason, what finally happened to the article, and the code that judged it.

* Not part of the scoring ledger: nothing here is referenced by ``signal_key``,
  ``revision_state_key`` or any ``news_cache`` column.
* UPDATE is forbidden.  DELETE is allowed only for rows older than the fixed
  seven-day floor, which is the expiry path used by ``cleanup_old_entries``.
* The table stores article text and model output by design (that is what makes
  a row replayable).  It never stores the article link, request messages, keys
  or configuration.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

TABLE = "first_rejection_evidence"
SCHEMA_VERSION = 1
RETENTION_FLOOR_SECONDS = 7 * 86400
# Approved hard maxima: configuration can only lower them.
MAX_ROWS_HARD = 20000
MAX_TOTAL_BYTES_HARD = 64 * 1024 * 1024


def safe_bounded_int(value: Any, default: int, low: int, high: int) -> int:
    """``value`` if it is a finite integer in [low, high]; otherwise ``default``.

    Booleans, strings, NaN/inf, fractions and out-of-range numbers are all invalid
    (``int(float('inf'))`` would raise OverflowError, so nothing is coerced).
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return default
        value = int(value)
    elif not isinstance(value, int):
        return default
    return value if low <= value <= high else default

COLUMNS = (
    "schema_version", "created_at", "dedupe_key", "article_id", "event_id",
    "signal_key", "strategy_version", "run_revision", "validator_version",
    "validator_files", "stage", "input_title", "input_source",
    "input_source_chars", "input_source_bytes", "input_hash", "request_hash",
    "rejected_backend", "rejected_model", "rejected_attempt", "rejected_raw",
    "rejected_parsed", "failure_reason", "failure_detail", "fact_failure_kind",
    "repair_trigger", "later_attempts", "final_handling",
    "accepted_is_fixed_neutral", "replayable", "replay_limit", "row_bytes",
)


def init_first_rejection_storage(cursor) -> None:
    """Additive schema: empty table and triggers; the capture switch stays off."""
    cursor.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_version INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        dedupe_key TEXT NOT NULL UNIQUE,
        article_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        signal_key TEXT NOT NULL DEFAULT '',
        strategy_version TEXT NOT NULL,
        run_revision TEXT NOT NULL,
        validator_version TEXT NOT NULL,
        validator_files TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('score','stage1','stage2')),
        input_title TEXT NOT NULL,
        input_source TEXT NOT NULL,
        input_source_chars INTEGER NOT NULL,
        input_source_bytes INTEGER NOT NULL,
        input_hash TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        rejected_backend TEXT NOT NULL,
        rejected_model TEXT NOT NULL,
        rejected_attempt TEXT NOT NULL,
        rejected_raw TEXT NOT NULL,
        rejected_parsed TEXT NOT NULL,
        failure_reason TEXT NOT NULL,
        failure_detail TEXT NOT NULL DEFAULT '',
        fact_failure_kind TEXT NOT NULL DEFAULT '',
        repair_trigger TEXT NOT NULL DEFAULT '',
        later_attempts TEXT NOT NULL,
        final_handling TEXT NOT NULL,
        accepted_is_fixed_neutral INTEGER NOT NULL
            CHECK(accepted_is_fixed_neutral IN (0,1)),
        replayable INTEGER NOT NULL CHECK(replayable IN (0,1)),
        replay_limit TEXT NOT NULL DEFAULT '',
        row_bytes INTEGER NOT NULL
    )""")
    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_created ON {TABLE}(created_at)"
    )
    # A write that collides with an existing row (same dedupe_key or same id) is
    # dropped before SQLite resolves the conflict.  Because this fires first, it
    # also stops INSERT OR REPLACE / REPLACE INTO from deleting the old row, on any
    # connection and with recursive_triggers either on or off, whatever the row's
    # age.  RAISE(IGNORE) is exactly the idempotent OR IGNORE outcome.
    cursor.execute(
        f"CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_no_conflict_write "
        f"BEFORE INSERT ON {TABLE} "
        f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE dedupe_key = NEW.dedupe_key "
        f"OR id = NEW.id) "
        f"BEGIN SELECT RAISE(IGNORE); END"
    )
    cursor.execute(
        f"CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_no_update "
        f"BEFORE UPDATE ON {TABLE} "
        f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
    )
    # Expiry is the only legal DELETE.  The floor is a constant so that no
    # configuration can shorten retention below seven days.  (REPLACE is stopped by
    # the insert trigger above, not by this one: with recursive_triggers off SQLite
    # does not fire DELETE triggers for REPLACE's internal delete.)
    cursor.execute(
        f"CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_expiry_only "
        f"BEFORE DELETE ON {TABLE} "
        f"WHEN OLD.created_at > CAST(strftime('%s','now') AS INTEGER) "
        f"- {RETENTION_FLOOR_SECONDS} "
        f"BEGIN SELECT RAISE(ABORT, '{TABLE} rows are kept for seven days'); END"
    )


def insert_evidence(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    max_rows: int,
    max_total_bytes: int,
) -> bool:
    """Atomically insert one row unless a volume cap or the dedupe key says no.

    Returns True only when a new row was written.  The caps are evaluated in the
    same statement as the insert, so concurrent writers cannot overshoot.
    """
    values = tuple(row[name] for name in COLUMNS)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO {TABLE} ({', '.join(COLUMNS)}) "
        f"SELECT {', '.join('?' for _ in COLUMNS)} "
        f"WHERE (SELECT COUNT(*) FROM {TABLE}) < ? "
        f"AND (SELECT COALESCE(SUM(row_bytes), 0) FROM {TABLE}) + ? <= ?",
        values + (
            min(int(max_rows), MAX_ROWS_HARD),
            int(row["row_bytes"]),
            min(int(max_total_bytes), MAX_TOTAL_BYTES_HARD),
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def purge_expired(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete rows older than ``retention_days`` (7-30, otherwise 7)."""
    days = safe_bounded_int(retention_days, RETENTION_FLOOR_SECONDS // 86400, 7, 30)
    cutoff = int(time.time()) - days * 86400
    try:
        before = conn.total_changes
        conn.execute(f"DELETE FROM {TABLE} WHERE created_at < ?", (cutoff,))
        return conn.total_changes - before
    except sqlite3.Error:
        # Diagnostics expiry must never abort the rest of the cache cleanup.
        logger.warning("[FirstRejection] expiry skipped", exc_info=False)
        return 0
