"""Explicit, restart-safe migration from legacy article/event rows to UUID events.

This module performs no network I/O and is intentionally not run during normal
API/worker startup.  Operators pause writers, take a SQLite backup, and execute
the companion maintenance script.  One ``BEGIN IMMEDIATE`` transaction covers
schema additions, data backfill, validation and the completion marker.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
import uuid
from datetime import datetime, time as datetime_time
from typing import Any
from zoneinfo import ZoneInfo

from db.event_core import (
    EVENT_CORE_BACKFILL_META_KEY,
    EVENT_CORE_SCHEMA_META_KEY,
    EventCoreConflictError,
    canonical_event_id,
    count_unresolved_event_core_outbox_references,
    init_event_core_schema,
    resolve_or_create_event,
    upsert_event_security,
    verify_event_core_backfill_state,
)
from db.integration_outbox import MAPPING_DIMENSIONS_BACKFILL_META


BACKFILL_META_KEY = EVENT_CORE_BACKFILL_META_KEY
SCHEMA_META_KEY = EVENT_CORE_SCHEMA_META_KEY
SCORE_DIMENSIONS_META_KEY = "event_score_dimensions_v1"
MAPPING_DIMENSION_REPAIR_VERSION = "mapping-dimensions-repair:v1"
MAPPING_DIMENSION_REPAIR_REASON = "legacy_score_confidence_conflation"
# Exact producer version observed in the deployed v2 release.  Repair must be
# provenance-gated: a nonzero value alone is never evidence of conflation.
LEGACY_CONFLATED_MAPPING_VERSIONS = frozenset({"asset-map:e15fc380db90"})
_PRICE_OUTCOME_CONTRACTS = {
    "a-share-event-day-close-v2": "future-prices-v2:",
    "a-share-first-post-close-v3": "future-prices-first-post-close-v3:",
    "a-share-event-ohlc-v4": "future-prices-event-ohlc-v4:",
    "a-share-event-ohlc-v5-frozen-mapping": (
        "future-prices-event-ohlc-v5-frozen-mapping:"
    ),
}
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = datetime_time(15, 0)

_SOURCE_INVARIANT_EXCLUSIONS = {
    # These are the only legacy-table fields the stable-core migration is
    # allowed to fill.  Every pre-existing business field is hashed below so
    # an equal-count UPDATE cannot pass unnoticed.
    "news_cache": {"event_id", "event_version"},
    "backtest_signals": {
        "event_id",
        "event_version",
        "event_mapping_id",
        # Additive, frozen mapping snapshots populated by this maintenance
        # migration.  They are validated against the referenced immutable
        # mapping revision before commit, so excluding them here does not make
        # their values unaudited.
        "mapping_confidence",
        "mapping_relevance",
        "expected_gap",
    },
    "future_prices": {"revision_id"},
    "integration_signal_outbox": {
        "domain_event_id",
        "event_version",
        "event_mapping_id",
        "mapping_confidence",
        "mapping_relevance",
        "expected_gap",
    },
}
_OUTBOX_INVARIANT_META_KEYS = (
    "integration_outbox_ledger_id",
    "integration_outbox_epoch",
    "integration_outbox_floor_seq",
    "integration_outbox_last_seq",
    MAPPING_DIMENSIONS_BACKFILL_META,
)
_OUTBOX_MAPPING_DIMENSION_COLUMNS = {
    "ledger_signal_id",
    "mapping_confidence",
    "mapping_relevance",
    "expected_gap",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _outbox_dimensions_current(conn: sqlite3.Connection) -> bool:
    """Return whether the retained Outbox has the complete v4 snapshot schema."""
    if not (
        _table_exists(conn, "app_meta")
        and _table_exists(conn, "integration_signal_outbox")
    ):
        return False
    marker = conn.execute(
        "SELECT v FROM app_meta WHERE k = ?",
        (MAPPING_DIMENSIONS_BACKFILL_META,),
    ).fetchone()
    return (
        marker is not None
        and str(marker[0]) == "1"
        and _OUTBOX_MAPPING_DIMENSION_COLUMNS.issubset(
            _columns(conn, "integration_signal_outbox")
        )
    )


def _digest_value(value: object) -> bytes:
    """Encode one SQLite scalar without delimiter or type ambiguity."""
    if value is None:
        payload = b""
        tag = b"N"
    elif isinstance(value, bytes):
        payload = value
        tag = b"B"
    elif isinstance(value, int):
        payload = str(value).encode("ascii")
        tag = b"I"
    elif isinstance(value, float):
        payload = value.hex().encode("ascii")
        tag = b"F"
    else:
        payload = str(value).encode("utf-8", errors="surrogatepass")
        tag = b"T"
    return tag + len(payload).to_bytes(8, "big") + payload


def _business_digest(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> str:
    """Return an order-independent digest of all legacy business values."""
    if not columns:
        return hashlib.sha256(b"no-business-columns").hexdigest()
    quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    row_digests: list[bytes] = []
    for row in conn.execute(f'SELECT {quoted} FROM "{table}"'):
        digest = hashlib.sha256()
        for value in row:
            digest.update(_digest_value(value))
        row_digests.append(digest.digest())
    combined = hashlib.sha256()
    for name in columns:
        combined.update(_digest_value(name))
    for digest in sorted(row_digests):
        combined.update(digest)
    return combined.hexdigest()


def _snapshot_source_invariants(conn: sqlite3.Connection) -> dict[str, Any]:
    """Freeze legacy data and consumer-cursor invariants inside the write lock."""
    tables: dict[str, dict[str, Any]] = {}
    for table, excluded in _SOURCE_INVARIANT_EXCLUSIONS.items():
        exists = _table_exists(conn, table)
        if not exists:
            tables[table] = {
                "existed": False,
                "row_count": 0,
                "business_columns": (),
                "business_digest": None,
            }
            continue
        columns = tuple(
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")')
            if str(row[1]) not in excluded
        )
        tables[table] = {
            "existed": True,
            "row_count": int(
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            ),
            "business_columns": columns,
            "business_digest": _business_digest(conn, table, columns),
        }

    if _table_exists(conn, "integration_signal_outbox"):
        bounds = conn.execute(
            "SELECT COUNT(*), MIN(seq), MAX(seq) "
            "FROM integration_signal_outbox"
        ).fetchone()
        outbox_bounds = (
            int(bounds[0]),
            int(bounds[1]) if bounds[1] is not None else None,
            int(bounds[2]) if bounds[2] is not None else None,
        )
    else:
        outbox_bounds = (0, None, None)

    outbox_meta: dict[str, tuple[bool, str | None]] = {}
    for key in _OUTBOX_INVARIANT_META_KEYS:
        row = (
            conn.execute("SELECT v FROM app_meta WHERE k = ?", (key,)).fetchone()
            if _table_exists(conn, "app_meta")
            else None
        )
        outbox_meta[key] = (row is not None, str(row[0]) if row is not None else None)
    return {
        "tables": tables,
        "outbox_bounds": outbox_bounds,
        "outbox_meta": outbox_meta,
    }


def _assert_source_invariants(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
) -> None:
    """Reject any migration that alters legacy rows or a retained cursor."""
    for table, expected in snapshot["tables"].items():
        actual_count = (
            int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if _table_exists(conn, table)
            else 0
        )
        if actual_count != int(expected["row_count"]):
            raise RuntimeError(
                f"event-core migration changed {table} row count: "
                f"{expected['row_count']} -> {actual_count}"
            )
        if not expected["existed"]:
            # Creating an empty compatibility table is a supported migration.
            continue
        columns = tuple(expected["business_columns"])
        current_columns = _columns(conn, table)
        missing = set(columns) - current_columns
        if missing:
            raise RuntimeError(
                f"event-core migration removed {table} business column(s): "
                + ", ".join(sorted(missing))
            )
        actual_digest = _business_digest(conn, table, columns)
        if actual_digest != expected["business_digest"]:
            raise RuntimeError(
                f"event-core migration changed {table} legacy business data"
            )

    if _table_exists(conn, "integration_signal_outbox"):
        bounds = conn.execute(
            "SELECT COUNT(*), MIN(seq), MAX(seq) "
            "FROM integration_signal_outbox"
        ).fetchone()
        actual_bounds = (
            int(bounds[0]),
            int(bounds[1]) if bounds[1] is not None else None,
            int(bounds[2]) if bounds[2] is not None else None,
        )
    else:
        actual_bounds = (0, None, None)
    if actual_bounds != tuple(snapshot["outbox_bounds"]):
        raise RuntimeError(
            "event-core migration changed integration outbox COUNT/MIN/MAX: "
            f"{snapshot['outbox_bounds']} -> {actual_bounds}"
        )

    # Missing metadata belongs to databases that predate the durable Outbox
    # and may be initialized.  Every value that already existed is immutable:
    # silently normalizing a damaged floor/last pair would invalidate a Quant
    # consumer cursor and must roll the whole migration back.
    for key, (existed, expected_value) in snapshot["outbox_meta"].items():
        if not existed:
            continue
        row = conn.execute("SELECT v FROM app_meta WHERE k = ?", (key,)).fetchone()
        actual_value = str(row[0]) if row is not None else None
        if actual_value != expected_value:
            raise RuntimeError(
                f"event-core migration changed integration outbox metadata {key}: "
                f"{expected_value!r} -> {actual_value!r}"
            )


def _reject_unmigrated_legacy_radar_memberships(
    conn: sqlite3.Connection,
) -> None:
    """Fail closed while legacy Radar membership edges remain unhandled.

    ``event_cache`` records only a cluster's canonical article, while the
    retired ``event_articles`` table records every source article belonging to
    that cluster.  Mapping the former without the latter would make a
    multi-source cluster appear successfully migrated while silently dropping
    its membership history from the stable event core.
    """
    if not _table_exists(conn, "event_articles"):
        return
    membership_rows = int(
        conn.execute("SELECT COUNT(*) FROM event_articles").fetchone()[0]
    )
    if membership_rows:
        raise RuntimeError(
            "legacy Radar membership migration is unsupported: event_articles "
            f"contains {membership_rows} row(s); refusing to drop multi-source "
            "membership history"
        )


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _ensure_compatibility_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
    )
    if not _table_exists(conn, "news_cache"):
        raise RuntimeError("news_cache is missing; initialize the application database first")
    if not _table_exists(conn, "backtest_signals"):
        raise RuntimeError(
            "backtest_signals is missing; initialize the application database first"
        )
    _add_column(conn, "news_cache", "event_id", "TEXT")
    _add_column(conn, "news_cache", "event_version", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "backtest_signals", "event_id", "TEXT")
    _add_column(
        conn,
        "backtest_signals",
        "event_version",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(conn, "backtest_signals", "event_mapping_id", "TEXT")
    _add_column(
        conn,
        "backtest_signals",
        "mapping_confidence",
        "REAL NOT NULL DEFAULT 0 CHECK (typeof(mapping_confidence) IN ('integer', 'real') AND mapping_confidence >= 0 AND mapping_confidence <= 1)",
    )
    _add_column(
        conn,
        "backtest_signals",
        "mapping_relevance",
        "REAL NOT NULL DEFAULT 0 CHECK (typeof(mapping_relevance) IN ('integer', 'real') AND mapping_relevance >= 0 AND mapping_relevance <= 10)",
    )
    _add_column(
        conn,
        "backtest_signals",
        "expected_gap",
        "REAL CHECK (expected_gap IS NULL OR (typeof(expected_gap) IN ('integer', 'real') AND expected_gap >= -100 AND expected_gap <= 100))",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cache_event "
        "ON news_cache(event_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_event_available "
        "ON backtest_signals(event_id, available_at DESC)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS legacy_event_id_map ("
        "legacy_event_id TEXT PRIMARY KEY, "
        "event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT, "
        "migrated_at INTEGER NOT NULL, "
        "note TEXT NOT NULL DEFAULT ''"
        ") WITHOUT ROWID"
    )


def _init_mapping_dimension_repair_schema(conn: sqlite3.Connection) -> None:
    """Create the append-only audit ledger for legacy dimension repairs."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS event_mapping_dimension_repairs ("
        "legacy_mapping_id TEXT PRIMARY KEY "
        "REFERENCES event_securities(mapping_id) ON DELETE RESTRICT, "
        "replacement_mapping_id TEXT "
        "REFERENCES event_securities(mapping_id) ON DELETE RESTRICT, "
        "repaired_at INTEGER NOT NULL CHECK (repaired_at >= 0), "
        "reason TEXT NOT NULL CHECK (reason = 'legacy_score_confidence_conflation'), "
        "CHECK (replacement_mapping_id IS NULL "
        "OR replacement_mapping_id <> legacy_mapping_id)"
        ") WITHOUT ROWID"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mapping_dimension_repairs_replacement "
        "ON event_mapping_dimension_repairs(replacement_mapping_id)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS event_mapping_dimension_repairs_no_update "
        "BEFORE UPDATE ON event_mapping_dimension_repairs "
        "BEGIN SELECT RAISE(ABORT, 'mapping dimension repair audit is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS event_mapping_dimension_repairs_no_delete "
        "BEFORE DELETE ON event_mapping_dimension_repairs "
        "BEGIN SELECT RAISE(ABORT, 'mapping dimension repair audit is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS event_mapping_dimension_repairs_sealed "
        "BEFORE INSERT ON event_mapping_dimension_repairs WHEN EXISTS ("
        "SELECT 1 FROM app_meta WHERE k = 'event_score_dimensions_v1') "
        "BEGIN SELECT RAISE(ABORT, 'mapping dimension repair audit is sealed'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS event_score_dimensions_marker_no_update "
        "BEFORE UPDATE ON app_meta WHEN OLD.k = 'event_score_dimensions_v1' "
        "BEGIN SELECT RAISE(ABORT, 'score dimensions marker is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS event_score_dimensions_marker_no_delete "
        "BEFORE DELETE ON app_meta WHEN OLD.k = 'event_score_dimensions_v1' "
        "BEGIN SELECT RAISE(ABORT, 'score dimensions marker is immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS event_score_dimensions_marker_no_replace "
        "BEFORE INSERT ON app_meta WHEN NEW.k = 'event_score_dimensions_v1' "
        "AND EXISTS (SELECT 1 FROM app_meta WHERE k = NEW.k) "
        "BEGIN SELECT RAISE(ABORT, 'score dimensions marker is immutable'); END"
    )


def _init_compatibility_reference_triggers(conn: sqlite3.Connection) -> None:
    """Install referential guards for columns added with SQLite ``ALTER``."""
    statements = (
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_insert "
        "BEFORE INSERT ON news_cache WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown news_cache event_id'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_update "
        "BEFORE UPDATE OF event_id ON news_cache WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown news_cache event_id'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_insert "
        "BEFORE INSERT ON backtest_signals WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown backtest event_id'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_update "
        "BEFORE UPDATE OF event_id ON backtest_signals WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown backtest event_id'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_version_insert "
        "BEFORE INSERT ON news_cache WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NEW.event_version > (SELECT version FROM events WHERE event_id = NEW.event_id)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid news_cache event version'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_version_update "
        "BEFORE UPDATE OF event_id, event_version ON news_cache "
        "WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NEW.event_version > (SELECT version FROM events WHERE event_id = NEW.event_id)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid news_cache event version'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_version_insert "
        "BEFORE INSERT ON backtest_signals WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NEW.event_version > (SELECT version FROM events WHERE event_id = NEW.event_id)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event version'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_version_update "
        "BEFORE UPDATE OF event_id, event_version ON backtest_signals "
        "WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NEW.event_version > (SELECT version FROM events WHERE event_id = NEW.event_id)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event version'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_mapping_insert "
        "BEFORE INSERT ON backtest_signals "
        "WHEN COALESCE(TRIM(NEW.event_mapping_id), '') <> '' "
        "AND NOT EXISTS (SELECT 1 FROM event_securities AS mapping "
        "WHERE mapping.mapping_id = NEW.event_mapping_id "
        "AND mapping.event_id = NEW.event_id "
        "AND lower(mapping.security_id) = lower(COALESCE(NEW.backtest_symbol, ''))) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event mapping'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_mapping_update "
        "BEFORE UPDATE OF event_id, event_mapping_id, backtest_symbol ON backtest_signals "
        "WHEN COALESCE(TRIM(NEW.event_mapping_id), '') <> '' "
        "AND NOT EXISTS (SELECT 1 FROM event_securities AS mapping "
        "WHERE mapping.mapping_id = NEW.event_mapping_id "
        "AND mapping.event_id = NEW.event_id "
        "AND lower(mapping.security_id) = lower(COALESCE(NEW.backtest_symbol, ''))) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event mapping'); END",
    )
    for statement in statements:
        conn.execute(statement)


def _current_event(conn: sqlite3.Connection, raw_event_id: Any) -> tuple[str, int] | None:
    value = str(raw_event_id or "").strip()
    if not value:
        return None
    try:
        root = canonical_event_id(conn, value)
    except (KeyError, ValueError):
        return None
    row = conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (root,)
    ).fetchone()
    return (root, int(row[0])) if row else None


def _link_observed_at(conn: sqlite3.Connection, link: str, fallback: int) -> int:
    row = conn.execute(
        "SELECT MIN(recorded_at) FROM backtest_signals "
        "WHERE link = ? AND recorded_at > 0",
        (link,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else fallback


def _resolve_news_row(conn: sqlite3.Connection, row: sqlite3.Row, migrated_at: int) -> tuple[str, int]:
    # Import locally to avoid a database.py -> migration -> database.py cycle.
    from db.database import _record_article_evidence, _resolve_article_event

    raw_event_id = str(
        (row["event_id"] if "event_id" in row.keys() else "") or ""
    ).strip()
    existing = _current_event(conn, raw_event_id)
    if raw_event_id and existing is None:
        raise RuntimeError(f"news_cache references unknown event_id: {raw_event_id}")
    link = str(row["link"] or "").strip()
    observed_at = _link_observed_at(conn, link, migrated_at)
    if existing is None:
        event_id, event_version = _resolve_article_event(
            conn,
            link=link,
            title=row["raw_title"] or row["title"] or link,
            event_type=row["tag"] or "news",
            direction=row["direction"] or "中性",
            priority=float(row["score"] or 0.0),
            occurred_at=int(row["created_at"] or observed_at),
            strategy_version=row["strategy_version"] or "",
            model_version=row["model_name"] or "",
            observed_at=observed_at,
        )
    else:
        event_id, event_version = _resolve_article_event(
            conn,
            link=link,
            title=row["raw_title"] or row["title"] or link,
            event_type=row["tag"] or "news",
            direction=row["direction"] or "中性",
            priority=float(row["score"] or 0.0),
            occurred_at=int(row["created_at"] or observed_at),
            strategy_version=row["strategy_version"] or "",
            model_version=row["model_name"] or "",
            supplied_event_id=existing[0],
            observed_at=observed_at,
            update_metadata=False,
        )
    _record_article_evidence(
        conn,
        event_id=event_id,
        link=link,
        raw_title=row["raw_title"] or row["title"] or link,
        summary=row["summary"] or "",
        feed_url=row["feed_url"] or "",
        source_tier=row["source_tier"] or "",
        announcement_at=int(row["created_at"] or 0) or None,
        observed_at=observed_at,
    )
    return event_id, event_version


def _fill_event_reference(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    key_value: object,
    target_event_id: str,
    target_event_version: int,
) -> int:
    """Fill a missing reference without rewriting a frozen populated snapshot."""
    if (table, key_column) not in {
        ("news_cache", "link"),
        ("backtest_signals", "id"),
    }:
        raise ValueError("unsupported event-reference target")
    row = conn.execute(
        f"SELECT event_id, event_version FROM {table} WHERE {key_column} = ?",
        (key_value,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"{table} row disappeared during migration")
    stored_id = str(row[0] or "").strip()
    if not stored_id:
        cursor = conn.execute(
            f"UPDATE {table} SET event_id = ?, event_version = ? "
            f"WHERE {key_column} = ? AND COALESCE(event_id, '') = ''",
            (target_event_id, target_event_version, key_value),
        )
        return max(0, int(cursor.rowcount))

    stored = conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (stored_id,)
    ).fetchone()
    if stored is None:
        raise RuntimeError(f"{table} references unknown event_id: {stored_id}")
    if canonical_event_id(conn, stored_id) != canonical_event_id(conn, target_event_id):
        raise EventCoreConflictError(
            f"{table} frozen event_id conflicts with article identity"
        )
    raw_version = row[1]
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise RuntimeError(f"{table} contains a non-integer event_version")
    if raw_version < 1:
        cursor = conn.execute(
            f"UPDATE {table} SET event_version = ? WHERE {key_column} = ? "
            "AND event_version < 1",
            (int(stored[0]), key_value),
        )
        return max(0, int(cursor.rowcount))
    if raw_version > int(stored[0]):
        raise RuntimeError(f"{table} event_version exceeds its event version")
    return 0


def _backfill_news(conn: sqlite3.Connection, migrated_at: int) -> int:
    columns = _columns(conn, "news_cache")
    optional = {
        "raw_title": "NULL",
        "score": "0",
        "tag": "''",
        "created_at": "0",
        "summary": "''",
        "feed_url": "''",
        "source_tier": "''",
        "direction": "'中性'",
        "strategy_version": "''",
        "model_name": "''",
        "event_id": "NULL",
    }
    select_fields = ["link", "title"] + [
        name if name in columns else f"{fallback} AS {name}"
        for name, fallback in optional.items()
    ]
    rows = conn.execute(
        "SELECT " + ", ".join(select_fields) + " FROM news_cache ORDER BY link"
    ).fetchall()
    updated = 0
    for row in rows:
        event_id, event_version = _resolve_news_row(conn, row, migrated_at)
        updated += _fill_event_reference(
            conn,
            table="news_cache",
            key_column="link",
            key_value=row["link"],
            target_event_id=event_id,
            target_event_version=event_version,
        )
        ledger_rows = conn.execute(
            "SELECT id FROM backtest_signals WHERE link = ? ORDER BY id",
            (row["link"],),
        ).fetchall()
        for ledger_row in ledger_rows:
            _fill_event_reference(
                conn,
                table="backtest_signals",
                key_column="id",
                key_value=int(ledger_row[0]),
                target_event_id=event_id,
                target_event_version=event_version,
            )
    return updated


def _backfill_ledger_only(conn: sqlite3.Connection, migrated_at: int) -> int:
    from db.database import _resolve_article_event

    rows = conn.execute(
        "SELECT b.link, MIN(b.available_at) AS available_at, "
        "MIN(b.published_at) AS published_at, MAX(b.score) AS score, "
        "MAX(b.direction) AS direction, MAX(b.strategy_version) AS strategy_version, "
        "MAX(b.mapped_theme) AS mapped_theme, MAX(b.event_key) AS event_key, "
        "COUNT(*) AS row_count "
        "FROM backtest_signals AS b "
        "LEFT JOIN news_cache AS n ON n.link = b.link "
        "WHERE n.link IS NULL GROUP BY b.link ORDER BY b.link"
    ).fetchall()
    updated = 0
    for row in rows:
        ledger_rows = conn.execute(
            "SELECT id, event_id FROM backtest_signals WHERE link = ? ORDER BY id",
            (row["link"],),
        ).fetchall()
        canonical_ids: set[str] = set()
        for ledger_row in ledger_rows:
            stored_id = str(ledger_row[1] or "").strip()
            if not stored_id:
                continue
            existing = _current_event(conn, stored_id)
            if existing is None:
                raise RuntimeError(
                    f"backtest_signals references unknown event_id: {stored_id}"
                )
            canonical_ids.add(existing[0])
        if len(canonical_ids) > 1:
            raise EventCoreConflictError(
                "ledger rows for one article reference different canonical events"
            )
        if not canonical_ids:
            event_id, event_version = _resolve_article_event(
                conn,
                link=row["link"],
                title=row["mapped_theme"] or row["event_key"] or row["link"],
                event_type="legacy_signal",
                direction=row["direction"] or "中性",
                priority=float(row["score"] or 0.0),
                occurred_at=int(row["published_at"] or row["available_at"] or migrated_at),
                strategy_version=row["strategy_version"] or "",
                observed_at=int(row["available_at"] or migrated_at),
                update_metadata=False,
            )
        else:
            event_id = canonical_ids.pop()
            version_row = conn.execute(
                "SELECT version FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if version_row is None:
                raise RuntimeError("canonical ledger event disappeared during migration")
            event_version = int(version_row[0])
        for ledger_row in ledger_rows:
            updated += _fill_event_reference(
                conn,
                table="backtest_signals",
                key_column="id",
                key_value=int(ledger_row[0]),
                target_event_id=event_id,
                target_event_version=event_version,
            )
    return updated


def _backfill_signal_mappings(conn: sqlite3.Connection, migrated_at: int) -> int:
    from db.database import _upsert_event_proxy_mapping

    rows = conn.execute(
        "SELECT id, event_id, backtest_symbol, direction, mapped_theme, "
        "mapping_version, mapping_confidence, mapping_relevance, expected_gap "
        "FROM backtest_signals "
        "WHERE COALESCE(event_id, '') <> '' AND COALESCE(backtest_symbol, '') <> '' "
        "AND COALESCE(event_mapping_id, '') = '' "
        "ORDER BY id"
    ).fetchall()
    updated = 0
    for row in rows:
        mapping_id = _upsert_event_proxy_mapping(
            conn,
            event_id=str(row["event_id"]),
            symbol=row["backtest_symbol"],
            direction=row["direction"] or "中性",
            expected_gap=row["expected_gap"],
            # Truly legacy rows receive additive defaults 0/0/NULL.  If a
            # partially upgraded producer already persisted explicit mapping
            # dimensions, preserve those values instead of discarding them.
            confidence=float(row["mapping_confidence"] or 0.0),
            relevance=float(row["mapping_relevance"] or 0.0),
            mapped_theme=row["mapped_theme"] or "",
            mapping_version=row["mapping_version"] or "",
            now=migrated_at,
        )
        if not mapping_id:
            continue
        cursor = conn.execute(
            "UPDATE backtest_signals SET event_mapping_id = ? WHERE id = ? "
            "AND COALESCE(event_mapping_id, '') <> ?",
            (mapping_id, int(row["id"]), mapping_id),
        )
        updated += max(0, int(cursor.rowcount))
    return updated


def _audit_and_retire_legacy_mapping_dimensions(
    conn: sqlite3.Connection,
    *,
    migrated_at: int,
) -> int:
    """Preserve contaminated revisions and replace current ones with unknowns.

    The deployed v2 migration populated mapping confidence/relevance from the
    scorer's threshold-distance confidence.  Those revisions remain immutable
    evidence, but their dimensions cannot be presented under the new names.
    Every affected revision receives an append-only audit row; a current one is
    superseded by a new 0/0/NULL revision rather than being edited in place.
    """
    placeholders = ", ".join("?" for _ in LEGACY_CONFLATED_MAPPING_VERSIONS)
    rows = conn.execute(
        "SELECT mapping_id, event_id, security_id, relation_type, direction, "
        "confidence, relevance, expected_gap, is_current "
        "FROM event_securities WHERE mapping_version IN (" + placeholders + ") "
        "AND (confidence <> 0 OR relevance <> 0 OR expected_gap IS NOT NULL) "
        "ORDER BY is_current DESC, event_id, security_id, relation_type, revision",
        tuple(sorted(LEGACY_CONFLATED_MAPPING_VERSIONS)),
    ).fetchall()
    repaired = 0
    for row in rows:
        legacy_mapping_id = str(row["mapping_id"])
        replacement_mapping_id: str | None = None
        if bool(row["is_current"]):
            replacement_mapping_id = upsert_event_security(
                conn,
                str(row["event_id"]),
                str(row["security_id"]),
                relation_type=str(row["relation_type"]),
                direction=str(row["direction"]),
                expected_gap=None,
                confidence=0.0,
                relevance=0.0,
                rationale=(
                    "旧版曾把评分阈值置信度复用为事件证券映射置信度；"
                    "原修订保留审计，当前修订重置为未知。"
                ),
                mapping_version=MAPPING_DIMENSION_REPAIR_VERSION,
                now=migrated_at,
            )
        conn.execute(
            "INSERT INTO event_mapping_dimension_repairs "
            "(legacy_mapping_id, replacement_mapping_id, repaired_at, reason) "
            "VALUES (?, ?, ?, ?)",
            (
                legacy_mapping_id,
                replacement_mapping_id,
                migrated_at,
                MAPPING_DIMENSION_REPAIR_REASON,
            ),
        )
        repaired += 1
    return repaired


def _sync_frozen_mapping_dimensions(conn: sqlite3.Connection) -> int:
    """Freeze verified dimensions and make audited legacy dimensions unknown."""
    before = conn.total_changes
    conn.execute(
        "UPDATE backtest_signals AS b SET "
        "mapping_confidence = CASE WHEN EXISTS ("
        "SELECT 1 FROM event_mapping_dimension_repairs AS repair "
        "WHERE repair.legacy_mapping_id = b.event_mapping_id) THEN 0 ELSE COALESCE(("
        "SELECT m.confidence FROM event_securities AS m "
        "WHERE m.mapping_id = b.event_mapping_id), 0) END, "
        "mapping_relevance = CASE WHEN EXISTS ("
        "SELECT 1 FROM event_mapping_dimension_repairs AS repair "
        "WHERE repair.legacy_mapping_id = b.event_mapping_id) THEN 0 ELSE COALESCE(("
        "SELECT m.relevance FROM event_securities AS m "
        "WHERE m.mapping_id = b.event_mapping_id), 0) END, "
        "expected_gap = CASE WHEN EXISTS ("
        "SELECT 1 FROM event_mapping_dimension_repairs AS repair "
        "WHERE repair.legacy_mapping_id = b.event_mapping_id) THEN NULL ELSE ("
        "SELECT m.expected_gap FROM event_securities AS m "
        "WHERE m.mapping_id = b.event_mapping_id) END"
    )
    return max(0, conn.total_changes - before)


def _upgrade_completed_score_dimensions(
    conn: sqlite3.Connection,
    *,
    migrated_at: int,
) -> tuple[int, int]:
    """Atomically upgrade an already-backfilled v2 database.

    This deliberately does not replay news, event identities, mappings, or the
    Quant Outbox.  It only installs the additive schema and freezes the three
    ledger dimensions from the already referenced mapping revision.  The
    caller synchronizes the retained Outbox after every possible ledger repair.
    """
    init_event_core_schema(conn)
    _ensure_compatibility_schema(conn)
    _init_compatibility_reference_triggers(conn)
    _init_mapping_dimension_repair_schema(conn)
    repaired = _audit_and_retire_legacy_mapping_dimensions(
        conn,
        migrated_at=migrated_at,
    )
    updated = _sync_frozen_mapping_dimensions(conn)
    return updated, repaired


def _map_legacy_radar(conn: sqlite3.Connection, migrated_at: int) -> int:
    if not _table_exists(conn, "event_cache"):
        return 0
    rows = conn.execute(
        "SELECT event_id, canonical_title, canonical_link, score, tag, "
        "first_seen_at, strategy_version FROM event_cache ORDER BY event_id"
    ).fetchall()
    mapped = 0
    for row in rows:
        legacy_id = str(row["event_id"] or "").strip()
        if not legacy_id:
            continue
        article = conn.execute(
            "SELECT event_id FROM news_cache WHERE link = ?",
            (row["canonical_link"],),
        ).fetchone()
        existing = _current_event(conn, article[0] if article else "")
        if existing is None:
            event_id = resolve_or_create_event(
                conn,
                {"legacy_radar_record": legacy_id},
                title=row["canonical_title"] or row["canonical_link"] or legacy_id,
                event_type=row["tag"] or "legacy_radar",
                direction="中性",
                priority=max(0.0, min(10.0, float(row["score"] or 0.0))),
                occurred_at=int(row["first_seen_at"] or migrated_at),
                now=migrated_at,
                strategy_version=row["strategy_version"] or "",
            )
        else:
            event_id = existing[0]
        prior = conn.execute(
            "SELECT event_id FROM legacy_event_id_map WHERE legacy_event_id = ?",
            (legacy_id,),
        ).fetchone()
        if prior is not None:
            if canonical_event_id(conn, str(prior[0])) != canonical_event_id(conn, event_id):
                raise EventCoreConflictError(
                    "legacy Radar identifier conflicts with its frozen audit mapping"
                )
            continue
        conn.execute(
            "INSERT INTO legacy_event_id_map "
            "(legacy_event_id, event_id, migrated_at, note) VALUES (?, ?, ?, ?)",
            (
                legacy_id,
                event_id,
                migrated_at,
                "legacy Radar identifier retained for audit only; never a public event identity",
            ),
        )
        mapped += 1
    return mapped


_COMPATIBILITY_TRIGGER_NAMES = {
    "trg_news_cache_event_insert",
    "trg_news_cache_event_update",
    "trg_backtest_event_insert",
    "trg_backtest_event_update",
    "trg_news_cache_event_version_insert",
    "trg_news_cache_event_version_update",
    "trg_backtest_event_version_insert",
    "trg_backtest_event_version_update",
    "trg_backtest_mapping_insert",
    "trg_backtest_mapping_update",
}
_MAPPING_DIMENSION_REPAIR_TRIGGER_NAMES = {
    "event_mapping_dimension_repairs_no_update",
    "event_mapping_dimension_repairs_no_delete",
    "event_mapping_dimension_repairs_sealed",
    "event_score_dimensions_marker_no_update",
    "event_score_dimensions_marker_no_delete",
    "event_score_dimensions_marker_no_replace",
}


def _market_date(timestamp: object) -> str | None:
    try:
        value = int(timestamp)
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, _MARKET_TZ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _price_revision_timestamp(row: sqlite3.Row) -> int | None:
    try:
        source_ts = int(row["source_ts"] or 0)
        if source_ts > 0:
            return source_ts
        day = datetime.strptime(str(row["trade_date"]), "%Y-%m-%d").date()
        return int(datetime.combine(day, _MARKET_CLOSE, tzinfo=_MARKET_TZ).timestamp())
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return None


def _outcome_number_matches(stored: object, calculated: float) -> bool:
    try:
        actual = float(stored)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        math.isfinite(actual)
        and math.isfinite(calculated)
        and math.isclose(actual, calculated, rel_tol=1e-10, abs_tol=1e-12)
    )


def _stable_validation_schema(conn):
    required_tables = {
        "events",
        "event_identities",
        "securities",
        "event_securities",
        "event_evidence",
        "event_outcomes",
        "event_similarities",
        "event_merge_history",
        "news_cache",
        "backtest_signals",
        "future_prices",
        "future_price_revisions",
        "integration_signal_outbox",
        "app_meta",
    }
    mapping_validation_marker = (
        conn.execute(
            "SELECT v FROM app_meta WHERE k = 'mapping_validation_started_at'"
        ).fetchone()
        if _table_exists(conn, "app_meta")
        else None
    )
    if mapping_validation_marker is not None:
        required_tables.add("backtest_signal_securities")
    score_marker = (
        conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (SCORE_DIMENSIONS_META_KEY,)
        ).fetchone()
        if _table_exists(conn, "app_meta")
        else None
    )
    if score_marker is not None:
        required_tables.add("event_mapping_dimension_repairs")
        try:
            score_marker_time = int(str(score_marker[0]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("score dimensions marker is invalid") from exc
        if score_marker_time < 0:
            raise RuntimeError("score dimensions marker is invalid")
    else:
        score_marker_time = None
    missing_tables = sorted(
        table for table in required_tables if not _table_exists(conn, table)
    )
    if missing_tables:
        raise RuntimeError(
            "event-core validation missing table(s): " + ", ".join(missing_tables)
        )
    return mapping_validation_marker, score_marker_time


def _validate_stable_columns(conn, mapping_validation_marker):
    required_columns = {
        "event_securities": {"expected_gap"},
        "news_cache": {"event_id", "event_version"},
        "backtest_signals": {
            "event_id",
            "event_version",
            "event_mapping_id",
            "backtest_symbol",
            "mapping_confidence",
            "mapping_relevance",
            "expected_gap",
        },
        **(
            {
                "backtest_signal_securities": {
                    "ledger_signal_id",
                    "mapping_id",
                    "event_id",
                    "security_id",
                    "mapping_revision",
                    "signal_available_at",
                    "captured_at",
                }
            }
            if mapping_validation_marker is not None
            else {}
        ),
        "event_outcomes": {
            "signal_ledger_id",
            "signal_available_at",
            "observation_state",
        },
        "future_prices": {
            "symbol",
            "trade_date",
            "name",
            "close_price",
            "change_amt",
            "change_pct",
            "revision_id",
            "open_price",
            "high_price",
            "low_price",
            "prev_close",
            "volume",
            "amount",
            "limit_up",
            "limit_down",
            "suspension_state",
            "limit_state",
            "tradability_state",
            "ts",
        },
        "future_price_revisions": {
            "revision_id",
            "symbol",
            "trade_date",
            "name",
            "close_price",
            "change_amt",
            "change_pct",
            "source_ts",
            "open_price",
            "high_price",
            "low_price",
            "prev_close",
            "volume",
            "amount",
            "limit_up",
            "limit_down",
            "suspension_state",
            "limit_state",
            "tradability_state",
            "recorded_at",
        },
        "integration_signal_outbox": {
            "seq",
            "ledger_signal_id",
            "domain_event_id",
            "event_version",
            "event_mapping_id",
            "mapping_confidence",
            "mapping_relevance",
            "expected_gap",
        },
    }
    for table, expected in required_columns.items():
        missing = expected - _columns(conn, table)
        if missing:
            raise RuntimeError(
                f"event-core validation {table} missing column(s): "
                + ", ".join(sorted(missing))
            )


def _validate_stable_outbox_dimensions(conn):
    outbox_dimension_marker = conn.execute(
        "SELECT v FROM app_meta WHERE k = ?",
        (MAPPING_DIMENSIONS_BACKFILL_META,),
    ).fetchone()
    if (
        outbox_dimension_marker is None
        or str(outbox_dimension_marker[0]) != "1"
    ):
        raise RuntimeError("integration outbox mapping dimensions marker is invalid")
    invalid_outbox_mapping_dimensions = conn.execute(
        "SELECT 1 FROM integration_signal_outbox WHERE "
        "typeof(mapping_confidence) NOT IN ('integer', 'real') "
        "OR mapping_confidence < 0 OR mapping_confidence > 1 "
        "OR typeof(mapping_relevance) NOT IN ('integer', 'real') "
        "OR mapping_relevance < 0 OR mapping_relevance > 10 "
        "OR (expected_gap IS NOT NULL AND ("
        "typeof(expected_gap) NOT IN ('integer', 'real') "
        "OR expected_gap < -100 OR expected_gap > 100)) LIMIT 1"
    ).fetchone()
    if invalid_outbox_mapping_dimensions is not None:
        raise RuntimeError("integration outbox contains invalid mapping dimensions")
    mismatched_outbox_mapping_dimensions = conn.execute(
        "SELECT o.seq FROM integration_signal_outbox AS o "
        "JOIN backtest_signals AS b ON b.id = o.ledger_signal_id "
        "WHERE o.mapping_confidence IS NOT b.mapping_confidence "
        "OR o.mapping_relevance IS NOT b.mapping_relevance "
        "OR o.expected_gap IS NOT b.expected_gap LIMIT 1"
    ).fetchone()
    if mismatched_outbox_mapping_dimensions is not None:
        raise RuntimeError(
            "integration outbox mapping dimensions do not match its frozen signal ledger"
        )


def _validate_stable_triggers(conn):
    trigger_names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    missing_triggers = sorted(_COMPATIBILITY_TRIGGER_NAMES - trigger_names)
    if _table_exists(conn, "event_mapping_dimension_repairs"):
        missing_triggers.extend(
            sorted(_MAPPING_DIMENSION_REPAIR_TRIGGER_NAMES - trigger_names)
        )
    if missing_triggers:
        raise RuntimeError(
            "event-core validation missing trigger(s): "
            + ", ".join(missing_triggers)
        )
    return trigger_names


def _validate_stable_frozen_mappings(conn, mapping_validation_marker, trigger_names):
    frozen_mapping_rows = 0
    if mapping_validation_marker is not None:
        required_snapshot_triggers = {
            "trg_backtest_signal_securities_insert",
            "trg_backtest_signal_securities_update",
            "trg_backtest_signal_securities_delete",
        }
        missing_snapshot_triggers = sorted(required_snapshot_triggers - trigger_names)
        if missing_snapshot_triggers:
            raise RuntimeError(
                "mapping validation missing trigger(s): "
                + ", ".join(missing_snapshot_triggers)
            )
        frozen_mapping_rows = int(
            conn.execute("SELECT COUNT(*) FROM backtest_signal_securities").fetchone()[0]
        )
        invalid_snapshot = conn.execute(
            "SELECT frozen.ledger_signal_id FROM backtest_signal_securities AS frozen "
            "LEFT JOIN backtest_signals AS signal ON signal.id = frozen.ledger_signal_id "
            "LEFT JOIN event_securities AS mapping ON mapping.mapping_id = frozen.mapping_id "
            "AND mapping.event_id = frozen.event_id "
            "AND mapping.security_id = frozen.security_id "
            "AND mapping.revision = frozen.mapping_revision "
            "WHERE signal.id IS NULL OR mapping.mapping_id IS NULL "
            "OR frozen.event_id IS NOT signal.event_id "
            "OR frozen.signal_available_at IS NOT signal.available_at "
            "OR frozen.captured_at IS NOT signal.recorded_at "
            "OR mapping.created_at > frozen.captured_at "
            "OR mapping.mapping_version IS NOT signal.mapping_version LIMIT 1"
        ).fetchone()
        if invalid_snapshot is not None:
            raise RuntimeError("backtest_signal_securities contains an invalid snapshot")
    return frozen_mapping_rows


def _validate_stable_news_and_ledger(conn):
    news_rows = int(conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0])
    ledger_rows = int(
        conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0]
    )
    invalid_news = conn.execute(
        "SELECT n.link FROM news_cache AS n LEFT JOIN events AS e "
        "ON e.event_id = n.event_id WHERE COALESCE(TRIM(n.event_id), '') = '' "
        "OR e.event_id IS NULL OR typeof(n.event_version) <> 'integer' "
        "OR n.event_version < 1 OR n.event_version > e.version LIMIT 1"
    ).fetchone()
    if invalid_news is not None:
        raise RuntimeError("news_cache contains an invalid event reference/version")
    invalid_ledger = conn.execute(
        "SELECT b.id FROM backtest_signals AS b LEFT JOIN events AS e "
        "ON e.event_id = b.event_id WHERE COALESCE(TRIM(b.event_id), '') = '' "
        "OR e.event_id IS NULL OR typeof(b.event_version) <> 'integer' "
        "OR b.event_version < 1 OR b.event_version > e.version LIMIT 1"
    ).fetchone()
    if invalid_ledger is not None:
        raise RuntimeError("backtest_signals contains an invalid event reference/version")
    invalid_ledger_mapping_score = conn.execute(
        "SELECT 1 FROM backtest_signals WHERE "
        "typeof(mapping_confidence) NOT IN ('integer', 'real') "
        "OR mapping_confidence < 0 OR mapping_confidence > 1 "
        "OR typeof(mapping_relevance) NOT IN ('integer', 'real') "
        "OR mapping_relevance < 0 OR mapping_relevance > 10 "
        "OR (expected_gap IS NOT NULL AND ("
        "typeof(expected_gap) NOT IN ('integer', 'real') "
        "OR expected_gap < -100 OR expected_gap > 100)) LIMIT 1"
    ).fetchone()
    if invalid_ledger_mapping_score is not None:
        raise RuntimeError("backtest_signals contains invalid mapping dimensions")
    return news_rows, ledger_rows


def _validate_stable_mapping_repairs(conn, score_marker_time):
    repaired_mapping_ids: set[str] = set()
    if _table_exists(conn, "event_mapping_dimension_repairs"):
        repair_rows = conn.execute(
            "SELECT r.legacy_mapping_id, r.replacement_mapping_id, r.repaired_at, r.reason, "
            "legacy.event_id AS legacy_event_id, "
            "legacy.security_id AS legacy_security_id, "
            "legacy.relation_type AS legacy_relation_type, "
            "legacy.revision AS legacy_revision, legacy.is_current AS legacy_is_current, "
            "legacy.confidence AS legacy_confidence, "
            "legacy.relevance AS legacy_relevance, "
            "legacy.expected_gap AS legacy_expected_gap, "
            "legacy.mapping_version AS legacy_mapping_version, "
            "replacement.event_id AS replacement_event_id, "
            "replacement.security_id AS replacement_security_id, "
            "replacement.relation_type AS replacement_relation_type, "
            "replacement.revision AS replacement_revision, "
            "replacement.confidence AS replacement_confidence, "
            "replacement.relevance AS replacement_relevance, "
            "replacement.expected_gap AS replacement_expected_gap, "
            "replacement.mapping_version AS replacement_mapping_version "
            "FROM event_mapping_dimension_repairs AS r "
            "LEFT JOIN event_securities AS legacy "
            "ON legacy.mapping_id = r.legacy_mapping_id "
            "LEFT JOIN event_securities AS replacement "
            "ON replacement.mapping_id = r.replacement_mapping_id "
            "ORDER BY r.legacy_mapping_id"
        ).fetchall()
        for repair in repair_rows:
            legacy_mapping_id = str(repair["legacy_mapping_id"] or "")
            if not legacy_mapping_id or repair["legacy_event_id"] is None:
                raise RuntimeError(
                    "mapping dimension repair references a missing legacy revision"
                )
            if str(repair["reason"]) != MAPPING_DIMENSION_REPAIR_REASON:
                raise RuntimeError("mapping dimension repair has an invalid reason")
            if (
                str(repair["legacy_mapping_version"])
                not in LEGACY_CONFLATED_MAPPING_VERSIONS
                or (
                    float(repair["legacy_confidence"]) == 0.0
                    and float(repair["legacy_relevance"]) == 0.0
                    and repair["legacy_expected_gap"] is None
                )
            ):
                raise RuntimeError(
                    "mapping dimension repair lacks legacy conflation provenance"
                )
            if score_marker_time is not None:
                if int(repair["repaired_at"]) != score_marker_time:
                    raise RuntimeError(
                        "mapping dimension repair timestamp does not match its marker"
                    )
            replacement_mapping_id = str(
                repair["replacement_mapping_id"] or ""
            )
            if replacement_mapping_id:
                if repair["replacement_event_id"] is None:
                    raise RuntimeError(
                        "mapping dimension repair references a missing replacement"
                    )
                if (
                    str(repair["legacy_event_id"])
                    != str(repair["replacement_event_id"])
                    or str(repair["legacy_security_id"])
                    != str(repair["replacement_security_id"])
                    or str(repair["legacy_relation_type"])
                    != str(repair["replacement_relation_type"])
                    or int(repair["replacement_revision"])
                    <= int(repair["legacy_revision"])
                    or float(repair["replacement_confidence"]) != 0.0
                    or float(repair["replacement_relevance"]) != 0.0
                    or repair["replacement_expected_gap"] is not None
                    or str(repair["replacement_mapping_version"])
                    != MAPPING_DIMENSION_REPAIR_VERSION
                ):
                    raise RuntimeError(
                        "mapping dimension repair replacement is not an audited unknown revision"
                    )
            elif bool(repair["legacy_is_current"]):
                raise RuntimeError(
                    "mapping dimension repair left a contaminated revision current"
                )
            repaired_mapping_ids.add(legacy_mapping_id)
    return repaired_mapping_ids


def _validate_stable_ledger_mappings(conn, repaired_mapping_ids):
    mapping_rows = conn.execute(
        "SELECT b.id, b.event_id, b.event_mapping_id, b.backtest_symbol, "
        "b.mapping_confidence, b.mapping_relevance, b.expected_gap, "
        "m.event_id, m.security_id, m.confidence, m.relevance, m.expected_gap "
        "FROM backtest_signals AS b "
        "LEFT JOIN event_securities AS m ON m.mapping_id = b.event_mapping_id "
        "WHERE COALESCE(TRIM(b.event_mapping_id), '') <> '' "
        "OR COALESCE(TRIM(b.backtest_symbol), '') <> '' ORDER BY b.id"
    ).fetchall()
    for row in mapping_rows:
        mapping_id = str(row[2] or "").strip()
        symbol = str(row[3] or "").strip()
        if not mapping_id or not symbol or row[7] is None or row[8] is None:
            raise RuntimeError("backtest_signals contains a missing event mapping")
        if str(row[1]) != str(row[7]) or symbol.casefold() != str(row[8]).casefold():
            raise RuntimeError("backtest_signals contains a mismatched event mapping")
        dimensions_mismatch = (
            float(row[4]) != float(row[9])
            or float(row[5]) != float(row[10])
            or row[6] != row[11]
        )
        if dimensions_mismatch and not (
            mapping_id in repaired_mapping_ids
            and float(row[4]) == 0.0
            and float(row[5]) == 0.0
            and row[6] is None
        ):
            raise RuntimeError(
                "backtest_signals contains mapping dimensions that do not match its frozen revision"
            )
    return mapping_rows


def _validate_stable_current_series(conn):
    duplicate_mapping = conn.execute(
        "SELECT 1 FROM event_securities WHERE is_current = 1 "
        "GROUP BY event_id, security_id, relation_type HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate_mapping is not None:
        raise RuntimeError("event_securities contains duplicate current mappings")
    invalid_mapping_score = conn.execute(
        "SELECT 1 FROM event_securities WHERE expected_gap IS NOT NULL AND ("
        "typeof(expected_gap) NOT IN ('integer', 'real') "
        "OR expected_gap < -100 OR expected_gap > 100) LIMIT 1"
    ).fetchone()
    if invalid_mapping_score is not None:
        raise RuntimeError("event_securities contains an invalid expected_gap")
    duplicate_frozen_outcome = conn.execute(
        "SELECT 1 FROM event_outcomes WHERE is_current = 1 "
        "AND signal_ledger_id IS NOT NULL "
        "GROUP BY signal_ledger_id, mapping_id, horizon, method_version "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    duplicate_legacy_outcome = conn.execute(
        "SELECT 1 FROM event_outcomes WHERE is_current = 1 "
        "AND signal_ledger_id IS NULL "
        "GROUP BY event_id, security_id, mapping_id, horizon, method_version "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate_frozen_outcome is not None or duplicate_legacy_outcome is not None:
        raise RuntimeError("event_outcomes contains duplicate current series")


def _validate_stable_current_prices(conn):
    future_price_rows = int(
        conn.execute("SELECT COUNT(*) FROM future_prices").fetchone()[0]
    )
    invalid_future_price = conn.execute(
        "SELECT p.symbol, p.trade_date FROM future_prices AS p "
        "LEFT JOIN future_price_revisions AS r ON r.revision_id = p.revision_id "
        "WHERE COALESCE(TRIM(p.revision_id), '') = '' OR r.revision_id IS NULL "
        "OR p.symbol IS NOT r.symbol OR p.trade_date IS NOT r.trade_date "
        "OR p.name IS NOT r.name OR p.close_price IS NOT r.close_price "
        "OR p.change_amt IS NOT r.change_amt OR p.change_pct IS NOT r.change_pct "
        "OR p.open_price IS NOT r.open_price OR p.high_price IS NOT r.high_price "
        "OR p.low_price IS NOT r.low_price OR p.prev_close IS NOT r.prev_close "
        "OR p.volume IS NOT r.volume OR p.amount IS NOT r.amount "
        "OR p.limit_up IS NOT r.limit_up OR p.limit_down IS NOT r.limit_down "
        "OR p.suspension_state IS NOT r.suspension_state "
        "OR p.limit_state IS NOT r.limit_state "
        "OR p.tradability_state IS NOT r.tradability_state "
        "OR p.ts IS NOT r.source_ts "
        "LIMIT 1"
    ).fetchone()
    if invalid_future_price is not None:
        raise RuntimeError(
            "future_prices contains a missing or mismatched immutable revision"
        )
    return future_price_rows


def _stable_validation_outcomes(conn, price_methods, price_prefixes):
    if _table_exists(conn, "backtest_signal_securities"):
        frozen_projection = (
            "frozen.mapping_id AS frozen_mapping_id, "
            "frozen.mapping_revision AS frozen_mapping_revision, "
            "frozen.signal_available_at AS frozen_available_at, "
            "frozen.captured_at AS frozen_captured_at "
        )
        frozen_join = (
            "LEFT JOIN backtest_signal_securities AS frozen "
            "ON frozen.ledger_signal_id = outcome.signal_ledger_id "
            "AND frozen.mapping_id = outcome.mapping_id "
            "AND frozen.event_id = outcome.event_id "
            "AND frozen.security_id = outcome.security_id "
            "AND frozen.mapping_revision = outcome.mapping_revision "
        )
    else:
        frozen_projection = (
            "NULL AS frozen_mapping_id, NULL AS frozen_mapping_revision, "
            "NULL AS frozen_available_at, NULL AS frozen_captured_at "
        )
        frozen_join = ""

    return conn.execute(
        "SELECT outcome.*, ledger.event_id AS ledger_event_id, "
        "ledger.event_version AS ledger_event_version, "
        "ledger.event_mapping_id AS ledger_mapping_id, "
        "ledger.backtest_symbol AS ledger_security_id, "
        "ledger.available_at AS ledger_available_at, "
        "ledger.selected AS ledger_selected, "
        "ledger.is_similarity_copy AS ledger_similarity_copy, "
        "ledger.direction AS ledger_direction, "
        "ledger.recorded_at AS ledger_recorded_at, "
        "ledger.mapping_version AS ledger_mapping_version, "
        "mapping.direction AS mapping_direction, "
        "mapping.created_at AS mapping_created_at, "
        "mapping.mapping_version AS mapping_version, "
        + frozen_projection
        +
        "FROM event_outcomes AS outcome "
        "LEFT JOIN backtest_signals AS ledger ON ledger.id = outcome.signal_ledger_id "
        "LEFT JOIN event_securities AS mapping ON mapping.mapping_id = outcome.mapping_id "
        + frozen_join
        +
        "WHERE outcome.method_version IN (?, ?, ?, ?) "
        "OR outcome.data_version LIKE ? OR outcome.data_version LIKE ? "
        "OR outcome.data_version LIKE ? OR outcome.data_version LIKE ? "
        "ORDER BY outcome.created_at, outcome.outcome_revision_id",
        (*price_methods, *(f"{prefix}%" for prefix in price_prefixes)),
    )


def _stable_outcome_price_revisions(conn, row):
    method_version = str(row["method_version"] or "")
    data_version = str(row["data_version"] or "")
    data_prefix = _PRICE_OUTCOME_CONTRACTS.get(method_version)
    if data_prefix is None or not data_version.startswith(data_prefix):
        raise RuntimeError("event_outcomes contains an invalid price data_version")
    revision_ids = data_version[len(data_prefix):].split(":")
    if len(revision_ids) != 4:
        raise RuntimeError("event_outcomes contains an invalid price data_version")
    try:
        canonical_ids = [str(uuid.UUID(value)) for value in revision_ids]
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "event_outcomes contains an invalid price data_version"
        ) from exc
    if canonical_ids != revision_ids:
        raise RuntimeError("event_outcomes contains an invalid price data_version")
    placeholders = ", ".join("?" for _ in canonical_ids)
    revision_rows = conn.execute(
        "SELECT revision_id, symbol, trade_date, close_price, source_ts, recorded_at "
        "FROM future_price_revisions "
        f"WHERE revision_id IN ({placeholders})",
        tuple(canonical_ids),
    ).fetchall()
    revisions = {str(reference["revision_id"]): reference for reference in revision_rows}
    if not set(canonical_ids).issubset(revisions):
        raise RuntimeError(
            "event_outcomes references a missing future price revision"
        )
    return method_version, [revisions[revision_id] for revision_id in canonical_ids]


def _validate_stable_outcome_scalar(row, method_version, benchmark_symbol, horizon_days, proxy_symbols):
    is_v4 = method_version == "a-share-event-ohlc-v4"
    is_v5 = method_version == "a-share-event-ohlc-v5-frozen-mapping"
    is_equity_ohlc = is_v4 or is_v5
    valid_horizon_days = horizon_days if is_equity_ohlc else (1, 2)

    try:
        data_quality = float(row["data_quality"])
        complete_data_quality = math.isfinite(data_quality) and (
            0.0 <= data_quality <= 1.0 if is_equity_ohlc else data_quality == 1.0
        )
    except (TypeError, ValueError, OverflowError):
        complete_data_quality = False
    if (
        str(row["observation_state"]) != "complete"
        or str(row["horizon"]) not in {
            f"T+{offset}" for offset in valid_horizon_days
        }
        or str(row["benchmark_security_id"] or "").casefold()
        != benchmark_symbol
        or (
            not is_equity_ohlc
            and str(row["security_id"] or "").casefold()
            not in proxy_symbols
        )
        or (not is_equity_ohlc and row["mfe"] is not None)
        or (not is_equity_ohlc and row["mae"] is not None)
        or (is_v5 and row["mfe"] is not None)
        or (is_v5 and row["mae"] is not None)
        or not complete_data_quality
    ):
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome violates its scalar contract"
        )
    if is_v4:
        try:
            mfe_valid = row["mfe"] is None or (
                math.isfinite(float(row["mfe"]))
                and 0.0 <= float(row["mfe"]) <= 1_000_000.0
            )
            mae_valid = row["mae"] is None or (
                math.isfinite(float(row["mae"])) and -1.0 <= float(row["mae"]) <= 0.0
            )
        except (TypeError, ValueError, OverflowError):
            mfe_valid = mae_valid = False
        if not (mfe_valid and mae_valid):
            raise RuntimeError("v4 event_outcome violates its excursion contract")
    return is_v4, is_v5, is_equity_ohlc


def _validate_stable_outcome_ledger(row, flags):
    is_v4, is_v5, is_equity_ohlc = flags
    try:
        signal_ledger_id = int(row["signal_ledger_id"])
        signal_available_at = int(row["signal_available_at"])
        baseline_at = int(row["baseline_at"])
        measured_at = int(row["measured_at"])
        ledger_exact = (
            signal_ledger_id >= 1
            and int(row["ledger_available_at"]) == signal_available_at
            and int(row["ledger_selected"]) == 1
            and int(row["ledger_similarity_copy"]) == 0
            and int(row["ledger_event_version"]) >= 1
            and signal_available_at < baseline_at
        )
        if ledger_exact and not is_equity_ohlc:
            ledger_exact = (
                str(row["ledger_event_id"]) == str(row["event_id"])
                and str(row["ledger_mapping_id"]) == str(row["mapping_id"])
                and str(row["ledger_security_id"]).casefold()
                == str(row["security_id"]).casefold()
            )
        if ledger_exact and is_v5:
            ledger_exact = (
                str(row["frozen_mapping_id"] or "") == str(row["mapping_id"])
                and int(row["frozen_mapping_revision"] or 0)
                == int(row["mapping_revision"])
                and int(row["frozen_available_at"] or 0) == signal_available_at
                and int(row["frozen_captured_at"] or 0)
                == int(row["ledger_recorded_at"] or 0)
                and int(row["mapping_created_at"] or 0)
                <= int(row["frozen_captured_at"] or 0)
                and str(row["mapping_version"] or "")
                == str(row["ledger_mapping_version"] or "")
            )
    except (TypeError, ValueError, OverflowError):
        ledger_exact = False
    if not ledger_exact:
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome does not match its frozen signal ledger"
        )
    return signal_available_at, baseline_at, measured_at


def _stable_outcome_dates(row, flags, observation, baseline_trade_date, trading_day_after):
    is_v4, is_v5, _is_equity_ohlc = flags
    signal_available_at, baseline_at, measured_at = observation
    offset = int(str(row["horizon"])[2:])
    expected_baseline_date = baseline_trade_date(signal_available_at)
    expected_measured_date = (
        None
        if expected_baseline_date is None
        else trading_day_after(expected_baseline_date, offset)
    )
    if (
        expected_baseline_date is None
        or expected_measured_date is None
        or _market_date(baseline_at) != expected_baseline_date
        or _market_date(measured_at) != expected_measured_date
    ):
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome uses invalid horizon dates"
        )
    return expected_baseline_date, expected_measured_date


def _validate_stable_outcome_roles(row, referenced, flags, observation, dates, benchmark_symbol):
    is_v4, is_v5, is_equity_ohlc = flags
    signal_available_at, baseline_at, measured_at = observation
    expected_baseline_date, expected_measured_date = dates
    if is_v5:
        try:
            outcome_created_at = int(row["created_at"])
            recorded_at = [int(revision["recorded_at"]) for revision in referenced]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "v5 event_outcome has invalid price revision availability"
            ) from exc
        if any(
            value <= 0 or value > outcome_created_at for value in recorded_at
        ):
            raise RuntimeError(
                "v5 event_outcome uses a price revision unavailable when created"
            )
    asset_baseline, asset_measured, market_baseline, market_measured = referenced
    asset_symbol = (
        str(asset_baseline["symbol"]).casefold()
        if is_equity_ohlc
        else str(row["security_id"]).casefold()
    )
    expected_roles = (
        (asset_symbol, expected_baseline_date),
        (asset_symbol, expected_measured_date),
        (benchmark_symbol, expected_baseline_date),
        (benchmark_symbol, expected_measured_date),
    )
    if any(
        str(revision["symbol"]).casefold() != symbol
        or str(revision["trade_date"]) != trade_date
        for revision, (symbol, trade_date) in zip(referenced, expected_roles)
    ):
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome price revisions have invalid roles"
        )
    revision_timestamps = [
        _price_revision_timestamp(revision) for revision in referenced
    ]
    if (
        any(timestamp is None for timestamp in revision_timestamps)
        or revision_timestamps[0] != baseline_at
        or revision_timestamps[1] != measured_at
        or int(revision_timestamps[2]) <= signal_available_at
        or any(
            _market_date(timestamp) != trade_date
            for timestamp, (_, trade_date) in zip(
                revision_timestamps, expected_roles
            )
        )
    ):
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome timestamps do not match price revisions"
        )


def _validate_stable_outcome_returns(row, referenced, flags, direction_hit_for_return):
    is_v4, is_v5, is_equity_ohlc = flags
    asset_baseline, asset_measured, market_baseline, market_measured = referenced
    try:
        asset_base = float(asset_baseline["close_price"])
        asset_target = float(asset_measured["close_price"])
        market_base = float(market_baseline["close_price"])
        market_target = float(market_measured["close_price"])
        if min(asset_base, asset_target, market_base, market_target) <= 0:
            raise ValueError("close must be positive")
        gross_return = asset_target / asset_base - 1.0
        benchmark_return = market_target / market_base - 1.0
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome references invalid close prices"
        ) from exc
    if not (
        _outcome_number_matches(row["baseline_price"], asset_base)
        and _outcome_number_matches(row["gross_return"], gross_return)
        and _outcome_number_matches(row["benchmark_return"], benchmark_return)
        and _outcome_number_matches(
            row["excess_return"], gross_return - benchmark_return
        )
    ):
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome returns do not match price revisions"
        )
    expected_hit = direction_hit_for_return(
        str(
            row["mapping_direction"]
            if is_equity_ohlc
            else row["ledger_direction"]
        ),
        gross_return,
    )
    stored_hit = row["direction_hit"]
    try:
        hit_matches = (
            stored_hit is None
            if expected_hit is None
            else stored_hit is not None
            and int(stored_hit) == int(expected_hit)
        )
    except (TypeError, ValueError, OverflowError):
        hit_matches = False
    if not hit_matches:
        raise RuntimeError(
            f"{'v5' if is_v5 else 'v4' if is_v4 else 'v3'} event_outcome direction result does not match its frozen signal"
        )


def _validate_stable_outcome_row(conn, row):
    method_version, referenced = _stable_outcome_price_revisions(conn, row)
    if method_version not in {
        "a-share-first-post-close-v3",
        "a-share-event-ohlc-v4",
        "a-share-event-ohlc-v5-frozen-mapping",
    }:
        return
    from services.event_outcomes import (
        MARKET_BENCHMARK_SYMBOL,
        OUTCOME_HORIZON_DAYS,
        OUTCOME_PROXY_SYMBOLS,
        baseline_trade_date,
        direction_hit_for_return,
    )
    from db import database as database_module

    flags = _validate_stable_outcome_scalar(
        row, method_version, MARKET_BENCHMARK_SYMBOL,
        OUTCOME_HORIZON_DAYS, OUTCOME_PROXY_SYMBOLS,
    )
    observation = _validate_stable_outcome_ledger(row, flags)
    dates = _stable_outcome_dates(
        row, flags, observation, baseline_trade_date, database_module._trading_day_after,
    )
    _validate_stable_outcome_roles(
        row, referenced, flags, observation, dates, MARKET_BENCHMARK_SYMBOL,
    )
    _validate_stable_outcome_returns(row, referenced, flags, direction_hit_for_return)


def _validate_stable_outcome_prices(conn):
    outcome_price_references = 0
    price_methods = tuple(_PRICE_OUTCOME_CONTRACTS)
    price_prefixes = tuple(_PRICE_OUTCOME_CONTRACTS.values())
    invalid_legacy_observation = conn.execute(
        "SELECT 1 FROM event_outcomes WHERE method_version NOT IN (?, ?, ?) AND ("
        "observation_state <> 'legacy_unknown' OR signal_ledger_id IS NOT NULL "
        "OR signal_available_at IS NOT NULL) LIMIT 1",
        (
            "a-share-first-post-close-v3",
            "a-share-event-ohlc-v4",
            "a-share-event-ohlc-v5-frozen-mapping",
        ),
    ).fetchone()
    if invalid_legacy_observation is not None:
        raise RuntimeError(
            "legacy event_outcomes contains fabricated signal observation metadata"
        )

    for row in _stable_validation_outcomes(conn, price_methods, price_prefixes):
        outcome_price_references += 1
        _validate_stable_outcome_row(conn, row)
    return outcome_price_references


def validate_stable_event_core(conn: sqlite3.Connection) -> dict[str, int]:
    """Read-only, full referential validation for a completed event migration."""
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise RuntimeError("foreign-key enforcement is required for validation")
    _reject_unmigrated_legacy_radar_memberships(conn)

    mapping_validation_marker, score_marker_time = _stable_validation_schema(conn)
    _validate_stable_columns(conn, mapping_validation_marker)
    _validate_stable_outbox_dimensions(conn)
    trigger_names = _validate_stable_triggers(conn)
    frozen_mapping_rows = _validate_stable_frozen_mappings(
        conn, mapping_validation_marker, trigger_names,
    )
    news_rows, ledger_rows = _validate_stable_news_and_ledger(conn)
    repaired_mapping_ids = _validate_stable_mapping_repairs(conn, score_marker_time)
    mapping_rows = _validate_stable_ledger_mappings(conn, repaired_mapping_ids)
    _validate_stable_current_series(conn)
    future_price_rows = _validate_stable_current_prices(conn)
    outcome_price_references = _validate_stable_outcome_prices(conn)

    for row in conn.execute("SELECT event_id FROM events ORDER BY event_id"):
        canonical_event_id(conn, str(row[0]))
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign_key_check failed with {len(violations)} row(s)")
    from db.integration_outbox import get_integration_outbox_state

    unresolved_outbox = count_unresolved_event_core_outbox_references(conn)
    if unresolved_outbox:
        raise RuntimeError(
            "integration outbox contains "
            f"{unresolved_outbox} unresolved stable event reference(s)"
        )
    outbox_state = get_integration_outbox_state(conn)
    return {
        "validated_news_rows": news_rows,
        "validated_ledger_rows": ledger_rows,
        "validated_signal_mapping_rows": frozen_mapping_rows,
        "validated_mapping_rows": len(mapping_rows),
        "validated_outbox_rows": int(
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
        ),
        "validated_future_price_rows": future_price_rows,
        "validated_outcome_price_references": outcome_price_references,
        "outbox_floor_seq": int(outbox_state["floor_seq"]),
        "outbox_last_seq": int(outbox_state["last_seq"]),
        "foreign_key_violations": 0,
    }


def backfill_stable_event_core_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
    force: bool = False,
) -> dict[str, int | str | bool]:
    """Run the migration inside a caller-owned write transaction.

    This entry point never commits or rolls back.  It exists so the maintenance
    script can hold one ``BEGIN IMMEDIATE`` lock across backup and migration.
    """
    if not conn.in_transaction:
        raise RuntimeError("event-core migration requires an active transaction")
    conn.row_factory = sqlite3.Row
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise RuntimeError("foreign-key enforcement is required")
    # Snapshot before the first schema/data helper runs.  The assertion at
    # every successful return remains inside the caller-owned IMMEDIATE
    # transaction, immediately before its commit boundary.
    source_invariants = _snapshot_source_invariants(conn)
    migrated_at = int(now if now is not None else time.time())
    if migrated_at <= 0:
        raise ValueError("event-core migration timestamp must be positive")
    if _table_exists(conn, "app_meta"):
        marker = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()
    else:
        marker = None
    if marker is not None:
        score_marker = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (SCORE_DIMENSIONS_META_KEY,)
        ).fetchone()
        outbox_dimensions_current = _outbox_dimensions_current(conn)
        upgraded_rows = 0
        repaired_mappings = 0
        if score_marker is None:
            upgraded_rows, repaired_mappings = _upgrade_completed_score_dimensions(
                conn,
                migrated_at=migrated_at,
            )
        outbox_dimension_rows = 0
        outbox_dimensions_upgraded = (
            score_marker is None or not outbox_dimensions_current
        )
        if outbox_dimensions_upgraded:
            from db.integration_outbox import backfill_outbox_mapping_dimensions

            outbox_dimension_rows = backfill_outbox_mapping_dimensions(
                conn,
                force=True,
            )
        # ``force`` is a backwards-compatible spelling for validation.  A
        # completed migration never replays event identity/history; the sole
        # allowed write is a one-time additive dimension upgrade/resync.
        verify_event_core_backfill_state(conn)
        validation = validate_stable_event_core(conn)
        if score_marker is None:
            conn.execute(
                "INSERT INTO app_meta (k, v) VALUES (?, ?)",
                (SCORE_DIMENSIONS_META_KEY, str(migrated_at)),
            )
        _assert_source_invariants(conn, source_invariants)
        return {
            "already_complete": True,
            "score_dimensions_upgraded": score_marker is None,
            "score_dimension_rows": upgraded_rows,
            "outbox_dimensions_upgraded": outbox_dimensions_upgraded,
            "outbox_dimension_rows": outbox_dimension_rows,
            "repaired_mapping_rows": repaired_mappings,
            "news_rows": 0,
            "ledger_rows": 0,
            "mapping_rows": 0,
            "legacy_maps": 0,
            "completed_at": str(marker[0]),
            **validation,
        }

    # Run the legacy-membership preflight before any schema or data write.  The
    # public wrapper and maintenance CLI both invoke this entry point while an
    # IMMEDIATE transaction is held, so failure is atomic for dry-run and apply.
    _reject_unmigrated_legacy_radar_memberships(conn)
    init_event_core_schema(conn)
    _ensure_compatibility_schema(conn)
    # These ledgers predate the stable event core in production.  Initialize
    # their additive compatibility schema inside this same maintenance
    # transaction so validation never depends on a prior new-image startup.
    # ``_init_future_prices`` deterministically freezes every legacy current
    # close before installing its revision pointer guards.
    from db.database import _init_future_prices

    _init_future_prices(conn.cursor())
    news_rows = _backfill_news(conn, migrated_at)
    ledger_rows = _backfill_ledger_only(conn, migrated_at)
    mapping_rows = _backfill_signal_mappings(conn, migrated_at)
    legacy_maps = _map_legacy_radar(conn, migrated_at)
    _init_compatibility_reference_triggers(conn)
    _init_mapping_dimension_repair_schema(conn)
    from db.integration_outbox import (
        backfill_outbox_event_references,
        init_integration_signal_outbox,
    )

    # The legacy reference helper only alters an already-present Outbox.  The
    # explicit initializer also creates a missing Outbox, adds v3 columns and
    # seeds/normalizes durable cursor metadata.  Disable both historical signal
    # import and retention trimming: migration must neither invent nor discard
    # any retained replay revision.
    init_integration_signal_outbox(
        conn,
        now=migrated_at,
        backfill=False,
        trim=False,
    )
    unresolved_outbox = backfill_outbox_event_references(conn)
    if unresolved_outbox:
        raise RuntimeError(
            "integration outbox contains "
            f"{unresolved_outbox} unrepairable legacy reference(s); "
            "rotate/clear it explicitly before migration"
        )
    validation = validate_stable_event_core(conn)
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
        (SCHEMA_META_KEY, "2"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
        (BACKFILL_META_KEY, str(migrated_at)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
        (SCORE_DIMENSIONS_META_KEY, str(migrated_at)),
    )
    verify_event_core_backfill_state(conn)
    _assert_source_invariants(conn, source_invariants)
    return {
        "already_complete": False,
        "news_rows": news_rows,
        "ledger_rows": ledger_rows,
        "mapping_rows": mapping_rows,
        "legacy_maps": legacy_maps,
        "completed_at": str(migrated_at),
        **validation,
    }


def backfill_stable_event_core(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
    force: bool = False,
) -> dict[str, int | str | bool]:
    """Atomically migrate existing rows; caller must not have a transaction."""
    if conn.in_transaction:
        raise RuntimeError("event-core migration requires a clean connection")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise RuntimeError("foreign-key enforcement is required")

    # Completed migrations take a genuinely read-only fast path only after both
    # additive dimension migrations are recorded and their columns exist.  An
    # older production database otherwise needs the normal IMMEDIATE transaction
    # below so schema, frozen values, marker and validation remain atomic.
    if _table_exists(conn, "app_meta"):
        marker = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()
        score_marker = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (SCORE_DIMENSIONS_META_KEY,)
        ).fetchone()
        if (
            marker is not None
            and score_marker is not None
            and _outbox_dimensions_current(conn)
        ):
            verify_event_core_backfill_state(conn)
            validation = validate_stable_event_core(conn)
            return {
                "already_complete": True,
                "score_dimensions_upgraded": False,
                "score_dimension_rows": 0,
                "outbox_dimensions_upgraded": False,
                "outbox_dimension_rows": 0,
                "news_rows": 0,
                "ledger_rows": 0,
                "mapping_rows": 0,
                "legacy_maps": 0,
                "completed_at": str(marker[0]),
                **validation,
            }

    conn.execute("BEGIN IMMEDIATE")
    try:
        result = backfill_stable_event_core_in_transaction(
            conn, now=now, force=force
        )
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


__all__ = [
    "backfill_stable_event_core",
    "backfill_stable_event_core_in_transaction",
    "validate_stable_event_core",
    "BACKFILL_META_KEY",
    "SCHEMA_META_KEY",
    "SCORE_DIMENSIONS_META_KEY",
    "MAPPING_DIMENSION_REPAIR_VERSION",
    "MAPPING_DIMENSION_REPAIR_REASON",
]
