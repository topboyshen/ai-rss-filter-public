"""Drain due natural-score continuation entries on a short timer.

``refresh._execute_feed_refresh`` runs one continuation batch before it starts
fetching feeds.  An entry deferred later in that round, or created by a feed
that finishes after the batch, would otherwise wait for the next full round
(up to roughly two hours).  This worker runs the same bounded batch on its own
timer so the journal is drained between refreshes.

The batch never fetches sources, creates tasks, or starts a new refresh.  If a
full refresh is already running, the tick is skipped; the refresh owns the
scoring lane and its own continuation batch.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid

from services.admission_fence import AdmissionClosed, guarded

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
DEFAULT_INITIAL_DELAY_SECONDS = 30

_INTERVAL_ENV = "AI_FILTER_SCORE_CONTINUATION_INTERVAL_SECONDS"
_INITIAL_DELAY_ENV = "AI_FILTER_SCORE_CONTINUATION_INITIAL_DELAY_SECONDS"


def _bounded_seconds(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def interval_seconds() -> int:
    return _bounded_seconds(
        _INTERVAL_ENV,
        DEFAULT_INTERVAL_SECONDS,
        MIN_INTERVAL_SECONDS,
        MAX_INTERVAL_SECONDS,
    )


def initial_delay_seconds() -> int:
    return _bounded_seconds(
        _INITIAL_DELAY_ENV,
        DEFAULT_INITIAL_DELAY_SECONDS,
        0,
        MAX_INTERVAL_SECONDS,
    )


def _refresh_running() -> bool:
    from routes.shared import is_refresh_job_running

    return is_refresh_job_running()


def _resolve_cfg():
    from core.user_feedback import resolve_scoring_contract
    from routes.shared import _cfg

    return resolve_scoring_contract(_cfg(), register_snapshot=False)


@guarded("score_continuations_timer", kind="intake")
def _run_batch(cfg):
    from services.refresh import run_continuations_batch

    return run_continuations_batch(cfg, refresh_id=uuid.uuid4().hex)


def run_once(*, cfg=None):
    """Run one bounded batch unless a full refresh currently owns the lane."""
    if _refresh_running():
        return {"skipped": "refresh_running"}
    if cfg is None:
        cfg = _resolve_cfg()
    result = _run_batch(cfg)
    logger.info(
        "[ScoreContinuation] attempted=%d completed=%d deferred=%d "
        "terminal_unscored=%d errors=%d remaining=%s",
        result.get("attempted", 0),
        result.get("completed", 0),
        result.get("deferred", 0),
        result.get("terminal_unscored", 0),
        result.get("errors", 0),
        result.get("remaining"),
    )
    return result


def _loop(stop: threading.Event, *, run_once_fn=None) -> None:
    run = run_once_fn or run_once
    delay = initial_delay_seconds()
    if delay and stop.wait(delay):
        return
    while not stop.is_set():
        try:
            run()
        except AdmissionClosed:
            logger.info("[ScoreContinuation] held by admission fence")
        except Exception:
            # A scoring or database failure must not kill the timer; the
            # journal keeps the record for the next tick.
            logger.exception("[ScoreContinuation] batch failed; timer retained")
        if stop.wait(interval_seconds()):
            break


def start_score_continuation_worker(stop: threading.Event | None = None) -> threading.Thread:
    """Start the daemon timer; the caller's stop event ends it."""
    event = stop if stop is not None else threading.Event()
    thread = threading.Thread(
        target=_loop,
        args=(event,),
        name="score-continuation",
        daemon=True,
    )
    thread.start()
    logger.info(
        "[ScoreContinuation] worker started interval=%ss initial_delay=%ss",
        interval_seconds(),
        initial_delay_seconds(),
    )
    return thread
