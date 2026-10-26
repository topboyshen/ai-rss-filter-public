"""Bounded, read-only observations of persisted safe-neutral cache records."""

from __future__ import annotations

import math
import sqlite3
import time

from db.database import get_read_only_connection
from scoring_provenance import (
    NEUTRAL_EVIDENCE_REASONS,
    NEUTRAL_REASON_CONTRACT,
    NEUTRAL_REASON_FACTS,
    NEUTRAL_REASON_INITIAL,
    NEUTRAL_REASON_THEME,
    NON_SUBSTANTIVE_SCORE_PATHS,
    SAFE_NEUTRAL_SCORE_PATH_METADATA,
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT,
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING,
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL,
    normalize_score_path,
)

_HOURS = 24
_LIMIT = 50
_READ_SECONDS = 0.5
_PROGRESS_INTERVAL = 1000
_MAX_READ_STEPS = 2_000_000
_REQUIRED_COLUMNS = {"title", "link", "created_at", "score_path", "strategy_version"}
_OUTPUT_COLUMNS = (
    "title", "raw_title", "link", "feed_url", "created_at", "score_path",
    "safe_neutral_reason",
)
_REASONS_BY_RESOLUTION = {
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_CONTRACT: {NEUTRAL_REASON_CONTRACT},
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_GROUNDING: {
        NEUTRAL_REASON_FACTS, NEUTRAL_REASON_THEME,
    },
    SEMANTIC_RESOLUTION_SAFE_NEUTRAL_INITIAL: {NEUTRAL_REASON_INITIAL},
}


def _bounded_read(conn):
    """Bound both lock waits and scans even on legacy databases without indexes."""
    deadline = time.monotonic() + _READ_SECONDS
    steps = 0

    def exhausted():
        nonlocal steps
        steps += _PROGRESS_INTERVAL
        return steps >= _MAX_READ_STEPS or time.monotonic() >= deadline

    conn.execute("PRAGMA busy_timeout=250")
    conn.set_progress_handler(exhausted, _PROGRESS_INTERVAL)


def _record(row):
    record = dict(row)
    path = normalize_score_path(record["score_path"])
    reason = str(record["safe_neutral_reason"] or "").strip()
    resolution = SAFE_NEUTRAL_SCORE_PATH_METADATA[path][1]
    if reason not in NEUTRAL_EVIDENCE_REASONS or reason not in _REASONS_BY_RESOLUTION[resolution]:
        reason = ""
    record["score_path"] = path
    record["safe_neutral_reason"] = reason
    for field in ("title", "raw_title", "link", "feed_url"):
        record[field] = str(record[field] or "")
    return record


def read_neutral_records(strategy_version: str, *, now: float | None = None) -> dict:
    """Read the current exact contract's latest 50 observations within 24 hours.

    Missing schemas, unavailable databases and exhausted read budgets are not
    empty results. Optional historical fields stay unknown; this reader never
    initializes storage, migrates columns or reconstructs absent reasons.
    """
    result = {
        "available": False,
        "strategy_version": strategy_version,
        "hours": _HOURS,
        "limit": _LIMIT,
        "has_more": False,
        "records": [],
    }
    if not isinstance(strategy_version, str) or not strategy_version.strip():
        return result
    try:
        current_time = time.time() if now is None else float(now)
        if not math.isfinite(current_time):
            return result
        with get_read_only_connection() as conn:
            _bounded_read(conn)
            conn.execute("BEGIN")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(news_cache)")}
            if not _REQUIRED_COLUMNS.issubset(columns):
                return result
            projection = ", ".join(
                name if name in columns else f"'' AS {name}"
                for name in _OUTPUT_COLUMNS
            )
            paths = tuple(sorted(NON_SUBSTANTIVE_SCORE_PATHS))
            placeholders = ", ".join("?" for _ in paths)
            rows = conn.execute(
                f"SELECT {projection} FROM news_cache "
                "WHERE strategy_version = ? COLLATE BINARY "
                "AND created_at >= ? AND created_at <= ? "
                f"AND LOWER(TRIM(score_path)) IN ({placeholders}) "
                "ORDER BY created_at DESC, link ASC LIMIT ?",
                (strategy_version, current_time - _HOURS * 3600, current_time,
                 *paths, _LIMIT + 1),
            ).fetchall()
        result.update(
            available=True,
            has_more=len(rows) > _LIMIT,
            records=[_record(row) for row in rows[:_LIMIT]],
        )
    except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
        # This administrative view must not expose paths or database errors.
        return result
    return result
