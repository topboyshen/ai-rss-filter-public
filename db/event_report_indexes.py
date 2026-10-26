"""Optional additive indexes for bounded event-report history reads.

These are performance aids, not part of the event-core required-object set.
An older release may therefore continue validating and reading the database.
"""
from __future__ import annotations

import re
import sqlite3

from db.event_core import EventCoreSchemaError

HISTORY_INDEX_NAME = "idx_event_evidence_unbound_event_observed"
_HISTORY_INDEX_SQL = (
    f"CREATE INDEX {HISTORY_INDEX_NAME} "
    "ON event_evidence(event_id, observed_at, evidence_id) WHERE mapping_id IS NULL"
)


def _normalized_sql(value: object) -> str:
    sql = str(value or "").strip().rstrip(";").lower()
    sql = re.sub(r"\bif\s+not\s+exists\b", "", sql)
    return re.sub(r"\s+", "", sql)


def _existing_index_is_valid(conn: sqlite3.Connection) -> bool:
    objects = conn.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master WHERE name = ? COLLATE NOCASE",
        (HISTORY_INDEX_NAME,),
    ).fetchall()
    if not objects:
        return False
    if len(objects) != 1 or (
        str(objects[0][0]) != "index"
        or str(objects[0][1]) != "event_evidence"
        or _normalized_sql(objects[0][2]) != _normalized_sql(_HISTORY_INDEX_SQL)
    ):
        raise EventCoreSchemaError(
            f"incompatible optional event-report index: {HISTORY_INDEX_NAME}"
        )
    return True


def init_event_report_indexes(conn: sqlite3.Connection) -> None:
    """Add the history lookup index, rejecting a conflicting existing object.

    Run only after the caller's strict event-core startup preflight and schema
    initialization. No rows, required schema objects or migration markers change.
    Verification precedes DDL and repeats afterwards, so a concurrent creator
    cannot make ``IF NOT EXISTS`` silently accept a wrong definition.
    """
    if _existing_index_is_valid(conn):
        return
    conn.execute(_HISTORY_INDEX_SQL.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
    if not _existing_index_is_valid(conn):
        raise EventCoreSchemaError(f"event-report index was not installed: {HISTORY_INDEX_NAME}")
