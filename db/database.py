import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from utils.beijing_time import beijing_now, utc_naive_fromtimestamp
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import quote, urlsplit, urlunsplit

from db.first_rejection_evidence import (
    init_first_rejection_storage,
    safe_bounded_int,
    purge_expired as purge_first_rejection_evidence,
)
from db.score_quality import init_score_quality_storage
from db.event_report_indexes import init_event_report_indexes
from db.request_logs import get_request_logs, log_request
from db.news_processing import (
    get_news_processing_trace,
    init_news_processing_storage,
    record_news_processing,
)
from db.integration_outbox import (
    configured_max_rows,
    configured_retention_days,
    enqueue_signal_revision,
    init_integration_signal_outbox,
    is_exportable_score_path,
    trim_integration_signal_outbox,
)
from db.score_explanation import (
    EXPLANATION_COLUMNS, SCHEMA_COLUMNS,
    bind_cache_score_explanation, bind_revised_score_explanation,
    check_cached_score_explanation_replay, check_score_explanation_replay,
    init_score_explanation_storage, prepare_signal_score_explanation,
    retain_unchanged_score_explanation,
    score_revision_payload, update_revised_cache_score, upsert_explained_cache_entry,
    write_cache_score_explanation, write_ledger_score_explanation,
)
from db.event_mapping_gate import (
    EventMappingMigrationGateError as EventMappingMigrationGateError,
    event_mapping_migration_gate_path,
    require_event_mapping_connection_write_authorized,
    require_event_mapping_write_authorized,
)
from scoring_provenance import (
    FORMAL_SIGNAL_SCORE_PATHS,
    NON_EXPORTABLE_SCORE_PATHS,
    NON_SUBSTANTIVE_SCORE_PATHS,
    PUBLIC_SCORE_PATHS,
    RECOGNIZED_SCORE_PATHS,
    SAFE_NEUTRAL_CONTRACT_VALUES,
    SAFE_NEUTRAL_SCORE_PATH_METADATA,
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL,
    is_fixed_safe_neutral_fields,
    is_recognized_score_path,
    normalize_score_path,
)
from db.event_core import (
    EVENT_CORE_BACKFILL_META_KEY,
    EVENT_CORE_SCHEMA_META_KEY,
    EVENT_CORE_SCHEMA_VERSION,
    EVENT_CORE_TABLE_NAMES,
    EventCoreConflictError,
    EventCoreSchemaError,
    canonical_event_group,
    canonical_event_id,
    count_unresolved_event_core_outbox_references,
    init_event_core_schema,
    normalize_evidence_text,
    reconcile_event_group_priorities,
    resolve_or_create_event,
    snapshot_digest,
    upsert_event_evidence,
    upsert_event_security,
    upsert_security,
    verify_event_core_backfill_state,
    verify_event_core_schema,
    verify_integration_outbox_schema,
)

logger = logging.getLogger(__name__)

# 支持通过环境变量覆盖数据库路径（解决硬编码问题）
# Docker 环境通常使用 /app/cache.db，本地开发时回退到项目根目录
_default_db_path = "/app/cache.db"
if not os.path.isdir("/app"):
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _default_db_path = os.path.join(_project_root, "data", "cache.db")
DB_PATH = os.environ.get("AI_FILTER_DB_PATH", _default_db_path)
db_lock = threading.Lock()
# One SQLite lock-wait budget for every shared connection helper.  Python's
# ``timeout=`` and ``PRAGMA busy_timeout`` set the same busy handler, so the
# later PRAGMA used to silently shrink the advertised 30 s to 5 s.  5 s is kept
# (no behaviour change): API request threads use these helpers, and a longer
# wait would hold a Gunicorn thread that long.  Background spool tasks treat a
# remaining ``database is locked`` as a bounded transient deferral instead.
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
_SQLITE_BUSY_TIMEOUT_PRAGMA = f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"


def _event_mapping_migration_gate_path() -> str:
    return event_mapping_migration_gate_path(DB_PATH)


def _require_event_mapping_write_authorized(
    mapping_version: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    if conn is None:
        require_event_mapping_write_authorized(mapping_version, db_path=DB_PATH)
        return
    require_event_mapping_connection_write_authorized(conn, mapping_version)


def _normalized_article_identity(link: str) -> tuple[str, str]:
    """Return a durable observation key without exposing the source URL.

    The digest is an external identity lookup key only.  The event's public,
    long-lived identity is the UUID stored in ``events.event_id``.
    """
    raw = str(link or "").strip()
    if not raw:
        raise ValueError("article link is required for stable event identity")
    normalized = raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
            port = parsed.port
            default_port = (parsed.scheme.lower() == "http" and port == 80) or (
                parsed.scheme.lower() == "https" and port == 443
            )
            netloc = hostname if not port or default_port else f"{hostname}:{port}"
            normalized = urlunsplit(
                (
                    parsed.scheme.lower(),
                    netloc,
                    parsed.path or "/",
                    parsed.query,
                    "",
                )
            )
    except (UnicodeError, ValueError):
        # Non-URL fixture or malformed legacy input: exact normalized text is
        # still deterministic and is never fetched by the event core.
        normalized = raw
    digest = hashlib.sha256(
        b"ai-filter-article-observation-v1\0" + normalized.encode("utf-8", errors="replace")
    ).hexdigest()
    return "article_url_sha256", digest


def _safe_evidence_url(link: str) -> bool:
    try:
        parsed = urlsplit(str(link or "").strip())
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _resolve_article_event(
    conn: sqlite3.Connection,
    *,
    link: str,
    title: str,
    event_type: str = "news",
    direction: str = "中性",
    priority: float = 0.0,
    occurred_at: Optional[int] = None,
    strategy_version: str = "",
    model_version: str = "",
    supplied_event_id: str = "",
    observed_at: Optional[int] = None,
    update_metadata: bool = True,
) -> Tuple[str, int]:
    """Resolve one article to a persisted canonical UUID inside ``conn``."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone() is None:
        init_event_core_schema(conn)
    identity = _normalized_article_identity(link)
    identities: List[Tuple[str, str]] = [identity]
    supplied = str(supplied_event_id or "").strip()
    linked = conn.execute(
        "SELECT event_id FROM event_identities WHERE identity_type = ? AND identity_key = ?",
        identity,
    ).fetchone()
    if linked is not None and not update_metadata:
        linked_id = canonical_event_id(conn, str(linked[0]))
        if supplied and canonical_event_id(conn, supplied) != linked_id:
            raise EventCoreConflictError(
                "article identity conflicts with supplied event_id"
            )
        version_row = conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (linked_id,)
        ).fetchone()
        if version_row is None:
            raise RuntimeError("resolved event disappeared before persistence")
        return linked_id, int(version_row[0])
    if supplied:
        supplied = canonical_event_id(conn, supplied)
        if linked is not None:
            linked_id = canonical_event_id(conn, str(linked[0]))
            if linked_id != supplied:
                raise EventCoreConflictError(
                    "article identity conflicts with supplied event_id"
                )
            version_row = conn.execute(
                "SELECT version FROM events WHERE event_id = ?", (linked_id,)
            ).fetchone()
            if version_row is None:
                raise RuntimeError("resolved event disappeared before persistence")
            return linked_id, int(version_row[0])
        known = conn.execute(
            "SELECT identity_type, identity_key FROM event_identities "
            "WHERE event_id = ? ORDER BY created_at, identity_type, identity_key LIMIT 1",
            (supplied,),
        ).fetchone()
        if known is None:
            raise EventCoreConflictError("supplied event has no durable identity")
        identities.append((str(known[0]), str(known[1])))
    now = int(observed_at or time.time())
    resolved_title = (str(title or "").strip() or str(link).strip())[:1000]
    resolved_type = (str(event_type or "news").strip() or "news")[:64]
    resolved_direction = (str(direction or "中性").strip() or "中性")[:64]
    resolved_priority = max(0.0, min(10.0, float(priority or 0.0)))
    resolved_occurred_at = int(occurred_at) if occurred_at is not None else now
    resolved_strategy = str(strategy_version or "")[:255]
    resolved_model = str(model_version or "")[:255]
    if supplied and not update_metadata:
        frozen = conn.execute(
            "SELECT title, event_type, direction, priority, occurred_at, "
            "strategy_version, model_version FROM events WHERE event_id = ?",
            (supplied,),
        ).fetchone()
        if frozen is None:
            raise KeyError(f"unknown event_id: {supplied}")
        (
            resolved_title,
            resolved_type,
            resolved_direction,
            resolved_priority,
            resolved_occurred_at,
            resolved_strategy,
            resolved_model,
        ) = tuple(frozen)
    resolved = resolve_or_create_event(
        conn,
        identities,
        title=resolved_title,
        event_type=resolved_type,
        direction=resolved_direction,
        priority=resolved_priority,
        occurred_at=resolved_occurred_at,
        available_at=now if linked is None and not supplied else None,
        strategy_version=resolved_strategy,
        model_version=resolved_model,
        now=now,
    )
    resolved = canonical_event_id(conn, resolved)
    if supplied and resolved != supplied:
        raise EventCoreConflictError("article identity conflicts with supplied event_id")
    row = conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (resolved,)
    ).fetchone()
    if row is None:
        raise RuntimeError("resolved event disappeared before persistence")
    return resolved, int(row[0])


def _current_canonical_event_identity(
    conn: sqlite3.Connection, event_id: str
) -> Tuple[str, int]:
    """Resolve an existing event identity without mutating event-core state."""
    canonical_id = canonical_event_id(conn, str(event_id or "").strip())
    row = conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (canonical_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("resolved event disappeared before persistence")
    return canonical_id, int(row[0])


def _record_article_evidence(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    link: str,
    raw_title: str,
    summary: str,
    feed_url: str,
    source_tier: str = "",
    announcement_at: Optional[int],
    observed_at: int,
) -> None:
    """Freeze a bounded source snapshot; never fetch a stored URL."""
    from core.source_contract import ARTICLE_CLAIM_PREFIX

    if not _safe_evidence_url(link):
        return
    # Hash exactly the canonical form that upsert_event_evidence verifies
    # (NFC + strip); hashing the raw title rejected e.g. U+F90A forever.
    headline = normalize_evidence_text(raw_title)[:4000].rstrip()
    if not headline:
        # Missing publisher text is not a verified source claim. In particular
        # neither a URL nor a generated summary may acquire the new identity.
        return
    # Model-generated summaries must not occupy the source excerpt/snapshot.
    snapshot = headline
    snapshot_hash = snapshot_digest(snapshot)
    # A changed source snapshot is a new evidence claim rather than an
    # in-place overwrite, so the earlier observation remains auditable.
    claim_key = f"{ARTICLE_CLAIM_PREFIX}{snapshot_hash}"
    if conn.execute(
        "SELECT 1 FROM event_evidence WHERE event_id = ? AND source_uri = ? "
        "AND claim_key = ?",
        (event_id, str(link).strip(), claim_key),
    ).fetchone() is not None:
        return
    # This is source/evidence confidence, not article importance and not the
    # event-to-security mapping confidence.  Keep the mapping deterministic so
    # ingestion remains available when no model is configured, while making
    # unknown tiers explicitly lower-confidence than audited primary sources.
    reliability_by_tier = {
        "official": 0.95,
        "primary_news": 0.85,
        "analysis": 0.70,
        "sentiment": 0.50,
        "noise_prone": 0.35,
    }
    source_confidence = reliability_by_tier.get(
        str(source_tier or "").strip().lower(), 0.50
    )
    upsert_event_evidence(
        conn,
        event_id,
        source_uri=str(link).strip(),
        source_type="article",
        source_name=str(feed_url or "")[:255],
        claim_key=claim_key,
        claim_text=headline,
        excerpt="",
        snapshot_hash=snapshot_hash,
        snapshot_text=snapshot,
        stance="supports",
        reliability=source_confidence,
        announcement_at=(
            int(announcement_at) if announcement_at is not None and int(announcement_at) >= 0 else None
        ),
        observed_at=int(observed_at),
        now=int(observed_at),
    )


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _add_column(cursor, table: str, column: str, definition: str) -> None:
    """安全添加列：先检查 schema，避免重复 ALTER 和吞掉无关错误。"""
    if _column_exists(cursor, table, column):
        return
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


def _event_core_startup_preflight(conn: sqlite3.Connection) -> bool:
    """Return whether a database without event data may receive initial markers.

    The check runs before any write-capable PRAGMA or schema helper.  Only a
    database without event-core/compatibility rows or a pre-existing Outbox
    table can use normal additive initialization.  Unrelated application
    tables keep ownership of their own legacy-schema migrations.
    """
    try:
        user_objects = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*'"
            ).fetchall()
        }
        user_tables = {
            name for name, object_type in user_objects.items()
            if object_type == "table"
        }
        marker = (
            conn.execute(
                "SELECT v FROM app_meta WHERE k = ?",
                (EVENT_CORE_BACKFILL_META_KEY,),
            ).fetchone()
            if "app_meta" in user_tables
            else None
        )
        schema_marker = (
            conn.execute(
                "SELECT v FROM app_meta WHERE k = ?",
                (EVENT_CORE_SCHEMA_META_KEY,),
            ).fetchone()
            if "app_meta" in user_tables
            else None
        )
    except sqlite3.DatabaseError as exc:
        raise EventCoreSchemaError(
            "event-core startup preflight cannot read migration state"
        ) from exc

    if "integration_signal_outbox" in user_tables:
        verify_integration_outbox_schema(conn)

    if marker is not None:
        # A recorded migration must already satisfy both the exact event schema
        # and compatibility boundary.  Checking both here keeps corrupt/partial
        # databases untouched by startup PRAGMAs or DDL.
        verify_event_core_schema(conn)
        verify_event_core_backfill_state(conn)
        return False

    event_related_names = set(EVENT_CORE_TABLE_NAMES).union(
        {
            "app_meta",
            "news_cache",
            "backtest_signals",
            "integration_signal_outbox",
        }
    )
    wrong_object_types = sorted(
        name
        for name in event_related_names.intersection(user_objects)
        if user_objects[name] != "table"
    )
    if wrong_object_types:
        raise EventCoreSchemaError(
            "event-core startup preflight found incompatible schema objects: "
            + ", ".join(wrong_object_types)
        )

    try:
        populated_compatibility_tables = [
            table
            for table in ("news_cache", "backtest_signals")
            if table in user_tables
            and conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
            is not None
        ]
        outbox_exists = "integration_signal_outbox" in user_tables
        unresolved_outbox = count_unresolved_event_core_outbox_references(conn)
        present_event_tables = set(EVENT_CORE_TABLE_NAMES).intersection(user_tables)
        populated_event_tables = [
            table
            for table in sorted(present_event_tables)
            if conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
            is not None
        ]
    except sqlite3.DatabaseError as exc:
        raise EventCoreSchemaError(
            "event-core startup preflight cannot inspect migration data"
        ) from exc

    if (
        schema_marker is not None
        or populated_compatibility_tables
        or outbox_exists
        or unresolved_outbox
        or populated_event_tables
    ):
        reasons = sorted(
            set(populated_compatibility_tables)
            .union(populated_event_tables)
            .union({"integration_signal_outbox"} if outbox_exists else set())
        )
        detail = ", ".join(reasons) if reasons else "event-core schema marker"
        raise EventCoreSchemaError(
            "event-core backfill marker is missing from a non-pristine database "
            f"with event-core state ({detail}); "
            "run scripts/migrate_event_core.py during maintenance"
        )

    if present_event_tables:
        # Empty pre-created event tables are safe only when the complete exact
        # schema is already present.  Partial or weakened schemas fail before
        # WAL/DDL instead of being silently completed by normal startup.
        try:
            verify_event_core_schema(conn)
        except EventCoreSchemaError as exc:
            raise EventCoreSchemaError(
                "event-core backfill marker is missing and the existing empty "
                "event-core schema is incomplete; run scripts/migrate_event_core.py "
                "during maintenance"
            ) from exc
    return True


def _reject_replacement_chars(*values) -> None:
    """Cache writes must not persist U+FFFD payloads."""
    if any("\ufffd" in str(value or "") for value in values):
        raise ValueError(
            "cached persistence payload contains a Unicode replacement character"
        )


def _normalize_cache_labels(error_type, safe_neutral_reason) -> tuple:
    """Normalize cache labels; only bounded neutral reasons persist."""
    from scoring_provenance import NEUTRAL_EVIDENCE_REASONS
    reason = str(safe_neutral_reason or "").strip()
    if reason not in NEUTRAL_EVIDENCE_REASONS:
        reason = ""
    return str(error_type or "").strip(), reason


def init_db() -> None:
    with db_lock:
        with get_connection(configure_storage=False) as conn:
            cursor = conn.cursor()
            # Inspect legacy completion before this initializer executes any
            # write-capable PRAGMA, DDL or data migration.
            initialize_event_core_markers = _event_core_startup_preflight(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SQLITE_BUSY_TIMEOUT_PRAGMA)
            conn.execute("PRAGMA synchronous=NORMAL")
            _init_app_meta(cursor)
            _init_news_cache(cursor)
            _init_article_sources(cursor)
            _run_migrations(conn, cursor)
            init_event_core_schema(conn)
            init_event_report_indexes(conn)
            repaired_event_priorities = reconcile_event_group_priorities(conn)
            if repaired_event_priorities:
                logger.warning(
                    "Reconciled %d canonical event priorities from audited merge groups",
                    repaired_event_priorities,
                )
            cursor.execute(
                "INSERT OR REPLACE INTO app_meta (k, v) "
                "VALUES ('event_mapping_replay_schema_version', '2')"
            )
            _init_api_stats(cursor)
            _init_score_distribution(cursor)
            _init_market_prices(cursor)
            _init_future_prices(cursor)
            _init_backtest_signals(cursor)
            init_score_explanation_storage(cursor)
            _init_backtest_signal_securities(cursor)
            _init_score_bridge(cursor)
            _init_event_reference_triggers(cursor)
            init_integration_signal_outbox(conn)
            _init_request_logs(cursor)
            _init_feed_health(cursor)
            _init_llm_call_logs(cursor)
            init_news_processing_storage(cursor)
            init_score_quality_storage(cursor)
            init_first_rejection_storage(cursor)
            _sync_feed_health_from_config(cursor)  # 启动时同步 config → feed_health
            _init_article_feedback(cursor)
            _init_feedback_example_snapshots(cursor)
            _init_scoring_contract_snapshots(cursor)
            _ensure_news_cache_indexes(conn)
            if initialize_event_core_markers:
                initialized_at = max(1, int(time.time()))
                cursor.execute(
                    "INSERT OR IGNORE INTO app_meta (k, v) VALUES (?, ?)",
                    (EVENT_CORE_SCHEMA_META_KEY, str(EVENT_CORE_SCHEMA_VERSION)),
                )
                cursor.execute(
                    "INSERT OR IGNORE INTO app_meta (k, v) VALUES (?, ?)",
                    (EVENT_CORE_BACKFILL_META_KEY, str(initialized_at)),
                )
            verify_event_core_backfill_state(conn)
            conn.commit()
            logger.info("Database initialized: %s", DB_PATH)


def startup_schema_migration_groups():
    """Fixed ordered news schema migrations, shared with controlled transactions."""
    return [
        # version 1: 添加 feed_url 字段
        [("feed_url", "TEXT")],
        # version 2: 评分 metadata 字段
        [
            ("score_path", "TEXT DEFAULT ''"),
            ("prompt_key", "TEXT DEFAULT ''"),
            ("model_name", "TEXT DEFAULT ''"),
            ("latency_ms", "INTEGER DEFAULT 0"),
            ("error_type", "TEXT DEFAULT ''"),
            ("source_tier", "TEXT DEFAULT ''"),
        ],
        # version 3: 评分策略版本，用于策略升级后自动绕过旧评分缓存
        [("strategy_version", "TEXT DEFAULT ''")],
        # version 4: 两阶段评分 metadata
        [
            ("stage1_score", "REAL"),
            ("stage1_model", "TEXT DEFAULT ''"),
            ("stage2_score", "REAL"),
            ("stage2_model", "TEXT DEFAULT ''"),
            ("score_confidence", "REAL DEFAULT 0"),
            ("review_reason", "TEXT DEFAULT ''"),
        ],
        # version 5: 独立方向字段，避免展示标题清洗后丢失评分情绪
        [("direction", "TEXT DEFAULT '中性'")],
        # version 6: 稳定事件核心引用；UUID 由 events 表持久化生成。
        [("event_id", "TEXT"), ("event_version", "INTEGER NOT NULL DEFAULT 0")],
        # version 7: nullable explanation snapshots; historical values stay unknown.
        SCHEMA_COLUMNS,
    ]


def _run_migrations(conn: sqlite3.Connection, cursor) -> None:
    """按语义版本执行 schema 迁移；每个版本可包含多条幂等步骤。"""
    try:
        cursor.execute("SELECT v FROM app_meta WHERE k = 'schema_version'")
        row = cursor.fetchone()
        stored_version = int(row[0]) if row else 0
    except Exception:
        stored_version = 0

    migration_groups = startup_schema_migration_groups()
    target_version = len(migration_groups)

    # 兼容旧实现：旧 schema_version 按每条 SQL 递增，最高可能为 7。
    # 如果实际列已齐全，将它归一为当前语义版本，避免未来 v3 被旧数字跳过。
    required_columns = [column for group in migration_groups for column, _ in group]
    all_columns_exist = all(_column_exists(cursor, "news_cache", column) for column in required_columns)
    if stored_version > target_version and all_columns_exist:
        cursor.execute(
            "INSERT OR REPLACE INTO app_meta (k, v) VALUES ('schema_version', ?)",
            (str(target_version),),
        )
        conn.commit()
        logger.info("[Migration] Normalized legacy schema_version: %d -> %d", stored_version, target_version)
        return

    current_version = max(0, min(stored_version, target_version))
    for index, group in enumerate(migration_groups[:current_version]):
        if any(not _column_exists(cursor, "news_cache", column) for column, _ in group):
            current_version = index
            break
    for version in range(current_version + 1, target_version + 1):
        for column, definition in migration_groups[version - 1]:
            _add_column(cursor, "news_cache", column, definition)
        cursor.execute(
            "INSERT OR REPLACE INTO app_meta (k, v) VALUES ('schema_version', ?)",
            (str(version),),
        )
        conn.commit()
        logger.info("[Migration] Applied schema v%d", version)

    if stored_version != current_version:
        logger.info("[Migration] Clamped schema_version for migration: %d -> %d", stored_version, current_version)


def _ensure_news_cache_indexes(conn: sqlite3.Connection) -> None:
    """加速按时间窗拉取、按分数排序（dashboard / cached_feed / 相似度窗口）。"""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cache_created_at "
        "ON news_cache(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cache_score_created "
        "ON news_cache(score DESC, created_at DESC)"
    )
    # P0 优化：feed_url 索引加速按源过滤（cached_feed / get_recent_entries）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cache_feed_url "
        "ON news_cache(feed_url, created_at DESC)"
    )
    # P0 优化：request_logs 时间索引加速日志查询
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp "
        "ON request_logs(timestamp DESC)"
    )
    # P0 优化：feed_health 最后检查时间索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feed_health_last_check "
        "ON feed_health(last_check DESC)"
    )
    # P1 优化：清理专用复合索引（created_at + score）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cache_cleanup "
        "ON news_cache(created_at, score)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_cache_event "
        "ON news_cache(event_id, created_at DESC)"
    )


@contextmanager
def get_connection(*, configure_storage: bool = True):
    """连接上下文管理器，确保自动关闭。显式启用 WAL + busy_timeout 以防御并发读写锁冲突。"""
    conn = sqlite3.connect(
        DB_PATH, check_same_thread=False, timeout=SQLITE_BUSY_TIMEOUT_SECONDS
    )
    conn.row_factory = sqlite3.Row
    # SQLite does not inherit this setting between connections.  Every API,
    # worker and maintenance connection must enforce the event-core graph.
    conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("SQLite foreign-key enforcement is unavailable")
    # SQLite REPLACE internally deletes the old row. DELETE triggers only run
    # for that path when recursive triggers are enabled, so this is required
    # for every table advertised as immutable.
    conn.execute("PRAGMA recursive_triggers=ON")
    if int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("SQLite recursive-trigger enforcement is unavailable")
    if configure_storage:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SQLITE_BUSY_TIMEOUT_PRAGMA)
        conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_read_only_connection():
    """Open an existing database without a write-capable SQLite handle."""
    encoded_path = quote(os.path.abspath(DB_PATH), safe="/")
    conn = sqlite3.connect(
        f"file:{encoded_path}?mode=ro",
        uri=True,
        check_same_thread=False,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("SQLite query-only enforcement is unavailable")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute(_SQLITE_BUSY_TIMEOUT_PRAGMA)
    try:
        yield conn
    finally:
        conn.close()


def _init_news_cache(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            link TEXT PRIMARY KEY, title TEXT NOT NULL, raw_title TEXT,
            score REAL, tag TEXT, logic TEXT, created_at INTEGER, summary TEXT,
            feed_url TEXT, score_path TEXT DEFAULT '', prompt_key TEXT DEFAULT '',
            model_name TEXT DEFAULT '', latency_ms INTEGER DEFAULT 0,
            error_type TEXT DEFAULT '', source_tier TEXT DEFAULT '',
            strategy_version TEXT DEFAULT '', stage1_score REAL,
            stage1_model TEXT DEFAULT '', stage2_score REAL,
            stage2_model TEXT DEFAULT '', score_confidence REAL DEFAULT 0,
            review_reason TEXT DEFAULT '', direction TEXT DEFAULT '中性',
            event_id TEXT, event_version INTEGER NOT NULL DEFAULT 0,
            safe_neutral_reason TEXT DEFAULT ''
        )
    """)
    columns = [
        ("raw_title", "TEXT"), ("score", "REAL"), ("tag", "TEXT"),
        ("logic", "TEXT"), ("created_at", "INTEGER"), ("summary", "TEXT"),
        ("feed_url", "TEXT"), ("score_path", "TEXT DEFAULT ''"),
        ("prompt_key", "TEXT DEFAULT ''"), ("model_name", "TEXT DEFAULT ''"),
        ("latency_ms", "INTEGER DEFAULT 0"), ("error_type", "TEXT DEFAULT ''"),
        ("source_tier", "TEXT DEFAULT ''"), ("strategy_version", "TEXT DEFAULT ''"),
        ("stage1_score", "REAL"), ("stage1_model", "TEXT DEFAULT ''"),
        ("stage2_score", "REAL"), ("stage2_model", "TEXT DEFAULT ''"),
        ("score_confidence", "REAL DEFAULT 0"), ("review_reason", "TEXT DEFAULT ''"),
        ("direction", "TEXT DEFAULT '中性'"),
        ("event_id", "TEXT"), ("event_version", "INTEGER NOT NULL DEFAULT 0"),
        ("safe_neutral_reason", "TEXT DEFAULT ''")
    ]
    for col_name, col_type in columns:
        _add_column(cursor, "news_cache", col_name, col_type)
    # 修\u590d\u65e7\u6570\u636e\u4e2d\u7684 NULL feed_url，\u907f\u514d\u67e5\u8be2\u5931\u8d25
    try:
        cursor.execute("UPDATE news_cache SET feed_url = '' WHERE feed_url IS NULL")
        cursor.execute("UPDATE news_cache SET direction = '中性' WHERE direction IS NULL OR direction = ''")
        # Backfill legacy rows from the scored title before presentation code
        # strips the model prefix. New writes always provide direction directly.
        cursor.execute(
            "UPDATE news_cache SET direction = '看多' "
            "WHERE direction = '中性' AND instr(title, '📈') > 0"
        )
        cursor.execute(
            "UPDATE news_cache SET direction = '看空' "
            "WHERE direction = '中性' AND instr(title, '📉') > 0"
        )
    except sqlite3.OperationalError:
        pass


def _init_article_sources(cursor) -> None:
    """Create the additive article/source relation and backfill legacy rows.

    ``news_cache.feed_url`` remains the compatibility/primary-source field.
    This table records every source that has observed the same canonical link,
    so a later cache write cannot erase an earlier source attribution.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS article_sources (
            link TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            PRIMARY KEY (link, feed_url)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_sources_feed_url_seen "
        "ON article_sources(feed_url, last_seen_at DESC)"
    )
    # Additive, idempotent migration: retain at least the currently selected
    # legacy source without changing or deleting any news_cache row.
    cursor.execute(
        "INSERT OR IGNORE INTO article_sources "
        "(link, feed_url, first_seen_at, last_seen_at) "
        "SELECT link, feed_url, "
        "CASE WHEN COALESCE(created_at, 0) > 0 THEN created_at ELSE CAST(strftime('%s', 'now') AS INTEGER) END, "
        "CASE WHEN COALESCE(created_at, 0) > 0 THEN created_at ELSE CAST(strftime('%s', 'now') AS INTEGER) END "
        "FROM news_cache WHERE TRIM(COALESCE(feed_url, '')) != ''"
    )


def _init_api_stats(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_requests INTEGER DEFAULT 0, api_calls_made INTEGER DEFAULT 0,
            cache_hits INTEGER DEFAULT 0, similarity_hits INTEGER DEFAULT 0,
            blacklist_hits INTEGER DEFAULT 0, fast_pass_hits INTEGER DEFAULT 0,
            filtered_count INTEGER DEFAULT 0, passed_count INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO api_stats (id, updated_at) VALUES (1, 0)")
    _add_column(cursor, "api_stats", "blacklist_hits", "INTEGER DEFAULT 0")
    _add_column(cursor, "api_stats", "fast_pass_hits", "INTEGER DEFAULT 0")


def _init_score_distribution(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS score_distribution (
            date TEXT PRIMARY KEY,
            s10 INTEGER DEFAULT 0, s9 INTEGER DEFAULT 0, s8 INTEGER DEFAULT 0,
            s7 INTEGER DEFAULT 0, s6 INTEGER DEFAULT 0, s5 INTEGER DEFAULT 0,
            s4 INTEGER DEFAULT 0, s3 INTEGER DEFAULT 0, s2 INTEGER DEFAULT 0,
            s1 INTEGER DEFAULT 0, s0 INTEGER DEFAULT 0
        )
    """)


def _init_market_prices(cursor) -> None:
    """Create the legacy latest-market snapshot table.

    ``market_prices`` is kept for compatibility with existing collectors.  New
    backtests should prefer the date-keyed ``future_prices`` table below.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_prices (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            prev_close REAL,
            change_amt REAL,
            change_pct REAL,
            ts INTEGER,
            trade_date TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_prices_trade_date "
        "ON market_prices(trade_date)"
    )


_PRICE_REVISION_NAMESPACE_V1 = uuid.UUID("f0e5b31c-2e95-4b52-88d5-17b42442db5d")
# V2 deliberately has a separate namespace: source timestamps were not part of
# the original UUID payload, so changing the V1 algorithm in place would make
# an existing immutable revision impossible to reproduce safely.
_PRICE_REVISION_NAMESPACE_V2 = uuid.UUID("7f6d8b87-afed-5d1a-add8-31afa5f01241")
_FUTURE_PRICE_POINTER_TRIGGER_MARKER = "future price pointer mirror v3"


_FUTURE_PRICE_POINTER_MIRROR_COLUMNS = (
    ("symbol", "symbol"),
    ("trade_date", "trade_date"),
    ("name", "name"),
    ("close_price", "close_price"),
    ("change_amt", "change_amt"),
    ("change_pct", "change_pct"),
    ("open_price", "open_price"),
    ("high_price", "high_price"),
    ("low_price", "low_price"),
    ("prev_close", "prev_close"),
    ("volume", "volume"),
    ("amount", "amount"),
    ("limit_up", "limit_up"),
    ("limit_down", "limit_down"),
    ("suspension_state", "suspension_state"),
    ("limit_state", "limit_state"),
    ("tradability_state", "tradability_state"),
    ("ts", "source_ts"),
    ("revision_id", "revision_id"),
)


def _future_price_revision_is_fresher_sql(
    candidate_alias: str,
    current_alias: str,
) -> str:
    """Return the shared total-order predicate for a current price pointer.

    Vendor/source time is authoritative.  ``recorded_at`` orders corrections
    carrying the same source timestamp, while the immutable revision UUID is a
    deterministic final tie-breaker when two distinct corrections arrive in
    the same process-clock second.  Reading an already persisted ledger row is
    important: replaying an old payload cannot acquire a new freshness rank.
    """
    return (
        f"(COALESCE({candidate_alias}.source_ts, 0) > "
        f"COALESCE({current_alias}.source_ts, 0) OR ("
        f"COALESCE({candidate_alias}.source_ts, 0) = "
        f"COALESCE({current_alias}.source_ts, 0) AND ("
        f"{candidate_alias}.recorded_at > {current_alias}.recorded_at OR ("
        f"{candidate_alias}.recorded_at = {current_alias}.recorded_at AND "
        f"{candidate_alias}.revision_id > {current_alias}.revision_id))))"
    )


def _future_price_revision_is_best_sql(
    candidate_alias: str,
    rival_alias: str,
) -> str:
    """Return whether ``candidate_alias`` is the total-order best for its key."""
    rival_is_fresher = _future_price_revision_is_fresher_sql(
        rival_alias,
        candidate_alias,
    )
    return (
        "NOT EXISTS (SELECT 1 FROM future_price_revisions AS "
        f"{rival_alias} WHERE {rival_alias}.symbol IS {candidate_alias}.symbol "
        f"AND {rival_alias}.trade_date IS {candidate_alias}.trade_date "
        f"AND {rival_is_fresher})"
    )


def _future_price_pointer_matches_revision_sql(
    pointer_alias: str,
    revision_alias: str,
) -> str:
    """Return a NULL-safe, complete pointer-to-ledger mirror predicate."""
    return " AND ".join(
        f"{pointer_alias}.{pointer_column} IS "
        f"{revision_alias}.{revision_column}"
        for pointer_column, revision_column in _FUTURE_PRICE_POINTER_MIRROR_COLUMNS
    )


def _normalize_future_price_source_ts(value: object) -> int | None:
    """Return the canonical source timestamp used by storage and identity."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("future price source timestamp must be an integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("future price source timestamp must be an integer") from exc
    if isinstance(value, float) and (not math.isfinite(value) or value != normalized):
        raise ValueError("future price source timestamp must be an integer")
    return normalized if normalized > 0 else None


def _future_price_revision_payload(
    symbol: object,
    trade_date: object,
    name: object,
    close_price: object,
    change_amt: object,
    change_pct: object,
    *,
    open_price: object = None,
    high_price: object = None,
    low_price: object = None,
    prev_close: object = None,
    volume: object = None,
    amount: object = None,
    limit_up: object = None,
    limit_down: object = None,
    suspension_state: object = "unknown",
    limit_state: object = "unknown",
    tradability_state: object = "unknown",
) -> dict[str, object]:
    return {
        "symbol": str(symbol or "").strip().casefold(),
        "trade_date": str(trade_date or "").strip(),
        "name": str(name or "").strip(),
        "close_price": format(float(close_price), ".17g"),
        "change_amt": None if change_amt is None else format(float(change_amt), ".17g"),
        "change_pct": None if change_pct is None else format(float(change_pct), ".17g"),
        "open_price": None if open_price is None else format(float(open_price), ".17g"),
        "high_price": None if high_price is None else format(float(high_price), ".17g"),
        "low_price": None if low_price is None else format(float(low_price), ".17g"),
        "prev_close": None if prev_close is None else format(float(prev_close), ".17g"),
        "volume": None if volume is None else format(float(volume), ".17g"),
        "amount": None if amount is None else format(float(amount), ".17g"),
        "limit_up": None if limit_up is None else format(float(limit_up), ".17g"),
        "limit_down": None if limit_down is None else format(float(limit_down), ".17g"),
        "suspension_state": str(suspension_state or "unknown"),
        "limit_state": str(limit_state or "unknown"),
        "tradability_state": str(tradability_state or "unknown"),
    }


def _revision_uuid(namespace: uuid.UUID, payload: dict[str, object]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(namespace, serialized))


def _legacy_future_price_revision_id(*args: object, **kwargs: object) -> str:
    """Reproduce a persisted V1 identity without changing its namespace."""
    return _revision_uuid(
        _PRICE_REVISION_NAMESPACE_V1,
        _future_price_revision_payload(*args, **kwargs),
    )


def _future_price_revision_id(
    symbol: object,
    trade_date: object,
    name: object,
    close_price: object,
    change_amt: object,
    change_pct: object,
    *,
    open_price: object = None,
    high_price: object = None,
    low_price: object = None,
    prev_close: object = None,
    volume: object = None,
    amount: object = None,
    limit_up: object = None,
    limit_down: object = None,
    suspension_state: object = "unknown",
    limit_state: object = "unknown",
    tradability_state: object = "unknown",
    source_ts: object = None,
) -> str:
    """Return a V2 UUID for one immutable, source-timestamped snapshot."""
    payload = _future_price_revision_payload(
        symbol,
        trade_date,
        name,
        close_price,
        change_amt,
        change_pct,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        prev_close=prev_close,
        volume=volume,
        amount=amount,
        limit_up=limit_up,
        limit_down=limit_down,
        suspension_state=suspension_state,
        limit_state=limit_state,
        tradability_state=tradability_state,
    )
    payload["identity_version"] = 2
    payload["source_ts"] = _normalize_future_price_source_ts(source_ts)
    return _revision_uuid(_PRICE_REVISION_NAMESPACE_V2, payload)


def _init_future_prices(cursor) -> None:
    """Create current closes plus an append-only revision ledger.

    ``future_prices`` remains the compatibility/current-value table.  Every
    value it points at is first frozen in ``future_price_revisions`` so an
    outcome revision remains reproducible after a vendor correction.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS future_price_revisions (
            revision_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            name TEXT,
            close_price REAL NOT NULL,
            change_amt REAL,
            change_pct REAL,
            open_price REAL,
            high_price REAL,
            low_price REAL,
            prev_close REAL,
            volume REAL,
            amount REAL,
            limit_up REAL,
            limit_down REAL,
            suspension_state TEXT NOT NULL DEFAULT 'unknown',
            limit_state TEXT NOT NULL DEFAULT 'unknown',
            tradability_state TEXT NOT NULL DEFAULT 'unknown',
            source_ts INTEGER,
            recorded_at INTEGER NOT NULL,
            UNIQUE (symbol, trade_date, revision_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS future_prices (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            name TEXT,
            close_price REAL NOT NULL,
            change_amt REAL,
            change_pct REAL,
            open_price REAL,
            high_price REAL,
            low_price REAL,
            prev_close REAL,
            volume REAL,
            amount REAL,
            limit_up REAL,
            limit_down REAL,
            suspension_state TEXT NOT NULL DEFAULT 'unknown',
            limit_state TEXT NOT NULL DEFAULT 'unknown',
            tradability_state TEXT NOT NULL DEFAULT 'unknown',
            ts INTEGER,
            revision_id TEXT,
            PRIMARY KEY (symbol, trade_date)
        )
    """)
    _add_column(cursor, "future_prices", "revision_id", "TEXT")
    for table in ("future_price_revisions", "future_prices"):
        for column, definition in (
            ("open_price", "REAL"),
            ("high_price", "REAL"),
            ("low_price", "REAL"),
            ("prev_close", "REAL"),
            ("volume", "REAL"),
            ("amount", "REAL"),
            ("limit_up", "REAL"),
            ("limit_down", "REAL"),
            ("suspension_state", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("limit_state", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("tradability_state", "TEXT NOT NULL DEFAULT 'unknown'"),
        ):
            _add_column(cursor, table, column, definition)
    # One-time compatibility backfill for close rows written before the
    # revision ledger existed.  The UUID is derived from the frozen values, so
    # a crash/retry cannot create a second identity for the same snapshot.
    legacy_rows = cursor.execute(
        "SELECT pointer.symbol, pointer.trade_date, pointer.name, "
        "pointer.close_price, pointer.change_amt, pointer.change_pct, "
        "pointer.open_price, pointer.high_price, pointer.low_price, "
        "pointer.prev_close, pointer.volume, pointer.amount, pointer.limit_up, "
        "pointer.limit_down, pointer.suspension_state, pointer.limit_state, "
        "pointer.tradability_state, pointer.ts "
        "FROM future_prices AS pointer "
        "WHERE COALESCE(pointer.revision_id, '') = '' AND NOT EXISTS ("
        "SELECT 1 FROM future_price_revisions AS revision "
        "WHERE revision.symbol = pointer.symbol "
        "AND revision.trade_date = pointer.trade_date)"
    ).fetchall()
    recorded_at = int(time.time())
    for row in legacy_rows:
        # Keep legacy V1 IDs reproducible for rows which predate the pointer.
        # New writes below use V2 and include source_ts in their identity.
        revision_fields = {
            "open_price": row[6],
            "high_price": row[7],
            "low_price": row[8],
            "prev_close": row[9],
            "volume": row[10],
            "amount": row[11],
            "limit_up": row[12],
            "limit_down": row[13],
            "suspension_state": row[14],
            "limit_state": row[15],
            "tradability_state": row[16],
        }
        revision_id = _legacy_future_price_revision_id(
            *row[:6],
            **revision_fields,
        )
        cursor.execute(
            "INSERT OR IGNORE INTO future_price_revisions "
            "(revision_id, symbol, trade_date, name, close_price, change_amt, "
            "change_pct, open_price, high_price, low_price, prev_close, volume, "
            "amount, limit_up, limit_down, suspension_state, limit_state, "
            "tradability_state, source_ts, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, *row, recorded_at),
        )
        cursor.execute(
            "UPDATE future_prices SET revision_id = ? "
            "WHERE symbol = ? AND trade_date = ? AND COALESCE(revision_id, '') = ''",
            (revision_id, row[0], row[1]),
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_future_prices_symbol_date "
        "ON future_prices(symbol, trade_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_future_price_revisions_symbol_date "
        "ON future_price_revisions(symbol, trade_date, recorded_at DESC)"
    )
    pointer_triggers = cursor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name IN ('trg_future_prices_revision_insert', "
        "'trg_future_prices_revision_update')"
    ).fetchall()
    trigger_sql = {
        str(row[0]): " ".join(str(row[1] or "").casefold().split())
        for row in pointer_triggers
    }
    required_trigger_names = {
        "trg_future_prices_revision_insert",
        "trg_future_prices_revision_update",
    }
    marker = _FUTURE_PRICE_POINTER_TRIGGER_MARKER.casefold()
    if set(trigger_sql) != required_trigger_names or any(
        marker not in trigger_sql[name] for name in required_trigger_names
    ):
        cursor.execute("DROP TRIGGER IF EXISTS trg_future_prices_revision_insert")
        cursor.execute("DROP TRIGGER IF EXISTS trg_future_prices_revision_update")
    pointer_matches_candidate = _future_price_pointer_matches_revision_sql(
        "NEW",
        "candidate",
    )
    candidate_is_best = _future_price_revision_is_best_sql("candidate", "rival")
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_future_prices_revision_insert "
        "BEFORE INSERT ON future_prices WHEN NOT EXISTS ("
        "SELECT 1 FROM future_price_revisions AS candidate WHERE "
        f"{pointer_matches_candidate} AND {candidate_is_best}) "
        "BEGIN SELECT RAISE(ABORT, '"
        f"{_FUTURE_PRICE_POINTER_TRIGGER_MARKER} violation'); END"
    )
    old_has_same_key_revision = (
        "EXISTS (SELECT 1 FROM future_price_revisions AS old_revision "
        "WHERE old_revision.revision_id IS OLD.revision_id "
        "AND old_revision.symbol IS OLD.symbol "
        "AND old_revision.trade_date IS OLD.trade_date)"
    )
    candidate_advances_old = (
        "EXISTS (SELECT 1 FROM future_price_revisions AS candidate "
        "JOIN future_price_revisions AS old_revision "
        "ON old_revision.revision_id IS OLD.revision_id "
        "WHERE candidate.revision_id IS NEW.revision_id "
        "AND candidate.symbol IS NEW.symbol "
        "AND candidate.trade_date IS NEW.trade_date "
        "AND old_revision.symbol IS OLD.symbol "
        "AND old_revision.trade_date IS OLD.trade_date AND "
        f"{_future_price_revision_is_fresher_sql('candidate', 'old_revision')})"
    )
    new_is_best = (
        "EXISTS (SELECT 1 FROM future_price_revisions AS candidate "
        "WHERE candidate.revision_id IS NEW.revision_id "
        "AND candidate.symbol IS NEW.symbol "
        "AND candidate.trade_date IS NEW.trade_date AND "
        f"{candidate_is_best})"
    )
    update_is_monotonic = (
        "(NEW.revision_id IS OLD.revision_id OR "
        f"({new_is_best} AND (NOT {old_has_same_key_revision} OR "
        f"{candidate_advances_old})))"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_future_prices_revision_update "
        "BEFORE UPDATE ON future_prices WHEN NEW.symbol IS NOT OLD.symbol "
        "OR NEW.trade_date IS NOT OLD.trade_date OR NOT EXISTS ("
        "SELECT 1 FROM future_price_revisions AS candidate WHERE "
        f"{pointer_matches_candidate}) OR NOT {update_is_monotonic} "
        "BEGIN SELECT RAISE(ABORT, '"
        f"{_FUTURE_PRICE_POINTER_TRIGGER_MARKER} violation'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_future_price_revisions_immutable_update "
        "BEFORE UPDATE ON future_price_revisions "
        "BEGIN SELECT RAISE(ABORT, 'future price revisions are immutable'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_future_price_revisions_immutable_delete "
        "BEFORE DELETE ON future_price_revisions "
        "BEGIN SELECT RAISE(ABORT, 'future price revisions are immutable'); END"
    )


def _init_backtest_signals(cursor) -> None:
    """Create the append-only signal ledger used by historical validation.

    This table deliberately has no foreign key to ``news_cache``: cache
    retention is seven days, while signal/outcome history must survive for the
    requested validation horizon.  ``signal_key`` makes repeated refreshes
    idempotent without rewriting the original point-in-time observation.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL,
            event_key TEXT DEFAULT '',
            link TEXT NOT NULL,
            feed_url TEXT DEFAULT '',
            available_at INTEGER NOT NULL,
            scored_at INTEGER NOT NULL,
            published_at INTEGER,
            recorded_at INTEGER NOT NULL,
            score REAL NOT NULL,
            direction TEXT NOT NULL DEFAULT '中性',
            score_confidence REAL DEFAULT 0,
            strategy_version TEXT DEFAULT '',
            mapping_version TEXT DEFAULT '',
            mapped_theme TEXT DEFAULT '',
            backtest_symbol TEXT,
            mapping_confidence REAL NOT NULL DEFAULT 0
                CHECK (typeof(mapping_confidence) IN ('integer', 'real') AND mapping_confidence >= 0 AND mapping_confidence <= 1),
            mapping_relevance REAL NOT NULL DEFAULT 0
                CHECK (typeof(mapping_relevance) IN ('integer', 'real') AND mapping_relevance >= 0 AND mapping_relevance <= 10),
            expected_gap REAL
                CHECK (expected_gap IS NULL OR (typeof(expected_gap) IN ('integer', 'real') AND expected_gap >= -100 AND expected_gap <= 100)),
            score_path TEXT DEFAULT '',
            revision_key TEXT NOT NULL DEFAULT 'base',
            signal_stage TEXT NOT NULL DEFAULT 'base',
            revision_seq INTEGER NOT NULL DEFAULT 0,
            revision_state_key TEXT NOT NULL DEFAULT '',
            independence_key TEXT NOT NULL DEFAULT '',
            event_id TEXT,
            event_version INTEGER NOT NULL DEFAULT 0,
            event_mapping_id TEXT,
            aggregation_threshold REAL NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
            is_similarity_copy INTEGER NOT NULL DEFAULT 0
                CHECK (is_similarity_copy IN (0, 1))
        )
    """)
    # Forward-compatible for databases created by the first signal-ledger
    # release candidate.  These additive migrations preserve existing base
    # snapshots and allow immutable post-score revisions such as Radar.
    _add_column(
        cursor,
        "backtest_signals",
        "revision_key",
        "TEXT NOT NULL DEFAULT 'base'",
    )
    _add_column(
        cursor,
        "backtest_signals",
        "signal_stage",
        "TEXT NOT NULL DEFAULT 'base'",
    )
    _add_column(
        cursor,
        "backtest_signals",
        "revision_seq",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        cursor,
        "backtest_signals",
        "revision_state_key",
        "TEXT NOT NULL DEFAULT ''",
    )
    _add_column(
        cursor,
        "backtest_signals",
        "independence_key",
        "TEXT NOT NULL DEFAULT ''",
    )
    _add_column(cursor, "backtest_signals", "event_id", "TEXT")
    _add_column(
        cursor,
        "backtest_signals",
        "event_version",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(cursor, "backtest_signals", "event_mapping_id", "TEXT")
    _add_column(
        cursor,
        "backtest_signals",
        "mapping_confidence",
        "REAL NOT NULL DEFAULT 0 CHECK (typeof(mapping_confidence) IN ('integer', 'real') AND mapping_confidence >= 0 AND mapping_confidence <= 1)",
    )
    _add_column(
        cursor,
        "backtest_signals",
        "mapping_relevance",
        "REAL NOT NULL DEFAULT 0 CHECK (typeof(mapping_relevance) IN ('integer', 'real') AND mapping_relevance >= 0 AND mapping_relevance <= 10)",
    )
    _add_column(
        cursor,
        "backtest_signals",
        "expected_gap",
        "REAL CHECK (expected_gap IS NULL OR (typeof(expected_gap) IN ('integer', 'real') AND expected_gap >= -100 AND expected_gap <= 100))",
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_signals_signal_key "
        "ON backtest_signals(signal_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_strategy_available "
        "ON backtest_signals(strategy_version, available_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_available "
        "ON backtest_signals(available_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_selected_available "
        "ON backtest_signals(selected, is_similarity_copy, available_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_symbol_available "
        "ON backtest_signals(backtest_symbol, available_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_link_revision "
        "ON backtest_signals(strategy_version, link, available_at DESC, id DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_independence "
        "ON backtest_signals(strategy_version, independence_key, available_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_event_available "
        "ON backtest_signals(event_id, available_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signals_mapping "
        "ON backtest_signals(event_mapping_id)"
    )
    cursor.execute(
        "INSERT OR IGNORE INTO app_meta (k, v) VALUES "
        "('backtest_signals_started_at', ?)",
        (str(int(time.time())),),
    )


def _init_backtest_signal_securities(cursor) -> None:
    """Create the append-only signal-to-equity mapping snapshot ledger.

    Earlier outcome releases froze price revisions but rediscovered an event's
    *current* equity mappings later.  That allowed a mapping produced after a
    signal to be paired with the old signal.  This table records the exact
    mapping revisions present in the accepted signal transaction.  Existing
    signals are deliberately not backfilled: absence means ``legacy_unfrozen``.
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_signal_securities (
            ledger_signal_id INTEGER NOT NULL REFERENCES backtest_signals(id)
                ON DELETE RESTRICT,
            mapping_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            security_id TEXT NOT NULL,
            mapping_revision INTEGER NOT NULL
                CHECK (typeof(mapping_revision) = 'integer' AND mapping_revision >= 1),
            signal_available_at INTEGER NOT NULL
                CHECK (typeof(signal_available_at) = 'integer' AND signal_available_at > 0),
            captured_at INTEGER NOT NULL
                CHECK (typeof(captured_at) = 'integer' AND captured_at > 0
                       AND captured_at <= signal_available_at),
            PRIMARY KEY (ledger_signal_id, mapping_id),
            FOREIGN KEY (mapping_id, event_id, security_id, mapping_revision)
                REFERENCES event_securities(mapping_id, event_id, security_id, revision)
                ON DELETE RESTRICT
        ) WITHOUT ROWID
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signal_securities_mapping "
        "ON backtest_signal_securities(mapping_id, mapping_revision)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_backtest_signal_securities_event "
        "ON backtest_signal_securities(event_id, security_id, ledger_signal_id)"
    )
    existing_insert_trigger = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'trg_backtest_signal_securities_insert'"
    ).fetchone()
    if existing_insert_trigger is not None:
        normalized_sql = " ".join(str(existing_insert_trigger[0] or "").casefold().split())
        if "signal.event_id = new.event_id" not in normalized_sql:
            # CREATE TRIGGER IF NOT EXISTS cannot upgrade the pre-event guard.
            # Replace it once, transactionally, while retaining the same name.
            cursor.execute("DROP TRIGGER trg_backtest_signal_securities_insert")
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_signal_securities_insert "
        "BEFORE INSERT ON backtest_signal_securities WHEN NOT EXISTS ("
        "SELECT 1 FROM backtest_signals AS signal WHERE signal.id = NEW.ledger_signal_id "
        "AND signal.event_id = NEW.event_id "
        "AND signal.available_at = NEW.signal_available_at "
        "AND signal.recorded_at = NEW.captured_at) "
        "BEGIN SELECT RAISE(ABORT, 'mapping snapshot does not match signal ledger'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_signal_securities_update "
        "BEFORE UPDATE ON backtest_signal_securities "
        "BEGIN SELECT RAISE(ABORT, 'signal mapping snapshots are immutable'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_signal_securities_delete "
        "BEFORE DELETE ON backtest_signal_securities "
        "BEGIN SELECT RAISE(ABORT, 'signal mapping snapshots are immutable'); END"
    )
    cursor.execute(
        "INSERT OR IGNORE INTO app_meta (k, v) VALUES "
        "('mapping_validation_started_at', ?)",
        (str(int(time.time())),),
    )


def _init_score_bridge(cursor) -> None:
    """Create the append-only, preview-only historical compatibility ledger."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS score_bridge_runs (
            run_id TEXT PRIMARY KEY,
            source_strategy_version TEXT NOT NULL CHECK (TRIM(source_strategy_version) <> ''),
            target_strategy_version TEXT NOT NULL CHECK (TRIM(target_strategy_version) <> ''),
            source_mapping_version TEXT NOT NULL CHECK (TRIM(source_mapping_version) <> ''),
            target_mapping_version TEXT NOT NULL CHECK (TRIM(target_mapping_version) <> ''),
            policy_version TEXT NOT NULL CHECK (TRIM(policy_version) <> ''),
            mode TEXT NOT NULL CHECK (mode = 'preview_only'),
            approval_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (approval_status IN ('pending', 'approved', 'rejected')),
            approved_at INTEGER,
            approved_by TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL CHECK (created_at > 0),
            artifact_name TEXT NOT NULL CHECK (TRIM(artifact_name) <> ''),
            artifact_sha256 TEXT NOT NULL CHECK (LENGTH(artifact_sha256) = 64),
            artifact_schema_version TEXT NOT NULL
                CHECK (TRIM(artifact_schema_version) <> ''),
            source_row_count INTEGER NOT NULL CHECK (source_row_count >= 0),
            eligible_count INTEGER NOT NULL CHECK (eligible_count >= 0),
            ineligible_count INTEGER NOT NULL CHECK (ineligible_count >= 0),
            unknown_count INTEGER NOT NULL CHECK (unknown_count >= 0),
            manifest_hash TEXT NOT NULL CHECK (LENGTH(manifest_hash) = 64),
            CHECK (source_row_count = eligible_count + ineligible_count + unknown_count),
            CHECK ((approval_status = 'pending' AND approved_at IS NULL AND approved_by = '')
                   OR (approval_status IN ('approved', 'rejected')
                       AND approved_at > 0 AND TRIM(approved_by) <> '')),
            UNIQUE (source_strategy_version, target_strategy_version,
                    source_mapping_version, target_mapping_version,
                    policy_version, artifact_sha256, manifest_hash)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS score_bridge_members (
            run_id TEXT NOT NULL REFERENCES score_bridge_runs(run_id)
                ON DELETE RESTRICT,
            ledger_signal_id INTEGER NOT NULL REFERENCES backtest_signals(id)
                ON DELETE RESTRICT,
            source_signal_key TEXT NOT NULL CHECK (TRIM(source_signal_key) <> ''),
            source_available_at INTEGER NOT NULL CHECK (source_available_at > 0),
            selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
            eligibility TEXT NOT NULL
                CHECK (eligibility IN ('eligible', 'ineligible', 'unknown')),
            reason_code TEXT NOT NULL CHECK (TRIM(reason_code) <> ''),
            stage_metadata_json TEXT NOT NULL CHECK (TRIM(stage_metadata_json) <> ''),
            output_hash TEXT NOT NULL CHECK (LENGTH(output_hash) = 64),
            evidence_hash TEXT NOT NULL CHECK (LENGTH(evidence_hash) = 64),
            PRIMARY KEY (run_id, ledger_signal_id),
            UNIQUE (run_id, source_signal_key)
        ) WITHOUT ROWID
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_score_bridge_runs_target "
        "ON score_bridge_runs(target_strategy_version, target_mapping_version, "
        "approval_status, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_score_bridge_members_status "
        "ON score_bridge_members(run_id, eligibility, selected)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_score_bridge_members_signal "
        "ON score_bridge_members(ledger_signal_id)"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_score_bridge_members_insert "
        "BEFORE INSERT ON score_bridge_members WHEN NOT EXISTS ("
        "SELECT 1 FROM score_bridge_runs AS run "
        "JOIN backtest_signals AS signal ON signal.id = NEW.ledger_signal_id "
        "WHERE run.run_id = NEW.run_id "
        "AND signal.signal_key = NEW.source_signal_key "
        "AND signal.strategy_version = run.source_strategy_version "
        "AND signal.mapping_version = run.source_mapping_version "
        "AND signal.available_at = NEW.source_available_at "
        "AND signal.selected = NEW.selected "
        "AND signal.is_similarity_copy = 0) "
        "BEGIN SELECT RAISE(ABORT, 'bridge member does not match source ledger'); END"
    )
    for table in ("score_bridge_runs", "score_bridge_members"):
        cursor.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_update "
            f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is immutable'); END"
        )
        cursor.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_delete "
            f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, "
            f"'{table} is immutable'); END"
        )


def store_score_bridge_preview(
    run: Dict[str, Any], members: List[Dict[str, Any]]
) -> bool:
    """Atomically append one pending preview manifest without touching signals."""
    counts = {status: 0 for status in ("eligible", "ineligible", "unknown")}
    for member in members:
        status = str(member.get("eligibility") or "")
        if status not in counts:
            raise ValueError("invalid bridge member eligibility")
        counts[status] += 1
    expected = (
        int(run.get("source_row_count") or 0),
        int(run.get("eligible_count") or 0),
        int(run.get("ineligible_count") or 0),
        int(run.get("unknown_count") or 0),
    )
    actual = (len(members), counts["eligible"], counts["ineligible"], counts["unknown"])
    if expected != actual:
        raise ValueError("bridge run counts do not match members")
    if str(run.get("mode") or "") != "preview_only" or str(
        run.get("approval_status") or "pending"
    ) != "pending":
        raise ValueError("bridge writer accepts pending previews only")

    run_columns = (
        "run_id", "source_strategy_version", "target_strategy_version",
        "source_mapping_version", "target_mapping_version", "policy_version",
        "mode", "approval_status", "approved_at", "approved_by", "created_at",
        "artifact_name", "artifact_sha256", "artifact_schema_version",
        "source_row_count", "eligible_count", "ineligible_count", "unknown_count",
        "manifest_hash",
    )
    member_columns = (
        "run_id", "ledger_signal_id", "source_signal_key", "source_available_at",
        "selected", "eligibility", "reason_code", "stage_metadata_json",
        "output_hash", "evidence_hash",
    )
    run_values = tuple(run.get(column) for column in run_columns)
    with db_lock:
        with get_connection() as conn:
            _init_score_bridge(conn.cursor())
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT " + ", ".join(run_columns) + " FROM score_bridge_runs "
                "WHERE run_id = ?",
                (str(run.get("run_id") or ""),),
            ).fetchone()
            if existing is not None:
                existing_values = tuple(existing[column] for column in run_columns)
                stable_positions = [
                    index for index, column in enumerate(run_columns)
                    if column != "created_at"
                ]
                if tuple(existing_values[index] for index in stable_positions) != tuple(
                    run_values[index] for index in stable_positions
                ):
                    raise EventCoreConflictError("bridge run identity conflicts")
                stored_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM score_bridge_members WHERE run_id = ?",
                        (str(run["run_id"]),),
                    ).fetchone()[0]
                )
                if stored_count != len(members):
                    raise EventCoreConflictError("bridge run manifest is incomplete")
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO score_bridge_runs (" + ", ".join(run_columns) + ") "
                "VALUES (" + ", ".join("?" for _ in run_columns) + ")",
                run_values,
            )
            conn.executemany(
                "INSERT INTO score_bridge_members ("
                + ", ".join(member_columns)
                + ") VALUES ("
                + ", ".join("?" for _ in member_columns)
                + ")",
                [tuple(member.get(column) for column in member_columns) for member in members],
            )
            conn.commit()
            return True


def _freeze_backtest_signal_securities(
    conn: sqlite3.Connection,
    *,
    ledger_signal_id: int,
    mapping_ids: List[str],
    signal_available_at: int,
    captured_at: int,
) -> int:
    """Bind exact immutable mapping revisions to one accepted ledger row."""
    normalized = sorted({str(value).strip() for value in mapping_ids if str(value).strip()})
    for mapping_id in normalized:
        row = conn.execute(
            "SELECT mapping_id, event_id, security_id, revision, created_at "
            "FROM event_securities WHERE mapping_id = ?",
            (mapping_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("cannot freeze an unknown event-security mapping")
        if int(row[4]) > int(captured_at):
            raise RuntimeError("cannot freeze a mapping created after signal capture")
        conn.execute(
            "INSERT INTO backtest_signal_securities ("
            "ledger_signal_id, mapping_id, event_id, security_id, mapping_revision, "
            "signal_available_at, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(ledger_signal_id),
                str(row[0]),
                str(row[1]),
                str(row[2]),
                int(row[3]),
                int(signal_available_at),
                int(captured_at),
            ),
        )
    return len(normalized)


def _init_event_reference_triggers(cursor) -> None:
    """Enforce additive event references that SQLite ALTER cannot FK-upgrade."""
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_insert "
        "BEFORE INSERT ON news_cache WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown news_cache event_id'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_update "
        "BEFORE UPDATE OF event_id ON news_cache WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown news_cache event_id'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_insert "
        "BEFORE INSERT ON backtest_signals WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown backtest event_id'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_update "
        "BEFORE UPDATE OF event_id ON backtest_signals WHEN NEW.event_id IS NOT NULL "
        "AND TRIM(NEW.event_id) <> '' AND NOT EXISTS "
        "(SELECT 1 FROM events WHERE event_id = NEW.event_id) "
        "BEGIN SELECT RAISE(ABORT, 'unknown backtest event_id'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_version_insert "
        "BEFORE INSERT ON news_cache WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NOT EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id "
        "AND NEW.event_version <= version)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid news_cache event version'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_news_cache_event_version_update "
        "BEFORE UPDATE OF event_id, event_version ON news_cache "
        "WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NOT EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id "
        "AND NEW.event_version <= version)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid news_cache event version'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_version_insert "
        "BEFORE INSERT ON backtest_signals WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NOT EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id "
        "AND NEW.event_version <= version)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event version'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_event_version_update "
        "BEFORE UPDATE OF event_id, event_version ON backtest_signals "
        "WHEN COALESCE(TRIM(NEW.event_id), '') <> '' "
        "AND (typeof(NEW.event_version) <> 'integer' OR NEW.event_version < 1 "
        "OR NOT EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id "
        "AND NEW.event_version <= version)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event version'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_mapping_insert "
        "BEFORE INSERT ON backtest_signals "
        "WHEN COALESCE(TRIM(NEW.event_mapping_id), '') <> '' "
        "AND NOT EXISTS (SELECT 1 FROM event_securities AS mapping "
        "WHERE mapping.mapping_id = NEW.event_mapping_id "
        "AND mapping.event_id = NEW.event_id "
        "AND lower(mapping.security_id) = lower(COALESCE(NEW.backtest_symbol, ''))) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event mapping'); END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_backtest_mapping_update "
        "BEFORE UPDATE OF event_id, event_mapping_id, backtest_symbol ON backtest_signals "
        "WHEN COALESCE(TRIM(NEW.event_mapping_id), '') <> '' "
        "AND NOT EXISTS (SELECT 1 FROM event_securities AS mapping "
        "WHERE mapping.mapping_id = NEW.event_mapping_id "
        "AND mapping.event_id = NEW.event_id "
        "AND lower(mapping.security_id) = lower(COALESCE(NEW.backtest_symbol, ''))) "
        "BEGIN SELECT RAISE(ABORT, 'invalid backtest event mapping'); END"
    )


CHINA_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
}
CHINA_HOLIDAYS_BY_YEAR = {2026: CHINA_HOLIDAYS_2026}


def _is_trading_day_local(date_str: str) -> bool:
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    holidays = CHINA_HOLIDAYS_BY_YEAR.get(day.year)
    if holidays is None:
        raise ValueError(
            f"A股交易日历尚未配置 {day.year} 年，拒绝按工作日猜测"
        )
    return day.weekday() < 5 and date_str not in holidays


def _latest_trading_day(before: Optional[datetime] = None) -> str:
    day = (before or beijing_now()).date()
    for _ in range(10):
        if _is_trading_day_local(day.isoformat()):
            return day.isoformat()
        day -= timedelta(days=1)
    return day.isoformat()


def store_future_prices(prices: List[Dict[str, Any]]) -> None:
    """Freeze source snapshots, then monotonically advance the current close."""
    with db_lock:
        with get_connection() as conn:
            cursor = conn.cursor()
            _init_future_prices(cursor)
            candidate_advances_current = _future_price_revision_is_fresher_sql(
                "candidate",
                "current",
            )
            candidate_is_best = _future_price_revision_is_best_sql(
                "candidate",
                "rival",
            )
            for price in prices:
                recorded_at = int(time.time())
                source_ts = _normalize_future_price_source_ts(
                    price.get("ts", recorded_at)
                )
                revision_args = (
                    price["symbol"],
                    price["trade_date"],
                    price.get("name", ""),
                    price["close_price"],
                    price.get("change_amt"),
                    price.get("change_pct"),
                )
                revision_fields = {
                    "open_price": price.get("open_price"),
                    "high_price": price.get("high_price"),
                    "low_price": price.get("low_price"),
                    "prev_close": price.get("prev_close"),
                    "volume": price.get("volume"),
                    "amount": price.get("amount"),
                    "limit_up": price.get("limit_up"),
                    "limit_down": price.get("limit_down"),
                    "suspension_state": price.get("suspension_state"),
                    "limit_state": price.get("limit_state"),
                    "tradability_state": price.get("tradability_state"),
                }
                legacy_revision_id = _legacy_future_price_revision_id(
                    *revision_args,
                    **revision_fields,
                )
                legacy_row = cursor.execute(
                    "SELECT source_ts FROM future_price_revisions WHERE revision_id = ?",
                    (legacy_revision_id,),
                ).fetchone()
                try:
                    legacy_source_ts = (
                        _normalize_future_price_source_ts(legacy_row[0])
                        if legacy_row is not None
                        else object()
                    )
                except ValueError:
                    legacy_source_ts = object()
                # Reuse an exact persisted V1 snapshot so deploying V2 does not
                # rotate current pointers or outcome inputs merely due to the
                # identity upgrade.  All genuinely new snapshots use V2.
                revision_id = (
                    legacy_revision_id
                    if legacy_source_ts == source_ts
                    else _future_price_revision_id(
                        *revision_args,
                        **revision_fields,
                        source_ts=source_ts,
                    )
                )
                cursor.execute(
                    "INSERT OR IGNORE INTO future_price_revisions "
                    "(revision_id, symbol, trade_date, name, close_price, change_amt, "
                    "change_pct, open_price, high_price, low_price, prev_close, volume, "
                    "amount, limit_up, limit_down, suspension_state, limit_state, "
                    "tradability_state, source_ts, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        price["symbol"],
                        price["trade_date"],
                        price.get("name", ""),
                        price["close_price"],
                        price.get("change_amt"),
                        price.get("change_pct"),
                        price.get("open_price"),
                        price.get("high_price"),
                        price.get("low_price"),
                        price.get("prev_close"),
                        price.get("volume"),
                        price.get("amount"),
                        price.get("limit_up"),
                        price.get("limit_down"),
                        str(price.get("suspension_state") or "unknown"),
                        str(price.get("limit_state") or "unknown"),
                        str(price.get("tradability_state") or "unknown"),
                        source_ts,
                        recorded_at,
                    ),
                )
                pointer_values = (
                    price.get("name", ""), price["close_price"],
                    price.get("change_amt"), price.get("change_pct"),
                    price.get("open_price"), price.get("high_price"),
                    price.get("low_price"), price.get("prev_close"),
                    price.get("volume"), price.get("amount"),
                    price.get("limit_up"), price.get("limit_down"),
                    str(price.get("suspension_state") or "unknown"),
                    str(price.get("limit_state") or "unknown"),
                    str(price.get("tradability_state") or "unknown"),
                    source_ts, revision_id,
                )
                # Do not use INSERT .. ON CONFLICT here. SQLite evaluates a
                # BEFORE INSERT trigger before it knows that the UPSERT's
                # conflict branch will be a no-op, so replaying an older ledger
                # revision would be rejected by the global-best insert guard.
                # Updating first lets the monotonic UPDATE trigger validate the
                # actual transition. A missing compatibility row is inserted
                # only when this immutable revision is the global best.
                cursor.execute(
                    f"""
                    UPDATE future_prices SET
                        name=?, close_price=?, change_amt=?, change_pct=?,
                        open_price=?, high_price=?, low_price=?, prev_close=?,
                        volume=?, amount=?, limit_up=?, limit_down=?,
                        suspension_state=?, limit_state=?, tradability_state=?,
                        ts=?, revision_id=?
                    WHERE symbol IS ? AND trade_date IS ? AND EXISTS (
                        SELECT 1
                        FROM future_price_revisions AS candidate
                        WHERE candidate.revision_id IS ?
                          AND candidate.symbol IS future_prices.symbol
                          AND candidate.trade_date IS future_prices.trade_date
                          AND (
                            EXISTS (
                              SELECT 1 FROM future_price_revisions AS current
                              WHERE current.revision_id IS future_prices.revision_id
                                AND current.symbol IS future_prices.symbol
                                AND current.trade_date IS future_prices.trade_date
                                AND (
                                  candidate.revision_id IS current.revision_id
                                  OR {candidate_advances_current}
                                )
                            )
                            OR (
                              NOT EXISTS (
                                SELECT 1 FROM future_price_revisions AS current
                                WHERE current.revision_id IS future_prices.revision_id
                                  AND current.symbol IS future_prices.symbol
                                  AND current.trade_date IS future_prices.trade_date
                              )
                              AND {candidate_is_best}
                            )
                          )
                    )
                    """,
                    pointer_values
                    + (price["symbol"], price["trade_date"], revision_id),
                )
                cursor.execute(
                    f"""
                    INSERT INTO future_prices
                        (symbol, trade_date, name, close_price, change_amt, change_pct,
                         open_price, high_price, low_price, prev_close, volume, amount,
                         limit_up, limit_down, suspension_state, limit_state,
                         tradability_state, ts, revision_id)
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM future_prices
                        WHERE symbol IS ? AND trade_date IS ?
                    ) AND EXISTS (
                        SELECT 1 FROM future_price_revisions AS candidate
                        WHERE candidate.revision_id IS ?
                          AND candidate.symbol IS ?
                          AND candidate.trade_date IS ?
                          AND {candidate_is_best}
                    )
                    """,
                    (price["symbol"], price["trade_date"])
                    + pointer_values
                    + (
                        price["symbol"], price["trade_date"], revision_id,
                        price["symbol"], price["trade_date"],
                    ),
                )
            conn.commit()


def get_future_price(symbol: str, trade_date: str) -> Optional[Dict[str, Any]]:
    """Return the exact date-keyed close snapshot, if present."""
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM future_prices WHERE symbol = ? AND trade_date = ? LIMIT 1",
                (symbol, trade_date),
            ).fetchone()
            return dict(row) if row else None


def get_event_quote_universe(
    *,
    since: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, str]]:
    """Return the bounded real-equity universe referenced by recent events."""
    cutoff = int(since or (time.time() - 120 * 86400))
    bounded_limit = max(1, min(5_000, int(limit)))
    with db_lock:
        with get_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT security.security_id, security.exchange, security.ticker, "
                    "security.name, security.asset_type, MAX(mapping.relevance) relevance, "
                    "MAX(mapping.created_at) mapped_at, "
                    "MAX(event.available_at) latest_event_available_at "
                    "FROM event_securities AS mapping "
                    "JOIN securities AS security ON security.security_id = mapping.security_id "
                    "JOIN events AS event ON event.event_id = mapping.event_id "
                    "WHERE mapping.is_current = 1 AND event.available_at >= ? "
                    "AND lower(mapping.relation_type) <> 'benchmark_proxy' "
                    "AND security.is_active = 1 AND lower(security.asset_type) = 'equity' "
                    "GROUP BY security.security_id, security.exchange, security.ticker, "
                    "security.name, security.asset_type "
                    "ORDER BY latest_event_available_at DESC, relevance DESC, mapped_at DESC, "
                    "security.security_id LIMIT ?",
                    (cutoff, bounded_limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # Standalone collector/unit-test databases can predate the
                # stable event core.  The four mandatory indices remain a
                # complete fallback universe in that case.
                if "no such table" not in str(exc).casefold():
                    raise
                rows = []
    prefixes = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}
    result = []
    for row in rows:
        prefix = prefixes.get(str(row["exchange"]).upper())
        ticker = str(row["ticker"] or "")
        if prefix and re.fullmatch(r"[0-9]{6}", ticker):
            result.append(
                {
                    "security_id": str(row["security_id"]),
                    "symbol": prefix + ticker,
                    "ticker": ticker,
                    "exchange": str(row["exchange"]),
                    "name": str(row["name"]),
                    "asset_type": str(row["asset_type"]),
                }
            )
    return result


def _direction_from_change(change_amt: Any) -> Optional[int]:
    if change_amt is None:
        return None
    value = float(change_amt)
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _trading_day_after(from_date: str, offset_days: int) -> str:
    """Return the exact Nth A-share trading day after ``from_date``.

    Calendar position, rather than the number of rows present in
    ``future_prices``, defines the horizon.  This distinction is critical for
    realized backtests: a missing close on T+1 must not silently turn the T+2
    row into a T+1 observation.
    """
    if offset_days < 1:
        raise ValueError("offset_days must be at least 1")
    day = datetime.strptime(from_date, "%Y-%m-%d").date()
    remaining = offset_days
    # A horizon of a few trading days can cross long public holidays.  The
    # generous bound prevents an accidental infinite loop if a future calendar
    # definition is malformed while still failing closed.
    for _ in range(370):
        day += timedelta(days=1)
        if _is_trading_day_local(day.isoformat()):
            remaining -= 1
            if remaining == 0:
                return day.isoformat()
    raise ValueError(f"无法从 {from_date} 定位第 {offset_days} 个后续交易日")


def _get_future_price_at_offset(
    symbol: str,
    from_date: str,
    offset_days: int,
) -> Optional[Dict[str, Any]]:
    """Return an exact horizon close, or ``None`` when that close is missing."""
    try:
        target_date = _trading_day_after(from_date, offset_days)
    except ValueError:
        # The local calendar deliberately fails closed for unsupported years.
        # A dashboard backtest should represent that as unavailable data rather
        # than guess weekdays or shift the requested horizon.
        return None
    return get_future_price(symbol, target_date)


def _get_future_direction(symbol: str, from_date: str, offset_days: int) -> Optional[int]:
    """Return direction on the exact Nth trading day after ``from_date``.

    Missing target-day data yields ``None``.  Later stored rows are never used
    as substitutes.
    """
    if offset_days < 1:
        raise ValueError("offset_days must be at least 1")
    row = _get_future_price_at_offset(symbol, from_date, offset_days)
    if not row:
        return None
    return _direction_from_change(row.get("change_amt"))


def store_market_prices(prices: List[Dict[str, Any]]) -> None:
    """Upsert the legacy latest snapshot for each symbol."""
    with db_lock:
        with get_connection() as conn:
            cursor = conn.cursor()
            _init_market_prices(cursor)
            for price in prices:
                cursor.execute(
                    """
                    INSERT INTO market_prices
                        (symbol, name, price, prev_close, change_amt, change_pct, ts, trade_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name,
                        price=excluded.price,
                        prev_close=excluded.prev_close,
                        change_amt=excluded.change_amt,
                        change_pct=excluded.change_pct,
                        ts=excluded.ts,
                        trade_date=excluded.trade_date
                    """,
                    (
                        price["symbol"], price.get("name", ""), price["price"],
                        price.get("prev_close"), price.get("change_amt"),
                        price.get("change_pct"), price.get("ts", int(time.time())),
                        price.get("trade_date", ""),
                    ),
                )
            conn.commit()


def get_market_price_by_date(symbol: str, trade_date: str) -> Optional[Dict[str, Any]]:
    """Compatibility lookup for collectors that still use ``market_prices``."""
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM market_prices WHERE symbol = ? AND trade_date = ? "
                "ORDER BY ts DESC LIMIT 1",
                (symbol, trade_date),
            ).fetchone()
            return dict(row) if row else None


def get_latest_market_price(symbol: str) -> Optional[Dict[str, Any]]:
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM market_prices WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None


def _init_request_logs(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, url TEXT,
            articles_total INTEGER, articles_passed INTEGER, cache_hits INTEGER,
            similarity_hits INTEGER, blacklist_hits INTEGER, fast_pass_hits INTEGER DEFAULT 0,
            api_calls INTEGER, duration_ms INTEGER, error TEXT
        )
    """)
    _add_column(cursor, "request_logs", "fast_pass_hits", "INTEGER DEFAULT 0")


def _init_feed_health(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feed_health (
            url TEXT PRIMARY KEY, name TEXT, last_check INTEGER,
            response_time_ms INTEGER, success INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,
            articles_total INTEGER DEFAULT 0, articles_passed INTEGER DEFAULT 0,
            avg_score REAL, last_error TEXT,
            consecutive_empty_checks INTEGER NOT NULL DEFAULT 0,
            last_nonempty_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    _add_column(
        cursor,
        "feed_health",
        "consecutive_empty_checks",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column(
        cursor,
        "feed_health",
        "last_nonempty_at",
        "INTEGER NOT NULL DEFAULT 0",
    )
    # Legacy rows cannot reconstruct the exact historical non-empty check.
    # Seed it conservatively from last_check only when cumulative articles show
    # that the source has produced usable entries before.
    cursor.execute(
        "UPDATE feed_health SET last_nonempty_at=last_check "
        "WHERE COALESCE(last_nonempty_at, 0)=0 "
        "AND COALESCE(articles_total, 0)>0"
    )


def _init_llm_call_logs(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            purpose TEXT DEFAULT '',
            backend_name TEXT DEFAULT '',
            model_name TEXT DEFAULT '',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            success INTEGER DEFAULT 1,
            error_type TEXT DEFAULT '',
            strategy_version TEXT DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            attempt_kind TEXT NOT NULL DEFAULT '',
            response_mode TEXT NOT NULL DEFAULT '',
            format_retry INTEGER NOT NULL DEFAULT 0,
            usage_status TEXT NOT NULL DEFAULT 'legacy',
            processing_id TEXT NOT NULL DEFAULT '',
            article_key TEXT NOT NULL DEFAULT ''
        )
    """)
    # Attribution cannot be reconstructed for historical attempts. Keep their
    # existing token/cost values and mark only the new metadata as unknown.
    for column, definition in (
        ("request_id", "TEXT NOT NULL DEFAULT ''"),
        ("attempt_kind", "TEXT NOT NULL DEFAULT ''"),
        ("response_mode", "TEXT NOT NULL DEFAULT ''"),
        ("format_retry", "INTEGER NOT NULL DEFAULT 0"),
        ("usage_status", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("processing_id", "TEXT NOT NULL DEFAULT ''"),
        ("article_key", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column(cursor, "llm_call_logs", column, definition)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_call_logs_ts "
        "ON llm_call_logs(timestamp DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_call_logs_purpose "
        "ON llm_call_logs(purpose, timestamp DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_call_logs_processing "
        "ON llm_call_logs(processing_id, id) WHERE processing_id <> ''"
    )


def _init_event_tables(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_cache (
            event_id TEXT PRIMARY KEY,
            canonical_title TEXT DEFAULT '',
            canonical_link TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            score REAL DEFAULT 0,
            tag TEXT DEFAULT '',
            source_count INTEGER DEFAULT 0,
            first_seen_at INTEGER DEFAULT 0,
            last_seen_at INTEGER DEFAULT 0,
            strategy_version TEXT DEFAULT ''
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_articles (
            event_id TEXT NOT NULL,
            link TEXT NOT NULL,
            feed_url TEXT DEFAULT '',
            raw_title TEXT DEFAULT '',
            score REAL DEFAULT 0,
            created_at INTEGER DEFAULT 0,
            PRIMARY KEY (event_id, link)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_cache_last_seen "
        "ON event_cache(last_seen_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_articles_link "
        "ON event_articles(link)"
    )


def _sync_feed_health_from_config(cursor) -> None:
    """启动时同步：config 中启用的 feed 若在 feed_health 无记录，则插入占位行。

    解决「新增 feed 后 /health/feeds 不显示」问题：
    - feed_health 仅通过 /refresh 写入，但极速路由器模式下 /refresh 禁用
    - 新 feed 永远不会有记录 → AI-Filter 面板显示不完整

    注意：这里不删除历史 feed_health 行。Docker bridge 与本地地址改写、临时禁用 feed、
    或配置文件挂载差异都不应在进程启动时清空历史健康数据。
    """
    from core.config import config as _cfg

    feeds = _cfg.cfg.get("feeds", [])
    for feed in feeds:
        url = feed.get("url", "")
        if not url or not feed.get("enabled", True):
            continue
        name = feed.get("name", url)
        cursor.execute(
            "INSERT OR IGNORE INTO feed_health (url, name, last_check, response_time_ms, "
            "success, fail, articles_total, articles_passed) "
            "VALUES (?, ?, 0, 0, 0, 0, 0, 0)",
            (url, name),
        )


def _init_app_meta(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
    """)


def _init_article_feedback(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS article_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link TEXT NOT NULL,
            label TEXT NOT NULL,
            target_score REAL,
            note TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_feedback_link_created "
        "ON article_feedback(link, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_feedback_link_created_id "
        "ON article_feedback(link, created_at DESC, id DESC)"
    )


def _init_feedback_example_snapshots(cursor) -> None:
    """Freeze feedback-visible article fields independently from live cache rows."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback_example_snapshots (
            feedback_id INTEGER PRIMARY KEY,
            link TEXT NOT NULL,
            label TEXT NOT NULL,
            target_score REAL,
            note TEXT NOT NULL DEFAULT '',
            feedback_created_at INTEGER NOT NULL,
            raw_title TEXT NOT NULL,
            score REAL,
            tag TEXT NOT NULL DEFAULT '',
            logic TEXT NOT NULL DEFAULT '',
            source_strategy_version TEXT NOT NULL DEFAULT '',
            captured_at INTEGER NOT NULL,
            snapshot_origin TEXT NOT NULL,
            FOREIGN KEY(feedback_id) REFERENCES article_feedback(id) ON DELETE RESTRICT,
            CHECK(snapshot_origin IN ('feedback_write', 'posthoc_materialized'))
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_snapshot_label_created "
        "ON feedback_example_snapshots(label, feedback_created_at DESC, feedback_id DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_snapshot_link_created "
        "ON feedback_example_snapshots(link, feedback_created_at DESC, feedback_id DESC)"
    )
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_feedback_example_snapshots_immutable_update
        BEFORE UPDATE ON feedback_example_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'feedback example snapshots are immutable');
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_feedback_example_snapshots_immutable_delete
        BEFORE DELETE ON feedback_example_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'feedback example snapshots are immutable');
        END
    """)
    # Legacy feedback did not freeze prompt-visible fields. Materialize the
    # values known at this upgrade boundary once, label them honestly, and use
    # them only for prospective contracts. Never claim they existed at the
    # original feedback timestamp.
    captured_at = int(time.time())
    cursor.execute(
        "INSERT OR IGNORE INTO feedback_example_snapshots "
        "(feedback_id, link, label, target_score, note, feedback_created_at, "
        "raw_title, score, tag, logic, source_strategy_version, captured_at, "
        "snapshot_origin) "
        "SELECT f.id, f.link, f.label, f.target_score, COALESCE(f.note, ''), "
        "f.created_at, n.raw_title, n.score, COALESCE(n.tag, ''), "
        "COALESCE(n.logic, ''), COALESCE(n.strategy_version, ''), ?, "
        "'posthoc_materialized' "
        "FROM article_feedback f JOIN news_cache n ON n.link = f.link "
        "WHERE n.raw_title IS NOT NULL",
        (captured_at,),
    )
    cursor.execute(
        "INSERT OR REPLACE INTO app_meta (k, v) "
        "VALUES ('feedback_example_schema_version', '1')"
    )


def _init_scoring_contract_snapshots(cursor) -> None:
    """Create the immutable registry behind dynamic scoring contexts.

    ``news_cache`` and the signal ledger already freeze ``strategy_version``.
    This registry makes a dynamic few-shot suffix reproducible by preserving
    the exact ordered feedback snapshot that produced it.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scoring_contract_snapshots (
            strategy_version TEXT PRIMARY KEY,
            base_strategy_version TEXT NOT NULL,
            feedback_profile_hash TEXT NOT NULL,
            feedback_profile_json TEXT NOT NULL,
            feedback_sample_ids_json TEXT NOT NULL,
            feedback_snapshot_json TEXT NOT NULL,
            feedback_context TEXT NOT NULL,
            feedback_context_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    required_columns = {
        "feedback_profile_json": "TEXT NOT NULL DEFAULT '{}'",
        "feedback_context": "TEXT NOT NULL DEFAULT ''",
    }
    missing_columns = [
        (name, definition)
        for name, definition in required_columns.items()
        if not _column_exists(cursor, "scoring_contract_snapshots", name)
    ]
    if missing_columns:
        existing_rows = int(cursor.execute(
            "SELECT COUNT(*) FROM scoring_contract_snapshots"
        ).fetchone()[0])
        if existing_rows:
            raise RuntimeError(
                "cannot safely upgrade non-empty legacy scoring contract snapshots"
            )
        for name, definition in missing_columns:
            _add_column(
                cursor, "scoring_contract_snapshots", name, definition
            )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_contract_base_created "
        "ON scoring_contract_snapshots(base_strategy_version, created_at DESC)"
    )
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_scoring_contract_snapshots_immutable_update
        BEFORE UPDATE ON scoring_contract_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'scoring contract snapshots are immutable');
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_scoring_contract_snapshots_immutable_delete
        BEFORE DELETE ON scoring_contract_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'scoring contract snapshots are immutable');
        END
    """)
    cursor.execute(
        "INSERT OR REPLACE INTO app_meta (k, v) "
        "VALUES ('scoring_contract_schema_version', '2')"
    )


def _strategy_where_clause(cfg: Optional[Dict], params: List[Any]) -> str:
    """按评分策略过滤缓存行。

    完整版本形如 manual:fingerprint。指纹随配置微调会变，但 manual 段不变时
    仍应展示历史高分（与 Briefing 跨策略读取一致）；仅 manual 段变化时才隔离。
    """
    strategy_version = str((cfg or {}).get("scoring_strategy_version") or "")
    if not strategy_version:
        return ""
    if ":" in strategy_version:
        manual_prefix = strategy_version.split(":", 1)[0] + ":"
        params.append(f"{manual_prefix}%")
        return " AND strategy_version LIKE ?"
    params.append(strategy_version)
    return " AND strategy_version = ?"


def _exact_strategy_where_clause(cfg: Optional[Dict], params: List[Any]) -> str:
    """Isolate inference inputs by the complete effective strategy identity.

    Read models intentionally retain manual-prefix compatibility so dashboards
    can show history across static configuration fingerprints.  Similarity
    candidates are model inputs, not a read model: reusing a title scored under
    another few-shot/profile suffix would silently import the previous scoring
    contract into the current run.
    """
    strategy_version = str((cfg or {}).get("scoring_strategy_version") or "")
    if not strategy_version:
        return ""
    params.append(strategy_version)
    return " AND strategy_version = ?"


def _feed_config_for_url(cfg: Optional[Dict], feed_url: str) -> Dict[str, Any]:
    if not cfg or not feed_url:
        return {}
    for feed in cfg.get("feeds", []) or []:
        if feed.get("url") == feed_url:
            return feed
    aliases = cfg.get("_runtime_feed_url_aliases", {}) or {}
    if isinstance(aliases, dict):
        for canonical, originals in aliases.items():
            family = [str(canonical or "")]
            if isinstance(originals, (list, tuple)):
                family.extend(str(item or "") for item in originals)
            if feed_url not in family:
                continue
            for feed in cfg.get("feeds", []) or []:
                if feed.get("url") == canonical:
                    return feed
    return {}


def get_feed_config(cfg: Optional[Dict], feed_url: str) -> Dict[str, Any]:
    """按精确 URL 或配置加载时记录的受控别名获取 feed 配置。"""
    return _feed_config_for_url(cfg, feed_url)


def canonical_feed_url(cfg: Optional[Dict], feed_url: str = "") -> str:
    """Normalize only exact, configuration-generated runtime URL aliases."""
    value = str(feed_url or "")
    aliases = (cfg or {}).get("_runtime_feed_url_aliases", {}) or {}
    if not isinstance(aliases, dict):
        return value
    for canonical, originals in aliases.items():
        canonical = str(canonical or "")
        if not canonical:
            continue
        family = [canonical]
        if isinstance(originals, (list, tuple)):
            family.extend(str(item or "") for item in originals)
        if value in family:
            return canonical
    return value


def get_feed_thresholds(cfg: Optional[Dict], feed_url: str = "") -> Dict[str, float]:
    """Feed 阈值唯一入口：pass / notify / aggregation 的单一事实来源。"""
    cfg = cfg or {}
    threshold_cfg = cfg.get("threshold", {}) or {}
    base_pass = float(threshold_cfg.get("pass", 7))
    base_notify = float(threshold_cfg.get("notify", 9))
    feed_cfg = get_feed_config(cfg, feed_url)
    pass_threshold = float(feed_cfg.get("pass_threshold", base_pass))
    return {
        "pass": pass_threshold,
        "notify": float(feed_cfg.get("notify_threshold", base_notify)),
        "aggregation": float(feed_cfg.get("aggregation_threshold", pass_threshold)),
    }


def pass_threshold_for_feed(cfg: Optional[Dict], feed_url: str = "") -> float:
    return get_feed_thresholds(cfg, feed_url)["pass"]


def notify_threshold_for_feed(cfg: Optional[Dict], feed_url: str = "") -> float:
    return get_feed_thresholds(cfg, feed_url)["notify"]


def aggregation_threshold_for_feed(cfg: Optional[Dict], feed_url: str = "") -> float:
    return get_feed_thresholds(cfg, feed_url)["aggregation"]


def equivalent_feed_urls(cfg: Optional[Dict], feed_url: str = "") -> List[str]:
    """Return the exact URL family recorded during a controlled host rewrite."""
    urls: List[str] = []
    if feed_url:
        urls.append(feed_url)
    aliases = (cfg or {}).get("_runtime_feed_url_aliases", {}) or {}
    if isinstance(aliases, dict):
        for canonical, originals in aliases.items():
            family = [str(canonical or "")]
            if isinstance(originals, (list, tuple)):
                family.extend(str(item or "") for item in originals)
            family = [item for item in family if item]
            if any(item in urls for item in family):
                urls.extend(item for item in family if item not in urls)
    return urls


# ===== 模块级函数（替代静态方法类）=====


def get_cached_entry(link: str) -> Optional[Tuple]:
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT title, raw_title, score, tag, logic, created_at, summary, "
                "strategy_version, score_path, score_confidence, direction, "
                f"event_id, event_version, {EXPLANATION_COLUMNS} "
                "FROM news_cache WHERE link = ?", (link,)
            ).fetchone()
            return tuple(row) if row else None


def get_cached_entry_meta(link: str) -> Dict[str, Any]:
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT score_path, prompt_key, model_name, latency_ms, error_type, "
                "source_tier, strategy_version, score_confidence, direction, "
                f"event_id, event_version, {EXPLANATION_COLUMNS} "
                "FROM news_cache WHERE link = ?",
                (link,),
            ).fetchone()
            return dict(row) if row else {}


def get_latest_signal_score_path(link: str, strategy_version: str = "") -> str:
    """Return the newest ledger score path for one link and strategy."""
    normalized = str(link or "").strip()
    strategy = str(strategy_version or "").strip()
    if not normalized or not strategy:
        return ""
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT score_path FROM backtest_signals "
                "WHERE link = ? AND strategy_version = ? "
                "ORDER BY available_at DESC, id DESC LIMIT 1",
                (normalized, strategy),
            ).fetchone()
    if row is None:
        return ""
    return normalize_score_path(row["score_path"] if "score_path" in row.keys() else row[0])


def get_event_identity_for_link(link: str) -> Dict[str, Any]:
    """Return the canonical persisted event identity for one cached article."""
    normalized = str(link or "").strip()
    if not normalized:
        return {}
    with db_lock:
        with get_connection() as conn:
            try:
                row = conn.execute(
                    "SELECT event_id FROM news_cache WHERE link = ?", (normalized,)
                ).fetchone()
                if row is None or not str(row[0] or "").strip():
                    row = conn.execute(
                        "SELECT event_id FROM backtest_signals WHERE link = ? "
                        "AND COALESCE(event_id, '') <> '' "
                        "ORDER BY available_at DESC, id DESC LIMIT 1",
                        (normalized,),
                    ).fetchone()
                if row is None or not str(row[0] or "").strip():
                    return {}
                root = canonical_event_id(conn, str(row[0]))
                event = conn.execute(
                    "SELECT event_id, version, event_type, direction, priority, "
                    "occurred_at, merge_state FROM events WHERE event_id = ?",
                    (root,),
                ).fetchone()
            except (sqlite3.OperationalError, KeyError, EventCoreConflictError):
                return {}
            return dict(event) if event else {}


def get_canonical_event(event_id: str) -> Dict[str, Any]:
    """Read one canonical event without inventing or mutating an identity."""
    value = str(event_id or "").strip()
    if not value:
        return {}
    with db_lock:
        with get_connection() as conn:
            try:
                root = canonical_event_id(conn, value)
                row = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?", (root,)
                ).fetchone()
            except (sqlite3.OperationalError, KeyError, EventCoreConflictError):
                return {}
            return dict(row) if row else {}


def get_cached_entries_by_links(links: List[str]) -> Dict[str, Tuple]:
    """批量读取缓存行，供 process_feed 减少每条目的 SQLite 往返。"""
    if not links:
        return {}
    uniq: List[str] = list(dict.fromkeys(links))
    if len(uniq) > 300:
        uniq = uniq[:300]
    with db_lock:
        with get_connection() as conn:
            ph = ",".join("?" * len(uniq))
            rows = conn.execute(
                f"SELECT link, title, raw_title, score, tag, logic, created_at, summary, "
                f"strategy_version, score_path, score_confidence, direction, "
                f"event_id, event_version, {EXPLANATION_COLUMNS} "
                f"FROM news_cache WHERE link IN ({ph})",
                uniq,
            ).fetchall()
            return {str(r[0]): tuple(r[1:]) for r in rows}


def _upsert_article_source(
    conn: sqlite3.Connection,
    link: str,
    feed_url: str,
    observed_at: Optional[int] = None,
) -> bool:
    """Record one source sighting in the caller's transaction.

    The ``EXISTS`` guard prevents an orphan relation if a stale cache-repair
    job races with retention cleanup.
    """
    normalized_link = str(link or "").strip()
    normalized_feed_url = str(feed_url or "").strip()
    if not normalized_link or not normalized_feed_url:
        return False
    seen_at = int(observed_at if observed_at is not None else time.time())
    cursor = conn.execute(
        "INSERT INTO article_sources (link, feed_url, first_seen_at, last_seen_at) "
        "SELECT ?, ?, ?, ? WHERE EXISTS "
        "(SELECT 1 FROM news_cache WHERE link = ?) "
        "ON CONFLICT(link, feed_url) DO UPDATE SET "
        "first_seen_at = MIN(article_sources.first_seen_at, excluded.first_seen_at), "
        "last_seen_at = MAX(article_sources.last_seen_at, excluded.last_seen_at)",
        (
            normalized_link,
            normalized_feed_url,
            seen_at,
            seen_at,
            normalized_link,
        ),
    )
    return cursor.rowcount > 0


def record_article_source(
    link: str,
    feed_url: str,
    observed_at: Optional[int] = None,
) -> bool:
    """Idempotently register that ``feed_url`` observed ``link``."""
    with db_lock:
        with get_connection() as conn:
            dashboard_changed = _new_dashboard_article_source(conn, link, feed_url)
            changed = _upsert_article_source(conn, link, feed_url, observed_at)
            _commit_dashboard_inputs(conn, changed=changed and dashboard_changed)
            return changed


def record_article_sources(
    sightings: List[Tuple[str, str]],
    observed_at: Optional[int] = None,
) -> int:
    """Register multiple source sightings in one transaction.

    The returned count is the number of non-empty, existing article/source
    pairs attempted. Repeated pairs remain a single relationship row.
    """
    rows = [
        (str(link).strip(), str(feed_url).strip())
        for link, feed_url in sightings
        if str(link or "").strip() and str(feed_url or "").strip()
    ]
    if not rows:
        return 0
    seen_at = int(observed_at if observed_at is not None else time.time())
    recorded = 0
    with db_lock:
        with get_connection() as conn:
            dashboard_changed = False
            for link, feed_url in rows:
                dashboard_changed = (
                    _new_dashboard_article_source(conn, link, feed_url)
                    or dashboard_changed
                )
                if _upsert_article_source(conn, link, feed_url, seen_at):
                    recorded += 1
            _commit_dashboard_inputs(conn, changed=bool(recorded) and dashboard_changed)
    return recorded


def get_article_sources(link: str) -> List[Dict[str, Any]]:
    """Return every known source for a link without changing legacy rows."""
    normalized_link = str(link or "").strip()
    if not normalized_link:
        return []
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT feed_url, first_seen_at, last_seen_at "
                "FROM article_sources WHERE link = ? "
                "ORDER BY first_seen_at ASC, feed_url ASC",
                (normalized_link,),
            ).fetchall()
            return [dict(row) for row in rows]


def _backtest_signal_key(
    link: str,
    strategy_version: str,
    revision_key: str = "base",
) -> str:
    normalized_revision = str(revision_key or "base").strip() or "base"
    # Keep the original base key stable for databases that already contain the
    # first immutable snapshot format.  Revisions add a third identity part.
    identity = f"{str(link).strip()}\0{str(strategy_version).strip()}"
    if normalized_revision != "base":
        identity += f"\0{normalized_revision}"
    payload = identity.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finite_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _validate_safe_neutral_fields(
    *,
    score_path: str,
    score: Any,
    tag: Any,
    logic: Any,
    direction: Any,
    score_confidence: Any,
    field_prefix: str,
) -> None:
    """Require the exact frozen tuple for every non-substantive score path."""
    score_path = normalize_score_path(score_path)
    if score_path not in NON_SUBSTANTIVE_SCORE_PATHS:
        return
    expected_score, expected_tag, expected_logic, _sentiment = (
        SAFE_NEUTRAL_CONTRACT_VALUES
    )
    actual_score = _finite_float(score, f"{field_prefix}.score")
    actual_confidence = _finite_float(
        score_confidence or 0.0,
        f"{field_prefix}.score_confidence",
    )
    if not math.isclose(
        actual_score,
        expected_score,
        rel_tol=0.0,
        abs_tol=0.0,
    ):
        raise ValueError(f"{field_prefix} safe-neutral score must be exactly 4")
    if str(tag or "") != expected_tag:
        raise ValueError(f"{field_prefix} safe-neutral tag is invalid")
    if str(logic or "") != expected_logic:
        raise ValueError(f"{field_prefix} safe-neutral logic is invalid")
    if str(direction or "").strip() != "中性":
        raise ValueError(f"{field_prefix} safe-neutral direction must be neutral")
    if not math.isclose(
        actual_confidence,
        0.0,
        rel_tol=0.0,
        abs_tol=0.0,
    ):
        raise ValueError(f"{field_prefix} safe-neutral confidence must be zero")


def _validate_safe_neutral_signal_payload(
    signal: Dict[str, Any],
    *,
    score_path: str,
    score: Any,
    direction: Any,
) -> None:
    score_path = normalize_score_path(score_path)
    if score_path not in NON_SUBSTANTIVE_SCORE_PATHS:
        return
    _validate_safe_neutral_fields(
        score_path=score_path,
        score=score,
        tag=signal.get("event_type") or signal.get("tag"),
        logic=signal.get("logic"),
        direction=direction,
        score_confidence=signal.get("score_confidence", 0.0),
        field_prefix="backtest_signal",
    )
    if "selected" not in signal or bool(signal.get("selected")):
        raise ValueError("backtest_signal safe-neutral result must be unselected")
    if bool(signal.get("is_similarity_copy")):
        raise ValueError("backtest_signal safe-neutral result cannot be a similarity copy")
    if bool(signal.get("auto_revision")):
        raise ValueError("backtest_signal safe-neutral result cannot be an auto revision")


_BACKTEST_PROXY_SECURITIES = {
    "sh000001": ("SSE", "000001", "上证综指"),
    "sh000300": ("SSE", "000300", "沪深300"),
    "sz399001": ("SZSE", "399001", "深证成指"),
    "sz399006": ("SZSE", "399006", "创业板指"),
}

_EQUITY_NAME_CACHE: tuple[
    str,
    tuple[int, int, int],
    float,
    list[tuple[str, str, str, str]],
] = ("", (0, 0, 0), 0.0, [])
_EQUITY_NAME_CACHE_TTL_SECONDS = 300
_EQUITY_MENTION_INDEX_CACHE: tuple[
    object | None,
    tuple[
        tuple[tuple[str, str, str, str, str], ...],
        dict[str, tuple[int, ...]],
        dict[str, tuple[int, ...]],
        tuple[int, ...],
    ]
    | None,
] = (None, None)


def _equity_name_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """Return a short-lived in-process security-name index for direct mentions."""
    global _EQUITY_NAME_CACHE
    database_path = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    signature_row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), 0), COALESCE(SUM(version), 0) "
        "FROM securities "
        "WHERE is_active = 1 AND lower(asset_type) = 'equity'"
    ).fetchone()
    signature = (
        int(signature_row[0]),
        int(signature_row[1]),
        int(signature_row[2]),
    )
    cached_path, cached_signature, loaded_at, rows = _EQUITY_NAME_CACHE
    now = time.monotonic()
    if (
        rows
        and cached_path == database_path
        and cached_signature == signature
        and now - loaded_at < _EQUITY_NAME_CACHE_TTL_SECONDS
    ):
        return rows
    rows = [
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in conn.execute(
            "SELECT security_id, name, ticker, exchange FROM securities "
            "WHERE is_active = 1 AND lower(asset_type) = 'equity' "
            "ORDER BY length(name) DESC, security_id"
        ).fetchall()
        if str(row[1] or "").strip()
    ]
    _EQUITY_NAME_CACHE = (database_path, signature, now, rows)
    return rows


def _equity_mention_index(
    conn: sqlite3.Connection,
) -> tuple[
    tuple[tuple[str, str, str, str, str], ...],
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    tuple[int, ...],
]:
    """Index exact names/tickers while preserving the master row ordering."""
    global _EQUITY_MENTION_INDEX_CACHE
    source_rows = _equity_name_rows(conn)
    cached_rows, cached_index = _EQUITY_MENTION_INDEX_CACHE
    if cached_rows is source_rows and cached_index is not None:
        return cached_index

    indexed_rows: list[tuple[str, str, str, str, str]] = []
    name_positions: dict[str, list[int]] = {}
    ticker_positions: dict[str, list[int]] = {}
    for security_id, name, ticker, exchange in source_rows:
        normalized_name = re.sub(r"\s+", "", name).casefold()
        if not normalized_name:
            continue
        position = len(indexed_rows)
        indexed_rows.append(
            (security_id, name, ticker, exchange, normalized_name)
        )
        name_positions.setdefault(normalized_name, []).append(position)
        if ticker:
            ticker_positions.setdefault(ticker, []).append(position)
    index = (
        tuple(indexed_rows),
        {key: tuple(value) for key, value in name_positions.items()},
        {key: tuple(value) for key, value in ticker_positions.items()},
        tuple(sorted({len(key) for key in name_positions})),
    )
    _EQUITY_MENTION_INDEX_CACHE = (source_rows, index)
    return index


def _direct_equity_mentions(
    conn: sqlite3.Connection,
    text: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    # Keep this import lazy: ``core.__init__`` exposes the feed processor,
    # which imports this database module during application startup.
    from core.asset_mapping import (
        is_direct_security_mention,
        is_direct_security_ticker_mention,
    )
    from db.title_mentions import _code_owner_names, _ticker_evidence

    compact = re.sub(r"\s+", "", str(text or "")).casefold()
    if not compact:
        return []
    ticker_tokens = set(re.findall(r"(?<!\d)[0-9]{6}(?!\d)", compact))
    index = _equity_mention_index(conn)
    rows, name_positions, ticker_positions, name_lengths = index
    candidate_positions: set[int] = set()
    for ticker in ticker_tokens:
        candidate_positions.update(ticker_positions.get(ticker, ()))
    compact_length = len(compact)
    for start in range(compact_length):
        for name_length in name_lengths:
            end = start + name_length
            if end > compact_length:
                break
            candidate_positions.update(
                name_positions.get(compact[start:end], ())
            )

    matches: list[dict[str, Any]] = []
    for position in sorted(candidate_positions):
        security_id, name, ticker, exchange, normalized_name = rows[position]
        if _ticker_evidence(compact, rows[position], False)[1]:
            continue
        if _code_owner_names(compact, ticker, index) - {normalized_name}:
            continue
        named_entity = (
            normalized_name in compact
            and is_direct_security_mention(text, name, allow_qualified_ticker=True)
        )
        ticker_entity = ticker in ticker_tokens and is_direct_security_ticker_mention(
            text, name, ticker, exchange=exchange
        )
        if not named_entity and not ticker_entity:
            continue
        matches.append(
            {
                "security_id": security_id,
                "name": name,
                "ticker": ticker,
                "exchange": exchange,
                "relation_type": "direct",
                # Name matching is deterministic but not a calibrated 97%
                # posterior.  An exact security-master ticker is stronger.
                "confidence": 0.96 if ticker_entity else 0.90,
                "relevance": 10.0,
                "rationale": (
                    f"原文直接出现证券代码{ticker}（{name}）。"
                    if ticker_entity
                    else f"原文直接点名{name}。"
                ),
            }
        )
        if len(matches) >= max(1, min(20, int(limit))):
            break
    return matches


def get_direct_equity_mentions_batch(
    raw_titles: list[str], *, limit: int = 5,
) -> list[dict[str, Any]]:
    """Read one master snapshot for final publisher titles, without writes.

    Results stay bound to their exact input title and preserve input order.
    An unavailable master is not an empty successful match. This card-only
    API does not expose event-mapping confidence or investment relevance.
    """
    from db.title_mentions import mention_result, resolve_title_mentions

    titles = [title if isinstance(title, str) else "" for title in raw_titles]
    if not any(title.strip() for title in titles):
        return [mention_result(title, "unavailable", "missing_title") for title in titles]
    try:
        with get_read_only_connection() as conn:
            index = _equity_mention_index(conn)
        return [resolve_title_mentions(title, index, limit=limit) for title in titles]
    except (sqlite3.Error, OSError, RuntimeError):
        logger.warning("Title mentions unavailable: security master could not be read")
        return [
            mention_result(
                title, "unavailable", "master_unavailable" if title.strip() else "missing_title",
            )
            for title in titles
        ]


def get_similarity_identity_rows() -> list[tuple] | None:
    """One read-only master snapshot; no display/mapping decisions or writes."""
    try:
        with get_read_only_connection() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT security_id, name, ticker, exchange, aliases FROM securities "
                "WHERE is_active = 1 AND lower(asset_type) = 'equity'"
            )]
    except (sqlite3.Error, OSError, RuntimeError):
        logger.warning("Similarity identity unavailable; candidates will be rescored")
        return None


def _candidate_security_row(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
) -> sqlite3.Row | None:
    security_id = str(candidate.get("security_id") or "").strip()
    if security_id:
        return conn.execute(
            "SELECT security_id, name, ticker, exchange FROM securities "
            "WHERE security_id = ? AND is_active = 1 AND lower(asset_type) = 'equity'",
            (security_id,),
        ).fetchone()
    name = str(candidate.get("name") or "").strip()
    if not name:
        return None
    return conn.execute(
        "SELECT security_id, name, ticker, exchange FROM securities "
        "WHERE name = ? AND is_active = 1 AND lower(asset_type) = 'equity' "
        # A metadata refresh timestamp is not entity identity.  Resolve an
        # otherwise ambiguous same-name master deterministically so an
        # unrelated sync cannot silently move an event to another security.
        "ORDER BY security_id LIMIT 1",
        (name,),
    ).fetchone()


def _bind_mapping_evidence(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    mapping_id: str,
    security_id: str,
    source_uri: str,
) -> Optional[str]:
    from core.source_contract import (
        ARTICLE_CLAIM_PREFIX, MAPPING_CLAIM_PREFIX, is_current_mapping_writer,
    )

    mapping = conn.execute(
        "SELECT event_id, mapping_version FROM event_securities WHERE mapping_id = ?",
        (mapping_id,),
    ).fetchone()
    if mapping is None:
        return None
    mapping_event_id = str(mapping[0])
    forward_writer = is_current_mapping_writer(str(mapping[1]))
    source_scope = (
        "AND substr(claim_key, 1, ?) = ? AND length(claim_key) = ? "
        "AND substr(claim_key, ?, 64) NOT GLOB '*[^0-9a-f]*' "
        if forward_writer else ""
    )
    base = conn.execute(
        "SELECT evidence_id, source_type, source_name, claim_text, excerpt, "
        "snapshot_hash, snapshot_text, stance, reliability, announcement_at, observed_at "
        "FROM event_evidence WHERE source_uri = ? "
        "AND event_id IN (SELECT event_id FROM events WHERE event_id IN (?, ?) "
        "OR merged_into IN (?, ?)) "
        "AND mapping_id IS NULL " + source_scope
        + "ORDER BY observed_at DESC, created_at DESC LIMIT 1",
        (
            source_uri,
            event_id,
            mapping_event_id,
            event_id,
            mapping_event_id,
            *((len(ARTICLE_CLAIM_PREFIX), ARTICLE_CLAIM_PREFIX,
               len(ARTICLE_CLAIM_PREFIX) + 64, len(ARTICLE_CLAIM_PREFIX) + 1)
              if forward_writer else ()),
        ),
    ).fetchone()
    if base is None:
        return None
    return upsert_event_evidence(
        conn,
        mapping_event_id,
        source_uri=source_uri,
        source_type=str(base[1]),
        source_name=str(base[2]),
        claim_key=(
            f"{MAPPING_CLAIM_PREFIX}{mapping_id}:{base[0]}"
            if forward_writer else f"mapping:{mapping_id}:{base[0]}"
        ),
        claim_text=str(base[3]),
        excerpt=str(base[4]),
        snapshot_hash=str(base[5]),
        snapshot_text=str(base[6]),
        stance=str(base[7]),
        reliability=float(base[8]),
        announcement_at=base[9],
        observed_at=int(base[10]),
        security_id=security_id,
        mapping_id=mapping_id,
    )


def _resolved_real_event_mapping_candidates(
    conn: sqlite3.Connection,
    *,
    article_text: str,
    candidates: Optional[List[Dict[str, Any]]] = None,
    source_text: Optional[str] = None,
) -> List[Tuple[Dict[str, Any], sqlite3.Row]]:
    """Resolve and rank candidates exactly as the persistent mapping writer."""
    from db.title_mentions import _ticker_evidence

    proposed: list[dict[str, Any]] = list(candidates or [])
    # Direct name/ticker hits may only come from publisher-owned source text.
    proposed.extend(
        _direct_equity_mentions(
            conn, article_text if source_text is None else source_text
        )
    )
    by_security: dict[str, tuple[int, dict[str, Any], sqlite3.Row]] = {}
    relation_rank = {"direct": 5, "upstream": 4, "downstream": 4, "ecosystem": 3, "concept": 2}
    for raw in proposed:
        if not isinstance(raw, dict):
            continue
        row = _candidate_security_row(conn, raw)
        if row is None:
            continue
        relation = str(raw.get("relation_type") or "concept").strip().lower()
        if relation not in relation_rank:
            relation = "concept"
        if relation == "direct":
            source = article_text if source_text is None else source_text
            compact = re.sub(r"\s+", "", str(source or "")).casefold()
            identity = (*tuple(row), re.sub(r"\s+", "", str(row[1])).casefold())
            if _ticker_evidence(compact, identity, False)[1]:
                continue
        security_id = str(row[0])
        rank = relation_rank[relation]
        previous = by_security.get(security_id)
        if previous is None or rank > previous[0]:
            by_security[security_id] = (rank, {**raw, "relation_type": relation}, row)

    return [
        (item, row)
        for _rank, item, row in sorted(
            by_security.values(),
            key=lambda value: (
                -float(value[1].get("relevance") or 0.0),
                str(value[2][2]),
            ),
        )[:8]
    ]


def upsert_real_event_mappings(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    article_text: str,
    source_uri: str,
    direction: str,
    mapping_version: str,
    candidates: Optional[List[Dict[str, Any]]] = None,
    now: Optional[int] = None,
    source_text: Optional[str] = None,
) -> list[str]:
    """Persist real-equity relationships and bind each to frozen evidence."""
    from core.source_contract import is_current_mapping_writer

    _require_event_mapping_write_authorized(mapping_version, conn)
    if is_current_mapping_writer(mapping_version) and not str(source_text or "").strip():
        return []
    current_time = int(now or time.time())
    mapping_ids: list[str] = []
    for item, row in _resolved_real_event_mapping_candidates(
        conn,
        article_text=article_text,
        candidates=candidates,
        source_text=source_text,
    ):
        expected_gap = item.get("expected_gap")
        mapping_id = upsert_event_security(
            conn,
            event_id,
            str(row[0]),
            relation_type=str(item["relation_type"]),
            direction=str(item.get("direction") or direction or "中性"),
            expected_gap=None if expected_gap in (None, "") else float(expected_gap),
            confidence=max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
            relevance=max(0.0, min(10.0, float(item.get("relevance") or 0.0))),
            rationale=str(item.get("rationale") or "原文证券映射")[:4000],
            mapping_version=(mapping_version or "asset-map:unknown")[:255],
            now=current_time,
        )
        mapping_ids.append(mapping_id)
        if source_uri:
            _bind_mapping_evidence(
                conn,
                event_id=event_id,
                mapping_id=mapping_id,
                security_id=str(row[0]),
                source_uri=source_uri,
            )
    return mapping_ids


def upsert_event_market_references(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    source_uri: str,
    direction: str,
    mapping_version: str,
    references: Optional[List[Dict[str, Any]]] = None,
    now: Optional[int] = None,
) -> List[str]:
    """Persist only explicitly named market indices; never invent an ETF."""
    _require_event_mapping_write_authorized(mapping_version, conn)
    current_time = int(now or time.time())
    mapping_ids: List[str] = []
    seen: set[str] = set()
    for raw in references or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("asset_type") or "").casefold() != "index":
            continue
        if str(raw.get("reference_type") or "") != "explicit_mention":
            continue
        exchange = str(raw.get("exchange") or "").strip().upper()
        ticker = str(raw.get("ticker") or "").strip()
        name = str(raw.get("name") or "").strip()
        if exchange not in {"SSE", "SZSE"} or not re.fullmatch(r"\d{6}", ticker) or not name:
            continue
        symbol = ("sh" if exchange == "SSE" else "sz") + ticker
        if symbol in seen:
            continue
        seen.add(symbol)
        security_id = upsert_security(
            conn,
            exchange=exchange,
            ticker=ticker,
            name=name,
            security_id=symbol,
            asset_type="index",
            currency="CNY",
            aliases=[symbol, ticker, name],
            industry="市场指数",
            concepts=[],
            now=current_time,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            security_id,
            relation_type="market_reference",
            direction=str(direction or "中性"),
            expected_gap=None,
            confidence=0.99,
            relevance=10.0,
            rationale=f"原文明确提及{name}；仅作为市场标的引用，不代表相关个股。",
            mapping_version=(mapping_version or "asset-map:unknown")[:255],
            now=current_time,
        )
        mapping_ids.append(mapping_id)
        if source_uri:
            _bind_mapping_evidence(
                conn,
                event_id=event_id,
                mapping_id=mapping_id,
                security_id=security_id,
                source_uri=source_uri,
            )
    return mapping_ids


def retire_stale_rule_event_mappings(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    keep_mapping_ids: List[str],
    current_mapping_version: str,
    retire_legacy: bool = False,
) -> int:
    """Retire obsolete output only within the writer's authorized cohort.

    Forward title writers leave legacy mappings alone. Only a separately
    requested legacy maintenance replay can opt into retiring them. Frozen
    payloads/revisions are never overwritten; retirement changes the current
    pointer. Manual/external mappings and benchmark proxies are never touched.
    """
    from core.source_contract import is_current_mapping_writer

    _require_event_mapping_write_authorized(current_mapping_version, conn)
    canonical_id = canonical_event_id(conn, event_id)
    version = str(current_mapping_version or "").strip()
    if not version.startswith("asset-map:"):
        raise ValueError("current_mapping_version must be an asset-map version")
    keep = {
        str(mapping_id).strip()
        for mapping_id in keep_mapping_ids
        if str(mapping_id).strip()
    }
    current_rows = conn.execute(
        "WITH RECURSIVE event_group(event_id) AS ("
        " SELECT ?"
        " UNION"
        " SELECT child.event_id FROM events AS child"
        " JOIN event_group AS parent ON child.merged_into = parent.event_id"
        ") SELECT mapping.mapping_id, mapping.mapping_version FROM event_securities AS mapping"
        " WHERE mapping.event_id IN (SELECT event_id FROM event_group)"
        " AND mapping.is_current = 1"
        " AND mapping.mapping_version LIKE 'asset-map:%'"
        " AND lower(mapping.relation_type) <> 'benchmark_proxy'",
        (canonical_id,),
    ).fetchall()
    forward_writer = is_current_mapping_writer(version)
    retire = [
        str(row[0]) for row in current_rows
        if str(row[0]) not in keep
        and (
            retire_legacy or not forward_writer
            or is_current_mapping_writer(str(row[1]))
        )
    ]
    if retire:
        conn.executemany(
            "UPDATE event_securities SET is_current = 0 "
            "WHERE mapping_id = ? AND is_current = 1",
            ((mapping_id,) for mapping_id in retire),
        )
    return len(retire)


def _upsert_event_proxy_mapping(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    symbol: Optional[str],
    direction: str,
    expected_gap: Optional[float],
    confidence: float,
    relevance: float,
    mapped_theme: str,
    mapping_version: str,
    now: int,
) -> Optional[str]:
    metadata = _BACKTEST_PROXY_SECURITIES.get(str(symbol or "").strip())
    if metadata is None:
        return None
    exchange, ticker, name = metadata
    security_id = upsert_security(
        conn,
        exchange=exchange,
        ticker=ticker,
        name=name,
        security_id=str(symbol),
        asset_type="index",
        currency="CNY",
        aliases=[str(symbol), ticker],
        industry="市场指数",
        concepts=[],
        now=now,
    )
    return upsert_event_security(
        conn,
        event_id,
        security_id,
        relation_type="benchmark_proxy",
        direction=direction,
        expected_gap=expected_gap,
        confidence=confidence,
        relevance=max(0.0, min(10.0, relevance)),
        rationale=(mapped_theme or "回测市场代理")[:4000],
        mapping_version=(mapping_version or "asset-map:unknown")[:255],
        now=now,
    )


def _contains_unicode_replacement(value: Any) -> bool:
    """Return whether any text nested in a persistence payload contains U+FFFD."""
    if isinstance(value, str):
        return "\ufffd" in value
    if isinstance(value, dict):
        return any(
            _contains_unicode_replacement(key)
            or _contains_unicode_replacement(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_unicode_replacement(item) for item in value)
    return False


def _optional_mapping_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def _mapping_semantics_digest(values: set[tuple]) -> str:
    payload = json.dumps(
        sorted([list(value) for value in values], key=lambda item: repr(item)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _is_real_quality_repair_model(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized) and normalized.casefold() not in {
        "fallback_local",
        "local_safe_neutral",
        "local",
        "none",
        "unknown",
    }


def _validate_quality_repair_cache_payload(
    *,
    link: str,
    raw_title: str,
    score: float,
    tag: str,
    logic: str,
    summary: str,
    score_path: str,
    model_name: str,
    error_type: str,
    strategy_version: str,
    stage1_model: str,
    stage2_model: str,
    score_confidence: float,
    direction: str,
    signal: Dict[str, Any],
) -> bool:
    """Validate cache and ledger repair payloads without trusting the caller."""
    from core.scorer import is_usable_score_result

    if not is_usable_score_result(score, tag, logic):
        raise EventCoreConflictError("quality repair result is unusable")
    signal_score = _finite_float(signal.get("score"), "backtest_signal.score")
    signal_confidence = _finite_float(
        signal.get("score_confidence", 0.0) or 0.0,
        "backtest_signal.score_confidence",
    )
    consistency_pairs = (
        ("link", str(signal.get("link") or "").strip(), str(link or "").strip()),
        (
            "strategy_version",
            str(signal.get("strategy_version") or "").strip(),
            str(strategy_version or "").strip(),
        ),
        ("raw_title", str(signal.get("raw_title") or ""), str(raw_title or "")),
        ("summary", str(signal.get("summary") or ""), str(summary or "")),
        ("event_type", str(signal.get("event_type") or ""), str(tag or "")),
        ("logic", str(signal.get("logic") or ""), str(logic or "")),
        (
            "score_path",
            str(signal.get("score_path") or "").strip(),
            str(score_path or "").strip(),
        ),
        (
            "direction",
            str(signal.get("direction") or "").strip(),
            str(direction or "").strip(),
        ),
    )
    for field, signal_value, cache_value in consistency_pairs:
        if signal_value != cache_value:
            raise EventCoreConflictError(
                f"quality repair cache/ledger {field} differs"
            )
    if not math.isclose(signal_score, float(score), rel_tol=0.0, abs_tol=1e-9):
        raise EventCoreConflictError("quality repair cache/ledger score differs")
    if not math.isclose(
        signal_confidence,
        float(score_confidence or 0.0),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise EventCoreConflictError(
            "quality repair cache/ledger score_confidence differs"
        )

    path = str(score_path or "").strip()
    error = str(error_type or "").strip()
    selected_model = str(model_name or "").strip()
    is_copy = bool(signal.get("is_similarity_copy"))
    if is_copy:
        if (
            path != "similarity"
            or error
            or selected_model
            or str(stage1_model or "").strip()
            or str(stage2_model or "").strip()
        ):
            raise EventCoreConflictError(
                "quality repair similarity copy has direct/error metadata"
            )
    elif (
        path not in {"llm", "stage2"}
        or error
        or not _is_real_quality_repair_model(selected_model)
        or (
            path == "llm"
            and str(stage1_model or "").strip() != selected_model
        )
        or (
            path == "stage2"
            and str(stage2_model or "").strip() != selected_model
        )
    ):
        raise EventCoreConflictError(
            "quality repair direct result is not a successful model score"
        )
    return is_copy


def _validate_cache_signal_score_contract(
    *,
    score_path: str,
    score: Any,
    tag: Any,
    logic: Any,
    direction: Any,
    score_confidence: Any,
    model_name: Any,
    error_type: Any,
    stage1_score: Any,
    stage1_model: Any,
    stage2_score: Any,
    stage2_model: Any,
    signal: Optional[Dict[str, Any]],
) -> None:
    """Bind mutable cache provenance to the same immutable signal payload."""
    cache_path = normalize_score_path(score_path)
    selected_model = str(model_name or "").strip()
    if (
        selected_model.casefold() == "local_safe_neutral"
        and cache_path not in NON_SUBSTANTIVE_SCORE_PATHS
    ):
        raise ValueError("local safe-neutral identity requires a non-substantive path")
    if signal is not None:
        signal_path = normalize_score_path(signal.get("score_path"))
        if signal_path != cache_path:
            raise ValueError("cache and backtest signal score_path differ")
    if cache_path not in NON_SUBSTANTIVE_SCORE_PATHS:
        return
    if signal is None:
        raise ValueError("safe-neutral cache write requires a backtest signal")
    _validate_safe_neutral_fields(
        score_path=cache_path,
        score=score,
        tag=tag,
        logic=logic,
        direction=direction,
        score_confidence=score_confidence,
        field_prefix="news_cache",
    )
    _validate_safe_neutral_signal_payload(
        signal,
        score_path=cache_path,
        score=signal.get("score"),
        direction=signal.get("direction"),
    )
    if str(error_type or "").strip():
        raise ValueError("safe-neutral cache write cannot carry an error type")
    stage1_identity = str(stage1_model or "").strip()
    stage2_identity = str(stage2_model or "").strip()
    stage, _resolution = SAFE_NEUTRAL_SCORE_PATH_METADATA[cache_path]
    expected_score = SAFE_NEUTRAL_CONTRACT_VALUES[0]
    if selected_model == "local_safe_neutral":
        if _resolution == SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL:
            raise ValueError("local safe-neutral identity cannot be an initial model result")
    elif not _is_real_quality_repair_model(selected_model):
        raise ValueError("safe-neutral cache write requires a real or local neutral identity")
    if stage == "stage1":
        if (
            stage1_identity != selected_model
            or stage2_identity
            or stage1_score is None
            or not math.isclose(
                _finite_float(stage1_score, "news_cache.stage1_score"),
                expected_score,
                rel_tol=0.0,
                abs_tol=0.0,
            )
            or stage2_score is not None
        ):
            raise ValueError("safe-neutral stage1 provenance is inconsistent")
    elif (
        not stage1_identity
        or stage2_identity != selected_model
        or stage2_score is None
        or not math.isclose(
            _finite_float(stage2_score, "news_cache.stage2_score"),
            expected_score,
            rel_tol=0.0,
            abs_tol=0.0,
        )
    ):
        raise ValueError("safe-neutral stage2 provenance is inconsistent")


def _reject_non_substantive_path_transition(
    existing_path: Any,
    new_path: Any,
    *,
    source: str,
) -> None:
    existing = str(existing_path or "").strip()
    new = normalize_score_path(new_path)
    existing = normalize_score_path(existing)
    if existing != new and (
        existing in NON_SUBSTANTIVE_SCORE_PATHS
        or new in NON_SUBSTANTIVE_SCORE_PATHS
    ):
        raise EventCoreConflictError(
            f"{source} cannot transition a non-substantive score path"
        )


def _quality_repair_real_mapping_semantics(
    conn: sqlite3.Connection,
    *,
    signal: Dict[str, Any],
    inherited_signal: sqlite3.Row,
    event_id: str,
    direction: str,
    mapping_version: str,
    raw_title: str,
    logic: str,
    summary: str,
    mapped_theme: str,
) -> None:
    """Reject a textual repair that would change a frozen asset mapping."""
    frozen_rows = conn.execute(
        "SELECT mapping.security_id, mapping.relation_type, mapping.direction, "
        "mapping.expected_gap, mapping.confidence, mapping.relevance, "
        "mapping.mapping_version FROM backtest_signal_securities AS frozen "
        "JOIN event_securities AS mapping ON mapping.mapping_id = frozen.mapping_id "
        "AND mapping.event_id = frozen.event_id "
        "AND mapping.security_id = frozen.security_id "
        "AND mapping.revision = frozen.mapping_revision "
        "WHERE frozen.ledger_signal_id = ?",
        (int(inherited_signal["id"]),),
    ).fetchall()
    frozen_real = {
        (
            str(row["security_id"]),
            str(row["relation_type"]),
            str(row["direction"]),
            _optional_mapping_number(row["expected_gap"]),
            float(row["confidence"]),
            float(row["relevance"]),
            str(row["mapping_version"] or ""),
        )
        for row in frozen_rows
    }
    fresh_real = set()
    article_text = " ".join((raw_title, logic, summary, mapped_theme))
    for item, row in _resolved_real_event_mapping_candidates(
        conn,
        article_text=article_text, source_text=raw_title,
        candidates=(
            signal.get("security_candidates")
            if isinstance(signal.get("security_candidates"), list)
            else []
        ),
    ):
        fresh_real.add(
            (
                str(row["security_id"]),
                str(item.get("relation_type") or "concept"),
                str(item.get("direction") or direction),
                _optional_mapping_number(item.get("expected_gap")),
                max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                max(0.0, min(10.0, float(item.get("relevance") or 0.0))),
                mapping_version,
            )
        )
    if fresh_real != frozen_real:
        raise EventCoreConflictError(
            "quality repair changes frozen real mappings "
            f"(before={_mapping_semantics_digest(frozen_real)}, "
            f"fresh={_mapping_semantics_digest(fresh_real)})"
        )

    frozen_market_rows = conn.execute(
        "SELECT DISTINCT mapping.security_id, mapping.relation_type, "
        "mapping.direction, mapping.expected_gap, mapping.confidence, "
        "mapping.relevance, mapping.mapping_version, mapping.event_id "
        "FROM event_evidence AS evidence JOIN event_securities AS mapping "
        "ON mapping.mapping_id = evidence.mapping_id "
        "AND mapping.event_id = evidence.event_id "
        "AND mapping.security_id = evidence.security_id "
        "AND mapping.revision = evidence.mapping_revision "
        "WHERE evidence.source_uri = ? AND mapping.relation_type = 'market_reference' "
        "AND mapping.is_current = 1",
        (str(signal.get("link") or ""),),
    ).fetchall()
    frozen_market = {
        (
            str(row["security_id"]),
            str(row["relation_type"]),
            str(row["direction"]),
            _optional_mapping_number(row["expected_gap"]),
            float(row["confidence"]),
            float(row["relevance"]),
            str(row["mapping_version"] or ""),
        )
        for row in frozen_market_rows
        if canonical_event_id(conn, str(row["event_id"]))
        == canonical_event_id(conn, event_id)
    }
    fresh_market = set()
    seen_market: set[str] = set()
    for raw in signal.get("market_references") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("asset_type") or "").casefold() != "index":
            continue
        if str(raw.get("reference_type") or "") != "explicit_mention":
            continue
        exchange = str(raw.get("exchange") or "").strip().upper()
        ticker = str(raw.get("ticker") or "").strip()
        name = str(raw.get("name") or "").strip()
        if (
            exchange not in {"SSE", "SZSE"}
            or not re.fullmatch(r"\d{6}", ticker)
            or not name
        ):
            continue
        symbol = ("sh" if exchange == "SSE" else "sz") + ticker
        if symbol in seen_market:
            continue
        seen_market.add(symbol)
        fresh_market.add(
            (
                symbol,
                "market_reference",
                direction,
                None,
                0.99,
                10.0,
                mapping_version,
            )
        )
    if fresh_market != frozen_market:
        raise EventCoreConflictError(
            "quality repair changes frozen market references "
            f"(before={_mapping_semantics_digest(frozen_market)}, "
            f"fresh={_mapping_semantics_digest(fresh_market)})"
        )


def _validate_quality_repair_predecessor(
    conn: sqlite3.Connection,
    *,
    signal: Dict[str, Any],
    inherited_signal: Optional[sqlite3.Row],
    resolved_event_id: str,
    direction: str,
    mapping_version: str,
    mapped_theme: str,
    backtest_symbol: Optional[str],
    mapping_confidence: float,
    mapping_relevance: float,
    expected_gap: Optional[float],
    raw_title: str,
    logic: str,
    summary: str,
) -> None:
    if (
        inherited_signal is None
        or str(inherited_signal["score_path"] or "") != "quality_quarantine"
        or str(inherited_signal["signal_stage"] or "") != "quality_quarantine"
    ):
        raise EventCoreConflictError(
            "quality repair requires the latest strategy revision to be quarantined"
        )
    inherited_event_id = str(inherited_signal["event_id"] or "").strip()
    inherited_event_version = int(inherited_signal["event_version"] or 0)
    supplied_event_id = str(signal.get("event_id") or "").strip()
    if (
        not inherited_event_id
        or inherited_event_version < 1
        or canonical_event_id(conn, inherited_event_id)
        != canonical_event_id(conn, supplied_event_id)
        or canonical_event_id(conn, inherited_event_id)
        != canonical_event_id(conn, resolved_event_id)
    ):
        raise EventCoreConflictError(
            "quality repair conflicts with frozen predecessor identity"
        )
    if bool(signal.get("is_similarity_copy")) != bool(
        inherited_signal["is_similarity_copy"]
    ):
        raise EventCoreConflictError(
            "quality repair changes similarity-copy identity"
        )
    if str(inherited_signal["mapping_version"] or "") != mapping_version:
        raise EventCoreConflictError("quality repair changes mapping version")
    if (str(inherited_signal["backtest_symbol"] or "").strip() or None) != backtest_symbol:
        raise EventCoreConflictError("quality repair changes backtest symbol")

    scalar_pairs = (
        ("mapping_confidence", float(inherited_signal["mapping_confidence"]), mapping_confidence),
        ("mapping_relevance", float(inherited_signal["mapping_relevance"]), mapping_relevance),
    )
    for field, frozen, fresh in scalar_pairs:
        if not math.isclose(frozen, fresh, rel_tol=0.0, abs_tol=1e-9):
            raise EventCoreConflictError(f"quality repair changes {field}")
    frozen_gap = _optional_mapping_number(inherited_signal["expected_gap"])
    if (
        (frozen_gap is None) != (expected_gap is None)
        or (
            frozen_gap is not None
            and not math.isclose(frozen_gap, expected_gap, rel_tol=0.0, abs_tol=1e-9)
        )
    ):
        raise EventCoreConflictError("quality repair changes expected_gap")

    event_mapping_id = str(inherited_signal["event_mapping_id"] or "").strip()
    if event_mapping_id:
        proxy = conn.execute(
            "SELECT event_id, security_id, relation_type, direction, expected_gap, "
            "confidence, relevance, mapping_version FROM event_securities "
            "WHERE mapping_id = ?",
            (event_mapping_id,),
        ).fetchone()
        if proxy is None:
            raise EventCoreConflictError("quality repair predecessor proxy is missing")
        proxy_gap = _optional_mapping_number(proxy["expected_gap"])
        if (
            canonical_event_id(conn, str(proxy["event_id"]))
            != canonical_event_id(conn, inherited_event_id)
            or str(proxy["security_id"] or "") != str(backtest_symbol or "")
            or str(proxy["relation_type"] or "") != "benchmark_proxy"
            or str(proxy["direction"] or "") != direction
            or str(proxy["mapping_version"] or "") != mapping_version
            or not math.isclose(float(proxy["confidence"]), mapping_confidence, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(float(proxy["relevance"]), mapping_relevance, rel_tol=0.0, abs_tol=1e-9)
            or (proxy_gap is None) != (expected_gap is None)
            or (
                proxy_gap is not None
                and not math.isclose(proxy_gap, expected_gap, rel_tol=0.0, abs_tol=1e-9)
            )
        ):
            raise EventCoreConflictError(
                "quality repair changes frozen proxy mapping"
            )
    elif backtest_symbol:
        # A symbol without a frozen proxy is valid for legacy rows only when it
        # was already represented that way by the predecessor.
        if str(inherited_signal["backtest_symbol"] or "").strip() != backtest_symbol:
            raise EventCoreConflictError("quality repair invents a proxy mapping")

    _quality_repair_real_mapping_semantics(
        conn,
        signal=signal,
        inherited_signal=inherited_signal,
        event_id=inherited_event_id,
        direction=direction,
        mapping_version=mapping_version,
        raw_title=raw_title,
        logic=logic,
        summary=summary,
        mapped_theme=mapped_theme,
    )


def _record_news_processing_signal_hint(link, strategy_version, signal_key) -> None:
    """Record an exact ledger hint; diagnostics cannot abort its transaction."""
    try:
        from services.news_processing_trace import record_news_processing_signal

        record_news_processing_signal(link, strategy_version, signal_key)
    except Exception:
        logger.warning("[NewsProcessing] signal association skipped", exc_info=False)


def _insert_backtest_signal_impl(conn: sqlite3.Connection, signal: Dict[str, Any]) -> bool:
    """Insert one immutable signal snapshot in the caller's transaction."""
    if not isinstance(signal, dict):
        raise TypeError("backtest_signal must be a mapping")
    link = str(signal.get("link") or "").strip()
    if not link:
        raise ValueError("backtest_signal.link is required")
    if _contains_unicode_replacement(signal):
        raise ValueError(
            "backtest signal persistence payload contains a Unicode replacement character"
        )
    strategy_version = str(signal.get("strategy_version") or "").strip()
    score = _finite_float(signal.get("score"), "backtest_signal.score")
    threshold = _finite_float(
        signal.get("aggregation_threshold"),
        "backtest_signal.aggregation_threshold",
    )
    if not 0.0 <= threshold <= 10.0:
        raise ValueError("backtest_signal.aggregation_threshold must be in 0..10")
    now = int(time.time())
    scored_at = int(signal.get("scored_at") or now)
    available_at = max(scored_at, int(signal.get("available_at") or scored_at))
    if scored_at <= 0 or available_at <= 0:
        raise ValueError("backtest signal timestamps must be positive")
    raw_published_at = signal.get("published_at")
    published_at = int(raw_published_at) if raw_published_at not in (None, "") else None
    event_key = str(signal.get("event_key") or "").strip()
    feed_url = str(signal.get("feed_url") or "").strip()
    raw_direction = str(signal.get("direction") or "中性").strip()
    # The immutable signal/export contract has exactly three directions.  Do
    # not let an unbounded or model-invented label reach event metadata or the
    # frozen security mapping; normalize it at the write boundary so a poison
    # value cannot abort the otherwise valid ledger/outbox transaction.
    direction = raw_direction if raw_direction in {"看多", "看空", "中性"} else "中性"
    mapping_version = str(signal.get("mapping_version") or "").strip()
    mapped_theme = str(signal.get("mapped_theme") or "").strip()
    backtest_symbol = str(signal.get("backtest_symbol") or "").strip() or None
    score_path = normalize_score_path(signal.get("score_path"))
    if not is_recognized_score_path(score_path):
        raise ValueError("backtest_signal.score_path is unsupported")
    signal_stage = str(
        signal.get("signal_stage") or score_path or "base"
    ).strip() or "base"
    auto_revision = bool(signal.get("auto_revision"))
    require_insert = bool(signal.get("require_insert"))
    quality_repair = signal_stage == "quality_repair"
    provenance_preserving_revision = bool(
        auto_revision
        and signal_stage in {"quality_quarantine", "quality_repair"}
    )
    _validate_safe_neutral_signal_payload(
        signal,
        score_path=score_path,
        score=score,
        direction=raw_direction,
    )
    if require_insert and not auto_revision:
        raise ValueError("required signal insertion must be an auto revision")
    if quality_repair and not (auto_revision and require_insert):
        raise ValueError(
            "quality repair requires an insert-required auto revision"
        )
    inherited_signal = None
    if auto_revision:
        inherited_signal = conn.execute(
            "SELECT * FROM backtest_signals "
            "WHERE link = ? AND strategy_version = ? "
            "ORDER BY available_at DESC, id DESC LIMIT 1",
            (link, strategy_version),
        ).fetchone()
        if inherited_signal is not None:
            _reject_non_substantive_path_transition(
                inherited_signal["score_path"],
                score_path,
                source="automated revision",
            )
            if (
                str(inherited_signal["score_path"] or "").strip()
                in NON_SUBSTANTIVE_SCORE_PATHS
            ):
                raise EventCoreConflictError(
                    "automated revision cannot inherit a non-substantive score"
                )
    if quality_repair and (
        inherited_signal is None
        or str(inherited_signal["score_path"] or "") != "quality_quarantine"
        or str(inherited_signal["signal_stage"] or "") != "quality_quarantine"
    ):
        raise EventCoreConflictError(
            "quality repair requires the latest strategy revision to be quarantined"
        )
    if provenance_preserving_revision:
        if inherited_signal is None:
            raise EventCoreConflictError(
                "quality maintenance revision requires a frozen predecessor"
            )
        inherited_event_id = str(inherited_signal["event_id"] or "").strip()
        supplied_event_id = str(signal.get("event_id") or "").strip()
        if not inherited_event_id or (
            supplied_event_id and supplied_event_id != inherited_event_id
        ):
            raise EventCoreConflictError(
                "quality maintenance revision changes frozen event identity"
            )
        resolved_event_id, resolved_event_version = (
            _current_canonical_event_identity(conn, inherited_event_id)
        )
    else:
        resolved_event_id, resolved_event_version = _resolve_article_event(
            conn,
            link=link,
            title=(
                signal.get("raw_title")
                or signal.get("event_title")
                or mapped_theme
                or event_key
                or link
            ),
            event_type=signal.get("event_type") or signal.get("tag") or "news",
            direction=direction,
            priority=score,
            occurred_at=published_at if published_at is not None else scored_at,
            strategy_version=strategy_version,
            model_version=signal.get("model_version") or signal.get("model_name") or "",
            supplied_event_id=signal.get("event_id") or "",
            observed_at=available_at,
            update_metadata=False,
        )
    if inherited_signal is not None:
        # Radar/cache revisions revise a frozen score decision.  A later event
        # merge changes the read-model root, but must not rewrite the immutable
        # ledger identity underneath the predecessor's mapping FKs.  Retain the
        # exact recorded event/version and let canonicalized readers project it
        # onto the audited merge target.
        inherited_event_id = str(inherited_signal["event_id"] or "").strip()
        inherited_event_version = int(inherited_signal["event_version"] or 0)
        if (
            not inherited_event_id
            or inherited_event_version < 1
            or canonical_event_id(conn, inherited_event_id) != resolved_event_id
        ):
            raise EventCoreConflictError(
                "automated revision conflicts with its frozen event identity"
            )
        event_id, event_version = inherited_event_id, inherited_event_version
    else:
        event_id, event_version = resolved_event_id, resolved_event_version
    raw_title = str(
        signal.get("raw_title")
        or signal.get("event_title")
        or mapped_theme
        or event_key
        or link
    )
    summary = str(signal.get("summary") or "")
    logic = str(signal.get("logic") or "")
    if not provenance_preserving_revision:
        _record_article_evidence(
            conn,
            event_id=event_id,
            link=link,
            raw_title=str(signal.get("raw_title") or ""),
            summary=summary,
            feed_url=feed_url,
            source_tier=str(signal.get("source_tier") or ""),
            announcement_at=published_at,
            observed_at=available_at,
        )
    revision_key = str(signal.get("revision_key") or "base").strip() or "base"
    independence_key = str(
        event_id or signal.get("independence_key") or event_key or link
    ).strip() or link
    selected = bool(signal.get("selected", score >= threshold))
    is_similarity_copy = bool(signal.get("is_similarity_copy")) or score_path == "similarity"
    # Score confidence describes the score/threshold decision and must never
    # be recycled as event-to-security mapping confidence.  Legacy snapshots
    # without an explicit mapping score remain unknown (0), rather than being
    # silently assigned a fabricated relationship strength.
    score_confidence = _finite_float(
        signal.get("score_confidence", 0.0) or 0.0,
        "backtest_signal.score_confidence",
    )
    score_confidence = max(0.0, min(1.0, score_confidence))
    mapping_confidence = _finite_float(
        signal.get("mapping_confidence", 0.0) or 0.0,
        "backtest_signal.mapping_confidence",
    )
    mapping_confidence = max(0.0, min(1.0, mapping_confidence))
    mapping_relevance = _finite_float(
        signal.get("mapping_relevance", 0.0) or 0.0,
        "backtest_signal.mapping_relevance",
    )
    mapping_relevance = max(0.0, min(10.0, mapping_relevance))
    raw_expected_gap = signal.get("expected_gap")
    expected_gap = (
        None
        if raw_expected_gap in (None, "")
        else _finite_float(raw_expected_gap, "backtest_signal.expected_gap")
    )
    if expected_gap is not None and not -100.0 <= expected_gap <= 100.0:
        raise ValueError("backtest_signal.expected_gap must be in -100..100")
    if quality_repair:
        _validate_quality_repair_predecessor(
            conn,
            signal=signal,
            inherited_signal=inherited_signal,
            resolved_event_id=resolved_event_id,
            direction=direction,
            mapping_version=mapping_version,
            mapped_theme=mapped_theme,
            backtest_symbol=backtest_symbol,
            mapping_confidence=mapping_confidence,
            mapping_relevance=mapping_relevance,
            expected_gap=expected_gap,
            raw_title=raw_title,
            logic=logic,
            summary=summary,
        )
        # A quality repair corrects text/model output inside the predecessor's
        # frozen policy contract.  It cannot silently adopt a newer feed
        # threshold or alter duplicate/canonical semantics.
        threshold = float(inherited_signal["aggregation_threshold"])
        selected = score >= threshold
        is_similarity_copy = bool(inherited_signal["is_similarity_copy"])
    if auto_revision:
        # Automated Radar/cache revisions change the score decision, not the
        # historical asset snapshot.  Copy only a snapshot that was already
        # frozen.  A legacy predecessor with no rows remains explicitly
        # unfrozen instead of being guessed from today's mappings.
        real_mapping_ids = (
            [
                str(row[0])
                for row in conn.execute(
                    "SELECT mapping_id FROM backtest_signal_securities "
                    "WHERE ledger_signal_id = ? ORDER BY mapping_id",
                    (int(inherited_signal["id"]),),
                ).fetchall()
            ]
            if inherited_signal is not None
            else []
        )
    else:
        real_mapping_ids = upsert_real_event_mappings(
            conn,
            event_id=event_id,
            article_text=" ".join((raw_title, logic, summary, mapped_theme)), source_text=str(signal.get("raw_title") or ""),
            source_uri=link,
            direction=direction,
            mapping_version=mapping_version,
            candidates=(
                signal.get("security_candidates")
                if isinstance(signal.get("security_candidates"), list)
                else []
            ),
            now=now,
        )
    if inherited_signal is not None:
        market_reference_mapping_ids = []
        event_mapping_id = inherited_signal["event_mapping_id"]
    else:
        market_reference_mapping_ids = upsert_event_market_references(
            conn,
            event_id=event_id,
            source_uri=link,
            direction=direction,
            mapping_version=mapping_version,
            references=(
                signal.get("market_references")
                if isinstance(signal.get("market_references"), list)
                else []
            ),
            now=now,
        )
        event_mapping_id = _upsert_event_proxy_mapping(
            conn,
            event_id=event_id,
            symbol=backtest_symbol,
            direction=direction,
            expected_gap=expected_gap,
            confidence=mapping_confidence,
            relevance=mapping_relevance,
            mapped_theme=mapped_theme,
            mapping_version=mapping_version,
            now=now,
        )
    state_payload = score_revision_payload(
        {
            "event_key": event_key,
            "event_id": event_id,
            "event_version": event_version,
            "event_mapping_id": event_mapping_id,
            "event_security_mapping_ids": real_mapping_ids,
            "event_market_reference_mapping_ids": market_reference_mapping_ids,
            "feed_url": feed_url,
            "score": score,
            "direction": direction,
            "score_confidence": score_confidence,
            "mapping_confidence": mapping_confidence,
            "mapping_relevance": mapping_relevance,
            "expected_gap": expected_gap,
            "mapping_version": mapping_version,
            "mapped_theme": mapped_theme,
            "backtest_symbol": backtest_symbol,
            "score_path": score_path,
            "signal_stage": signal_stage,
            "independence_key": independence_key,
            "aggregation_threshold": threshold,
            "selected": selected,
            "is_similarity_copy": is_similarity_copy,
        },
        signal,
    )
    revision_state_key = str(signal.get("revision_state_key") or "").strip()
    if not revision_state_key:
        revision_state_key = hashlib.sha256(state_payload.encode("utf-8")).hexdigest()
    try:
        revision_seq = int(signal.get("revision_seq") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("backtest_signal.revision_seq must be an integer") from exc
    if revision_seq < 0:
        raise ValueError("backtest_signal.revision_seq must be non-negative")
    if auto_revision:
        latest = inherited_signal
        if latest and str(latest["revision_state_key"] or "") == revision_state_key:
            return False
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(revision_seq), 0) FROM backtest_signals "
            "WHERE link = ? AND strategy_version = ?",
            (link, strategy_version),
        ).fetchone()
        revision_seq = int(seq_row[0] or 0) + 1
        revision_key = f"{signal_stage}:{revision_seq}"
    signal_key = _backtest_signal_key(link, strategy_version, revision_key)
    # Capture ``now`` after the write lock: waiting behind a long writer must
    # not backdate this durable observation into a pre-close T0 sample.
    available_at = max(available_at, now)
    cursor = conn.execute(
        """
        INSERT INTO backtest_signals (
            signal_key, event_key, event_id, event_version, event_mapping_id, link, feed_url, available_at, scored_at,
            published_at, recorded_at, score, direction, score_confidence,
            strategy_version, mapping_version, mapped_theme, backtest_symbol,
            mapping_confidence, mapping_relevance, expected_gap,
            score_path, revision_key, signal_stage, revision_seq,
            revision_state_key, independence_key, aggregation_threshold, selected,
            is_similarity_copy
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_key) DO NOTHING
        """,
        (
            signal_key,
            event_key,
            event_id,
            event_version,
            event_mapping_id,
            link,
            feed_url,
            available_at,
            scored_at,
            published_at,
            now,
            score,
            direction,
            score_confidence,
            strategy_version,
            mapping_version,
            mapped_theme,
            backtest_symbol,
            mapping_confidence,
            mapping_relevance,
            expected_gap,
            score_path,
            revision_key,
            signal_stage,
            revision_seq,
            revision_state_key,
            independence_key,
            threshold,
            1 if selected else 0,
            1 if is_similarity_copy else 0,
        ),
    )
    if cursor.rowcount <= 0:
        return False
    ledger_signal_id = int(cursor.lastrowid)
    write_ledger_score_explanation(conn, ledger_signal_id, signal)
    _freeze_backtest_signal_securities(
        conn,
        ledger_signal_id=ledger_signal_id,
        mapping_ids=real_mapping_ids,
        signal_available_at=available_at,
        captured_at=now,
    )
    if is_exportable_score_path(score_path) and not is_similarity_copy:
        enqueued = enqueue_signal_revision(
            conn,
            ledger_signal_id=ledger_signal_id,
            signal_revision_key=signal_key,
            signal={
                "event_key": event_key,
                "event_id": event_id,
                "event_version": event_version,
                "event_mapping_id": event_mapping_id,
                "event_security_mapping_ids": real_mapping_ids,
                "link": link,
                "available_at": available_at,
                "recorded_at": now,
                "score": score,
                "direction": direction,
                "score_confidence": score_confidence,
                "mapping_confidence": mapping_confidence,
                "mapping_relevance": mapping_relevance,
                "expected_gap": expected_gap,
                "strategy_version": strategy_version,
                "mapping_version": mapping_version,
                "mapped_theme": mapped_theme,
                "backtest_symbol": backtest_symbol,
                "score_path": score_path,
                "revision_key": revision_key,
                "signal_stage": signal_stage,
                "revision_seq": revision_seq,
                "revision_state_key": revision_state_key,
                "independence_key": independence_key,
                "aggregation_threshold": threshold,
                "selected": selected,
                "is_similarity_copy": is_similarity_copy,
            },
            enqueued_at=now,
        )
        if not enqueued:
            raise RuntimeError("signal revision Outbox row was not inserted")
    _record_news_processing_signal_hint(link, strategy_version, signal_key)
    return True


def _insert_backtest_signal(conn: sqlite3.Connection, signal: Dict[str, Any]) -> bool:
    """Atomically persist one signal and every mapping/Outbox side effect.

    Mapping revision creation precedes the ledger's unique-key insert because
    the frozen ``event_mapping_id`` is part of that row.  A duplicate replay
    must therefore roll the mapping revision back as well; otherwise an
    unaccepted signal could silently flip the Dashboard's current mapping or
    the sign of ``expected_gap``.
    """
    signal = prepare_signal_score_explanation(signal)
    savepoint = f"backtest_signal_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        inserted = _insert_backtest_signal_impl(conn, signal)
        if not inserted:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            check_score_explanation_replay(conn, signal)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return inserted
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        finally:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _commit_dashboard_inputs(conn: sqlite3.Connection, *, changed: bool = True) -> None:
    """Commit dashboard-visible writes and their publication obligation together."""
    if changed:
        from services.dashboard_snapshot_updates import mark_realtime_inputs_dirty

        mark_realtime_inputs_dirty(conn)
    conn.commit()


def store_backtest_signal(signal: Dict[str, Any]) -> bool:
    """Persist a signal when no ``news_cache`` row is desired.

    This is primarily used when rejected-entry caching is disabled.  It still
    writes a single local transaction and performs no network or model work.
    """
    with db_lock:
        with get_connection() as conn:
            cursor = conn.cursor()
            _init_backtest_signals(cursor)
            init_score_explanation_storage(cursor)
            init_event_core_schema(conn)
            _init_backtest_signal_securities(cursor)
            init_integration_signal_outbox(conn)
            # Schema setup is independently idempotent; start the business
            # write only after it has committed so BEGIN IMMEDIATE is valid.
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            inserted = _insert_backtest_signal(conn, signal)
            _commit_dashboard_inputs(conn, changed=inserted)
            return inserted


def get_backtest_signals(
    *,
    cutoff: int,
    strategy_version: Optional[str] = None,
    mapping_version: Optional[str] = None,
    min_score: Optional[float] = None,
    selected_only: bool = True,
    include_similarity: bool = False,
) -> List[Dict[str, Any]]:
    """Return the final immutable revision without applying current feed policy."""
    where = ["available_at >= ?"]
    params: List[Any] = [int(cutoff)]
    if strategy_version is not None:
        where.append("strategy_version = ?")
        params.append(str(strategy_version))
    if mapping_version is not None:
        where.append("mapping_version = ?")
        params.append(str(mapping_version))
    with db_lock:
        with get_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM backtest_signals WHERE "
                    + " AND ".join(where)
                    + " ORDER BY available_at ASC, id ASC",
                    tuple(params),
                ).fetchall()
                rows = _canonicalize_backtest_rows(conn, rows)
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    return []
                raise
    # Multiple immutable rows can describe one signal as post-score production
    # stages (currently Radar) revise it.  Resolve the latest effective revision
    # before applying selection; filtering first could incorrectly resurrect an
    # older selected revision after a later policy decision.
    latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        latest[(str(row.get("link") or ""), str(row.get("strategy_version") or ""))] = row
    effective = list(latest.values())
    if not include_similarity:
        # Independence is revision state.  A later Radar decision may mark a
        # previously independent base row as a non-canonical cluster member, so
        # filtering before resolving the latest revision would resurrect it.
        effective = [row for row in effective if not bool(row.get("is_similarity_copy"))]
    if min_score is not None:
        threshold = float(min_score)
        effective = [row for row in effective if float(row.get("score") or 0) >= threshold]
    elif selected_only:
        # The selection bit and threshold were captured when the final revision
        # became available; never reinterpret it using today's feed policy.
        effective = [row for row in effective if bool(row.get("selected"))]
    return sorted(
        effective,
        key=lambda row: (int(row.get("available_at") or 0), int(row.get("id") or 0)),
    )


def _canonicalize_backtest_rows(
    conn: sqlite3.Connection,
    rows: List[Any],
) -> List[Dict[str, Any]]:
    """Resolve stored event UUIDs through audited merges for consumers.

    The immutable ledger keeps the point-in-time source UUID.  Read models use
    the current canonical root so Briefing/Radar/backtest cannot each invent a
    separate post-merge cluster.  The original UUID remains available as
    ``recorded_event_id`` in the returned in-memory row.
    """
    canonical_cache: Dict[str, Tuple[str, int]] = {}
    resolved: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        stored = str(row.get("event_id") or "").strip()
        if not stored:
            resolved.append(row)
            continue
        if stored not in canonical_cache:
            root = canonical_event_id(conn, stored)
            version_row = conn.execute(
                "SELECT version FROM events WHERE event_id = ?", (root,)
            ).fetchone()
            if version_row is None:
                raise RuntimeError("backtest signal references a missing canonical event")
            canonical_cache[stored] = (root, int(version_row[0]))
        root, version = canonical_cache[stored]
        row["recorded_event_id"] = stored
        row["recorded_event_version"] = int(row.get("event_version") or 0)
        row["event_id"] = root
        row["event_version"] = version
        row["independence_key"] = root
        resolved.append(row)
    return resolved


def get_backtest_signal_stats(
    strategy_version: Optional[str] = None,
    mapping_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Return independently scored ledger coverage and collection start.

    Similarity copies are retained for auditability but are not new model
    observations, so neither the history switch nor the displayed scored-sample
    count may treat them as independent signals.
    """
    clauses: List[str] = []
    params_list: List[Any] = []
    if strategy_version is not None:
        clauses.append("strategy_version = ?")
        params_list.append(str(strategy_version))
    if mapping_version is not None:
        clauses.append("mapping_version = ?")
        params_list.append(str(mapping_version))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params = tuple(params_list)
    with db_lock:
        with get_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT link, strategy_version, available_at, id, "
                    "is_similarity_copy, score_path FROM backtest_signals" + where
                    + " ORDER BY available_at ASC, id ASC",
                    params,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    rows = []
                else:
                    raise
            try:
                started_row = conn.execute(
                    "SELECT v FROM app_meta WHERE k = 'backtest_signals_started_at'"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    started_row = None
                else:
                    raise
    substantive_rows = [
        dict(raw_row)
        for raw_row in rows
        if (
            normalize_score_path(raw_row["score_path"])
            in RECOGNIZED_SCORE_PATHS
            and normalize_score_path(raw_row["score_path"])
            not in NON_SUBSTANTIVE_SCORE_PATHS
        )
    ]
    latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw_row in rows:
        signal_row = dict(raw_row)
        latest[(str(signal_row.get("link") or ""), str(signal_row.get("strategy_version") or ""))] = signal_row
    independent = [
        signal_row
        for signal_row in latest.values()
        if (
            not bool(signal_row.get("is_similarity_copy"))
            and normalize_score_path(signal_row.get("score_path"))
            in RECOGNIZED_SCORE_PATHS
            and normalize_score_path(signal_row.get("score_path"))
            not in NON_SUBSTANTIVE_SCORE_PATHS
        )
    ]
    return {
        "count": len(independent),
        "revision_count": len(substantive_rows),
        "first_available_at": min(
            (int(signal_row.get("available_at") or 0) for signal_row in independent),
            default=0,
        ),
        "last_available_at": max(
            (int(signal_row.get("available_at") or 0) for signal_row in independent),
            default=0,
        ),
        "collection_started_at": int(started_row[0] or 0) if started_row else 0,
    }


def get_backtest_base_scores(
    links: List[str],
    strategy_version: str,
) -> Dict[str, float]:
    """Return frozen pre-revision scores for Radar without per-link queries."""
    unique_links = [str(item).strip() for item in dict.fromkeys(links) if str(item).strip()]
    if not unique_links:
        return {}
    # Stay below SQLite's common 999-variable limit.  Refresh already caps feed
    # output, but chunking keeps this helper safe for future callers.
    result: Dict[str, float] = {}
    with db_lock:
        with get_connection() as conn:
            for offset in range(0, len(unique_links), 800):
                chunk = unique_links[offset:offset + 800]
                placeholders = ",".join("?" for _ in chunk)
                try:
                    rows = conn.execute(
                        "SELECT link, score FROM backtest_signals "
                        "WHERE strategy_version = ? AND revision_key = 'base' "
                        "AND is_similarity_copy = 0 AND link IN (" + placeholders + ")",
                        (str(strategy_version), *chunk),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        return {}
                    raise
                for row in rows:
                    result[str(row["link"])] = float(row["score"])
    return result


def get_backtest_signal_states(
    links: List[str],
    strategy_version: str,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return frozen base and latest revisions for each requested link."""
    unique_links = [str(item).strip() for item in dict.fromkeys(links) if str(item).strip()]
    if not unique_links:
        return {}
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with db_lock:
        with get_connection() as conn:
            for offset in range(0, len(unique_links), 800):
                chunk = unique_links[offset:offset + 800]
                placeholders = ",".join("?" for _ in chunk)
                try:
                    rows = conn.execute(
                        "SELECT * FROM backtest_signals "
                        "WHERE strategy_version = ? AND link IN (" + placeholders + ") "
                        "ORDER BY available_at ASC, id ASC",
                        (str(strategy_version), *chunk),
                    ).fetchall()
                    rows = _canonicalize_backtest_rows(conn, rows)
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        return {}
                    raise
                for raw_row in rows:
                    row = dict(raw_row)
                    link = str(row.get("link") or "")
                    state = result.setdefault(link, {})
                    if str(row.get("revision_key") or "base") == "base" and "base" not in state:
                        state["base"] = row
                    state["latest"] = row
    return result


def get_backtest_cluster_signal_states(
    independence_keys: List[str],
    strategy_version: str,
) -> Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]:
    """Return effective member states for durable Radar cluster identities.

    Current refresh entries are not the complete cluster population: an older
    canonical may already have fallen out of every source feed while a retained
    copy is still present.  Resolve every link that has used one of the requested
    identities, then keep only links whose *latest* revision still belongs to
    that identity.
    """
    keys = [str(item).strip() for item in dict.fromkeys(independence_keys) if str(item).strip()]
    if not keys:
        return {}
    states_by_link: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with db_lock:
        with get_connection() as conn:
            expanded_keys: List[str] = []
            for key in keys:
                try:
                    expanded_keys.extend(canonical_event_group(conn, key))
                except (KeyError, EventCoreConflictError, ValueError):
                    # A broken persisted reference is not a title-clustering
                    # fallback.  Leave it absent so Radar fails closed.
                    continue
            expanded_keys = list(dict.fromkeys(expanded_keys))
            for offset in range(0, len(expanded_keys), 700):
                chunk = expanded_keys[offset:offset + 700]
                placeholders = ",".join("?" for _ in chunk)
                try:
                    rows = conn.execute(
                        "SELECT * FROM backtest_signals "
                        "WHERE strategy_version = ? AND link IN ("
                        "SELECT DISTINCT link FROM backtest_signals "
                        "WHERE strategy_version = ? AND independence_key IN ("
                        + placeholders
                        + ")) ORDER BY available_at ASC, id ASC",
                        (str(strategy_version), str(strategy_version), *chunk),
                    ).fetchall()
                    rows = _canonicalize_backtest_rows(conn, rows)
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        return {}
                    raise
                for raw_row in rows:
                    row = dict(raw_row)
                    link = str(row.get("link") or "")
                    state = states_by_link.setdefault(link, {})
                    if str(row.get("revision_key") or "base") == "base" and "base" not in state:
                        state["base"] = row
                    state["latest"] = row

    requested = set(keys)
    result: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
    for link, state in states_by_link.items():
        latest = state.get("latest") or {}
        identity = str(latest.get("independence_key") or "")
        if identity in requested:
            result.setdefault(identity, {})[link] = state
    return result


def update_cached_entry_score_state(
    link: str,
    *,
    title: str,
    score: float,
    score_path: str,
    direction: str,
    score_confidence: float,
    backtest_signal: Dict[str, Any],
) -> bool:
    """Atomically update Radar-visible cache state and its immutable ledger row.

    The narrow UPDATE deliberately preserves the frozen base tag, logic, model
    metadata, publication timestamp and summary.  Radar changes only the score
    state; overwriting those fields would make a later no-bonus rollback
    impossible and could silently remap the historical asset proxy.
    """
    return bool(
        update_cached_entry_score_states(
            [
                {
                    "link": link,
                    "title": title,
                    "score": score,
                    "score_path": score_path,
                    "direction": direction,
                    "score_confidence": score_confidence,
                    "backtest_signal": backtest_signal,
                    "update_cache": True,
                }
            ]
        )
    )


def update_cached_entry_score_states(updates: List[Dict[str, Any]]) -> int:
    """Atomically persist every cache/ledger state transition in one group.

    Radar decisions are cluster-wide: committing one member before another can
    temporarily expose two effective canonicals, and a process crash can make
    that transient state permanent.  This batch primitive validates all input,
    updates current cache rows, and appends every immutable ledger revision in
    one SQLite transaction.  Ledger-only rows are allowed for an old canonical
    whose seven-day cache row has already expired.
    """
    def normalized_state(
        *,
        score: Any,
        score_path: Any,
        direction: Any,
        score_confidence: Any,
        prefix: str,
    ) -> Tuple[float, str, str, float]:
        normalized_direction = str(direction or "中性").strip()
        if normalized_direction not in {"看多", "看空", "中性"}:
            raise ValueError(f"{prefix}.direction is invalid")
        confidence = _finite_float(
            score_confidence or 0.0,
            f"{prefix}.score_confidence",
        )
        normalized_path = normalize_score_path(score_path)
        if not is_recognized_score_path(normalized_path):
            raise ValueError(f"{prefix}.score_path is unsupported")
        return (
            _finite_float(score, f"{prefix}.score"),
            normalized_path,
            normalized_direction,
            max(0.0, min(1.0, confidence)),
        )

    def states_match(
        left: Tuple[float, str, str, float],
        right: Tuple[float, str, str, float],
    ) -> bool:
        return (
            math.isclose(left[0], right[0], rel_tol=0.0, abs_tol=1e-9)
            and left[1:] == right[1:]
        )

    normalized: List[Dict[str, Any]] = []
    seen_links: set[str] = set()
    for raw_update in updates or []:
        if not isinstance(raw_update, dict):
            raise TypeError("score state update must be a mapping")
        link = str(raw_update.get("link") or "").strip()
        if not link:
            raise ValueError("link is required")
        if link in seen_links:
            raise ValueError("score state batch contains a duplicate link")
        seen_links.add(link)
        backtest_signal = raw_update.get("backtest_signal")
        if not isinstance(backtest_signal, dict):
            raise TypeError("backtest_signal must be a mapping")
        stable_signal = dict(backtest_signal)
        if str(stable_signal.get("link") or "").strip() != link:
            raise ValueError("cache and backtest signal links must match")
        if not bool(stable_signal.get("auto_revision")):
            raise ValueError("score state updates require an auto revision")
        signal_state = normalized_state(
            score=stable_signal.get("score"),
            score_path=stable_signal.get("score_path"),
            direction=stable_signal.get("direction"),
            score_confidence=stable_signal.get("score_confidence", 0.0),
            prefix="backtest_signal",
        )
        update_cache = bool(raw_update.get("update_cache", True))
        item: Dict[str, Any] = {
            "link": link,
            "backtest_signal": stable_signal,
            "signal_state": signal_state,
            "update_cache": update_cache,
        }
        if update_cache:
            cache_state = normalized_state(
                score=raw_update.get("score"),
                score_path=raw_update.get("score_path"),
                direction=raw_update.get("direction"),
                score_confidence=raw_update.get("score_confidence", 0.0),
                prefix="news_cache",
            )
            if not states_match(cache_state, signal_state):
                raise ValueError("cache and backtest signal score states differ")
            item.update(
                {
                    "title": str(raw_update.get("title") or ""),
                    "score": cache_state[0],
                    "score_path": cache_state[1],
                    "direction": cache_state[2],
                    "score_confidence": cache_state[3],
                }
            )
        normalized.append(item)
    if not normalized:
        return 0

    inserted = 0
    with db_lock:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in normalized:
                    signal = item["backtest_signal"]
                    signal_state = item["signal_state"]
                    strategy_version = str(
                        signal.get("strategy_version") or ""
                    ).strip()
                    latest_signal = conn.execute(
                        "SELECT * "
                        "FROM backtest_signals WHERE link = ? "
                        "AND strategy_version = ? "
                        "ORDER BY available_at DESC, id DESC LIMIT 1",
                        (item["link"], strategy_version),
                    ).fetchone()
                    cache_row = None
                    if item["update_cache"]:
                        cache_row = conn.execute(
                            "SELECT * FROM news_cache "
                            "WHERE link = ?",
                            (item["link"],),
                        ).fetchone()
                        if cache_row is None:
                            raise ValueError(
                                f"cached entry does not exist: {item['link']}"
                            )
                        if str(cache_row["strategy_version"] or "").strip() != (
                            strategy_version
                        ):
                            raise EventCoreConflictError(
                                "cache and backtest signal strategy versions differ"
                            )
                        _reject_non_substantive_path_transition(
                            cache_row["score_path"],
                            signal_state[1],
                            source="score state cache update",
                        )
                    if latest_signal is not None:
                        _reject_non_substantive_path_transition(
                            latest_signal["score_path"],
                            signal_state[1],
                            source="score state ledger update",
                        )

                    bind_revised_score_explanation(item, cache_row, latest_signal)
                    inserted_now = _insert_backtest_signal(conn, signal)
                    if not inserted_now:
                        latest_after = conn.execute(
                            "SELECT score, score_path, direction, score_confidence "
                            "FROM backtest_signals WHERE link = ? "
                            "AND strategy_version = ? "
                            "ORDER BY available_at DESC, id DESC LIMIT 1",
                            (item["link"], strategy_version),
                        ).fetchone()
                        if latest_after is None or not states_match(
                            normalized_state(
                                score=latest_after["score"],
                                score_path=latest_after["score_path"],
                                direction=latest_after["direction"],
                                score_confidence=latest_after["score_confidence"],
                                prefix="persisted_signal",
                            ),
                            signal_state,
                        ):
                            raise EventCoreConflictError(
                                "idempotent ledger replay changes score state"
                            )
                        if item["update_cache"]:
                            check_cached_score_explanation_replay(cache_row, signal)
                            cache_state = normalized_state(
                                score=cache_row["score"],
                                score_path=cache_row["score_path"],
                                direction=cache_row["direction"],
                                score_confidence=cache_row["score_confidence"],
                                prefix="persisted_cache",
                            )
                            if (
                                not states_match(cache_state, signal_state)
                                or str(cache_row["title"] or "") != item["title"]
                            ):
                                raise EventCoreConflictError(
                                    "idempotent ledger replay cannot change cache state"
                                )
                        continue

                    if item["update_cache"]:
                        update_revised_cache_score(conn, item)
                    inserted += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    return inserted


def set_cached_entry(
    link: str, title: str, raw_title: str, score: float,
    tag: str, logic: str, summary: str = "", feed_url: str = "",
    score_path: str = "", prompt_key: str = "", model_name: str = "",
    latency_ms: int = 0, error_type: str = "", source_tier: str = "",
    strategy_version: str = "", stage1_score: Optional[float] = None,
    safe_neutral_reason: str = "",
    stage1_model: str = "", stage2_score: Optional[float] = None,
    stage2_model: str = "", score_confidence: float = 0.0, review_reason: str = "",
    direction: str = "中性", created_at: Optional[int] = None,
    backtest_signal: Optional[Dict[str, Any]] = None,
    score_explanation: Optional[Dict[str, Any]] = None,
) -> Tuple[str, int]:
    score_path = normalize_score_path(score_path)
    if score_path and not is_recognized_score_path(score_path):
        raise ValueError("news_cache.score_path is unsupported")
    direction = str(direction or "中性").strip() or "中性"
    model_name = str(model_name or "").strip()
    error_type, safe_neutral_reason = _normalize_cache_labels(error_type, safe_neutral_reason)
    stage1_model = str(stage1_model or "").strip()
    stage2_model = str(stage2_model or "").strip()
    _reject_replacement_chars(title, raw_title, tag, logic, summary)
    with db_lock:
        with get_connection() as conn:
            cached_at = int(created_at or time.time())
            observed_at = int(time.time())
            stable_signal = (
                dict(backtest_signal) if backtest_signal is not None else None
            )
            explanation = bind_cache_score_explanation(
                score_explanation, stable_signal, score=score, score_path=score_path
            )
            if stable_signal is not None:
                stable_signal["score_path"] = normalize_score_path(
                    stable_signal.get("score_path")
                )
                if not is_recognized_score_path(stable_signal["score_path"]):
                    raise ValueError("backtest_signal.score_path is unsupported")
            quality_repair = bool(
                stable_signal
                and stable_signal.get("auto_revision")
                and stable_signal.get("require_insert")
                and str(stable_signal.get("signal_stage") or "").strip()
                == "quality_repair"
            )
            repair_is_copy = False
            if quality_repair:
                # Preserve the quality-repair API's domain-specific rejection
                # before applying the generic cache/ledger contract below.
                repair_is_copy = _validate_quality_repair_cache_payload(
                    link=link,
                    raw_title=raw_title,
                    score=score,
                    tag=tag,
                    logic=logic,
                    summary=summary,
                    score_path=score_path,
                    model_name=model_name,
                    error_type=error_type,
                    strategy_version=strategy_version,
                    stage1_model=stage1_model,
                    stage2_model=stage2_model,
                    score_confidence=score_confidence,
                    direction=direction,
                    signal=stable_signal,
                )
            _validate_cache_signal_score_contract(
                score_path=score_path,
                score=score,
                tag=tag,
                logic=logic,
                direction=direction,
                score_confidence=score_confidence,
                model_name=model_name,
                error_type=error_type,
                stage1_score=stage1_score,
                stage1_model=stage1_model,
                stage2_score=stage2_score,
                stage2_model=stage2_model,
                signal=stable_signal,
            )
            conn.execute("BEGIN IMMEDIATE")
            existing_cache_path = conn.execute(
                "SELECT * FROM news_cache WHERE link = ?",
                (link,),
            ).fetchone()
            explanation = retain_unchanged_score_explanation(
                conn, explanation, stable_signal, existing_cache_path,
                score=score, score_path=score_path, strategy_version=strategy_version,
                direction=direction, score_confidence=float(score_confidence or 0.0),
                raw_title=raw_title, tag=tag, logic=logic, summary=summary,
                model_name=model_name, stage1_score=stage1_score, stage1_model=stage1_model,
                stage2_score=stage2_score, stage2_model=stage2_model, review_reason=review_reason,
            )
            if (
                existing_cache_path is not None
                and str(existing_cache_path["strategy_version"] or "")
                == str(strategy_version or "")
            ):
                _reject_non_substantive_path_transition(
                    existing_cache_path["score_path"],
                    score_path,
                    source="news cache upsert",
                )
            latest_signal_path = conn.execute(
                "SELECT score_path FROM backtest_signals "
                "WHERE link = ? AND strategy_version = ? "
                "ORDER BY available_at DESC, id DESC LIMIT 1",
                (link, str(strategy_version or "")),
            ).fetchone()
            if latest_signal_path is not None:
                _reject_non_substantive_path_transition(
                    latest_signal_path["score_path"],
                    score_path,
                    source="cache/ledger replay",
                )
            quarantined_identity: Optional[Tuple[str, int]] = None
            if quality_repair:
                quarantined = conn.execute(
                    "SELECT raw_title, summary, score_path, strategy_version, "
                    "event_id, event_version FROM news_cache WHERE link = ?",
                    (link,),
                ).fetchone()
                if (
                    quarantined is None
                    or str(quarantined["score_path"] or "")
                    != "quality_quarantine"
                    or str(quarantined["strategy_version"] or "")
                    == str(strategy_version or "")
                ):
                    raise EventCoreConflictError(
                        "quality repair requires an exact quarantined cache row"
                    )
                if (
                    str(quarantined["raw_title"] or "") != str(raw_title or "")
                    or str(quarantined["summary"] or "") != str(summary or "")
                ):
                    raise EventCoreConflictError(
                        "quality repair source snapshot changed after quarantine"
                    )
                quarantine_event_id = str(quarantined["event_id"] or "").strip()
                quarantine_event_version = int(
                    quarantined["event_version"] or 0
                )
                signal_event_id = str(stable_signal.get("event_id") or "").strip()
                signal_event_version = int(
                    stable_signal.get("event_version") or 0
                )
                if (
                    not quarantine_event_id
                    or quarantine_event_version < 1
                    or signal_event_id != quarantine_event_id
                    or signal_event_version != quarantine_event_version
                ):
                    raise EventCoreConflictError(
                        "quality repair conflicts with quarantined event identity"
                    )
                predecessor = conn.execute(
                    "SELECT score_path, signal_stage, is_similarity_copy "
                    "FROM backtest_signals WHERE link = ? AND strategy_version = ? "
                    "ORDER BY available_at DESC, id DESC LIMIT 1",
                    (link, str(stable_signal.get("strategy_version") or "")),
                ).fetchone()
                if (
                    predecessor is None
                    or str(predecessor["score_path"] or "")
                    != "quality_quarantine"
                    or str(predecessor["signal_stage"] or "")
                    != "quality_quarantine"
                ):
                    raise EventCoreConflictError(
                        "quality repair requires the latest strategy revision "
                        "to be quarantined"
                    )
                if bool(predecessor["is_similarity_copy"]) != repair_is_copy:
                    raise EventCoreConflictError(
                        "quality repair changes frozen direct/copy semantics"
                    )
                quarantined_identity = (
                    quarantine_event_id,
                    quarantine_event_version,
                )

            if quarantined_identity is not None:
                resolved_event_id, resolved_event_version = (
                    _current_canonical_event_identity(
                        conn, quarantined_identity[0]
                    )
                )
            else:
                resolved_event_id, resolved_event_version = _resolve_article_event(
                    conn,
                    link=link,
                    title=raw_title or title,
                    event_type=tag or "news",
                    direction=direction,
                    priority=score,
                    occurred_at=cached_at,
                    strategy_version=strategy_version,
                    model_version=model_name,
                    supplied_event_id=(stable_signal or {}).get("event_id") or "",
                    observed_at=observed_at,
                )
            # The mutable cache is a current read model, so it follows the
            # resolved event's current version.  The immutable repair ledger
            # separately inherits its predecessor's frozen event/version.
            event_id, event_version = resolved_event_id, resolved_event_version
            upsert_explained_cache_entry(
                conn,
                (
                    link, title, raw_title, score, tag, logic, cached_at, summary, feed_url,
                    score_path, prompt_key, model_name, int(latency_ms or 0), error_type, source_tier,
                    strategy_version, stage1_score, stage1_model, stage2_score, stage2_model,
                    float(score_confidence or 0.0), review_reason, direction,
                    event_id, event_version, safe_neutral_reason,
                )
            )
            if not quality_repair:
                _upsert_article_source(conn, link, feed_url)
                _record_article_evidence(
                    conn,
                    event_id=event_id,
                    link=link,
                    raw_title=raw_title,
                    summary=summary,
                    feed_url=feed_url,
                    source_tier=source_tier,
                    announcement_at=cached_at,
                    observed_at=observed_at,
                )
            if stable_signal is not None:
                if not quality_repair:
                    stable_signal["event_id"] = event_id
                    stable_signal["event_version"] = event_version
                stable_signal.setdefault("raw_title", raw_title)
                stable_signal.setdefault("event_type", tag or "news")
                if stable_signal.get("auto_revision"):
                    for transient_key in (
                        "id",
                        "signal_key",
                        "recorded_at",
                        "revision_state_key",
                        "revision_seq",
                    ):
                        stable_signal.pop(transient_key, None)
                inserted = _insert_backtest_signal(conn, stable_signal)
                if stable_signal.get("require_insert") and not inserted:
                    raise RuntimeError(
                        "required backtest signal revision was not inserted"
                    )
            write_cache_score_explanation(conn, link, explanation, stable_signal)
            _commit_dashboard_inputs(conn)
            return event_id, event_version


def _new_dashboard_article_source(conn: sqlite3.Connection, link: str, feed_url: str) -> bool:
    from services.dashboard_snapshots import snapshots_enabled

    if not snapshots_enabled() or not str(feed_url or "").strip():
        return False
    return bool(conn.execute(
        "SELECT 1 FROM news_cache n WHERE n.link=? AND NOT EXISTS "
        "(SELECT 1 FROM article_sources s WHERE s.link=n.link AND s.feed_url=?)",
        (str(link or "").strip(), str(feed_url).strip()),
    ).fetchone())


def _cached_origin_would_change(
    conn: sqlite3.Connection, link: str, created_at: Optional[int], feed_url: str,
) -> bool:
    from services.dashboard_snapshots import snapshots_enabled

    if not snapshots_enabled():
        return False
    row = conn.execute(
        "SELECT created_at,feed_url FROM news_cache WHERE link=?", (link,)
    ).fetchone()
    if row is None:
        return False
    if created_at is not None and row[0] != int(created_at):
        return True
    if feed_url and not str(row[1] or "").strip():
        return True
    return _new_dashboard_article_source(conn, link, feed_url)


def update_cached_entry_origin(
    link: str,
    *,
    created_at: Optional[int] = None,
    feed_url: str = "",
    backtest_selection: Optional[Dict[str, Any]] = None,
) -> None:
    """Refresh trusted RSS metadata and atomically upgrade cached selection.

    A score may first be seen on a strict feed and later reused by a looser
    source.  Once the frozen score becomes aggregation-eligible it is a real
    point-in-time signal; record that transition in the same transaction as the
    source sighting.  Selection upgrades are monotonic and never retract an
    already-issued signal merely because another feed has a stricter policy.
    """
    updates: List[str] = []
    params: List[Any] = []
    if created_at is not None:
        updates.append("created_at = ?")
        params.append(int(created_at))
    if feed_url:
        # feed_url is the stable compatibility/primary source. A later
        # sighting is stored in article_sources but must not relabel existing
        # SSE/Briefing rows as whichever source happened to run last.
        updates.append(
            "feed_url = CASE WHEN TRIM(COALESCE(feed_url, '')) = '' "
            "THEN ? ELSE feed_url END"
        )
        params.append(str(feed_url))
    if not updates and not backtest_selection:
        return
    if updates:
        params.append(link)
    with db_lock:
        with get_connection() as conn:
            changed = _cached_origin_would_change(conn, link, created_at, feed_url)
            if updates:
                conn.execute(
                    f"UPDATE news_cache SET {', '.join(updates)} WHERE link = ?",
                    tuple(params),
                )
            if feed_url:
                _upsert_article_source(conn, link, feed_url)
            selection = backtest_selection or {}
            strategy_version = str(selection.get("strategy_version") or "").strip()
            if selection:
                threshold = _finite_float(
                    selection.get("aggregation_threshold"),
                    "backtest_selection.aggregation_threshold",
                )
                if not 0.0 <= threshold <= 10.0:
                    raise ValueError("backtest_selection.aggregation_threshold must be in 0..10")
                latest = conn.execute(
                    "SELECT * FROM backtest_signals WHERE link = ? AND strategy_version = ? "
                    "ORDER BY available_at DESC, id DESC LIMIT 1",
                    (str(link), strategy_version),
                ).fetchone()
                if (
                    latest
                    and not bool(latest["selected"])
                    and not bool(latest["is_similarity_copy"])
                    and float(latest["score"]) >= threshold
                ):
                    revision = dict(latest)
                    for transient_key in (
                        "id", "signal_key", "recorded_at", "revision_state_key",
                    ):
                        revision.pop(transient_key, None)
                    now = int(time.time())
                    revision.update({
                        "feed_url": str(feed_url or revision.get("feed_url") or ""),
                        "available_at": now,
                        "aggregation_threshold": threshold,
                        "selected": True,
                        "revision_key": "cache_selection:auto",
                        "signal_stage": "cache_selection",
                        "auto_revision": True,
                    })
                    changed = _insert_backtest_signal(conn, revision) or changed
            _commit_dashboard_inputs(conn, changed=changed)


def update_cached_entry_origins(
    origins: List[Tuple[str, int, str]],
) -> int:
    """Bulk-reconcile trusted publication/source metadata in one transaction."""
    rows = [
        (int(created_at), str(feed_url), str(link))
        for link, created_at, feed_url in origins
        if link and feed_url
    ]
    if not rows:
        return 0
    with db_lock:
        with get_connection() as conn:
            changed = any(
                _cached_origin_would_change(conn, link, created_at, feed_url)
                for created_at, feed_url, link in rows
            )
            before = conn.total_changes
            conn.executemany(
                "UPDATE news_cache SET created_at = ?, "
                "feed_url = CASE WHEN TRIM(COALESCE(feed_url, '')) = '' "
                "THEN ? ELSE feed_url END WHERE link = ?",
                rows,
            )
            updated = int(conn.total_changes - before)
            observed_at = int(time.time())
            for _created_at, feed_url, link in rows:
                _upsert_article_source(conn, link, feed_url, observed_at)
            _commit_dashboard_inputs(conn, changed=changed)
            # Preserve the legacy return contract: count only news_cache
            # updates, not additive article_sources writes.
            return updated


def get_recent_entries(
    hours: Optional[int] = None,
    cfg: Optional[Dict] = None,
    feed_url: Optional[str] = None,
) -> List[Tuple]:
    """Return similarity candidates from the exact effective score contract."""
    hours = hours or (cfg["similarity_window_hours"] if cfg else 72)
    cutoff = int(time.time()) - hours * 3600
    candidate_paths = tuple(sorted(
        RECOGNIZED_SCORE_PATHS - NON_EXPORTABLE_SCORE_PATHS
    ))
    candidate_placeholders = ",".join("?" for _ in candidate_paths)
    candidate_clause = (
        " AND LOWER(TRIM(COALESCE(score_path, ''))) IN "
        f"({candidate_placeholders})"
    )
    with db_lock:
        with get_connection() as conn:
            params: List[Any] = [cutoff]
            strategy_clause = _exact_strategy_where_clause(cfg, params)
            if feed_url:
                feed_urls = equivalent_feed_urls(cfg, feed_url)
                placeholders = ",".join("?" for _ in feed_urls)
                rows = conn.execute(
                    "SELECT link, title, raw_title, score, tag, logic, direction "
                    f"FROM news_cache WHERE created_at > ? AND "
                    f"(feed_url IN ({placeholders}) OR EXISTS ("
                    "SELECT 1 FROM article_sources AS source_sighting "
                    "WHERE source_sighting.link = news_cache.link "
                    f"AND source_sighting.feed_url IN ({placeholders})))"
                    f"{candidate_clause}"
                    f"{strategy_clause} ORDER BY created_at DESC LIMIT 200",
                    (
                        cutoff,
                        *feed_urls,
                        *feed_urls,
                        *candidate_paths,
                        *params[1:],
                    )
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT link, title, raw_title, score, tag, logic, direction "
                    "FROM news_cache WHERE created_at > ?"
                    f"{candidate_clause}"
                    f"{strategy_clause} ORDER BY created_at DESC LIMIT 200",
                    (cutoff, *candidate_paths, *params[1:]),
                ).fetchall()
            return [
                tuple(r)
                for r in rows
                if not is_fixed_safe_neutral_fields(*tuple(r)[3:7])
            ]


def get_entries_for_cached_rss(
    cfg: dict,
    hours: int = 24,
    limit: int = 20,
    feed_url: Optional[str] = None,
    min_score: Optional[float] = None,
    theme_terms: Optional[Tuple[str, ...]] = None,
    include_event_identity: bool = False,
) -> List[Tuple]:
    """重建 cached_feed：统一查询层高分视图。"""
    from services.intelligence_query import query_feed_tuples

    return query_feed_tuples(
        cfg,
        feed_url=feed_url,
        hours=hours,
        limit=limit,
        preview=False,
        order="recent",
        min_score=min_score,
        theme_terms=theme_terms,
        include_event_identity=include_event_identity,
    )


def get_recent_entries_for_feed_preview(
    cfg: dict,
    hours: int = 24 * 7,
    limit: int = 5,
    feed_url: Optional[str] = None,
    order_by: str = "score",
) -> List[Tuple]:
    """单源可读性兜底：统一查询层观察流视图。"""
    if not feed_url:
        return []
    from services.intelligence_query import query_feed_tuples

    order = "recent" if order_by == "recent" else "score"
    return query_feed_tuples(cfg, feed_url=feed_url, hours=hours, limit=limit, preview=True, order=order)


def get_high_score_articles_with_summary(
    min_score: Optional[float] = None, limit: int = 20, hours: int = 48, cfg: Optional[Dict] = None,
    feed_url: Optional[str] = None,
) -> List[Tuple]:
    from services.intelligence_query import query_summary_tuples

    return query_summary_tuples(
        cfg or {}, min_score=min_score, limit=limit, hours=hours, feed_url=feed_url
    )


def get_stats() -> Optional[Tuple]:
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT total_requests, api_calls_made, cache_hits, similarity_hits, "
                "blacklist_hits, fast_pass_hits, filtered_count, passed_count, updated_at "
                "FROM api_stats WHERE id = 1"
            ).fetchone()
            return tuple(row) if row else None


def update_stats(delta: Dict[str, int]) -> None:
    if not delta:
        return
    with db_lock:
        with get_connection() as conn:
            fields = ", ".join([f"{k} = {k} + ?" for k in delta.keys()])
            conn.execute(
                f"UPDATE api_stats SET {fields}, updated_at = ? WHERE id = 1",
                tuple(delta.values()) + (int(time.time()),)
            )
            conn.commit()


def reset_stats() -> None:
    with db_lock:
        with get_connection() as conn:
            conn.execute(
                "UPDATE api_stats SET total_requests=0, api_calls_made=0, cache_hits=0, "
                "similarity_hits=0, blacklist_hits=0, fast_pass_hits=0, filtered_count=0, passed_count=0, "
                "updated_at=? WHERE id=1", (int(time.time()),)
            )
            conn.commit()


def record_score(score: float) -> None:
    today = beijing_now().strftime("%Y-%m-%d")
    ib = int(round(float(score)))
    ib = max(0, min(10, ib))
    bucket = f"s{ib}"
    with db_lock:
        with get_connection() as conn:
            conn.execute(
                f"INSERT INTO score_distribution (date, {bucket}) VALUES (?, 1) "
                f"ON CONFLICT(date) DO UPDATE SET {bucket} = {bucket} + 1",
                (today,)
            )
            conn.commit()


def get_score_distribution(days: int = 7) -> List[Tuple]:
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT date, s10, s9, s8, s7, s6, s5, s4, s3, s2, s1, s0 "
                "FROM score_distribution ORDER BY date DESC LIMIT ?", (days,)
            ).fetchall()
            return [tuple(r) for r in rows]


def update_feed_health(
    url: str, name: str, response_time_ms: int, success: bool,
    articles_total: int, articles_passed: int,
    avg_score: Optional[float], error: Optional[str] = None
) -> None:
    successful_nonempty = bool(success and articles_total > 0)
    initial_empty_checks = 1 if success and articles_total == 0 else 0
    with db_lock:
        with get_connection() as conn:
            # Acquire SQLite's cross-process writer lock before sampling time.
            # This keeps last_check and empty-streak transitions in commit order
            # when webhook and scheduled refresh touch the same source.
            conn.execute("BEGIN IMMEDIATE")
            checked_at = int(time.time())
            conn.execute(
                "INSERT INTO feed_health (url, name, last_check, response_time_ms, "
                "success, fail, articles_total, articles_passed, avg_score, last_error, "
                "consecutive_empty_checks, last_nonempty_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET "
                "name=excluded.name, last_check=excluded.last_check, "
                "response_time_ms=excluded.response_time_ms, "
                "success=feed_health.success + excluded.success, "
                "fail=feed_health.fail + excluded.fail, "
                "articles_total=feed_health.articles_total + excluded.articles_total, "
                "articles_passed=feed_health.articles_passed + excluded.articles_passed, "
                "avg_score=excluded.avg_score, last_error=excluded.last_error, "
                "consecutive_empty_checks=CASE "
                "WHEN excluded.success=1 AND excluded.articles_total>0 THEN 0 "
                "WHEN excluded.success=1 AND excluded.articles_total=0 "
                "THEN COALESCE(feed_health.consecutive_empty_checks, 0) + 1 "
                "ELSE COALESCE(feed_health.consecutive_empty_checks, 0) END, "
                "last_nonempty_at=CASE "
                "WHEN excluded.success=1 AND excluded.articles_total>0 "
                "THEN excluded.last_check "
                "ELSE COALESCE(feed_health.last_nonempty_at, 0) END",
                (url, name, checked_at, response_time_ms,
                 1 if success else 0, 0 if success else 1,
                 articles_total, articles_passed, avg_score, error,
                 initial_empty_checks, checked_at if successful_nonempty else 0)
            )
            conn.commit()


def get_feed_health() -> List[Tuple]:
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT url, name, last_check, response_time_ms, success, fail, "
                "articles_total, articles_passed, avg_score, last_error, "
                "consecutive_empty_checks, last_nonempty_at "
                "FROM feed_health ORDER BY last_check DESC"
            ).fetchall()
            return [tuple(r) for r in rows]


def get_app_meta(key: str) -> Optional[str]:
    with db_lock:
        with get_connection() as conn:
            row = conn.execute("SELECT v FROM app_meta WHERE k = ?", (key,)).fetchone()
            return row[0] if row else None


def set_app_meta(key: str, value: str) -> None:
    with db_lock:
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)", (key, value)
            )
            conn.commit()


def get_last_full_refresh_ts() -> int:
    v = get_app_meta("last_full_refresh")
    try:
        return int(v) if v else 0
    except (TypeError, ValueError):
        return 0


def set_last_full_refresh_ts(ts: int) -> None:
    set_app_meta("last_full_refresh", str(int(ts)))


def record_refresh_attempt(result: Dict[str, Any], successful_at: Optional[int] = None) -> None:
    """Persist refresh observability and freshness in one transaction.

    The caller supplies ``successful_at`` only after every enabled source
    completed successfully. Keeping both keys in the same transaction prevents
    a partial refresh from being recorded as a full-source freshness update.
    The changed latest attempt also commits an existing cheap-snapshot
    publication obligation, including all-failed attempts with no new news.
    """
    if successful_at is not None and result.get("status") != "success":
        logger.warning(
            "Ignoring full-refresh timestamp for non-success status: %s",
            result.get("status"),
        )
        successful_at = None
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    with db_lock:
        with get_connection() as conn:
            previous = conn.execute(
                "SELECT v FROM app_meta WHERE k = ?", ("last_refresh_result",),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
                ("last_refresh_result", payload),
            )
            if successful_at is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
                    ("last_full_refresh", str(int(successful_at))),
                )
            _commit_dashboard_inputs(conn, changed=previous is None or previous[0] != payload)


def cleanup_test_records() -> dict:
    """清理测试记录（test.com/feed 等）并返回统计信息。"""
    removed = {
        "news_cache": 0,
        "backtest_signals": 0,
        "feed_health": 0,
        "request_logs": 0,
    }
    with db_lock:
        with get_connection() as conn:
            c = conn.cursor()
            # news_cache
            try:
                c.execute("DELETE FROM news_cache WHERE link LIKE '%test.com%' OR feed_url LIKE '%test.com%'")
                removed["news_cache"] = c.rowcount
            except sqlite3.OperationalError:
                pass
            # The signal ledger intentionally outlives news_cache, so test-data
            # cleanup must name it explicitly instead of relying on a cascade.
            try:
                c.execute(
                    "DELETE FROM backtest_signals "
                    "WHERE link LIKE '%test.com%' OR feed_url LIKE '%test.com%'"
                )
                removed["backtest_signals"] = c.rowcount
            except sqlite3.OperationalError:
                pass
            try:
                c.execute(
                    "DELETE FROM article_sources "
                    "WHERE link LIKE '%test.com%' OR feed_url LIKE '%test.com%'"
                )
                c.execute(
                    "DELETE FROM article_sources WHERE NOT EXISTS "
                    "(SELECT 1 FROM news_cache WHERE news_cache.link = article_sources.link)"
                )
            except sqlite3.OperationalError:
                pass
            # feed_health
            try:
                c.execute("DELETE FROM feed_health WHERE url LIKE '%test.com%'")
                removed["feed_health"] = c.rowcount
            except sqlite3.OperationalError:
                pass
            # request_logs
            try:
                c.execute("DELETE FROM request_logs WHERE url LIKE '%test.com%'")
                removed["request_logs"] = c.rowcount
            except sqlite3.OperationalError:
                pass
            conn.commit()
    logger.info("[Cleanup] 清理测试记录: %s", removed)
    return removed


def _first_rejection_retention_days(cfg: Optional[Dict]) -> int:
    """Configured retention for first-rejection evidence: 7-30 days, else 7."""
    options = (cfg or {}).get("first_rejection_retention")
    value = options.get("retention_days") if isinstance(options, dict) else None
    return safe_bounded_int(value, 7, 7, 30)


def cleanup_old_entries(conn: sqlite3.Connection, cfg: Optional[Dict] = None) -> None:
    """压缩 SQLite 表：保留 72h 内全部记录，72h-7天仅清理低于源阈值下限的噪音，7天前全删。"""
    if cfg is None:
        try:
            from core.config import config
            cfg = config.cfg
        except Exception:
            cfg = {}
    now = int(time.time())
    cutoff_72h = now - 72 * 3600
    cutoff_7d = now - 7 * 24 * 3600

    # 删除 7 天前的所有记录
    conn.execute("DELETE FROM news_cache WHERE created_at < ?", (cutoff_7d,))
    deleted_total = conn.total_changes

    threshold_candidates = [get_feed_thresholds(cfg, "")["aggregation"]]
    for feed in (cfg or {}).get("feeds", []) or []:
        if feed.get("enabled", True):
            threshold_candidates.append(get_feed_thresholds(cfg, feed.get("url", ""))["aggregation"])
    retention_floor = min(threshold_candidates) if threshold_candidates else 7.0

    # 删除 72h-7天的低分记录；使用最低源聚合阈值，避免误删低阈值源的有效历史情报。
    conn.execute(
        "DELETE FROM news_cache WHERE created_at < ? AND created_at >= ? AND score < ?",
        (cutoff_72h, cutoff_7d, retention_floor)
    )
    deleted_noise = conn.total_changes - deleted_total
    conn.execute("DELETE FROM score_validation_logs WHERE timestamp < ?", (cutoff_7d,))
    purge_first_rejection_evidence(
        conn, _first_rejection_retention_days(cfg)
    )
    # article_sources intentionally has no foreign-key enforcement so rolling
    # upgrades remain safe with legacy SQLite connections. Retention therefore
    # removes additive source rows explicitly in the same transaction.
    conn.execute(
        "DELETE FROM article_sources WHERE NOT EXISTS "
        "(SELECT 1 FROM news_cache WHERE news_cache.link = article_sources.link)"
    )

    # The public endpoint is capped at 365 days.  Keep a safety margin while
    # bounding the long-lived ledger, and run the potentially larger DELETE at
    # most once per local calendar day rather than on every refresh.
    retention_cfg = (cfg or {}).get("backtest", {}) or {}
    if not isinstance(retention_cfg, dict):
        retention_cfg = {}
    try:
        signal_retention_days = int(
            retention_cfg.get(
                "signal_retention_days",
                (cfg or {}).get("backtest_signal_retention_days", 400),
            )
        )
    except (TypeError, ValueError):
        signal_retention_days = 400
    signal_retention_days = max(366, min(3650, signal_retention_days))
    cleanup_day = beijing_now().date().isoformat()
    cleanup_row = conn.execute(
        "SELECT v FROM app_meta WHERE k = 'backtest_signals_last_cleanup_day'"
    ).fetchone()
    if not cleanup_row or str(cleanup_row[0] or "") != cleanup_day:
        signal_cutoff = now - signal_retention_days * 86400
        before_signal_cleanup = conn.total_changes
        # Retain the complete immutable revision chain whenever any revision is
        # still in-window; deleting only an old base row would make Radar rollback
        # and frozen mapping reconstruction impossible.
        conn.execute(
            "DELETE FROM backtest_signals WHERE (link, strategy_version) IN ("
            "SELECT link, strategy_version FROM backtest_signals "
            "GROUP BY link, strategy_version HAVING MAX(available_at) < ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM backtest_signals AS retained_signal "
            "JOIN score_bridge_members AS bridge_member "
            "ON bridge_member.ledger_signal_id = retained_signal.id "
            "WHERE retained_signal.link = backtest_signals.link "
            "AND retained_signal.strategy_version = backtest_signals.strategy_version"
            "))",
            (signal_cutoff,),
        )
        deleted_signals = conn.total_changes - before_signal_cleanup
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (k, v) VALUES "
            "('backtest_signals_last_cleanup_day', ?)",
            (cleanup_day,),
        )
        if deleted_signals:
            logger.info(
                "[Cleanup] 删除超过 %d 天的回测信号修订: %d 条",
                signal_retention_days,
                deleted_signals,
            )

    trimmed_outbox = trim_integration_signal_outbox(
        conn,
        now=now,
        retention_days=configured_retention_days(),
        max_rows=configured_max_rows(),
    )
    if trimmed_outbox:
        logger.info(
            "[Cleanup] integration signal outbox trimmed prefix: %d rows",
            trimmed_outbox,
        )

    if deleted_total or deleted_noise:
        logger.info(
            "[Cleanup] 删除 7天前: %d 条, 72h-7天低分(<%.1f): %d 条",
            deleted_total, retention_floor, deleted_noise
        )
    conn.commit()


def log_llm_call(
    purpose: str,
    backend_name: str,
    model_name: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost: float = 0.0,
    latency_ms: int = 0,
    success: bool = True,
    error_type: str = "",
    strategy_version: str = "",
    *,
    request_id: str = "",
    attempt_kind: str = "",
    response_mode: str = "",
    format_retry: bool = False,
    usage_status: str = "legacy",
    processing_id: str = "",
    article_key: str = "",
) -> None:
    """Record one physical call, preserving unknown usage as unknown metadata."""
    bounded_attempt_kind = str(attempt_kind or "").strip().lower()
    if bounded_attempt_kind not in {"initial", "fallback", "repair", "direct"}:
        bounded_attempt_kind = ""
    bounded_response_mode = str(response_mode or "").strip().lower()
    if bounded_response_mode not in {"json_schema", "json_object", "prompt_only"}:
        bounded_response_mode = ""
    bounded_usage_status = str(usage_status or "legacy").strip().lower()
    if bounded_usage_status not in {"complete", "partial", "unavailable", "legacy"}:
        bounded_usage_status = "legacy"
    timestamp = int(time.time())
    with db_lock:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO llm_call_logs (timestamp, purpose, backend_name, model_name, "
                "prompt_tokens, completion_tokens, total_tokens, estimated_cost, latency_ms, "
                "success, error_type, strategy_version, request_id, attempt_kind, response_mode, "
                "format_retry, usage_status, processing_id, article_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    timestamp, purpose, backend_name, model_name,
                    int(prompt_tokens or 0), int(completion_tokens or 0), int(total_tokens or 0),
                    float(estimated_cost or 0.0), int(latency_ms or 0), 1 if success else 0,
                    error_type or "", strategy_version or "",
                    str(request_id or "").strip()[:128], bounded_attempt_kind,
                    bounded_response_mode, 1 if format_retry else 0, bounded_usage_status,
                    str(processing_id or "").strip()[:128], str(article_key or "").strip()[:128],
                ),
            )
            # Keep the Dashboard's cumulative counter aligned with the
            # authoritative attempt log for every application path (polling,
            # webhook, digest, band, and preference extraction). Per-feed
            # request_logs retain their own ContextVar-scoped count.
            conn.execute(
                "UPDATE api_stats SET api_calls_made=api_calls_made+1, "
                "updated_at=? WHERE id=1",
                (timestamp,),
            )
            conn.commit()


def get_score_fallback_stats(hours: int = 24) -> Dict[str, Any]:
    """Return fallback health plus independent safe-neutral observations.

    ``eligible`` and ``fallback_ratio`` deliberately retain their established
    definition: direct model scores plus ``fallback_local`` only.  Safe-neutral
    paths are reported separately because they are auditable, non-substantive
    semantic resolutions rather than backend failures.  Their denominator is
    direct model scores, local fallbacks, and safe-neutral resolutions; cache,
    similarity, local rejects, and other non-model paths remain excluded.
    """
    bounded_hours = max(1, min(168, int(hours or 24)))
    cutoff = int(time.time()) - bounded_hours * 3600
    safe_neutral_paths = tuple(sorted(NON_SUBSTANTIVE_SCORE_PATHS))
    safe_neutral_placeholders = ",".join("?" for _ in safe_neutral_paths)
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN LOWER(TRIM(COALESCE(score_path, ''))) "
                "= 'fallback_local' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN LOWER(TRIM(COALESCE(score_path, ''))) "
                "IN ('llm', 'stage2') THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN LOWER(TRIM(COALESCE(score_path, ''))) "
                f"IN ({safe_neutral_placeholders}) THEN 1 ELSE 0 END), 0) "
                "FROM news_cache WHERE created_at >= ?",
                (*safe_neutral_paths, cutoff),
            ).fetchone()
    fallback = int(row[0] or 0) if row else 0
    model_scored = int(row[1] or 0) if row else 0
    safe_neutral = int(row[2] or 0) if row else 0
    eligible = fallback + model_scored
    safe_neutral_denominator = eligible + safe_neutral
    return {
        "window_hours": bounded_hours,
        "fallback_local": fallback,
        "model_scored": model_scored,
        "eligible": eligible,
        "fallback_ratio": fallback / eligible if eligible else 0.0,
        "safe_neutral": safe_neutral,
        "safe_neutral_denominator": safe_neutral_denominator,
        "safe_neutral_ratio": (
            safe_neutral / safe_neutral_denominator
            if safe_neutral_denominator
            else 0.0
        ),
    }


def get_llm_cost_stats(
    hours: int = 24, cfg: Optional[Dict] = None, *, all_strategies: bool = False,
) -> Dict[str, Any]:
    """Recorded application usage in a fixed rolling window, not provider billing."""
    window_end = int(time.time())
    cutoff = window_end - hours * 3600
    with db_lock:
        with get_connection() as conn:
            params: List[Any] = [cutoff, window_end]
            strategy_clause = "" if all_strategies else _strategy_where_clause(cfg, params)
            summary = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_tokens), 0), COALESCE(SUM(estimated_cost), 0), "
                "COALESCE(AVG(latency_ms), 0), COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN COALESCE(usage_status, 'legacy') "
                "NOT IN ('complete', 'partial') THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN usage_status='partial' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN success=0 AND COALESCE(usage_status, 'legacy') "
                "!='complete' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0) "
                "FROM llm_call_logs WHERE timestamp > ? AND timestamp <= ?" + strategy_clause,
                tuple(params),
            ).fetchone()
            by_model = conn.execute(
                "SELECT COALESCE(NULLIF(model_name, ''), backend_name) AS model, COUNT(*), "
                "COALESCE(SUM(total_tokens), 0), COALESCE(SUM(estimated_cost), 0) "
                "FROM llm_call_logs WHERE timestamp > ? AND timestamp <= ?" + strategy_clause +
                " GROUP BY model ORDER BY COUNT(*) DESC LIMIT 10",
                tuple(params),
            ).fetchall()
            by_purpose = conn.execute(
                "SELECT COALESCE(NULLIF(purpose, ''), 'unknown'), COUNT(*), "
                "COALESCE(SUM(total_tokens), 0), COALESCE(SUM(estimated_cost), 0) "
                "FROM llm_call_logs WHERE timestamp > ? AND timestamp <= ?" + strategy_clause +
                " GROUP BY purpose ORDER BY COUNT(*) DESC LIMIT 10",
                tuple(params),
            ).fetchall()
    return {
        "calls": summary[0] if summary else 0,
        "total_tokens": int(summary[1] or 0) if summary else 0,
        "estimated_cost": round(float(summary[2] or 0.0), 6) if summary else 0.0,
        "avg_latency_ms": round(float(summary[3] or 0.0), 1) if summary else 0.0,
        "failures": int(summary[4] or 0) if summary else 0,
        "unknown_usage_calls": int(summary[5] or 0) if summary else 0,
        "partial_usage_calls": int(summary[6] or 0) if summary else 0,
        "failed_unknown_usage_calls": int(summary[7] or 0) if summary else 0,
        "prompt_tokens": int(summary[8] or 0) if summary else 0,
        "completion_tokens": int(summary[9] or 0) if summary else 0,
        "window_hours": hours,
        "window_start_utc": utc_naive_fromtimestamp(cutoff).isoformat() + "Z",
        "window_end_utc": utc_naive_fromtimestamp(window_end).isoformat() + "Z",
        "all_strategies": all_strategies,
        "budget_enforced": False,
        "by_model": [(r[0], r[1], int(r[2] or 0), round(float(r[3] or 0.0), 6)) for r in by_model],
        "by_purpose": [(r[0], r[1], int(r[2] or 0), round(float(r[3] or 0.0), 6)) for r in by_purpose],
    }


def get_quality_metrics(hours: int = 24, cfg: Optional[Dict] = None) -> Dict[str, Any]:
    if cfg is None:
        try:
            from core.config import config
            cfg = config.cfg
        except Exception:
            cfg = {}
    cutoff = int(time.time()) - hours * 3600
    with db_lock:
        with get_connection() as conn:
            path_rows = conn.execute(
                "SELECT COALESCE(NULLIF(score_path, ''), 'unknown') AS path, COUNT(*) "
                "FROM news_cache WHERE created_at > ? GROUP BY path ORDER BY COUNT(*) DESC",
                (cutoff,),
            ).fetchall()
            tag_source_rows = conn.execute(
                "SELECT COALESCE(NULLIF(tag, ''), '未分类') AS tag, score, "
                "feed_url, score_path "
                "FROM news_cache WHERE created_at > ?",
                (cutoff,),
            ).fetchall()
            err_rows = conn.execute(
                "SELECT COALESCE(NULLIF(error_type, ''), 'none') AS err, COUNT(*) "
                "FROM news_cache WHERE created_at > ? GROUP BY err ORDER BY COUNT(*) DESC",
                (cutoff,),
            ).fetchall()
            latency = conn.execute(
                "SELECT AVG(latency_ms), MAX(latency_ms) FROM news_cache "
                "WHERE created_at > ? AND latency_ms > 0",
                (cutoff,),
            ).fetchone()
            feedback_rows = conn.execute(
                "SELECT label, COUNT(*) FROM article_feedback "
                "WHERE created_at > ? GROUP BY label",
                (cutoff,),
            ).fetchall()
            precision_rows = conn.execute(
                "SELECT s.score, s.label FROM feedback_example_snapshots s "
                "WHERE s.feedback_created_at > ? AND s.feedback_id = ("
                "SELECT latest.id FROM article_feedback latest "
                "WHERE latest.link = s.link "
                "ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)",
                (cutoff,),
            ).fetchall()
    tag_stats: Dict[str, List[float]] = {}
    for row in tag_source_rows:
        score = row[1]
        if (
            score is None
            or normalize_score_path(row[3]) not in FORMAL_SIGNAL_SCORE_PATHS
        ):
            continue
        if float(score) >= get_feed_thresholds(cfg, row[2] or "")["aggregation"]:
            tag_stats.setdefault(row[0], []).append(float(score))
    tag_rows = sorted(
        ((tag, len(scores), sum(scores) / len(scores)) for tag, scores in tag_stats.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:12]

    precision = {}
    for threshold in (7, 8, 9):
        rows = [r for r in precision_rows if (r[0] or 0) >= threshold]
        good = sum(1 for r in rows if r[1] == "good")
        precision[f"precision_at_{threshold}"] = round(good / len(rows) * 100, 1) if rows else None
        precision[f"precision_n_at_{threshold}"] = len(rows)
        precision[f"precision_good_at_{threshold}"] = good
    return {
        "score_paths": [(r[0], r[1]) for r in path_rows],
        "top_tags": [(r[0], r[1], round(r[2] or 0, 2)) for r in tag_rows],
        "errors": [(r[0], r[1]) for r in err_rows],
        "avg_latency_ms": round((latency[0] or 0), 1) if latency else 0,
        "max_latency_ms": latency[1] or 0 if latency else 0,
        "feedback": [(r[0], r[1]) for r in feedback_rows],
        **precision,
    }


def get_feedback_examples(label: Optional[str] = None, limit: int = 8, cfg: Optional[Dict] = None) -> List[Tuple]:
    with db_lock:
        with get_connection() as conn:
            params: List[Any] = []
            label_clause = ""
            if label:
                label_clause = " AND f.label = ?"
                params.append(label)
            rows = conn.execute(
                "SELECT f.label, f.target_score, f.note, n.raw_title, n.score, n.tag, n.logic, n.summary "
                "FROM article_feedback f LEFT JOIN news_cache n ON n.link = f.link "
                "WHERE n.raw_title IS NOT NULL" + label_clause +
                " ORDER BY f.created_at DESC LIMIT ?",
                tuple(params + [limit]),
            ).fetchall()
            return [tuple(r) for r in rows]


def get_feedback_contract_samples(
    label: str,
    limit: int = 8,
    cfg: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """Return deterministic, identity-bearing rows used in a scoring prompt."""
    bounded_limit = max(0, min(50, int(limit or 0)))
    if bounded_limit == 0:
        return []
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT feedback_id, link, label, target_score, note, "
                "feedback_created_at, raw_title, score, tag, logic, "
                "source_strategy_version, captured_at, snapshot_origin "
                "FROM feedback_example_snapshots s WHERE label = ? "
                "AND s.feedback_id = (SELECT latest.id FROM article_feedback latest "
                "WHERE latest.link = s.link "
                "ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1) "
                "ORDER BY feedback_created_at DESC, feedback_id DESC LIMIT ?",
                (str(label or ""), bounded_limit),
            ).fetchall()
    return [dict(row) for row in rows]


def get_feedback_contract_profile(
    positive_limit: int,
    negative_limit: int,
    cfg: Optional[Dict] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Read both feedback buckets from one SQLite snapshot."""
    limits = {
        "positive": max(0, min(50, int(positive_limit or 0))),
        "negative": max(0, min(50, int(negative_limit or 0))),
    }
    query = (
        "SELECT feedback_id, link, label, target_score, note, "
        "feedback_created_at, raw_title, score, tag, logic, "
        "source_strategy_version, captured_at, snapshot_origin "
        "FROM feedback_example_snapshots s WHERE label = ? "
        "AND s.feedback_id = (SELECT latest.id FROM article_feedback latest "
        "WHERE latest.link = s.link "
        "ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1) "
        "ORDER BY feedback_created_at DESC, feedback_id DESC LIMIT ?"
    )
    result: Dict[str, List[Dict[str, Any]]] = {
        "positive": [],
        "negative": [],
    }
    with db_lock:
        with get_read_only_connection() as conn:
            conn.execute("BEGIN")
            for bucket, label in (("positive", "good"), ("negative", "bad")):
                if limits[bucket] <= 0:
                    continue
                rows = conn.execute(query, (label, limits[bucket])).fetchall()
                result[bucket] = [dict(row) for row in rows]
            conn.rollback()
    return result


def register_scoring_contract_snapshot(contract: Dict[str, Any]) -> None:
    """Validate and idempotently register one immutable scoring contract."""
    strategy_version = str(contract.get("strategy_version") or "").strip()
    base_strategy_version = str(
        contract.get("base_strategy_version") or ""
    ).strip()
    profile_hash = str(contract.get("feedback_profile_hash") or "").strip()
    context_hash = str(contract.get("feedback_context_hash") or "").strip()
    context = str(contract.get("feedback_context") or "")
    sample_ids = [int(value) for value in contract.get("feedback_sample_ids") or []]
    snapshot = list(contract.get("feedback_snapshot") or [])
    profile_payload = contract.get("feedback_profile_payload")
    if not strategy_version or len(strategy_version) > 255:
        raise ValueError("invalid scoring contract strategy_version")
    if not base_strategy_version or len(base_strategy_version) > 255:
        raise ValueError("invalid scoring contract base_strategy_version")
    if not re.fullmatch(r"[0-9a-f]{64}", profile_hash):
        raise ValueError("invalid feedback profile hash")
    if not re.fullmatch(r"[0-9a-f]{64}", context_hash):
        raise ValueError("invalid feedback context hash")
    if strategy_version != f"{base_strategy_version}:fb-{profile_hash[:32]}":
        raise ValueError("scoring strategy version does not match profile hash")
    if not isinstance(profile_payload, dict):
        raise ValueError("invalid feedback profile payload")
    if profile_payload.get("schema") != "few-shot-feedback-v1":
        raise ValueError("unsupported feedback profile schema")
    profile_samples = profile_payload.get("samples")
    if not isinstance(profile_samples, list) or profile_samples != snapshot:
        raise ValueError("feedback snapshot does not match profile payload")
    if profile_payload.get("context") != context:
        raise ValueError("feedback context does not match profile payload")
    try:
        profile_sample_ids = [
            int(row["feedback_id"])
            for row in profile_samples
            if isinstance(row, dict)
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid feedback sample identity") from exc
    if len(profile_sample_ids) != len(profile_samples):
        raise ValueError("invalid feedback sample row")
    if sample_ids != profile_sample_ids or len(sample_ids) != len(set(sample_ids)):
        raise ValueError("feedback sample IDs do not match profile payload")

    ids_json = json.dumps(sample_ids, separators=(",", ":"), allow_nan=False)
    snapshot_json = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    profile_json = json.dumps(
        profile_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if hashlib.sha256(profile_json.encode("utf-8")).hexdigest() != profile_hash:
        raise ValueError("feedback profile hash mismatch")
    if hashlib.sha256(context.encode("utf-8")).hexdigest() != context_hash:
        raise ValueError("feedback context hash mismatch")
    expected = (
        base_strategy_version,
        profile_hash,
        profile_json,
        ids_json,
        snapshot_json,
        context,
        context_hash,
    )
    select_sql = (
        "SELECT base_strategy_version, feedback_profile_hash, "
        "feedback_profile_json, feedback_sample_ids_json, "
        "feedback_snapshot_json, feedback_context, "
        "feedback_context_hash FROM scoring_contract_snapshots "
        "WHERE strategy_version = ?"
    )
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(select_sql, (strategy_version,)).fetchone()
            if row is not None:
                if tuple(row) != expected:
                    raise RuntimeError(
                        "scoring contract hash collision or snapshot mismatch"
                    )
                return
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(select_sql, (strategy_version,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO scoring_contract_snapshots "
                    "(strategy_version, base_strategy_version, "
                    "feedback_profile_hash, feedback_profile_json, "
                    "feedback_sample_ids_json, feedback_snapshot_json, "
                    "feedback_context, feedback_context_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (strategy_version, *expected, int(time.time())),
                )
            elif tuple(row) != expected:
                conn.rollback()
                raise RuntimeError("scoring contract hash collision or snapshot mismatch")
            conn.commit()


def get_scoring_contract_snapshot(strategy_version: str) -> Optional[Dict[str, Any]]:
    with db_lock:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM scoring_contract_snapshots WHERE strategy_version = ?",
                (str(strategy_version or ""),),
            ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["feedback_sample_ids"] = json.loads(
        result.pop("feedback_sample_ids_json")
    )
    result["feedback_snapshot"] = json.loads(
        result.pop("feedback_snapshot_json")
    )
    result["feedback_profile_payload"] = json.loads(
        result.pop("feedback_profile_json")
    )
    return result


def get_positive_feedback_articles(limit: int = 5, cfg: Optional[Dict] = None) -> List[Tuple]:
    return get_feedback_examples("good", limit=limit, cfg=cfg)


def get_negative_feedback_articles(limit: int = 5, cfg: Optional[Dict] = None) -> List[Tuple]:
    return get_feedback_examples("bad", limit=limit, cfg=cfg)


def add_article_feedback(link: str, label: str, target_score: Optional[float] = None, note: str = "") -> None:
    created_at = int(time.time())
    with db_lock:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO article_feedback (link, label, target_score, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (link, label, target_score, note, created_at),
            )
            conn.execute(
                "INSERT INTO feedback_example_snapshots "
                "(feedback_id, link, label, target_score, note, "
                "feedback_created_at, raw_title, score, tag, logic, "
                "source_strategy_version, captured_at, snapshot_origin) "
                "SELECT ?, ?, ?, ?, ?, ?, n.raw_title, n.score, "
                "COALESCE(n.tag, ''), COALESCE(n.logic, ''), "
                "COALESCE(n.strategy_version, ''), ?, 'feedback_write' "
                "FROM news_cache n WHERE n.link = ? AND n.raw_title IS NOT NULL",
                (
                    int(cursor.lastrowid), link, label, target_score, note,
                    created_at, created_at, link,
                ),
            )
            conn.commit()


def upsert_event_cluster(event: Dict[str, Any], articles: List[Dict[str, Any]], cfg: Optional[Dict] = None) -> None:
    """Deprecated compatibility hook; never create a headline-derived event.

    Radar now writes pairwise ``event_similarities`` directly.  Keeping this
    method as a validating no-op prevents an older caller from silently
    resurrecting ``event_cache`` as a second identity authority.
    """
    candidate = str(event.get("event_id") or "").strip()
    if not candidate:
        logger.warning("[EventCore] ignored legacy cluster without persisted UUID")
        return
    with db_lock:
        with get_connection() as conn:
            try:
                canonical_event_id(conn, candidate)
            except (KeyError, ValueError, EventCoreConflictError):
                logger.warning("[EventCore] ignored non-canonical legacy cluster id")
                return


def get_events(
    cfg: Optional[Dict] = None,
    hours: int = 24,
    limit: int = 20,
    min_score: Optional[float] = None,
    direction: Optional[str] = None,
) -> List[Tuple]:
    try:
        bounded_hours = max(1, min(168, int(hours)))
    except (TypeError, ValueError):
        bounded_hours = 24
    try:
        bounded_limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        bounded_limit = 20
    bounded_min_score: Optional[float] = None
    if min_score is not None:
        bounded_min_score = float(min_score)
        if (
            not math.isfinite(bounded_min_score)
            or not 0 <= bounded_min_score <= 10
        ):
            raise ValueError("min_score must be a finite number in 0..10")
    direction_aliases = {
        "看多": ("看多", "多", "bullish", "📈"),
        "看空": ("看空", "空", "bearish", "📉"),
        "中性": ("中性", "neutral", "⚖️", "⚡"),
    }
    normalized_direction = str(direction or "").strip()
    if normalized_direction and normalized_direction not in direction_aliases:
        raise ValueError("direction must be 看多, 看空, or 中性")
    cutoff = int(time.time()) - bounded_hours * 3600
    with db_lock:
        with get_connection() as conn:
            news_cache_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(news_cache)").fetchall()
            }
            public_evidence_clause = ""
            public_evidence_params: List[Any] = []
            if "score_path" in news_cache_columns:
                public_paths = tuple(sorted(PUBLIC_SCORE_PATHS))
                public_placeholders = ",".join("?" for _ in public_paths)
                public_evidence_clause = (
                    " WHERE NOT EXISTS ("
                    "SELECT 1 FROM news_cache AS cached_source "
                    "WHERE cached_source.link = evidence.source_uri "
                    "AND LOWER(TRIM(COALESCE(cached_source.score_path, ''))) "
                    f"NOT IN ({public_placeholders}))"
                )
                public_evidence_params.extend(public_paths)
            params: List[Any] = [cutoff]
            event_filter_clause = ""
            if bounded_min_score is not None:
                event_filter_clause += " AND priority >= ?"
                params.append(bounded_min_score)
            if normalized_direction:
                aliases = direction_aliases[normalized_direction]
                placeholders = ",".join("?" for _ in aliases)
                event_filter_clause += (
                    f" AND LOWER(TRIM(direction)) IN ({placeholders})"
                )
                params.extend(alias.lower() for alias in aliases)
            strategy_clause = _strategy_where_clause(cfg, params)
            # Keep the public feed to one bounded result query.  The recursive
            # group retains evidence from audited merge descendants, while the
            # window and aggregate replace per-event evidence materialization.
            rows = conn.execute(
                "WITH RECURSIVE selected_events AS ("
                "  SELECT event_id, title, priority, event_type, direction, first_seen_at, last_seen_at "
                "  FROM events WHERE merge_state = 'active' AND last_seen_at > ?" +
                event_filter_clause +
                strategy_clause +
                "  ORDER BY priority DESC, last_seen_at DESC, event_id ASC LIMIT ?"
                "), event_group(root_event_id, member_event_id) AS ("
                "  SELECT event_id, event_id FROM selected_events "
                "  UNION "
                "  SELECT event_group.root_event_id, child.event_id FROM events AS child "
                "  JOIN event_group ON child.merged_into = event_group.member_event_id"
                "), ranked_evidence AS ("
                "  SELECT event_group.root_event_id, evidence.source_uri, "
                "         evidence.source_name, evidence.excerpt, evidence.snapshot_text, "
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY event_group.root_event_id "
                "           ORDER BY evidence.reliability DESC, evidence.observed_at ASC, "
                "                    evidence.evidence_id ASC"
                "         ) AS evidence_rank "
                # Drive evidence lookups from the bounded event page. SQLite
                # otherwise may scan all evidence before applying this join.
                "  FROM event_group CROSS JOIN event_evidence AS evidence "
                "    ON evidence.event_id = event_group.member_event_id" +
                public_evidence_clause +
                "), canonical_evidence AS ("
                "  SELECT root_event_id, source_uri, "
                "         CASE WHEN excerpt <> '' THEN excerpt ELSE snapshot_text END AS summary "
                "  FROM ranked_evidence WHERE evidence_rank = 1"
                "), source_counts AS ("
                "  SELECT root_event_id, COUNT(DISTINCT CASE "
                "           WHEN source_name <> '' THEN NULLIF(TRIM(source_name), '') "
                "           ELSE NULLIF(TRIM(source_uri), '') END) AS source_count "
                "  FROM ranked_evidence GROUP BY root_event_id"
                ") "
                "SELECT selected.event_id, selected.title, "
                "       COALESCE(canonical.source_uri, ''), "
                "       SUBSTR(COALESCE(canonical.summary, ''), 1, 8000), "
                "       selected.priority, selected.event_type, "
                "       COALESCE(source_counts.source_count, 0), "
                "       selected.first_seen_at, selected.last_seen_at, selected.direction "
                "FROM selected_events AS selected "
                "JOIN canonical_evidence AS canonical "
                "  ON canonical.root_event_id = selected.event_id "
                "JOIN source_counts ON source_counts.root_event_id = selected.event_id "
                "ORDER BY selected.priority DESC, selected.last_seen_at DESC, "
                "         selected.event_id ASC",
                tuple(params + [bounded_limit, *public_evidence_params]),
            ).fetchall()
            return [
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2] or ""),
                    str(row[3] or ""),
                    float(row[4] or 0.0),
                    str(row[5] or ""),
                    int(row[6] or 0),
                    int(row[7] or 0),
                    int(row[8] or 0),
                    str(row[9] or "中性"),
                )
                for row in rows
            ]


def get_event_articles(event_id: str) -> List[Tuple]:
    with db_lock:
        with get_connection() as conn:
            try:
                group = canonical_event_group(conn, event_id)
            except (KeyError, ValueError, EventCoreConflictError):
                return []
            placeholders = ",".join("?" for _ in group)
            rows = conn.execute(
                "SELECT ev.source_uri, ev.source_name, ev.claim_text, e.priority, "
                "COALESCE(ev.announcement_at, ev.observed_at) "
                "FROM event_evidence AS ev JOIN events AS e ON e.event_id = ev.event_id "
                "WHERE ev.event_id IN (" + placeholders + ") "
                "ORDER BY e.priority DESC, ev.observed_at DESC",
                group,
            ).fetchall()
            return [tuple(r) for r in rows]


class DB:
    """兼容层：保留旧的 DB 类接口，内部转发到模块级函数"""
    get_cached_entry = staticmethod(get_cached_entry)
    get_feed_config = staticmethod(get_feed_config)
    get_similarity_identity_rows = staticmethod(get_similarity_identity_rows)
    canonical_feed_url = staticmethod(canonical_feed_url)
    get_feed_thresholds = staticmethod(get_feed_thresholds)
    pass_threshold_for_feed = staticmethod(pass_threshold_for_feed)
    notify_threshold_for_feed = staticmethod(notify_threshold_for_feed)
    aggregation_threshold_for_feed = staticmethod(aggregation_threshold_for_feed)
    equivalent_feed_urls = staticmethod(equivalent_feed_urls)
    get_cached_entry_meta = staticmethod(get_cached_entry_meta)
    get_latest_signal_score_path = staticmethod(get_latest_signal_score_path)
    get_event_identity_for_link = staticmethod(get_event_identity_for_link)
    get_canonical_event = staticmethod(get_canonical_event)
    get_cached_entries_by_links = staticmethod(get_cached_entries_by_links)
    get_article_sources = staticmethod(get_article_sources)
    record_article_source = staticmethod(record_article_source)
    record_article_sources = staticmethod(record_article_sources)
    set_cached_entry = staticmethod(set_cached_entry)
    store_backtest_signal = staticmethod(store_backtest_signal)
    store_score_bridge_preview = staticmethod(store_score_bridge_preview)
    get_backtest_signals = staticmethod(get_backtest_signals)
    get_backtest_signal_stats = staticmethod(get_backtest_signal_stats)
    get_backtest_base_scores = staticmethod(get_backtest_base_scores)
    get_backtest_signal_states = staticmethod(get_backtest_signal_states)
    get_backtest_cluster_signal_states = staticmethod(get_backtest_cluster_signal_states)
    update_cached_entry_score_state = staticmethod(update_cached_entry_score_state)
    update_cached_entry_score_states = staticmethod(update_cached_entry_score_states)
    update_cached_entry_origin = staticmethod(update_cached_entry_origin)
    update_cached_entry_origins = staticmethod(update_cached_entry_origins)
    get_recent_entries = staticmethod(get_recent_entries)
    get_high_score_articles_with_summary = staticmethod(get_high_score_articles_with_summary)
    get_entries_for_cached_rss = staticmethod(get_entries_for_cached_rss)
    get_recent_entries_for_feed_preview = staticmethod(get_recent_entries_for_feed_preview)
    get_stats = staticmethod(get_stats)
    update_stats = staticmethod(update_stats)
    reset_stats = staticmethod(reset_stats)
    record_score = staticmethod(record_score)
    get_score_distribution = staticmethod(get_score_distribution)
    log_request = staticmethod(log_request)
    get_request_logs = staticmethod(get_request_logs)
    update_feed_health = staticmethod(update_feed_health)
    get_feed_health = staticmethod(get_feed_health)
    get_app_meta = staticmethod(get_app_meta)
    set_app_meta = staticmethod(set_app_meta)
    get_last_full_refresh_ts = staticmethod(get_last_full_refresh_ts)
    set_last_full_refresh_ts = staticmethod(set_last_full_refresh_ts)
    record_refresh_attempt = staticmethod(record_refresh_attempt)
    cleanup_test_records = staticmethod(cleanup_test_records)
    log_llm_call = staticmethod(log_llm_call)
    record_news_processing = staticmethod(record_news_processing)
    get_news_processing_trace = staticmethod(get_news_processing_trace)
    get_score_fallback_stats = staticmethod(get_score_fallback_stats)
    get_llm_cost_stats = staticmethod(get_llm_cost_stats)
    get_quality_metrics = staticmethod(get_quality_metrics)
    get_feedback_examples = staticmethod(get_feedback_examples)
    get_feedback_contract_samples = staticmethod(get_feedback_contract_samples)
    get_feedback_contract_profile = staticmethod(get_feedback_contract_profile)
    register_scoring_contract_snapshot = staticmethod(register_scoring_contract_snapshot)
    get_scoring_contract_snapshot = staticmethod(get_scoring_contract_snapshot)
    get_positive_feedback_articles = staticmethod(get_positive_feedback_articles)
    get_negative_feedback_articles = staticmethod(get_negative_feedback_articles)
    add_article_feedback = staticmethod(add_article_feedback)
    upsert_event_cluster = staticmethod(upsert_event_cluster)
    get_events = staticmethod(get_events)
    get_event_articles = staticmethod(get_event_articles)
