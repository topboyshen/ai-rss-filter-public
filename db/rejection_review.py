"""Bounded reads and the durable human-review ledger for rejected records.

Reading is strictly read-only and bounded by both a deadline and a row cap, so
a slow or legacy database yields an unavailable page instead of an unbounded
scan.  Writing never rewrites ``news_cache``: the score and score path that
caused a rejection stay exactly as recorded, and each decision row freezes the
inputs a person saw when making the call.  Decisions therefore survive the
72h cache cleanup that removes most low-score rows.
"""

from __future__ import annotations

import math
import sqlite3
import time

from db.database import (
    db_lock,
    get_connection,
    get_read_only_connection,
    pass_threshold_for_feed,
)


REJECTION_WINDOW_HOURS = 72
PAGE_SIZE = 25
MAX_PAGE = 200
MAX_ROWS_READ = 20000
READ_SECONDS = 2.0
NOTE_MAX_LENGTH = 500
DECISION_RESTORED = "restored"
DECISION_CONFIRMED = "confirmed"
DECISIONS = (DECISION_RESTORED, DECISION_CONFIRMED)
PATH_FILTER_NONE = "__none__"

_PROGRESS_INTERVAL = 1000
_MAX_READ_STEPS = 4_000_000
_COLUMNS = (
    "link",
    "title",
    "raw_title",
    "feed_url",
    "created_at",
    "score",
    "score_path",
    "review_reason",
    "logic",
    "strategy_version",
)


def _bounded_read(conn) -> None:
    """Bound both lock waits and scans even on legacy databases."""
    deadline = time.monotonic() + READ_SECONDS
    steps = 0

    def exhausted():
        nonlocal steps
        steps += _PROGRESS_INTERVAL
        return steps >= _MAX_READ_STEPS or time.monotonic() >= deadline

    conn.execute("PRAGMA busy_timeout=250")
    conn.set_progress_handler(exhausted, _PROGRESS_INTERVAL)


def _max_pass_threshold(cfg: dict) -> float:
    """Upper bound used only to pre-filter the SQL scan, never as a verdict."""
    thresholds = [pass_threshold_for_feed(cfg, "")]
    seen = set()
    for feed in cfg.get("feeds") or []:
        if not isinstance(feed, dict):
            continue
        url = str(feed.get("url") or "")
        if not url or url in seen or feed.get("enabled", True) is False:
            continue
        seen.add(url)
        thresholds.append(pass_threshold_for_feed(cfg, url))
    return max(thresholds)


def _reason_codes(value) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(code for code in str(value or "").split(",") if code)
    )


def _active_decisions(conn) -> dict:
    try:
        rows = conn.execute(
            "SELECT link, decision, note, decided_at "
            "FROM article_review_decisions WHERE revoked_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        # A web process that started before the additive table existed must not
        # turn the whole view into a 503; it simply has no decisions to show.
        return {}
    return {row["link"]: dict(row) for row in rows}


def _item(row, threshold: float, decision: dict) -> dict:
    return {
        "link": row["link"],
        "title": row["title"],
        "raw_title": row["raw_title"],
        "feed_url": str(row["feed_url"] or ""),
        "created_at": row["created_at"],
        "score": float(row["score"]),
        "threshold": float(threshold),
        "score_path": str(row["score_path"] or ""),
        "review_reason": str(row["review_reason"] or ""),
        "reason_codes": _reason_codes(row["review_reason"]),
        "logic": str(row["logic"] or ""),
        "strategy_version": str(row["strategy_version"] or ""),
        "decision": str(decision.get("decision") or ""),
        "decision_note": str(decision.get("note") or ""),
        "decision_at": decision.get("decided_at"),
    }


def read_rejection_page(
    cfg: dict,
    *,
    path_filter: str = "",
    reason_filter: str = "",
    decision_filter: str = "",
    page: int = 1,
) -> dict:
    """Return one bounded page of records rejected inside the 72h window."""
    cfg = cfg or {}
    now = int(time.time())
    since = now - REJECTION_WINDOW_HOURS * 3600
    try:
        page = max(1, min(MAX_PAGE, int(page or 1)))
    except (TypeError, ValueError):
        page = 1
    max_threshold = _max_pass_threshold(cfg)

    with get_read_only_connection() as conn:
        _bounded_read(conn)
        window_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM news_cache WHERE created_at >= ?",
                (since,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT "
            + ", ".join(_COLUMNS)
            + " FROM news_cache WHERE created_at >= ? AND score IS NOT NULL"
            " AND score < ? ORDER BY created_at DESC, link DESC LIMIT ?",
            (since, max_threshold, MAX_ROWS_READ),
        ).fetchall()
        truncated = len(rows) >= MAX_ROWS_READ
        decisions = _active_decisions(conn)

    thresholds: dict[str, float] = {}
    rejected = []
    for row in rows:
        feed_url = str(row["feed_url"] or "")
        if feed_url not in thresholds:
            thresholds[feed_url] = pass_threshold_for_feed(cfg, feed_url)
        threshold = thresholds[feed_url]
        try:
            score = float(row["score"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score) or score >= threshold:
            continue
        rejected.append(
            _item(row, threshold, decisions.get(row["link"]) or {})
        )

    by_path: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for item in rejected:
        by_path[item["score_path"]] = by_path.get(item["score_path"], 0) + 1
        for code in item["reason_codes"]:
            by_reason[code] = by_reason.get(code, 0) + 1

    matches = rejected
    if path_filter == PATH_FILTER_NONE:
        matches = [i for i in matches if not i["score_path"]]
    elif path_filter:
        matches = [i for i in matches if i["score_path"] == path_filter]
    if reason_filter == "rule":
        matches = [i for i in matches if i["reason_codes"]]
    elif reason_filter == "none":
        matches = [i for i in matches if not i["reason_codes"]]
    elif reason_filter:
        matches = [i for i in matches if reason_filter in i["reason_codes"]]
    if decision_filter in DECISIONS:
        matches = [i for i in matches if i["decision"] == decision_filter]

    real_pages = max(1, math.ceil(len(matches) / PAGE_SIZE))
    pages = min(real_pages, MAX_PAGE)
    page = min(page, pages)
    start = (page - 1) * PAGE_SIZE

    return {
        "available": True,
        "generated_at": now,
        "window_hours": REJECTION_WINDOW_HOURS,
        "window_total": window_total,
        "truncated": truncated,
        "page": page,
        "pages": pages,
        "capped": real_pages > pages,
        "page_capacity": MAX_PAGE * PAGE_SIZE,
        "has_prev": page > 1,
        "has_next": page < pages,
        "matches": len(matches),
        "items": matches[start : start + PAGE_SIZE],
        "summary": {
            "rejected": len(rejected),
            "with_rule_reason": sum(
                1 for i in rejected if i["reason_codes"]
            ),
            "restored": sum(
                1 for i in rejected if i["decision"] == DECISION_RESTORED
            ),
            "confirmed": sum(
                1 for i in rejected if i["decision"] == DECISION_CONFIRMED
            ),
            "by_path": sorted(
                by_path.items(), key=lambda kv: (-kv[1], kv[0])
            ),
            "by_reason": sorted(
                by_reason.items(), key=lambda kv: (-kv[1], kv[0])
            ),
        },
        "filters": {
            "path": path_filter,
            "reason": reason_filter,
            "decision": decision_filter,
        },
    }


def find_rejected_record(link: str, cfg: dict) -> dict | None:
    """Return the frozen decision inputs, but only for a reviewable rejection."""
    cfg = cfg or {}
    since = int(time.time()) - REJECTION_WINDOW_HOURS * 3600
    with get_read_only_connection() as conn:
        _bounded_read(conn)
        row = conn.execute(
            "SELECT "
            + ", ".join(_COLUMNS)
            + " FROM news_cache WHERE link = ? AND created_at >= ?"
            " AND score IS NOT NULL",
            (link, since),
        ).fetchone()
    if row is None:
        return None
    threshold = pass_threshold_for_feed(cfg, str(row["feed_url"] or ""))
    try:
        score = float(row["score"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score >= threshold:
        return None
    return _item(row, threshold, {})


def record_decision(item: dict, decision: str, note: str = "") -> None:
    """Append a decision; switching the verdict revokes the previous one."""
    if decision not in DECISIONS:
        raise ValueError("unknown review decision")
    note = str(note or "")[:NOTE_MAX_LENGTH]
    now = int(time.time())
    with db_lock:
        with get_connection() as conn:
            conn.execute(
                "UPDATE article_review_decisions SET revoked_at = ? "
                "WHERE link = ? AND revoked_at IS NULL AND decision <> ?",
                (now, item["link"], decision),
            )
            try:
                conn.execute(
                    "INSERT INTO article_review_decisions "
                    "(link, decision, note, raw_title, feed_url, score, "
                    "score_path, review_reason, logic, strategy_version, "
                    "decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["link"],
                        decision,
                        note,
                        str(item["raw_title"] or item["title"] or ""),
                        item["feed_url"],
                        item["score"],
                        item["score_path"],
                        item["review_reason"],
                        item["logic"],
                        item["strategy_version"],
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # The same active verdict already exists; the click is idempotent.
                pass
            conn.commit()


def revoke_decision(link: str) -> None:
    """Retire the active verdict, keeping the append-only history intact."""
    now = int(time.time())
    with db_lock:
        with get_connection() as conn:
            conn.execute(
                "UPDATE article_review_decisions SET revoked_at = ? "
                "WHERE link = ? AND revoked_at IS NULL",
                (now, link),
            )
            conn.commit()
