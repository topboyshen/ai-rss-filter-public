"""Durable, bounded export log for the AI -> Quant integration.

This is intentionally separate from ``backtest_signals``.  The historical
ledger may delete whole revision chains for retention or test-data cleanup;
an external cursor must instead observe a contiguous append-only sequence with
an explicit retained floor.  Outbox rows contain only bounded, export-safe
business fields and retain no article/feed URL or raw event/source key.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import time
import unicodedata
import uuid
from typing import Any

from scoring_provenance import (
    NON_EXPORTABLE_SCORE_PATHS,
    RECOGNIZED_SCORE_PATHS,
    normalize_score_path,
)


LEDGER_ID_META = "integration_outbox_ledger_id"
EPOCH_META = "integration_outbox_epoch"
FLOOR_META = "integration_outbox_floor_seq"
LAST_META = "integration_outbox_last_seq"
BACKFILL_META = "integration_outbox_backfill_v1"
MAPPING_DIMENSIONS_BACKFILL_META = "integration_outbox_mapping_dimensions_v1"

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_ROWS = 20_000
BACKFILL_MAX_ROWS = 5_000
MIN_MAX_ROWS = 100
MAX_MAX_ROWS = 100_000

_HEX_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
FIELD_BYTES = {
    "event_id": 36,
    "signal_revision_id": 64,
    "event_mapping_id": 36,
    "direction": 32,
    "strategy_version": 128,
    "mapping_version": 128,
    "mapped_theme": 192,
    "backtest_symbol": 32,
    "score_path": 64,
    "revision_key": 128,
    "signal_stage": 64,
    "revision_state_key": 128,
}

_LEDGER_EXPORT_FIELDS = (
    "signal_key",
    "event_key",
    "link",
    "available_at",
    "recorded_at",
    "score",
    "direction",
    "score_confidence",
    "strategy_version",
    "mapping_version",
    "mapped_theme",
    "backtest_symbol",
    "mapping_confidence",
    "mapping_relevance",
    "expected_gap",
    "score_path",
    "revision_key",
    "signal_stage",
    "revision_seq",
    "revision_state_key",
    "independence_key",
    "aggregation_threshold",
    "selected",
    "is_similarity_copy",
    "event_id",
    "event_version",
    "event_mapping_id",
)


def is_exportable_score_path(value: object) -> bool:
    """Keep non-substantive safe-neutral resolutions out of Quant export."""
    path = normalize_score_path(value)
    return (
        path in RECOGNIZED_SCORE_PATHS
        and path not in NON_EXPORTABLE_SCORE_PATHS
    )


def _meta_get(conn, key: str) -> str:
    row = conn.execute("SELECT v FROM app_meta WHERE k = ?", (key,)).fetchone()
    return str(row[0]) if row else ""


def _meta_set(conn, key: str, value: object) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
        (key, str(value)),
    )


def _meta_set_default(conn, key: str, value: object) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO app_meta (k, v) VALUES (?, ?)",
        (key, str(value)),
    )


def bounded_text(value: object, max_bytes: int) -> str:
    """Normalize controls and truncate on a UTF-8 boundary."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in text
    ).strip()
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def stable_text_hash(value: object) -> str:
    return hashlib.sha256(
        str(value or "").encode("utf-8", errors="replace")
    ).hexdigest()


def stable_signal_revision_id(value: object) -> str:
    """Return the legacy opaque delivery key for one immutable revision.

    This hash is intentionally *not* the domain event identity.  The database
    column retains its historical ``event_id`` name for an additive production
    migration, while the v3 wire contract exposes it as
    ``signal_revision_id``.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(
        b"ai-filter-event-id-v1\0" + raw.encode("utf-8", errors="replace")
    ).hexdigest()


# Private callers in older deployments may still import this helper.  Keep the
# alias during the additive schema migration, but never expose its result as a
# domain event UUID.
stable_event_id = stable_signal_revision_id


def _uuid_text(value: object, field: str, *, required: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError(f"integration outbox {field} is required")
        return ""
    try:
        return str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"integration outbox {field} must be a UUID") from exc


def _domain_event_reference(
    conn,
    signal: dict[str, Any],
    *,
    allow_missing: bool = False,
) -> tuple[str | None, int | None, str | None]:
    """Validate one persistent event reference without inventing an identity."""
    try:
        event_id = _uuid_text(signal.get("event_id"), "event_id")
        event_version = int(signal.get("event_version") or 0)
        if event_version < 1:
            raise ValueError("integration outbox event_version must be positive")
    except (TypeError, ValueError):
        if allow_missing:
            return None, None, None
        raise

    event_row = conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if event_row is None:
        if allow_missing:
            return None, None, None
        raise ValueError("integration outbox event_id is not persisted")
    if event_version > int(event_row[0]):
        if allow_missing:
            return None, None, None
        raise ValueError("integration outbox event_version exceeds persisted event")

    raw_mapping_id = str(signal.get("event_mapping_id") or "").strip()
    try:
        event_mapping_id = (
            _uuid_text(raw_mapping_id, "event_mapping_id", required=False)
            if raw_mapping_id
            else ""
        )
    except ValueError:
        if allow_missing:
            return None, None, None
        raise
    if event_mapping_id:
        mapping_row = conn.execute(
            "SELECT 1 FROM event_securities WHERE mapping_id = ? AND event_id = ?",
            (event_mapping_id, event_id),
        ).fetchone()
        if mapping_row is None:
            if allow_missing:
                return None, None, None
            raise ValueError(
                "integration outbox event_mapping_id is not persisted for event"
            )
    return event_id, event_version, event_mapping_id or None


def _ensure_export_columns(conn) -> set[str]:
    """Install additive Outbox columns and return the names just added."""
    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(integration_signal_outbox)"
        ).fetchall()
    }
    additions = (
        ("domain_event_id", "TEXT"),
        ("event_version", "INTEGER"),
        ("event_mapping_id", "TEXT"),
        (
            "mapping_confidence",
            "REAL NOT NULL DEFAULT 0 CHECK ("
            "typeof(mapping_confidence) IN ('integer', 'real') "
            "AND mapping_confidence >= 0 AND mapping_confidence <= 1)",
        ),
        (
            "mapping_relevance",
            "REAL NOT NULL DEFAULT 0 CHECK ("
            "typeof(mapping_relevance) IN ('integer', 'real') "
            "AND mapping_relevance >= 0 AND mapping_relevance <= 10)",
        ),
        (
            "expected_gap",
            "REAL CHECK (expected_gap IS NULL OR ("
            "typeof(expected_gap) IN ('integer', 'real') "
            "AND expected_gap >= -100 AND expected_gap <= 100))",
        ),
    )
    added: set[str] = set()
    for column, definition in additions:
        if column not in columns:
            conn.execute(
                f"ALTER TABLE integration_signal_outbox ADD COLUMN {column} {definition}"
            )
            added.add(column)
    return added


def backfill_outbox_mapping_dimensions(conn, *, force: bool = False) -> int:
    """Freeze retained v4 dimensions from the immutable signal ledger.

    Existing v3-only rows receive conservative 0/0/NULL defaults when their
    ledger row has already expired.  Rows with a retained ledger source copy
    that row's frozen values exactly, including the sign of ``expected_gap``.
    Normal startup performs this once; an offline semantic-repair migration may
    pass ``force=True`` before v4 traffic is enabled.
    """
    if not _table_exists(conn, "integration_signal_outbox"):
        return 0
    added = _ensure_export_columns(conn)
    marker = _meta_get(conn, MAPPING_DIMENSIONS_BACKFILL_META)
    if not force and not added and marker == "1":
        return 0
    if not _table_exists(conn, "backtest_signals"):
        if conn.execute(
            "SELECT 1 FROM integration_signal_outbox LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError(
                "cannot initialize Outbox mapping dimensions without signal ledger"
            )
        _meta_set(conn, MAPPING_DIMENSIONS_BACKFILL_META, 1)
        return 0
    ledger_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(backtest_signals)")
    }
    required = {"mapping_confidence", "mapping_relevance", "expected_gap"}
    if not required.issubset(ledger_columns):
        raise RuntimeError("signal ledger mapping dimensions are unavailable")

    before = conn.total_changes
    conn.execute(
        "UPDATE integration_signal_outbox AS outbox SET "
        "mapping_confidence = (SELECT ledger.mapping_confidence "
        "FROM backtest_signals AS ledger "
        "WHERE ledger.id = outbox.ledger_signal_id), "
        "mapping_relevance = (SELECT ledger.mapping_relevance "
        "FROM backtest_signals AS ledger "
        "WHERE ledger.id = outbox.ledger_signal_id), "
        "expected_gap = (SELECT ledger.expected_gap "
        "FROM backtest_signals AS ledger "
        "WHERE ledger.id = outbox.ledger_signal_id) "
        "WHERE EXISTS (SELECT 1 FROM backtest_signals AS ledger "
        "WHERE ledger.id = outbox.ledger_signal_id)"
    )
    updated = max(0, conn.total_changes - before)
    _meta_set(conn, MAPPING_DIMENSIONS_BACKFILL_META, 1)
    return updated


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def backfill_outbox_event_references(conn) -> int:
    """Fill missing v3 identity fields and return the unresolved row count.

    The retained ``backtest_signals`` row is the only permitted source.  The
    helper never synthesizes an event UUID, deletes an outbox row, or clears an
    existing value.  Outbox rows deliberately outlive ledger cleanup, so rows
    without a valid retained source remain unresolved and the page reader
    fails closed when it reaches them.

    This helper intentionally does not commit so an explicit event migration
    can update the ledger and repair the outbox in one transaction.
    """
    if not _table_exists(conn, "integration_signal_outbox"):
        return 0
    _ensure_export_columns(conn)

    required_tables = {"backtest_signals", "events", "event_securities"}
    if not all(_table_exists(conn, table) for table in required_tables):
        return int(
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
        )

    rows = conn.execute(
        "SELECT o.seq, o.domain_event_id, o.event_version, o.event_mapping_id, "
        "b.event_id, b.event_version, b.event_mapping_id "
        "FROM integration_signal_outbox AS o "
        "JOIN backtest_signals AS b ON b.id = o.ledger_signal_id "
        "WHERE COALESCE(o.domain_event_id, '') = '' "
        "OR o.event_version IS NULL OR o.event_version = 0 "
        "OR (COALESCE(o.event_mapping_id, '') = '' "
        "AND COALESCE(b.event_mapping_id, '') <> '')"
    ).fetchall()
    for row in rows:
        signal = {
            "event_id": row[4],
            "event_version": row[5],
            "event_mapping_id": row[6],
        }
        event_id, event_version, event_mapping_id = _domain_event_reference(
            conn, signal, allow_missing=True
        )
        if not event_id or not event_version:
            continue
        conn.execute(
            "UPDATE integration_signal_outbox SET "
            "domain_event_id = CASE WHEN COALESCE(domain_event_id, '') = '' "
            "THEN ? ELSE domain_event_id END, "
            "event_version = CASE WHEN event_version IS NULL OR event_version = 0 "
            "THEN ? ELSE event_version END, "
            "event_mapping_id = CASE "
            "WHEN COALESCE(event_mapping_id, '') = '' AND ? IS NOT NULL "
            "THEN ? ELSE event_mapping_id END WHERE seq = ?",
            (
                event_id,
                event_version,
                event_mapping_id,
                event_mapping_id,
                int(row[0]),
            ),
        )

    return count_unresolved_outbox_event_references(conn)


def count_unresolved_outbox_event_references(conn) -> int:
    """Read-only count of retained rows that cannot satisfy the v3 contract."""
    if not _table_exists(conn, "integration_signal_outbox"):
        return 0
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
    }
    if not {"domain_event_id", "event_version", "event_mapping_id"}.issubset(columns):
        return int(
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
        )
    if not _table_exists(conn, "events") or not _table_exists(conn, "event_securities"):
        return int(
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
        )
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM integration_signal_outbox AS o "
        "LEFT JOIN events AS e ON e.event_id = o.domain_event_id "
        "LEFT JOIN event_securities AS mapping "
        "ON mapping.mapping_id = o.event_mapping_id "
        "AND mapping.event_id = o.domain_event_id "
        "WHERE e.event_id IS NULL "
        "OR typeof(o.event_version) <> 'integer' "
        "OR o.event_version < 1 OR o.event_version > e.version "
        "OR (COALESCE(o.event_mapping_id, '') <> '' "
        "AND mapping.mapping_id IS NULL)"
    ).fetchone()
    return int(unresolved[0] or 0)


def _finite(value: object, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integration outbox {name}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"invalid integration outbox {name}")
    return parsed


def _bounded_number(value: object, name: str, minimum: float, maximum: float) -> float:
    parsed = _finite(value, name)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"invalid integration outbox {name}")
    return parsed


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def configured_max_rows() -> int:
    return _bounded_env_int(
        "AI_FILTER_INTEGRATION_OUTBOX_MAX_ROWS",
        DEFAULT_MAX_ROWS,
        MIN_MAX_ROWS,
        MAX_MAX_ROWS,
    )


def configured_retention_days() -> int:
    return _bounded_env_int(
        "AI_FILTER_INTEGRATION_OUTBOX_RETENTION_DAYS",
        DEFAULT_RETENTION_DAYS,
        7,
        365,
    )


def init_integration_signal_outbox(
    conn,
    *,
    now: int | None = None,
    backfill: bool = True,
    trim: bool = True,
) -> None:
    """Create stream metadata and optionally backfill/trim retained revisions.

    Offline migrations pass ``backfill=False, trim=False`` so additive schema
    repair cannot silently change the consumer's retained replay window.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS integration_signal_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ledger_signal_id INTEGER NOT NULL UNIQUE,
            event_id TEXT NOT NULL UNIQUE,
            domain_event_id TEXT,
            event_version INTEGER,
            event_mapping_id TEXT,
            mapping_confidence REAL NOT NULL DEFAULT 0
                CHECK (typeof(mapping_confidence) IN ('integer', 'real') AND mapping_confidence >= 0 AND mapping_confidence <= 1),
            mapping_relevance REAL NOT NULL DEFAULT 0
                CHECK (typeof(mapping_relevance) IN ('integer', 'real') AND mapping_relevance >= 0 AND mapping_relevance <= 10),
            expected_gap REAL
                CHECK (expected_gap IS NULL OR (typeof(expected_gap) IN ('integer', 'real') AND expected_gap >= -100 AND expected_gap <= 100)),
            available_at INTEGER NOT NULL,
            recorded_at INTEGER NOT NULL,
            enqueued_at INTEGER NOT NULL,
            score REAL NOT NULL,
            direction TEXT NOT NULL,
            score_confidence REAL NOT NULL,
            strategy_version TEXT NOT NULL,
            mapping_version TEXT NOT NULL,
            mapped_theme TEXT NOT NULL,
            backtest_symbol TEXT,
            score_path TEXT NOT NULL,
            revision_key TEXT NOT NULL,
            signal_stage TEXT NOT NULL,
            revision_seq INTEGER NOT NULL,
            revision_state_key TEXT NOT NULL,
            independence_id TEXT NOT NULL,
            aggregation_threshold REAL NOT NULL,
            selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
            is_similarity_copy INTEGER NOT NULL CHECK (is_similarity_copy IN (0, 1))
        )
    """)
    # Install and freeze the additive v4 fields before creating indexes that
    # depend on other additive export columns.
    backfill_outbox_mapping_dimensions(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_integration_outbox_enqueued "
        "ON integration_signal_outbox(enqueued_at, seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_integration_outbox_domain_event "
        "ON integration_signal_outbox(domain_event_id, event_version, seq)"
    )
    _meta_set_default(conn, LEDGER_ID_META, secrets.token_hex(16))
    _meta_set_default(conn, EPOCH_META, secrets.token_hex(16))
    _meta_set_default(conn, FLOOR_META, 1)
    _meta_set_default(conn, LAST_META, 0)

    if backfill and _meta_get(conn, BACKFILL_META) != "1":
        current = int(now if now is not None else time.time())
        cutoff = current - configured_retention_days() * 86400
        exportable_paths = tuple(sorted(
            RECOGNIZED_SCORE_PATHS - NON_EXPORTABLE_SCORE_PATHS
        ))
        exportable_placeholders = ",".join("?" for _ in exportable_paths)
        rows = conn.execute(
            "SELECT id, signal_key, event_key, link, available_at, recorded_at, "
            "score, direction, score_confidence, strategy_version, mapping_version, "
            "mapped_theme, backtest_symbol, mapping_confidence, mapping_relevance, "
            "expected_gap, score_path, revision_key, signal_stage, "
            "revision_seq, revision_state_key, independence_key, aggregation_threshold, "
            "selected, is_similarity_copy, event_id, event_version, event_mapping_id "
            "FROM backtest_signals "
            "WHERE recorded_at >= ? "
            "AND LOWER(TRIM(COALESCE(score_path, ''))) IN ("
            f"{exportable_placeholders}) "
            "AND is_similarity_copy = 0 "
            "ORDER BY id DESC LIMIT ?",
            (
                cutoff,
                *exportable_paths,
                BACKFILL_MAX_ROWS,
            ),
        ).fetchall()
        for raw_row in reversed(rows):
            row = dict(raw_row)
            enqueue_signal_revision(
                conn,
                ledger_signal_id=int(row.pop("id")),
                signal=row,
                signal_revision_key=str(row.pop("signal_key")),
                enqueued_at=int(row.get("recorded_at") or current),
                ignore_conflict=True,
                enforce_bound=False,
                _allow_legacy_missing_identity=True,
            )
        _meta_set(conn, BACKFILL_META, 1)

    backfill_outbox_event_references(conn)

    actual = conn.execute(
        "SELECT MIN(seq), MAX(seq) FROM integration_signal_outbox"
    ).fetchone()
    minimum = int(actual[0]) if actual and actual[0] is not None else None
    maximum = int(actual[1]) if actual and actual[1] is not None else 0
    stored_last = int(_meta_get(conn, LAST_META) or 0)
    last = max(stored_last, maximum)
    _meta_set(conn, LAST_META, last)
    if minimum is not None:
        _meta_set(conn, FLOOR_META, minimum)
    elif int(_meta_get(conn, FLOOR_META) or 1) <= last:
        _meta_set(conn, FLOOR_META, last + 1)
    if trim:
        trim_integration_signal_outbox(
            conn,
            now=now,
            retention_days=configured_retention_days(),
            max_rows=configured_max_rows(),
        )


def enqueue_signal_revision(
    conn,
    *,
    ledger_signal_id: int,
    signal: dict[str, Any],
    signal_revision_key: str,
    enqueued_at: int | None = None,
    ignore_conflict: bool = False,
    enforce_bound: bool = True,
    _allow_legacy_missing_identity: bool = False,
) -> bool:
    """Append one ledger-bound sanitized revision in the caller's transaction."""
    ledger_row = conn.execute(
        "SELECT " + ", ".join(_LEDGER_EXPORT_FIELDS) + " "
        "FROM backtest_signals WHERE id = ?",
        (int(ledger_signal_id),),
    ).fetchone()
    if ledger_row is None:
        raise ValueError("integration outbox signal ledger row is missing")
    ledger = dict(ledger_row)

    def mismatch(field: str) -> ValueError:
        return ValueError(
            f"integration outbox signal {field} does not match ledger"
        )

    if str(signal_revision_key or "").strip() != str(ledger["signal_key"] or ""):
        raise mismatch("signal_key")

    text_fields = (
        "event_key",
        "link",
        "direction",
        "strategy_version",
        "mapping_version",
        "mapped_theme",
        "backtest_symbol",
        "revision_key",
        "signal_stage",
        "revision_state_key",
        "independence_key",
        "event_id",
        "event_mapping_id",
    )
    for field in text_fields:
        if str(signal.get(field) or "") != str(ledger[field] or ""):
            raise mismatch(field)
    if normalize_score_path(signal.get("score_path")) != normalize_score_path(
        ledger["score_path"]
    ):
        raise mismatch("score_path")

    integer_fields = (
        "available_at",
        "recorded_at",
        "revision_seq",
        "event_version",
        "selected",
        "is_similarity_copy",
    )
    for field in integer_fields:
        try:
            matches = int(signal.get(field) or 0) == int(ledger[field] or 0)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise mismatch(field)

    number_fields = (
        "score",
        "score_confidence",
        "mapping_confidence",
        "mapping_relevance",
        "aggregation_threshold",
    )
    for field in number_fields:
        try:
            supplied = float(signal.get(field))
            persisted = float(ledger[field])
            matches = (
                math.isfinite(supplied)
                and math.isfinite(persisted)
                and supplied == persisted
            )
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise mismatch(field)

    supplied_gap = signal.get("expected_gap")
    persisted_gap = ledger["expected_gap"]
    if supplied_gap is None or persisted_gap is None:
        gap_matches = supplied_gap is None and persisted_gap is None
    else:
        try:
            supplied_gap_number = float(supplied_gap)
            persisted_gap_number = float(persisted_gap)
            gap_matches = (
                math.isfinite(supplied_gap_number)
                and math.isfinite(persisted_gap_number)
                and supplied_gap_number == persisted_gap_number
            )
        except (TypeError, ValueError):
            gap_matches = False
    if not gap_matches:
        raise mismatch("expected_gap")

    ledger_score_path = normalize_score_path(ledger["score_path"])
    if (
        not is_exportable_score_path(ledger_score_path)
        or bool(ledger["is_similarity_copy"])
    ):
        return False
    signal_revision_id = stable_signal_revision_id(ledger["signal_key"])
    if not signal_revision_id:
        raise ValueError("integration outbox signal revision identity is required")
    domain_event_id, event_version, event_mapping_id = _domain_event_reference(
        conn,
        ledger,
        allow_missing=_allow_legacy_missing_identity,
    )
    available_at = int(ledger.get("available_at") or 0)
    recorded_at = int(ledger.get("recorded_at") or 0)
    queued_at = int(enqueued_at if enqueued_at is not None else recorded_at)
    revision_seq = int(ledger.get("revision_seq") or 0)
    if min(available_at, recorded_at, queued_at) <= 0 or revision_seq < 0:
        raise ValueError("invalid integration outbox timestamps or revision")
    raw_direction = bounded_text(ledger.get("direction"), FIELD_BYTES["direction"])
    direction = raw_direction if raw_direction in {"看多", "看空", "中性"} else "中性"
    mapping_confidence = _bounded_number(
        ledger["mapping_confidence"], "mapping_confidence", 0.0, 1.0
    )
    mapping_relevance = _bounded_number(
        ledger["mapping_relevance"], "mapping_relevance", 0.0, 10.0
    )
    raw_expected_gap = ledger["expected_gap"]
    expected_gap = (
        None
        if raw_expected_gap is None
        else _bounded_number(raw_expected_gap, "expected_gap", -100.0, 100.0)
    )
    values = (
        int(ledger_signal_id),
        signal_revision_id,
        domain_event_id,
        event_version,
        event_mapping_id,
        mapping_confidence,
        mapping_relevance,
        expected_gap,
        available_at,
        recorded_at,
        queued_at,
        # -1 is the established parse/model-error sentinel in the immutable
        # signal ledger; retaining it must never make the primary write fail.
        _bounded_number(ledger.get("score"), "score", -1.0, 10.0),
        direction,
        _bounded_number(
            ledger.get("score_confidence") or 0.0,
            "score_confidence",
            0.0,
            1.0,
        ),
        bounded_text(ledger.get("strategy_version"), FIELD_BYTES["strategy_version"]),
        bounded_text(ledger.get("mapping_version"), FIELD_BYTES["mapping_version"]),
        bounded_text(ledger.get("mapped_theme"), FIELD_BYTES["mapped_theme"]),
        bounded_text(ledger.get("backtest_symbol"), FIELD_BYTES["backtest_symbol"]) or None,
        bounded_text(ledger_score_path, FIELD_BYTES["score_path"]),
        bounded_text(ledger.get("revision_key") or "base", FIELD_BYTES["revision_key"]),
        bounded_text(ledger.get("signal_stage") or "base", FIELD_BYTES["signal_stage"]),
        revision_seq,
        bounded_text(
            ledger.get("revision_state_key"), FIELD_BYTES["revision_state_key"]
        ),
        stable_text_hash(ledger.get("independence_key")),
        _bounded_number(
            ledger.get("aggregation_threshold"),
            "aggregation_threshold",
            0.0,
            10.0,
        ),
        1 if bool(ledger.get("selected")) else 0,
        1 if bool(ledger.get("is_similarity_copy")) else 0,
    )
    if ignore_conflict:
        existing = conn.execute(
            "SELECT 1 FROM integration_signal_outbox "
            "WHERE ledger_signal_id = ? OR event_id = ? LIMIT 1",
            (int(ledger_signal_id), signal_revision_id),
        ).fetchone()
        if existing:
            return False
    cursor = conn.execute(
        """
        INSERT INTO integration_signal_outbox (
            ledger_signal_id, event_id, domain_event_id, event_version,
            event_mapping_id, mapping_confidence, mapping_relevance, expected_gap,
            available_at, recorded_at, enqueued_at, score,
            direction, score_confidence, strategy_version, mapping_version,
            mapped_theme, backtest_symbol, score_path, revision_key, signal_stage,
            revision_seq, revision_state_key, independence_id,
            aggregation_threshold, selected, is_similarity_copy
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    if cursor.rowcount <= 0:
        return False
    seq = int(cursor.lastrowid)
    _meta_set(conn, LAST_META, seq)
    if not _meta_get(conn, FLOOR_META):
        _meta_set(conn, FLOOR_META, seq)
    if enforce_bound:
        trim_integration_signal_outbox(
            conn,
            now=queued_at,
            retention_days=None,
            max_rows=configured_max_rows(),
        )
    return True


def get_integration_outbox_state(conn) -> dict[str, Any]:
    ledger_id = _meta_get(conn, LEDGER_ID_META)
    epoch = _meta_get(conn, EPOCH_META)
    try:
        floor_seq = int(_meta_get(conn, FLOOR_META))
        last_seq = int(_meta_get(conn, LAST_META))
    except ValueError as exc:
        raise RuntimeError("integration outbox metadata is invalid") from exc
    if (
        not _HEX_ID_RE.fullmatch(ledger_id)
        or not _HEX_ID_RE.fullmatch(epoch)
        or floor_seq < 1
        or last_seq < 0
        or floor_seq > last_seq + 1
    ):
        raise RuntimeError("integration outbox metadata is invalid")
    actual = conn.execute(
        "SELECT MIN(seq), MAX(seq), COUNT(*) FROM integration_signal_outbox"
    ).fetchone()
    minimum = int(actual[0]) if actual and actual[0] is not None else None
    maximum = int(actual[1]) if actual and actual[1] is not None else None
    count = int(actual[2] or 0) if actual else 0
    expected_count = max(0, last_seq - floor_seq + 1)
    if count != expected_count:
        raise RuntimeError("integration outbox sequence has a retained gap")
    if count:
        if minimum != floor_seq or maximum != last_seq:
            raise RuntimeError("integration outbox bounds do not match metadata")
    elif floor_seq != last_seq + 1:
        raise RuntimeError("integration outbox empty bounds are invalid")
    return {
        "ledger_id": ledger_id,
        "epoch": epoch,
        "floor_seq": floor_seq,
        "last_seq": last_seq,
    }


def rotate_integration_outbox_epoch(conn, *, clear: bool = False) -> str:
    """Invalidate every previously issued cursor after a restore/rebuild.

    A database restore can move the durable sequence backwards while also
    restoring an older epoch.  The restore runbook must invoke this helper in
    the same exclusive maintenance transaction before reopening the API.  A
    destructive rebuild may additionally clear retained rows; sequence reuse
    is safe because consumers are forced to bootstrap under the new epoch.
    """
    state = get_integration_outbox_state(conn)
    if clear:
        conn.execute("DELETE FROM integration_signal_outbox")
        _meta_set(conn, FLOOR_META, int(state["last_seq"]) + 1)
    new_epoch = secrets.token_hex(16)
    _meta_set(conn, EPOCH_META, new_epoch)
    return new_epoch


def trim_integration_signal_outbox(
    conn,
    *,
    now: int | None = None,
    retention_days: int | None = DEFAULT_RETENTION_DAYS,
    max_rows: int | None = DEFAULT_MAX_ROWS,
) -> int:
    """Delete only one continuous prefix and advance the durable floor."""
    state = get_integration_outbox_state(conn)
    floor = int(state["floor_seq"])
    last = int(state["last_seq"])
    delete_through = floor - 1
    if retention_days is not None:
        days = max(7, min(365, int(retention_days)))
        cutoff = int(now if now is not None else time.time()) - days * 86400
        # Delete only the *continuous* expired prefix.  enqueued_at normally
        # follows seq, but wall-clock rollback can make it non-monotonic.  A
        # MAX(seq) over all old rows would then jump across a newer retained
        # row and silently expire a cursor that should remain replayable.
        row = conn.execute(
            "SELECT MIN(seq) FROM integration_signal_outbox WHERE enqueued_at >= ?",
            (cutoff,),
        ).fetchone()
        first_retained = int(row[0]) if row and row[0] is not None else last + 1
        delete_through = max(delete_through, first_retained - 1)
    if max_rows is not None:
        bounded = max(MIN_MAX_ROWS, min(MAX_MAX_ROWS, int(max_rows)))
        if last - floor + 1 > bounded:
            delete_through = max(delete_through, last - bounded)
    if delete_through < floor:
        return 0
    cursor = conn.execute(
        "DELETE FROM integration_signal_outbox WHERE seq <= ?",
        (delete_through,),
    )
    _meta_set(conn, FLOOR_META, delete_through + 1)
    return max(0, int(cursor.rowcount))
