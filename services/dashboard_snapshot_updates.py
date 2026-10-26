"""Durable, level-triggered publication of committed dashboard inputs.

The input counter lives in existing ``app_meta`` and is advanced in the same
transaction as a cache/signal write.  Queue receipts are not freshness evidence:
only successful worker publication acknowledges a captured input revision.
"""
from __future__ import annotations

import logging
import os
import time

from services.dashboard_snapshots import snapshots_enabled

logger = logging.getLogger(__name__)
INPUT_REVISION_KEY = "dashboard_realtime_input_revision"
PUBLISHED_REVISION_KEY = "dashboard_realtime_published_revision"
PUBLISHED_AT_KEY = "dashboard_realtime_published_at"
REALTIME_MERGE_SECONDS = 30
REALTIME_CARDS = frozenset({"briefing", "realtime", "event_radar"})


def _revision(value: object) -> int:
    raw = "0" if value is None else str(value)
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("invalid dashboard input revision")
    return int(raw)


def mark_realtime_inputs_dirty(conn) -> None:
    """Advance within the caller's write transaction; never commit separately."""
    if not snapshots_enabled():
        return
    row = conn.execute("SELECT v FROM app_meta WHERE k=?", (INPUT_REVISION_KEY,)).fetchone()
    revision = _revision(row[0] if row else 0) + 1
    conn.execute(
        "INSERT INTO app_meta(k,v) VALUES (?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (INPUT_REVISION_KEY, str(revision)),
    )


def realtime_revision_status() -> dict[str, int]:
    """Read only three metadata rows; no cache scan or implicit DB creation."""
    if not snapshots_enabled():
        return {"input": 0, "published": 0, "published_at": 0}
    from db.database import get_read_only_connection

    with get_read_only_connection() as conn:
        rows = conn.execute(
            "SELECT k,v FROM app_meta WHERE k IN (?,?,?)",
            (INPUT_REVISION_KEY, PUBLISHED_REVISION_KEY, PUBLISHED_AT_KEY),
        ).fetchall()
    values = {row[0]: _revision(row[1]) for row in rows}
    return {
        "input": values.get(INPUT_REVISION_KEY, 0),
        "published": values.get(PUBLISHED_REVISION_KEY, 0),
        "published_at": values.get(PUBLISHED_AT_KEY, 0),
    }


def acknowledge_realtime_revision(revision: int) -> None:
    """Acknowledge only captured inputs, preserving writes made during render."""
    revision = _revision(revision)
    if not snapshots_enabled() or not revision:
        return
    from db.database import db_lock, get_connection

    with db_lock, get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT k,v FROM app_meta WHERE k IN (?,?)",
            (INPUT_REVISION_KEY, PUBLISHED_REVISION_KEY),
        ).fetchall()
        values = {row[0]: _revision(row[1]) for row in rows}
        latest = values.get(INPUT_REVISION_KEY, 0)
        previous = values.get(PUBLISHED_REVISION_KEY, 0)
        if revision > latest:
            raise ValueError("cannot acknowledge an uncommitted dashboard input")
        conn.executemany(
            "INSERT INTO app_meta(k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            [(PUBLISHED_REVISION_KEY, str(max(previous, revision))),
             (PUBLISHED_AT_KEY, str(int(time.time())))],
        )
        conn.commit()


def enqueue_pending_realtime_snapshot() -> str:
    from services.admission_fence import admission

    # A bounded read-only hint avoids permanent leases for clean idle ticks.
    # Dirty inputs are re-read under the activity lease before any enqueue.
    if os.environ.get("AI_FILTER_ADMISSION_MODE", "disabled") != "disabled":
        if not snapshots_enabled():
            return "disabled"
        try:
            state = realtime_revision_status()
            if state["input"] <= state["published"]:
                return "clean"
            if 0 <= int(time.time()) - state["published_at"] < REALTIME_MERGE_SECONDS:
                return "coalesced"
        except Exception:
            logger.warning("Dashboard publication read-only hint unavailable")
            return "unavailable"
    with admission("dirty_input_publication", kind="drain"):
        return _enqueue_pending_realtime_admitted()


def _enqueue_pending_realtime_admitted() -> str:
    """Recover pending work after queue saturation, batch failure or restart.

    A short merge interval limits cheap-card publication, not source freshness.
    The durable spool additionally bounds this task type to one successor.
    A changing revision defeats a completed receipt in the same time bucket;
    the retry window lets a still-dirty revision recover after a dead letter.
    """
    if not snapshots_enabled():
        return "disabled"
    try:
        state = realtime_revision_status()
        if state["input"] <= state["published"]:
            return "clean"
        now = int(time.time())
        if 0 <= now - state["published_at"] < REALTIME_MERGE_SECONDS:
            return "coalesced"
        from routes.webhook_routes import _submit_background_task
        from services.release_info import release_version

        return _submit_background_task({
            "task_type": "dashboard_realtime_refresh",
            "snapshot_version": 1,
            "release_version": release_version(),
            "realtime_input_revision": state["input"],
            "request_window": now // REALTIME_MERGE_SECONDS,
        })
    except Exception:
        # No marker is cleared on an enqueue/read error.  Worker health ticks
        # retry this durable obligation independently of webhook batch success.
        logger.exception("Dashboard pending publication could not be queued")
        return "unavailable"


def capture_realtime_revision() -> int:
    """Take the acknowledged build boundary immediately before worker render."""
    return realtime_revision_status()["input"]


def acknowledge_published_realtime(result: dict, revision: int) -> None:
    if (
        REALTIME_CARDS <= set(result.get("ok") or ())
        and not REALTIME_CARDS.intersection(result.get("failed") or ())
        and not REALTIME_CARDS.intersection(result.get("deferred") or ())
    ):
        acknowledge_realtime_revision(revision)
