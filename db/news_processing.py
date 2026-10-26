"""Best-effort, bounded diagnostics joining article processing to physical calls.

Rows describe the returned processor result, not a public delivery or a scoring
ledger mutation. Existing call logs remain authoritative for usage. Both this
table and those logs currently have no automatic retention policy.
"""
from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import time
from typing import Any, Mapping
from urllib.parse import quote

logger = logging.getLogger(__name__)
WRITE_BUDGET_SECONDS = 0.2
TRACE_BUDGET_SECONDS = 0.2
MAX_TRACE_CALLS = 200
_COLUMNS = (
    "processing_id", "article_key", "link", "feed_url", "strategy_version",
    "started_at", "finished_at", "outcome", "state", "error_type",
    "model_attempts", "result_returned", "reuse_kind", "signal_key", "score",
    "score_path", "aggregation_selected", "signal_association",
    "trigger_source", "refresh_id", "continuation_key", "continuation_attempt",
)
_ORIGIN_COLUMNS = {
    "trigger_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "refresh_id": "TEXT NOT NULL DEFAULT ''",
    "continuation_key": "TEXT NOT NULL DEFAULT ''",
    "continuation_attempt": "INTEGER",
}
_TRIGGER_SOURCES = frozenset({"unknown", "natural_feed", "natural_continuation", "webhook"})


def init_news_processing_storage(cursor) -> None:
    """Add diagnostics without updating existing cache, ledger or call rows."""
    cursor.execute("""CREATE TABLE IF NOT EXISTS news_processing_logs (
        processing_id TEXT PRIMARY KEY NOT NULL,
        article_key TEXT NOT NULL,
        link TEXT NOT NULL,
        feed_url TEXT NOT NULL DEFAULT '',
        strategy_version TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL,
        finished_at REAL NOT NULL,
        outcome TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL CHECK(state IN ('completed', 'deferred', 'error')),
        error_type TEXT NOT NULL DEFAULT '',
        model_attempts INTEGER NOT NULL DEFAULT 0 CHECK(model_attempts >= 0),
        result_returned INTEGER NOT NULL DEFAULT 0 CHECK(result_returned IN (0,1)),
        reuse_kind TEXT NOT NULL DEFAULT '',
        signal_key TEXT,
        score REAL,
        score_path TEXT,
        aggregation_selected INTEGER CHECK(aggregation_selected IN (0,1)),
        signal_association TEXT NOT NULL DEFAULT 'none'
    )""")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_processing_article "
        "ON news_processing_logs(article_key, finished_at DESC)"
    )
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(news_processing_logs)")}
    for name, declaration in _ORIGIN_COLUMNS.items():
        if name not in columns:
            cursor.execute(f"ALTER TABLE news_processing_logs ADD COLUMN {name} {declaration}")


def _origin_values(row: Mapping[str, Any]) -> dict:
    source = row.get("trigger_source", "unknown")
    refresh_id = row.get("refresh_id", "")
    key = row.get("continuation_key", "")
    attempt = row.get("continuation_attempt")
    if not isinstance(source, str) or source not in _TRIGGER_SOURCES:
        raise ValueError("invalid processing trigger source")
    for value, length, name in ((refresh_id, 32, "refresh"), (key, 64, "continuation")):
        if not isinstance(value, str) or (value and not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value)):
            raise ValueError(f"invalid processing {name} identity")
    if attempt is not None and (type(attempt) is not int or not 0 <= attempt <= 3):
        raise ValueError("invalid processing continuation attempt")
    if bool(key) != (attempt is not None):
        raise ValueError("incomplete processing continuation identity")
    return {
        "trigger_source": source,
        "refresh_id": refresh_id,
        "continuation_key": key,
        "continuation_attempt": attempt,
    }


def _row_values(row: Mapping[str, Any], signal_key_fn) -> dict:
    processing_id = str(row.get("processing_id") or "").strip()
    link = str(row.get("link") or "").strip()
    strategy = str(row.get("strategy_version") or "").strip()
    state = str(row.get("state") or "")
    started, finished = float(row["started_at"]), float(row["finished_at"])
    if not processing_id or len(processing_id) > 128 or not link:
        raise ValueError("invalid processing identity")
    if state not in {"completed", "deferred", "error"}:
        raise ValueError("invalid processing state")
    if not all(math.isfinite(value) for value in (started, finished)) or finished < started:
        raise ValueError("invalid processing times")
    error_type = str(row.get("error_type") or "")
    if error_type and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type):
        error_type = "UnknownError"
    article_key = signal_key_fn(link, strategy)
    origin = _origin_values(row)
    if origin["continuation_key"] and origin["continuation_key"] != article_key:
        raise ValueError("processing continuation belongs to another article")
    return {
        "processing_id": processing_id,
        "article_key": article_key,
        "link": link,
        "feed_url": str(row.get("feed_url") or ""),
        "strategy_version": strategy,
        "started_at": started,
        "finished_at": finished,
        "outcome": str(row.get("outcome") or "")[:128],
        "state": state,
        "error_type": error_type,
        "model_attempts": max(0, int(row.get("model_attempts") or 0)),
        "result_returned": int(bool(row.get("result_returned"))),
        "reuse_kind": str(row.get("reuse_kind") or "")[:32],
        "signal_key": None,
        "score": None,
        "score_path": None,
        "aggregation_selected": None,
        "signal_association": "none",
        "signal_key_hint": str(row.get("signal_key_hint") or "").strip(),
    } | origin


def _connection(db_path: str, deadline: float, *, readonly: bool = False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("diagnostic budget exhausted")
    encoded = quote(os.path.abspath(db_path), safe="/")
    conn = sqlite3.connect(
        f"file:{encoded}?mode={'ro' if readonly else 'rw'}", uri=True,
        timeout=remaining,
    )
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return conn


def _execute(conn, deadline: float, sql: str, params=()):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("diagnostic budget exhausted")
    conn.execute(f"PRAGMA busy_timeout={max(0, int(remaining * 1000))}")
    return conn.execute(sql, params)


def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        logger.warning("[NewsProcessing] diagnostic connection close failed", exc_info=False)


def _link_completed_signal(conn, deadline: float, values: dict) -> None:
    if values["state"] != "completed":
        return
    hint = values["signal_key_hint"]
    if hint:
        sql = (
            "SELECT signal_key, score, score_path, selected FROM backtest_signals "
            "WHERE signal_key = ? AND strategy_version = ? AND link = ?"
        )
        params = (hint, values["strategy_version"], values["link"])
    else:
        sql = (
            "SELECT signal_key, score, score_path, selected FROM backtest_signals "
            "WHERE strategy_version = ? AND link = ? "
            "ORDER BY available_at DESC, id DESC LIMIT 1"
        )
        params = (values["strategy_version"], values["link"])
    signal = _execute(conn, deadline, sql, params).fetchone()
    if signal is not None:
        values.update(
            signal_key=signal["signal_key"], score=signal["score"],
            score_path=signal["score_path"], aggregation_selected=signal["selected"],
            signal_association="written_in_processing" if hint else "existing_at_completion",
        )


def record_news_processing(row: Mapping[str, Any]) -> bool:
    """Try one diagnostic insert within one 200ms lock/SQL deadline.

The budget bounds Python/SQLite lock waits and interruptible SQLite work. It is
not a hard OS I/O deadline. Failure (including busy storage) drops diagnostics;
no business result is changed and article text never appears in the warning.
"""
    from db.database import DB_PATH, _backtest_signal_key, db_lock

    deadline = time.monotonic() + WRITE_BUDGET_SECONDS
    locked, conn = False, None
    try:
        values = _row_values(row, _backtest_signal_key)
        remaining = max(0.0, deadline - time.monotonic())
        locked = db_lock.acquire(timeout=remaining)
        if not locked:
            raise TimeoutError("diagnostic lock budget exhausted")
        conn = _connection(DB_PATH, deadline)
        _link_completed_signal(conn, deadline, values)
        cursor = _execute(
            conn, deadline,
            f"INSERT INTO news_processing_logs ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
            "ON CONFLICT(processing_id) DO NOTHING",
            tuple(values[name] for name in _COLUMNS),
        )
        _execute(conn, deadline, "COMMIT")
        return cursor.rowcount == 1
    except Exception:
        logger.warning("[NewsProcessing] diagnostic insert skipped", exc_info=False)
        return False
    finally:
        try:
            _safe_close(conn)
        finally:
            if locked:
                db_lock.release()


def _usage_summary(calls: list[dict]) -> dict:
    complete = [row for row in calls if row["usage_status"] == "complete"]
    partial = [row for row in calls if row["usage_status"] == "partial"]
    reported = complete + partial
    unknown = len(calls) - len(reported)
    return {
        "complete_calls": len(complete), "partial_calls": len(partial),
        "unknown_calls": unknown,
        "unknown_or_partial": bool(unknown or partial),
        "reported_prompt_tokens": sum(int(row["prompt_tokens"] or 0) for row in reported),
        "reported_completion_tokens": sum(int(row["completion_tokens"] or 0) for row in reported),
        "reported_total_tokens": sum(int(row["total_tokens"] or 0) for row in reported),
        "complete_total_tokens": sum(int(row["total_tokens"] or 0) for row in complete),
        "reported_estimated_cost": sum(float(row["estimated_cost"] or 0) for row in reported),
        "scope": "returned_call_rows_only",
        "unlogged_attempts": None,
        "processing_usage_complete": False,
    }


def _processing_usage(calls: list[dict], processing, truncated: bool) -> dict:
    usage = _usage_summary(calls)
    if processing is None or truncated:
        return usage
    attempts = int(processing["model_attempts"])
    missing = max(0, attempts - len(calls))
    usage.update(
        unlogged_attempts=missing,
        unknown_or_partial=bool(usage["unknown_or_partial"] or missing),
        processing_usage_complete=(attempts == len(calls) and not usage["unknown_or_partial"]),
    )
    return usage


def get_news_processing_trace(processing_id: str, limit: int = MAX_TRACE_CALLS) -> dict:
    """Read one result and <=200 linked calls; incomplete usage stays explicit.

A missing processing row does not invent a final outcome, even if call rows
survive. Calls from older releases and direct calls have no processing identity
and are never guessed from their link, timestamp or request ID.
"""
    from db.database import DB_PATH

    try:
        bounded = max(1, min(MAX_TRACE_CALLS, int(limit)))
    except (ValueError, TypeError, OverflowError):
        bounded = MAX_TRACE_CALLS
    result = {
        "found": False, "processing": None, "calls": [], "calls_linked": False,
        "truncated": False, "usage": _usage_summary([]), "status": "ok",
        "unassociated_history": "not_attributable",
    }
    identity = str(processing_id or "").strip()
    if not identity or len(identity) > 128:
        result["status"] = "invalid_processing_id"
        return result
    conn = None
    try:
        deadline = time.monotonic() + TRACE_BUDGET_SECONDS
        conn = _connection(DB_PATH, deadline, readonly=True)
        _execute(conn, deadline, "BEGIN")
        processing = _execute(
            conn, deadline,
            "SELECT * FROM news_processing_logs WHERE processing_id = ?", (identity,),
        ).fetchone()
        rows = _execute(
            conn, deadline,
            "SELECT * FROM llm_call_logs WHERE processing_id = ? "
            "AND processing_id <> '' ORDER BY id LIMIT ?",
            (identity, bounded + 1),
        ).fetchall()
        calls = [dict(row) for row in rows[:bounded]]
        result.update(
            found=processing is not None,
            processing=dict(processing) if processing is not None else None,
            calls=calls, calls_linked=bool(calls), truncated=len(rows) > bounded,
            usage=_processing_usage(calls, processing, len(rows) > bounded),
        )
    except Exception:
        result["status"] = "unavailable"
    finally:
        _safe_close(conn)
    return result
