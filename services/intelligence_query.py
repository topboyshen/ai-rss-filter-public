from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from db.database import DB, db_lock, get_connection
from db.event_core import canonical_event_id
from scoring_provenance import (
    PUBLIC_SCORE_PATHS,
)
from utils.helpers import normalize_direction


IRRELEVANT_TAGS = {"无", "无关", "无相关", "无相关标签", "未知"}
_PUBLIC_INTELLIGENCE_SCORE_PATHS = tuple(sorted(
    PUBLIC_SCORE_PATHS
))
_EVENT_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class IntelligenceItem:
    link: str
    title: str
    raw_title: str
    score: float | None
    tag: str
    logic: str
    summary: str
    created_at: int | None
    feed_url: str
    direction: str
    event_id: str = ""
    event_version: int = 0

    def as_briefing_tuple(self) -> tuple:
        return (
            self.link,
            self.title,
            self.raw_title,
            self.score,
            self.tag,
            self.logic,
            self.summary,
            self.created_at,
            self.feed_url,
            self.direction,
            self.event_id,
            self.event_version,
        )

    def as_feed_tuple(self, *, include_event_identity: bool = False) -> tuple:
        legacy = (
            self.link,
            self.title,
            self.raw_title,
            self.score,
            self.tag,
            self.logic,
            self.summary,
            self.created_at,
        )
        if not include_event_identity:
            return legacy
        return (*legacy, self.event_id, self.event_version)

    def as_article_dict(self) -> dict:
        return {
            "link": self.link,
            "title": self.title,
            "raw_title": self.raw_title,
            "score": self.score,
            "tag": self.tag,
            "logic": self.logic,
            "summary": self.summary,
            "created_at": self.created_at,
            "feed_url": self.feed_url,
            "direction": self.direction,
            "event_id": self.event_id,
            "event_version": self.event_version,
        }


def _unique_urls(urls: Iterable[str]) -> list[str]:
    return [u for u in dict.fromkeys(str(u or "").strip() for u in urls) if u]


def equivalent_feed_urls(cfg: dict, feed_url: str | None = None) -> list[str]:
    if not feed_url:
        return []
    try:
        return _unique_urls(DB.equivalent_feed_urls(cfg, feed_url))
    except Exception:
        return _unique_urls([feed_url])


def canonical_feed_url(cfg: dict, feed_url: str | None = None) -> str:
    if not feed_url:
        return ""
    try:
        return str(DB.canonical_feed_url(cfg, feed_url) or feed_url)
    except Exception:
        return str(feed_url or "")


def _global_aggregation_threshold_sql(cfg: dict, params: list) -> str:
    """Build a bounded CASE matching the existing per-primary-feed policy."""
    default_threshold = float(DB.get_feed_thresholds(cfg, "")["aggregation"])
    known_urls: list[str] = []
    for feed in cfg.get("feeds", []) or []:
        if not isinstance(feed, dict):
            continue
        configured_url = str(feed.get("url") or "").strip()
        if not configured_url:
            continue
        known_urls.extend(equivalent_feed_urls(cfg, configured_url))
    # Resolve every exact URL independently. An alias family can contain an
    # explicitly configured URL with its own threshold; inheriting whichever
    # family member appeared first would disagree with the Python post-filter.
    threshold_by_url = {
        url: float(DB.get_feed_thresholds(cfg, url)["aggregation"])
        for url in _unique_urls(known_urls)
    }
    if not threshold_by_url:
        params.append(default_threshold)
        return "?"

    grouped: dict[float, list[str]] = {}
    for url, threshold in threshold_by_url.items():
        grouped.setdefault(threshold, []).append(url)
    clauses = []
    for threshold, urls in grouped.items():
        placeholders = ",".join("?" for _ in urls)
        clauses.append(f"WHEN feed_url IN ({placeholders}) THEN ?")
        params.extend(urls)
        params.append(threshold)
    params.append(default_threshold)
    return "CASE " + " ".join(clauses) + " ELSE ? END"


def _strategy_clause(cfg: dict | None, params: list) -> str:
    version = str((cfg or {}).get("scoring_strategy_version") or "")
    if not version:
        return ""
    if ":" in version:
        params.append(f"{version.split(':', 1)[0]}:%")
        return " AND COALESCE(strategy_version, '') LIKE ? "
    params.append(version)
    return " AND COALESCE(strategy_version, '') = ? "


def _canonical_event_reference(conn, event_id: object) -> tuple[str, int]:
    """Return the current canonical identity, never a stale merge source."""
    value = str(event_id or "").strip()
    if not value:
        return "", 0
    try:
        canonical_id = canonical_event_id(conn, value)
        row = conn.execute(
            "SELECT version FROM events WHERE event_id = ?",
            (canonical_id,),
        ).fetchone()
        return canonical_id, max(1, int(row[0])) if row else 0
    except Exception:
        # A rolling deployment can briefly expose a legacy/cache row before its
        # explicit event-core backfill.  Such a row remains legacy and must not
        # advertise a non-canonical identity to Briefing consumers.
        return "", 0


def _active_event_references(conn, rows) -> dict[str, tuple[str, int]]:
    """Batch the common unmerged case; retain merge-chain validation otherwise."""
    event_ids = list(dict.fromkeys(
        str(row[10] or "").strip()
        for row in rows
        if len(row) > 10 and _EVENT_UUID.fullmatch(str(row[10] or "").strip())
    ))
    references: dict[str, tuple[str, int]] = {}
    # Stay below SQLite's legacy 999-parameter ceiling even for large feeds.
    for start in range(0, len(event_ids), 500):
        batch = event_ids[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        try:
            active_rows = conn.execute(
                "SELECT event_id, version FROM events "
                f"WHERE event_id IN ({placeholders}) "
                "AND merge_state = 'active' AND merged_into IS NULL",
                batch,
            ).fetchall()
        except Exception:
            # Old replicas with no event-core schema use the existing per-row
            # resolver, which excludes broken references from public output.
            continue
        for event_id, version in active_rows:
            try:
                references[event_id] = (event_id, max(1, int(version)))
            except (TypeError, ValueError, OverflowError):
                references[event_id] = ("", 0)
    return references


def _rows_to_items(rows, conn=None) -> list[IntelligenceItem]:
    rows = list(rows)
    items: list[IntelligenceItem] = []
    event_cache = _active_event_references(conn, rows) if conn is not None else {}
    for r in rows:
        raw_event_id = str(r[10] or "").strip() if len(r) > 10 else ""
        event_id = raw_event_id
        event_version = r[11] if len(r) > 11 else 0
        if conn is not None and event_id:
            if raw_event_id not in event_cache:
                event_cache[raw_event_id] = _canonical_event_reference(
                    conn, raw_event_id
                )
            event_id, event_version = event_cache[raw_event_id]
            if not event_id:
                # A row carrying a broken event reference is not a legacy row.
                # Hide it instead of downgrading to title-based Briefing
                # clustering, which could silently join unrelated events.
                continue
        items.append(
            IntelligenceItem(
                link=r[0] or "",
                title=r[1] or "",
                raw_title=r[2] or "",
                score=r[3],
                tag=r[4] or "",
                logic=r[5] or "",
                summary=r[6] or "",
                created_at=int(r[7] or 0) if len(r) > 7 and r[7] is not None else None,
                feed_url=r[8] or "" if len(r) > 8 else "",
                direction=normalize_direction(
                    r[9] if len(r) > 9 else "",
                    text=r[1] if len(r) > 1 else "",
                ),
                event_id=str(event_id or ""),
                event_version=max(0, int(event_version or 0)),
            )
        )
    return items


def query_intelligence_items(
    cfg: dict,
    *,
    view: str = "high_score",
    feed_url: str | None = None,
    hours: int = 48,
    start_ts: int | None = None,
    end_ts: int | None = None,
    limit: int = 20,
    min_score: float | None = None,
    theme_terms: Iterable[str] | None = None,
    order: str = "score",
    candidate_multiplier: int = 80,
    require_summary: bool = False,
    noise_filter: Optional[Callable[[dict], bool]] = None,
    preserve_feed_thresholds: bool = False,
) -> list[IntelligenceItem]:
    """统一情报查询入口。

    view:
    - high_score: 高分情报视图，按 feed aggregation 阈值过滤。
    - feed_high_score: 单源高分输出，语义同 high_score 但要求 feed_url。
    - feed_preview: 单源观察流，不按高分阈值过滤。
    """
    if limit <= 0:
        return []
    if min_score is not None:
        min_score = float(min_score)
        if not math.isfinite(min_score) or not 0 <= min_score <= 10:
            raise ValueError("min_score must be a finite number in 0..10")
    raw_theme_terms = (
        [theme_terms] if isinstance(theme_terms, str) else (theme_terms or [])
    )
    normalized_theme_terms: list[str] = []
    seen_theme_terms: set[str] = set()
    for raw_term in raw_theme_terms:
        term = str(raw_term or "").strip()
        key = term.casefold()
        if not term or len(term) > 100 or key in seen_theme_terms:
            continue
        seen_theme_terms.add(key)
        normalized_theme_terms.append(term)
        if len(normalized_theme_terms) >= 64:
            break
    now_ts = int(time.time())
    cutoff = int(start_ts) if start_ts is not None else now_ts - max(1, int(hours)) * 3600
    upper_ts = int(end_ts) if end_ts is not None else None
    feed_urls = equivalent_feed_urls(cfg, feed_url)
    params: list = [cutoff]
    where = ["created_at >= ?", "score IS NOT NULL"]
    if upper_ts is not None:
        where.append("created_at <= ?")
        params.append(upper_ts)
    if require_summary:
        where.append("summary IS NOT NULL")
        where.append("summary != ''")
    if view != "feed_preview":
        placeholders = ",".join("?" for _ in IRRELEVANT_TAGS)
        where.append(f"COALESCE(tag, '') NOT IN ({placeholders})")
        params.extend(IRRELEVANT_TAGS)
    if feed_urls:
        placeholders = ",".join("?" for _ in feed_urls)
        # news_cache.feed_url is intentionally a stable primary-source field.
        # Source-specific views must also match later sightings, while the OR
        # keeps compatibility with rows inserted by legacy/direct SQL callers.
        where.append(
            f"(feed_url IN ({placeholders}) OR EXISTS ("
            "SELECT 1 FROM article_sources AS source_sighting "
            "WHERE source_sighting.link = news_cache.link "
            f"AND source_sighting.feed_url IN ({placeholders})))"
        )
        params.extend(feed_urls)
        params.extend(feed_urls)
    if normalized_theme_terms:
        searchable = (
            "LOWER(COALESCE(raw_title, '') || ' ' || "
            "COALESCE(title, '') || ' ' || COALESCE(tag, ''))"
        )
        where.append(
            "(" + " OR ".join(
                f"INSTR({searchable}, LOWER(?)) > 0"
                for _ in normalized_theme_terms
            ) + ")"
        )
        params.extend(normalized_theme_terms)
    # Apply every strict feed floor before ORDER/LIMIT. Filtering only after
    # LIMIT lets a burst of newer ineligible rows hide older valid entries.
    if view != "feed_preview" and preserve_feed_thresholds:
        if feed_url:
            feed_threshold = float(
                DB.get_feed_thresholds(cfg, feed_url)["aggregation"]
            )
            effective_floor = (
                feed_threshold
                if min_score is None
                else max(float(min_score), feed_threshold)
            )
            where.append("score >= ?")
            params.append(effective_floor)
        else:
            requested_floor = 0.0 if min_score is None else float(min_score)
            params.append(requested_floor)
            threshold_sql = _global_aggregation_threshold_sql(cfg, params)
            where.append(f"score >= MAX(?, {threshold_sql})")
    elif view != "feed_preview" and min_score is not None:
        where.append("score >= ?")
        params.append(float(min_score))
    elif view == "feed_high_score" and feed_url:
        feed_threshold = float(
            DB.get_feed_thresholds(cfg, feed_url)["aggregation"]
        )
        where.append("score >= ?")
        params.append(feed_threshold)
    strategy_params: list = []
    strategy_clause = _strategy_clause(cfg, strategy_params)
    order_clause = "created_at DESC, score DESC" if order == "recent" else "score DESC, created_at DESC"
    sql_limit = limit if view == "feed_preview" else max(limit * candidate_multiplier, limit)
    with db_lock:
        with get_connection() as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(news_cache)").fetchall()
            }
            # Keep rolling upgrades readable: the additive columns may not yet
            # exist on an old replica, but consumers must never synthesize an
            # event identity to fill the gap.
            event_projection = (
                "event_id, event_version"
                if {"event_id", "event_version"}.issubset(columns)
                else "'' AS event_id, 0 AS event_version"
            )
            public_where = list(where)
            public_params = list(params)
            if "score_path" in columns:
                placeholders = ",".join(
                    "?" for _ in _PUBLIC_INTELLIGENCE_SCORE_PATHS
                )
                public_where.append(
                    "LOWER(TRIM(COALESCE(score_path, ''))) "
                    f"IN ({placeholders})"
                )
                public_params.extend(_PUBLIC_INTELLIGENCE_SCORE_PATHS)
            public_params.extend(strategy_params)
            public_params.append(sql_limit)
            sql = (
                "SELECT link, title, raw_title, score, tag, logic, summary, "
                f"created_at, feed_url, direction, {event_projection} "
                f"FROM news_cache WHERE {' AND '.join(public_where)} {strategy_clause}"
                f"ORDER BY {order_clause} LIMIT ?"
            )
            rows = conn.execute(sql, tuple(public_params)).fetchall()
            items = _rows_to_items(rows, conn=conn)
    if view == "feed_preview":
        return items[:limit]

    filtered: list[IntelligenceItem] = []
    for item in items:
        threshold_source = feed_url or item.feed_url or ""
        source_threshold = float(
            DB.get_feed_thresholds(cfg, threshold_source)["aggregation"]
        )
        if min_score is None:
            threshold = source_threshold
        elif preserve_feed_thresholds:
            threshold = max(float(min_score), source_threshold)
        else:
            threshold = float(min_score)
        if item.score is None or float(item.score) < threshold:
            continue
        if noise_filter and noise_filter(item.as_article_dict()):
            continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


def query_briefing_tuples(
    cfg: dict,
    *,
    min_score: float | None = None,
    limit: int = 12,
    hours: int = 48,
    feed_url: str | None = None,
    noise_filter: Optional[Callable[[dict], bool]] = None,
    candidate_multiplier: int = 80,
) -> list[tuple]:
    return [
        item.as_briefing_tuple()
        for item in query_intelligence_items(
            cfg,
            view="high_score",
            feed_url=feed_url,
            hours=hours,
            limit=limit,
            min_score=min_score,
            order="score",
            noise_filter=noise_filter,
            candidate_multiplier=max(1, int(candidate_multiplier)),
        )
    ]

def query_feed_tuples(
    cfg: dict,
    *,
    feed_url: str | None,
    hours: int = 48,
    limit: int = 20,
    preview: bool = False,
    order: str = "score",
    min_score: float | None = None,
    theme_terms: Iterable[str] | None = None,
    include_event_identity: bool = False,
) -> list[tuple]:
    view = "feed_preview" if preview else "feed_high_score"
    return [
        item.as_feed_tuple(include_event_identity=include_event_identity)
        for item in query_intelligence_items(
            cfg,
            view=view,
            feed_url=feed_url,
            hours=hours,
            limit=limit,
            min_score=min_score,
            theme_terms=theme_terms,
            order=order,
            candidate_multiplier=8,
            preserve_feed_thresholds=True,
        )
    ]


def query_summary_tuples(
    cfg: dict,
    *,
    min_score: float | None = None,
    limit: int = 20,
    hours: int = 48,
    feed_url: str | None = None,
    noise_filter: Optional[Callable[[dict], bool]] = None,
) -> list[tuple]:
    return [
        (
            item.link,
            item.title,
            item.raw_title,
            item.score,
            item.tag,
            item.logic,
            item.summary,
            item.created_at,
        )
        for item in query_intelligence_items(
            cfg,
            view="high_score",
            feed_url=feed_url,
            hours=hours,
            limit=limit,
            min_score=min_score,
            order="recent",
            candidate_multiplier=8,
            require_summary=True,
            noise_filter=noise_filter,
        )
    ]
