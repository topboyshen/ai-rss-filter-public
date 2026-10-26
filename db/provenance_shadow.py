"""Rebuildable provenance-shadow storage outside the stable event contract.

These tables are deliberately not part of ``db.event_core``.  They are a
derived, display-only projection and must never become a prerequisite for
article ingestion, event resolution, scoring, mapping, or backtesting.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager


PROVENANCE_SHADOW_SCHEMA_META_KEY = "source_provenance_shadow_schema_version"
PROVENANCE_SHADOW_SCHEMA_VERSION = 1


class ProvenanceShadowSchemaError(RuntimeError):
    """The optional shadow schema exists but does not match its exact contract."""


_SAVEPOINTS = itertools.count()
_HASH_CHECK = (
    "length({column}) = 64 AND "
    "{column} NOT GLOB '*[^0-9a-f]*'"
)


_TABLE_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS event_evidence_provenance_shadow (
        evidence_id TEXT NOT NULL CHECK (length(trim(evidence_id)) BETWEEN 1 AND 64),
        evidence_version INTEGER NOT NULL
            CHECK (typeof(evidence_version) = 'integer' AND evidence_version >= 1),
        event_id TEXT NOT NULL CHECK (length(trim(event_id)) BETWEEN 1 AND 64),
        root_event_id TEXT NOT NULL CHECK (length(trim(root_event_id)) BETWEEN 1 AND 64),
        root_event_version INTEGER NOT NULL
            CHECK (typeof(root_event_version) = 'integer' AND root_event_version >= 1),
        projection_version TEXT NOT NULL
            CHECK (length(trim(projection_version)) BETWEEN 1 AND 128),
        claim_fingerprint TEXT NOT NULL
            CHECK ({_HASH_CHECK.format(column='claim_fingerprint')}),
        claim_basis TEXT NOT NULL
            CHECK (claim_basis IN ('exact_normalized_title', 'isolated_short_claim')),
        publisher_group TEXT NOT NULL
            CHECK (length(trim(publisher_group)) BETWEEN 1 AND 128),
        publisher_basis TEXT NOT NULL CHECK (
            publisher_basis IN (
                'registry_domain', 'registry_route', 'registrable_domain', 'unknown'
            )
        ),
        source_class TEXT NOT NULL CHECK (
            source_class IN (
                'official_record', 'issuer_first_party', 'wire', 'newsroom',
                'analysis', 'aggregator', 'social', 'unknown'
            )
        ),
        first_party_scope_json TEXT NOT NULL DEFAULT '[]'
            CHECK (length(first_party_scope_json) <= 4000),
        lineage_cluster TEXT NOT NULL
            CHECK (length(trim(lineage_cluster)) BETWEEN 1 AND 128),
        lineage_representative_evidence_id TEXT NOT NULL
            CHECK (length(trim(lineage_representative_evidence_id)) BETWEEN 1 AND 64),
        lineage_basis TEXT NOT NULL
            CHECK (lineage_basis IN ('exact_snapshot', 'unique_evidence')),
        input_fingerprint TEXT NOT NULL
            CHECK ({_HASH_CHECK.format(column='input_fingerprint')}),
        computed_at INTEGER NOT NULL
            CHECK (typeof(computed_at) = 'integer' AND computed_at > 0),
        PRIMARY KEY (evidence_id, projection_version)
    ) WITHOUT ROWID
    """,
    f"""
    CREATE TABLE IF NOT EXISTS event_claim_state_shadow (
        root_event_id TEXT NOT NULL CHECK (length(trim(root_event_id)) BETWEEN 1 AND 64),
        root_event_version INTEGER NOT NULL
            CHECK (typeof(root_event_version) = 'integer' AND root_event_version >= 1),
        claim_fingerprint TEXT NOT NULL
            CHECK ({_HASH_CHECK.format(column='claim_fingerprint')}),
        projection_version TEXT NOT NULL
            CHECK (length(trim(projection_version)) BETWEEN 1 AND 128),
        input_fingerprint TEXT NOT NULL
            CHECK ({_HASH_CHECK.format(column='input_fingerprint')}),
        state TEXT NOT NULL CHECK (
            state IN (
                'single_support', 'multi_independent_support', 'contested',
                'contradicted_only', 'context_only'
            )
        ),
        support_evidence_count INTEGER NOT NULL
            CHECK (typeof(support_evidence_count) = 'integer' AND support_evidence_count >= 0),
        support_publisher_count INTEGER NOT NULL
            CHECK (typeof(support_publisher_count) = 'integer' AND support_publisher_count >= 0),
        support_independent_units INTEGER NOT NULL CHECK (
            typeof(support_independent_units) = 'integer' AND support_independent_units >= 0
        ),
        contradict_evidence_count INTEGER NOT NULL CHECK (
            typeof(contradict_evidence_count) = 'integer' AND contradict_evidence_count >= 0
        ),
        contradict_publisher_count INTEGER NOT NULL CHECK (
            typeof(contradict_publisher_count) = 'integer' AND contradict_publisher_count >= 0
        ),
        contradict_independent_units INTEGER NOT NULL CHECK (
            typeof(contradict_independent_units) = 'integer' AND contradict_independent_units >= 0
        ),
        context_evidence_count INTEGER NOT NULL
            CHECK (typeof(context_evidence_count) = 'integer' AND context_evidence_count >= 0),
        official_record_support_count INTEGER NOT NULL CHECK (
            typeof(official_record_support_count) = 'integer'
            AND official_record_support_count >= 0
        ),
        first_party_support_count INTEGER NOT NULL CHECK (
            typeof(first_party_support_count) = 'integer' AND first_party_support_count >= 0
        ),
        unclassified_evidence_count INTEGER NOT NULL CHECK (
            typeof(unclassified_evidence_count) = 'integer' AND unclassified_evidence_count >= 0
        ),
        first_evidence_at INTEGER NOT NULL
            CHECK (typeof(first_evidence_at) = 'integer' AND first_evidence_at >= 0),
        latest_evidence_at INTEGER NOT NULL CHECK (
            typeof(latest_evidence_at) = 'integer' AND latest_evidence_at >= first_evidence_at
        ),
        computed_at INTEGER NOT NULL
            CHECK (typeof(computed_at) = 'integer' AND computed_at > 0),
        PRIMARY KEY (root_event_id, claim_fingerprint, projection_version)
    ) WITHOUT ROWID
    """,
)


_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_provenance_shadow_event_claim "
    "ON event_evidence_provenance_shadow("
    "event_id, projection_version, claim_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_provenance_shadow_root_claim "
    "ON event_evidence_provenance_shadow("
    "root_event_id, projection_version, claim_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_provenance_shadow_publisher "
    "ON event_evidence_provenance_shadow(projection_version, publisher_group)",
    "CREATE INDEX IF NOT EXISTS idx_provenance_shadow_lineage "
    "ON event_evidence_provenance_shadow(projection_version, lineage_cluster)",
    "CREATE INDEX IF NOT EXISTS idx_claim_shadow_root_computed "
    "ON event_claim_state_shadow(root_event_id, projection_version, computed_at DESC)",
)


_REQUIRED_COLUMNS = {
    "event_evidence_provenance_shadow": {
        "evidence_id",
        "evidence_version",
        "event_id",
        "root_event_id",
        "root_event_version",
        "projection_version",
        "claim_fingerprint",
        "claim_basis",
        "publisher_group",
        "publisher_basis",
        "source_class",
        "first_party_scope_json",
        "lineage_cluster",
        "lineage_representative_evidence_id",
        "lineage_basis",
        "input_fingerprint",
        "computed_at",
    },
    "event_claim_state_shadow": {
        "root_event_id",
        "root_event_version",
        "claim_fingerprint",
        "projection_version",
        "input_fingerprint",
        "state",
        "support_evidence_count",
        "support_publisher_count",
        "support_independent_units",
        "contradict_evidence_count",
        "contradict_publisher_count",
        "contradict_independent_units",
        "context_evidence_count",
        "official_record_support_count",
        "first_party_support_count",
        "unclassified_evidence_count",
        "first_evidence_at",
        "latest_evidence_at",
        "computed_at",
    },
}


def _normalize_schema_sql(value: object) -> str:
    sql = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b",
        "",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").casefold()


def _named_contracts(
    statements: Iterable[str], object_type: str
) -> Mapping[str, str]:
    pattern = re.compile(
        rf"\bCREATE\s+(?:UNIQUE\s+)?{re.escape(object_type)}\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)",
        re.IGNORECASE,
    )
    result: dict[str, str] = {}
    for statement in statements:
        match = pattern.search(statement)
        if match is None:
            raise AssertionError(f"cannot identify {object_type} shadow contract")
        result[match.group(1)] = _normalize_schema_sql(statement)
    return result


_TABLE_CONTRACTS = _named_contracts(_TABLE_DDL, "TABLE")
_INDEX_CONTRACTS = _named_contracts(_INDEX_DDL, "INDEX")


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if conn.in_transaction:
        name = f"provenance_shadow_{next(_SAVEPOINTS)}"
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


def _verify_named_objects(
    conn: sqlite3.Connection, object_type: str, contracts: Mapping[str, str]
) -> None:
    for name, expected in contracts.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
        if row is None or _normalize_schema_sql(row[0]) != expected:
            raise ProvenanceShadowSchemaError(
                f"incompatible provenance shadow {object_type}: {name}"
            )


def verify_provenance_shadow_schema(
    conn: sqlite3.Connection, *, require_marker: bool = True
) -> None:
    """Verify the exact optional sidecar schema without mutating SQLite."""
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        missing = required.difference(columns)
        if missing:
            raise ProvenanceShadowSchemaError(
                f"incompatible provenance shadow table {table}; missing: "
                + ", ".join(sorted(missing))
            )
    _verify_named_objects(conn, "table", _TABLE_CONTRACTS)
    _verify_named_objects(conn, "index", _INDEX_CONTRACTS)

    if not require_marker:
        return
    app_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
    ).fetchone()
    if app_meta is None:
        raise ProvenanceShadowSchemaError(
            "provenance shadow requires the application metadata table"
        )
    marker = conn.execute(
        "SELECT v FROM app_meta WHERE k = ?",
        (PROVENANCE_SHADOW_SCHEMA_META_KEY,),
    ).fetchone()
    if marker is None or str(marker[0]) != str(PROVENANCE_SHADOW_SCHEMA_VERSION):
        raise ProvenanceShadowSchemaError(
            f"provenance shadow schema marker must be {PROVENANCE_SHADOW_SCHEMA_VERSION}"
        )


def init_provenance_shadow_schema(conn: sqlite3.Connection) -> None:
    """Install the independent sidecar atomically, never backfilling core rows."""
    app_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
    ).fetchone()
    if app_meta is None:
        raise ProvenanceShadowSchemaError(
            "provenance shadow requires initialized application metadata"
        )

    marker = conn.execute(
        "SELECT v FROM app_meta WHERE k = ?",
        (PROVENANCE_SHADOW_SCHEMA_META_KEY,),
    ).fetchone()
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('event_evidence_provenance_shadow', "
            "'event_claim_state_shadow')"
        ).fetchall()
    }
    expected = set(_REQUIRED_COLUMNS)
    if marker is not None:
        if str(marker[0]) != str(PROVENANCE_SHADOW_SCHEMA_VERSION):
            raise ProvenanceShadowSchemaError(
                "unsupported provenance shadow schema marker"
            )
        verify_provenance_shadow_schema(conn)
        return
    if present and present != expected:
        raise ProvenanceShadowSchemaError(
            "partial provenance shadow schema requires explicit repair"
        )
    if present == expected:
        # A prior transaction may have installed both exact tables before its
        # marker was externally removed.  Never complete a weakened schema.
        verify_provenance_shadow_schema(conn, require_marker=False)

    with _atomic(conn):
        for statement in _TABLE_DDL:
            conn.execute(statement)
        for statement in _INDEX_DDL:
            conn.execute(statement)
        verify_provenance_shadow_schema(conn, require_marker=False)
        conn.execute(
            "INSERT INTO app_meta (k, v) VALUES (?, ?)",
            (
                PROVENANCE_SHADOW_SCHEMA_META_KEY,
                str(PROVENANCE_SHADOW_SCHEMA_VERSION),
            ),
        )
        verify_provenance_shadow_schema(conn)


__all__ = [
    "PROVENANCE_SHADOW_SCHEMA_META_KEY",
    "PROVENANCE_SHADOW_SCHEMA_VERSION",
    "ProvenanceShadowSchemaError",
    "init_provenance_shadow_schema",
    "verify_provenance_shadow_schema",
]
