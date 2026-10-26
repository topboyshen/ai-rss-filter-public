"""Bounded read model for the authenticated Event Radar Dashboard.

This module deliberately performs no clustering, enrichment, or persistence.
Every relationship comes from the stable event core.  The detail loader uses
one read-only SQLite transaction so the list and detail share a snapshot
without waiting for unrelated work holding the process-wide write lock.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import sqlite3
import time
from time import perf_counter
import uuid
from datetime import datetime, time as datetime_time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from core.config import config as runtime_config
from core.asset_mapping import get_mapping_version
from core.source_contract import (
    ARTICLE_CLAIM_PREFIX,
    MAPPING_CLAIM_PREFIX,
    is_current_mapping_writer,
    is_current_source_claim,
    mapping_contract_status,
)
from db.database import db_lock, get_connection, get_read_only_connection
from db.event_core import (
    EventCoreConflictError,
    canonical_event_group,
    canonical_event_id,
)
from scoring_provenance import (
    FORMAL_SIGNAL_SCORE_PATHS,
    PUBLIC_SCORE_PATHS,
    normalize_score_path,
)
from utils.helpers import plain_text_from_markup
from services.event_outcomes import (
    LEGACY_OUTCOME_DATA_VERSION_PREFIX,
    MARKET_BENCHMARK_SYMBOL,
    OUTCOME_DATA_VERSION_PREFIX,
    OUTCOME_HORIZON_DAYS,
    OUTCOME_METHOD_VERSION,
    baseline_trade_date,
    direction_hit_for_return,
    horizon_trade_date,
    is_completed_close_price,
    is_current_outcome_contract,
    normalize_mapping_relation,
    tradable_baseline_trade_date,
)

try:
    from services.provenance_shadow import (
        load_event_provenance_shadow as _load_event_provenance_shadow,
    )
except Exception:  # Optional sidecar failures must not prevent the core Radar API.
    _load_event_provenance_shadow = None

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 50
MAX_SOURCES = 30
MAX_SOURCE_OBSERVATIONS = 5
MAX_OUTCOMES = 50
MAX_SIMILAR_EVENTS = 20
MAX_SIMILARITY_EDGES_PER_SIDE = 100
MAX_MAPPING_EVIDENCE = 5
MAX_HISTORICAL_COHORT = 5_000
MAX_JSON_ITEMS = 20
MAX_CURSOR_BYTES = 384
SLOW_EVENT_RADAR_OPERATION_MS = 500.0
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = datetime_time(15, 0)
_PUBLIC_SCORE_PATH_VALUES = tuple(sorted(PUBLIC_SCORE_PATHS))

_CURSOR_VERSION = 1
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_UNSIGNED_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_TAB_ALIASES = {
    "overview": "overview",
    "detail": "overview",
    "market": "market",
    "outcomes": "market",
    "reaction": "market",
    "similar": "similar",
    "similarities": "similar",
    "history": "similar",
}


def _public_cache_evidence_predicate(evidence_alias: str) -> str:
    placeholders = ",".join("?" for _ in _PUBLIC_SCORE_PATH_VALUES)
    return (
        "NOT EXISTS (SELECT 1 FROM news_cache AS public_cache "
        f"WHERE public_cache.link = {evidence_alias}.source_uri "
        "AND LOWER(TRIM(COALESCE(public_cache.score_path, ''))) "
        f"NOT IN ({placeholders}))"
    )

# Stable write-side values accepted for the first Dashboard release.  The
# legacy/default ``affected`` value is presented as a direct impact.  A market
# ``benchmark_proxy`` is intentionally absent: it belongs in the outcomes tab,
# never in the top-three-equities panel.
_RELATION_LABELS = {
    "direct": "直接影响",
    "direct_impact": "直接影响",
    "affected": "直接影响",
    "upstream": "上游",
    "downstream": "下游",
    "ecosystem": "生态",
    "concept": "概念",
}
_RELATION_SQL_VALUES = tuple(_RELATION_LABELS)
# Keep representative mappings and the displayed top three consistent across
# list/detail queries without changing their distinct evidence/time predicates.
_STOCK_RELATION_WEAKNESS_SQL = (
    "(CASE WHEN lower(mapping.relation_type) = 'concept' THEN 1 ELSE 0 END)"
)
_STOCK_MAPPING_ORDER_SQL = (
    f"{_STOCK_RELATION_WEAKNESS_SQL}, mapping.relevance DESC, "
    "mapping.confidence DESC, mapping.created_at DESC, mapping.mapping_id DESC"
)
_STOCK_DISPLAY_ORDER_SQL = (
    "relation_weakness, relevance DESC, confidence DESC, security_id ASC"
)
_NON_CONCEPT_RELATION_SQL_VALUES = tuple(
    value for value in _RELATION_SQL_VALUES if value != "concept"
)
_PROVENANCE_SHADOW_STATUSES = frozenset({"ready", "pending", "degraded"})
_PROVENANCE_SHADOW_CLAIM_STATES = (
    "single_support",
    "multi_independent_support",
    "contested",
    "contradicted_only",
    "context_only",
)
_PROVENANCE_SHADOW_COUNT_FIELDS = (
    "raw_evidence_count",
    "projected_evidence_count",
    "publisher_group_count",
    "lineage_unit_count",
    "first_party_evidence_count",
    "official_record_evidence_count",
    "claim_count",
)


class EventRadarQueryError(ValueError):
    """A caller supplied a malformed or out-of-range query value."""


class EventRadarNotFoundError(LookupError):
    """The requested stable event UUID does not exist."""


class EventRadarUnavailableError(RuntimeError):
    """The event-core read model is unavailable or internally inconsistent."""


def _bounded_text(value: object, maximum: int) -> str:
    text = "" if value is None else str(value)
    return text[:maximum]


def _finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _pending_provenance_shadow(status: object = "pending") -> dict[str, Any]:
    normalized = str(status or "").strip()
    if normalized not in {"pending", "degraded"}:
        normalized = "pending"
    return {"status": normalized}


def _normalize_provenance_shadow(value: object) -> dict[str, Any]:
    """Allow only complete aggregate sidecar output onto the Radar read model."""
    if not isinstance(value, dict):
        return _pending_provenance_shadow()
    status = str(value.get("status") or "").strip()
    if status not in _PROVENANCE_SHADOW_STATUSES:
        return _pending_provenance_shadow()
    if status != "ready":
        return _pending_provenance_shadow(status)

    root_event_version = _nonnegative_integer(value.get("root_event_version"))
    if root_event_version is None or root_event_version < 1:
        return _pending_provenance_shadow()
    counts: dict[str, int] = {}
    for field in _PROVENANCE_SHADOW_COUNT_FIELDS:
        parsed = _nonnegative_integer(value.get(field))
        if parsed is None:
            return _pending_provenance_shadow()
        counts[field] = parsed

    raw_count = counts["raw_evidence_count"]
    projected_count = counts["projected_evidence_count"]
    if projected_count != raw_count:
        return _pending_provenance_shadow()
    # V1 freezes publisher metadata but has no audited event-subject binding.
    # Any non-zero value therefore belongs to another/invalid contract.
    if counts["first_party_evidence_count"] != 0:
        return _pending_provenance_shadow()
    if any(
        counts[field] > projected_count
        for field in (
            "publisher_group_count",
            "lineage_unit_count",
            "first_party_evidence_count",
            "official_record_evidence_count",
        )
    ):
        return _pending_provenance_shadow()
    raw_state_counts = value.get("claim_state_counts")
    if not isinstance(raw_state_counts, dict) or set(raw_state_counts) != set(
        _PROVENANCE_SHADOW_CLAIM_STATES
    ):
        return _pending_provenance_shadow()
    claim_state_counts: dict[str, int] = {}
    for state in _PROVENANCE_SHADOW_CLAIM_STATES:
        parsed = _nonnegative_integer(raw_state_counts.get(state))
        if parsed is None:
            return _pending_provenance_shadow()
        claim_state_counts[state] = parsed
    if sum(claim_state_counts.values()) != counts["claim_count"]:
        return _pending_provenance_shadow()

    latest_evidence_at = _nonnegative_integer(value.get("latest_evidence_at"))
    computed_at = _nonnegative_integer(value.get("computed_at"))
    if latest_evidence_at is None or computed_at is None or computed_at < 1:
        return _pending_provenance_shadow()
    return {
        "status": "ready",
        **counts,
        "claim_state_counts": claim_state_counts,
        "latest_evidence_at": latest_evidence_at,
        "computed_at": computed_at,
    }


def _read_provenance_shadow(
    conn: sqlite3.Connection, root_event_id: str
) -> dict[str, Any]:
    """Read the optional projection without making it a core-lane dependency."""
    if _load_event_provenance_shadow is None:
        return _pending_provenance_shadow()
    try:
        raw = _load_event_provenance_shadow(
            conn,
            root_event_id,
            # Pass the complete runtime object.  The sidecar owns validation of
            # an explicitly malformed source_provenance section; replacing it
            # with None could accidentally select an older empty-rules image.
            config=runtime_config.cfg,
        )
    except Exception as exc:
        logger.warning(
            "Event Radar provenance shadow unavailable: %s", type(exc).__name__
        )
        return _pending_provenance_shadow()
    return _normalize_provenance_shadow(raw)


def _freeze_read_at() -> int:
    """Return the single wall-clock boundary for one read transaction."""
    return int(time.time())


def _log_operation_duration(
    operation: str, started_at: float, *, item_count: int | None = None
) -> float:
    """Emit bounded structured latency telemetry and return elapsed milliseconds."""
    elapsed_ms = max(0.0, (perf_counter() - started_at) * 1000.0)
    level = (
        logging.WARNING
        if elapsed_ms >= SLOW_EVENT_RADAR_OPERATION_MS
        else logging.DEBUG
    )
    logger.log(
        level,
        "event_radar operation=%s latency_ms=%.2f item_count=%s slow=%s",
        operation,
        elapsed_ms,
        "unknown" if item_count is None else item_count,
        elapsed_ms >= SLOW_EVENT_RADAR_OPERATION_MS,
    )
    return elapsed_ms


def _parse_limit(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool):
        raise EventRadarQueryError("limit must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if len(raw) > len(str(MAX_PAGE_SIZE)) or not _UNSIGNED_RE.fullmatch(raw):
            raise EventRadarQueryError("limit must be an integer")
        parsed = int(raw)
    else:
        raise EventRadarQueryError("limit must be an integer")
    if not 1 <= parsed <= MAX_PAGE_SIZE:
        raise EventRadarQueryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return parsed


def _parse_tab(value: object) -> str:
    raw = "overview" if value is None else str(value).strip().lower()
    raw = raw or "overview"
    if len(raw) > 32:
        logger.debug("_parse_tab: invalid tab=%s", raw)
        raise EventRadarQueryError("tab must be overview, market, or similar")
    tab = _TAB_ALIASES.get(raw)
    if tab is None:
        raise EventRadarQueryError("tab must be overview, market, or similar")
    return tab


def _parse_has_stocks(value: object) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw in {"0", "1"}:
            return raw == "1"
    raise EventRadarQueryError("has_stocks must be 0 or 1")


def _parse_uuid(value: object, *, optional: bool = False) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw and optional:
        return ""
    if len(raw) != 36 or not _UUID_RE.fullmatch(raw):
        raise EventRadarQueryError("event_id must be a canonical UUID")
    try:
        normalized = str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise EventRadarQueryError("event_id must be a canonical UUID") from exc
    return normalized


def _encode_cursor(occurred_at: int, priority: float, event_id: str) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "occurred_at": int(occurred_at),
            "priority": float(priority),
            "event_id": event_id,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: object) -> tuple[int, float, str] | None:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None
    if len(raw.encode("utf-8")) > MAX_CURSOR_BYTES or not re.fullmatch(
        r"[A-Za-z0-9_-]+", raw
    ):
        raise EventRadarQueryError("cursor is malformed")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise EventRadarQueryError("cursor is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "occurred_at",
        "priority",
        "event_id",
    }:
        raise EventRadarQueryError("cursor is malformed")
    if payload.get("v") != _CURSOR_VERSION:
        raise EventRadarQueryError("cursor version is unsupported")
    occurred_at = payload.get("occurred_at")
    if isinstance(occurred_at, bool) or not isinstance(occurred_at, int):
        raise EventRadarQueryError("cursor is malformed")
    priority = _finite_float(payload.get("priority"))
    if (
        occurred_at < 0
        or occurred_at > _MAX_SQLITE_INTEGER
        or priority is None
        or not 0 <= priority <= 10
    ):
        raise EventRadarQueryError("cursor is malformed")
    event_id = _parse_uuid(payload.get("event_id"))
    return occurred_at, priority, event_id


def _safe_external_url(value: object) -> str:
    """Return a display-only credential-free HTTP(S) URL.

    Evidence URLs are already validated on new writes.  This second boundary
    protects the Dashboard from malformed legacy rows and prevents embedded
    URL credentials from being reflected to the browser.
    """
    raw = "" if value is None else str(value).strip()
    if (
        not raw
        or len(raw) > 2048
        or "\\" in raw
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
    ):
        return ""
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""
        _ = parsed.port
    except (TypeError, ValueError):
        return ""
    return raw


def _bounded_json_strings(value: object, *, maximum: int = MAX_JSON_ITEMS) -> list[str]:
    if value in (None, ""):
        return []
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    result: list[str] = []
    for item in decoded[:maximum]:
        if isinstance(item, str):
            text = item
        elif item is None or isinstance(item, (bool, int, float)):
            text = str(item)
        else:
            try:
                text = json.dumps(
                    item,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                continue
        text = text.strip()[:500]
        if text and text not in result:
            result.append(text)
    return result


def _event_group_cte() -> str:
    return (
        "WITH RECURSIVE event_group(event_id) AS ("
        " SELECT ? UNION "
        " SELECT child.event_id FROM events AS child "
        " JOIN event_group AS parent ON child.merged_into = parent.event_id"
        ") "
    )


def _mapping_source_contract_sql() -> str:
    """Require every visible, exactly bound source to carry its write contract.

    A current writer cannot promote attached historical evidence by proximity.
    Keep this bounded to the selected mapping and the frozen read boundary.
    """
    identity = (
        "evidence.mapping_id = mapping.mapping_id "
        "AND evidence.event_id = mapping.event_id "
        "AND evidence.security_id = mapping.security_id "
        "AND evidence.mapping_revision = mapping.revision "
        "AND evidence.observed_at <= (SELECT read_at FROM read_boundary)"
    )
    article_suffix = f"substr(evidence.claim_key, {len(ARTICLE_CLAIM_PREFIX) + 1})"
    mapping_suffix = f"substr(evidence.claim_key, {len(MAPPING_CLAIM_PREFIX) + 1})"
    uuid_pattern = "-".join("[0-9a-f]" * size for size in (8, 4, 4, 4, 12))
    marked = (
        f"(substr(evidence.claim_key, 1, {len(ARTICLE_CLAIM_PREFIX)}) = '{ARTICLE_CLAIM_PREFIX}' "
        f"AND length({article_suffix}) = 64 AND {article_suffix} NOT GLOB '*[^0-9a-f]*') OR "
        f"(substr(evidence.claim_key, 1, {len(MAPPING_CLAIM_PREFIX)}) = '{MAPPING_CLAIM_PREFIX}' "
        f"AND {mapping_suffix} GLOB '{uuid_pattern}:{uuid_pattern}')"
    )
    return (
        f"EXISTS (SELECT 1 FROM event_evidence AS evidence WHERE {identity}) "
        f"AND NOT EXISTS (SELECT 1 FROM event_evidence AS evidence WHERE {identity} "
        f"AND NOT ({marked}))"
    )


def _current_mapping_contracts(
    conn: sqlite3.Connection, versions: list[str],
) -> tuple[set[str], bool]:
    """Compute a long writer identity at most once per bounded loader, if needed."""
    current = {get_mapping_version()}
    if any(":writer-" in version and is_current_mapping_writer(version) for version in versions):
        try:
            # Lazy import avoids a module-import dependency cycle. This helper
            # fingerprints the source/master in the same read transaction only.
            from services.event_enrichment import _mapping_contract_version

            current.add(_mapping_contract_version(conn)[0])
        except Exception as exc:
            logger.warning("Event Radar mapping identity unavailable: %s", type(exc).__name__)
            return current, False
    return current, True


def _mapping_contract_view(
    version: object, source_current: bool, current_versions: set[str] | None = None,
    current_contract_available: bool = True,
) -> dict[str, Any]:
    """Label stored associations without replaying or upgrading their identity."""
    version = str(version or "").strip()
    state = mapping_contract_status(
        version, current_versions=current_versions if current_versions is not None else {get_mapping_version()},
    )
    if state == "historical" and ":writer-" in version and not current_contract_available:
        state = "unavailable"
    if state == "current" and not source_current:
        state, label = "source_unknown", "映射来源契约未知 · 未作为当前原文证据"
    else:
        label = {
            "legacy_unknown": "历史映射 · 写入契约未知",
            "historical": "历史映射 · 未按当前规则重算",
            "unavailable": "映射契约核验暂不可用 · 保留原记录",
            "current": "当前标题映射 · 不代表受益",
        }[state]
    return {
        "mapping_contract_state": state,
        "mapping_contract_label": label,
        "mapping_contract_current": state == "current",
    }


def _load_page_stock_chips(
    conn: sqlite3.Connection,
    event_ids: list[str],
    *,
    read_at: int,
    evidence_backed_only: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Load at most three current equities for every visible root event."""
    roots = [str(event_id) for event_id in event_ids if str(event_id)]
    if not roots:
        return {}
    root_placeholders = ",".join("?" for _ in roots)
    relation_placeholders = ",".join("?" for _ in _RELATION_SQL_VALUES)
    evidence_clause = (
        " AND EXISTS (SELECT 1 FROM event_evidence AS mapping_evidence "
        "WHERE mapping_evidence.mapping_id = mapping.mapping_id "
        "AND mapping_evidence.event_id = mapping.event_id "
        "AND mapping_evidence.security_id = mapping.security_id "
        "AND mapping_evidence.mapping_revision = mapping.revision "
        "AND mapping_evidence.observed_at <= "
        "(SELECT read_at FROM read_boundary))"
        if evidence_backed_only
        else ""
    )
    rows = conn.execute(
        "WITH RECURSIVE read_boundary(read_at) AS (VALUES (?)), "
        "event_group(root_id, member_id) AS ("
        " SELECT event_id, event_id FROM events "
        f" WHERE event_id IN ({root_placeholders}) UNION "
        " SELECT parent.root_id, child.event_id FROM events AS child "
        " JOIN event_group AS parent ON child.merged_into = parent.member_id "
        " WHERE child.available_at <= (SELECT read_at FROM read_boundary)"
        "), candidates AS ("
        # 概念池（concept）来自宽泛主题规则股票池，证据强度弱于 direct/upstream/
        # downstream/ecosystem；同一证券多条映射并存时先取非概念关系，展示排序
        # 同理，避免规则池股票在摘要卡片上盖过真实个股关系。
        " SELECT grouped.root_id, security.security_id, security.name, "
        " security.exchange, security.ticker, lower(mapping.relation_type) relation_type, "
        " mapping.direction, mapping.confidence, mapping.relevance, "
        " mapping.mapping_version, "
        f" ({_mapping_source_contract_sql()}) AS mapping_source_current, "
        f" {_STOCK_RELATION_WEAKNESS_SQL} AS relation_weakness, "
        " ROW_NUMBER() OVER (PARTITION BY grouped.root_id, security.security_id "
        f" ORDER BY {_STOCK_MAPPING_ORDER_SQL}) AS security_rank "
        # Keep the bounded root set on the outer side. Without sqlite_stat1,
        # SQLite otherwise scans every current mapping before matching roots.
        " FROM event_group AS grouped CROSS JOIN event_securities AS mapping "
        " INDEXED BY uq_event_securities_current "
        " ON mapping.event_id = grouped.member_id "
        " JOIN securities AS security ON security.security_id = mapping.security_id "
        " WHERE mapping.is_current = 1 AND security.is_active = 1 "
        " AND mapping.created_at <= (SELECT read_at FROM read_boundary) "
        " AND security.created_at <= (SELECT read_at FROM read_boundary) "
        " AND lower(security.asset_type) = 'equity' "
        f" AND lower(mapping.relation_type) IN ({relation_placeholders})"
        + evidence_clause
        + "), best_security AS ("
        " SELECT * FROM candidates WHERE security_rank = 1"
        "), display_ranked AS ("
        " SELECT best_security.*, ROW_NUMBER() OVER (PARTITION BY root_id "
        f" ORDER BY {_STOCK_DISPLAY_ORDER_SQL})"
        " AS display_rank "
        " FROM best_security"
        ") SELECT * FROM display_ranked WHERE display_rank <= 3 "
        " ORDER BY root_id, display_rank",
        (read_at, *roots, *_RELATION_SQL_VALUES),
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {root: [] for root in roots}
    current_versions, contract_available = _current_mapping_contracts(
        conn, [str(row["mapping_version"]) for row in rows],
    )
    for row in rows:
        result.setdefault(str(row["root_id"]), []).append(
            {
                "security_id": str(row["security_id"]),
                "name": _bounded_text(row["name"], 255),
                "exchange": _bounded_text(row["exchange"], 32),
                "ticker": _bounded_text(row["ticker"], 32),
                "relation_type": _bounded_text(row["relation_type"], 64),
                "direction": _bounded_text(row["direction"], 64),
                "mapping_confidence": float(row["confidence"]),
                "relevance": float(row["relevance"]),
                "mapping_version": _bounded_text(row["mapping_version"], 255),
                **_mapping_contract_view(
                    row["mapping_version"], bool(row["mapping_source_current"]), current_versions,
                    contract_available,
                ),
            }
        )
    return result


def _load_event_page(
    conn: sqlite3.Connection,
    cursor: tuple[int, float, str] | None,
    limit: int,
    has_stocks: bool,
    read_at: int,
    min_priority: float | None = None,
    *,
    require_non_concept_stock: bool = False,
    excluded_event_types: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Load one stable keyset-paginated page from a frozen database snapshot.

    Args:
        conn: Active SQLite connection with ``db_lock`` held.
        cursor: Decoded ordering tuple from the preceding page.
        limit: Maximum number of visible events.
        has_stocks: Restrict results to events with eligible equity mappings.
        read_at: Snapshot boundary used by every query in the request.
        min_priority: Optional lower priority bound for compact public cards.
        require_non_concept_stock: Require evidence-backed non-concept mappings.
        excluded_event_types: Bounded event types omitted from public surfaces.

    Returns:
        ``(items, next_cursor, has_more)`` with a stable opaque cursor.
    """
    started_at = perf_counter()
    params: list[object] = [read_at]
    equity_filter_ctes = ""
    equity_filter_clause = ""
    if has_stocks:
        relation_values = (
            _NON_CONCEPT_RELATION_SQL_VALUES
            if require_non_concept_stock
            else _RELATION_SQL_VALUES
        )
        relation_placeholders = ",".join("?" for _ in relation_values)
        evidence_clause = (
            " AND EXISTS (SELECT 1 FROM event_evidence AS mapping_evidence "
            "WHERE mapping_evidence.mapping_id = mapping.mapping_id "
            "AND mapping_evidence.event_id = mapping.event_id "
            "AND mapping_evidence.security_id = mapping.security_id "
            "AND mapping_evidence.mapping_revision = mapping.revision "
            "AND mapping_evidence.observed_at <= "
            "(SELECT read_at FROM read_boundary))"
            if require_non_concept_stock
            else ""
        )
        # Correlate the recursive group walk to each index-ordered candidate.
        # The former global CTE expanded every active event before LIMIT, which
        # cost hundreds of milliseconds on production-sized ledgers.
        # Keep that bounded group on the outer side too: fresh read-only SQLite
        # connections may lack planner statistics and otherwise scan mappings.
        equity_filter_clause = (
            " AND EXISTS (WITH RECURSIVE candidate_group(member_id) AS ("
            " SELECT e.event_id UNION "
            " SELECT child.event_id FROM events AS child "
            " JOIN candidate_group AS parent ON child.merged_into = parent.member_id "
            " WHERE child.available_at <= (SELECT read_at FROM read_boundary)"
            ") SELECT 1 FROM candidate_group AS grouped "
            " CROSS JOIN event_securities AS mapping "
            " INDEXED BY uq_event_securities_current "
            " ON mapping.event_id = grouped.member_id "
            " JOIN securities AS security ON security.security_id = mapping.security_id "
            " WHERE mapping.is_current = 1 AND security.is_active = 1 "
            " AND mapping.created_at <= (SELECT read_at FROM read_boundary) "
            " AND security.created_at <= (SELECT read_at FROM read_boundary) "
            " AND lower(security.asset_type) = 'equity' "
            f" AND lower(mapping.relation_type) IN ({relation_placeholders})"
            + evidence_clause
            + " LIMIT 1)"
        )
        params.extend(relation_values)
    public_evidence_predicate = _public_cache_evidence_predicate(
        "public_evidence"
    )
    # ``mapping_id IS NULL`` can tempt an unanalyzed production database into
    # scanning the mapping-first index for every candidate.  These aliases are
    # correlated by event_id, whose existing index keeps each probe bounded.
    public_event_clause = (
        " AND EXISTS (WITH RECURSIVE public_group(member_id) AS ("
        " SELECT e.event_id UNION "
        " SELECT child.event_id FROM events AS child "
        " JOIN public_group AS parent ON child.merged_into = parent.member_id "
        " WHERE child.available_at <= (SELECT read_at FROM read_boundary)"
        ") SELECT 1 WHERE NOT EXISTS ("
        " SELECT 1 FROM public_group AS grouped "
        " JOIN event_evidence AS any_evidence "
        " INDEXED BY idx_event_evidence_event_time "
        " ON any_evidence.event_id = grouped.member_id "
        " AND any_evidence.mapping_id IS NULL "
        " AND any_evidence.observed_at <= "
        " (SELECT read_at FROM read_boundary) LIMIT 1"
        ") OR EXISTS ("
        " SELECT 1 FROM public_group AS grouped "
        " JOIN event_evidence AS public_evidence "
        " INDEXED BY idx_event_evidence_event_time "
        " ON public_evidence.event_id = grouped.member_id "
        " AND public_evidence.mapping_id IS NULL "
        " AND public_evidence.observed_at <= "
        " (SELECT read_at FROM read_boundary) WHERE "
        + public_evidence_predicate
        + " LIMIT 1))"
    )
    params.extend(_PUBLIC_SCORE_PATH_VALUES)
    priority_clause = ""
    if min_priority is not None:
        priority_clause = " AND e.priority >= ?"
        params.append(float(min_priority))
    event_type_clause = ""
    if excluded_event_types:
        event_type_placeholders = ",".join("?" for _ in excluded_event_types)
        event_type_clause = (
            f" AND COALESCE(e.event_type, '') NOT IN ({event_type_placeholders})"
        )
        params.extend(excluded_event_types)
    cursor_clause = ""
    if cursor is not None:
        occurred_at, priority, event_id = cursor
        # A row-value comparison is semantically identical to the expanded OR
        # predicate, but lets SQLite seek into ``idx_events_radar_order`` instead
        # of scanning the partial index from its newest entry on every page.
        cursor_clause = " AND (e.occurred_at, e.priority, e.event_id) < (?, ?, ?)"
        params.extend([occurred_at, priority, event_id])
    params.append(limit + 1)
    source_count_predicate = _public_cache_evidence_predicate("evidence")
    params.extend(_PUBLIC_SCORE_PATH_VALUES)
    rows = conn.execute(
        "WITH RECURSIVE read_boundary(read_at) AS (VALUES (?)), "
        + equity_filter_ctes
        + "page_events AS ("
        " SELECT e.event_id, e.title, e.event_type, e.direction, e.priority, "
        " e.occurred_at, e.version, e.first_seen_at, e.last_seen_at, e.available_at "
        " FROM events AS e WHERE e.merge_state = 'active'"
        " AND e.available_at <= (SELECT read_at FROM read_boundary)"
        + equity_filter_clause
        + public_event_clause
        + priority_clause
        + event_type_clause
        + cursor_clause
        + " ORDER BY e.occurred_at DESC, e.priority DESC, e.event_id DESC LIMIT ?"
        "), event_group(root_id, member_id) AS ("
        " SELECT event_id, event_id FROM page_events UNION "
        " SELECT parent.root_id, child.event_id FROM events AS child "
        " JOIN event_group AS parent ON child.merged_into = parent.member_id "
        " WHERE child.available_at <= (SELECT read_at FROM read_boundary)"
        "), source_counts AS ("
        " SELECT grouped.root_id, COUNT(DISTINCT evidence.source_uri) AS source_count "
        " FROM event_group AS grouped "
        " LEFT JOIN event_evidence AS evidence ON evidence.event_id = grouped.member_id "
        " AND evidence.mapping_id IS NULL "
        " AND evidence.observed_at <= (SELECT read_at FROM read_boundary) "
        " AND "
        + source_count_predicate
        + " "
        " GROUP BY grouped.root_id"
        ") SELECT page_events.*, COALESCE(source_counts.source_count, 0) AS source_count "
        " FROM page_events LEFT JOIN source_counts "
        " ON source_counts.root_id = page_events.event_id "
        " ORDER BY page_events.occurred_at DESC, page_events.priority DESC, "
        " page_events.event_id DESC",
        tuple(params),
    ).fetchall()

    logger.debug(
        "event_radar page query rows=%d limit=%d has_stocks=%s",
        len(rows),
        limit,
        has_stocks,
    )
    has_more = len(rows) > limit
    visible = rows[:limit]
    stock_chips = _load_page_stock_chips(
        conn,
        [str(row["event_id"]) for row in visible],
        read_at=read_at,
        evidence_backed_only=require_non_concept_stock,
    )
    items = [
        {
            "event_id": str(row["event_id"]),
            "title": _bounded_text(plain_text_from_markup(row["title"]), 1000),
            "event_type": _bounded_text(row["event_type"], 64),
            "direction": _bounded_text(row["direction"], 64),
            "priority": float(row["priority"]),
            "importance_score": float(row["priority"]),
            "occurred_at": int(row["occurred_at"]),
            "version": int(row["version"]),
            "first_seen_at": int(row["first_seen_at"]),
            "last_seen_at": int(row["last_seen_at"]),
            "available_at": int(row["available_at"]),
            "source_count": int(row["source_count"]),
            "stocks": stock_chips.get(str(row["event_id"]), []),
            "has_stocks": bool(stock_chips.get(str(row["event_id"]))),
        }
        for row in visible
    ]
    next_cursor = None
    if has_more and items:
        final = items[-1]
        next_cursor = _encode_cursor(
            final["occurred_at"], final["priority"], final["event_id"]
        )
    _log_operation_duration("load_event_page", started_at, item_count=len(items))
    return items, next_cursor, has_more


def _source_dict(row: sqlite3.Row) -> dict[str, Any]:
    source_confidence = _finite_float(row["reliability"])
    source_current = is_current_source_claim(row["claim_key"] if "claim_key" in row.keys() else "")
    return {
        "evidence_id": str(row["evidence_id"]),
        "source_name": _bounded_text(row["source_name"], 255),
        "url": _safe_external_url(row["source_uri"]),
        "claim_text": _bounded_text(plain_text_from_markup(row["claim_text"]), 4000),
        "excerpt": _bounded_text(plain_text_from_markup(row["excerpt"]), 8000),
        "original_claim": _bounded_text(plain_text_from_markup(row["claim_text"]), 4000),
        "source_excerpt": _bounded_text(plain_text_from_markup(row["excerpt"]), 8000),
        "source_contract_state": "current" if source_current else "legacy_unknown",
        "claim_label": "发布方标题" if source_current else "存档标题（来源契约未核验）",
        "source_contract_label": (
            "当前标题留存契约 · 不等于事实核验"
            if source_current else "历史来源记录 · 来源契约未知"
        ),
        "stance_record_label": (
            ("记录立场：" if source_current else "历史立场字段：")
            + {"supports": "支持", "contradicts": "否认", "context": "背景"}.get(row["stance"], "未知")
            + "（不等于事实核验）"
        ),
        "stance": _bounded_text(row["stance"], 16),
        "reliability": source_confidence,
        "source_confidence": source_confidence,
        # The current schema stores a bounded reliability prior, not a
        # calibrated empirical probability.  Expose that provenance so the UI
        # cannot present a default 0.5 as if it were model-measured certainty.
        "source_confidence_basis": "rule_prior",
        "announcement_at": (
            None if row["announcement_at"] is None else int(row["announcement_at"])
        ),
        "observed_at": int(row["observed_at"]),
        "version": int(row["version"]),
        "observation_count": int(row["observation_count"]),
    }


def _load_sources(
    conn: sqlite3.Connection, root_event_id: str, read_at: int
) -> tuple[list[dict[str, Any]], int, bool, str]:
    logger.debug("event_radar sources root_event_id=%s", root_event_id)
    public_evidence_predicate = _public_cache_evidence_predicate("evidence")
    # Probe the bounded event group, not all historical mapping_id=NULL rows.
    rows = conn.execute(
        _event_group_cte() + ", ranked AS ("
        " SELECT evidence.*, "
        " ROW_NUMBER() OVER (PARTITION BY evidence.source_uri ORDER BY "
        " evidence.observed_at DESC, evidence.version DESC, evidence.evidence_id DESC"
        " ) AS source_rank, "
        " COUNT(*) OVER (PARTITION BY evidence.source_uri) AS observation_count, "
        " MAX(evidence.stance = 'supports') OVER (PARTITION BY evidence.source_uri) AS has_supports, "
        " MAX(evidence.stance = 'contradicts') OVER (PARTITION BY evidence.source_uri) AS has_contradicts "
        " FROM event_group CROSS JOIN event_evidence AS evidence "
        " INDEXED BY idx_event_evidence_event_time "
        " ON event_group.event_id = evidence.event_id "
        " CROSS JOIN events AS evidence_event ON evidence_event.event_id = evidence.event_id "
        " WHERE evidence.mapping_id IS NULL AND evidence_event.available_at <= ? "
        " AND evidence.observed_at <= ? AND "
        + public_evidence_predicate
        + "), deduplicated AS ("
        " SELECT *, COUNT(*) OVER () AS total_sources FROM ranked WHERE source_rank = 1"
        ") SELECT evidence_id, source_uri, source_name, claim_key, claim_text, excerpt, stance, "
        " reliability, announcement_at, observed_at, version, observation_count, total_sources, "
        " has_supports, has_contradicts "
        " FROM deduplicated ORDER BY "
        " CASE stance WHEN 'supports' THEN 0 WHEN 'context' THEN 1 ELSE 2 END, "
        " reliability DESC, COALESCE(announcement_at, observed_at) DESC, evidence_id DESC "
        " LIMIT ?",
        (
            root_event_id,
            read_at,
            read_at,
            *_PUBLIC_SCORE_PATH_VALUES,
            MAX_SOURCES + 1,
        ),
    ).fetchall()
    total = int(rows[0]["total_sources"]) if rows else 0
    truncated = total > MAX_SOURCES
    sources = [_source_dict(row) for row in rows[:MAX_SOURCES]]
    observations = _load_source_observations(
        conn, root_event_id, read_at,
        [str(row["source_uri"]) for row in rows[:MAX_SOURCES] if row["observation_count"] > 1],
    )
    for row, source in zip(rows, sources):
        source["has_stance_conflict"] = bool(row["has_supports"] and row["has_contradicts"])
        source["observations"] = observations.get(str(row["source_uri"]), [])
        source["observations_truncated"] = source["observation_count"] > MAX_SOURCE_OBSERVATIONS
    summary = ""
    for source in sources:
        # Publisher title is the original claim. Do not prefer excerpt: historical
        # rows may store a generated summary there.
        summary = source["claim_text"].strip()
        if summary:
            break
    return sources, total, truncated, summary[:4000]


def _load_source_observations(
    conn: sqlite3.Connection, root_event_id: str, read_at: int, source_uris: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Read a bounded history in one query, retaining the latest of each stance.

    A shared URL is a publisher location, not an immutable claim identity.
    Different stances remain observations; this does not resolve their truth.
    """
    if not source_uris:
        return {}
    placeholders = ",".join("?" for _ in source_uris)
    rows = conn.execute(
        _event_group_cte() + ", stance_ranked AS ("
        " SELECT evidence.*, COUNT(*) OVER (PARTITION BY evidence.source_uri) AS observation_count, "
        " ROW_NUMBER() OVER (PARTITION BY evidence.source_uri, evidence.stance ORDER BY "
        " evidence.observed_at DESC, evidence.version DESC, evidence.evidence_id DESC) AS stance_rank "
        " FROM event_group CROSS JOIN event_evidence AS evidence "
        " INDEXED BY idx_event_evidence_event_time ON event_group.event_id = evidence.event_id "
        " CROSS JOIN events AS evidence_event ON evidence_event.event_id = evidence.event_id "
        f" WHERE evidence.source_uri IN ({placeholders}) AND evidence.mapping_id IS NULL "
        " AND evidence_event.available_at <= ? AND evidence.observed_at <= ? AND "
        + _public_cache_evidence_predicate("evidence")
        + "), history_ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY source_uri "
        " ORDER BY CASE WHEN stance_rank = 1 THEN 0 ELSE 1 END, "
        " observed_at DESC, version DESC, evidence_id DESC) AS history_rank FROM stance_ranked) "
        " SELECT * FROM history_ranked WHERE history_rank <= ? "
        " ORDER BY source_uri, observed_at DESC, version DESC, evidence_id DESC",
        (root_event_id, *source_uris, read_at, read_at, *_PUBLIC_SCORE_PATH_VALUES, MAX_SOURCE_OBSERVATIONS),
    ).fetchall()
    histories: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        histories.setdefault(str(row["source_uri"]), []).append(_source_dict(row))
    return histories


def _event_group_has_public_evidence(
    conn: sqlite3.Connection,
    root_event_id: str,
    read_at: int,
) -> bool:
    public_evidence_predicate = _public_cache_evidence_predicate("evidence")
    row = conn.execute(
        _event_group_cte()
        + "SELECT COUNT(evidence.evidence_id) AS evidence_count, "
        "COALESCE(SUM(CASE WHEN "
        + public_evidence_predicate
        + " THEN 1 ELSE 0 END), 0) AS public_count "
        "FROM event_group LEFT JOIN event_evidence AS evidence "
        "ON evidence.event_id = event_group.event_id "
        "AND evidence.mapping_id IS NULL AND evidence.observed_at <= ?",
        (
            root_event_id,
            *_PUBLIC_SCORE_PATH_VALUES,
            read_at,
        ),
    ).fetchone()
    return bool(
        row is not None
        and (
            int(row["evidence_count"] or 0) == 0
            or int(row["public_count"] or 0) > 0
        )
    )


def _merge_stock_mapping(
    stocks_by_id: dict[str, dict[str, Any]],
    row: sqlite3.Row | dict[str, Any],
    rank_by_security: dict[str, tuple[float, float, float | None]],
    evidence_by_mapping: dict[str, list[dict[str, Any]]],
    current_versions: set[str] | None = None,
    current_contract_available: bool = True,
) -> None:
    """Merge one ranked mapping row into a stock read-model entry.

    This pure formatting helper mutates only ``stocks_by_id``. Keeping row
    normalization outside the SQL loader makes conflict aggregation directly
    testable without a database.
    """
    security_id = str(row["security_id"])
    stock = stocks_by_id.get(security_id)
    if stock is None:
        best_relevance, best_confidence, best_expected_gap = rank_by_security[
            security_id
        ]
        stock = {
            "security_id": security_id,
            "exchange": _bounded_text(row["exchange"], 32),
            "ticker": _bounded_text(row["ticker"], 32),
            "name": _bounded_text(row["name"], 255),
            "asset_type": _bounded_text(row["asset_type"], 64),
            "currency": _bounded_text(row["currency"], 16),
            "aliases": _bounded_json_strings(row["aliases"]),
            "industry": _bounded_text(row["industry"], 255),
            "concepts": _bounded_json_strings(row["concepts"]),
            "is_active": bool(row["is_active"]),
            "version": int(row["security_version"]),
            "best_relevance": best_relevance,
            "best_confidence": best_confidence,
            "best_mapping_confidence": best_confidence,
            "best_expected_gap": best_expected_gap,
            "mapping_candidate_count": 0,
            "has_direction_conflict": False,
            "relations": [],
        }
        stocks_by_id[security_id] = stock
    mapping_id = str(row["mapping_id"])
    relation_type = str(row["relation_type"])
    candidate_count = int(row["candidate_count"])
    direction_variant_count = int(row["direction_variant_count"])
    direction_conflict = direction_variant_count > 1
    stock["mapping_candidate_count"] += candidate_count
    stock["has_direction_conflict"] = bool(
        stock["has_direction_conflict"] or direction_conflict
    )
    stock["relations"].append(
        {
            "mapping_id": mapping_id,
            "event_id": str(row["mapping_event_id"]),
            "relation_type": relation_type,
            "relation_label": _RELATION_LABELS[relation_type],
            "direction": _bounded_text(row["direction"], 64),
            "expected_gap": _finite_float(row["expected_gap"]),
            "confidence": float(row["confidence"]),
            "mapping_confidence": float(row["confidence"]),
            "relevance": float(row["relevance"]),
            "rationale": _bounded_text(row["rationale"], 4000),
            "mapping_version": _bounded_text(row["mapping_version"], 255),
            "revision": int(row["revision"]),
            "created_at": int(row["created_at"]),
            **_mapping_contract_view(
                row["mapping_version"],
                bool(row["mapping_source_current"]) if "mapping_source_current" in row.keys() else False,
                current_versions,
                current_contract_available,
            ),
            "candidate_count": candidate_count,
            "candidate_event_count": int(row["candidate_event_count"]),
            "direction_variant_count": direction_variant_count,
            "direction_conflict": direction_conflict,
            "evidence": evidence_by_mapping.get(mapping_id, []),
        }
    )


def _load_top_stock_rows(
    conn: sqlite3.Connection, root_event_id: str, read_at: int
) -> list[sqlite3.Row]:
    """Select detail equities with the same representative/order policy as chips."""
    relation_placeholders = ",".join("?" for _ in _RELATION_SQL_VALUES)
    return conn.execute(
        _event_group_cte() + ", ranked_security AS ("
        " SELECT security.security_id, mapping.relevance, mapping.confidence, "
        " mapping.expected_gap, "
        f" {_STOCK_RELATION_WEAKNESS_SQL} AS relation_weakness, "
        " ROW_NUMBER() OVER (PARTITION BY security.security_id "
        f" ORDER BY {_STOCK_MAPPING_ORDER_SQL}) AS security_rank "
        " FROM event_group CROSS JOIN event_securities AS mapping "
        " INDEXED BY uq_event_securities_current ON event_group.event_id = mapping.event_id "
        " CROSS JOIN events AS mapping_event ON mapping_event.event_id = mapping.event_id "
        " CROSS JOIN securities AS security ON security.security_id = mapping.security_id "
        " WHERE mapping.is_current = 1 AND security.is_active = 1 "
        " AND mapping.created_at <= ? AND mapping_event.available_at <= ? "
        " AND security.created_at <= ? "
        " AND LOWER(security.asset_type) = 'equity' "
        f" AND LOWER(mapping.relation_type) IN ({relation_placeholders})"
        ") SELECT security_id, relevance AS best_relevance, "
        " confidence AS best_confidence, expected_gap AS best_expected_gap "
        " FROM ranked_security "
        " WHERE security_rank = 1 "
        f" ORDER BY {_STOCK_DISPLAY_ORDER_SQL} "
        " LIMIT 3",
        (root_event_id, read_at, read_at, read_at, *_RELATION_SQL_VALUES),
    ).fetchall()


def _load_stocks(
    conn: sqlite3.Connection, root_event_id: str, read_at: int
) -> list[dict[str, Any]]:
    """Load top-three detail equities, preserving all selected relation evidence.

    Selection shares the list's non-concept-first representative mapping policy.
    Each stock's best relevance, confidence and gap come from that same mapping;
    relation aggregation and conflict disclosure remain independent of selection.
    """
    logger.debug("_load_stocks: root_event_id=%s", root_event_id)
    relation_placeholders = ",".join("?" for _ in _RELATION_SQL_VALUES)
    top_rows = _load_top_stock_rows(conn, root_event_id, read_at)
    security_ids = [str(row["security_id"]) for row in top_rows]
    if not security_ids:
        return []
    rank_by_security = {
        str(row["security_id"]): (
            float(row["best_relevance"]),
            float(row["best_confidence"]),
            _finite_float(row["best_expected_gap"]),
        )
        for row in top_rows
    }
    security_placeholders = ",".join("?" for _ in security_ids)
    rows = conn.execute(
        _event_group_cte() + ", read_boundary(read_at) AS (VALUES (?)), mapping_candidates AS ("
        " SELECT security.security_id, security.exchange, security.ticker, security.name, "
        " security.asset_type, security.currency, security.aliases, security.industry, "
        " security.concepts, security.is_active, security.version AS security_version, "
        " mapping.mapping_id, mapping.event_id AS mapping_event_id, "
        " LOWER(mapping.relation_type) AS relation_type, mapping.direction, mapping.expected_gap, "
        " mapping.confidence, mapping.relevance, mapping.rationale, mapping.mapping_version, "
        f" ({_mapping_source_contract_sql()}) AS mapping_source_current, "
        " mapping.revision, mapping.created_at, "
        " CASE LOWER(mapping.relation_type) "
        " WHEN 'direct' THEN 'direct' WHEN 'direct_impact' THEN 'direct' "
        " WHEN 'affected' THEN 'direct' ELSE LOWER(mapping.relation_type) "
        " END AS canonical_relation, "
        " CASE LOWER(TRIM(mapping.direction)) "
        " WHEN '看多' THEN 'bullish' WHEN '利多' THEN 'bullish' "
        " WHEN 'bullish' THEN 'bullish' WHEN 'positive' THEN 'bullish' "
        " WHEN 'up' THEN 'bullish' WHEN 'long' THEN 'bullish' "
        " WHEN '看空' THEN 'bearish' WHEN '利空' THEN 'bearish' "
        " WHEN 'bearish' THEN 'bearish' WHEN 'negative' THEN 'bearish' "
        " WHEN 'down' THEN 'bearish' WHEN 'short' THEN 'bearish' "
        " WHEN '中性' THEN 'neutral' WHEN 'neutral' THEN 'neutral' "
        " WHEN 'flat' THEN 'neutral' WHEN 'mixed' THEN 'neutral' "
        " ELSE LOWER(TRIM(mapping.direction)) END AS direction_bucket "
        " FROM event_group CROSS JOIN event_securities AS mapping "
        " INDEXED BY uq_event_securities_current ON event_group.event_id = mapping.event_id "
        " CROSS JOIN events AS mapping_event ON mapping_event.event_id = mapping.event_id "
        " CROSS JOIN securities AS security ON security.security_id = mapping.security_id "
        " WHERE mapping.is_current = 1 AND security.is_active = 1 "
        " AND mapping.created_at <= ? AND mapping_event.available_at <= ? "
        " AND security.created_at <= ? "
        " AND LOWER(security.asset_type) = 'equity' "
        f" AND security.security_id IN ({security_placeholders}) "
        f" AND LOWER(mapping.relation_type) IN ({relation_placeholders})"
        "), candidate_stats AS ("
        " SELECT security_id, canonical_relation, COUNT(*) AS candidate_count, "
        " COUNT(DISTINCT mapping_event_id) AS candidate_event_count, "
        " COUNT(DISTINCT direction_bucket) AS direction_variant_count "
        " FROM mapping_candidates GROUP BY security_id, canonical_relation"
        "), ranked AS ("
        " SELECT mapping_candidates.*, ROW_NUMBER() OVER ("
        " PARTITION BY security_id, canonical_relation "
        " ORDER BY relevance DESC, confidence DESC, created_at DESC, mapping_id DESC"
        " ) AS relation_rank FROM mapping_candidates"
        ") SELECT ranked.*, candidate_stats.candidate_count, "
        " candidate_stats.candidate_event_count, "
        " candidate_stats.direction_variant_count FROM ranked JOIN candidate_stats "
        " USING (security_id, canonical_relation) WHERE relation_rank = 1 "
        " ORDER BY relevance DESC, confidence DESC, security_id ASC, canonical_relation ASC",
        (
            root_event_id,
            read_at,
            read_at,
            read_at,
            read_at,
            *security_ids,
            *_RELATION_SQL_VALUES,
        ),
    ).fetchall()

    # Alias relation values (``affected``/``direct``/``direct_impact``) are
    # already collapsed by the SQL ranking.  Candidate counts and normalized
    # direction variants preserve conflicts introduced by later event merges
    # instead of silently making the discarded current mappings invisible.
    mapping_rows = list(rows)
    evidence_by_mapping = _load_stock_mapping_evidence(conn, mapping_rows, read_at)
    stocks_by_id: dict[str, dict[str, Any]] = {}
    current_versions, contract_available = _current_mapping_contracts(
        conn, [str(row["mapping_version"]) for row in mapping_rows],
    )
    for row in mapping_rows:
        _merge_stock_mapping(
            stocks_by_id, row, rank_by_security, evidence_by_mapping, current_versions, contract_available,
        )
    return [stocks_by_id[security_id] for security_id in security_ids]


def _load_stock_mapping_evidence(
    conn: sqlite3.Connection, mapping_rows: list[sqlite3.Row], read_at: int,
) -> dict[str, list[dict[str, Any]]]:
    """Read bounded attached observations, preserving their explicit source identity."""
    evidence_by_mapping: dict[str, list[dict[str, Any]]] = {
        str(row["mapping_id"]): [] for row in mapping_rows
    }
    if mapping_rows:
        mapping_ids = [str(row["mapping_id"]) for row in mapping_rows]
        mapping_placeholders = ",".join("?" for _ in mapping_ids)
        evidence_rows = conn.execute(
            "WITH source_ranked AS ("
            " SELECT evidence.*, ROW_NUMBER() OVER ("
            " PARTITION BY evidence.mapping_id, evidence.source_uri "
            " ORDER BY evidence.reliability DESC, "
            " COALESCE(evidence.announcement_at, evidence.observed_at) DESC, "
            " evidence.observed_at DESC, evidence.evidence_id DESC) AS source_rank, "
            " COUNT(*) OVER (PARTITION BY evidence.mapping_id, evidence.source_uri) "
            " AS observation_count "
            " FROM event_evidence AS evidence JOIN event_securities AS mapping "
            " ON mapping.mapping_id = evidence.mapping_id "
            " AND mapping.revision = evidence.mapping_revision "
            " AND mapping.event_id = evidence.event_id "
            " AND mapping.security_id = evidence.security_id "
            f" WHERE evidence.mapping_id IN ({mapping_placeholders})"
            " AND evidence.observed_at <= ?"
            "), ranked AS ("
            " SELECT source_ranked.*, ROW_NUMBER() OVER (PARTITION BY mapping_id "
            " ORDER BY reliability DESC, COALESCE(announcement_at, observed_at) DESC, "
            " observed_at DESC, evidence_id DESC) AS evidence_rank "
            " FROM source_ranked WHERE source_rank = 1"
            ") SELECT * FROM ranked WHERE evidence_rank <= ? "
            " ORDER BY mapping_id ASC, evidence_rank ASC",
            (*mapping_ids, read_at, MAX_MAPPING_EVIDENCE),
        ).fetchall()
        for evidence in evidence_rows:
            evidence_by_mapping.setdefault(str(evidence["mapping_id"]), []).append(
                _source_dict(evidence)
            )

    return evidence_by_mapping


def _load_market_references(
    conn: sqlite3.Connection, root_event_id: str, read_at: int
) -> list[dict[str, Any]]:
    logger.debug("event_radar market references root_event_id=%s", root_event_id)
    rows = conn.execute(
        _event_group_cte() + ", ranked AS ("
        " SELECT security.security_id, security.exchange, security.ticker, "
        " security.name, security.asset_type, mapping.mapping_id, mapping.direction, "
        " mapping.confidence, mapping.rationale, "
        " ROW_NUMBER() OVER (PARTITION BY security.security_id "
        " ORDER BY mapping.relevance DESC, mapping.confidence DESC, "
        " mapping.created_at DESC, mapping.mapping_id DESC) AS reference_rank "
        " FROM event_group AS grouped JOIN event_securities AS mapping "
        " ON mapping.event_id = grouped.event_id "
        " JOIN events AS mapping_event ON mapping_event.event_id = mapping.event_id "
        " JOIN securities AS security ON security.security_id = mapping.security_id "
        " WHERE mapping.is_current = 1 AND security.is_active = 1 "
        " AND mapping.created_at <= ? AND mapping_event.available_at <= ? "
        " AND security.created_at <= ? "
        " AND lower(mapping.relation_type) = 'market_reference' "
        " AND lower(security.asset_type) = 'index'"
        ") SELECT * FROM ranked WHERE reference_rank = 1 "
        "ORDER BY security_id LIMIT 8",
        (root_event_id, read_at, read_at, read_at),
    ).fetchall()
    return [
        {
            "security_id": str(row["security_id"]),
            "exchange": _bounded_text(row["exchange"], 32),
            "ticker": _bounded_text(row["ticker"], 32),
            "name": _bounded_text(row["name"], 255),
            "asset_type": _bounded_text(row["asset_type"], 64),
            "reference_type": "explicit_mention",
            "direction": _bounded_text(row["direction"], 64),
            "mapping_confidence": float(row["confidence"]),
            "rationale": _bounded_text(row["rationale"], 4000),
        }
        for row in rows
    ]


def _wilson_interval(successes: int, samples: int) -> tuple[float | None, float | None]:
    """Return a 95% Wilson interval for an auditable binary cohort."""
    if samples <= 0:
        return None, None
    bounded_successes = max(0, min(int(successes), int(samples)))
    z = 1.959963984540054
    rate = bounded_successes / samples
    denominator = 1.0 + (z * z / samples)
    centre = (rate + z * z / (2.0 * samples)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1.0 - rate) / samples + z * z / (4.0 * samples * samples))
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _historical_outcome_stats(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> tuple[int, int, int]:
    """Compute directional accuracy stats from strictly prior outcomes.

    Retrieves historical outcome records for the same security/horizon that
    occurred before the current signal. Computes direction hit rate from
    this prior cohort under exact current contract.

    Args:
        conn: Active SQLite connection with db_lock held.
        row: Current outcome row with signal_available_at, security_id,
            horizon, mapping_id, and method_version.

    Returns:
        Tuple of (hits, total, unique_days) where hits is direction matches,
        total is prior outcomes, unique_days is independent result days.
    """
    logger.debug("_historical_outcome_stats: processing outcome row")
    cutoff = row["signal_available_at"]
    if cutoff is None:
        return 0, 0, 0
    candidates = conn.execute(
        "SELECT historical.outcome_id, historical.event_id, historical.security_id, "
        "historical.mapping_id, historical.mapping_revision, historical.horizon, "
        "historical.method_version, historical.created_at, "
        "historical.benchmark_security_id, historical.data_version, "
        "historical.signal_ledger_id, historical.signal_available_at, "
        "historical.observation_state, "
        "historical.baseline_at, historical.measured_at, historical.baseline_price, "
        "historical.gross_return, historical.benchmark_return, historical.excess_return, "
        "historical.mfe, historical.mae, historical.direction_hit, "
        "historical.data_quality, "
        "historical_mapping.direction AS mapping_direction, "
        "historical_mapping.created_at AS mapping_created_at, "
        "historical_mapping.mapping_version AS mapping_version, "
        "ledger.event_id AS ledger_event_id, "
        "ledger.event_mapping_id AS ledger_mapping_id, "
        "ledger.backtest_symbol AS ledger_security_id, "
        "ledger.available_at AS ledger_available_at, "
        "ledger.recorded_at AS ledger_recorded_at, ledger.selected AS ledger_selected, "
        "ledger.is_similarity_copy AS ledger_similarity_copy, "
        "ledger.score_path AS ledger_score_path, "
        "ledger.event_version AS ledger_event_version, "
        "ledger.direction AS ledger_direction, "
        "ledger.strategy_version AS ledger_strategy_version, "
        "ledger.mapping_version AS ledger_mapping_version, "
        "frozen.mapping_id AS frozen_mapping_id, "
        "frozen.mapping_revision AS frozen_mapping_revision, "
        "frozen.signal_available_at AS frozen_available_at, "
        "frozen.captured_at AS frozen_captured_at "
        "FROM event_outcomes AS historical "
        "JOIN event_securities AS historical_mapping "
        "ON historical_mapping.mapping_id = historical.mapping_id "
        "AND historical_mapping.event_id = historical.event_id "
        "AND historical_mapping.security_id = historical.security_id "
        "AND historical_mapping.revision = historical.mapping_revision "
        "LEFT JOIN backtest_signals AS ledger "
        "ON ledger.id = historical.signal_ledger_id "
        "LEFT JOIN backtest_signal_securities AS frozen "
        "ON frozen.ledger_signal_id = historical.signal_ledger_id "
        "AND frozen.mapping_id = historical.mapping_id "
        "AND frozen.event_id = historical.event_id "
        "AND frozen.security_id = historical.security_id "
        "AND frozen.mapping_revision = historical.mapping_revision "
        "WHERE historical.is_current = 1 "
        "AND historical_mapping.is_current = 1 "
        "AND historical.observation_state = 'complete' "
        "AND historical.direction_hit IS NOT NULL "
        "AND historical.security_id = ? AND historical.horizon = ? "
        "AND historical.method_version = ? "
        "AND historical.benchmark_security_id IS ? "
        "AND LOWER(historical_mapping.relation_type) = LOWER(?) "
        "AND ledger.strategy_version = ? AND ledger.mapping_version = ? "
        "AND historical.signal_available_at IS NOT NULL "
        "AND historical.signal_available_at < ? "
        "AND historical.measured_at < ? AND historical.created_at < ? "
        "AND historical.data_version LIKE ? "
        "ORDER BY historical.signal_available_at, historical.outcome_id LIMIT ?",
        (
            str(row["security_id"]),
            str(row["horizon"]),
            str(row["method_version"]),
            row["benchmark_security_id"],
            str(row["relation_type"]),
            str(row["ledger_strategy_version"]),
            str(row["ledger_mapping_version"]),
            int(cutoff),
            int(cutoff),
            int(cutoff),
            OUTCOME_DATA_VERSION_PREFIX + "%",
            MAX_HISTORICAL_COHORT + 1,
        ),
    ).fetchall()
    if len(candidates) > MAX_HISTORICAL_COHORT:
        raise EventRadarUnavailableError(
            "historical outcome cohort exceeds the auditable read bound"
        )
    revision_ids: set[str] = set()
    for candidate in candidates:
        data_version = str(candidate["data_version"] or "")
        if data_version.startswith(OUTCOME_DATA_VERSION_PREFIX):
            revision_ids.update(
                data_version[len(OUTCOME_DATA_VERSION_PREFIX) :].split(":")
            )
    revisions = _load_price_revisions(conn, revision_ids)
    valid = [
        candidate
        for candidate in candidates
        if _revision_contract_is_valid(candidate, revisions)
    ]
    outcome_ids = {str(candidate["outcome_id"]) for candidate in valid}
    independent_dates = {
        _market_date(candidate["measured_at"])
        for candidate in valid
        if _market_date(candidate["measured_at"]) is not None
    }
    successful_ids = {
        str(candidate["outcome_id"])
        for candidate in valid
        if int(candidate["direction_hit"]) == 1
    }
    return len(outcome_ids), len(independent_dates), len(successful_ids)


def _market_date(timestamp: object) -> str | None:
    try:
        value = int(timestamp)
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, _MARKET_TZ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _market_close_timestamp(trade_date: str) -> int | None:
    try:
        day = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
        return int(datetime.combine(day, _MARKET_CLOSE, tzinfo=_MARKET_TZ).timestamp())
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _load_price_revisions(
    conn: sqlite3.Connection,
    revision_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not revision_ids:
        return {}
    placeholders = ",".join("?" for _ in revision_ids)
    rows = conn.execute(
        "SELECT revision_id, symbol, trade_date, close_price, open_price, "
        "high_price, low_price, suspension_state, limit_state, "
        "tradability_state, source_ts, recorded_at "
        f"FROM future_price_revisions WHERE revision_id IN ({placeholders})",
        tuple(sorted(revision_ids)),
    ).fetchall()
    return {str(item["revision_id"]): dict(item) for item in rows}


def _price_revision_timestamp(revision: dict[str, Any]) -> int | None:
    try:
        source_timestamp = int(revision.get("source_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if source_timestamp > 0:
        return source_timestamp
    return _market_close_timestamp(str(revision.get("trade_date") or ""))


def _revision_contract_is_valid(
    row: sqlite3.Row,
    revisions: dict[str, dict[str, Any]],
) -> bool:
    """Validate that an outcome row satisfies current contract requirements.

    Checks whether an outcome record matches the current scoring strategy,
    mapping version, and outcome method using price revision data.

    Args:
        row: Outcome row from SQLite with security_id, method_version, horizon.
        revisions: Dict mapping security IDs to their price revision metadata.

    Returns:
        True if row satisfies current contract, False otherwise.
    """
    if not is_current_outcome_contract(
        security_id=row["security_id"],
        method_version=row["method_version"],
        horizon=row["horizon"],
        benchmark_security_id=row["benchmark_security_id"],
        data_version=row["data_version"],
        signal_available_at=row["signal_available_at"],
        baseline_at=row["baseline_at"],
    ):
        return False
    try:
        if str(row["observation_state"]) != "complete":
            return False
        data_quality = float(row["data_quality"])
        if not math.isfinite(data_quality) or not 0.0 <= data_quality <= 1.0:
            return False
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return False
    try:
        ledger_is_exact = (
            int(row["signal_ledger_id"]) >= 1
            and int(row["ledger_available_at"]) == int(row["signal_available_at"])
            and int(row["ledger_recorded_at"]) == int(row["frozen_captured_at"])
            and int(row["ledger_selected"]) == 1
            and int(row["ledger_similarity_copy"]) == 0
            and normalize_score_path(row["ledger_score_path"])
            in FORMAL_SIGNAL_SCORE_PATHS
            and int(row["ledger_event_version"]) >= 1
            and str(row["ledger_event_id"]) == str(row["event_id"])
            and str(row["ledger_mapping_version"] or "")
            == str(row["mapping_version"] or "")
            and str(row["frozen_mapping_id"]) == str(row["mapping_id"])
            and int(row["frozen_mapping_revision"]) == int(row["mapping_revision"])
            and int(row["frozen_available_at"]) == int(row["signal_available_at"])
            and int(row["mapping_created_at"]) <= int(row["frozen_captured_at"])
        )
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        ledger_is_exact = False
    if not ledger_is_exact:
        return False
    revision_ids = str(row["data_version"])[len(OUTCOME_DATA_VERSION_PREFIX) :].split(
        ":"
    )
    referenced = [revisions.get(revision_id) for revision_id in revision_ids]
    if any(item is None for item in referenced):
        return False
    try:
        outcome_created_at = int(row["created_at"])
        recorded_at = [int(revision["recorded_at"]) for revision in referenced]
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return False
    if outcome_created_at <= 0 or any(
        value <= 0 or value > outcome_created_at for value in recorded_at
    ):
        return False
    asset_baseline, asset_measured, benchmark_baseline, benchmark_measured = referenced
    baseline_date = _market_date(row["baseline_at"])
    measured_date = _market_date(row["measured_at"])
    if baseline_date is None or measured_date is None:
        return False
    try:
        offset = int(str(row["horizon"]).removeprefix("T+"))
        expected_baseline_date = baseline_trade_date(int(row["signal_available_at"]))
        expected_measured_date = (
            None
            if expected_baseline_date is None
            else horizon_trade_date(expected_baseline_date, offset)
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        baseline_date != expected_baseline_date
        or measured_date != expected_measured_date
    ):
        return False
    asset_symbol = str(asset_baseline["symbol"]).casefold()
    if not asset_symbol or str(asset_measured["symbol"]).casefold() != asset_symbol:
        return False
    expected = (
        (asset_symbol, baseline_date),
        (asset_symbol, measured_date),
        (MARKET_BENCHMARK_SYMBOL, baseline_date),
        (MARKET_BENCHMARK_SYMBOL, measured_date),
    )
    if any(
        str(revision["symbol"]).casefold() != symbol
        or not is_completed_close_price(
            revision, trade_date, evaluation_at=outcome_created_at, timestamp_key="source_ts"
        )
        for revision, (symbol, trade_date) in zip(referenced, expected)
    ):
        return False
    revision_timestamps = [
        _price_revision_timestamp(revision) for revision in referenced
    ]
    if any(value is None for value in revision_timestamps):
        return False
    if (
        int(revision_timestamps[0]) != int(row["baseline_at"])
        or int(revision_timestamps[1]) != int(row["measured_at"])
        or int(revision_timestamps[2]) <= int(row["signal_available_at"])
    ):
        return False
    try:
        asset_base = float(asset_baseline["close_price"])
        asset_target = float(asset_measured["close_price"])
        benchmark_base = float(benchmark_baseline["close_price"])
        benchmark_target = float(benchmark_measured["close_price"])
        if min(asset_base, asset_target, benchmark_base, benchmark_target) <= 0:
            return False
        gross = asset_target / asset_base - 1.0
        market = benchmark_target / benchmark_base - 1.0
        stored = (
            float(row["baseline_price"]),
            float(row["gross_return"]),
            float(row["benchmark_return"]),
            float(row["excess_return"]),
        )
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False
    calculated = (asset_base, gross, market, gross - market)
    returns_match = all(
        math.isfinite(actual)
        and math.isfinite(value)
        and math.isclose(actual, value, rel_tol=1e-10, abs_tol=1e-12)
        for actual, value in zip(stored, calculated)
    )
    if not returns_match:
        return False
    direction_value = (
        row["mapping_direction"]
        if "mapping_direction" in row.keys()
        else row["ledger_direction"]
    )
    expected_hit = direction_hit_for_return(str(direction_value), gross)
    stored_hit = row["direction_hit"]
    if expected_hit is None:
        return stored_hit is None
    try:
        return stored_hit is not None and int(stored_hit) == int(expected_hit)
    except (TypeError, ValueError, OverflowError):
        return False


def _tradability_for_symbol(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    signal_available_at: int,
    read_at: int,
) -> dict[str, Any]:
    trade_date = tradable_baseline_trade_date(signal_available_at)
    if not symbol or trade_date is None:
        return {}
    try:
        open_at = int(
            datetime.combine(
                datetime.strptime(trade_date, "%Y-%m-%d").date(),
                datetime_time(9, 30),
                tzinfo=_MARKET_TZ,
            ).timestamp()
        )
    except (TypeError, ValueError, OSError, OverflowError):
        open_at = None
    tradable = conn.execute(
        "SELECT open_price, suspension_state, limit_state, tradability_state "
        "FROM future_price_revisions WHERE lower(symbol) = ? AND trade_date = ? "
        "AND recorded_at <= ? "
        "ORDER BY recorded_at DESC, revision_id DESC LIMIT 1",
        (symbol.casefold(), trade_date, read_at),
    ).fetchone()
    if tradable is None:
        return {
            "tradable_baseline_at": open_at,
            "tradable_baseline_price": None,
            "tradability_status": "missing_open",
            "suspension_status": "unknown",
            "limit_status": "unknown",
        }
    try:
        open_price = float(tradable["open_price"])
        if not math.isfinite(open_price) or open_price <= 0:
            open_price = None
    except (TypeError, ValueError):
        open_price = None
    return {
        "tradable_baseline_at": open_at,
        "tradable_baseline_price": open_price,
        "tradability_status": _bounded_text(tradable["tradability_state"], 32),
        "suspension_status": _bounded_text(tradable["suspension_state"], 32),
        "limit_status": _bounded_text(tradable["limit_state"], 32),
    }


def _outcome_revision_ids(data_version: object) -> list[str]:
    value = str(data_version or "")
    for prefix in (
        OUTCOME_DATA_VERSION_PREFIX,
        LEGACY_OUTCOME_DATA_VERSION_PREFIX,
    ):
        if value.startswith(prefix):
            parts = value[len(prefix) :].split(":")
            return parts if len(parts) == 4 else []
    return []


def _tradability_view(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    revisions: dict[str, dict[str, Any]],
    read_at: int,
) -> dict[str, Any]:
    revision_ids = _outcome_revision_ids(row["data_version"])
    if len(revision_ids) != 4:
        return {}
    baseline = revisions.get(revision_ids[0])
    if baseline is None:
        return {}
    symbol = str(baseline.get("symbol") or "").casefold()
    return _tradability_for_symbol(
        conn,
        symbol=symbol,
        signal_available_at=int(row["signal_available_at"] or 0),
        read_at=read_at,
    )


def _load_actual_series_keys(
    conn: sqlite3.Connection,
    root_event_id: str,
    read_at: int,
) -> set[tuple[str, str, str, str, str, str, str, int, int]]:
    """Return every visible materialized identity, including folded rows.

    The public outcome list intentionally folds duplicate rows within one
    current/retired mapping layer.  Pending suppression cannot use only that
    representative: a materialized row hidden by the fold would otherwise be
    mislabeled ``complete_pending_materialization``.  This bounded identity
    read keeps presentation folding separate from immutable signal contracts.
    """
    read_limit = MAX_OUTCOMES * 20
    rows = conn.execute(
        _event_group_cte() + "SELECT ledger.strategy_version, ledger.mapping_version, "
        "outcome.security_id, outcome.horizon, outcome.method_version, "
        "outcome.benchmark_security_id, outcome.mapping_id, "
        "outcome.mapping_revision, outcome.signal_ledger_id "
        "FROM event_outcomes AS outcome JOIN event_group "
        "ON event_group.event_id = outcome.event_id "
        "JOIN events AS outcome_event ON outcome_event.event_id = outcome.event_id "
        "JOIN event_securities AS mapping ON mapping.mapping_id = outcome.mapping_id "
        "AND mapping.event_id = outcome.event_id "
        "AND mapping.security_id = outcome.security_id "
        "AND mapping.revision = outcome.mapping_revision "
        "LEFT JOIN backtest_signals AS ledger ON ledger.id = outcome.signal_ledger_id "
        "LEFT JOIN backtest_signal_securities AS frozen "
        "ON frozen.ledger_signal_id = outcome.signal_ledger_id "
        "AND frozen.mapping_id = outcome.mapping_id "
        "AND frozen.event_id = outcome.event_id "
        "AND frozen.security_id = outcome.security_id "
        "AND frozen.mapping_revision = outcome.mapping_revision "
        "WHERE outcome.is_current = 1 "
        "AND outcome.created_at <= ? AND outcome.measured_at <= ? "
        "AND (outcome.signal_available_at IS NULL "
        "OR outcome.signal_available_at <= ?) "
        "AND mapping.created_at <= ? AND outcome_event.available_at <= ? "
        "AND (ledger.recorded_at IS NULL OR ledger.recorded_at <= ?) "
        "AND (frozen.captured_at IS NULL OR frozen.captured_at <= ?) "
        "ORDER BY outcome.created_at DESC, outcome.outcome_revision_id DESC LIMIT ?",
        (
            root_event_id,
            read_at,
            read_at,
            read_at,
            read_at,
            read_at,
            read_at,
            read_at,
            read_limit + 1,
        ),
    ).fetchall()
    if len(rows) > read_limit:
        raise EventRadarUnavailableError(
            "materialized outcome identity cohort exceeds the auditable read bound"
        )
    return {
        (
            str(row["strategy_version"] or ""),
            str(row["mapping_version"] or ""),
            str(row["security_id"]).casefold(),
            str(row["horizon"]),
            str(row["method_version"]),
            str(row["benchmark_security_id"] or "").casefold(),
            str(row["mapping_id"] or ""),
            int(row["mapping_revision"] or 0),
            int(row["signal_ledger_id"] or 0),
        )
        for row in rows
    }


def _materialized_outcome_view(
    row: sqlite3.Row | dict[str, Any],
    *,
    tradability: dict[str, Any],
    method_supported: bool,
    mapping_current: bool,
    samples: int,
    independent_days: int,
    successes: int,
    ci_low: float | None,
    ci_high: float | None,
) -> dict[str, Any]:
    """Build the stable API representation for one materialized outcome row."""
    benchmark_id = (
        None
        if row["benchmark_security_id"] is None
        else str(row["benchmark_security_id"])
    )
    gross_return = _finite_float(row["gross_return"])
    benchmark_return = _finite_float(row["benchmark_return"])
    excess_return = _finite_float(row["excess_return"])
    complete_close_result = all(
        value is not None for value in (gross_return, benchmark_return, excess_return)
    )
    return {
        "outcome_id": str(row["outcome_id"]),
        "outcome_revision_id": str(row["outcome_revision_id"]),
        "event_id": str(row["event_id"]),
        "mapping_id": str(row["mapping_id"]),
        "mapping_revision": int(row["mapping_revision"]),
        "horizon": _bounded_text(row["horizon"], 64),
        "signal_ledger_id": (
            None if row["signal_ledger_id"] is None else int(row["signal_ledger_id"])
        ),
        "strategy_version": _bounded_text(row["ledger_strategy_version"], 255),
        "mapping_version": _bounded_text(row["ledger_mapping_version"], 255),
        "signal_available_at": (
            None
            if row["signal_available_at"] is None
            else int(row["signal_available_at"])
        ),
        "baseline_at": int(row["baseline_at"]),
        "observation_baseline_at": int(row["baseline_at"]),
        "tradable_baseline_at": tradability.get("tradable_baseline_at"),
        "tradable_baseline_price": tradability.get("tradable_baseline_price"),
        "measured_at": int(row["measured_at"]),
        "baseline_price": _finite_float(row["baseline_price"]),
        "gross_return": gross_return,
        "benchmark_return": benchmark_return,
        "excess_return": excess_return,
        "market_benchmark_return": benchmark_return,
        "market_excess_return": excess_return,
        "industry_benchmark_return": None,
        "industry_excess_return": None,
        "mfe": _finite_float(row["mfe"]),
        "mae": _finite_float(row["mae"]),
        "direction_hit": (
            None if row["direction_hit"] is None else bool(row["direction_hit"])
        ),
        "method_version": _bounded_text(row["method_version"], 255),
        "method_supported": method_supported,
        "mapping_current": mapping_current,
        "materialized": True,
        "data_version": _bounded_text(row["data_version"], 255),
        "data_quality": _finite_float(row["data_quality"]),
        "observation_state": _bounded_text(row["observation_state"], 32),
        "data_status": (
            "legacy_unknown"
            if not method_supported
            else "complete_close_only"
            if complete_close_result and row["observation_state"] == "complete"
            else _bounded_text(row["observation_state"], 32)
        ),
        "tradability_status": tradability.get("tradability_status", "unknown"),
        "suspension_status": tradability.get("suspension_status", "unknown"),
        "limit_status": tradability.get("limit_status", "unknown"),
        "revision": int(row["revision"]),
        "created_at": int(row["created_at"]),
        "sample_size": samples,
        "independent_trading_days": independent_days,
        "direction_hit_rate": successes / samples if samples else None,
        "confidence_level": 0.95,
        "confidence_interval_low": ci_low,
        "confidence_interval_high": ci_high,
        "sample_sufficient": samples >= 30 and independent_days >= 30,
        "merged_series_count": int(row["merged_series_count"]),
        "merged_series": int(row["merged_series_count"]) > 1,
        "security": {
            "security_id": str(row["security_id"]),
            "exchange": _bounded_text(row["exchange"], 32),
            "ticker": _bounded_text(row["ticker"], 32),
            "name": _bounded_text(row["name"], 255),
            "asset_type": _bounded_text(row["asset_type"], 64),
        },
        "mapping": {
            "relation_type": _bounded_text(row["relation_type"], 64),
            "direction": _bounded_text(row["mapping_direction"], 64),
            "rationale": _bounded_text(row["rationale"], 4000),
        },
        "benchmark": (
            None
            if benchmark_id is None
            else {
                "security_id": benchmark_id,
                "exchange": _bounded_text(row["benchmark_exchange"], 32),
                "ticker": _bounded_text(row["benchmark_ticker"], 32),
                "name": _bounded_text(row["benchmark_name"], 255),
                "is_market_standard": benchmark_id == MARKET_BENCHMARK_SYMBOL,
            }
        ),
    }


def _load_outcomes(
    conn: sqlite3.Connection, root_event_id: str, read_at: int
) -> tuple[list[dict[str, Any]], bool]:
    """Load all settled outcome results for an event group.

    Retrieves deduplicated outcome records (returns, directions, benchmark comparisons)
    for all securities mapped to the event group via recursive CTE traversal.

    Args:
        conn: Active SQLite connection with db_lock held.
        root_event_id: Canonical event UUID for the event group root.
        read_at: Frozen read timestamp for snapshot consistency.

    Returns:
        Tuple of (outcomes_list, has_more) where outcomes_list contains outcome dicts
        with returns/metadata and has_more indicates truncation at MAX_OUTCOMES.
    """
    logger.debug("_load_outcomes: root_event_id=%s", root_event_id)
    rows = conn.execute(
        _event_group_cte() + ", ranked AS ("
        "SELECT outcome.outcome_id, outcome.outcome_revision_id, outcome.event_id, "
        " outcome.security_id, outcome.mapping_id, outcome.mapping_revision, "
        " outcome.horizon, outcome.baseline_at, outcome.measured_at, "
        " outcome.baseline_price, outcome.gross_return, outcome.benchmark_return, "
        " outcome.excess_return, outcome.mfe, outcome.mae, outcome.direction_hit, "
        " outcome.method_version, outcome.data_version, outcome.data_quality, "
        " outcome.revision, outcome.created_at, outcome.signal_ledger_id, "
        " outcome.signal_available_at, outcome.observation_state, "
        " ledger.event_id AS ledger_event_id, "
        " ledger.event_mapping_id AS ledger_mapping_id, "
        " ledger.backtest_symbol AS ledger_security_id, "
        " ledger.available_at AS ledger_available_at, "
        " ledger.recorded_at AS ledger_recorded_at, ledger.selected AS ledger_selected, "
        " ledger.is_similarity_copy AS ledger_similarity_copy, "
        " ledger.score_path AS ledger_score_path, "
        " ledger.event_version AS ledger_event_version, ledger.direction AS ledger_direction, "
        " ledger.strategy_version AS ledger_strategy_version, "
        " ledger.mapping_version AS ledger_mapping_version, "
        " frozen.mapping_id AS frozen_mapping_id, "
        " frozen.mapping_revision AS frozen_mapping_revision, "
        " frozen.signal_available_at AS frozen_available_at, "
        " frozen.captured_at AS frozen_captured_at, "
        " security.exchange, security.ticker, security.name, security.asset_type, "
        " mapping.relation_type, mapping.direction AS mapping_direction, mapping.rationale, "
        " mapping.is_current AS mapping_is_current, "
        " mapping.created_at AS mapping_created_at, "
        " mapping.mapping_version AS mapping_version, "
        " benchmark.security_id AS benchmark_security_id, "
        " benchmark.exchange AS benchmark_exchange, benchmark.ticker AS benchmark_ticker, "
        " benchmark.name AS benchmark_name, "
        " COUNT(*) OVER (PARTITION BY outcome.security_id, outcome.horizon, "
        " outcome.method_version, outcome.benchmark_security_id, "
        " ledger.strategy_version, ledger.mapping_version, mapping.is_current) "
        " AS merged_series_count, "
        " ROW_NUMBER() OVER (PARTITION BY outcome.security_id, outcome.horizon, "
        " outcome.method_version, outcome.benchmark_security_id, "
        " ledger.strategy_version, ledger.mapping_version, mapping.is_current "
        " ORDER BY mapping.is_current DESC, outcome.measured_at DESC, "
        " outcome.data_quality DESC, CASE WHEN outcome.event_id = ? THEN 0 ELSE 1 END, "
        " outcome.created_at DESC, outcome.revision DESC, "
        " outcome.outcome_revision_id DESC) AS canonical_series_rank "
        " FROM event_outcomes AS outcome JOIN event_group "
        " ON event_group.event_id = outcome.event_id "
        " JOIN events AS outcome_event ON outcome_event.event_id = outcome.event_id "
        " JOIN securities AS security ON security.security_id = outcome.security_id "
        " JOIN event_securities AS mapping ON mapping.mapping_id = outcome.mapping_id "
        " AND mapping.event_id = outcome.event_id "
        " AND mapping.security_id = outcome.security_id "
        " AND mapping.revision = outcome.mapping_revision "
        " LEFT JOIN securities AS benchmark "
        " ON benchmark.security_id = outcome.benchmark_security_id "
        " LEFT JOIN backtest_signals AS ledger ON ledger.id = outcome.signal_ledger_id "
        " LEFT JOIN backtest_signal_securities AS frozen "
        " ON frozen.ledger_signal_id = outcome.signal_ledger_id "
        " AND frozen.mapping_id = outcome.mapping_id "
        " AND frozen.event_id = outcome.event_id "
        " AND frozen.security_id = outcome.security_id "
        " AND frozen.mapping_revision = outcome.mapping_revision "
        " WHERE outcome.is_current = 1 "
        " AND outcome.created_at <= ? AND outcome.measured_at <= ? "
        " AND (outcome.signal_available_at IS NULL "
        " OR outcome.signal_available_at <= ?) "
        " AND mapping.created_at <= ? AND outcome_event.available_at <= ? "
        " AND (ledger.recorded_at IS NULL OR ledger.recorded_at <= ?) "
        " AND (frozen.captured_at IS NULL OR frozen.captured_at <= ?)"
        ") SELECT * FROM ranked WHERE canonical_series_rank = 1 "
        " ORDER BY measured_at DESC, horizon ASC, security_id ASC, outcome_id ASC LIMIT ?",
        (
            root_event_id,
            root_event_id,
            read_at,
            read_at,
            read_at,
            read_at,
            read_at,
            read_at,
            read_at,
            MAX_OUTCOMES + 1,
        ),
    ).fetchall()
    truncated = len(rows) > MAX_OUTCOMES
    revision_ids: set[str] = set()
    for row in rows[:MAX_OUTCOMES]:
        revision_ids.update(_outcome_revision_ids(row["data_version"]))
    revisions: dict[str, dict[str, Any]] = {}
    if revision_ids:
        revisions = _load_price_revisions(conn, revision_ids)
    outcomes: list[dict[str, Any]] = []
    for row in rows[:MAX_OUTCOMES]:
        method_supported = _revision_contract_is_valid(row, revisions)
        tradability = _tradability_view(conn, row, revisions, read_at)
        mapping_current = bool(row["mapping_is_current"])
        samples, independent_days, successes = (
            _historical_outcome_stats(conn, row)
            if method_supported and mapping_current
            else (0, 0, 0)
        )
        ci_low, ci_high = _wilson_interval(successes, samples)
        outcomes.append(
            _materialized_outcome_view(
                row,
                tradability=tradability,
                method_supported=method_supported,
                mapping_current=mapping_current,
                samples=samples,
                independent_days=independent_days,
                successes=successes,
                ci_low=ci_low,
                ci_high=ci_high,
            )
        )
    actual_series = _load_actual_series_keys(conn, root_event_id, read_at)
    status_rows = _load_generated_observation_states(
        conn,
        root_event_id,
        read_at=read_at,
        excluded_series=actual_series,
    )
    combined = outcomes + status_rows
    combined.sort(
        key=lambda item: (
            bool(item.get("mapping_current")),
            int(item.get("signal_available_at") or 0),
            int(item.get("measured_at") or 0),
            str(item.get("security", {}).get("security_id") or ""),
            str(item.get("horizon") or ""),
        ),
        reverse=True,
    )
    return combined[:MAX_OUTCOMES], truncated or len(combined) > MAX_OUTCOMES


def _pending_outcome_view(
    mapping: dict[str, Any],
    *,
    horizon: str,
    baseline_at: int,
    measured_at: int,
    tradability: dict[str, Any],
    state: str,
    data_status: str,
    ticker: str,
    security_id: str,
) -> dict[str, Any]:
    """Build the stable API representation for an unmaterialized horizon."""
    signal_id = int(mapping["ledger_signal_id"])
    return {
        "outcome_id": (f"pending:{signal_id}:{mapping['frozen_mapping_id']}:{horizon}"),
        "outcome_revision_id": None,
        "event_id": str(mapping["mapping_event_id"]),
        "mapping_id": str(mapping["frozen_mapping_id"]),
        "mapping_revision": int(mapping["revision"]),
        "horizon": horizon,
        "signal_ledger_id": signal_id,
        "strategy_version": str(mapping["strategy_version"] or ""),
        "mapping_version": str(mapping["signal_mapping_version"] or ""),
        "signal_available_at": int(mapping["available_at"]),
        "baseline_at": baseline_at,
        "observation_baseline_at": baseline_at,
        "tradable_baseline_at": tradability.get("tradable_baseline_at"),
        "tradable_baseline_price": tradability.get("tradable_baseline_price"),
        "measured_at": measured_at,
        "baseline_price": None,
        "gross_return": None,
        "benchmark_return": None,
        "excess_return": None,
        "market_benchmark_return": None,
        "market_excess_return": None,
        "industry_benchmark_return": None,
        "industry_excess_return": None,
        "mfe": None,
        "mae": None,
        "direction_hit": None,
        "method_version": OUTCOME_METHOD_VERSION,
        "method_supported": True,
        "mapping_current": bool(mapping["mapping_is_current"]),
        "materialized": False,
        "data_version": "",
        "data_quality": None,
        "observation_state": state,
        "data_status": data_status,
        "tradability_status": tradability.get("tradability_status", "unknown"),
        "suspension_status": tradability.get("suspension_status", "unknown"),
        "limit_status": tradability.get("limit_status", "unknown"),
        "revision": 0,
        "created_at": 0,
        "sample_size": 0,
        "independent_trading_days": 0,
        "direction_hit_rate": None,
        "confidence_level": 0.95,
        "confidence_interval_low": None,
        "confidence_interval_high": None,
        "sample_sufficient": False,
        "merged_series_count": 1,
        "merged_series": False,
        "security": {
            "security_id": security_id,
            "exchange": str(mapping["exchange"]),
            "ticker": ticker,
            "name": str(mapping["name"]),
            "asset_type": str(mapping["asset_type"]),
        },
        "mapping": {
            "relation_type": str(mapping["relation_type"]),
            "direction": str(mapping["direction"]),
            "rationale": str(mapping["rationale"]),
        },
        "benchmark": {
            "security_id": MARKET_BENCHMARK_SYMBOL,
            "exchange": "SSE",
            "ticker": "000300",
            "name": "沪深300",
            "is_market_standard": True,
        },
    }


def _completed_price_pairs(
    conn: sqlite3.Connection, read_at: int, frozen_mappings: Any,
) -> set[tuple[str, str]]:
    """Read completed closes only for the selected event's frozen horizon pairs."""
    prefixes = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}
    dates_by_baseline: dict[str, set[str]] = {}
    requested: set[tuple[str, str]] = set()
    for mapping in frozen_mappings:
        prefix = prefixes.get(str(mapping["exchange"]).upper())
        ticker = str(mapping["ticker"] or "")
        if prefix is None or len(ticker) != 6 or not ticker.isdigit():
            continue
        baseline = baseline_trade_date(int(mapping["available_at"]))
        if baseline is None:
            continue
        if baseline not in dates_by_baseline:
            dates_by_baseline[baseline] = {baseline} | {
                day for offset in OUTCOME_HORIZON_DAYS
                if (day := horizon_trade_date(baseline, offset)) is not None
            }
        for symbol in (prefix + ticker, MARKET_BENCHMARK_SYMBOL):
            # Preserve the old case-insensitive lookup without lower(column),
            # which would prevent exact probes of the existing symbol/date index.
            variants = {
                left + right + symbol[2:]
                for left in (symbol[0].lower(), symbol[0].upper())
                for right in (symbol[1].lower(), symbol[1].upper())
            }
            requested.update(
                (variant, day) for variant in variants for day in dates_by_baseline[baseline]
            )
    completed: set[tuple[str, str]] = set()
    ordered = sorted(requested)
    for start in range(0, len(ordered), 400):
        batch = ordered[start:start + 400]
        placeholders = ",".join("(?, ?)" for _ in batch)
        rows = conn.execute(
            "WITH requested(symbol, trade_date) AS (VALUES " + placeholders + ") "
            "SELECT DISTINCT lower(price.symbol) AS symbol, price.trade_date, "
            "price.close_price, price.source_ts FROM requested "
            "CROSS JOIN future_price_revisions AS price "
            "ON price.symbol = requested.symbol AND price.trade_date = requested.trade_date "
            "WHERE price.recorded_at > 0 AND price.recorded_at <= ?",
            [value for pair in batch for value in pair] + [read_at],
        )
        completed.update(
            (str(row["symbol"]), str(row["trade_date"]))
            for row in rows
            if is_completed_close_price(
                row, str(row["trade_date"]), evaluation_at=read_at, timestamp_key="source_ts"
            )
        )
    return completed


def _load_generated_observation_states(
    conn: sqlite3.Connection,
    root_event_id: str,
    *,
    read_at: int,
    excluded_series: set[tuple[str, str, str, str, str, str, str, int, int]],
) -> list[dict[str, Any]]:
    """Project exact frozen-contract horizons without inventing returns.

    Generates forward-looking observation states for all eligible trading horizons
    based on the current outcome contract. Projects which outcomes would be observable
    at each future date without fabricating actual returns.

    Args:
        conn: Active SQLite connection with db_lock held.
        root_event_id: Canonical event UUID for the event group.
        read_at: Frozen read timestamp for snapshot consistency.
        excluded_series: Set of outcome series tuples to exclude from projection.

    Returns:
        List of observation state dictionaries with horizon, baseline_at, measured_at,
        and contract metadata. Limited to MAX_OUTCOMES * 20 rows.
    """
    logger.debug("_load_generated_observation_states: root_event_id=%s", root_event_id)
    read_limit = MAX_OUTCOMES * 20
    formal_paths = tuple(sorted(FORMAL_SIGNAL_SCORE_PATHS))
    formal_placeholders = ",".join("?" for _ in formal_paths)
    # Keep the event cohort outermost and use the frozen ledger primary key;
    # older SQLite can otherwise build a covering index over unrelated rows.
    frozen_rows = conn.execute(
        _event_group_cte() + ", effective AS (SELECT item.*, ROW_NUMBER() OVER ("
        "PARTITION BY item.link, item.strategy_version, item.mapping_version "
        "ORDER BY item.available_at DESC, item.id DESC"
        ") effective_rank FROM event_group CROSS JOIN backtest_signals AS item "
        "ON event_group.event_id = item.event_id "
        "CROSS JOIN events AS signal_event ON signal_event.event_id = item.event_id "
        "WHERE item.available_at <= ? AND item.recorded_at <= ? "
        "AND signal_event.available_at <= ?) "
        "SELECT signal.id AS ledger_signal_id, "
        "signal.event_id AS signal_event_id, signal.event_version, "
        "signal.available_at, signal.recorded_at, signal.strategy_version, "
        "signal.score_path, "
        "signal.mapping_version AS signal_mapping_version, "
        "frozen.mapping_id AS frozen_mapping_id, "
        "frozen.event_id AS frozen_event_id, "
        "frozen.security_id AS frozen_security_id, "
        "frozen.mapping_revision AS frozen_mapping_revision, "
        "frozen.signal_available_at AS frozen_available_at, "
        "frozen.captured_at, mapping.event_id AS mapping_event_id, "
        "mapping.security_id, mapping.revision, mapping.relation_type, "
        "mapping.direction, mapping.rationale, mapping.mapping_version, "
        "mapping.created_at AS mapping_created_at, mapping.is_current AS mapping_is_current, "
        "security.exchange, security.ticker, security.name, security.asset_type "
        "FROM effective AS signal "
        "CROSS JOIN backtest_signal_securities AS frozen INDEXED BY sqlite_autoindex_backtest_signal_securities_1 "
        "ON frozen.ledger_signal_id = signal.id "
        "CROSS JOIN event_securities AS mapping ON mapping.mapping_id = frozen.mapping_id "
        "AND mapping.event_id = frozen.event_id "
        "AND mapping.security_id = frozen.security_id "
        "AND mapping.revision = frozen.mapping_revision "
        "CROSS JOIN securities AS security ON security.security_id = frozen.security_id "
        "WHERE signal.effective_rank = 1 AND signal.selected = 1 "
        "AND signal.is_similarity_copy = 0 AND signal.event_version >= 1 "
        "AND LOWER(TRIM(COALESCE(signal.score_path, ''))) IN ("
        f"{formal_placeholders}) "
        "AND signal.available_at > 0 "
        "AND mapping.created_at <= ? AND frozen.captured_at <= ? "
        "AND lower(mapping.relation_type) <> 'benchmark_proxy' "
        "AND lower(security.asset_type) = 'equity' "
        "ORDER BY signal.available_at, signal.id, frozen.mapping_id LIMIT ?",
        (
            root_event_id,
            read_at,
            read_at,
            read_at,
            *formal_paths,
            read_at,
            read_at,
            read_limit + 1,
        ),
    ).fetchall()
    if len(frozen_rows) > read_limit:
        raise EventRadarUnavailableError(
            "frozen outcome status cohort exceeds the auditable read bound"
        )
    frozen_pairs: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
    for raw in frozen_rows:
        mapping = dict(raw)
        relation = normalize_mapping_relation(mapping.get("relation_type"))
        try:
            exact = (
                relation in {"direct", "upstream", "downstream", "ecosystem"}
                and normalize_score_path(mapping.get("score_path"))
                in FORMAL_SIGNAL_SCORE_PATHS
                and int(mapping["available_at"]) > 0
                and int(mapping["recorded_at"]) == int(mapping["captured_at"])
                and int(mapping["frozen_available_at"]) == int(mapping["available_at"])
                and int(mapping["frozen_mapping_revision"]) == int(mapping["revision"])
                and int(mapping["mapping_created_at"]) <= int(mapping["captured_at"])
                and str(mapping["signal_mapping_version"] or "")
                == str(mapping["mapping_version"] or "")
                and str(mapping["signal_event_id"]) == str(mapping["mapping_event_id"])
                and str(mapping["frozen_event_id"]) == str(mapping["mapping_event_id"])
                and str(mapping["frozen_security_id"]) == str(mapping["security_id"])
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            exact = False
        if not exact:
            continue
        pair_key = (
            str(mapping["strategy_version"] or ""),
            str(mapping["signal_mapping_version"] or ""),
            str(mapping["security_id"]).casefold(),
            str(mapping["frozen_mapping_id"]),
            int(mapping["frozen_mapping_revision"]),
        )
        previous = frozen_pairs.get(pair_key)
        if previous is None or (
            int(mapping["available_at"]),
            int(mapping["ledger_signal_id"]),
            str(mapping["frozen_mapping_id"]),
        ) < (
            int(previous["available_at"]),
            int(previous["ledger_signal_id"]),
            str(previous["frozen_mapping_id"]),
        ):
            frozen_pairs[pair_key] = mapping
    if not frozen_pairs:
        return []
    prefixes = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}
    prices = _completed_price_pairs(conn, read_at, frozen_pairs.values())
    result: list[dict[str, Any]] = []
    ordered_pairs = sorted(
        frozen_pairs.values(),
        key=lambda item: (
            int(item["available_at"]),
            int(item["ledger_signal_id"]),
            str(item["frozen_mapping_id"]),
        ),
    )
    for mapping in ordered_pairs:
        baseline_date = baseline_trade_date(int(mapping["available_at"]))
        if baseline_date is None:
            continue
        prefix = prefixes.get(str(mapping["exchange"]).upper())
        ticker = str(mapping["ticker"] or "")
        if prefix is None or len(ticker) != 6 or not ticker.isdigit():
            continue
        symbol = prefix + ticker
        security_id = str(mapping["security_id"])
        tradability = _tradability_for_symbol(
            conn,
            symbol=symbol,
            signal_available_at=int(mapping["available_at"]),
            read_at=read_at,
        )
        for offset in OUTCOME_HORIZON_DAYS:
            horizon = f"T+{offset}"
            key = (
                str(mapping["strategy_version"] or ""),
                str(mapping["signal_mapping_version"] or ""),
                security_id.casefold(),
                horizon,
                OUTCOME_METHOD_VERSION,
                MARKET_BENCHMARK_SYMBOL,
                str(mapping["frozen_mapping_id"]),
                int(mapping["frozen_mapping_revision"]),
                int(mapping["ledger_signal_id"]),
            )
            if key in excluded_series:
                continue
            measured_date = horizon_trade_date(baseline_date, offset)
            baseline_at = _market_close_timestamp(baseline_date)
            measured_at = (
                None
                if measured_date is None
                else _market_close_timestamp(measured_date)
            )
            if measured_date is None or baseline_at is None or measured_at is None:
                continue
            asset_complete = all(
                (symbol, date) in prices for date in (baseline_date, measured_date)
            )
            benchmark_complete = all(
                (MARKET_BENCHMARK_SYMBOL, date) in prices
                for date in (baseline_date, measured_date)
            )
            if measured_at > read_at:
                state = data_status = "not_due"
            elif not asset_complete:
                state = data_status = "missing_asset_close"
            elif not benchmark_complete:
                state = data_status = "missing_benchmark_close"
            else:
                state, data_status = "complete", "complete_pending_materialization"

            result.append(
                _pending_outcome_view(
                    mapping,
                    horizon=horizon,
                    baseline_at=baseline_at,
                    measured_at=measured_at,
                    tradability=tradability,
                    state=state,
                    data_status=data_status,
                    ticker=ticker,
                    security_id=security_id,
                )
            )
    return result


def _load_similar_events(
    conn: sqlite3.Connection,
    root_event_id: str,
    root_occurred_at: int,
    root_available_at: int,
    read_at: int,
) -> tuple[list[dict[str, Any]], bool]:
    logger.debug("event_radar similarities root_event_id=%s", root_event_id)
    rows = conn.execute(
        _event_group_cte() + ", left_edges AS ("
        " SELECT similarity.*, similarity.similar_event_id AS candidate_event_id, "
        " 'left' AS edge_side "
        " FROM event_similarities AS similarity JOIN event_group "
        " ON event_group.event_id = similarity.event_id "
        " JOIN events AS edge_event ON edge_event.event_id = similarity.event_id "
        " WHERE edge_event.available_at <= ? AND similarity.computed_at <= ? "
        " AND similarity.similarity > 0 "
        " ORDER BY similarity.similarity DESC, similarity.computed_at DESC LIMIT ?"
        "), right_edges AS ("
        " SELECT similarity.*, similarity.event_id AS candidate_event_id, "
        " 'right' AS edge_side "
        " FROM event_similarities AS similarity JOIN event_group "
        " ON event_group.event_id = similarity.similar_event_id "
        " JOIN events AS edge_event ON edge_event.event_id = similarity.similar_event_id "
        " WHERE edge_event.available_at <= ? AND similarity.computed_at <= ? "
        " AND similarity.similarity > 0 "
        " ORDER BY similarity.similarity DESC, similarity.computed_at DESC LIMIT ?"
        ") SELECT * FROM left_edges UNION ALL SELECT * FROM right_edges",
        (
            root_event_id,
            read_at,
            read_at,
            MAX_SIMILARITY_EDGES_PER_SIDE + 1,
            read_at,
            read_at,
            MAX_SIMILARITY_EDGES_PER_SIDE + 1,
        ),
    ).fetchall()

    canonical_cache: dict[str, str] = {}
    best_by_event: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_candidate = str(row["candidate_event_id"])
        if raw_candidate not in canonical_cache:
            try:
                canonical_cache[raw_candidate] = canonical_event_id(conn, raw_candidate)
            except (KeyError, ValueError, EventCoreConflictError) as exc:
                raise EventRadarUnavailableError(
                    "event similarity references an invalid event"
                ) from exc
        candidate = canonical_cache[raw_candidate]
        if candidate == root_event_id:
            # A once-valid edge can become internal to one canonical group
            # after a later audited merge.  It is not historical self-similarity.
            continue
        item = {
            "event_id": candidate,
            "similarity": float(row["similarity"]),
            "method": _bounded_text(row["method"], 64),
            "model_version": _bounded_text(row["model_version"], 255),
            "rationale": _bounded_text(row["rationale"], 4000),
            "common_securities": _bounded_json_strings(row["common_securities"]),
            "shared_paths": _bounded_json_strings(row["shared_paths"]),
            "computed_at": int(row["computed_at"]),
            "version": int(row["version"]),
        }
        previous = best_by_event.get(candidate)
        rank = (
            item["similarity"],
            item["computed_at"],
            item["version"],
            item["method"],
            item["model_version"],
        )
        previous_rank = (
            (
                previous["similarity"],
                previous["computed_at"],
                previous["version"],
                previous["method"],
                previous["model_version"],
            )
            if previous is not None
            else None
        )
        if previous_rank is None or rank > previous_rank:
            best_by_event[candidate] = item

    left_count = sum(1 for row in rows if row["edge_side"] == "left")
    right_count = len(rows) - left_count
    edge_truncated = (
        left_count > MAX_SIMILARITY_EDGES_PER_SIDE
        or right_count > MAX_SIMILARITY_EDGES_PER_SIDE
    )
    if best_by_event:
        ids = list(best_by_event)
        placeholders = ",".join("?" for _ in ids)
        metadata_rows = conn.execute(
            "SELECT event_id, title, event_type, direction, priority, occurred_at, "
            " version, first_seen_at, last_seen_at, available_at, merge_state "
            f"FROM events WHERE event_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        metadata = {str(row["event_id"]): row for row in metadata_rows}
        if len(metadata) != len(ids):
            raise EventRadarUnavailableError(
                "canonical similar event metadata is unavailable"
            )
        for item in best_by_event.values():
            event = metadata.get(item["event_id"])
            if event is None:
                raise EventRadarUnavailableError(
                    "canonical similar event metadata is unavailable"
                )
            if str(event["merge_state"]) != "active":
                raise EventRadarUnavailableError(
                    "canonical similar event metadata is unavailable"
                )
            # Canonicalization uses today's immutable merge graph.  A visible
            # historical child can now resolve to a root that was not yet
            # available at this transaction boundary.  Treat that candidate
            # as unavailable, rather than failing the entire optional tab.
            if int(event["available_at"]) > read_at:
                continue
            # The tab is explicitly historical.  A later event may be highly
            # similar, but showing it while inspecting an older event would be
            # look-ahead and contradict the page contract.
            if int(event["occurred_at"]) >= root_occurred_at:
                continue
            # Historical similarity must respect the selected event's own
            # knowledge boundary.  A backfilled event may have occurred
            # earlier while only becoming available after the selected event;
            # including it here would be an availability-time look-ahead.
            if int(event["available_at"]) > root_available_at:
                continue
            item["event"] = {
                "event_id": str(event["event_id"]),
                "title": _bounded_text(plain_text_from_markup(event["title"]), 1000),
                "event_type": _bounded_text(event["event_type"], 64),
                "direction": _bounded_text(event["direction"], 64),
                "priority": float(event["priority"]),
                "importance_score": float(event["priority"]),
                "occurred_at": int(event["occurred_at"]),
                "version": int(event["version"]),
                "first_seen_at": int(event["first_seen_at"]),
                "last_seen_at": int(event["last_seen_at"]),
                "available_at": int(event["available_at"]),
            }
    ordered = sorted(
        (item for item in best_by_event.values() if "event" in item),
        key=lambda item: (
            item["similarity"],
            item["computed_at"],
            item["event_id"],
        ),
        reverse=True,
    )
    truncated = edge_truncated or len(ordered) > MAX_SIMILAR_EVENTS
    selected = ordered[:MAX_SIMILAR_EVENTS]
    return selected, truncated


def _load_detail(
    conn: sqlite3.Connection,
    requested_event_id: str,
    tab: str,
    read_at: int,
) -> tuple[str, dict[str, Any]]:
    logger.debug(
        "event_radar detail requested_event_id=%s tab=%s", requested_event_id, tab
    )
    requested = conn.execute(
        "SELECT available_at FROM events WHERE event_id = ?",
        (requested_event_id,),
    ).fetchone()
    if requested is None or int(requested["available_at"]) > read_at:
        # An event that is not yet available is indistinguishable from an
        # unknown UUID at this read boundary.
        raise EventRadarNotFoundError("event was not found")
    try:
        root_event_id = canonical_event_id(conn, requested_event_id)
        group = canonical_event_group(conn, root_event_id)
    except KeyError as exc:
        raise EventRadarNotFoundError("event was not found") from exc
    except (ValueError, EventCoreConflictError) as exc:
        raise EventRadarUnavailableError("event merge graph is invalid") from exc
    if not _event_group_has_public_evidence(conn, root_event_id, read_at):
        raise EventRadarNotFoundError("event was not found")
    row = conn.execute(
        "SELECT event_id, title, event_type, direction, priority, occurred_at, "
        "version, merge_state, strategy_version, model_version, created_at, updated_at, "
        "first_seen_at, last_seen_at, available_at FROM events WHERE event_id = ?",
        (root_event_id,),
    ).fetchone()
    if row is None or str(row["merge_state"]) != "active":
        raise EventRadarUnavailableError("canonical event metadata is unavailable")
    if int(row["available_at"]) > read_at:
        raise EventRadarNotFoundError("event was not found")

    sources, source_count, sources_truncated, summary = _load_sources(
        conn, root_event_id, read_at
    )
    provenance_shadow = _read_provenance_shadow(conn, root_event_id)
    stocks = _load_stocks(conn, root_event_id, read_at)
    market_references = _load_market_references(conn, root_event_id, read_at)
    outcomes: list[dict[str, Any]] = []
    outcomes_truncated = False
    similar_events: list[dict[str, Any]] = []
    similarities_truncated = False
    if tab == "market":
        outcomes, outcomes_truncated = _load_outcomes(conn, root_event_id, read_at)
    elif tab == "similar":
        similar_events, similarities_truncated = _load_similar_events(
            conn,
            root_event_id,
            int(row["occurred_at"]),
            int(row["available_at"]),
            read_at,
        )

    event = {
        "event_id": str(row["event_id"]),
        "title": _bounded_text(plain_text_from_markup(row["title"]), 1000),
        "event_type": _bounded_text(row["event_type"], 64),
        "direction": _bounded_text(row["direction"], 64),
        "priority": float(row["priority"]),
        "importance_score": float(row["priority"]),
        "occurred_at": int(row["occurred_at"]),
        "version": int(row["version"]),
        "merge_state": str(row["merge_state"]),
        "strategy_version": _bounded_text(row["strategy_version"], 255),
        "model_version": _bounded_text(row["model_version"], 255),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "first_seen_at": int(row["first_seen_at"]),
        "last_seen_at": int(row["last_seen_at"]),
        "available_at": int(row["available_at"]),
    }
    return root_event_id, {
        "event": event,
        "requested_event_id": requested_event_id,
        "group_size": len(group),
        "summary": summary or event["title"],
        "source_count": source_count,
        "sources": sources,
        "sources_truncated": sources_truncated,
        "provenance_shadow": provenance_shadow,
        "stocks": stocks,
        "stock_count": len(stocks),
        "market_references": market_references,
        "mentions_unspecified_etf": "ETF" in str(row["title"] or "").upper(),
        "outcomes": outcomes,
        "outcomes_truncated": outcomes_truncated,
        "similar_events": similar_events,
        "similarities_truncated": similarities_truncated,
    }


def load_event_radar(
    event_id: object = None,
    cursor: object = None,
    limit: object = DEFAULT_PAGE_SIZE,
    tab: object = "overview",
    has_stocks: object = False,
) -> dict[str, Any]:
    """Load one bounded Event Radar page and optional selected detail.

    ``cursor`` is an opaque keyset cursor returned by the preceding call.
    Invalid query values raise :class:`EventRadarQueryError`; an unknown UUID
    raises :class:`EventRadarNotFoundError`.  The function never falls back to
    the retired headline/event-cache identity or performs a model call.
    """
    parsed_limit = _parse_limit(limit)
    parsed_cursor = _decode_cursor(cursor)
    parsed_tab = _parse_tab(tab)
    parsed_has_stocks = _parse_has_stocks(has_stocks)
    requested_event_id = _parse_uuid(event_id, optional=True)
    request_started_at = perf_counter()
    logger.info(
        "load_event_radar called: event_id=%s tab=%s limit=%s", event_id, tab, limit
    )

    try:
        with get_read_only_connection() as conn:
            conn.execute("BEGIN")
            try:
                read_at = _freeze_read_at()
                events, next_cursor, has_more = _load_event_page(
                    conn,
                    parsed_cursor,
                    parsed_limit,
                    parsed_has_stocks,
                    read_at,
                )
                selected_request = requested_event_id
                if not selected_request and events:
                    selected_request = events[0]["event_id"]
                selected_event_id = ""
                detail = None
                if selected_request:
                    selected_event_id, detail = _load_detail(
                        conn, selected_request, parsed_tab, read_at
                    )
                for item in events:
                    item["selected"] = item["event_id"] == selected_event_id
                result = {
                    "events": events,
                    "pagination": {
                        "limit": parsed_limit,
                        "cursor": "" if cursor is None else str(cursor).strip(),
                        "next_cursor": next_cursor,
                        "has_more": has_more,
                    },
                    "tab": parsed_tab,
                    "filters": {"has_stocks": parsed_has_stocks},
                    "selected_event_id": selected_event_id,
                    "detail": detail,
                    "read_at": read_at,
                }
            finally:
                if conn.in_transaction:
                    conn.rollback()
        _log_operation_duration(
            "load_event_radar", request_started_at, item_count=len(result["events"])
        )
        return result
    except (
        EventRadarQueryError,
        EventRadarNotFoundError,
        EventRadarUnavailableError,
    ):
        raise
    except sqlite3.Error as exc:
        logger.warning(
            "Event Radar SQLite read failed: %s - %s", type(exc).__name__, str(exc)
        )
        raise EventRadarUnavailableError("event radar data is unavailable") from exc


def load_event_stream(
    *,
    limit: object = 8,
    has_stocks: object = True,
    min_priority: object = None,
    require_non_concept_stock: bool = False,
    excluded_event_types: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Load only the bounded event stream for public summary surfaces.

    This deliberately omits selected-event detail, evidence, outcomes, and
    similarities.  Public cards therefore reuse the stable core and the exact
    real-equity filter without widening the authenticated Dashboard surface.
    ``min_priority`` drops low-importance events from space-constrained cards;
    the authenticated radar page never applies it.
    """
    request_started_at = perf_counter()
    logger.info("load_event_stream called: limit=%s has_stocks=%s", limit, has_stocks)
    parsed_limit = _parse_limit(limit)
    parsed_has_stocks = _parse_has_stocks(has_stocks)
    parsed_min_priority: float | None = None
    if min_priority is not None:
        parsed_min_priority = _finite_float(min_priority)
        if parsed_min_priority is None or not 0 <= parsed_min_priority <= 10:
            raise EventRadarQueryError("min_priority must be between 0 and 10")
    if not isinstance(require_non_concept_stock, bool):
        raise EventRadarQueryError("require_non_concept_stock must be boolean")
    if not isinstance(excluded_event_types, tuple) or len(excluded_event_types) > 10:
        raise EventRadarQueryError("excluded_event_types must be a bounded tuple")
    normalized_excluded_types: list[str] = []
    for value in excluded_event_types:
        text = str(value or "").strip()
        if not text or len(text) > 64:
            raise EventRadarQueryError("excluded event type is invalid")
        normalized_excluded_types.append(text)
    try:
        with db_lock:
            with get_connection() as conn:
                conn.execute("BEGIN")
                try:
                    read_at = _freeze_read_at()
                    events, next_cursor, has_more = _load_event_page(
                        conn,
                        None,
                        parsed_limit,
                        parsed_has_stocks,
                        read_at,
                        min_priority=parsed_min_priority,
                        require_non_concept_stock=require_non_concept_stock,
                        excluded_event_types=tuple(normalized_excluded_types),
                    )
                    result = {
                        "events": events,
                        "pagination": {
                            "limit": parsed_limit,
                            "next_cursor": next_cursor,
                            "has_more": has_more,
                        },
                        "filters": {"has_stocks": parsed_has_stocks},
                        "read_at": read_at,
                    }
                finally:
                    if conn.in_transaction:
                        conn.rollback()
        _log_operation_duration(
            "load_event_stream", request_started_at, item_count=len(result["events"])
        )
        return result
    except (EventRadarQueryError, EventRadarUnavailableError):
        raise
    except sqlite3.Error as exc:
        logger.warning("Event stream SQLite read failed: %s", type(exc).__name__)
        raise EventRadarUnavailableError("event stream data is unavailable") from exc


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "EventRadarNotFoundError",
    "EventRadarQueryError",
    "EventRadarUnavailableError",
    "load_event_stream",
    "load_event_radar",
]
