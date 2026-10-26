"""Write-side enrichment for stable events.

The scorer produces an event and an auditable article snapshot first.  This
module resolves real A-share names against the synchronized security master,
binds the article evidence to each relationship, and then computes a bounded
event graph.  Headline similarity is never an identity by itself: automatic
merges require a complete normalized wire headline, identical non-empty frozen
snapshots and either independent publisher domains or one narrow publisher
replay window. Every merge is written to ``event_merge_history``.
"""

from __future__ import annotations

import hashlib
import itertools
import ipaddress
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from typing import Any, Callable, Hashable
from urllib.parse import urlsplit

from core.asset_mapping import get_mapping_version, map_article_to_assets
from core.digest_engine import normalize_event_identity_key
from core.source_contract import (
    ARTICLE_CLAIM_PREFIX,
    MAPPING_WRITER_MARKER,
    is_current_mapping_writer,
)
from db import database
from db.event_core import (
    EventCoreConflictError,
    canonical_event_group,
    canonical_event_id,
    merge_events,
    record_event_mapping_replay,
    upsert_event_similarity,
)
from utils.helpers import normalize_direction, title_similarity


logger = logging.getLogger(__name__)

_RELATION_PRIORITY = {
    "direct": 0,
    "upstream": 1,
    "downstream": 2,
    "ecosystem": 3,
    "concept": 4,
}
_RELATION_TYPES = set(_RELATION_PRIORITY)
_EVENT_GROUP_BATCH_SIZE = 500
_MAX_EVENT_GROUP_MEMBERS_PER_BATCH = 100_000
_MAPPING_REPLAY_MAX_ATTEMPTS = 3
_MAX_MAPPING_GROUP_MEMBERS = 500
_MAX_MAPPING_GROUP_EVIDENCE = 5_000
_CANDIDATE_BUCKET_LIMIT = 80
_SIMILARITY_METHOD = "event-graph-v1"
_SIMILARITY_MODEL_VERSION = "deterministic-title-security-v1"
_SIMILARITY_QUERY_BATCH = 400

# This explicit identity is a new generation relative to the dynamic AST/hash
# contract used by the d359 release. This is the store/migration generation,
# not permission to rewrite history when the title-only writer changes. The
# default lane has a separate explicit writer identity and only consumes its
# versioned publisher-title cohort. Historical seeding/rematerialize requires
# include_legacy=True under a separately authorized maintenance workflow.
# Store-schema/generation changes still require their explicit version bump;
# forward-only writer/rule edits must not silently queue all historical roots.
# Security-master membership is fingerprinted separately below.
EVENT_MAPPING_CONTRACT_VERSION = "event-mapping-store-v7"
# SHA-256("event-mapping-store-v7:usd-denomination-anchor-v1").
EVENT_MAPPING_SEMANTIC_FINGERPRINT = (
    "927d98f02fc1b4c99a1992c73ed5747c4"
    "b3ef426e0f2a4b0a8dc541bc11f4bb8"
)


class MappingReplaySafetyError(RuntimeError):
    """Fail the lane before an unbounded or dependency-unstable replay."""

# Fixed and offline: publisher identity must be deterministic in historical
# replay and must never depend on a runtime public-suffix download.  Unknown
# multi-label suffixes collapse to a broader identity (safe under-merge), never
# to a narrower identity that could manufacture independent confirmation.
_COMMON_TWO_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "ac.cn",
        "ac.uk",
        "co.in",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.uk",
        "com.au",
        "com.cn",
        "com.hk",
        "com.my",
        "com.sg",
        "com.tw",
        "edu.cn",
        "gov.cn",
        "gov.uk",
        "mil.cn",
        "net.au",
        "net.cn",
        "org.au",
        "org.cn",
        "org.uk",
    }
)
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _source_host(uri: object) -> str:
    try:
        host = (urlsplit(str(uri or "")).hostname or "").strip().rstrip(".")
        if not host:
            return ""
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
        normalized = host.encode("idna").decode("ascii").casefold()
        labels = normalized.split(".")
        if any(not _HOST_LABEL.fullmatch(label) for label in labels):
            return ""
        return normalized
    except (UnicodeError, ValueError):
        return ""


def _publisher_domain(host: object) -> str:
    """Return a conservative registrable publisher identity for one host."""
    normalized = str(host or "").strip().casefold().rstrip(".")
    if not normalized:
        return ""
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError:
        pass
    labels = normalized.split(".")
    if len(labels) <= 2:
        return normalized
    suffix = ".".join(labels[-2:])
    if suffix in _COMMON_TWO_LABEL_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return suffix


def _publisher_set(event: dict[str, Any]) -> frozenset[str]:
    publishers = {
        _publisher_domain(host) for host in event.get("sources", set())
    }
    publishers.discard("")
    return frozenset(publishers)


def _security_master_fingerprint(conn) -> tuple[str, int]:
    """Hash the effective active-equity resolver view, not master metadata."""
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(
        "SELECT security_id, name, ticker "
        "FROM securities WHERE is_active = 1 AND lower(asset_type) = 'equity' "
        "ORDER BY security_id"
    ):
        count += 1
        digest.update(
            json.dumps(
                [str(value or "") for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest(), count


def _mapping_generation_prefix() -> str:
    """Store-generation identity that owns rematerialize seeding.

    Keyword / ASSET_RULES edits change the rules suffix on new writes but must
    not queue every historical current row for mapping_contract_migration.
    """
    return (
        f"asset-map:{EVENT_MAPPING_CONTRACT_VERSION}:"
        f"manifest-{EVENT_MAPPING_SEMANTIC_FINGERPRINT}"
    )


def _mapping_generation_like_pattern() -> str:
    """LIKE pattern matching any current generation (rules/master suffixes)."""
    return f"{_mapping_generation_prefix()}:%"


def _mapping_rules_fingerprint() -> str:
    """Backtest-aligned asset-mapping source fingerprint without asset-map:."""
    version = str(get_mapping_version() or "")
    prefix = f"asset-map:{MAPPING_WRITER_MARKER}:"
    if version.startswith(prefix):
        return version[len(prefix) :]
    prefix = "asset-map:"
    if version.startswith(prefix):
        return version[len(prefix):]
    return version


def _mapping_contract_version(conn, *, include_legacy: bool = False) -> tuple[str, int]:
    # The security master is part of the deterministic input: a late listing,
    # rename or transient deactivation changes which exact names can resolve.
    # ASSET_RULES / matcher source changes are folded via get_mapping_version
    # so new enrichment writes carry a distinct identity without bumping the
    # store generation that rematerialize seeding keys on.
    master_fingerprint, active_equities = _security_master_fingerprint(conn)
    rules_fingerprint = _mapping_rules_fingerprint()
    writer = "" if include_legacy else f":writer-{MAPPING_WRITER_MARKER}"
    contract = (
        f"{_mapping_generation_prefix()}{writer}:"
        f"rules-{rules_fingerprint}:"
        f"master-{master_fingerprint}"
    )
    return contract, active_equities


def _same_generation_mapping_version_params(
    mapping_contract: str,
) -> tuple[str, str, str]:
    """Params for accepting same-generation rules-fingerprint drift.

    Qualifying ledger identities share the live store generation/writer prefix
    ``asset-map:{CONTRACT}:manifest-{SEMANTIC}`` and the live security-master
    fingerprint. They may omit or differ in the ``rules-<hash>`` segment, but
    forward-writer ledgers never borrow unmarked legacy terminal rows.

    Returns ``(exact, pre_rules_master, rules_any_master_like)`` for::

        (column = ? OR column = ? OR column LIKE ?)

    Other generations and master-fingerprint changes intentionally fail the
    match so pending / migration-complete still require rematerialize. When the
    live contract cannot be parsed, all three values collapse to exact equality.
    """
    exact = str(mapping_contract or "")
    prefix = _mapping_generation_prefix()
    if f":writer-{MAPPING_WRITER_MARKER}:" in exact:
        prefix += f":writer-{MAPPING_WRITER_MARKER}"
    master_token = ":master-"
    if not exact.startswith(f"{prefix}:") or master_token not in exact:
        return exact, exact, exact
    master_fp = exact.rsplit(master_token, 1)[1]
    if not master_fp or ":" in master_fp:
        return exact, exact, exact
    pre_rules = f"{prefix}{master_token}{master_fp}"
    rules_like = f"{prefix}:rules-%{master_token}{master_fp}"
    return exact, pre_rules, rules_like


def _same_generation_mapping_version_sql(column: str) -> str:
    """SQL predicate using params from ``_same_generation_mapping_version_params``."""
    return f"({column} = ? OR {column} = ? OR {column} LIKE ?)"


def _mapping_data_version(conn) -> int:
    """Return the connection-local external-commit generation."""
    row = conn.execute("PRAGMA data_version").fetchone()
    if row is None:
        raise MappingReplaySafetyError("SQLite data_version is unavailable")
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise MappingReplaySafetyError("SQLite data_version is invalid") from exc


def _verify_mapping_dependencies_after_external_commit(
    conn,
    *,
    observed_data_version: int,
    mapping_contract: str,
    active_equities: int,
    failure_message: str,
    include_legacy: bool = False,
) -> int:
    """Re-hash the exact mapping contract only after another connection commits."""
    live_data_version = _mapping_data_version(conn)
    if live_data_version == observed_data_version:
        return observed_data_version
    live_contract, live_active_equities = _mapping_contract_version(
        conn, **({"include_legacy": True} if include_legacy else {}),
    )
    if (
        live_contract != mapping_contract
        or live_active_equities != active_equities
    ):
        raise MappingReplaySafetyError(failure_message)
    return live_data_version


# Deterministic event-mapper writers use the ``asset-map:`` namespace.  Current
# rows outside it are not owned by this migration (the production population is
# benchmark_proxy); manual/external revisions and every benchmark proxy must
# remain frozen and must not block the all-history completion gate.
# Outdated-current seeding uses same-generation acceptance (live store
# generation + live security-master fingerprint, rules-<hash> optional). Same-gen
# rules-fingerprint drift stays current; master-fingerprint changes and other
# generations are queued as mapping_contract_migration.
_OLD_CURRENT_MAPPING_ROOTS_CTE = (
    "WITH RECURSIVE mapping_roots(origin_event_id, root_event_id, depth) AS ("
    " SELECT DISTINCT mapping.event_id, mapping.event_id, 0"
    " FROM event_securities AS mapping"
    " WHERE mapping.is_current = 1"
    " AND mapping.mapping_version LIKE 'asset-map:%'"
    " AND NOT ("
    "  mapping.mapping_version = ? OR mapping.mapping_version = ?"
    "  OR mapping.mapping_version LIKE ?"
    " )"
    " AND lower(mapping.relation_type) <> 'benchmark_proxy'"
    " UNION ALL"
    " SELECT walk.origin_event_id, parent.merged_into, walk.depth + 1"
    " FROM mapping_roots AS walk JOIN events AS parent"
    " ON parent.event_id = walk.root_event_id"
    " WHERE parent.merged_into IS NOT NULL AND walk.depth < 64"
    "), canonical_roots(root_event_id) AS ("
    " SELECT DISTINCT root.event_id"
    " FROM mapping_roots AS walk JOIN events AS root"
    " ON root.event_id = walk.root_event_id"
    " WHERE root.merge_state = 'active' AND root.merged_into IS NULL"
    ")"
)


def _old_current_mapping_counts(
    conn,
    mapping_contract: str,
    *,
    include_legacy: bool = True,
) -> tuple[int, int]:
    same_gen = _same_generation_mapping_version_params(mapping_contract)
    cohort = "" if include_legacy else f" AND {_forward_mapping_sql('event_securities')}"
    roots_cte = _OLD_CURRENT_MAPPING_ROOTS_CTE
    if not include_legacy:
        roots_cte = roots_cte.replace(
            "AND mapping.mapping_version LIKE 'asset-map:%'",
            "AND mapping.mapping_version LIKE 'asset-map:%'"
            f" AND {_forward_mapping_sql('mapping')}",
        )
    old_mappings = int(
        conn.execute(
            "SELECT COUNT(*) FROM event_securities"
            " WHERE is_current = 1 AND mapping_version LIKE 'asset-map:%'"
            " AND NOT ("
            "  mapping_version = ? OR mapping_version = ?"
            "  OR mapping_version LIKE ?"
            " )"
            " AND lower(relation_type) <> 'benchmark_proxy'" + cohort,
            same_gen,
        ).fetchone()[0]
    )
    old_groups = int(
        conn.execute(
            roots_cte
            + " SELECT COUNT(*) FROM canonical_roots",
            same_gen,
        ).fetchone()[0]
    )
    return old_mappings, old_groups


def _seed_outdated_mapping_roots(
    conn,
    mapping_contract: str,
    *,
    dirty_at: int,
    include_legacy: bool = False,
) -> tuple[int, int]:
    """Durably queue roots outside live same-generation acceptance."""
    if not include_legacy:
        # A new writer identity is not authorization to repair frozen history.
        return 0, 0
    same_gen = _same_generation_mapping_version_params(mapping_contract)
    _old_mappings, old_groups = _old_current_mapping_counts(conn, mapping_contract)
    changes_before = int(conn.total_changes)
    # An exhausted same-generation input stays visible in the immutable ledger
    # and in the migration gate, but is not requeued forever on every recent
    # cron run. Same-gen acceptance keeps rules-hash drift from rematerializing
    # history while still queuing security-master fingerprint changes.
    conn.execute(
        _OLD_CURRENT_MAPPING_ROOTS_CTE
        + " INSERT OR IGNORE INTO event_mapping_replay_dirty_roots("
        "root_event_id, reason, dirty_at)"
        " SELECT root_event_id, 'mapping_contract_migration', ?"
        " FROM canonical_roots AS candidate"
        " WHERE NOT EXISTS ("
        "  SELECT 1 FROM event_mapping_replay_ledger AS failed"
        "  JOIN event_evidence AS evidence"
        "  ON evidence.evidence_id = failed.evidence_id"
        "  JOIN events AS root ON root.event_id = failed.root_event_id"
        "  WHERE failed.root_event_id = candidate.root_event_id"
        f"  AND {_same_generation_mapping_version_sql('failed.mapping_version')}"
        "  AND failed.status = 'failed'"
        "  AND failed.attempts >= ? AND evidence.mapping_id IS NULL"
        "  AND evidence.version = failed.evidence_version"
        "  AND root.version = failed.root_event_version"
        "  AND root.merge_state = 'active' AND root.merged_into IS NULL"
        " )",
        (
            *same_gen,
            int(dirty_at),
            *same_gen,
            _MAPPING_REPLAY_MAX_ATTEMPTS,
        ),
    )
    return old_groups, max(0, int(conn.total_changes) - changes_before)


def _legacy_mapping_count(conn) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM event_securities AS mapping WHERE is_current = 1 "
        "AND mapping_version LIKE 'asset-map:%' "
        f"AND NOT {_forward_mapping_sql('mapping')} "
        "AND lower(relation_type) <> 'benchmark_proxy'",
    ).fetchone()[0])


def _event_mapping_migration_status(
    conn,
    *,
    mapping_contract: str,
    active_equities: int,
    include_legacy: bool = True,
) -> dict[str, Any]:
    old_current_mappings, old_current_groups = _old_current_mapping_counts(
        conn, mapping_contract, include_legacy=include_legacy,
    )
    pending = conn.execute(
        "WITH RECURSIVE event_roots(origin_event_id, root_event_id, depth) AS ("
        " SELECT event_id, event_id, 0 FROM events"
        " UNION ALL"
        " SELECT walk.origin_event_id, parent.merged_into, walk.depth + 1"
        " FROM event_roots AS walk JOIN events AS parent"
        " ON parent.event_id = walk.root_event_id"
        " WHERE parent.merged_into IS NOT NULL AND walk.depth < 64"
        "), resolved_roots AS ("
        " SELECT walk.origin_event_id, root.event_id AS root_event_id,"
        " root.version AS root_event_version"
        " FROM event_roots AS walk JOIN events AS root"
        " ON root.event_id = walk.root_event_id"
        " WHERE root.merge_state = 'active' AND root.merged_into IS NULL"
        ")"
        " SELECT COUNT(*), COUNT(DISTINCT roots.root_event_id)"
        " FROM event_evidence AS evidence JOIN resolved_roots AS roots"
        " ON roots.origin_event_id = evidence.event_id"
        " WHERE evidence.mapping_id IS NULL"
        f" AND {_article_cohort_sql('evidence', include_legacy=include_legacy)}"
        " AND NOT EXISTS ("
        "  SELECT 1 FROM event_mapping_replay_ledger AS replay"
        "  WHERE replay.evidence_id = evidence.evidence_id"
        "  AND replay.evidence_version = evidence.version"
        "  AND replay.root_event_id = roots.root_event_id"
        "  AND replay.root_event_version = roots.root_event_version"
        f"  AND {_same_generation_mapping_version_sql('replay.mapping_version')}"
        "  AND replay.status IN ('mapped', 'no_match')"
        " )"
        " AND NOT EXISTS ("
        "  SELECT 1 FROM event_mapping_replay_ledger AS failed"
        "  WHERE failed.evidence_id = evidence.evidence_id"
        "  AND failed.evidence_version = evidence.version"
        "  AND failed.root_event_id = roots.root_event_id"
        "  AND failed.root_event_version = roots.root_event_version"
        f"  AND {_same_generation_mapping_version_sql('failed.mapping_version')}"
        "  AND failed.status = 'failed'"
        "  AND failed.attempts >= ?"
        " )",
        (
            *_same_generation_mapping_version_params(mapping_contract),
            *_same_generation_mapping_version_params(mapping_contract),
            _MAPPING_REPLAY_MAX_ATTEMPTS,
        ),
    ).fetchone()
    pending_evidence = int(pending[0])
    pending_groups = int(pending[1])
    dirty_groups = int(
        conn.execute(
            "SELECT COUNT(*) FROM (" + _eligible_dirty_mapping_roots_sql(include_legacy=include_legacy) + ")"
        ).fetchone()[0]
    )
    similarity_dirty_groups = int(
        conn.execute(
            "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
        ).fetchone()[0]
    )
    exhausted_groups = int(
        conn.execute(
            "SELECT COUNT(DISTINCT failed.root_event_id)"
            " FROM event_mapping_replay_ledger AS failed"
            " JOIN event_evidence AS evidence"
            " ON evidence.evidence_id = failed.evidence_id"
            " JOIN events AS root ON root.event_id = failed.root_event_id"
            " WHERE "
            f"{_same_generation_mapping_version_sql('failed.mapping_version')}"
            f" AND {_article_cohort_sql('evidence', include_legacy=include_legacy)}"
            " AND failed.status = 'failed'"
            " AND failed.attempts >= ? AND evidence.mapping_id IS NULL"
            " AND evidence.version = failed.evidence_version"
            " AND root.version = failed.root_event_version"
            " AND root.merge_state = 'active' AND root.merged_into IS NULL",
            (
                *_same_generation_mapping_version_params(mapping_contract),
                _MAPPING_REPLAY_MAX_ATTEMPTS,
            ),
        ).fetchone()[0]
    )
    migration_complete = all(
        count == 0
        for count in (
            old_current_mappings,
            pending_evidence,
            dirty_groups,
            exhausted_groups,
        )
    )
    legacy_preserved = _legacy_mapping_count(conn) if not include_legacy else 0
    return {
        "scope": "all_history" if include_legacy else "forward_only",
        "legacy_preserved": legacy_preserved,
        "forward_complete": migration_complete if not include_legacy else False,
        "mapping_contract": mapping_contract,
        "active_equities": int(active_equities),
        "old_current_mappings": old_current_mappings,
        "old_current_groups": old_current_groups,
        "pending_evidence": pending_evidence,
        "pending_groups": pending_groups,
        "dirty_groups": dirty_groups,
        # Mapping completion and graph convergence are separate phases.  The
        # strict host coordinator requires this queue to reach zero before it
        # declares the overall migration complete.
        "similarity_dirty_groups": similarity_dirty_groups,
        "exhausted_groups": exhausted_groups,
        "migration_complete": migration_complete if include_legacy else False,
    }


def event_mapping_migration_status(*, include_legacy: bool = True) -> dict[str, Any]:
    """Return the authoritative all-history mapping migration postcondition."""
    with database.db_lock:
        with database.get_connection() as conn:
            mapping_contract, active_equities = _mapping_contract_version(
                conn, **({"include_legacy": True} if include_legacy else {}),
            )
            return _event_mapping_migration_status(
                conn,
                mapping_contract=mapping_contract,
                active_equities=active_equities,
                include_legacy=include_legacy,
            )


def _audit_rows(rows: Any) -> tuple[int, str]:
    """Hash an ordered query without retaining its potentially large result."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        digest.update(
            json.dumps(
                [value for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return count, digest.hexdigest()


def _event_mapping_migration_audit_status(conn) -> dict[str, Any]:
    """Return bounded, content-free evidence for an explicit historical replay.

    The input snapshot hashes the complete mapping-relevant event/evidence
    projection. Mapping/ledger writes therefore do not change it, while a feed
    refresh, evidence edit, or event merge does. A host coordinator can pin
    this value across every batch without logging article text.
    """
    mapping_contract, active_equities = _mapping_contract_version(conn, include_legacy=True)
    migration = _event_mapping_migration_status(
        conn,
        mapping_contract=mapping_contract,
        active_equities=active_equities,
    )
    master_fingerprint, _master_count = _security_master_fingerprint(conn)

    input_count, input_snapshot = _audit_rows(
        conn.execute(
            "WITH RECURSIVE event_roots(origin_event_id, root_event_id, depth) AS ("
            " SELECT event_id, event_id, 0 FROM events"
            " UNION ALL"
            " SELECT walk.origin_event_id, parent.merged_into, walk.depth + 1"
            " FROM event_roots AS walk JOIN events AS parent"
            " ON parent.event_id = walk.root_event_id"
            " WHERE parent.merged_into IS NOT NULL AND walk.depth < 64"
            "), resolved_roots AS ("
            " SELECT walk.origin_event_id, root.event_id AS root_event_id,"
            " root.version AS root_event_version, root.event_type, root.direction"
            " FROM event_roots AS walk JOIN events AS root"
            " ON root.event_id = walk.root_event_id"
            " WHERE root.merge_state = 'active' AND root.merged_into IS NULL"
            ") SELECT roots.root_event_id, roots.root_event_version,"
            " roots.event_type, roots.direction, evidence.evidence_id,"
            " evidence.event_id, evidence.version, evidence.source_uri,"
            " evidence.claim_text, evidence.excerpt, evidence.snapshot_hash"
            " FROM event_evidence AS evidence JOIN resolved_roots AS roots"
            " ON roots.origin_event_id = evidence.event_id"
            " WHERE evidence.mapping_id IS NULL"
            " ORDER BY roots.root_event_id, evidence.evidence_id"
        )
    )
    mapping_dirty_count, mapping_dirty_snapshot = _audit_rows(
        conn.execute(
            "SELECT root_event_id, reason FROM event_mapping_replay_dirty_roots "
            "ORDER BY root_event_id"
        )
    )
    similarity_dirty_count, similarity_dirty_snapshot = _audit_rows(
        conn.execute(
            "SELECT root_event_id, generation "
            "FROM event_similarity_replay_dirty_roots ORDER BY root_event_id"
        )
    )
    terminal_count, terminal_snapshot = _audit_rows(
        conn.execute(
            "SELECT root_event_id, root_event_version, evidence_id,"
            " evidence_version, status, input_fingerprint, output_fingerprint"
            " FROM event_mapping_replay_ledger"
            " WHERE mapping_version = ? AND status IN ('mapped', 'no_match')"
            " ORDER BY root_event_id, evidence_id, input_fingerprint",
            (mapping_contract,),
        )
    )
    current_mapping_count, current_mapping_snapshot = _audit_rows(
        conn.execute(
            "SELECT mapping_id, event_id, security_id, lower(relation_type),"
            " direction, revision, mapping_version"
            " FROM event_securities WHERE is_current = 1"
            " AND mapping_version LIKE 'asset-map:%'"
            " AND lower(relation_type) <> 'benchmark_proxy'"
            " ORDER BY event_id, security_id, lower(relation_type), mapping_id"
        )
    )
    merge_count, merge_snapshot = _audit_rows(
        conn.execute(
            "SELECT merge_id, source_event_id, target_event_id, source_version,"
            " target_version, actor, merged_at FROM event_merge_history"
            " ORDER BY merge_id"
        )
    )
    orphan_current_groups = int(
        conn.execute(
            _OLD_CURRENT_MAPPING_ROOTS_CTE
            + ", event_roots(origin_event_id, root_event_id, depth) AS ("
            " SELECT event_id, event_id, 0 FROM events"
            " UNION ALL"
            " SELECT walk.origin_event_id, parent.merged_into, walk.depth + 1"
            " FROM event_roots AS walk JOIN events AS parent"
            " ON parent.event_id = walk.root_event_id"
            " WHERE parent.merged_into IS NOT NULL AND walk.depth < 64"
            "), evidence_roots(root_event_id) AS ("
            " SELECT DISTINCT root.event_id"
            " FROM event_evidence AS evidence"
            " JOIN event_roots AS walk ON walk.origin_event_id = evidence.event_id"
            " JOIN events AS root ON root.event_id = walk.root_event_id"
            " WHERE evidence.mapping_id IS NULL"
            " AND root.merge_state = 'active' AND root.merged_into IS NULL"
            ") SELECT COUNT(*) FROM canonical_roots AS candidate"
            " WHERE NOT EXISTS (SELECT 1 FROM evidence_roots AS evidence"
            " WHERE evidence.root_event_id = candidate.root_event_id)",
            _same_generation_mapping_version_params(mapping_contract),
        ).fetchone()[0]
    )
    foreign_key_violations = sum(1 for _row in conn.execute("PRAGMA foreign_key_check"))
    return {
        **migration,
        "security_master_fingerprint": master_fingerprint,
        "input_evidence": input_count,
        "input_snapshot_sha256": input_snapshot,
        "mapping_dirty_snapshot_sha256": mapping_dirty_snapshot,
        "similarity_dirty_groups": similarity_dirty_count,
        "similarity_dirty_snapshot_sha256": similarity_dirty_snapshot,
        "terminal_evidence": terminal_count,
        "terminal_snapshot_sha256": terminal_snapshot,
        "current_rule_mappings": current_mapping_count,
        "current_mapping_snapshot_sha256": current_mapping_snapshot,
        "merge_history_count": merge_count,
        "merge_history_snapshot_sha256": merge_snapshot,
        "orphan_current_groups": orphan_current_groups,
        "foreign_key_violations": foreign_key_violations,
        "mapping_dirty_groups": mapping_dirty_count,
    }


def event_mapping_migration_audit_status(*, read_only: bool = False) -> dict[str, Any]:
    """Return the authoritative migration gate plus reproducible row hashes."""
    with database.db_lock:
        connection = (
            database.get_read_only_connection
            if read_only
            else database.get_connection
        )
        with connection() as conn:
            return _event_mapping_migration_audit_status(conn)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _article_cohort_sql(alias: str, *, include_legacy: bool) -> str:
    """A stored identity, not a timestamp, owns the forward-only input lane."""
    if include_legacy:
        return "1 = 1"
    key = f"{alias}.claim_key"
    offset = len(ARTICLE_CLAIM_PREFIX) + 1
    return (
        f"(substr({key}, 1, {len(ARTICLE_CLAIM_PREFIX)}) = '{ARTICLE_CLAIM_PREFIX}' "
        f"AND length({key}) = {len(ARTICLE_CLAIM_PREFIX) + 64} "
        f"AND substr({key}, {offset}) NOT GLOB '*[^0-9a-f]*')"
    )


def _forward_mapping_sql(alias: str) -> str:
    version = f"{alias}.mapping_version"
    return (
        f"({version} LIKE 'asset-map:{MAPPING_WRITER_MARKER}:%' "
        f"OR {version} LIKE 'asset-map:%:writer-{MAPPING_WRITER_MARKER}:%')"
    )


def _eligible_dirty_mapping_roots_sql(*, include_legacy: bool) -> str:
    if include_legacy:
        return "SELECT root_event_id FROM event_mapping_replay_dirty_roots AS dirty "
    return (
        "SELECT dirty.root_event_id FROM event_mapping_replay_dirty_roots AS dirty "
        "WHERE EXISTS (WITH RECURSIVE dirty_group(event_id) AS ("
        " SELECT dirty.root_event_id UNION SELECT child.event_id FROM events AS child "
        "JOIN dirty_group AS parent ON child.merged_into = parent.event_id) "
        "SELECT 1 FROM dirty_group JOIN event_evidence AS evidence "
        "ON evidence.event_id = dirty_group.event_id WHERE evidence.mapping_id IS NULL "
        f"AND {_article_cohort_sql('evidence', include_legacy=False)}) "
        "OR EXISTS (WITH RECURSIVE dirty_group(event_id) AS ("
        " SELECT dirty.root_event_id UNION SELECT child.event_id FROM events AS child "
        "JOIN dirty_group AS parent ON child.merged_into = parent.event_id) "
        "SELECT 1 FROM dirty_group JOIN event_securities AS mapping "
        "ON mapping.event_id = dirty_group.event_id WHERE mapping.is_current = 1 "
        f"AND {_forward_mapping_sql('mapping')}) "
    )


def _eligible_dirty_mapping_roots(conn, limit: int, *, include_legacy: bool):
    return conn.execute(
        _eligible_dirty_mapping_roots_sql(include_legacy=include_legacy)
        + "ORDER BY dirty.dirty_at, dirty.root_event_id LIMIT ?", (int(limit),),
    ).fetchall()


def _pending_mapping_roots(
    conn,
    *,
    cutoff: int,
    limit: int,
    mapping_contract: str,
    include_legacy: bool = False,
) -> tuple[list[str], set[str]]:
    roots: list[str] = []
    seen: set[str] = set()

    def add_root(raw_root_id: object) -> None:
        if len(roots) >= int(limit):
            return
        root_id = canonical_event_id(conn, str(raw_root_id))
        if root_id not in seen:
            seen.add(root_id)
            roots.append(root_id)

    # SQLite triggers mark every mapping/provenance mutation, including writes
    # made by an older application image that does not know about the replay
    # ledger.  Dirty groups get first use of the bounded batch so live output
    # fingerprint validation cannot be starved by a historical backlog.
    dirty_rows = _eligible_dirty_mapping_roots(
        conn, limit, include_legacy=include_legacy,
    )
    for row in dirty_rows:
        add_root(row[0])

    remaining_limit = max(0, int(limit) - len(roots))
    rows = (
        conn.execute(
            "WITH RECURSIVE event_roots(origin_event_id, root_event_id, depth) AS ("
        " SELECT event_id, event_id, 0 FROM events"
        " UNION ALL"
        " SELECT walk.origin_event_id, parent.merged_into, walk.depth + 1"
        " FROM event_roots AS walk JOIN events AS parent"
        " ON parent.event_id = walk.root_event_id"
        " WHERE parent.merged_into IS NOT NULL AND walk.depth < 64"
        "), resolved_roots AS ("
        " SELECT walk.origin_event_id, root.event_id AS root_event_id,"
        " root.version AS root_event_version"
        " FROM event_roots AS walk JOIN events AS root"
        " ON root.event_id = walk.root_event_id"
        " WHERE root.merge_state = 'active' AND root.merged_into IS NULL"
        ")"
        " SELECT evidence.evidence_id, roots.root_event_id"
        " FROM event_evidence AS evidence JOIN resolved_roots AS roots"
        " ON roots.origin_event_id = evidence.event_id"
        " WHERE evidence.mapping_id IS NULL AND evidence.observed_at >= ?"
        f" AND {_article_cohort_sql('evidence', include_legacy=include_legacy)}"
        " AND NOT EXISTS ("
        "  SELECT 1 FROM event_mapping_replay_ledger AS replay"
        "  WHERE replay.evidence_id = evidence.evidence_id"
        "  AND replay.evidence_version = evidence.version"
        "  AND replay.root_event_id = roots.root_event_id"
        "  AND replay.root_event_version = roots.root_event_version"
        f"  AND {_same_generation_mapping_version_sql('replay.mapping_version')}"
        "  AND replay.status IN ('mapped', 'no_match')"
        " )"
        " AND NOT EXISTS ("
        "  SELECT 1 FROM event_mapping_replay_ledger AS failed"
        "  WHERE failed.evidence_id = evidence.evidence_id"
        "  AND failed.evidence_version = evidence.version"
        "  AND failed.root_event_id = roots.root_event_id"
        "  AND failed.root_event_version = roots.root_event_version"
        f"  AND {_same_generation_mapping_version_sql('failed.mapping_version')}"
        "  AND failed.status = 'failed'"
        "  AND failed.attempts >= ?"
        " )"
        " ORDER BY evidence.observed_at, evidence.evidence_id LIMIT ?",
            (
                int(cutoff),
                *_same_generation_mapping_version_params(mapping_contract),
                *_same_generation_mapping_version_params(mapping_contract),
                _MAPPING_REPLAY_MAX_ATTEMPTS,
                remaining_limit,
            ),
        ).fetchall()
        if remaining_limit
        else []
    )
    for row in rows:
        add_root(row[1])

    # Output drift is delivered through the durable dirty triggers above.
    # Looking at every historical terminal ledger row here is unsafe: an old
    # ``mapped`` generation can legitimately be superseded by a current
    # ``no_match`` generation, and treating the former as live drift would
    # select and skip that root forever. Additive mapping/provenance triggers
    # also cover writes made by the previous application image, so the queue is
    # the authoritative cross-version wake-up path.
    exhausted_roots = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT failed.root_event_id "
            "FROM event_mapping_replay_ledger AS failed "
            "JOIN event_evidence AS evidence ON evidence.evidence_id = failed.evidence_id "
            "JOIN events AS root ON root.event_id = failed.root_event_id "
            "WHERE "
            f"{_same_generation_mapping_version_sql('failed.mapping_version')} "
            f"AND {_article_cohort_sql('evidence', include_legacy=include_legacy)} "
            "AND failed.status = 'failed' "
            "AND failed.attempts >= ? AND evidence.mapping_id IS NULL "
            "AND evidence.version = failed.evidence_version "
            "AND root.version = failed.root_event_version "
            "AND root.merge_state = 'active' AND root.merged_into IS NULL",
            (
                *_same_generation_mapping_version_params(mapping_contract),
                _MAPPING_REPLAY_MAX_ATTEMPTS,
            ),
        ).fetchall()
    }
    return roots, exhausted_roots


def _mapping_group_rows(
    conn, root_id: str, *, include_legacy: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    canonical_id = canonical_event_id(conn, root_id)
    root = conn.execute(
        "SELECT event_id, version, event_type, direction FROM events WHERE event_id = ?",
        (canonical_id,),
    ).fetchone()
    if root is None:
        raise KeyError(f"unknown canonical event: {canonical_id}")
    group_ids = canonical_event_group(conn, canonical_id)
    if len(group_ids) > _MAX_MAPPING_GROUP_MEMBERS:
        raise MappingReplaySafetyError(
            "mapping replay canonical group exceeds the safe member bound"
        )
    placeholders = ",".join("?" for _ in group_ids)
    evidence_rows = conn.execute(
        "SELECT evidence_id, event_id, version, source_uri, claim_key, claim_text, excerpt, "
        "snapshot_hash, observed_at FROM event_evidence AS evidence "
        f"WHERE event_id IN ({placeholders}) AND mapping_id IS NULL "
        f"AND {_article_cohort_sql('evidence', include_legacy=include_legacy)} "
        "ORDER BY observed_at, evidence_id LIMIT ?",
        (*group_ids, _MAX_MAPPING_GROUP_EVIDENCE + 1),
    ).fetchall()
    if len(evidence_rows) > _MAX_MAPPING_GROUP_EVIDENCE:
        raise MappingReplaySafetyError(
            "mapping replay canonical group exceeds the safe evidence bound"
        )
    evidence = [
        dict(row)
        for row in evidence_rows
    ]
    root_payload = {
        "event_id": str(root[0]),
        "version": int(root[1]),
        "event_type": str(root[2] or ""),
        "direction": normalize_direction(str(root[3] or "中性")),
    }
    return root_payload, evidence


def _clear_mapping_group_dirty(conn, root_id: str) -> None:
    group_ids = canonical_event_group(conn, root_id)
    if len(group_ids) > _MAX_MAPPING_GROUP_MEMBERS:
        raise MappingReplaySafetyError(
            "mapping replay canonical group exceeds the safe member bound"
        )
    placeholders = ",".join("?" for _ in group_ids)
    conn.execute(
        "DELETE FROM event_mapping_replay_dirty_roots "
        f"WHERE root_event_id IN ({placeholders})",
        tuple(group_ids),
    )


def _mapping_group_input_fingerprint(
    root: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    mapping_contract: str,
) -> str:
    return _fingerprint(
        {
            "mapping_contract": mapping_contract,
            "root": root,
            "evidence": [
                {
                    "evidence_id": str(row["evidence_id"]),
                    "event_id": str(row["event_id"]),
                    "version": int(row["version"]),
                    "source_uri": str(row["source_uri"] or ""),
                    "claim_text": str(row["claim_text"] or ""),
                    "excerpt": str(row["excerpt"] or ""),
                    "snapshot_hash": str(row["snapshot_hash"] or ""),
                }
                for row in evidence_rows
            ],
        }
    )


def _mapping_group_output(
    conn,
    root_id: str,
    mapping_contract: str,
) -> tuple[str, int]:
    payload: list[dict[str, Any]] = []
    group_ids = canonical_event_group(conn, root_id)
    if len(group_ids) > _MAX_MAPPING_GROUP_MEMBERS:
        raise MappingReplaySafetyError(
            "mapping replay canonical group exceeds the safe member bound"
        )
    placeholders = ",".join("?" for _ in group_ids)
    mappings = conn.execute(
        "SELECT mapping_id, event_id, security_id, lower(relation_type), direction, "
        "expected_gap, confidence, relevance, rationale "
        f"FROM event_securities WHERE event_id IN ({placeholders}) AND is_current = 1 "
        "AND mapping_version = ? AND lower(relation_type) <> 'benchmark_proxy' "
        "ORDER BY event_id, security_id, lower(relation_type), mapping_id",
        (*group_ids, mapping_contract),
    ).fetchall()
    for mapping in mappings:
        provenance = [
            {
                "source_uri": str(item[0] or ""),
                "snapshot_hash": str(item[1] or ""),
                "claim_text": str(item[2] or ""),
            }
            for item in conn.execute(
                "SELECT source_uri, snapshot_hash, claim_text FROM event_evidence "
                "WHERE mapping_id = ? ORDER BY source_uri, evidence_id",
                (str(mapping[0]),),
            ).fetchall()
        ]
        payload.append(
            {
                "event_id": str(mapping[1]),
                "security_id": str(mapping[2]),
                "relation_type": str(mapping[3]),
                "direction": str(mapping[4]),
                "expected_gap": (
                    None if mapping[5] is None else round(float(mapping[5]), 8)
                ),
                "confidence": round(float(mapping[6]), 8),
                "relevance": round(float(mapping[7]), 8),
                "rationale": str(mapping[8] or ""),
                "mapping_contract": mapping_contract,
                "provenance": provenance,
            }
        )
    return _fingerprint(payload), len(payload)


def _mark_mapping_group_similarity_dirty(
    conn,
    root_id: str,
    *,
    dirty_at: int,
) -> None:
    """Persist the mapping-to-graph handoff until graph reconciliation acks it."""
    canonical_id = canonical_event_id(conn, root_id)
    conn.execute(
        "INSERT INTO event_similarity_replay_dirty_roots "
        "(root_event_id, dirty_at) VALUES (?, ?) "
        "ON CONFLICT(root_event_id) DO UPDATE SET "
        "generation = event_similarity_replay_dirty_roots.generation + 1, "
        "dirty_at = MAX(event_similarity_replay_dirty_roots.dirty_at, excluded.dirty_at)",
        (canonical_id, int(dirty_at)),
    )


def _write_mapping_group(
    conn,
    *,
    root: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    mapping_contract: str,
    processing_time: int,
) -> dict[str, Any]:
    """Materialize one complete desired mapping group deterministically."""
    desired_mapping_ids: set[str] = set()
    per_evidence_counts: dict[str, int] = {}
    processed = mapped = mapping_rows = 0
    direction = normalize_direction(root.get("direction"))
    source_only = is_current_mapping_writer(mapping_contract)

    for row in evidence_rows:
        article = {
            "title": str(row["claim_text"] or ""),
            "raw_title": str(row["claim_text"] or ""),
            # The forward writer's rule candidates must be grounded in the
            # publisher title, not a generated event theme or old excerpt.
            # Direction stays as existing metadata, not recognition input.
            "tag": "" if source_only else str(root["event_type"] or ""),
            "logic": "",
            "summary": "" if source_only else str(row["excerpt"] or ""),
            "direction": direction,
        }
        mapping = map_article_to_assets(article)
        ids = database.upsert_real_event_mappings(
            conn,
            event_id=str(row["event_id"]),
            article_text=" ".join(
                str(article[key] or "")
                for key in ("title", "tag", "logic", "summary")
            ),
            source_text=str(article.get("raw_title") or article.get("title") or ""),
            source_uri=str(row["source_uri"] or ""),
            direction=direction,
            mapping_version=mapping_contract,
            candidates=list(mapping.get("security_candidates") or []),
            now=processing_time,
        )
        reference_ids = database.upsert_event_market_references(
            conn,
            event_id=str(row["event_id"]),
            source_uri=str(row["source_uri"] or ""),
            direction=direction,
            mapping_version=mapping_contract,
            references=list(mapping.get("market_references") or []),
            now=processing_time,
        )
        evidence_id = str(row["evidence_id"])
        per_evidence_counts[evidence_id] = len(ids) + len(reference_ids)
        desired_mapping_ids.update(ids)
        desired_mapping_ids.update(reference_ids)
        processed += 1
        if ids:
            mapped += 1
            mapping_rows += len(ids)

    return {
        "desired_mapping_ids": desired_mapping_ids,
        "per_evidence_counts": per_evidence_counts,
        "processed": processed,
        "mapped": mapped,
        "mapping_rows": mapping_rows,
    }


def _materialize_mapping_group(
    conn,
    *,
    root: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    mapping_contract: str,
    input_fingerprint: str,
    processing_time: int,
    include_legacy: bool = False,
) -> dict[str, int]:
    """Write, retire, fingerprint and ledger one canonical group atomically."""
    group_write = _write_mapping_group(
        conn,
        root=root,
        evidence_rows=evidence_rows,
        mapping_contract=mapping_contract,
        processing_time=processing_time,
    )
    root_id = str(root["event_id"])
    retired = database.retire_stale_rule_event_mappings(
        conn,
        event_id=root_id,
        keep_mapping_ids=sorted(group_write["desired_mapping_ids"]),
        current_mapping_version=mapping_contract,
        retire_legacy=include_legacy,
    )
    output_fingerprint, _output_count = _mapping_group_output(
        conn,
        root_id,
        mapping_contract,
    )
    for row in evidence_rows:
        evidence_id = str(row["evidence_id"])
        count = int(group_write["per_evidence_counts"][evidence_id])
        record_event_mapping_replay(
            conn,
            evidence_id,
            mapping_contract,
            evidence_version=int(row["version"]),
            root_event_id=root_id,
            root_event_version=int(root["version"]),
            input_fingerprint=input_fingerprint,
            output_fingerprint=output_fingerprint,
            status="mapped" if count else "no_match",
            mapped_count=count,
            now=processing_time,
        )
    return {
        "processed": int(group_write["processed"]),
        "mapped": int(group_write["mapped"]),
        "mapping_rows": int(group_write["mapping_rows"]),
        "retired": int(retired),
    }


def _prepare_mapping_batch(conn, cutoff: int, limit: int, *, include_legacy: bool):
    """Atomically select only the authorized cohort and pin its dependencies."""
    with database.db_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            mapping_contract, active_equities = _mapping_contract_version(
                conn, **({"include_legacy": True} if include_legacy else {}),
            )
            outdated, seeded = _seed_outdated_mapping_roots(
                conn, mapping_contract, dirty_at=int(time.time()),
                include_legacy=include_legacy,
            )
            outputs = int(conn.execute(
                "SELECT COUNT(*) FROM event_securities WHERE is_current = 1 "
                "AND mapping_version LIKE 'asset-map:%' "
                "AND lower(relation_type) <> 'benchmark_proxy'",
            ).fetchone()[0])
            if active_equities == 0 and outputs > 0:
                raise MappingReplaySafetyError(
                    "active equity security master is empty while rule mappings exist"
                )
            roots, exhausted = _pending_mapping_roots(
                conn, cutoff=cutoff, limit=limit, mapping_contract=mapping_contract,
                include_legacy=include_legacy,
            )
            observed_data_version = _mapping_data_version(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return mapping_contract, active_equities, outdated, seeded, roots, exhausted, observed_data_version


def _has_old_mapping_output(conn, group_ids, mapping_contract, *, include_legacy: bool):
    placeholders = ",".join("?" for _ in group_ids)
    forward_only = "" if include_legacy else f"AND {_forward_mapping_sql('event_securities')} "
    return conn.execute(
        "SELECT 1 FROM event_securities "
        f"WHERE event_id IN ({placeholders}) "
        "AND is_current = 1 AND mapping_version LIKE 'asset-map:%' "
        "AND mapping_version <> ? " + forward_only
        + "AND lower(relation_type) <> 'benchmark_proxy' LIMIT 1",
        (*group_ids, mapping_contract),
    ).fetchone() is not None


def enrich_event_security_mappings(
    *,
    since: int | None = None,
    limit: int = 10_000,
    include_legacy: bool = False,
) -> dict[str, Any]:
    """Process the title-only cohort; historical replay needs explicit opt-in."""
    bounded_limit = max(1, min(100_000, int(limit)))
    cutoff = int(since or 0)
    with database.get_connection() as conn:
        (
            mapping_contract, active_equities, outdated_current_groups,
            migration_seeded_groups, root_ids, exhausted_root_ids, observed_data_version,
        ) = _prepare_mapping_batch(conn, cutoff, bounded_limit, include_legacy=include_legacy)
        exhausted_groups = len(exhausted_root_ids)

        processed = mapped = mapping_rows = retired_mappings = failed_evidence = 0
        graph_dirty_groups = 0
        skipped_groups = deferred_groups = unreplayable_groups = 0
        for root_offset, selected_root_id in enumerate(root_ids):
            if processed >= bounded_limit:
                deferred_groups += len(root_ids) - root_offset
                break
            with database.db_lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    observed_data_version = (
                        _verify_mapping_dependencies_after_external_commit(
                            conn,
                            observed_data_version=observed_data_version,
                            mapping_contract=mapping_contract,
                            active_equities=active_equities,
                            failure_message=(
                                "mapping dependencies changed during replay selection"
                            ),
                            include_legacy=include_legacy,
                        )
                    )
                    root, evidence_rows = _mapping_group_rows(
                        conn, selected_root_id, include_legacy=include_legacy,
                    )
                    if not evidence_rows:
                        orphan_retired = database.retire_stale_rule_event_mappings(
                            conn,
                            event_id=str(root["event_id"]),
                            keep_mapping_ids=[],
                            current_mapping_version=mapping_contract,
                            retire_legacy=include_legacy,
                        )
                        _clear_mapping_group_dirty(conn, str(root["event_id"]))
                        _mark_mapping_group_similarity_dirty(
                            conn,
                            str(root["event_id"]),
                            dirty_at=int(time.time()),
                        )
                        conn.commit()
                        retired_mappings += orphan_retired
                        graph_dirty_groups += 1
                        skipped_groups += 1
                        unreplayable_groups += 1
                        continue
                    root_id = str(root["event_id"])
                    input_fingerprint = _mapping_group_input_fingerprint(
                        root,
                        evidence_rows,
                        mapping_contract,
                    )
                    terminal = {
                        str(row[0]): (str(row[1]), str(row[2]))
                        for row in conn.execute(
                            "SELECT evidence_id, status, output_fingerprint "
                            "FROM event_mapping_replay_ledger "
                            "WHERE mapping_version = ? AND input_fingerprint = ? "
                            "AND status IN ('mapped', 'no_match')",
                            (mapping_contract, input_fingerprint),
                        ).fetchall()
                    }
                    actual_output, _actual_count = _mapping_group_output(
                        conn,
                        root_id,
                        mapping_contract,
                    )
                    group_ids = canonical_event_group(conn, root_id)
                    if len(group_ids) > _MAX_MAPPING_GROUP_MEMBERS:
                        raise MappingReplaySafetyError(
                            "mapping replay canonical group exceeds the safe member bound"
                        )
                    has_old_output = _has_old_mapping_output(
                        conn, group_ids, mapping_contract, include_legacy=include_legacy,
                    )
                    if (
                        len(terminal) == len(evidence_rows)
                        and all(str(row["evidence_id"]) in terminal for row in evidence_rows)
                        and {item[1] for item in terminal.values()} == {actual_output}
                        and not has_old_output
                    ):
                        _clear_mapping_group_dirty(conn, root_id)
                        conn.commit()
                        skipped_groups += 1
                        continue
                    exhausted = conn.execute(
                        "SELECT 1 FROM event_mapping_replay_ledger "
                        "WHERE mapping_version = ? AND input_fingerprint = ? "
                        "AND status = 'failed' AND attempts >= ? LIMIT 1",
                        (
                            mapping_contract,
                            input_fingerprint,
                            _MAPPING_REPLAY_MAX_ATTEMPTS,
                        ),
                    ).fetchone()
                    if exhausted is not None:
                        _clear_mapping_group_dirty(conn, root_id)
                        conn.commit()
                        if root_id not in exhausted_root_ids:
                            exhausted_root_ids.add(root_id)
                            exhausted_groups += 1
                        skipped_groups += 1
                        continue
                    if (
                        processed > 0
                        and processed + len(evidence_rows) > bounded_limit
                    ):
                        # Preserve canonical-group atomicity without turning the
                        # cron's evidence budget into ``limit × group_size``.
                        # One oversized first group may run alone, but every
                        # group is still bounded by _MAX_MAPPING_GROUP_EVIDENCE.
                        conn.commit()
                        deferred_groups += len(root_ids) - root_offset
                        break

                    processing_time = int(time.time())
                    materialized = _materialize_mapping_group(
                        conn,
                        root=root,
                        evidence_rows=evidence_rows,
                        mapping_contract=mapping_contract,
                        input_fingerprint=input_fingerprint,
                        processing_time=processing_time,
                        include_legacy=include_legacy,
                    )
                    _clear_mapping_group_dirty(conn, root_id)
                    _mark_mapping_group_similarity_dirty(
                        conn,
                        root_id,
                        dirty_at=processing_time,
                    )
                    conn.commit()
                    processed += int(materialized["processed"])
                    mapped += int(materialized["mapped"])
                    mapping_rows += int(materialized["mapping_rows"])
                    retired_mappings += int(materialized["retired"])
                    graph_dirty_groups += 1
                except (
                    EventCoreConflictError,
                    KeyError,
                    TypeError,
                    ValueError,
                    sqlite3.IntegrityError,
                ) as exc:
                    conn.rollback()
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        observed_data_version = (
                            _verify_mapping_dependencies_after_external_commit(
                                conn,
                                observed_data_version=observed_data_version,
                                mapping_contract=mapping_contract,
                                active_equities=active_equities,
                                failure_message=(
                                    "mapping dependencies changed before failure recording"
                                ),
                                include_legacy=include_legacy,
                            )
                        )
                        failed_root, failed_rows = _mapping_group_rows(
                            conn,
                            selected_root_id,
                            include_legacy=include_legacy,
                        )
                        failed_input = _mapping_group_input_fingerprint(
                            failed_root,
                            failed_rows,
                            mapping_contract,
                        )
                        already_terminal = conn.execute(
                            "SELECT 1 FROM event_mapping_replay_ledger "
                            "WHERE mapping_version = ? AND input_fingerprint = ? "
                            "AND status IN ('mapped', 'no_match') LIMIT 1",
                            (mapping_contract, failed_input),
                        ).fetchone()
                        if already_terminal is not None:
                            # A terminal audit row cannot be rewritten as a
                            # failure. This path means live output drift needed
                            # repair but the deterministic repair itself failed;
                            # retaining the dirty wake-up and failing the lane is
                            # safer than reporting a successful no-op forever.
                            raise MappingReplaySafetyError(
                                "terminal mapping output drift repair failed"
                            )
                        if already_terminal is None:
                            failed_output, _failed_count = _mapping_group_output(
                                conn,
                                str(failed_root["event_id"]),
                                mapping_contract,
                            )
                            failure_time = int(time.time())
                            failure_code = type(exc).__name__[:64]
                            durable_attempts: list[int] = []
                            for row in failed_rows:
                                durable_attempts.append(
                                    record_event_mapping_replay(
                                        conn,
                                        str(row["evidence_id"]),
                                        mapping_contract,
                                        evidence_version=int(row["version"]),
                                        root_event_id=str(failed_root["event_id"]),
                                        root_event_version=int(failed_root["version"]),
                                        input_fingerprint=failed_input,
                                        output_fingerprint=failed_output,
                                        status="failed",
                                        failure_code=failure_code,
                                        now=failure_time,
                                    )
                                )
                            if durable_attempts and min(durable_attempts) >= (
                                _MAPPING_REPLAY_MAX_ATTEMPTS
                            ):
                                # The durable ledger now suppresses this exact
                                # input. Remove its dirty wake-up in the same
                                # transaction so the next cron can advance to a
                                # different root without one cleanup-only pass.
                                _clear_mapping_group_dirty(
                                    conn,
                                    str(failed_root["event_id"]),
                                )
                                failed_root_id = str(failed_root["event_id"])
                                if failed_root_id not in exhausted_root_ids:
                                    exhausted_root_ids.add(failed_root_id)
                                    exhausted_groups += 1
                            failed_evidence += len(failed_rows)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                    logger.warning(
                        "[EventCore] mapping group failed root=%s error_type=%s",
                        selected_root_id,
                        type(exc).__name__,
                    )
                except BaseException:
                    conn.rollback()
                    raise
    migration = event_mapping_migration_status(include_legacy=include_legacy)
    return {
        "forward_only": not include_legacy,
        "forward_complete": bool(migration.get("forward_complete")),
        "mapping_contract": mapping_contract,
        "active_equities": active_equities,
        "outdated_current_groups": outdated_current_groups,
        "migration_seeded_groups": migration_seeded_groups,
        "migration": migration,
        "migration_complete": bool(migration["migration_complete"]),
        "selected_groups": len(root_ids),
        "processed_evidence": processed,
        "mapped_evidence": mapped,
        "mapping_rows": mapping_rows,
        "retired_mappings": retired_mappings,
        "graph_dirty_groups": graph_dirty_groups,
        "failed_evidence": failed_evidence,
        "skipped_groups": skipped_groups,
        "deferred_groups": deferred_groups,
        "unreplayable_groups": unreplayable_groups,
        "exhausted_groups": exhausted_groups,
    }


def _event_graph_rows(
    conn,
    cutoff: int,
    *,
    include_root_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    extra_roots = tuple(sorted(set(str(item) for item in include_root_ids)))
    extra_root_select = ""
    root_params: tuple[object, ...] = (int(cutoff),)
    if extra_roots:
        placeholders = ",".join("?" for _ in extra_roots)
        extra_root_select = (
            " UNION SELECT event_id FROM events "
            f"WHERE event_id IN ({placeholders}) "
            "AND merge_state = 'active' AND merged_into IS NULL"
        )
        root_params = (*root_params, *extra_roots)
    events = {
        str(row["event_id"]): {
            **dict(row),
            "member_ids": set(),
            "sources": set(),
            "snapshots": set(),
            "mappings": {},
        }
        for row in conn.execute(
            "WITH RECURSIVE recent_lineage(event_id, merged_into, depth) AS ("
            " SELECT event_id, merged_into, 0 FROM events WHERE occurred_at >= ?"
            " UNION ALL SELECT parent.event_id, parent.merged_into, "
            " recent.depth + 1 FROM recent_lineage AS recent JOIN events AS parent "
            " ON parent.event_id = recent.merged_into WHERE recent.depth < 64"
            "), scoped_roots(event_id) AS ("
            " SELECT DISTINCT event_id FROM recent_lineage "
            " WHERE merged_into IS NULL"
            f"{extra_root_select}"
            ") SELECT event_id, title, event_type, direction, priority, occurred_at "
            "FROM events WHERE merge_state = 'active' AND merged_into IS NULL "
            "AND event_id IN (SELECT event_id FROM scoped_roots) "
            "ORDER BY occurred_at, event_id",
            root_params,
        ).fetchall()
    }
    if not events:
        return []
    # Merges intentionally leave immutable evidence and mappings on the member
    # event that produced them. Resolve every recent active root to its entire
    # canonical tree before evaluating publisher independence; otherwise an
    # A(alpha)+B(beta) group looks alpha-only after B is merged and a later beta
    # replay can be mistaken for independent confirmation.
    member_roots: dict[str, str] = {}
    root_ids = tuple(events)
    for offset in range(0, len(root_ids), _EVENT_GROUP_BATCH_SIZE):
        batch = root_ids[offset : offset + _EVENT_GROUP_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        group_rows = conn.execute(
            "WITH RECURSIVE event_group(root_id, event_id, depth) AS ("
            " SELECT event_id, event_id, 0 FROM events "
            f" WHERE event_id IN ({placeholders})"
            " UNION ALL "
            " SELECT parent.root_id, child.event_id, parent.depth + 1 "
            " FROM event_group AS parent JOIN events AS child "
            " ON child.merged_into = parent.event_id WHERE parent.depth < 64"
            ") SELECT root_id, event_id, depth FROM event_group "
            "ORDER BY root_id, depth, event_id LIMIT ?",
            (*batch, _MAX_EVENT_GROUP_MEMBERS_PER_BATCH + 1),
        ).fetchall()
        if len(group_rows) > _MAX_EVENT_GROUP_MEMBERS_PER_BATCH or any(
            int(row["depth"]) >= 64 for row in group_rows
        ):
            raise EventCoreConflictError(
                "event graph canonical group exceeds the safe traversal bound"
            )
        for row in group_rows:
            root_id = str(row["root_id"])
            member_id = str(row["event_id"])
            previous_root = member_roots.setdefault(member_id, root_id)
            if previous_root != root_id:
                raise EventCoreConflictError(
                    "event graph member belongs to multiple canonical roots"
                )
            events[root_id]["member_ids"].add(member_id)

    member_ids = tuple(sorted(member_roots))
    for offset in range(0, len(member_ids), _EVENT_GROUP_BATCH_SIZE):
        batch = member_ids[offset : offset + _EVENT_GROUP_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT event_id, source_uri, snapshot_hash FROM event_evidence "
            f"WHERE event_id IN ({placeholders}) AND mapping_id IS NULL "
            "ORDER BY event_id, evidence_id",
            batch,
        ).fetchall():
            root_id = member_roots[str(row["event_id"])]
            host = _source_host(row["source_uri"])
            if host:
                events[root_id]["sources"].add(host)
            snapshot_hash = str(row["snapshot_hash"] or "").casefold()
            if len(snapshot_hash) == 64:
                events[root_id]["snapshots"].add(snapshot_hash)
        for row in conn.execute(
            "SELECT event_id, security_id, lower(relation_type) relation_type "
            "FROM event_securities "
            f"WHERE event_id IN ({placeholders}) AND is_current = 1 "
            "AND lower(relation_type) <> 'benchmark_proxy' "
            "ORDER BY event_id, security_id, lower(relation_type)",
            batch,
        ).fetchall():
            root_id = member_roots[str(row["event_id"])]
            relation = str(row["relation_type"])
            if relation in _RELATION_TYPES:
                security_id = str(row["security_id"])
                mappings = events[root_id]["mappings"]
                previous = mappings.get(security_id)
                if (
                    previous is None
                    or _RELATION_PRIORITY[relation] < _RELATION_PRIORITY[previous]
                ):
                    mappings[security_id] = relation
    return list(events.values())


def _candidate_pairs(
    events: list[dict[str, Any]],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return (exact-title pairs, shared-security pairs), kept separate.

    Exact normalized-title pairs are the only automatic merge candidates, so
    they must never compete for the pair budget with the far larger
    shared-security buckets: broad concept pools map dozens of events to the
    same securities and would otherwise crowd every wire duplicate out of a
    lexicographically truncated combined set.
    """
    exact: dict[str, list[str]] = defaultdict(list)
    by_security: dict[str, list[str]] = defaultdict(list)
    for event in events:
        key = normalize_event_identity_key(str(event["title"] or ""))
        if len(key) >= 12:
            exact[key].append(str(event["event_id"]))
        for security_id in event["mappings"]:
            by_security[security_id].append(str(event["event_id"]))
    exact_pairs: set[tuple[str, str]] = set()
    security_pairs: set[tuple[str, str]] = set()
    for groups, pairs in ((exact, exact_pairs), (by_security, security_pairs)):
        for group in groups.values():
            # Generic recurring headlines and broad concepts must not create an
            # unbounded quadratic graph.  Recent events are ordered before this
            # function; cap each evidence bucket deterministically.
            selected = group[-_CANDIDATE_BUCKET_LIMIT:]
            pairs.update(
                tuple(sorted(pair))
                for pair in itertools.combinations(selected, 2)
            )
    return exact_pairs, security_pairs


def _fixed_window_anchors(
    events: list[dict[str, Any]],
    *,
    window_seconds: int,
    group_key: Callable[[dict[str, Any]], Hashable],
) -> dict[str, str]:
    """Assign each event to a non-transitive, first-event-anchored window."""
    grouped: dict[Hashable, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[group_key(event)].append(event)
    anchors: dict[str, str] = {}
    for group in grouped.values():
        ordered = sorted(
            group,
            key=lambda item: (
                int(item["occurred_at"]),
                -float(item["priority"] or 0.0),
                str(item["event_id"]),
            ),
        )
        anchor_id = ""
        anchor_at = 0
        for event in ordered:
            event_id = str(event["event_id"])
            occurred_at = int(event["occurred_at"])
            if not anchor_id or occurred_at - anchor_at > window_seconds:
                anchor_id = event_id
                anchor_at = occurred_at
            anchors[event_id] = anchor_id
    return anchors


def _merge_anchor_maps(
    events: list[dict[str, Any]],
    identity_keys: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build non-transitive anchors for the two hard-merge contracts."""
    confirmation_anchors = _fixed_window_anchors(
        events,
        window_seconds=36 * 3600,
        group_key=lambda item: (
            identity_keys[str(item["event_id"])],
            tuple(sorted(item["snapshots"])),
        )
        if item["snapshots"]
        else ("unqualified", str(item["event_id"])),
    )
    replay_anchors = _fixed_window_anchors(
        events,
        window_seconds=2 * 3600,
        group_key=lambda item: (
            identity_keys[str(item["event_id"])],
            tuple(sorted(_publisher_set(item))),
            tuple(sorted(item["snapshots"])),
        )
        if _publisher_set(item) and item["snapshots"]
        else ("unqualified", str(item["event_id"])),
    )
    return confirmation_anchors, replay_anchors


def _automatic_merge_decision(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    identity_keys: dict[str, str],
    confirmation_anchors: dict[str, str],
    replay_anchors: dict[str, str],
) -> tuple[str, str, int] | None:
    """Return the audited target/reason only for a fully qualified hard merge."""
    left_id = str(left["event_id"])
    right_id = str(right["event_id"])
    left_key = identity_keys[left_id]
    right_key = identity_keys[right_id]
    if len(left_key) < 12 or left_key != right_key:
        return None

    # Frozen snapshots are the fact boundary.  Intersection is deliberately
    # insufficient: {old, contradictory} and {old} must not be merged merely
    # because one stale hash happens to overlap.
    if not left["snapshots"] or left["snapshots"] != right["snapshots"]:
        return None

    left_publishers = _publisher_set(left)
    right_publishers = _publisher_set(right)
    if not left_publishers or not right_publishers:
        return None

    independently_confirmed = bool(
        left_publishers.isdisjoint(right_publishers)
        and confirmation_anchors[left_id] == confirmation_anchors[right_id]
    )
    same_publisher_replay = bool(
        left_publishers == right_publishers
        and replay_anchors[left_id] == replay_anchors[right_id]
    )
    # Partially overlapping publisher sets are neither independent evidence nor
    # one publisher replay and therefore fail closed.
    if independently_confirmed:
        return (
            confirmation_anchors[left_id],
            "跨独立来源发布方的完整规范化标题与冻结快照均一致，且位于固定36小时窗口",
            1,
        )
    if same_publisher_replay:
        return (
            replay_anchors[left_id],
            "同一来源、完整规范化标题与快照均一致，且位于固定2小时窗口",
            0,
        )
    return None


def _anchor_qualified_exact_pairs(
    events: list[dict[str, Any]],
    *,
    identity_keys: dict[str, str],
    confirmation_anchors: dict[str, str],
    replay_anchors: dict[str, str],
) -> set[tuple[str, str]]:
    """Return every hard-qualified source-to-anchor edge in linear space.

    ``_candidate_pairs`` deliberately caps the quadratic neighborhood used for
    reversible similarity edges.  Hard merges have a stronger structure: both
    contracts select one fixed anchor, so checking each event against its two
    possible anchors covers every admissible edge without enumerating a large
    exact-title bucket.  This keeps an eligible old pair outside the recent
    bucket window from being starved forever.
    """
    by_id = {str(item["event_id"]): item for item in events}
    pairs: set[tuple[str, str]] = set()
    for event_id in sorted(by_id):
        targets = {
            str(confirmation_anchors[event_id]),
            str(replay_anchors[event_id]),
        }
        for target_id in sorted(targets):
            if target_id == event_id:
                continue
            decision = _automatic_merge_decision(
                by_id[event_id],
                by_id[target_id],
                identity_keys=identity_keys,
                confirmation_anchors=confirmation_anchors,
                replay_anchors=replay_anchors,
            )
            if decision is not None and decision[0] == target_id:
                pairs.add(tuple(sorted((event_id, target_id))))
    return pairs


def _similarity_output_signature(item: dict[str, Any]) -> tuple[object, ...]:
    """Mirror the stable fields persisted by ``upsert_event_similarity``."""
    common = json.dumps(
        sorted({str(value) for value in item["common"]}),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    paths = json.dumps(
        item["paths"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        float(item["score"]),
        str(item["rationale"]),
        common,
        paths,
    )


def _event_graph_member_ids(
    events_by_id: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    """Return the complete, stable member set for canonical graph roots."""
    member_ids: set[str] = set()
    for root_id in sorted(events_by_id):
        members = {
            str(member_id)
            for member_id in events_by_id[root_id].get("member_ids", ())
        }
        if root_id not in members:
            raise EventCoreConflictError(
                "event graph canonical root is missing from its member set"
            )
        member_ids.update(members)
    return tuple(sorted(member_ids))


def _incident_similarity_rows(
    conn,
    member_ids: tuple[str, ...],
) -> list[tuple[str, str, float, str, str, str]]:
    """Load current-contract edges incident to any member without a table scan.

    Similarity rows can still be keyed to a merged child for compatibility with
    an earlier writer. Query both endpoints in bounded batches, then collapse
    the overlap into one deterministic pair ordering.
    """
    rows_by_pair: dict[
        tuple[str, str], tuple[str, str, float, str, str, str]
    ] = {}
    ordered_ids = tuple(sorted(set(str(member_id) for member_id in member_ids)))
    endpoint_indexes = (
        ("event_id", "idx_event_similarities_contract_left"),
        ("similar_event_id", "idx_event_similarities_contract_right"),
    )
    for endpoint, index_name in endpoint_indexes:
        for offset in range(0, len(ordered_ids), _SIMILARITY_QUERY_BATCH):
            batch = ordered_ids[offset : offset + _SIMILARITY_QUERY_BATCH]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT event_id, similar_event_id, similarity, rationale, "
                "common_securities, shared_paths FROM event_similarities "
                f"INDEXED BY {index_name} "
                "WHERE method = ? AND model_version = ? "
                f"AND {endpoint} IN ({placeholders}) "
                "ORDER BY event_id, similar_event_id",
                (_SIMILARITY_METHOD, _SIMILARITY_MODEL_VERSION, *batch),
            ).fetchall()
            for row in rows:
                item = (
                    str(row[0]),
                    str(row[1]),
                    float(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                )
                pair = (item[0], item[1])
                previous = rows_by_pair.setdefault(pair, item)
                if previous != item:
                    raise EventCoreConflictError(
                        "event similarity incident query returned conflicting rows"
                    )
    return [rows_by_pair[pair] for pair in sorted(rows_by_pair)]


def _unchanged_similarity_pairs(
    candidates: list[dict[str, Any]],
    rows: list[tuple[str, str, float, str, str, str]],
) -> set[tuple[str, str]]:
    """Find current-model pairs whose derived durable output is unchanged."""
    desired = {
        (str(item["left"]), str(item["right"])): _similarity_output_signature(item)
        for item in candidates
    }
    unchanged: set[tuple[str, str]] = set()
    for row in rows:
        pair = (row[0], row[1])
        expected = desired.get(pair)
        if expected is not None and expected == row[2:]:
            unchanged.add(pair)
    return unchanged


def _similarity_candidate(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any] | None:
    """Derive one reversible edge from the complete current pair state."""
    left_id = str(left["event_id"])
    right_id = str(right["event_id"])
    left_key = normalize_event_identity_key(str(left["title"] or ""))
    right_key = normalize_event_identity_key(str(right["title"] or ""))
    exact_title = len(left_key) >= 12 and left_key == right_key
    common = sorted(set(left["mappings"]) & set(right["mappings"]))
    if not exact_title and not common:
        return None

    delta = abs(int(left["occurred_at"]) - int(right["occurred_at"]))
    title_score = title_similarity(str(left["title"]), str(right["title"]))
    same_direction = normalize_direction(str(left["direction"])) == normalize_direction(
        str(right["direction"])
    )
    same_type = str(left["event_type"] or "") == str(right["event_type"] or "")
    score = (
        0.62 * title_score
        + (0.20 if common else 0.0)
        + (0.08 if same_direction else 0.0)
        + (0.05 if same_type else 0.0)
        + (0.05 if delta <= 7 * 86400 else 0.0)
    )
    score = max(0.0, min(1.0, score))
    if score < 0.62:
        return None
    shared_paths = [
        {
            "security_id": security_id,
            "left_relation": left["mappings"][security_id],
            "right_relation": right["mappings"][security_id],
        }
        for security_id in common[:20]
    ]
    reasons = [f"标题语义相似度 {title_score:.2f}"]
    if common:
        reasons.append(f"共同证券 {len(common)} 只")
    if same_direction:
        reasons.append("方向一致")
    if same_type:
        reasons.append("事件类型一致")
    return {
        "left": left_id,
        "right": right_id,
        "score": score,
        "common": common,
        "paths": shared_paths,
        "rationale": "；".join(reasons),
    }


def _similarity_reconciliation(
    conn,
    events_by_id: dict[str, dict[str, Any]],
    rows: list[tuple[str, str, float, str, str, str]],
    *,
    owned_root_ids: set[str],
) -> tuple[
    list[tuple[str, str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Plan old-reader-safe deletes, child migrations and current updates."""
    existing_pairs = {(row[0], row[1]) for row in rows}
    claimed_pairs = set(existing_pairs)
    deletes: list[tuple[str, str]] = []
    migrations: list[dict[str, Any]] = []
    updates_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        raw_pair = (str(row[0]), str(row[1]))
        left_root = canonical_event_id(conn, raw_pair[0])
        right_root = canonical_event_id(conn, raw_pair[1])
        if left_root not in owned_root_ids and right_root not in owned_root_ids:
            continue
        if left_root == right_root:
            deletes.append(raw_pair)
            continue
        canonical_pair = tuple(sorted((left_root, right_root)))
        left = events_by_id.get(canonical_pair[0])
        right = events_by_id.get(canonical_pair[1])
        if left is None or right is None:
            raise EventCoreConflictError(
                "event similarity reconciliation is missing canonical root state"
            )
        desired = _similarity_candidate(left, right)
        if desired is None:
            deletes.append(raw_pair)
            continue
        if raw_pair != canonical_pair:
            if canonical_pair not in claimed_pairs:
                migrations.append(
                    {
                        "raw_left": raw_pair[0],
                        "raw_right": raw_pair[1],
                        "desired": desired,
                    }
                )
                claimed_pairs.add(canonical_pair)
            else:
                deletes.append(raw_pair)
            continue
        expected = _similarity_output_signature(desired)
        actual = (
            float(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
        )
        if actual != expected:
            updates_by_pair[canonical_pair] = desired
    return deletes, migrations, [updates_by_pair[key] for key in sorted(updates_by_pair)]


def _similarity_reconciliation_events(
    conn,
    scoped_events_by_id: dict[str, dict[str, Any]],
    rows: list[tuple[str, str, float, str, str, str]],
) -> dict[str, dict[str, Any]]:
    """Hydrate only out-of-scope roots connected to an owned current edge.

    Similarity rows are mutable public evidence.  A scheduled cutoff owns every
    current-model edge incident to one of its canonical groups, even when the
    other endpoint is older.  Loading the counterpart is bounded by the
    existing edge set and by ``_event_graph_rows``' canonical-group limits; it
    does not make that old endpoint eligible for new candidate generation.
    """
    owned_root_ids = set(scoped_events_by_id)
    if not owned_root_ids:
        return dict(scoped_events_by_id)
    missing_root_ids: set[str] = set()
    for row in rows:
        left_root = canonical_event_id(conn, row[0])
        right_root = canonical_event_id(conn, row[1])
        if left_root in owned_root_ids and right_root not in owned_root_ids:
            missing_root_ids.add(right_root)
        elif right_root in owned_root_ids and left_root not in owned_root_ids:
            missing_root_ids.add(left_root)
    expanded = dict(scoped_events_by_id)
    ordered_missing = sorted(missing_root_ids)
    for offset in range(0, len(ordered_missing), _EVENT_GROUP_BATCH_SIZE):
        batch = tuple(ordered_missing[offset : offset + _EVENT_GROUP_BATCH_SIZE])
        hydrated = _event_graph_rows(
            conn,
            2**63 - 1,
            include_root_ids=batch,
        )
        for event in hydrated:
            expanded[str(event["event_id"])] = event
    unresolved = missing_root_ids - set(expanded)
    if unresolved:
        raise EventCoreConflictError(
            "event similarity reconciliation cannot hydrate canonical root state"
        )
    return expanded


def _delete_event_graph_similarity(
    conn,
    left_id: str,
    right_id: str,
) -> bool:
    """Delete one mutable current-model edge for previous-reader compatibility."""
    cursor = conn.execute(
        "DELETE FROM event_similarities "
        "WHERE event_id = ? AND similar_event_id = ? "
        "AND method = ? AND model_version = ?",
        (
            str(left_id),
            str(right_id),
            _SIMILARITY_METHOD,
            _SIMILARITY_MODEL_VERSION,
        ),
    )
    return cursor.rowcount == 1


def _migrate_event_graph_similarity(
    conn,
    item: dict[str, Any],
    *,
    now: int,
) -> bool:
    """Move a child-keyed edge to its canonical pair in one durable write."""
    desired = item["desired"]
    common, paths = _similarity_output_signature(desired)[2:]
    cursor = conn.execute(
        "UPDATE event_similarities SET event_id = ?, similar_event_id = ?, "
        "similarity = ?, rationale = ?, common_securities = ?, shared_paths = ?, "
        "computed_at = ?, version = version + 1, updated_at = MAX(updated_at, ?) "
        "WHERE event_id = ? AND similar_event_id = ? "
        "AND method = ? AND model_version = ? "
        "AND NOT EXISTS (SELECT 1 FROM event_similarities AS current "
        " WHERE current.event_id = ? AND current.similar_event_id = ? "
        " AND current.method = ? AND current.model_version = ?)",
        (
            str(desired["left"]),
            str(desired["right"]),
            float(desired["score"]),
            str(desired["rationale"]),
            common,
            paths,
            int(now),
            int(now),
            str(item["raw_left"]),
            str(item["raw_right"]),
            _SIMILARITY_METHOD,
            _SIMILARITY_MODEL_VERSION,
            str(desired["left"]),
            str(desired["right"]),
            _SIMILARITY_METHOD,
            _SIMILARITY_MODEL_VERSION,
        ),
    )
    return cursor.rowcount == 1


def _merge_graph_row_state(
    events_by_id: dict[str, dict[str, Any]],
    source_id: str,
    target_id: str,
) -> None:
    """Mirror the graph-relevant effect of one audited merge in memory."""
    source = events_by_id[source_id]
    target = events_by_id[target_id]
    target["member_ids"].update(source["member_ids"])
    target["sources"].update(source["sources"])
    target["snapshots"].update(source["snapshots"])
    target["priority"] = max(
        float(target["priority"] or 0.0),
        float(source["priority"] or 0.0),
    )
    for security_id, relation in source["mappings"].items():
        previous = target["mappings"].get(security_id)
        if previous is None or _RELATION_PRIORITY[relation] < _RELATION_PRIORITY[previous]:
            target["mappings"][security_id] = relation
    del events_by_id[source_id]


def resolve_event_graph(
    *,
    since: int | None = None,
    max_pairs: int = 25_000,
    allow_merges: bool = True,
) -> dict[str, Any]:
    """Merge confirmed duplicates within an admitted-write pair budget.

    Qualification scans stay bounded by ``_candidate_pairs``' per-bucket caps;
    ``max_pairs`` limits durable merge/similarity writes rather than charging
    permanently ineligible exact-title pairs against the merge quota.
    """
    cutoff = int(time.time() - 30 * 86400) if since is None else int(since)
    budget = max(1, min(250_000, int(max_pairs)))
    graph_dirty_markers: tuple[tuple[str, int], ...] = ()
    graph_dirty_marker_ids: tuple[str, ...] = ()
    graph_dirty_root_ids: tuple[str, ...] = ()
    with database.db_lock:
        with database.get_connection() as conn:
            dirty_rows = conn.execute(
                "SELECT root_event_id, generation "
                "FROM event_similarity_replay_dirty_roots "
                "ORDER BY dirty_at, root_event_id LIMIT ?",
                (budget,),
            ).fetchall()
            graph_dirty_markers = tuple(
                (str(row[0]), int(row[1])) for row in dirty_rows
            )
            graph_dirty_marker_ids = tuple(item[0] for item in graph_dirty_markers)
            graph_dirty_root_ids = tuple(
                sorted(
                    {
                        canonical_event_id(conn, marker_id)
                        for marker_id in graph_dirty_marker_ids
                    }
                )
            )
            events = _event_graph_rows(
                conn,
                cutoff,
                include_root_ids=graph_dirty_root_ids,
            )
    by_id = {str(item["event_id"]): item for item in events}
    missing_dirty_roots = set(graph_dirty_root_ids) - set(by_id)
    if missing_dirty_roots:
        raise EventCoreConflictError(
            "event graph cannot load a pending similarity reconciliation root"
        )
    identity_keys = {
        event_id: normalize_event_identity_key(str(item["title"] or ""))
        for event_id, item in by_id.items()
    }
    confirmation_anchors, replay_anchors = _merge_anchor_maps(
        events,
        identity_keys,
    )
    exact_pairs, security_pairs = _candidate_pairs(events)
    exact_pairs.update(
        _anchor_qualified_exact_pairs(
            events,
            identity_keys=identity_keys,
            confirmation_anchors=confirmation_anchors,
            replay_anchors=replay_anchors,
        )
    )
    merge_candidates: dict[tuple[str, str], tuple[str, int]] = {}
    non_merge_exact_pairs: set[tuple[str, str]] = set()
    # Hard qualification happens before the write budget is consumed.  A large,
    # permanently ineligible lexicographic prefix can therefore no longer starve
    # a later evidence-qualified duplicate on every cron pass.
    for left_id, right_id in sorted(exact_pairs):
        decision = _automatic_merge_decision(
            by_id[left_id],
            by_id[right_id],
            identity_keys=identity_keys,
            confirmation_anchors=confirmation_anchors,
            replay_anchors=replay_anchors,
        )
        if decision is None:
            non_merge_exact_pairs.add((left_id, right_id))
            continue
        target_id = decision[0]
        admitted_edge = False
        for source_id in (left_id, right_id):
            if source_id == target_id:
                continue
            # A third publisher must not let an old same-publisher replay wash
            # through a pair that happens to be independently confirmed.  Each
            # irreversible source->anchor edge must independently satisfy the
            # hard predicate against the actual target.
            edge_decision = _automatic_merge_decision(
                by_id[source_id],
                by_id[target_id],
                identity_keys=identity_keys,
                confirmation_anchors=confirmation_anchors,
                replay_anchors=replay_anchors,
            )
            if edge_decision is None or edge_decision[0] != target_id:
                continue
            merge_candidates.setdefault(
                (source_id, target_id), (edge_decision[1], edge_decision[2])
            )
            admitted_edge = True
        if not admitted_edge:
            non_merge_exact_pairs.add((left_id, right_id))

    # max_pairs now bounds admitted graph writes.  Ineligible exact-title pairs
    # remain eligible for a similarity edge but never consume merge quota.
    # Collapse same-publisher replay windows before independent publishers.
    # Otherwise a tiny budget can first create an alpha+beta root and make a
    # beta replay that was already present look like a later partially
    # overlapping source. This phase ordering preserves the deliberate policy
    # that a genuinely later beta replay cannot wash into an existing mixed
    # group, while making one fixed candidate snapshot budget-independent.
    ordered_merge_candidates = sorted(
        merge_candidates.items(),
        key=lambda item: (item[1][1], item[0]),
    )
    selected_merges = ordered_merge_candidates[:budget] if allow_merges else []
    similarity_pool = non_merge_exact_pairs | (security_pairs - exact_pairs)

    qualified_similarities: list[dict[str, Any]] = []
    for left_id, right_id in sorted(similarity_pool):
        candidate = _similarity_candidate(by_id[left_id], by_id[right_id])
        if candidate is not None:
            qualified_similarities.append(candidate)

    unchanged_pairs: set[tuple[str, str]] = set()
    reconciliation_deletes: list[tuple[str, str]] = []
    reconciliation_migrations: list[dict[str, Any]] = []
    reconciliation_updates: list[dict[str, Any]] = []
    reconciliation_events_by_id: dict[str, dict[str, Any]] = dict(by_id)
    with database.db_lock:
        with database.get_connection() as conn:
            incident_rows = _incident_similarity_rows(
                conn,
                _event_graph_member_ids(by_id),
            )
            if qualified_similarities:
                unchanged_pairs = _unchanged_similarity_pairs(
                    qualified_similarities,
                    incident_rows,
                )
            reconciliation_events_by_id = _similarity_reconciliation_events(
                conn,
                by_id,
                incident_rows,
            )
            (
                reconciliation_deletes,
                reconciliation_migrations,
                reconciliation_updates,
            ) = _similarity_reconciliation(
                conn,
                reconciliation_events_by_id,
                incident_rows,
                owned_root_ids=set(by_id),
            )
    similarity_write_candidates = [
        item
        for item in qualified_similarities
        if (str(item["left"]), str(item["right"])) not in unchanged_pairs
    ]
    merged = similarities = reconciled_similarities = skipped = 0
    deleted_similarities = migrated_similarities = 0
    selected_similarity_deletes = selected_similarity_migrations = 0
    selected_similarity_updates = selected_similarity_writes = 0
    live_pending_similarity_deletes = len(reconciliation_deletes)
    live_pending_similarity_migrations = len(reconciliation_migrations)
    live_pending_similarity_updates = len(reconciliation_updates)
    live_pending_similarity_writes = len(similarity_write_candidates)
    live_deferred_merge_edges: set[tuple[str, str]] = set()
    graph_dirty_groups_acked = 0
    with database.db_lock:
        with database.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                has_graph_writes = bool(
                    selected_merges
                    or graph_dirty_marker_ids
                    or qualified_similarities
                    or reconciliation_deletes
                    or reconciliation_migrations
                    or reconciliation_updates
                    or similarity_write_candidates
                    or len(reconciliation_events_by_id) > len(by_id)
                )
                fresh_by_id: dict[str, dict[str, Any]] = {}
                if has_graph_writes:
                    fresh_events = _event_graph_rows(
                        conn,
                        cutoff,
                        include_root_ids=graph_dirty_root_ids,
                    )
                    fresh_by_id = {
                        str(item["event_id"]): item for item in fresh_events
                    }
                # Re-evaluate each irreversible edge against the state produced
                # by every earlier merge in this same transaction. One shared
                # pre-state can otherwise admit two >2h beta replays through an
                # alpha witness and manufacture publisher independence.
                for (source, target), (_reason, _merge_phase) in selected_merges:
                    try:
                        source_root = canonical_event_id(conn, source)
                        target_root = canonical_event_id(conn, target)
                        if source_root == target_root:
                            continue
                        if source_root not in fresh_by_id or target_root not in fresh_by_id:
                            skipped += 1
                            continue
                        current_events = list(fresh_by_id.values())
                        fresh_identity_keys = {
                            event_id: normalize_event_identity_key(
                                str(item["title"] or "")
                            )
                            for event_id, item in fresh_by_id.items()
                        }
                        fresh_confirmation, fresh_replay = _merge_anchor_maps(
                            current_events,
                            fresh_identity_keys,
                        )
                        decision = _automatic_merge_decision(
                            fresh_by_id[source_root],
                            fresh_by_id[target_root],
                            identity_keys=fresh_identity_keys,
                            confirmation_anchors=fresh_confirmation,
                            replay_anchors=fresh_replay,
                        )
                        if decision is None or decision[0] != target_root:
                            skipped += 1
                            continue
                        merge_events(
                            conn,
                            source_root,
                            target_root,
                            reason=decision[1],
                            actor="event-resolver-v1",
                        )
                    except (EventCoreConflictError, KeyError, ValueError):
                        skipped += 1
                        continue
                    _merge_graph_row_state(fresh_by_id, source_root, target_root)
                    merged += 1

                write_slots = max(0, budget - len(selected_merges))
                live_owned_root_ids = set(fresh_by_id)
                live_incident_rows = _incident_similarity_rows(
                    conn,
                    _event_graph_member_ids(fresh_by_id),
                )
                live_reconciliation_by_id = _similarity_reconciliation_events(
                    conn,
                    fresh_by_id,
                    live_incident_rows,
                )
                (
                    live_deletes,
                    live_migrations,
                    live_updates,
                ) = _similarity_reconciliation(
                    conn,
                    live_reconciliation_by_id,
                    live_incident_rows,
                    owned_root_ids=live_owned_root_ids,
                )
                live_pending_similarity_deletes = len(live_deletes)
                live_pending_similarity_migrations = len(live_migrations)
                live_pending_similarity_updates = len(live_updates)
                graph_time = int(time.time())
                delete_candidates = live_deletes[:write_slots]
                selected_similarity_deletes = len(delete_candidates)
                for left_id, right_id in delete_candidates:
                    if not _delete_event_graph_similarity(conn, left_id, right_id):
                        raise EventCoreConflictError(
                            "event similarity delete changed during reconciliation"
                        )
                    deleted_similarities += 1
                write_slots -= selected_similarity_deletes

                migration_candidates = live_migrations[:write_slots]
                selected_similarity_migrations = len(migration_candidates)
                reconciled_pairs: set[tuple[str, str]] = set()
                for item in migration_candidates:
                    if not _migrate_event_graph_similarity(
                        conn,
                        item,
                        now=graph_time,
                    ):
                        raise EventCoreConflictError(
                            "event similarity migration changed during reconciliation"
                        )
                    migrated_similarities += 1
                    reconciled_similarities += 1
                    desired = item["desired"]
                    reconciled_pairs.add(
                        (str(desired["left"]), str(desired["right"]))
                    )
                write_slots -= selected_similarity_migrations

                update_candidates = live_updates[:write_slots]
                selected_similarity_updates = len(update_candidates)
                for item in update_candidates:
                    upsert_event_similarity(
                        conn,
                        str(item["left"]),
                        str(item["right"]),
                        similarity=float(item["score"]),
                        method=_SIMILARITY_METHOD,
                        model_version=_SIMILARITY_MODEL_VERSION,
                        rationale=str(item["rationale"]),
                        common_securities=item["common"],
                        shared_paths=item["paths"],
                        computed_at=graph_time,
                        now=graph_time,
                    )
                    reconciled_similarities += 1
                    reconciled_pairs.add((str(item["left"]), str(item["right"])))
                write_slots -= selected_similarity_updates

                # Similarity is reversible, but it is still public evidence.
                # Recompute every admitted candidate after merges and from the
                # latest in-transaction mappings before persisting it.
                all_remaining_similarity_candidates = [
                    item
                    for item in similarity_write_candidates
                    if (str(item["left"]), str(item["right"]))
                    not in reconciled_pairs
                ]
                live_pending_similarity_writes = len(
                    all_remaining_similarity_candidates
                )
                remaining_similarity_candidates = all_remaining_similarity_candidates[
                    :write_slots
                ]
                for item in remaining_similarity_candidates:
                    selected_similarity_writes += 1
                    try:
                        left = canonical_event_id(conn, item["left"])
                        right = canonical_event_id(conn, item["right"])
                        if left == right:
                            continue
                        if left not in fresh_by_id or right not in fresh_by_id:
                            skipped += 1
                            continue
                        live_item = _similarity_candidate(
                            fresh_by_id[left],
                            fresh_by_id[right],
                        )
                        if live_item is None:
                            skipped += 1
                            continue
                        upsert_event_similarity(
                            conn,
                            left,
                            right,
                            similarity=float(live_item["score"]),
                            method=_SIMILARITY_METHOD,
                            model_version=_SIMILARITY_MODEL_VERSION,
                            rationale=str(live_item["rationale"]),
                            common_securities=live_item["common"],
                            shared_paths=live_item["paths"],
                            computed_at=graph_time,
                            now=graph_time,
                        )
                        similarities += 1
                    except (EventCoreConflictError, KeyError, ValueError):
                        skipped += 1
                # A single root may own more incident edges than this run's
                # write budget. Re-plan from the post-write state and ack each
                # durable mapping->graph handoff only when that root has no
                # remaining delete/migrate/update/new-edge work.
                (
                    remaining_deletes,
                    remaining_migrations,
                    remaining_updates,
                ) = _similarity_reconciliation(
                    conn,
                    live_reconciliation_by_id,
                    _incident_similarity_rows(
                        conn,
                        _event_graph_member_ids(fresh_by_id),
                    ),
                    owned_root_ids=live_owned_root_ids,
                )
                # Recompute merge debt from the post-write canonical graph.
                # This includes disabled and over-budget candidates as well as
                # selected edges that failed live revalidation or persistence.
                # An edge already absorbed by another merge in this transaction
                # has one canonical root and is therefore no longer deferred.
                pending_merge_roots: set[str] = set()
                for raw_source, raw_target in merge_candidates:
                    source_root = canonical_event_id(conn, raw_source)
                    target_root = canonical_event_id(conn, raw_target)
                    if source_root == target_root:
                        continue
                    live_edge = tuple(sorted((source_root, target_root)))
                    live_deferred_merge_edges.add(live_edge)
                    pending_merge_roots.update(live_edge)
                pending_similarity_roots: set[str] = set()
                for raw_left, raw_right in remaining_deletes:
                    pending_similarity_roots.add(canonical_event_id(conn, raw_left))
                    pending_similarity_roots.add(canonical_event_id(conn, raw_right))
                for item in (*remaining_migrations, *remaining_updates):
                    desired = item["desired"] if "desired" in item else item
                    pending_similarity_roots.add(
                        canonical_event_id(conn, str(desired["left"]))
                    )
                    pending_similarity_roots.add(
                        canonical_event_id(conn, str(desired["right"]))
                    )
                deferred_new_edges = all_remaining_similarity_candidates[
                    len(remaining_similarity_candidates) :
                ]
                for item in deferred_new_edges:
                    pending_similarity_roots.add(
                        canonical_event_id(conn, str(item["left"]))
                    )
                    pending_similarity_roots.add(
                        canonical_event_id(conn, str(item["right"]))
                    )
                pending_graph_roots = pending_similarity_roots | pending_merge_roots
                acknowledged_markers = tuple(
                    (marker_id, generation)
                    for marker_id, generation in graph_dirty_markers
                    if canonical_event_id(conn, marker_id)
                    not in pending_graph_roots
                )
                if acknowledged_markers:
                    for marker_id, generation in acknowledged_markers:
                        cursor = conn.execute(
                            "DELETE FROM event_similarity_replay_dirty_roots "
                            "WHERE root_event_id = ? AND generation = ?",
                            (marker_id, generation),
                        )
                        graph_dirty_groups_acked += max(0, cursor.rowcount)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    return {
        "merges_allowed": bool(allow_merges),
        "events": len(events),
        "examined_pairs": len(exact_pairs | security_pairs),
        "qualified_merges": len(merge_candidates),
        "deferred_merges": len(live_deferred_merge_edges),
        "suppressed_merges": (
            len(live_deferred_merge_edges) if not allow_merges else 0
        ),
        "qualified_similarities": len(qualified_similarities),
        "pending_similarity_writes": live_pending_similarity_writes,
        "pending_similarity_deletes": live_pending_similarity_deletes,
        "pending_similarity_migrations": live_pending_similarity_migrations,
        "pending_similarity_updates": live_pending_similarity_updates,
        "deferred_similarities": max(
            0,
            live_pending_similarity_writes - selected_similarity_writes,
        ),
        "deferred_similarity_deletes": max(
            0,
            live_pending_similarity_deletes - selected_similarity_deletes,
        ),
        "deferred_similarity_migrations": max(
            0,
            live_pending_similarity_migrations - selected_similarity_migrations,
        ),
        "deferred_similarity_updates": max(
            0,
            live_pending_similarity_updates - selected_similarity_updates,
        ),
        "candidate_pairs": (
            len(selected_merges)
            + selected_similarity_deletes
            + selected_similarity_migrations
            + selected_similarity_updates
            + selected_similarity_writes
        ),
        "merged": merged,
        "similarities": similarities,
        "deleted_similarities": deleted_similarities,
        "migrated_similarities": migrated_similarities,
        "reconciled_similarities": reconciled_similarities,
        "graph_dirty_groups": len(graph_dirty_marker_ids),
        "graph_dirty_groups_acked": graph_dirty_groups_acked,
        "skipped": skipped,
    }


def run_event_enrichment_lanes(
    *,
    since: int,
    mapping_since: int | None = None,
    mapping_limit: int = 5_000,
    max_pairs: int = 10_000,
    rebuild_graph_after_mapping: bool = False,
    mapping_include_legacy: bool = False,
) -> dict[str, Any]:
    """Run both durable lanes without allowing either one to suppress the other.

    Graph resolution is deliberately first: security mapping is useful context
    for similarity edges, but an unrelated mapping failure must not leave
    evidence-qualified exact wire duplicates active.  Each lane has its own
    failure boundary and reports only a stable code plus the exception type;
    exception messages may contain source text or deployment paths and must not
    enter refresh logs or the cron state payload.
    """
    result: dict[str, Any] = {}
    failed_lanes = 0
    degraded_lanes = 0
    lane_count = 2
    try:
        result["graph"] = resolve_event_graph(
            since=int(since),
            max_pairs=max_pairs,
        )
    except Exception as exc:
        failed_lanes += 1
        result["graph"] = {
            "status": "failed",
            "failure_code": "event_graph_failed",
            "error_type": type(exc).__name__,
        }
        logger.error(
            "[EventCore] event graph lane failed (%s)",
            type(exc).__name__,
        )

    try:
        mapping_options = {"include_legacy": True} if mapping_include_legacy else {}
        mapping_result = enrich_event_security_mappings(
            since=int(since if mapping_since is None else mapping_since),
            limit=mapping_limit,
            **mapping_options,
        )
        result["mappings"] = mapping_result
        if int(mapping_result.get("failed_evidence") or 0) > 0 or int(
            mapping_result.get("exhausted_groups") or 0
        ) > 0 or int(mapping_result.get("unreplayable_groups") or 0) > 0:
            degraded_lanes += 1
    except Exception as exc:
        failed_lanes += 1
        result["mappings"] = {
            "status": "failed",
            "failure_code": "security_mapping_failed",
            "error_type": type(exc).__name__,
        }
        logger.error(
            "[EventCore] security mapping lane failed (%s)",
            type(exc).__name__,
        )

    mapping_graph_dirty = int(
        result.get("mappings", {}).get("graph_dirty_groups") or 0
    )
    run_graph_after_mapping = bool(
        rebuild_graph_after_mapping or mapping_graph_dirty > 0
    )
    if run_graph_after_mapping:
        lane_count += 1
        try:
            result["graph_after_mapping"] = resolve_event_graph(
                since=int(since),
                max_pairs=max_pairs,
            )
        except Exception as exc:
            failed_lanes += 1
            result["graph_after_mapping"] = {
                "status": "failed",
                "failure_code": "event_graph_rebuild_failed",
                "error_type": type(exc).__name__,
            }
            logger.error(
                "[EventCore] post-mapping event graph lane failed (%s)",
                type(exc).__name__,
            )

    result["status"] = (
        "failed"
        if failed_lanes == lane_count
        else "degraded"
        if failed_lanes or degraded_lanes
        else "ok"
    )
    return result


__all__ = [
    "enrich_event_security_mappings",
    "event_mapping_migration_audit_status",
    "event_mapping_migration_status",
    "resolve_event_graph",
    "run_event_enrichment_lanes",
]
