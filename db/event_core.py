"""Stable, relational event core for intelligence-to-asset workflows.

The legacy radar tables use a headline-derived identifier and are deliberately
not reused here.  Events created by this module receive an opaque UUID once;
external identities are stored separately and may later resolve through an
audited merge chain.

All mutating functions are atomic.  They commit when they own the transaction
and use a savepoint when called inside a caller-managed transaction.  No API
silently resolves identity conflicts: ambiguous identities, identifier reuse,
cycles, and cross-record upsert conflicts raise an exception and roll back the
whole operation.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

EVENT_MERGE_CONTRACT = "event-merge-snapshot-publisher-v2"
EVENT_CORE_SCHEMA_META_KEY = "event_core_schema_version"
EVENT_CORE_SCHEMA_VERSION = 2
EVENT_CORE_BACKFILL_META_KEY = "event_core_backfill_v2"


class EventCoreError(RuntimeError):
    """Base error for event-core invariants."""


class EventCoreConflictError(EventCoreError):
    """Raised when supplied identities disagree with persisted state."""


class EventCoreSchemaError(EventCoreError):
    """Raised when an existing table is incompatible with this schema."""


_SAVEPOINTS = itertools.count()
_UUID_SQL_CHECK = (
    "length({column}) = 36 "
    "AND substr({column}, 9, 1) = '-' "
    "AND substr({column}, 14, 1) = '-' "
    "AND substr({column}, 19, 1) = '-' "
    "AND substr({column}, 24, 1) = '-'"
)
_EXPECTED_GAP_SQL_CHECK = (
    "{column} IS NULL OR ("
    "typeof({column}) IN ('integer', 'real') "
    "AND {column} >= -100 AND {column} <= 100)"
)
_FROZEN_OUTCOME_METHOD_VERSION = "a-share-event-ohlc-v5-frozen-mapping"
_FROZEN_OUTCOME_DATA_VERSION_PREFIX = (
    "future-prices-event-ohlc-v5-frozen-mapping:"
)


def _require_event_mapping_connection_write_authorized(
    conn: sqlite3.Connection,
    mapping_version: str,
) -> None:
    """Load the write fence only for mapping mutations.

    The restore-drill bundle imports this module solely for read-only schema
    verification.  Keeping the gate import at the mutation boundary preserves
    that isolated verifier layout; an unavailable gate module still fails
    before any mapping write.
    """
    from db.event_mapping_gate import (
        require_event_mapping_connection_write_authorized,
    )

    require_event_mapping_connection_write_authorized(conn, mapping_version)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Run one operation atomically without committing a caller transaction."""
    if conn.in_transaction:
        name = f"event_core_{next(_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _enable_foreign_keys(conn: sqlite3.Connection) -> None:
    enabled = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if enabled:
        return
    if conn.in_transaction:
        raise EventCoreSchemaError(
            "foreign keys are disabled and cannot be enabled inside a transaction"
        )
    conn.execute("PRAGMA foreign_keys = ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise EventCoreSchemaError("SQLite foreign-key enforcement is required")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def normalize_evidence_text(value: object) -> str:
    """The one canonical form for stored evidence text: NFC, then strip.

    Every producer that hashes text which is later verified by
    ``_snapshot_values`` must hash exactly this form, otherwise the stored
    digest cannot match (e.g. CJK compatibility ideographs such as U+F90A).
    """
    if value is None:
        value = ""
    return unicodedata.normalize("NFC", str(value)).strip()


def snapshot_digest(text: object) -> str:
    """SHA-256 of the canonical evidence text, as verified on write."""
    return hashlib.sha256(normalize_evidence_text(text).encode("utf-8")).hexdigest()


def _text(
    value: object,
    name: str,
    *,
    required: bool = False,
    max_length: int = 1_000,
) -> str:
    normalized = normalize_evidence_text(value)
    if required and not normalized:
        raise ValueError(f"{name} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    if "\x00" in normalized:
        raise ValueError(f"{name} contains a NUL character")
    return normalized


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _real(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _optional_real(
    value: object | None,
    name: str,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    return _real(value, name, minimum, maximum)


def _flag(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if value is True or value == 1:
        return 1
    if value is False or value == 0:
        return 0
    raise ValueError(f"{name} must be boolean")


def _timestamp(value: object | None, name: str, *, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError(f"{name} is required")
        return default
    return _integer(value, name)


def _now(value: int | None) -> int:
    return _timestamp(value, "now", default=int(time.time()))


def _normalize_identity_keys(
    identity_keys: Mapping[object, object]
    | Iterable[tuple[object, object] | str]
    | str,
) -> tuple[tuple[str, str], ...]:
    if isinstance(identity_keys, Mapping):
        raw: Iterable[Any] = identity_keys.items()
    elif isinstance(identity_keys, str):
        raw = (identity_keys,)
    else:
        raw = identity_keys

    normalized: set[tuple[str, str]] = set()
    try:
        for item in raw:
            if isinstance(item, str):
                identity_type, identity_key = "external", item
            else:
                identity_type, identity_key = item
            normalized.add(
                (
                    _text(identity_type, "identity type", required=True, max_length=64),
                    _text(identity_key, "identity key", required=True, max_length=512),
                )
            )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("identity "):
            raise
        raise ValueError(
            "identity_keys must be a mapping, strings, or (type, key) pairs"
        ) from exc
    if not normalized:
        raise ValueError("at least one identity key is required")
    if len(normalized) > 64:
        raise ValueError("at most 64 identity keys are allowed")
    return tuple(sorted(normalized))


def _string_list_json(value: Iterable[object] | str | None, name: str) -> str:
    """Return deterministic JSON for a bounded set of human-readable labels."""
    if value is None:
        items: Iterable[object] = ()
    elif isinstance(value, str):
        items = (value,)
    else:
        items = value
    normalized = sorted(
        {
            _text(item, name, required=True, max_length=255)
            for item in items
        }
    )
    if len(normalized) > 128:
        raise ValueError(f"{name} allows at most 128 values")
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 16_000:
        raise ValueError(f"{name} JSON is too large")
    return encoded


def _json_array(value: object | None, name: str, *, max_length: int = 32_000) -> str:
    """Canonicalize bounded JSON arrays without accepting NaN or infinities."""
    if value is None:
        value = []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} must be a JSON array") from exc
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a JSON array")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON-compatible finite values") from exc
    if len(encoded) > max_length:
        raise ValueError(f"{name} JSON is too large")
    return encoded


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _snapshot_values(snapshot_hash: str | None, snapshot_text: str | None) -> tuple[str, str]:
    text_value = _text(snapshot_text, "snapshot_text", max_length=32_000)
    hash_value = _text(snapshot_hash, "snapshot_hash", max_length=64).lower()
    if hash_value and not _SHA256_RE.fullmatch(hash_value):
        raise ValueError("snapshot_hash must be a lowercase SHA-256 digest")
    if text_value:
        computed = snapshot_digest(text_value)
        if hash_value and hash_value != computed:
            raise EventCoreConflictError("snapshot_hash does not match snapshot_text")
        hash_value = computed
    return hash_value, text_value


_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY
            CHECK ({_UUID_SQL_CHECK.format(column='event_id')}),
        title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 1000),
        event_type TEXT NOT NULL CHECK (length(trim(event_type)) BETWEEN 1 AND 64),
        direction TEXT NOT NULL CHECK (length(trim(direction)) BETWEEN 1 AND 64),
        priority REAL NOT NULL
            CHECK (typeof(priority) IN ('integer', 'real') AND priority >= 0 AND priority <= 10),
        occurred_at INTEGER NOT NULL
            CHECK (typeof(occurred_at) = 'integer' AND occurred_at >= 0),
        version INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(version) = 'integer' AND version >= 1),
        merge_state TEXT NOT NULL DEFAULT 'active'
            CHECK (merge_state IN ('active', 'merged')),
        merged_into TEXT REFERENCES events(event_id) ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED,
        strategy_version TEXT NOT NULL DEFAULT '' CHECK (length(strategy_version) <= 255),
        model_version TEXT NOT NULL DEFAULT '' CHECK (length(model_version) <= 255),
        merge_contract TEXT NOT NULL DEFAULT ''
            CHECK (length(merge_contract) <= 64),
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        updated_at INTEGER NOT NULL
            CHECK (typeof(updated_at) = 'integer' AND updated_at >= created_at),
        first_seen_at INTEGER NOT NULL
            CHECK (typeof(first_seen_at) = 'integer' AND first_seen_at >= 0),
        last_seen_at INTEGER NOT NULL
            CHECK (typeof(last_seen_at) = 'integer' AND last_seen_at >= first_seen_at),
        available_at INTEGER NOT NULL
            CHECK (
                typeof(available_at) = 'integer' AND
                available_at >= first_seen_at AND available_at <= last_seen_at
            ),
        CHECK (
            (merge_state = 'active' AND merged_into IS NULL) OR
            (merge_state = 'merged' AND merged_into IS NOT NULL AND merged_into <> event_id)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_identities (
        identity_type TEXT NOT NULL CHECK (length(trim(identity_type)) BETWEEN 1 AND 64),
        identity_key TEXT NOT NULL CHECK (length(trim(identity_key)) BETWEEN 1 AND 512),
        event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        PRIMARY KEY (identity_type, identity_key)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS securities (
        security_id TEXT PRIMARY KEY CHECK (length(trim(security_id)) BETWEEN 1 AND 128),
        exchange TEXT NOT NULL CHECK (length(trim(exchange)) BETWEEN 1 AND 32),
        ticker TEXT NOT NULL CHECK (length(trim(ticker)) BETWEEN 1 AND 32),
        name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 255),
        asset_type TEXT NOT NULL DEFAULT 'equity'
            CHECK (length(trim(asset_type)) BETWEEN 1 AND 64),
        currency TEXT NOT NULL DEFAULT 'CNY'
            CHECK (length(trim(currency)) BETWEEN 1 AND 16),
        aliases TEXT NOT NULL DEFAULT '[]' CHECK (length(aliases) <= 16000),
        industry TEXT NOT NULL DEFAULT '' CHECK (length(industry) <= 255),
        concepts TEXT NOT NULL DEFAULT '[]' CHECK (length(concepts) <= 16000),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        version INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(version) = 'integer' AND version >= 1),
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        updated_at INTEGER NOT NULL
            CHECK (typeof(updated_at) = 'integer' AND updated_at >= created_at),
        UNIQUE (exchange, ticker)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS event_securities (
        mapping_id TEXT PRIMARY KEY
            CHECK ({_UUID_SQL_CHECK.format(column='mapping_id')}),
        event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        security_id TEXT NOT NULL REFERENCES securities(security_id)
            ON DELETE RESTRICT,
        relation_type TEXT NOT NULL DEFAULT 'affected'
            CHECK (length(trim(relation_type)) BETWEEN 1 AND 64),
        direction TEXT NOT NULL DEFAULT 'neutral'
            CHECK (length(trim(direction)) BETWEEN 1 AND 64),
        expected_gap REAL
            CHECK ({_EXPECTED_GAP_SQL_CHECK.format(column='expected_gap')}),
        confidence REAL NOT NULL DEFAULT 0
            CHECK (typeof(confidence) IN ('integer', 'real') AND confidence >= 0 AND confidence <= 1),
        relevance REAL NOT NULL DEFAULT 0
            CHECK (typeof(relevance) IN ('integer', 'real') AND relevance >= 0 AND relevance <= 10),
        rationale TEXT NOT NULL DEFAULT '' CHECK (length(rationale) <= 4000),
        mapping_version TEXT NOT NULL DEFAULT '' CHECK (length(mapping_version) <= 255),
        revision INTEGER NOT NULL
            CHECK (typeof(revision) = 'integer' AND revision >= 1),
        is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        UNIQUE (event_id, security_id, relation_type, revision),
        UNIQUE (mapping_id, event_id, security_id, revision)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS event_evidence (
        evidence_id TEXT PRIMARY KEY
            CHECK ({_UUID_SQL_CHECK.format(column='evidence_id')}),
        event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        security_id TEXT REFERENCES securities(security_id)
            ON DELETE RESTRICT,
        mapping_id TEXT,
        mapping_revision INTEGER CHECK (
            mapping_revision IS NULL OR
            (typeof(mapping_revision) = 'integer' AND mapping_revision >= 1)
        ),
        source_uri TEXT NOT NULL CHECK (length(trim(source_uri)) BETWEEN 1 AND 2048),
        source_type TEXT NOT NULL DEFAULT 'article'
            CHECK (length(trim(source_type)) BETWEEN 1 AND 64),
        source_name TEXT NOT NULL DEFAULT '' CHECK (length(source_name) <= 255),
        claim_key TEXT NOT NULL CHECK (length(trim(claim_key)) BETWEEN 1 AND 512),
        claim_text TEXT NOT NULL CHECK (length(trim(claim_text)) BETWEEN 1 AND 4000),
        excerpt TEXT NOT NULL DEFAULT '' CHECK (length(excerpt) <= 8000),
        snapshot_hash TEXT NOT NULL DEFAULT '' CHECK (
            snapshot_hash = '' OR
            (length(snapshot_hash) = 64 AND snapshot_hash NOT GLOB '*[^0-9a-f]*')
        ),
        snapshot_text TEXT NOT NULL DEFAULT '' CHECK (length(snapshot_text) <= 32000),
        stance TEXT NOT NULL DEFAULT 'context'
            CHECK (stance IN ('supports', 'contradicts', 'context')),
        reliability REAL NOT NULL DEFAULT 0.5
            CHECK (typeof(reliability) IN ('integer', 'real') AND reliability >= 0 AND reliability <= 1),
        announcement_at INTEGER CHECK (
            announcement_at IS NULL OR (typeof(announcement_at) = 'integer' AND announcement_at >= 0)
        ),
        observed_at INTEGER NOT NULL
            CHECK (typeof(observed_at) = 'integer' AND observed_at >= 0),
        version INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(version) = 'integer' AND version >= 1),
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        updated_at INTEGER NOT NULL
            CHECK (typeof(updated_at) = 'integer' AND updated_at >= created_at),
        CHECK (
            (mapping_id IS NULL AND mapping_revision IS NULL) OR
            (mapping_id IS NOT NULL AND mapping_revision IS NOT NULL AND security_id IS NOT NULL)
        ),
        UNIQUE (event_id, source_uri, claim_key),
        FOREIGN KEY (mapping_id, event_id, security_id, mapping_revision)
            REFERENCES event_securities(mapping_id, event_id, security_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_mapping_replay_ledger (
        evidence_id TEXT NOT NULL REFERENCES event_evidence(evidence_id)
            ON DELETE RESTRICT,
        evidence_version INTEGER NOT NULL
            CHECK (typeof(evidence_version) = 'integer' AND evidence_version >= 1),
        root_event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        root_event_version INTEGER NOT NULL
            CHECK (typeof(root_event_version) = 'integer' AND root_event_version >= 1),
        mapping_version TEXT NOT NULL
            CHECK (length(trim(mapping_version)) BETWEEN 1 AND 255),
        input_fingerprint TEXT NOT NULL
            CHECK (
                length(input_fingerprint) = 64 AND
                input_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        output_fingerprint TEXT NOT NULL
            CHECK (
                length(output_fingerprint) = 64 AND
                output_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        status TEXT NOT NULL
            CHECK (status IN ('mapped', 'no_match', 'failed')),
        mapped_count INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(mapped_count) = 'integer' AND mapped_count >= 0),
        attempts INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(attempts) = 'integer' AND attempts >= 1),
        failure_code TEXT NOT NULL DEFAULT ''
            CHECK (length(failure_code) <= 64),
        processed_at INTEGER NOT NULL
            CHECK (typeof(processed_at) = 'integer' AND processed_at >= 0),
        updated_at INTEGER NOT NULL
            CHECK (typeof(updated_at) = 'integer' AND updated_at >= processed_at),
        CHECK (
            (status = 'mapped' AND mapped_count > 0 AND failure_code = '') OR
            (status = 'no_match' AND mapped_count = 0 AND failure_code = '') OR
            (status = 'failed' AND mapped_count = 0 AND length(trim(failure_code)) > 0)
        ),
        PRIMARY KEY (evidence_id, mapping_version, input_fingerprint)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS event_mapping_replay_dirty_roots (
        root_event_id TEXT PRIMARY KEY REFERENCES events(event_id)
            ON DELETE RESTRICT,
        reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 64),
        dirty_at INTEGER NOT NULL
            CHECK (typeof(dirty_at) = 'integer' AND dirty_at >= 0)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS event_similarity_replay_dirty_roots (
        root_event_id TEXT PRIMARY KEY REFERENCES events(event_id)
            ON DELETE RESTRICT,
        generation INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(generation) = 'integer' AND generation >= 1),
        dirty_at INTEGER NOT NULL
            CHECK (typeof(dirty_at) = 'integer' AND dirty_at >= 0)
    ) WITHOUT ROWID
    """,
    f"""
    CREATE TABLE IF NOT EXISTS event_outcomes (
        outcome_revision_id TEXT PRIMARY KEY
            CHECK ({_UUID_SQL_CHECK.format(column='outcome_revision_id')}),
        outcome_id TEXT NOT NULL
            CHECK ({_UUID_SQL_CHECK.format(column='outcome_id')}),
        event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        security_id TEXT NOT NULL REFERENCES securities(security_id)
            ON DELETE RESTRICT,
        mapping_id TEXT NOT NULL,
        mapping_revision INTEGER NOT NULL
            CHECK (typeof(mapping_revision) = 'integer' AND mapping_revision >= 1),
        signal_ledger_id INTEGER CHECK (
            signal_ledger_id IS NULL OR
            (typeof(signal_ledger_id) = 'integer' AND signal_ledger_id >= 1)
        ),
        signal_available_at INTEGER CHECK (
            signal_available_at IS NULL OR
            (typeof(signal_available_at) = 'integer' AND signal_available_at > 0)
        ),
        observation_state TEXT NOT NULL DEFAULT 'legacy_unknown'
            CHECK (observation_state IN (
                'legacy_unknown', 'not_due', 'missing_asset_close',
                'missing_benchmark_close', 'complete'
            )),
        horizon TEXT NOT NULL CHECK (length(trim(horizon)) BETWEEN 1 AND 64),
        baseline_at INTEGER NOT NULL
            CHECK (typeof(baseline_at) = 'integer' AND baseline_at >= 0),
        measured_at INTEGER NOT NULL
            CHECK (typeof(measured_at) = 'integer' AND measured_at >= baseline_at),
        baseline_price REAL CHECK (
            baseline_price IS NULL OR (
                typeof(baseline_price) IN ('integer', 'real') AND
                baseline_price > 0 AND baseline_price <= 1000000000000
            )
        ),
        gross_return REAL CHECK (
            gross_return IS NULL OR (
                typeof(gross_return) IN ('integer', 'real') AND
                gross_return >= -1 AND gross_return <= 1000000
            )
        ),
        benchmark_return REAL CHECK (
            benchmark_return IS NULL OR (
                typeof(benchmark_return) IN ('integer', 'real') AND
                benchmark_return >= -1000000 AND benchmark_return <= 1000000
            )
        ),
        excess_return REAL CHECK (
            excess_return IS NULL OR (
                typeof(excess_return) IN ('integer', 'real') AND
                excess_return >= -1000000 AND excess_return <= 1000000
            )
        ),
        mfe REAL CHECK (
            mfe IS NULL OR (
                typeof(mfe) IN ('integer', 'real') AND mfe >= 0 AND mfe <= 1000000
            )
        ),
        mae REAL CHECK (
            mae IS NULL OR (
                typeof(mae) IN ('integer', 'real') AND mae >= -1 AND mae <= 0
            )
        ),
        direction_hit INTEGER CHECK (direction_hit IS NULL OR direction_hit IN (0, 1)),
        benchmark_security_id TEXT REFERENCES securities(security_id)
            ON DELETE RESTRICT,
        method_version TEXT NOT NULL CHECK (length(trim(method_version)) BETWEEN 1 AND 255),
        data_version TEXT NOT NULL CHECK (length(trim(data_version)) BETWEEN 1 AND 255),
        data_quality REAL NOT NULL DEFAULT 1
            CHECK (typeof(data_quality) IN ('integer', 'real') AND data_quality >= 0 AND data_quality <= 1),
        revision INTEGER NOT NULL
            CHECK (typeof(revision) = 'integer' AND revision >= 1),
        is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        UNIQUE (outcome_id, revision),
        FOREIGN KEY (mapping_id, event_id, security_id, mapping_revision)
            REFERENCES event_securities(mapping_id, event_id, security_id, revision)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_similarities (
        event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        similar_event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        method TEXT NOT NULL DEFAULT 'manual'
            CHECK (length(trim(method)) BETWEEN 1 AND 64),
        model_version TEXT NOT NULL DEFAULT '' CHECK (length(model_version) <= 255),
        similarity REAL NOT NULL
            CHECK (typeof(similarity) IN ('integer', 'real') AND similarity >= 0 AND similarity <= 1),
        rationale TEXT NOT NULL DEFAULT '' CHECK (length(rationale) <= 4000),
        common_securities TEXT NOT NULL DEFAULT '[]'
            CHECK (length(common_securities) <= 32000),
        shared_paths TEXT NOT NULL DEFAULT '[]' CHECK (length(shared_paths) <= 32000),
        computed_at INTEGER NOT NULL
            CHECK (typeof(computed_at) = 'integer' AND computed_at >= 0),
        version INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(version) = 'integer' AND version >= 1),
        created_at INTEGER NOT NULL
            CHECK (typeof(created_at) = 'integer' AND created_at >= 0),
        updated_at INTEGER NOT NULL
            CHECK (typeof(updated_at) = 'integer' AND updated_at >= created_at),
        CHECK (event_id < similar_event_id),
        PRIMARY KEY (event_id, similar_event_id, method, model_version)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS event_merge_history (
        merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        target_event_id TEXT NOT NULL REFERENCES events(event_id)
            ON DELETE RESTRICT,
        source_version INTEGER NOT NULL
            CHECK (typeof(source_version) = 'integer' AND source_version >= 2),
        target_version INTEGER NOT NULL
            CHECK (typeof(target_version) = 'integer' AND target_version >= 2),
        reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 2000),
        actor TEXT NOT NULL DEFAULT 'system' CHECK (length(trim(actor)) BETWEEN 1 AND 255),
        merged_at INTEGER NOT NULL
            CHECK (typeof(merged_at) = 'integer' AND merged_at >= 0),
        CHECK (source_event_id <> target_event_id),
        UNIQUE (source_event_id)
    )
    """,
)


_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_events_occurred_priority ON events(occurred_at DESC, priority DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_radar_order ON events(occurred_at DESC, priority DESC, event_id DESC) WHERE merge_state = 'active'",
    "CREATE INDEX IF NOT EXISTS idx_events_merge_target ON events(merged_into) WHERE merged_into IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_event_identities_event ON event_identities(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_securities_name ON securities(name)",
    "CREATE INDEX IF NOT EXISTS idx_securities_industry ON securities(industry)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_event_securities_current ON event_securities(event_id, security_id, relation_type) WHERE is_current = 1",
    "CREATE INDEX IF NOT EXISTS idx_event_securities_security ON event_securities(security_id, is_current, relevance DESC)",
    "CREATE INDEX IF NOT EXISTS idx_event_evidence_event_time ON event_evidence(event_id, announcement_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_event_evidence_source ON event_evidence(source_uri)",
    "CREATE INDEX IF NOT EXISTS idx_event_evidence_mapping ON event_evidence(mapping_id, mapping_revision)",
    "CREATE INDEX IF NOT EXISTS idx_event_evidence_unbound_observed ON event_evidence(observed_at, evidence_id) WHERE mapping_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_event_mapping_replay_lookup ON event_mapping_replay_ledger(mapping_version, evidence_id, evidence_version, root_event_id, root_event_version, status, input_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_event_mapping_replay_contract_input ON event_mapping_replay_ledger(mapping_version, input_fingerprint, status, evidence_id, output_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_event_mapping_replay_contract_root ON event_mapping_replay_ledger(mapping_version, status, root_event_id, processed_at)",
    "CREATE INDEX IF NOT EXISTS idx_event_mapping_replay_dirty_order ON event_mapping_replay_dirty_roots(dirty_at, root_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_similarity_replay_dirty_order ON event_similarity_replay_dirty_roots(dirty_at, root_event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_event_outcomes_series_current ON event_outcomes(signal_ledger_id, mapping_id, horizon, method_version) WHERE is_current = 1 AND signal_ledger_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_event_outcomes_legacy_series_current ON event_outcomes(event_id, security_id, mapping_id, horizon, method_version) WHERE is_current = 1 AND signal_ledger_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_event_outcomes_series_revision ON event_outcomes(signal_ledger_id, mapping_id, horizon, method_version, revision) WHERE signal_ledger_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_event_outcomes_legacy_series_revision ON event_outcomes(event_id, security_id, mapping_id, horizon, method_version, revision) WHERE signal_ledger_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_event_outcomes_signal_contract ON event_outcomes(signal_ledger_id, method_version, observation_state, is_current, horizon) WHERE signal_ledger_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_event_outcomes_event_horizon ON event_outcomes(event_id, horizon, is_current)",
    "CREATE INDEX IF NOT EXISTS idx_event_outcomes_security_time ON event_outcomes(security_id, measured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_event_similarities_contract_left ON event_similarities(method, model_version, event_id, similar_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_similarities_contract_right ON event_similarities(method, model_version, similar_event_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_similarities_reverse ON event_similarities(similar_event_id, similarity DESC)",
    "CREATE INDEX IF NOT EXISTS idx_event_merge_target ON event_merge_history(target_event_id, merged_at)",
)


_TRIGGER_DDL = (
    """
    CREATE TRIGGER IF NOT EXISTS event_core_events_time_insert
    BEFORE INSERT ON events
    WHEN NEW.first_seen_at IS NULL OR typeof(NEW.first_seen_at) <> 'integer'
      OR NEW.first_seen_at < 0
      OR NEW.last_seen_at IS NULL OR typeof(NEW.last_seen_at) <> 'integer'
      OR NEW.last_seen_at < NEW.first_seen_at
      OR NEW.available_at IS NULL OR typeof(NEW.available_at) <> 'integer'
      OR NEW.available_at < NEW.first_seen_at OR NEW.available_at > NEW.last_seen_at
    BEGIN
        SELECT RAISE(ABORT, 'invalid event observation timestamps');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_events_time_update
    BEFORE UPDATE OF first_seen_at, last_seen_at, available_at ON events
    WHEN NEW.first_seen_at IS NULL OR typeof(NEW.first_seen_at) <> 'integer'
      OR NEW.first_seen_at < 0
      OR NEW.last_seen_at IS NULL OR typeof(NEW.last_seen_at) <> 'integer'
      OR NEW.last_seen_at < NEW.first_seen_at
      OR NEW.available_at IS NULL OR typeof(NEW.available_at) <> 'integer'
      OR NEW.available_at < NEW.first_seen_at OR NEW.available_at > NEW.last_seen_at
      OR NEW.first_seen_at <> OLD.first_seen_at
      OR NEW.last_seen_at < OLD.last_seen_at
      OR NEW.available_at < OLD.available_at
    BEGIN
        SELECT RAISE(ABORT, 'event observation timestamps cannot move backward');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_events_id_immutable
    BEFORE UPDATE OF event_id ON events
    WHEN NEW.event_id <> OLD.event_id
    BEGIN
        SELECT RAISE(ABORT, 'event UUID is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_events_version_monotonic
    BEFORE UPDATE OF version ON events
    WHEN NEW.version < OLD.version
    BEGIN
        SELECT RAISE(ABORT, 'event version cannot move backward');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_events_merge_contract_insert
    BEFORE INSERT ON events
    WHEN NEW.merge_state <> 'active' OR NEW.merged_into IS NOT NULL
      OR NEW.merge_contract IS NOT ''
    BEGIN
        SELECT RAISE(ABORT, 'events must be created active without a merge contract');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS event_core_events_merge_contract_update
    BEFORE UPDATE OF merge_state, merged_into, merge_contract ON events
    WHEN NOT (
        (
            NEW.merge_state IS OLD.merge_state
            AND NEW.merged_into IS OLD.merged_into
            AND NEW.merge_contract IS OLD.merge_contract
        ) OR (
            OLD.merge_state = 'active' AND OLD.merged_into IS NULL
            AND OLD.merge_contract IS ''
            AND NEW.merge_state = 'merged' AND NEW.merged_into IS NOT NULL
            AND NEW.merge_contract IS '{EVENT_MERGE_CONTRACT}'
            AND NEW.version = OLD.version + 1
            AND EXISTS (
                SELECT 1 FROM event_merge_history AS history
                JOIN events AS target
                  ON target.event_id = history.target_event_id
                WHERE history.source_event_id = OLD.event_id
                  AND history.target_event_id = NEW.merged_into
                  AND history.source_version = NEW.version
                  AND history.target_version = target.version
                  AND target.merge_state = 'active'
                  AND target.merged_into IS NULL
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'event merge contract transition is not authorized');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_merge_history_immutable_update
    BEFORE UPDATE ON event_merge_history
    BEGIN
        SELECT RAISE(ABORT, 'event merge history is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_merge_history_immutable_delete
    BEFORE DELETE ON event_merge_history
    BEGIN
        SELECT RAISE(ABORT, 'event merge history is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_event_input
    AFTER UPDATE OF version ON events
    WHEN NEW.version > OLD.version
      AND NEW.merge_state = 'active' AND NEW.merged_into IS NULL
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'input_version',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_group_merge
    AFTER UPDATE OF merge_state, merged_into ON events
    WHEN NEW.merge_state = 'merged' AND NEW.merged_into IS NOT NULL
      AND (OLD.merge_state IS NOT NEW.merge_state OR OLD.merged_into IS NOT NEW.merged_into)
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.merged_into,
            'group_merge',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_id_immutable
    BEFORE UPDATE OF mapping_id ON event_securities
    WHEN NEW.mapping_id <> OLD.mapping_id
    BEGIN
        SELECT RAISE(ABORT, 'mapping UUID is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_revision_immutable
    BEFORE UPDATE ON event_securities
    WHEN NEW.mapping_id IS NOT OLD.mapping_id
      OR NEW.event_id IS NOT OLD.event_id
      OR NEW.security_id IS NOT OLD.security_id
      OR NEW.relation_type IS NOT OLD.relation_type
      OR NEW.direction IS NOT OLD.direction
      OR NEW.expected_gap IS NOT OLD.expected_gap
      OR NEW.confidence IS NOT OLD.confidence
      OR NEW.relevance IS NOT OLD.relevance
      OR NEW.rationale IS NOT OLD.rationale
      OR NEW.mapping_version IS NOT OLD.mapping_version
      OR NEW.revision IS NOT OLD.revision
      OR NEW.created_at IS NOT OLD.created_at
      OR NEW.is_current > OLD.is_current
    BEGIN
        SELECT RAISE(ABORT, 'event-security revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_revision_immutable_delete
    BEFORE DELETE ON event_securities
    BEGIN
        SELECT RAISE(ABORT, 'event-security revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_evidence_id_immutable
    BEFORE UPDATE OF evidence_id ON event_evidence
    WHEN NEW.evidence_id <> OLD.evidence_id
    BEGIN
        SELECT RAISE(ABORT, 'evidence UUID is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_evidence_snapshot_immutable
    BEFORE UPDATE OF snapshot_hash, snapshot_text ON event_evidence
    WHEN NEW.snapshot_hash IS NOT OLD.snapshot_hash
      OR NEW.snapshot_text IS NOT OLD.snapshot_text
    BEGIN
        SELECT RAISE(ABORT, 'evidence snapshot is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_evidence_observed_monotonic
    BEFORE UPDATE OF observed_at ON event_evidence
    WHEN NEW.observed_at < OLD.observed_at
    BEGIN
        SELECT RAISE(ABORT, 'evidence observed_at cannot move backward');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_base_evidence_insert
    AFTER INSERT ON event_evidence
    WHEN NEW.mapping_id IS NULL
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'base_evidence_insert',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_base_evidence_update
    AFTER UPDATE OF version, source_uri, claim_text, excerpt, snapshot_hash, mapping_id
    ON event_evidence
    WHEN NEW.mapping_id IS NULL
      AND (
        NEW.version IS NOT OLD.version
        OR NEW.source_uri IS NOT OLD.source_uri
        OR NEW.claim_text IS NOT OLD.claim_text
        OR NEW.excerpt IS NOT OLD.excerpt
        OR NEW.snapshot_hash IS NOT OLD.snapshot_hash
        OR NEW.mapping_id IS NOT OLD.mapping_id
      )
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'base_evidence_update',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_base_evidence_delete
    AFTER DELETE ON event_evidence
    WHEN OLD.mapping_id IS NULL
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            OLD.event_id,
            'base_evidence_delete',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_mapping_insert
    AFTER INSERT ON event_securities
    WHEN NEW.mapping_version LIKE 'asset-map:%'
      AND lower(NEW.relation_type) <> 'benchmark_proxy'
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'mapping_insert',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_mapping_retire
    AFTER UPDATE OF is_current ON event_securities
    WHEN OLD.is_current IS NOT NEW.is_current
      AND NEW.mapping_version LIKE 'asset-map:%'
      AND lower(NEW.relation_type) <> 'benchmark_proxy'
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'mapping_retire',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_evidence_insert
    AFTER INSERT ON event_evidence
    WHEN NEW.mapping_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM event_securities AS mapping
        WHERE mapping.mapping_id = NEW.mapping_id
          AND mapping.mapping_version LIKE 'asset-map:%'
          AND lower(mapping.relation_type) <> 'benchmark_proxy'
    )
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'provenance_insert',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_evidence_update
    AFTER UPDATE OF source_uri, claim_text, excerpt, snapshot_hash, mapping_id ON event_evidence
    WHEN NEW.mapping_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM event_securities AS mapping
        WHERE mapping.mapping_id = NEW.mapping_id
          AND mapping.mapping_version LIKE 'asset-map:%'
          AND lower(mapping.relation_type) <> 'benchmark_proxy'
    )
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            NEW.event_id,
            'provenance_update',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_dirty_evidence_delete
    AFTER DELETE ON event_evidence
    WHEN OLD.mapping_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM event_securities AS mapping
        WHERE mapping.mapping_id = OLD.mapping_id
          AND mapping.mapping_version LIKE 'asset-map:%'
          AND lower(mapping.relation_type) <> 'benchmark_proxy'
    )
    BEGIN
        INSERT INTO event_mapping_replay_dirty_roots (root_event_id, reason, dirty_at)
        VALUES (
            OLD.event_id,
            'provenance_delete',
            CAST(strftime('%s', 'now') AS INTEGER)
        )
        ON CONFLICT(root_event_id) DO UPDATE SET
            reason = excluded.reason,
            dirty_at = MAX(event_mapping_replay_dirty_roots.dirty_at, excluded.dirty_at);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_similarity_replay_dirty_insert
    AFTER INSERT ON event_similarities
    WHEN NEW.method = 'event-graph-v1'
      AND NEW.model_version = 'deterministic-title-security-v1'
    BEGIN
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (NEW.event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (NEW.similar_event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_similarity_replay_dirty_update
    AFTER UPDATE ON event_similarities
    WHEN (
        OLD.method = 'event-graph-v1'
        AND OLD.model_version = 'deterministic-title-security-v1'
    ) OR (
        NEW.method = 'event-graph-v1'
        AND NEW.model_version = 'deterministic-title-security-v1'
    )
    BEGIN
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (OLD.event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (OLD.similar_event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (NEW.event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (NEW.similar_event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_similarity_replay_dirty_delete
    AFTER DELETE ON event_similarities
    WHEN OLD.method = 'event-graph-v1'
      AND OLD.model_version = 'deterministic-title-security-v1'
    BEGIN
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (OLD.event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
        INSERT INTO event_similarity_replay_dirty_roots
            (root_event_id, dirty_at)
        VALUES (OLD.similar_event_id, CAST(strftime('%s', 'now') AS INTEGER))
        ON CONFLICT(root_event_id) DO UPDATE SET
            generation = event_similarity_replay_dirty_roots.generation + 1,
            dirty_at = MAX(
                event_similarity_replay_dirty_roots.dirty_at,
                excluded.dirty_at
            );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_terminal_immutable
    BEFORE UPDATE ON event_mapping_replay_ledger
    WHEN OLD.status IN ('mapped', 'no_match')
    BEGIN
        SELECT RAISE(ABORT, 'terminal event mapping replay rows are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_insert_conflict_immutable
    BEFORE INSERT ON event_mapping_replay_ledger
    WHEN EXISTS (
        SELECT 1 FROM event_mapping_replay_ledger AS existing
        WHERE existing.evidence_id = NEW.evidence_id
          AND existing.mapping_version = NEW.mapping_version
          AND existing.input_fingerprint = NEW.input_fingerprint
    )
    BEGIN
        SELECT RAISE(ABORT, 'event mapping replay identity already exists');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_identity_immutable
    BEFORE UPDATE ON event_mapping_replay_ledger
    WHEN NEW.evidence_id IS NOT OLD.evidence_id
      OR NEW.evidence_version IS NOT OLD.evidence_version
      OR NEW.root_event_id IS NOT OLD.root_event_id
      OR NEW.root_event_version IS NOT OLD.root_event_version
      OR NEW.mapping_version IS NOT OLD.mapping_version
      OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
    BEGIN
        SELECT RAISE(ABORT, 'event mapping replay identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_failed_monotonic
    BEFORE UPDATE ON event_mapping_replay_ledger
    WHEN OLD.status = 'failed'
      AND NEW.evidence_id IS OLD.evidence_id
      AND NEW.evidence_version IS OLD.evidence_version
      AND NEW.root_event_id IS OLD.root_event_id
      AND NEW.root_event_version IS OLD.root_event_version
      AND NEW.mapping_version IS OLD.mapping_version
      AND NEW.input_fingerprint IS OLD.input_fingerprint
      AND (
        NEW.attempts <> OLD.attempts + 1
        OR NEW.processed_at < OLD.processed_at
        OR NEW.updated_at < OLD.updated_at
    )
    BEGIN
        SELECT RAISE(ABORT, 'failed event mapping replay attempts must advance once');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_mapping_replay_delete_immutable
    BEFORE DELETE ON event_mapping_replay_ledger
    BEGIN
        SELECT RAISE(ABORT, 'event mapping replay rows are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_outcome_ids_immutable
    BEFORE UPDATE OF outcome_revision_id, outcome_id ON event_outcomes
    WHEN NEW.outcome_revision_id <> OLD.outcome_revision_id
      OR NEW.outcome_id <> OLD.outcome_id
    BEGIN
        SELECT RAISE(ABORT, 'outcome UUID is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_outcome_revision_immutable
    BEFORE UPDATE ON event_outcomes
    WHEN NEW.outcome_revision_id IS NOT OLD.outcome_revision_id
      OR NEW.outcome_id IS NOT OLD.outcome_id
      OR NEW.event_id IS NOT OLD.event_id
      OR NEW.security_id IS NOT OLD.security_id
      OR NEW.mapping_id IS NOT OLD.mapping_id
      OR NEW.mapping_revision IS NOT OLD.mapping_revision
      OR NEW.signal_ledger_id IS NOT OLD.signal_ledger_id
      OR NEW.signal_available_at IS NOT OLD.signal_available_at
      OR NEW.observation_state IS NOT OLD.observation_state
      OR NEW.horizon IS NOT OLD.horizon
      OR NEW.baseline_at IS NOT OLD.baseline_at
      OR NEW.measured_at IS NOT OLD.measured_at
      OR NEW.baseline_price IS NOT OLD.baseline_price
      OR NEW.gross_return IS NOT OLD.gross_return
      OR NEW.benchmark_return IS NOT OLD.benchmark_return
      OR NEW.excess_return IS NOT OLD.excess_return
      OR NEW.mfe IS NOT OLD.mfe
      OR NEW.mae IS NOT OLD.mae
      OR NEW.direction_hit IS NOT OLD.direction_hit
      OR NEW.benchmark_security_id IS NOT OLD.benchmark_security_id
      OR NEW.method_version IS NOT OLD.method_version
      OR NEW.data_version IS NOT OLD.data_version
      OR NEW.data_quality IS NOT OLD.data_quality
      OR NEW.revision IS NOT OLD.revision
      OR NEW.created_at IS NOT OLD.created_at
      OR NEW.is_current > OLD.is_current
    BEGIN
        SELECT RAISE(ABORT, 'event outcome revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_core_outcome_revision_immutable_delete
    BEFORE DELETE ON event_outcomes
    BEGIN
        SELECT RAISE(ABORT, 'event outcome revisions are immutable');
    END
    """,
)


_REQUIRED_COLUMNS = {
    "events": {
        "event_id", "title", "event_type", "direction", "priority",
        "occurred_at", "version", "merge_state", "merged_into",
        "strategy_version", "model_version", "created_at", "updated_at",
        "merge_contract",
        "first_seen_at", "last_seen_at", "available_at",
    },
    "event_identities": {"identity_type", "identity_key", "event_id", "created_at"},
    "securities": {
        "security_id", "exchange", "ticker", "name", "asset_type", "currency",
        "aliases", "industry", "concepts", "is_active", "version", "created_at", "updated_at",
    },
    "event_securities": {
        "mapping_id", "event_id", "security_id", "relation_type", "direction", "expected_gap",
        "confidence", "relevance", "rationale", "mapping_version", "revision", "is_current",
        "created_at",
    },
    "event_evidence": {
        "evidence_id", "event_id", "security_id", "mapping_id", "mapping_revision",
        "source_uri", "source_type", "source_name", "claim_key", "claim_text", "excerpt",
        "snapshot_hash", "snapshot_text", "stance", "reliability", "announcement_at",
        "observed_at", "version", "created_at", "updated_at",
    },
    "event_mapping_replay_ledger": {
        "evidence_id", "evidence_version", "root_event_id", "root_event_version",
        "mapping_version", "input_fingerprint", "output_fingerprint", "status",
        "mapped_count", "attempts", "failure_code", "processed_at", "updated_at",
    },
    "event_mapping_replay_dirty_roots": {
        "root_event_id", "reason", "dirty_at",
    },
    "event_similarity_replay_dirty_roots": {
        "root_event_id", "generation", "dirty_at",
    },
    "event_outcomes": {
        "outcome_revision_id", "outcome_id", "event_id", "security_id", "mapping_id",
        "mapping_revision", "signal_ledger_id", "signal_available_at", "observation_state",
        "horizon", "baseline_at", "measured_at", "baseline_price",
        "gross_return", "benchmark_return", "excess_return", "mfe", "mae", "direction_hit",
        "benchmark_security_id", "method_version", "data_version", "data_quality", "revision",
        "is_current", "created_at",
    },
    "event_similarities": {
        "event_id", "similar_event_id", "method", "model_version", "similarity",
        "rationale", "common_securities", "shared_paths", "computed_at", "version",
        "created_at", "updated_at",
    },
    "event_merge_history": {
        "merge_id", "source_event_id", "target_event_id", "source_version",
        "target_version", "reason", "actor", "merged_at",
    },
}

# Public, immutable inventory used by startup's read-only migration gate.  Keep
# it derived from the exact schema verifier so a newly added event-core table
# cannot silently fall outside the fail-closed data check.
EVENT_CORE_TABLE_NAMES = frozenset(_REQUIRED_COLUMNS)

_INTEGRATION_OUTBOX_COLUMN_CONTRACT = {
    "seq": ("INTEGER", 0, 1),
    "ledger_signal_id": ("INTEGER", 1, 0),
    "event_id": ("TEXT", 1, 0),
    "available_at": ("INTEGER", 1, 0),
    "recorded_at": ("INTEGER", 1, 0),
    "enqueued_at": ("INTEGER", 1, 0),
    "score": ("REAL", 1, 0),
    "direction": ("TEXT", 1, 0),
    "score_confidence": ("REAL", 1, 0),
    "strategy_version": ("TEXT", 1, 0),
    "mapping_version": ("TEXT", 1, 0),
    "mapped_theme": ("TEXT", 1, 0),
    "backtest_symbol": ("TEXT", 0, 0),
    "score_path": ("TEXT", 1, 0),
    "revision_key": ("TEXT", 1, 0),
    "signal_stage": ("TEXT", 1, 0),
    "revision_seq": ("INTEGER", 1, 0),
    "revision_state_key": ("TEXT", 1, 0),
    "independence_id": ("TEXT", 1, 0),
    "aggregation_threshold": ("REAL", 1, 0),
    "selected": ("INTEGER", 1, 0),
    "is_similarity_copy": ("INTEGER", 1, 0),
    "domain_event_id": ("TEXT", 0, 0),
    "event_version": ("INTEGER", 0, 0),
    "event_mapping_id": ("TEXT", 0, 0),
    "mapping_confidence": ("REAL", 1, 0),
    "mapping_relevance": ("REAL", 1, 0),
    "expected_gap": ("REAL", 0, 0),
}

_INTEGRATION_OUTBOX_INDEX_CONTRACT = {
    "idx_integration_outbox_enqueued": ("enqueued_at", "seq"),
    "idx_integration_outbox_domain_event": (
        "domain_event_id",
        "event_version",
        "seq",
    ),
}


_REQUIRED_FOREIGN_KEY_GROUPS = {
    "events": {(('merged_into',), "events", ('event_id',), "RESTRICT")},
    "event_identities": {(('event_id',), "events", ('event_id',), "RESTRICT")},
    "event_securities": {
        (('event_id',), "events", ('event_id',), "RESTRICT"),
        (('security_id',), "securities", ('security_id',), "RESTRICT"),
    },
    "event_evidence": {
        (('event_id',), "events", ('event_id',), "RESTRICT"),
        (('security_id',), "securities", ('security_id',), "RESTRICT"),
        (
            ('mapping_id', 'event_id', 'security_id', 'mapping_revision'),
            "event_securities",
            ('mapping_id', 'event_id', 'security_id', 'revision'),
            "RESTRICT",
        ),
    },
    "event_mapping_replay_ledger": {
        (('evidence_id',), "event_evidence", ('evidence_id',), "RESTRICT"),
        (('root_event_id',), "events", ('event_id',), "RESTRICT"),
    },
    "event_mapping_replay_dirty_roots": {
        (('root_event_id',), "events", ('event_id',), "RESTRICT"),
    },
    "event_similarity_replay_dirty_roots": {
        (('root_event_id',), "events", ('event_id',), "RESTRICT"),
    },
    "event_outcomes": {
        (('event_id',), "events", ('event_id',), "RESTRICT"),
        (('security_id',), "securities", ('security_id',), "RESTRICT"),
        (('benchmark_security_id',), "securities", ('security_id',), "RESTRICT"),
        (
            ('mapping_id', 'event_id', 'security_id', 'mapping_revision'),
            "event_securities",
            ('mapping_id', 'event_id', 'security_id', 'revision'),
            "RESTRICT",
        ),
    },
    "event_similarities": {
        (('event_id',), "events", ('event_id',), "RESTRICT"),
        (('similar_event_id',), "events", ('event_id',), "RESTRICT"),
    },
    "event_merge_history": {
        (('source_event_id',), "events", ('event_id',), "RESTRICT"),
        (('target_event_id',), "events", ('event_id',), "RESTRICT"),
    },
}

_REQUIRED_INDEX_NAMES = {
    "idx_events_occurred_priority",
    "idx_events_radar_order",
    "idx_events_merge_target",
    "idx_event_identities_event",
    "idx_securities_name",
    "idx_securities_industry",
    "uq_event_securities_current",
    "idx_event_securities_security",
    "idx_event_evidence_event_time",
    "idx_event_evidence_source",
    "idx_event_evidence_mapping",
    "idx_event_evidence_unbound_observed",
    "idx_event_mapping_replay_lookup",
    "idx_event_mapping_replay_contract_input",
    "idx_event_mapping_replay_contract_root",
    "idx_event_mapping_replay_dirty_order",
    "idx_event_similarity_replay_dirty_order",
    "uq_event_outcomes_series_current",
    "uq_event_outcomes_legacy_series_current",
    "uq_event_outcomes_series_revision",
    "uq_event_outcomes_legacy_series_revision",
    "idx_event_outcomes_signal_contract",
    "idx_event_outcomes_event_horizon",
    "idx_event_outcomes_security_time",
    "idx_event_similarities_contract_left",
    "idx_event_similarities_contract_right",
    "idx_event_similarities_reverse",
    "idx_event_merge_target",
}

_REQUIRED_TRIGGER_NAMES = {
    "event_core_events_time_insert",
    "event_core_events_time_update",
    "event_core_events_id_immutable",
    "event_core_events_version_monotonic",
    "event_core_events_merge_contract_insert",
    "event_core_events_merge_contract_update",
    "event_core_merge_history_immutable_update",
    "event_core_merge_history_immutable_delete",
    "event_core_mapping_replay_dirty_event_input",
    "event_core_mapping_replay_dirty_group_merge",
    "event_core_mapping_id_immutable",
    "event_core_mapping_revision_immutable",
    "event_core_mapping_revision_immutable_delete",
    "event_core_evidence_id_immutable",
    "event_core_evidence_snapshot_immutable",
    "event_core_evidence_observed_monotonic",
    "event_core_mapping_replay_dirty_base_evidence_insert",
    "event_core_mapping_replay_dirty_base_evidence_update",
    "event_core_mapping_replay_dirty_base_evidence_delete",
    "event_core_mapping_replay_dirty_mapping_insert",
    "event_core_mapping_replay_dirty_mapping_retire",
    "event_core_mapping_replay_dirty_evidence_insert",
    "event_core_mapping_replay_dirty_evidence_update",
    "event_core_mapping_replay_dirty_evidence_delete",
    "event_core_similarity_replay_dirty_insert",
    "event_core_similarity_replay_dirty_update",
    "event_core_similarity_replay_dirty_delete",
    "event_core_mapping_replay_terminal_immutable",
    "event_core_mapping_replay_insert_conflict_immutable",
    "event_core_mapping_replay_identity_immutable",
    "event_core_mapping_replay_failed_monotonic",
    "event_core_mapping_replay_delete_immutable",
    "event_core_outcome_ids_immutable",
    "event_core_outcome_revision_immutable",
    "event_core_outcome_revision_immutable_delete",
}

_LEGACY_OUTCOME_CURRENT_INDEX = "uq_event_outcomes_current"
_LEGACY_OUTCOME_CURRENT_INDEX_DDL = (
    "CREATE UNIQUE INDEX uq_event_outcomes_current ON event_outcomes("
    "event_id, security_id, mapping_id, horizon, method_version, data_version) "
    "WHERE is_current = 1"
)


def _normalize_schema_sql(value: object) -> str:
    """Normalize SQLite's stored DDL without weakening its semantics."""
    sql = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b",
        "",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").casefold()


def _named_schema_contracts(
    statements: Iterable[str], object_type: str
) -> dict[str, str]:
    pattern = re.compile(
        rf"\bCREATE\s+(?:UNIQUE\s+)?{re.escape(object_type)}\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)",
        re.IGNORECASE,
    )
    contracts: dict[str, str] = {}
    for statement in statements:
        match = pattern.search(statement)
        if match is None:
            raise AssertionError(f"cannot identify {object_type} schema contract")
        contracts[match.group(1)] = _normalize_schema_sql(statement)
    return contracts


_ALL_TABLE_SQL = _named_schema_contracts(_DDL, "TABLE")
_REQUIRED_REPLAY_TABLE_SQL = {
    name: _ALL_TABLE_SQL[name]
    for name in (
        "event_mapping_replay_ledger",
        "event_mapping_replay_dirty_roots",
        "event_similarity_replay_dirty_roots",
    )
}
_REQUIRED_INDEX_SQL = _named_schema_contracts(_INDEX_DDL, "INDEX")
_REQUIRED_TRIGGER_SQL = _named_schema_contracts(_TRIGGER_DDL, "TRIGGER")

_MAPPING_REVISION_TRIGGER_NAME = "event_core_mapping_revision_immutable"
_MAPPING_REVISION_TRIGGER_DDL = next(
    statement
    for statement in _TRIGGER_DDL
    if _MAPPING_REVISION_TRIGGER_NAME in statement
)
_LEGACY_MAPPING_REVISION_TRIGGER_SQL = _normalize_schema_sql(
    _MAPPING_REVISION_TRIGGER_DDL.replace(
        "      OR NEW.expected_gap IS NOT OLD.expected_gap\n",
        "",
    )
)

_OUTCOME_REVISION_TRIGGER_NAME = "event_core_outcome_revision_immutable"
_OUTCOME_REVISION_TRIGGER_DDL = next(
    statement
    for statement in _TRIGGER_DDL
    if _OUTCOME_REVISION_TRIGGER_NAME in statement
)
_LEGACY_OUTCOME_REVISION_TRIGGER_SQL = _normalize_schema_sql(
    _OUTCOME_REVISION_TRIGGER_DDL.replace(
        "      OR NEW.signal_ledger_id IS NOT OLD.signal_ledger_id\n",
        "",
    ).replace(
        "      OR NEW.signal_available_at IS NOT OLD.signal_available_at\n",
        "",
    ).replace(
        "      OR NEW.observation_state IS NOT OLD.observation_state\n",
        "",
    )
)


def _foreign_key_groups(
    conn: sqlite3.Connection, table: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
        grouped.setdefault(int(row[0]), []).append(tuple(row))

    result: set[tuple[tuple[str, ...], str, tuple[str, ...], str]] = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        result.add(
            (
                tuple(str(row[3]) for row in ordered),
                str(ordered[0][2]),
                tuple(str(row[4]) for row in ordered),
                str(ordered[0][6]).upper(),
            )
        )
    return result


def _verify_named_schema_objects(
    conn: sqlite3.Connection, object_type: str, contracts: Mapping[str, str]
) -> None:
    for name, expected_sql in contracts.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
        if row is None or _normalize_schema_sql(row[0]) != expected_sql:
            raise EventCoreSchemaError(
                f"incompatible {object_type} definition: {name}"
            )


def _verify_schema(conn: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            raise EventCoreSchemaError(f"incompatible {table} table; missing: {missing}")
    event_table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    event_columns = {
        str(row[1]): tuple(row) for row in conn.execute("PRAGMA table_info(events)")
    }
    merge_contract_column = event_columns.get("merge_contract")
    merge_contract_check = _normalize_schema_sql(
        "CHECK (length(merge_contract) <= 64)"
    )
    if (
        event_table is None
        or merge_contract_column is None
        or str(merge_contract_column[2]).upper() != "TEXT"
        or int(merge_contract_column[3]) != 1
        or str(merge_contract_column[4]) != "''"
        or merge_contract_check not in _normalize_schema_sql(event_table[0])
    ):
        raise EventCoreSchemaError("incompatible events merge_contract constraint")
    mapping_table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'event_securities'"
    ).fetchone()
    expected_gap_check = _normalize_schema_sql(
        "CHECK (" + _EXPECTED_GAP_SQL_CHECK.format(column="expected_gap") + ")"
    )
    if (
        mapping_table is None
        or expected_gap_check not in _normalize_schema_sql(mapping_table[0])
    ):
        raise EventCoreSchemaError(
            "incompatible event_securities expected_gap constraint"
        )
    outcome_table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'event_outcomes'"
    ).fetchone()
    outcome_sql = "" if outcome_table is None else _normalize_schema_sql(outcome_table[0])
    outcome_checks = (
        _normalize_schema_sql(
            "CHECK (signal_ledger_id IS NULL OR "
            "(typeof(signal_ledger_id) = 'integer' AND signal_ledger_id >= 1))"
        ),
        _normalize_schema_sql(
            "CHECK (signal_available_at IS NULL OR "
            "(typeof(signal_available_at) = 'integer' AND signal_available_at > 0))"
        ),
        _normalize_schema_sql(
            "CHECK (observation_state IN ('legacy_unknown', 'not_due', "
            "'missing_asset_close', 'missing_benchmark_close', 'complete'))"
        ),
    )
    outcome_compact = re.sub(r"\s+", "", outcome_sql)
    if not outcome_sql or any(
        re.sub(r"\s+", "", check) not in outcome_compact
        for check in outcome_checks
    ):
        raise EventCoreSchemaError(
            "incompatible event_outcomes observation metadata constraints"
        )
    legacy_series_constraint = re.sub(
        r"\s+",
        "",
        _normalize_schema_sql(
            "UNIQUE (event_id, security_id, mapping_id, horizon, method_version, revision)"
        ),
    )
    if legacy_series_constraint in outcome_compact:
        raise EventCoreSchemaError(
            "incompatible event_outcomes signal-series identity"
        )
    # The replay tables are an audit boundary, so verify their complete DDL
    # (PK/CHECK/FK/WITHOUT ROWID included) before the generic FK subset check.
    # This also makes a weakened replacement fail with one stable contract error.
    _verify_named_schema_objects(conn, "table", _REQUIRED_REPLAY_TABLE_SQL)
    for table, required in _REQUIRED_FOREIGN_KEY_GROUPS.items():
        actual = _foreign_key_groups(conn, table)
        if not required.issubset(actual):
            raise EventCoreSchemaError(f"incompatible {table} foreign keys")
    _verify_named_schema_objects(conn, "index", _REQUIRED_INDEX_SQL)
    _verify_named_schema_objects(conn, "trigger", _REQUIRED_TRIGGER_SQL)


def _observation_times_are_valid(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE "
        "first_seen_at IS NULL OR typeof(first_seen_at) <> 'integer' OR first_seen_at < 0 "
        "OR last_seen_at IS NULL OR typeof(last_seen_at) <> 'integer' "
        "OR last_seen_at < first_seen_at "
        "OR available_at IS NULL OR typeof(available_at) <> 'integer' "
        "OR available_at < first_seen_at OR available_at > last_seen_at LIMIT 1"
    ).fetchone()
    return row is None


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """Read-only fast path for already initialized databases."""
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
    ).fetchall()
    objects = {(str(row[0]), str(row[1])) for row in rows}
    required = {
        *(("table", name) for name in _REQUIRED_COLUMNS),
        *(("index", name) for name in _REQUIRED_INDEX_NAMES),
        *(("trigger", name) for name in _REQUIRED_TRIGGER_NAMES),
    }
    if not required.issubset(objects):
        return False
    if ("index", _LEGACY_OUTCOME_CURRENT_INDEX) in objects:
        return False
    try:
        _verify_schema(conn)
        return _observation_times_are_valid(conn)
    except (sqlite3.DatabaseError, EventCoreSchemaError):
        return False


def _migrate_event_observation_times(conn: sqlite3.Connection) -> None:
    """Add and safely backfill observation columns on an earlier event-core DB."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(events)")}
    for column in ("first_seen_at", "last_seen_at", "available_at"):
        if column not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} INTEGER")

    # The earlier schema already requires non-negative integer created_at and
    # updated_at.  Use those persisted ingestion times, never occurred_at, so a
    # historical event cannot be made to appear known before ingestion.
    conn.execute(
        "UPDATE events SET "
        "first_seen_at = CAST(COALESCE(created_at, updated_at, 0) AS INTEGER) "
        "WHERE first_seen_at IS NULL"
    )
    conn.execute(
        "UPDATE events SET available_at = first_seen_at WHERE available_at IS NULL"
    )
    conn.execute(
        "UPDATE events SET last_seen_at = MAX("
        "CAST(COALESCE(updated_at, first_seen_at) AS INTEGER), first_seen_at, available_at) "
        "WHERE last_seen_at IS NULL"
    )
    if not _observation_times_are_valid(conn):
        raise EventCoreSchemaError("existing events contain invalid observation timestamps")


def _migrate_event_merge_contract(conn: sqlite3.Connection) -> None:
    """Fence irreversible merges while preserving legacy merged history."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(events)")}
    if "merge_contract" not in columns:
        conn.execute(
            "ALTER TABLE events ADD COLUMN merge_contract TEXT NOT NULL DEFAULT '' "
            "CHECK (length(merge_contract) <= 64)"
        )


def _migrate_event_security_expected_gap(conn: sqlite3.Connection) -> None:
    """Add the signed mapping estimate without fabricating legacy values.

    The prior release's immutable-revision trigger is replaced only when its
    SQL is the exact known legacy contract.  A same-named custom or corrupted
    trigger therefore still fails closed instead of being silently repaired.
    The caller holds the schema migration transaction, so the column and its
    upgraded trigger are committed or rolled back together.
    """
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(event_securities)")
    }
    if "expected_gap" in columns:
        return

    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (_MAPPING_REVISION_TRIGGER_NAME,),
    ).fetchone()
    if trigger is not None and _normalize_schema_sql(trigger[0]) != (
        _LEGACY_MAPPING_REVISION_TRIGGER_SQL
    ):
        raise EventCoreSchemaError(
            f"incompatible trigger definition: {_MAPPING_REVISION_TRIGGER_NAME}"
        )

    conn.execute(
        "ALTER TABLE event_securities ADD COLUMN expected_gap REAL CHECK ("
        f"{_EXPECTED_GAP_SQL_CHECK.format(column='expected_gap')})"
    )
    if trigger is not None:
        conn.execute(f"DROP TRIGGER {_MAPPING_REVISION_TRIGGER_NAME}")


def _migrate_event_outcome_signal_series_identity(conn: sqlite3.Connection) -> None:
    """Rebuild the outcome table so frozen signals own independent series.

    The earlier table-level revision key omitted ``signal_ledger_id``.  That
    made two strategy contracts sharing one frozen mapping collide even though
    their decisions were independent.  The rebuild is lossless and runs inside
    the caller's schema transaction; partial indexes are installed afterwards.
    """
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'event_outcomes'"
    ).fetchone()
    if table_row is None:
        return
    table_sql = str(table_row[0] or "")
    legacy_pattern = re.compile(
        r",\s*UNIQUE\s*\(\s*event_id\s*,\s*security_id\s*,\s*mapping_id\s*,"
        r"\s*horizon\s*,\s*method_version\s*,\s*revision\s*\)",
        flags=re.IGNORECASE,
    )
    if legacy_pattern.search(table_sql) is None:
        return
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'event_outcomes_signal_series_new'"
    ).fetchone() is not None:
        raise EventCoreSchemaError("stale event outcome migration table exists")

    for trigger_name in (
        "event_core_outcome_ids_immutable",
        "event_core_outcome_revision_immutable",
        "event_core_outcome_revision_immutable_delete",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    rebuilt_sql = legacy_pattern.sub("", table_sql, count=1)
    rebuilt_sql = re.sub(
        r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:[`\"\[]?event_outcomes[`\"\]]?)",
        "CREATE TABLE event_outcomes_signal_series_new",
        rebuilt_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if "event_outcomes_signal_series_new" not in rebuilt_sql:
        raise EventCoreSchemaError("cannot derive event outcome migration schema")
    conn.execute(rebuilt_sql)
    columns = [
        str(row[1]) for row in conn.execute("PRAGMA table_info(event_outcomes)")
    ]
    if not columns:
        raise EventCoreSchemaError("event_outcomes has no columns")
    quoted = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
    conn.execute(
        f"INSERT INTO event_outcomes_signal_series_new ({quoted}) "
        f"SELECT {quoted} FROM event_outcomes"
    )
    old_count = int(conn.execute("SELECT COUNT(*) FROM event_outcomes").fetchone()[0])
    new_count = int(
        conn.execute("SELECT COUNT(*) FROM event_outcomes_signal_series_new").fetchone()[0]
    )
    mismatch = conn.execute(
        f"SELECT 1 FROM (SELECT {quoted} FROM event_outcomes EXCEPT "
        f"SELECT {quoted} FROM event_outcomes_signal_series_new) LIMIT 1"
    ).fetchone()
    if old_count != new_count or mismatch is not None:
        raise EventCoreSchemaError("event outcome signal-series rebuild is not lossless")
    conn.execute("DROP TABLE event_outcomes")
    conn.execute(
        "ALTER TABLE event_outcomes_signal_series_new RENAME TO event_outcomes"
    )


def _migrate_event_outcome_current_index(conn: sqlite3.Connection) -> None:
    """Replace the v1 data-version-scoped current index exactly once.

    Older databases could contain one current row per ``data_version``.  Retire
    every older duplicate deterministically before installing the stable series
    index; historical rows and their outcome UUIDs remain untouched.
    """
    old_index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (_LEGACY_OUTCOME_CURRENT_INDEX,),
    ).fetchone()
    if old_index is None:
        # This is a one-way legacy migration, not a general repair pass.  A
        # current database can enter schema initialization because an unrelated
        # index or trigger is missing; collapsing its frozen-signal series by
        # the former event-level key would retire valid outcomes from another
        # scoring contract.  New-series constraint failures must fail closed
        # when the replacement indexes are recreated below.
        return
    if _normalize_schema_sql(old_index[0]) != _normalize_schema_sql(
        _LEGACY_OUTCOME_CURRENT_INDEX_DDL
    ):
        raise EventCoreSchemaError(
            f"incompatible index definition: {_LEGACY_OUTCOME_CURRENT_INDEX}"
        )

    current_rows = conn.execute(
        "SELECT event_id, security_id, mapping_id, horizon, method_version, "
        "outcome_revision_id, created_at, revision FROM event_outcomes "
        "WHERE is_current = 1 ORDER BY event_id, security_id, mapping_id, horizon, "
        "method_version, created_at DESC, revision DESC, outcome_revision_id DESC"
    ).fetchall()
    seen: set[tuple[str, str, str, str, str]] = set()
    retire: list[str] = []
    for row in current_rows:
        key = tuple(str(value) for value in row[:5])
        if key in seen:
            retire.append(str(row[5]))
        else:
            seen.add(key)
    if retire:
        conn.executemany(
            "UPDATE event_outcomes SET is_current = 0 WHERE outcome_revision_id = ?",
            ((revision_id,) for revision_id in retire),
        )
    conn.execute(f"DROP INDEX {_LEGACY_OUTCOME_CURRENT_INDEX}")


def _migrate_event_outcome_observation_metadata(conn: sqlite3.Connection) -> None:
    """Add frozen signal identity and explicit close-observation state.

    Existing outcomes predate the signal ledger reference, so they remain
    ``legacy_unknown`` rather than borrowing the mutable event availability
    timestamp.  The immutable-revision trigger is upgraded only from the exact
    known predecessor contract and is recreated by ``init_event_core_schema``.
    """
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(event_outcomes)")
    }
    required = {
        "signal_ledger_id",
        "signal_available_at",
        "observation_state",
    }
    if required.issubset(columns):
        return

    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (_OUTCOME_REVISION_TRIGGER_NAME,),
    ).fetchone()
    if trigger is not None and _normalize_schema_sql(trigger[0]) != (
        _LEGACY_OUTCOME_REVISION_TRIGGER_SQL
    ):
        raise EventCoreSchemaError(
            f"incompatible trigger definition: {_OUTCOME_REVISION_TRIGGER_NAME}"
        )

    if "signal_ledger_id" not in columns:
        conn.execute(
            "ALTER TABLE event_outcomes ADD COLUMN signal_ledger_id INTEGER "
            "CHECK (signal_ledger_id IS NULL OR "
            "(typeof(signal_ledger_id) = 'integer' AND signal_ledger_id >= 1))"
        )
    if "signal_available_at" not in columns:
        conn.execute(
            "ALTER TABLE event_outcomes ADD COLUMN signal_available_at INTEGER "
            "CHECK (signal_available_at IS NULL OR "
            "(typeof(signal_available_at) = 'integer' AND signal_available_at > 0))"
        )
    if "observation_state" not in columns:
        conn.execute(
            "ALTER TABLE event_outcomes ADD COLUMN observation_state TEXT NOT NULL "
            "DEFAULT 'legacy_unknown' CHECK (observation_state IN "
            "('legacy_unknown', 'not_due', 'missing_asset_close', "
            "'missing_benchmark_close', 'complete'))"
        )
    if trigger is not None:
        conn.execute(f"DROP TRIGGER {_OUTCOME_REVISION_TRIGGER_NAME}")


def _seed_mapping_replay_dirty_roots(conn: sqlite3.Connection) -> None:
    """Queue pre-ledger deterministic output exactly once during schema upgrade."""
    conn.execute(
        "WITH RECURSIVE mapping_roots(root_event_id, depth) AS ("
        " SELECT DISTINCT mapping.event_id, 0 FROM event_securities AS mapping"
        " WHERE mapping.is_current = 1"
        " AND mapping.mapping_version LIKE 'asset-map:%'"
        " AND lower(mapping.relation_type) <> 'benchmark_proxy'"
        " UNION ALL"
        " SELECT parent.merged_into, walk.depth + 1"
        " FROM mapping_roots AS walk JOIN events AS parent"
        " ON parent.event_id = walk.root_event_id"
        " WHERE parent.merged_into IS NOT NULL AND walk.depth < 64"
        ") INSERT INTO event_mapping_replay_dirty_roots"
        " (root_event_id, reason, dirty_at)"
        " SELECT DISTINCT root.event_id, 'schema_upgrade_seed',"
        " CAST(strftime('%s', 'now') AS INTEGER)"
        " FROM mapping_roots AS walk JOIN events AS root"
        " ON root.event_id = walk.root_event_id"
        " WHERE root.merge_state = 'active' AND root.merged_into IS NULL"
        " ON CONFLICT(root_event_id) DO NOTHING"
    )


def _seed_similarity_replay_dirty_roots(conn: sqlite3.Connection) -> None:
    """Queue both canonical endpoints of every pre-upgrade graph edge once."""
    endpoint_rows = conn.execute(
        "SELECT event_id FROM event_similarities "
        "WHERE method = 'event-graph-v1' "
        "AND model_version = 'deterministic-title-security-v1' "
        "UNION SELECT similar_event_id FROM event_similarities "
        "WHERE method = 'event-graph-v1' "
        "AND model_version = 'deterministic-title-security-v1' "
        "ORDER BY event_id"
    ).fetchall()
    dirty_at = int(time.time())
    for row in endpoint_rows:
        root_id = canonical_event_id(conn, str(row[0]))
        conn.execute(
            "INSERT INTO event_similarity_replay_dirty_roots "
            "(root_event_id, dirty_at) VALUES (?, ?) "
            "ON CONFLICT(root_event_id) DO NOTHING",
            (root_id, dirty_at),
        )


def init_event_core_schema(conn: sqlite3.Connection) -> None:
    """Create and validate the event core schema on ``conn`` idempotently."""
    _enable_foreign_keys(conn)
    if _schema_is_current(conn):
        return
    with _atomic(conn):
        if _schema_is_current(conn):
            return
        existing_replay_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('event_mapping_replay_ledger', "
                "'event_mapping_replay_dirty_roots')"
            ).fetchall()
        }
        replay_schema_was_complete = existing_replay_tables == {
            "event_mapping_replay_ledger",
            "event_mapping_replay_dirty_roots",
        }
        similarity_replay_table_existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'event_similarity_replay_dirty_roots'"
        ).fetchone() is not None
        for statement in _DDL:
            conn.execute(statement)
        _migrate_event_observation_times(conn)
        _migrate_event_merge_contract(conn)
        _migrate_event_security_expected_gap(conn)
        _migrate_event_outcome_observation_metadata(conn)
        # Detect and collapse the legacy data-version-scoped current index
        # before a table rebuild removes that index from sqlite_master.
        _migrate_event_outcome_current_index(conn)
        _migrate_event_outcome_signal_series_identity(conn)
        for statement in _INDEX_DDL:
            conn.execute(statement)
        for statement in _TRIGGER_DDL:
            conn.execute(statement)
        if not replay_schema_was_complete:
            # Triggers only observe writes made after installation. Queue every
            # pre-existing deterministic current output whenever either replay
            # table had to be created. This also heals an interrupted/partial
            # predecessor that left an empty dirty table but no ledger.
            _seed_mapping_replay_dirty_roots(conn)
        if not similarity_replay_table_existed:
            # Old current-model similarities can already be stale outside the
            # scheduled time window. Seed both canonical endpoints atomically
            # with table creation so the graph proves or withdraws every edge.
            _seed_similarity_replay_dirty_roots(conn)
        _verify_schema(conn)
        if not _observation_times_are_valid(conn):
            raise EventCoreSchemaError("event observation timestamp validation failed")


def verify_event_core_schema(conn: sqlite3.Connection) -> None:
    """Verify the exact event-core contract without mutating the database."""
    _verify_schema(conn)
    if not _observation_times_are_valid(conn):
        raise EventCoreSchemaError("event observation timestamp validation failed")


def verify_integration_outbox_schema(conn: sqlite3.Connection) -> None:
    """Verify the durable Outbox schema without installing or repairing it."""
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'integration_signal_outbox'"
    ).fetchone()
    if table_row is None:
        raise EventCoreSchemaError("integration_signal_outbox table is missing")

    actual_columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5]), row[4])
        for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
    }
    for name, expected in _INTEGRATION_OUTBOX_COLUMN_CONTRACT.items():
        actual = actual_columns.get(name)
        if actual is None or actual[:3] != expected:
            raise EventCoreSchemaError(
                f"incompatible integration_signal_outbox column: {name}"
            )
    for name in ("mapping_confidence", "mapping_relevance"):
        default = actual_columns[name][3]
        if str(default or "").strip("() ") != "0":
            raise EventCoreSchemaError(
                f"incompatible integration_signal_outbox default: {name}"
            )

    table_sql = _normalize_schema_sql(table_row[0])
    compact_sql = re.sub(r"\s+", "", table_sql)
    required_fragments = (
        "seqintegerprimarykeyautoincrement",
        "check(selectedin(0,1))",
        "check(is_similarity_copyin(0,1))",
        "check(typeof(mapping_confidence)in('integer','real')"
        "andmapping_confidence>=0andmapping_confidence<=1)",
        "check(typeof(mapping_relevance)in('integer','real')"
        "andmapping_relevance>=0andmapping_relevance<=10)",
        "check(expected_gapisnullor(typeof(expected_gap)in('integer','real')"
        "andexpected_gap>=-100andexpected_gap<=100))",
    )
    if any(fragment not in compact_sql for fragment in required_fragments):
        raise EventCoreSchemaError(
            "incompatible integration_signal_outbox table constraints"
        )

    index_rows = list(conn.execute("PRAGMA index_list(integration_signal_outbox)"))
    index_columns: dict[str, tuple[str, ...]] = {}
    unique_columns: set[tuple[str, ...]] = set()
    for row in index_rows:
        name = str(row[1])
        columns = tuple(
            str(item[2])
            for item in conn.execute(
                "SELECT seqno, cid, name FROM pragma_index_info(?) ORDER BY seqno",
                (name,),
            )
        )
        index_columns[name] = columns
        if int(row[2]) == 1 and int(row[4]) == 0:
            unique_columns.add(columns)
    for name, expected_columns in _INTEGRATION_OUTBOX_INDEX_CONTRACT.items():
        if index_columns.get(name) != expected_columns:
            raise EventCoreSchemaError(
                f"incompatible integration_signal_outbox index: {name}"
            )
    for expected_unique in (("ledger_signal_id",), ("event_id",)):
        if expected_unique not in unique_columns:
            raise EventCoreSchemaError(
                "integration_signal_outbox unique identity constraint is missing"
            )


def count_unresolved_event_core_outbox_references(
    conn: sqlite3.Connection,
) -> int:
    """Count retained Outbox rows that violate the stable-event reference contract."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'integration_signal_outbox'"
    ).fetchone() is None:
        return 0
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
    }
    required = {"domain_event_id", "event_version", "event_mapping_id"}
    if not required.issubset(columns):
        return int(
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
        )
    required_tables = {"events", "event_securities"}
    present_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('events', 'event_securities')"
        ).fetchall()
    }
    if present_tables != required_tables:
        return int(
            conn.execute("SELECT COUNT(*) FROM integration_signal_outbox").fetchone()[0]
        )
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM integration_signal_outbox AS outbox "
        "LEFT JOIN events AS event ON event.event_id = outbox.domain_event_id "
        "LEFT JOIN event_securities AS mapping "
        "ON mapping.mapping_id = outbox.event_mapping_id "
        "AND mapping.event_id = outbox.domain_event_id "
        "WHERE event.event_id IS NULL "
        "OR typeof(outbox.event_version) <> 'integer' "
        "OR outbox.event_version < 1 OR outbox.event_version > event.version "
        "OR (COALESCE(outbox.event_mapping_id, '') <> '' "
        "AND mapping.mapping_id IS NULL)"
    ).fetchone()
    return int(unresolved[0] or 0)


def verify_event_core_backfill_state(conn: sqlite3.Connection) -> None:
    """Verify legacy-table event references and migration markers read-only.

    This intentionally checks only the small compatibility boundary used on
    every startup and restore drill.  The offline migration performs the much
    broader historical validation before it records completion.
    """
    required_columns = {
        "app_meta": {"k", "v"},
        "events": {"event_id", "version"},
        "news_cache": {"event_id", "event_version"},
        "backtest_signals": {"event_id", "event_version"},
    }
    try:
        for table, expected in required_columns.items():
            present = {
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
            missing = expected.difference(present)
            if missing:
                raise EventCoreSchemaError(
                    f"event-core backfill verification requires {table} columns: "
                    + ", ".join(sorted(missing))
                )

        schema_row = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?",
            (EVENT_CORE_SCHEMA_META_KEY,),
        ).fetchone()
        if schema_row is None or str(schema_row[0]) != str(EVENT_CORE_SCHEMA_VERSION):
            raise EventCoreSchemaError(
                f"event-core schema marker must be {EVENT_CORE_SCHEMA_VERSION}"
            )

        backfill_row = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?",
            (EVENT_CORE_BACKFILL_META_KEY,),
        ).fetchone()
        backfill_marker = "" if backfill_row is None else str(backfill_row[0])
        if not re.fullmatch(r"0*[1-9][0-9]*", backfill_marker):
            raise EventCoreSchemaError(
                "event-core backfill marker must be a positive integer"
            )

        for table in ("news_cache", "backtest_signals"):
            invalid = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" AS source '
                "LEFT JOIN events AS event ON event.event_id = source.event_id "
                "WHERE source.event_id IS NULL "
                "OR TRIM(CAST(source.event_id AS TEXT)) = '' "
                "OR event.event_id IS NULL "
                "OR typeof(source.event_version) <> 'integer' "
                "OR source.event_version < 1 "
                "OR typeof(event.version) <> 'integer' "
                "OR source.event_version > event.version"
            ).fetchone()
            invalid_count = int(invalid[0]) if invalid is not None else 0
            if invalid_count:
                raise EventCoreSchemaError(
                    f"{table} contains {invalid_count} invalid event reference(s)"
                )
        unresolved_outbox = count_unresolved_event_core_outbox_references(conn)
        if unresolved_outbox:
            raise EventCoreSchemaError(
                "integration_signal_outbox contains "
                f"{unresolved_outbox} unresolved stable event reference(s)"
            )
    except EventCoreSchemaError:
        raise
    except sqlite3.DatabaseError as exc:
        raise EventCoreSchemaError(
            "event-core backfill state cannot be verified"
        ) from exc


def _event_row(conn: sqlite3.Connection, event_id: str) -> tuple[Any, ...]:
    normalized = _text(event_id, "event_id", required=True, max_length=36)
    row = conn.execute(
        "SELECT event_id, title, event_type, direction, priority, occurred_at, "
        "version, merge_state, merged_into, strategy_version, model_version, "
        "created_at, updated_at, first_seen_at, last_seen_at, available_at, "
        "merge_contract "
        "FROM events WHERE event_id = ?",
        (normalized,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown event_id: {normalized}")
    return tuple(row)


def canonical_event_id(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    max_depth: int = 64,
) -> str:
    """Resolve an event through its audited merge chain, detecting corruption."""
    current = _text(event_id, "event_id", required=True, max_length=36)
    depth_limit = _integer(max_depth, "max_depth", minimum=1)
    seen: set[str] = set()
    for _depth in range(depth_limit):
        if current in seen:
            raise EventCoreConflictError("cycle in event merge chain")
        seen.add(current)
        row = conn.execute(
            "SELECT merge_state, merged_into FROM events WHERE event_id = ?",
            (current,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown event_id: {current}")
        state, target = str(row[0]), row[1]
        if state == "active" and target is None:
            return current
        if state != "merged" or not target:
            raise EventCoreConflictError(f"invalid merge state for event {current}")
        current = str(target)
    raise EventCoreConflictError("event merge chain exceeds maximum depth")


def canonical_event_group(conn: sqlite3.Connection, event_id: str) -> tuple[str, ...]:
    """Return the canonical root followed by every event merged into its tree."""
    root = canonical_event_id(conn, event_id)
    rows = conn.execute(
        "WITH RECURSIVE event_group(event_id) AS ("
        "  SELECT ? "
        "  UNION "
        "  SELECT events.event_id FROM events "
        "  JOIN event_group ON events.merged_into = event_group.event_id"
        ") SELECT event_id FROM event_group "
        "ORDER BY CASE WHEN event_id = ? THEN 0 ELSE 1 END, event_id",
        (root, root),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _merged_descendant_max_priority(
    conn: sqlite3.Connection, root_event_id: str
) -> float | None:
    """Return the highest persisted priority below one canonical root.

    Merged member rows are immutable evidence of the score that caused that
    source event to enter the group.  Replaying any identity in the group must
    not let the canonical root fall below that evidence, while an unmerged
    event remains free to be rescored downward.
    """
    row = conn.execute(
        "WITH RECURSIVE descendants(event_id) AS ("
        " SELECT event_id FROM events WHERE merged_into = ?"
        " UNION "
        " SELECT child.event_id FROM events AS child "
        " JOIN descendants AS parent ON child.merged_into = parent.event_id"
        ") SELECT MAX(events.priority) FROM events "
        "WHERE events.event_id IN (SELECT event_id FROM descendants)",
        (root_event_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def reconcile_event_group_priorities(
    conn: sqlite3.Connection, *, now: int | None = None
) -> int:
    """Repair legacy canonical roots that are below a merged member score.

    The operation is idempotent and only raises a root to the maximum already
    present in its audited merge tree.  Each changed root receives one version
    increment so cache consumers can observe the repair normally.
    """
    current_time = _now(now)
    with _atomic(conn):
        rows = conn.execute(
            "WITH RECURSIVE event_group(root_id, member_id) AS ("
            " SELECT event_id, event_id FROM events WHERE merge_state = 'active'"
            " UNION "
            " SELECT parent.root_id, child.event_id FROM events AS child "
            " JOIN event_group AS parent ON child.merged_into = parent.member_id"
            ") SELECT root.event_id, MAX(member.priority), root.updated_at "
            "FROM event_group AS grouped "
            "JOIN events AS root ON root.event_id = grouped.root_id "
            "JOIN events AS member ON member.event_id = grouped.member_id "
            "GROUP BY root.event_id "
            "HAVING MAX(member.priority) > root.priority"
        ).fetchall()
        for root_id, max_priority, updated_at in rows:
            conn.execute(
                "UPDATE events SET priority = ?, version = version + 1, "
                "updated_at = ? WHERE event_id = ? AND priority < ?",
                (
                    float(max_priority),
                    max(current_time, int(updated_at)),
                    str(root_id),
                    float(max_priority),
                ),
            )
        return len(rows)


def resolve_or_create_event(
    conn: sqlite3.Connection,
    identity_keys: Mapping[object, object]
    | Iterable[tuple[object, object] | str]
    | str,
    *,
    title: str,
    event_type: str = "other",
    direction: str = "neutral",
    priority: float = 5.0,
    occurred_at: int | None = None,
    available_at: int | None = None,
    strategy_version: str = "",
    model_version: str = "",
    now: int | None = None,
) -> str:
    """Resolve external identities or create one UUID event.

    If supplied identities point at different canonical events, no row is
    changed.  Repeating identical input is a no-op; changed event metadata (or
    a newly attached identity) increments ``events.version`` exactly once.
    Observation time advances ``last_seen_at`` without changing that version;
    an explicitly supplied ``available_at`` can only move forward.
    """
    identities = _normalize_identity_keys(identity_keys)
    title_value = _text(title, "title", required=True, max_length=1_000)
    type_value = _text(event_type, "event_type", required=True, max_length=64)
    direction_value = _text(direction, "direction", required=True, max_length=64)
    priority_value = _real(priority, "priority", 0, 10)
    strategy_value = _text(strategy_version, "strategy_version", max_length=255)
    model_value = _text(model_version, "model_version", max_length=255)
    current_time = _now(now)
    occurred_value = (
        None if occurred_at is None else _timestamp(occurred_at, "occurred_at")
    )
    available_value = (
        None if available_at is None else _timestamp(available_at, "available_at")
    )

    with _atomic(conn):
        placeholders = ",".join("(?, ?)" for _ in identities)
        flat: list[str] = []
        for identity_type, identity_key in identities:
            flat.extend((identity_type, identity_key))
        matched = conn.execute(
            "SELECT identity_type, identity_key, event_id FROM event_identities "
            f"WHERE (identity_type, identity_key) IN ({placeholders})",
            flat,
        ).fetchall()
        canonical_ids = {canonical_event_id(conn, str(row[2])) for row in matched}
        if len(canonical_ids) > 1:
            raise EventCoreConflictError(
                "identity keys resolve to different canonical events"
            )

        if not canonical_ids:
            event_id = _new_uuid()
            create_occurred_at = current_time if occurred_value is None else occurred_value
            create_available_at = (
                current_time if available_value is None else available_value
            )
            # ``now`` is the first trustworthy observation.  A caller may
            # delay availability, but cannot backdate knowledge before that
            # observation and manufacture a look-ahead-safe history.
            create_first_seen_at = current_time
            create_available_at = max(current_time, create_available_at)
            create_last_seen_at = create_available_at
            conn.execute(
                "INSERT INTO events (event_id, title, event_type, direction, priority, "
                "occurred_at, version, merge_state, merged_into, strategy_version, "
                "model_version, created_at, updated_at, first_seen_at, last_seen_at, "
                "available_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'active', NULL, ?, ?, ?, ?, "
                "?, ?, ?)",
                (
                    event_id, title_value, type_value, direction_value, priority_value,
                    create_occurred_at, strategy_value, model_value, current_time, current_time,
                    create_first_seen_at, create_last_seen_at, create_available_at,
                ),
            )
            for identity_type, identity_key in identities:
                conn.execute(
                    "INSERT INTO event_identities "
                    "(identity_type, identity_key, event_id, created_at) VALUES (?, ?, ?, ?)",
                    (identity_type, identity_key, event_id, current_time),
                )
            return event_id

        event_id = canonical_ids.pop()
        row = _event_row(conn, event_id)
        existing_identities = {(str(item[0]), str(item[1])) for item in matched}
        new_identities = [item for item in identities if item not in existing_identities]
        descendant_priority = _merged_descendant_max_priority(conn, event_id)
        if descendant_priority is not None:
            priority_value = max(priority_value, descendant_priority)
        desired_occurred_at = int(row[5]) if occurred_value is None else occurred_value
        desired = (
            title_value, type_value, direction_value, priority_value,
            desired_occurred_at, strategy_value, model_value,
        )
        existing = (
            str(row[1]), str(row[2]), str(row[3]), float(row[4]), int(row[5]),
            str(row[9]), str(row[10]),
        )
        desired_available = max(
            int(row[15]),
            int(row[15]) if available_value is None else available_value,
        )
        desired_last_seen = max(int(row[14]), current_time, desired_available)
        changed = (
            desired != existing
            or bool(new_identities)
            or desired_available != int(row[15])
        )
        updated_at = max(current_time, int(row[12]))
        if changed:
            conn.execute(
                "UPDATE events SET title = ?, event_type = ?, direction = ?, priority = ?, "
                "occurred_at = ?, strategy_version = ?, model_version = ?, "
                "available_at = ?, last_seen_at = ?, version = version + 1, "
                "updated_at = ? WHERE event_id = ?",
                (*desired, desired_available, desired_last_seen, updated_at, event_id),
            )
        elif desired_last_seen != int(row[14]):
            conn.execute(
                "UPDATE events SET last_seen_at = ?, updated_at = ? WHERE event_id = ?",
                (desired_last_seen, updated_at, event_id),
            )
        for identity_type, identity_key in new_identities:
            conn.execute(
                "INSERT INTO event_identities "
                "(identity_type, identity_key, event_id, created_at) VALUES (?, ?, ?, ?)",
                (identity_type, identity_key, event_id, current_time),
            )
        return event_id


def merge_events(
    conn: sqlite3.Connection,
    source_event_id: str,
    target_event_id: str,
    *,
    reason: str = "",
    actor: str = "system",
    now: int | None = None,
) -> str:
    """Merge one active source into the canonical target and audit it."""
    source_id = _text(source_event_id, "source_event_id", required=True, max_length=36)
    requested_target = _text(
        target_event_id, "target_event_id", required=True, max_length=36
    )
    reason_value = _text(reason, "reason", max_length=2_000)
    actor_value = _text(actor, "actor", required=True, max_length=255)
    current_time = _now(now)

    with _atomic(conn):
        source = _event_row(conn, source_id)
        target_id = canonical_event_id(conn, requested_target)
        if source_id == target_id:
            if str(source[7]) == "merged":
                return target_id
            raise EventCoreConflictError("an event cannot be merged into itself")

        if str(source[7]) == "merged":
            existing_target = canonical_event_id(conn, source_id)
            if existing_target == target_id:
                return target_id
            raise EventCoreConflictError(
                "source event is already merged into a different canonical event"
            )
        if str(source[7]) != "active" or source[8] is not None:
            raise EventCoreConflictError("source event is not active")

        target = _event_row(conn, target_id)
        if str(target[7]) != "active" or target[8] is not None:
            raise EventCoreConflictError("target event is not active")
        source_version = int(source[6]) + 1
        target_version = int(target[6]) + 1
        source_updated = max(current_time, int(source[12]))
        target_updated = max(current_time, int(target[12]))
        target_available = max(int(source[15]), int(target[15]))
        target_last_seen = max(
            int(source[14]), int(target[14]), target_available
        )
        merged_priority = max(
            float(target[4]),
            float(source[4]),
            _merged_descendant_max_priority(conn, target_id) or float(target[4]),
        )
        conn.execute(
            "UPDATE events SET priority = ?, version = ?, updated_at = ?, "
            "available_at = ?, last_seen_at = ? WHERE event_id = ?",
            (
                merged_priority,
                target_version,
                target_updated,
                target_available,
                target_last_seen,
                target_id,
            ),
        )
        conn.execute(
            "INSERT INTO event_merge_history "
            "(source_event_id, target_event_id, source_version, target_version, "
            "reason, actor, merged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                source_id, target_id, source_version, target_version,
                reason_value, actor_value, current_time,
            ),
        )
        conn.execute(
            "UPDATE events SET merge_state = 'merged', merged_into = ?, version = ?, "
            "updated_at = ?, merge_contract = ? WHERE event_id = ?",
            (
                target_id,
                source_version,
                source_updated,
                EVENT_MERGE_CONTRACT,
                source_id,
            ),
        )
        return target_id


def upsert_security(
    conn: sqlite3.Connection,
    *,
    exchange: str,
    ticker: str,
    name: str,
    security_id: str | None = None,
    asset_type: str = "equity",
    currency: str = "CNY",
    aliases: Iterable[str] | str | None = None,
    industry: str = "",
    concepts: Iterable[str] | str | None = None,
    is_active: bool = True,
    now: int | None = None,
) -> str:
    """Create or update a security by its unique exchange/ticker identity."""
    exchange_value = _text(exchange, "exchange", required=True, max_length=32).upper()
    ticker_value = _text(ticker, "ticker", required=True, max_length=32).upper()
    name_value = _text(name, "name", required=True, max_length=255)
    asset_value = _text(asset_type, "asset_type", required=True, max_length=64)
    currency_value = _text(currency, "currency", required=True, max_length=16).upper()
    aliases_value = _string_list_json(aliases, "aliases")
    industry_value = _text(industry, "industry", max_length=255)
    concepts_value = _string_list_json(concepts, "concepts")
    active_value = _flag(is_active, "is_active")
    supplied_id = (
        None
        if security_id is None
        else _text(security_id, "security_id", required=True, max_length=128)
    )
    current_time = _now(now)

    with _atomic(conn):
        natural = conn.execute(
            "SELECT security_id, name, asset_type, currency, aliases, industry, concepts, "
            "is_active, version, updated_at "
            "FROM securities WHERE exchange = ? AND ticker = ?",
            (exchange_value, ticker_value),
        ).fetchone()
        by_id = None
        if supplied_id is not None:
            by_id = conn.execute(
                "SELECT exchange, ticker FROM securities WHERE security_id = ?",
                (supplied_id,),
            ).fetchone()
        if natural is not None and supplied_id is not None and str(natural[0]) != supplied_id:
            raise EventCoreConflictError(
                "exchange/ticker already belongs to another security_id"
            )
        if by_id is not None and (
            str(by_id[0]) != exchange_value or str(by_id[1]) != ticker_value
        ):
            raise EventCoreConflictError(
                "security_id already belongs to another exchange/ticker"
            )

        if natural is None:
            resolved_id = supplied_id or _new_uuid()
            conn.execute(
                "INSERT INTO securities "
                "(security_id, exchange, ticker, name, asset_type, currency, aliases, "
                "industry, concepts, is_active, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    resolved_id, exchange_value, ticker_value, name_value, asset_value,
                    currency_value, aliases_value, industry_value, concepts_value,
                    active_value, current_time, current_time,
                ),
            )
            return resolved_id

        resolved_id = str(natural[0])
        desired = (
            name_value, asset_value, currency_value, aliases_value,
            industry_value, concepts_value, active_value,
        )
        existing = (
            str(natural[1]), str(natural[2]), str(natural[3]), str(natural[4]),
            str(natural[5]), str(natural[6]), int(natural[7]),
        )
        if desired != existing:
            updated_at = max(current_time, int(natural[9]))
            conn.execute(
                "UPDATE securities SET name = ?, asset_type = ?, currency = ?, "
                "aliases = ?, industry = ?, concepts = ?, is_active = ?, "
                "version = version + 1, updated_at = ? "
                "WHERE security_id = ?",
                (*desired, updated_at, resolved_id),
            )
        return resolved_id


def _require_security(conn: sqlite3.Connection, security_id: str) -> str:
    value = _text(security_id, "security_id", required=True, max_length=128)
    if conn.execute(
        "SELECT 1 FROM securities WHERE security_id = ?", (value,)
    ).fetchone() is None:
        raise KeyError(f"unknown security_id: {value}")
    return value


def upsert_event_security(
    conn: sqlite3.Connection,
    event_id: str,
    security_id: str,
    *,
    relation_type: str = "affected",
    direction: str = "neutral",
    expected_gap: float | None = None,
    confidence: float = 0.0,
    relevance: float = 0.0,
    rationale: str = "",
    mapping_version: str = "",
    now: int | None = None,
) -> str:
    """Append a mapping revision, or return the identical current revision.

    Payload changes never overwrite an earlier mapping.  Only the prior row's
    ``is_current`` marker is retired before a new immutable revision is added.
    The returned UUID identifies the frozen mapping revision.
    """
    relation_value = _text(
        relation_type, "relation_type", required=True, max_length=64
    )
    direction_value = _text(direction, "direction", required=True, max_length=64)
    expected_gap_value = _optional_real(expected_gap, "expected_gap", -100, 100)
    confidence_value = _real(confidence, "confidence", 0, 1)
    relevance_value = _real(relevance, "relevance", 0, 10)
    rationale_value = _text(rationale, "rationale", max_length=4_000)
    mapping_value = _text(mapping_version, "mapping_version", max_length=255)
    _require_event_mapping_connection_write_authorized(conn, mapping_value)
    current_time = _now(now)

    with _atomic(conn):
        canonical_id = canonical_event_id(conn, event_id)
        security_value = _require_security(conn, security_id)
        row = conn.execute(
            "SELECT mapping_id, direction, expected_gap, confidence, relevance, "
            "rationale, mapping_version, revision FROM event_securities "
            "WHERE event_id = ? AND security_id = ? AND relation_type = ? "
            "AND is_current = 1",
            (canonical_id, security_value, relation_value),
        ).fetchone()
        desired = (
            direction_value, expected_gap_value, confidence_value, relevance_value,
            rationale_value, mapping_value,
        )
        if row is None:
            latest_revision = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM event_securities "
                "WHERE event_id = ? AND security_id = ? AND relation_type = ?",
                (canonical_id, security_value, relation_value),
            ).fetchone()
            next_revision = int(latest_revision[0]) + 1
            mapping_id = _new_uuid()
            conn.execute(
                "INSERT INTO event_securities "
                "(mapping_id, event_id, security_id, relation_type, direction, expected_gap, "
                "confidence, relevance, rationale, mapping_version, revision, is_current, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    mapping_id, canonical_id, security_value, relation_value,
                    *desired, next_revision, current_time,
                ),
            )
            return mapping_id

        existing = (
            str(row[1]),
            None if row[2] is None else float(row[2]),
            float(row[3]),
            float(row[4]),
            str(row[5]),
            str(row[6]),
        )
        if desired == existing:
            return str(row[0])
        previous_mapping_id = str(row[0])
        next_revision = int(row[7]) + 1
        mapping_id = _new_uuid()
        conn.execute(
            "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
            (previous_mapping_id,),
        )
        conn.execute(
            "INSERT INTO event_securities "
            "(mapping_id, event_id, security_id, relation_type, direction, expected_gap, "
            "confidence, relevance, rationale, mapping_version, revision, is_current, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                mapping_id, canonical_id, security_value, relation_value,
                *desired, next_revision, current_time,
            ),
        )
        return mapping_id


def _mapping_row(conn: sqlite3.Connection, mapping_id: str) -> tuple[str, str, int]:
    value = _text(mapping_id, "mapping_id", required=True, max_length=36)
    row = conn.execute(
        "SELECT event_id, security_id, revision FROM event_securities WHERE mapping_id = ?",
        (value,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown mapping_id: {value}")
    return str(row[0]), str(row[1]), int(row[2])


def upsert_event_evidence(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    source_uri: str,
    claim_key: str,
    claim_text: str,
    evidence_id: str | None = None,
    security_id: str | None = None,
    mapping_id: str | None = None,
    source_type: str = "article",
    source_name: str = "",
    excerpt: str = "",
    snapshot_hash: str | None = None,
    snapshot_text: str | None = None,
    stance: str = "context",
    reliability: float = 0.5,
    announcement_at: int | None = None,
    observed_at: int | None = None,
    now: int | None = None,
) -> str:
    """Upsert a claim/source observation with an optional frozen mapping link."""
    source_value = _text(source_uri, "source_uri", required=True, max_length=2_048)
    try:
        parsed_source = urlsplit(source_value)
    except ValueError as exc:
        raise ValueError("source_uri must be a valid HTTP(S) URL") from exc
    if (
        parsed_source.scheme.lower() not in {"http", "https"}
        or not parsed_source.hostname
        or parsed_source.username is not None
        or parsed_source.password is not None
    ):
        raise ValueError("source_uri must be a credential-free HTTP(S) URL")
    claim_key_value = _text(claim_key, "claim_key", required=True, max_length=512)
    claim_text_value = _text(claim_text, "claim_text", required=True, max_length=4_000)
    type_value = _text(source_type, "source_type", required=True, max_length=64)
    name_value = _text(source_name, "source_name", max_length=255)
    excerpt_value = _text(excerpt, "excerpt", max_length=8_000)
    snapshot_hash_value, snapshot_text_value = _snapshot_values(
        snapshot_hash, snapshot_text
    )
    stance_value = _text(stance, "stance", required=True, max_length=16)
    if stance_value not in {"supports", "contradicts", "context"}:
        raise ValueError("stance must be supports, contradicts, or context")
    reliability_value = _real(reliability, "reliability", 0, 1)
    announcement_value = (
        None
        if announcement_at is None
        else _timestamp(announcement_at, "announcement_at")
    )
    current_time = _now(now)
    observed_value = _timestamp(observed_at, "observed_at", default=current_time)
    supplied_id = (
        None
        if evidence_id is None
        else _text(evidence_id, "evidence_id", required=True, max_length=36)
    )
    if supplied_id is not None:
        try:
            supplied_id = str(uuid.UUID(supplied_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("evidence_id must be a UUID") from exc

    with _atomic(conn):
        canonical_id = canonical_event_id(conn, event_id)
        mapping_value: str | None = None
        mapping_revision: int | None = None
        security_value: str | None = None
        stored_event_id = canonical_id
        if mapping_id is not None:
            mapping_value = _text(mapping_id, "mapping_id", required=True, max_length=36)
            mapping_event, mapping_security, mapping_revision = _mapping_row(
                conn, mapping_value
            )
            if canonical_event_id(conn, mapping_event) != canonical_id:
                raise EventCoreConflictError("mapping belongs to another canonical event")
            if security_id is not None and _require_security(conn, security_id) != mapping_security:
                raise EventCoreConflictError("mapping belongs to another security")
            stored_event_id = mapping_event
            security_value = mapping_security
        elif security_id is not None:
            security_value = _require_security(conn, security_id)

        natural = conn.execute(
            "SELECT evidence_id, security_id, mapping_id, mapping_revision, source_type, "
            "source_name, claim_text, excerpt, snapshot_hash, snapshot_text, stance, "
            "reliability, announcement_at, observed_at, version, updated_at "
            "FROM event_evidence WHERE event_id = ? AND source_uri = ? AND claim_key = ?",
            (stored_event_id, source_value, claim_key_value),
        ).fetchone()
        by_id = None
        if supplied_id is not None:
            by_id = conn.execute(
                "SELECT event_id, source_uri, claim_key FROM event_evidence "
                "WHERE evidence_id = ?",
                (supplied_id,),
            ).fetchone()
        if natural is not None and supplied_id is not None and str(natural[0]) != supplied_id:
            raise EventCoreConflictError("evidence natural key belongs to another evidence_id")
        if by_id is not None and (
            str(by_id[0]), str(by_id[1]), str(by_id[2])
        ) != (stored_event_id, source_value, claim_key_value):
            raise EventCoreConflictError("evidence_id belongs to another evidence record")

        if natural is None:
            resolved_id = supplied_id or _new_uuid()
            conn.execute(
                "INSERT INTO event_evidence "
                "(evidence_id, event_id, security_id, mapping_id, mapping_revision, source_uri, "
                "source_type, source_name, claim_key, claim_text, excerpt, snapshot_hash, "
                "snapshot_text, stance, reliability, announcement_at, observed_at, version, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, 1, ?, ?)",
                (
                    resolved_id, stored_event_id, security_value, mapping_value,
                    mapping_revision, source_value, type_value, name_value,
                    claim_key_value, claim_text_value, excerpt_value, snapshot_hash_value,
                    snapshot_text_value, stance_value, reliability_value,
                    announcement_value, observed_value, current_time, current_time,
                ),
            )
            return resolved_id

        resolved_id = str(natural[0])
        existing_security = None if natural[1] is None else str(natural[1])
        existing_mapping = None if natural[2] is None else str(natural[2])
        existing_revision = None if natural[3] is None else int(natural[3])
        if (existing_security, existing_mapping, existing_revision) != (
            security_value, mapping_value, mapping_revision
        ):
            raise EventCoreConflictError(
                "evidence security/mapping identity cannot be rebound"
            )
        existing_observed_at = int(natural[13])
        if observed_value < existing_observed_at:
            raise EventCoreConflictError("evidence observed_at cannot move backward")
        existing_snapshot = (str(natural[8]), str(natural[9]))
        if (snapshot_hash_value, snapshot_text_value) != existing_snapshot:
            raise EventCoreConflictError(
                "evidence snapshot is immutable; use a new claim_key"
            )
        desired = (
            type_value, name_value, claim_text_value, excerpt_value,
            snapshot_hash_value, snapshot_text_value, stance_value, reliability_value,
            announcement_value, observed_value,
        )
        existing = (
            str(natural[4]), str(natural[5]), str(natural[6]), str(natural[7]),
            str(natural[8]), str(natural[9]), str(natural[10]), float(natural[11]),
            natural[12], int(natural[13]),
        )
        if desired != existing:
            conn.execute(
                "UPDATE event_evidence SET source_type = ?, source_name = ?, claim_text = ?, "
                "excerpt = ?, snapshot_hash = ?, snapshot_text = ?, stance = ?, "
                "reliability = ?, announcement_at = ?, observed_at = ?, "
                "version = version + 1, updated_at = ? WHERE evidence_id = ?",
                (
                    *desired, max(current_time, int(natural[15])), resolved_id,
                ),
            )
        return resolved_id


def record_event_mapping_replay(
    conn: sqlite3.Connection,
    evidence_id: str,
    mapping_version: str,
    *,
    evidence_version: int,
    root_event_id: str,
    root_event_version: int,
    input_fingerprint: str,
    output_fingerprint: str,
    status: str,
    mapped_count: int = 0,
    failure_code: str = "",
    now: int | None = None,
) -> int:
    """Record one versioned deterministic mapping attempt durably.

    Successful and no-match results are terminal for an immutable evidence
    snapshot under one mapping implementation.  Failed rows may be retried and
    advance ``attempts`` without allowing a completed result to be rewritten.
    The returned value is the durable attempt count.
    """
    evidence_value = _text(
        evidence_id, "evidence_id", required=True, max_length=36
    )
    version_value = _text(
        mapping_version, "mapping_version", required=True, max_length=255
    )
    if not version_value.startswith("asset-map:"):
        raise ValueError("mapping_version must be an asset-map version")
    _require_event_mapping_connection_write_authorized(conn, version_value)
    evidence_version_value = _integer(
        evidence_version, "evidence_version", minimum=1
    )
    root_value = _text(
        root_event_id, "root_event_id", required=True, max_length=36
    )
    root_version_value = _integer(
        root_event_version, "root_event_version", minimum=1
    )
    fingerprint_value = _text(
        input_fingerprint, "input_fingerprint", required=True, max_length=64
    ).casefold()
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint_value) is None:
        raise ValueError("input_fingerprint must be a lowercase SHA-256 digest")
    output_fingerprint_value = _text(
        output_fingerprint,
        "output_fingerprint",
        required=True,
        max_length=64,
    ).casefold()
    if re.fullmatch(r"[0-9a-f]{64}", output_fingerprint_value) is None:
        raise ValueError("output_fingerprint must be a lowercase SHA-256 digest")
    status_value = _text(status, "status", required=True, max_length=16)
    if status_value not in {"mapped", "no_match", "failed"}:
        raise ValueError("status must be mapped, no_match, or failed")
    count_value = _integer(mapped_count, "mapped_count", minimum=0)
    failure_value = _text(failure_code, "failure_code", max_length=64)
    if status_value == "mapped":
        if count_value < 1 or failure_value:
            raise ValueError("mapped replay requires mappings and no failure code")
    elif status_value == "no_match":
        if count_value or failure_value:
            raise ValueError("no_match replay cannot contain mappings or a failure code")
    elif count_value or not failure_value:
        raise ValueError("failed replay requires a failure code and no mappings")
    current_time = _now(now)

    with _atomic(conn):
        evidence_row = conn.execute(
            "SELECT version FROM event_evidence WHERE evidence_id = ?",
            (evidence_value,),
        ).fetchone()
        if evidence_row is None:
            raise KeyError(f"unknown evidence_id: {evidence_value}")
        if int(evidence_row[0]) != evidence_version_value:
            raise EventCoreConflictError("evidence changed before mapping replay commit")
        if canonical_event_id(conn, root_value) != root_value:
            raise EventCoreConflictError("mapping replay root is no longer canonical")
        root_row = conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (root_value,)
        ).fetchone()
        if root_row is None or int(root_row[0]) != root_version_value:
            raise EventCoreConflictError("canonical event changed before mapping replay commit")
        previous = conn.execute(
            "SELECT status, mapped_count, attempts, failure_code, processed_at, "
            "evidence_version, root_event_id, root_event_version, output_fingerprint "
            "FROM event_mapping_replay_ledger "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (evidence_value, version_value, fingerprint_value),
        ).fetchone()
        if previous is not None and (
            int(previous[5]), str(previous[6]), int(previous[7])
        ) != (evidence_version_value, root_value, root_version_value):
            raise EventCoreConflictError(
                "mapping replay fingerprint collides with different inputs"
            )
        if previous is not None and str(previous[0]) in {"mapped", "no_match"}:
            desired = (
                status_value,
                count_value,
                failure_value,
                output_fingerprint_value,
            )
            existing = (
                str(previous[0]),
                int(previous[1]),
                str(previous[3]),
                str(previous[8]),
            )
            if desired != existing:
                raise EventCoreConflictError(
                    "terminal event mapping replay conflicts with persisted result"
                )
            return int(previous[2])

        if previous is None:
            attempts = 1
            processed_at = current_time
            conn.execute(
                "INSERT INTO event_mapping_replay_ledger "
                "(evidence_id, evidence_version, root_event_id, root_event_version, "
                "mapping_version, input_fingerprint, output_fingerprint, status, mapped_count, attempts, "
                "failure_code, processed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_value,
                    evidence_version_value,
                    root_value,
                    root_version_value,
                    version_value,
                    fingerprint_value,
                    output_fingerprint_value,
                    status_value,
                    count_value,
                    attempts,
                    failure_value,
                    processed_at,
                    current_time,
                ),
            )
            return attempts

        attempts = int(previous[2]) + 1
        processed_at = max(int(previous[4]), current_time)
        conn.execute(
            "UPDATE event_mapping_replay_ledger SET output_fingerprint = ?, "
            "status = ?, mapped_count = ?, "
            "attempts = ?, failure_code = ?, processed_at = ?, updated_at = ? "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (
                output_fingerprint_value,
                status_value,
                count_value,
                attempts,
                failure_value,
                processed_at,
                processed_at,
                evidence_value,
                version_value,
                fingerprint_value,
            ),
        )
        return attempts


def upsert_event_outcome(
    conn: sqlite3.Connection,
    event_id: str,
    security_id: str,
    *,
    mapping_id: str,
    horizon: str,
    baseline_at: int,
    measured_at: int,
    baseline_price: float | None = None,
    gross_return: float | None = None,
    benchmark_return: float | None = None,
    excess_return: float | None = None,
    mfe: float | None = None,
    mae: float | None = None,
    direction_hit: bool | None = None,
    benchmark_security_id: str | None = None,
    signal_ledger_id: int | None = None,
    signal_available_at: int | None = None,
    observation_state: str = "legacy_unknown",
    method_version: str,
    data_version: str,
    data_quality: float = 1.0,
    outcome_id: str | None = None,
    now: int | None = None,
) -> str:
    """Append a posterior revision tied to one frozen mapping revision.

    Missing market observations remain SQL ``NULL``; this API never substitutes
    zeroes for unavailable prices or returns.  Identical payloads are no-ops.
    """
    horizon_value = _text(horizon, "horizon", required=True, max_length=64)
    baseline_value = _timestamp(baseline_at, "baseline_at")
    measured_value = _timestamp(measured_at, "measured_at")
    if measured_value < baseline_value:
        raise ValueError("measured_at must be >= baseline_at")
    if signal_ledger_id is None:
        signal_ledger_value = None
    elif isinstance(signal_ledger_id, bool) or not isinstance(signal_ledger_id, int):
        raise ValueError("signal_ledger_id must be a positive integer")
    elif signal_ledger_id < 1:
        raise ValueError("signal_ledger_id must be a positive integer")
    else:
        signal_ledger_value = signal_ledger_id
    signal_available_value = (
        None
        if signal_available_at is None
        else _timestamp(signal_available_at, "signal_available_at")
    )
    if signal_available_value is not None:
        if signal_available_value <= 0:
            raise ValueError("signal_available_at must be positive")
        if baseline_value <= signal_available_value:
            raise ValueError("baseline_at must be strictly after signal_available_at")
    state_value = _text(
        observation_state,
        "observation_state",
        required=True,
        max_length=32,
    )
    if state_value not in {
        "legacy_unknown",
        "not_due",
        "missing_asset_close",
        "missing_benchmark_close",
        "complete",
    }:
        raise ValueError("observation_state is unsupported")
    if (signal_ledger_value is None) != (signal_available_value is None):
        raise ValueError(
            "signal_ledger_id and signal_available_at must be supplied together"
        )
    if state_value == "complete" and signal_available_value is None:
        raise ValueError(
            "complete observations require frozen signal ledger metadata"
        )
    baseline_price_value = _optional_real(
        baseline_price, "baseline_price", 0.000000000001, 1_000_000_000_000
    )
    gross_value = _optional_real(gross_return, "gross_return", -1, 1_000_000)
    benchmark_return_value = _optional_real(
        benchmark_return, "benchmark_return", -1_000_000, 1_000_000
    )
    excess_value = _optional_real(
        excess_return, "excess_return", -1_000_000, 1_000_000
    )
    mfe_value = _optional_real(mfe, "mfe", 0, 1_000_000)
    mae_value = _optional_real(mae, "mae", -1, 0)
    hit_value = _flag(direction_hit, "direction_hit", optional=True)
    method_value = _text(
        method_version, "method_version", required=True, max_length=255
    )
    data_value = _text(
        data_version, "data_version", required=True, max_length=255
    )
    quality_value = _real(data_quality, "data_quality", 0, 1)
    current_time = _now(now)
    supplied_id = (
        None
        if outcome_id is None
        else _text(outcome_id, "outcome_id", required=True, max_length=36)
    )
    if supplied_id is not None:
        try:
            supplied_id = str(uuid.UUID(supplied_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("outcome_id must be a UUID") from exc

    with _atomic(conn):
        canonical_id = canonical_event_id(conn, event_id)
        security_value = _require_security(conn, security_id)
        mapping_value = _text(mapping_id, "mapping_id", required=True, max_length=36)
        mapping_event, mapping_security, mapping_revision = _mapping_row(
            conn, mapping_value
        )
        if canonical_event_id(conn, mapping_event) != canonical_id:
            raise EventCoreConflictError("mapping belongs to another canonical event")
        if mapping_security != security_value:
            raise EventCoreConflictError("mapping belongs to another security")
        benchmark_value = (
            None
            if benchmark_security_id is None
            else _require_security(conn, benchmark_security_id)
        )
        if signal_ledger_value is not None:
            # ``event_core.py`` is also loaded as a standalone schema verifier by
            # the content-addressed restore-drill bundle.  Keep the application-
            # only provenance dependency at the outcome-write boundary so that
            # the isolated verifier remains self-contained.
            from scoring_provenance import (
                FORMAL_SIGNAL_SCORE_PATHS,
                normalize_score_path,
            )

            try:
                frozen = conn.execute(
                    "SELECT signal.event_id AS signal_event_id, signal.available_at, "
                    "signal.recorded_at, signal.selected, signal.is_similarity_copy, "
                    "signal.score_path, "
                    "signal.event_version, signal.mapping_version AS signal_mapping_version, "
                    "snapshot.event_id AS frozen_event_id, "
                    "snapshot.security_id AS frozen_security_id, "
                    "snapshot.mapping_revision AS frozen_mapping_revision, "
                    "snapshot.signal_available_at AS frozen_available_at, "
                    "snapshot.captured_at, mapping.mapping_version, "
                    "mapping.created_at AS mapping_created_at "
                    "FROM backtest_signals AS signal "
                    "JOIN backtest_signal_securities AS snapshot "
                    "ON snapshot.ledger_signal_id = signal.id "
                    "JOIN event_securities AS mapping ON mapping.mapping_id = snapshot.mapping_id "
                    "AND mapping.event_id = snapshot.event_id "
                    "AND mapping.security_id = snapshot.security_id "
                    "AND mapping.revision = snapshot.mapping_revision "
                    "WHERE signal.id = ? AND snapshot.mapping_id = ?",
                    (signal_ledger_value, mapping_value),
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise EventCoreConflictError(
                    "frozen signal ledger is unavailable"
                ) from exc
            if frozen is None:
                raise EventCoreConflictError(
                    "outcome does not match a frozen signal mapping"
                )
            frozen_exact = (
                str(frozen["signal_event_id"] or "") == mapping_event
                and str(frozen["frozen_event_id"] or "") == mapping_event
                and str(frozen["frozen_security_id"] or "") == security_value
                and int(frozen["frozen_mapping_revision"] or 0) == mapping_revision
                and int(frozen["available_at"] or 0) == signal_available_value
                and int(frozen["frozen_available_at"] or 0) == signal_available_value
                and int(frozen["recorded_at"] or 0) == int(frozen["captured_at"] or 0)
                and int(frozen["mapping_created_at"] or 0)
                <= int(frozen["captured_at"] or 0)
                and int(frozen["selected"] or 0) == 1
                and int(frozen["is_similarity_copy"] or 0) == 0
                and normalize_score_path(frozen["score_path"])
                in FORMAL_SIGNAL_SCORE_PATHS
                and int(frozen["event_version"] or 0) >= 1
                and str(frozen["signal_mapping_version"] or "")
                == str(frozen["mapping_version"] or "")
            )
            if not frozen_exact:
                raise EventCoreConflictError(
                    "outcome signal metadata conflicts with its frozen mapping"
                )
            if method_value == _FROZEN_OUTCOME_METHOD_VERSION:
                if not data_value.startswith(_FROZEN_OUTCOME_DATA_VERSION_PREFIX):
                    raise EventCoreConflictError(
                        "frozen outcome has an invalid price revision contract"
                    )
                revision_ids = data_value[
                    len(_FROZEN_OUTCOME_DATA_VERSION_PREFIX):
                ].split(":")
                try:
                    canonical_revision_ids = [
                        str(uuid.UUID(revision_id)) for revision_id in revision_ids
                    ]
                except (AttributeError, TypeError, ValueError) as exc:
                    raise EventCoreConflictError(
                        "frozen outcome has an invalid price revision contract"
                    ) from exc
                if len(canonical_revision_ids) != 4 or canonical_revision_ids != revision_ids:
                    raise EventCoreConflictError(
                        "frozen outcome has an invalid price revision contract"
                    )
                placeholders = ", ".join("?" for _ in canonical_revision_ids)
                try:
                    price_revisions = conn.execute(
                        "SELECT revision.revision_id, revision.source_ts, "
                        "revision.recorded_at, current.revision_id "
                        "FROM future_price_revisions AS revision "
                        "LEFT JOIN future_prices AS current "
                        "ON current.symbol = revision.symbol "
                        "AND current.trade_date = revision.trade_date "
                        f"WHERE revision.revision_id IN ({placeholders})",
                        tuple(canonical_revision_ids),
                    ).fetchall()
                except sqlite3.DatabaseError as exc:
                    raise EventCoreConflictError(
                        "frozen outcome price revisions are unavailable"
                    ) from exc
                try:
                    revision_times = {
                        str(row[0]): (int(row[1] or 0), int(row[2]))
                        for row in price_revisions
                    }
                    current_price_revisions = {
                        str(row[0]): str(row[3] or "")
                        for row in price_revisions
                    }
                except (TypeError, ValueError, OverflowError) as exc:
                    raise EventCoreConflictError(
                        "frozen outcome has invalid price revision timestamps"
                    ) from exc
                if set(revision_times) != set(canonical_revision_ids) or any(
                    recorded_at <= 0 or recorded_at > current_time
                    for _, recorded_at in revision_times.values()
                ):
                    raise EventCoreConflictError(
                        "frozen outcome references a missing or future price revision"
                    )
                # This check executes inside the same IMMEDIATE transaction as
                # the current-outcome swap.  A calculation read before a market
                # correction therefore cannot commit after a newer calculation
                # and become current merely because it ran last.
                if any(
                    current_price_revisions.get(revision_id) != revision_id
                    for revision_id in canonical_revision_ids
                ):
                    raise EventCoreConflictError(
                        "frozen outcome price revisions are no longer current"
                    )
                if (
                    baseline_value > current_time
                    or measured_value > current_time
                    or any(
                        source_ts > current_time
                        for source_ts, _ in revision_times.values()
                    )
                ):
                    raise EventCoreConflictError(
                        "frozen outcome references future market data"
                    )
            natural = conn.execute(
                "SELECT outcome_revision_id, outcome_id, baseline_at, measured_at, "
                "baseline_price, gross_return, benchmark_return, excess_return, mfe, mae, "
                "direction_hit, benchmark_security_id, data_quality, revision, data_version, "
                "signal_ledger_id, signal_available_at, observation_state "
                "FROM event_outcomes WHERE signal_ledger_id = ? AND mapping_id = ? "
                "AND horizon = ? AND method_version = ? AND is_current = 1",
                (
                    signal_ledger_value,
                    mapping_value,
                    horizon_value,
                    method_value,
                ),
            ).fetchone()
        else:
            natural = conn.execute(
                "SELECT outcome_revision_id, outcome_id, baseline_at, measured_at, "
                "baseline_price, gross_return, benchmark_return, excess_return, mfe, mae, "
                "direction_hit, benchmark_security_id, data_quality, revision, data_version, "
                "signal_ledger_id, signal_available_at, observation_state "
                "FROM event_outcomes WHERE event_id = ? AND security_id = ? AND mapping_id = ? "
                "AND horizon = ? AND method_version = ? AND signal_ledger_id IS NULL "
                "AND is_current = 1",
                (
                    mapping_event,
                    security_value,
                    mapping_value,
                    horizon_value,
                    method_value,
                ),
            ).fetchone()
        by_id = None
        if supplied_id is not None:
            by_id = conn.execute(
                "SELECT event_id, security_id, mapping_id, horizon, method_version, "
                "signal_ledger_id "
                "FROM event_outcomes WHERE outcome_id = ? LIMIT 1",
                (supplied_id,),
            ).fetchone()
        natural_key = (
            mapping_event, security_value, mapping_value, horizon_value,
            method_value, signal_ledger_value,
        )
        if natural is not None and supplied_id is not None and str(natural[1]) != supplied_id:
            raise EventCoreConflictError("outcome natural key belongs to another outcome_id")
        by_id_key = None if by_id is None else (
            str(by_id[0]),
            str(by_id[1]),
            str(by_id[2]),
            str(by_id[3]),
            str(by_id[4]),
            None if by_id[5] is None else int(by_id[5]),
        )
        if by_id_key is not None and by_id_key != natural_key:
            raise EventCoreConflictError("outcome_id belongs to another outcome record")

        if natural is None:
            if by_id is not None:
                raise EventCoreConflictError("outcome series has no current revision")
            resolved_id = supplied_id or _new_uuid()
            revision_id = _new_uuid()
            conn.execute(
                "INSERT INTO event_outcomes "
                "(outcome_revision_id, outcome_id, event_id, security_id, mapping_id, "
                "mapping_revision, signal_ledger_id, signal_available_at, observation_state, "
                "horizon, baseline_at, measured_at, baseline_price, "
                "gross_return, benchmark_return, excess_return, mfe, mae, direction_hit, "
                "benchmark_security_id, method_version, data_version, data_quality, revision, "
                "is_current, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
                (
                    revision_id, resolved_id, mapping_event, security_value, mapping_value,
                    mapping_revision, signal_ledger_value, signal_available_value, state_value,
                    horizon_value, baseline_value, measured_value,
                    baseline_price_value, gross_value, benchmark_return_value, excess_value,
                    mfe_value, mae_value, hit_value, benchmark_value, method_value, data_value,
                    quality_value, current_time,
                ),
            )
            return resolved_id

        resolved_id = str(natural[1])
        desired = (
            baseline_value, measured_value, baseline_price_value, gross_value,
            benchmark_return_value, excess_value, mfe_value, mae_value,
            hit_value, benchmark_value, quality_value, data_value, signal_ledger_value,
            signal_available_value, state_value,
        )
        def _maybe_float(value: object) -> float | None:
            return None if value is None else float(value)

        existing = (
            int(natural[2]), int(natural[3]), _maybe_float(natural[4]),
            _maybe_float(natural[5]), _maybe_float(natural[6]), _maybe_float(natural[7]),
            _maybe_float(natural[8]), _maybe_float(natural[9]), natural[10], natural[11],
            float(natural[12]), str(natural[14]), natural[15], natural[16], str(natural[17]),
        )
        if desired == existing:
            return resolved_id

        next_revision = int(natural[13]) + 1
        conn.execute(
            "UPDATE event_outcomes SET is_current = 0 WHERE outcome_revision_id = ?",
            (str(natural[0]),),
        )
        conn.execute(
            "INSERT INTO event_outcomes "
            "(outcome_revision_id, outcome_id, event_id, security_id, mapping_id, "
            "mapping_revision, signal_ledger_id, signal_available_at, observation_state, "
            "horizon, baseline_at, measured_at, baseline_price, "
            "gross_return, benchmark_return, excess_return, mfe, mae, direction_hit, "
            "benchmark_security_id, method_version, data_version, data_quality, revision, "
            "is_current, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                _new_uuid(), resolved_id, mapping_event, security_value, mapping_value,
                mapping_revision, signal_ledger_value, signal_available_value, state_value,
                horizon_value, *desired[:10], method_value, data_value,
                quality_value, next_revision, current_time,
            ),
        )
        return resolved_id


def upsert_event_similarity(
    conn: sqlite3.Connection,
    event_id: str,
    similar_event_id: str,
    *,
    similarity: float,
    method: str = "manual",
    model_version: str = "",
    rationale: str = "",
    common_securities: Iterable[str] | str | None = None,
    shared_paths: object | None = None,
    computed_at: int | None = None,
    now: int | None = None,
) -> tuple[str, str, str, str]:
    """Upsert an unordered canonical event pair with a bounded score."""
    score_value = _real(similarity, "similarity", 0, 1)
    method_value = _text(method, "method", required=True, max_length=64)
    model_value = _text(model_version, "model_version", max_length=255)
    rationale_value = _text(rationale, "rationale", max_length=4_000)
    common_value = _string_list_json(common_securities, "common_securities")
    shared_value = _json_array(shared_paths, "shared_paths")
    current_time = _now(now)
    computed_value = (
        None if computed_at is None else _timestamp(computed_at, "computed_at")
    )

    with _atomic(conn):
        left = canonical_event_id(conn, event_id)
        right = canonical_event_id(conn, similar_event_id)
        if left == right:
            raise EventCoreConflictError("an event cannot be similar to itself")
        left, right = sorted((left, right))
        row = conn.execute(
            "SELECT similarity, rationale, common_securities, shared_paths, computed_at, "
            "version, updated_at "
            "FROM event_similarities WHERE event_id = ? AND similar_event_id = ? "
            "AND method = ? AND model_version = ?",
            (left, right, method_value, model_value),
        ).fetchone()
        if row is None:
            resolved_computed = current_time if computed_value is None else computed_value
            conn.execute(
                "INSERT INTO event_similarities "
                "(event_id, similar_event_id, method, model_version, similarity, rationale, "
                "common_securities, shared_paths, computed_at, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    left, right, method_value, model_value, score_value, rationale_value,
                    common_value, shared_value, resolved_computed, current_time, current_time,
                ),
            )
        else:
            resolved_computed = int(row[4]) if computed_value is None else computed_value
            desired = (
                score_value, rationale_value, common_value, shared_value, resolved_computed,
            )
            existing = (
                float(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]),
            )
            if desired != existing:
                conn.execute(
                    "UPDATE event_similarities SET similarity = ?, rationale = ?, "
                    "common_securities = ?, shared_paths = ?, computed_at = ?, "
                    "version = version + 1, updated_at = ? "
                    "WHERE event_id = ? AND similar_event_id = ? "
                    "AND method = ? AND model_version = ?",
                    (
                        *desired, max(current_time, int(row[6])),
                        left, right, method_value, model_value,
                    ),
                )
        return left, right, method_value, model_value


__all__ = [
    "EVENT_CORE_BACKFILL_META_KEY",
    "EVENT_CORE_SCHEMA_META_KEY",
    "EVENT_CORE_SCHEMA_VERSION",
    "EVENT_CORE_TABLE_NAMES",
    "EVENT_MERGE_CONTRACT",
    "EventCoreConflictError",
    "EventCoreError",
    "EventCoreSchemaError",
    "canonical_event_group",
    "canonical_event_id",
    "count_unresolved_event_core_outbox_references",
    "init_event_core_schema",
    "merge_events",
    "reconcile_event_group_priorities",
    "record_event_mapping_replay",
    "resolve_or_create_event",
    "upsert_event_evidence",
    "upsert_event_outcome",
    "upsert_event_security",
    "upsert_event_similarity",
    "upsert_security",
    "verify_event_core_schema",
    "verify_event_core_backfill_state",
    "verify_integration_outbox_schema",
]
