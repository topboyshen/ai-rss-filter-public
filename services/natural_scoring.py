"""Bounded continuations of polling scores, consumed only by natural refreshes.

No configuration/credentials are serialized. A matching scoring contract is
required before replaying the original input. Cache/ledger checks remain ahead
of the durable model-attempt checkpoint, including for webhook arrivals.
"""
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass

import feedparser

from services import natural_score_continuations as store

logger = logging.getLogger(__name__)
DEFERRED = "score_deferred"
TERMINAL_UNSCORED = "score_terminal_unscored"
_active = ContextVar("natural_score_continuation", default=None)


class ContinuationDeferred(Exception):
    """Fail closed without granting another scoring round."""


class ContinuationTerminalUnscored(Exception):
    """No final score is available and this identity cannot be resumed."""


@dataclass
class _Session:
    record: object = None
    scored: bool = False
    initial_attempts: int = 0


def _payload(entry, prompt, feed_url, few_shot_context):
    payload = {
        "title": str(entry.title), "link": str(entry.link),
        "summary": str(entry.get("summary", "") or ""),
        "prompt": prompt, "feed_url": feed_url,
        "few_shot_context": few_shot_context,
    }
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            payload[key] = list(value)
    return payload


def _entry(payload):
    entry = feedparser.FeedParserDict(
        title=payload["title"], link=payload["link"], summary=payload["summary"],
    )
    for key in ("published_parsed", "updated_parsed"):
        if payload.get(key):
            entry[key] = tuple(payload[key])
    return entry


def _finish_record(record, outcome, *, scored):
    data = record.data
    if data["state"] in store.TERMINAL_STATES:
        return
    if outcome[1] == TERMINAL_UNSCORED:
        return
    if outcome[1] != DEFERRED:
        data["state"] = "completed"
    elif not scored:
        return
    elif data["state"] != "commit_pending":
        data["state"] = "pending"
        data["next_attempt_at"] = time.time() + 60 * max(1, data["attempts"])
    data["updated_at"] = time.time()
    record.save(data)


def run_with_continuation(entry, prompt, cfg, feed_url, few_shot_context, run, capture=False):
    """Run under an existing durable identity, or capture the first deferral.

    ``run`` receives (entry, prompt, feed_url, few_shot_context). Its existing
    cache/ledger and fact guards remain authoritative. The caller owns the
    ordinary per-link single-flight lock.
    """
    strategy, link = str(cfg.get("scoring_strategy_version") or ""), str(entry.link)
    if not strategy or not link:
        return run(entry, prompt, feed_url, few_shot_context)
    session = _Session()
    token = _active.set(session)
    initial = None
    invoking = False
    try:
        with store.locked_record(strategy, link) as record:
            session.record = record
            if record is not None and record.data.get("payload"):
                saved = record.data["payload"]
                entry, prompt = _entry(saved), saved["prompt"]
                feed_url, few_shot_context = saved["feed_url"], saved["few_shot_context"]
            elif capture:
                initial = _payload(entry, prompt, feed_url, few_shot_context)
            invoking = True
            outcome = run(entry, prompt, feed_url, few_shot_context)
            invoking = False
            if record is not None:
                _finish_record(record, outcome, scored=session.scored)
                return outcome
        if capture and outcome[1] == DEFERRED:
            result = store.enqueue(
                strategy, link, initial,
                initial_attempts=session.initial_attempts if session.scored else 0,
            )
            if result not in {"stored", "duplicate"}:
                logger.warning("[NaturalContinuation] admission=%s; remains deferred", result)
        return outcome
    except ContinuationTerminalUnscored:
        return None, TERMINAL_UNSCORED
    except (ContinuationDeferred, OSError) as exc:
        if invoking and session.record is None and isinstance(exc, OSError):
            raise
        # Never include original text, model messages, credentials or the URL.
        logger.warning("[NaturalContinuation] unavailable=%s; remains deferred", type(exc).__name__)
        return None, DEFERRED
    finally:
        _active.reset(token)


def before_score():
    """Reserve one whole pipeline round durably BEFORE any model request."""
    session = _active.get()
    if session is None:
        return
    if session.record is None:
        session.scored = True
        return
    data = session.record.data
    if data["state"] in store.TERMINAL_STATES:
        raise ContinuationTerminalUnscored(data["state"])
    if time.time() - data["created_at"] >= store.TTL_SECONDS:
        data["state"] = "expired"
        data["updated_at"] = time.time()
        session.record.save(data)
        raise ContinuationTerminalUnscored("expired")
    if data["attempts"] >= store.MAX_ATTEMPTS:
        data["state"] = "exhausted"
        data["updated_at"] = time.time()
        session.record.save(data)
        raise ContinuationTerminalUnscored("exhausted")
    if data["next_attempt_at"] > time.time():
        raise ContinuationDeferred("not due")
    data.update(state="running", attempts=data["attempts"] + 1, updated_at=time.time())
    data["next_attempt_at"] = time.time() + 60 * data["attempts"]
    session.record.save(data)
    session.scored = True


def before_uncommitted_work():
    """Allow committed reuse, but never revive terminal/expired pending writes."""
    session = _active.get()
    if session is None or session.record is None:
        return
    data = session.record.data
    if data["state"] in store.TERMINAL_STATES:
        raise ContinuationTerminalUnscored(data["state"])
    if time.time() - data["created_at"] >= store.TTL_SECONDS:
        data.update(state="expired", updated_at=time.time())
        session.record.save(data)
        raise ContinuationTerminalUnscored("expired")
    if data["next_attempt_at"] > time.time():
        raise ContinuationDeferred("not due")


def before_source_rejection():
    """A changed/bad arrival cannot disguise a failed terminal identity as filtered."""
    session = _active.get()
    if session is None or session.record is None:
        return
    state = session.record.data["state"]
    if state in store.TERMINAL_STATES - {"completed"}:
        raise ContinuationTerminalUnscored(state)


def durable_attempt():
    session = _active.get()
    if session is None or session.record is None:
        return None
    return session.record.data["attempts"]


def current_processing_metadata():
    """Describe the locked journal actually used by this processing execution.

    Read after the processor finishes, so the attempt includes any durable
    reservation. Reuse keeps the existing count; it never becomes a new model
    attempt. An initial deferral is traced before journal admission completes.
    """
    session = _active.get()
    if session is None or session.record is None:
        return {"continuation_key": "", "continuation_attempt": None}
    data = session.record.data
    return {
        "continuation_key": data["article_key"],
        "continuation_attempt": data["attempts"],
    }


def note_deferred_attempt(attempt):
    session = _active.get()
    if session is not None and session.record is None:
        session.initial_attempts = attempt


def freeze_write(value):
    """A completed decision survives write failure/restart without more models."""
    session = _active.get()
    if session is not None and session.record is not None:
        data = session.record.data
        if data["state"] in store.TERMINAL_STATES:
            raise ContinuationTerminalUnscored(data["state"])
        data.update(state="commit_pending", frozen_result=value, updated_at=time.time())
        session.record.save(data)


def pending_write():
    session = _active.get()
    if session is None or session.record is None:
        return None
    return session.record.data.get("frozen_result")


def bound_feed():
    session = _active.get()
    if session is not None and session.record is not None:
        return session.record.data.get("payload", {}).get("feed_url")
    return None


def resume_continuations(cfg, *, run_entry):
    """One bounded batch; never fetch sources, create tasks or run extra rounds."""
    from db.database import DB
    from services.news_processing_trace import current_refresh_id, processing_origin

    result = {"attempted": 0, "completed": 0, "deferred": 0, "terminal_unscored": 0, "errors": 0}
    strategy = str(cfg.get("scoring_strategy_version") or "")
    if not strategy and not store.journal_directory().exists():
        return result
    terminal_counts = {}
    try:
        jobs = store.pending(strategy, terminal_counts=terminal_counts)
    except (OSError, ValueError):
        logger.warning("[NaturalContinuation] pending journal unavailable")
        result["terminal_unscored"] = sum(terminal_counts.values())
        result["errors"] = 1
        return result
    result["terminal_unscored"] = sum(terminal_counts.values())
    enabled = {
        DB.canonical_feed_url(cfg, str(feed["url"]))
        for feed in cfg.get("feeds", []) if feed.get("enabled", True) and feed.get("url")
    }
    for job in jobs:
        saved = job["payload"]
        try:
            if saved["feed_url"] not in enabled:
                with store.locked_record(strategy, job["link"]) as record:
                    if record and record.data["state"] not in store.TERMINAL_STATES:
                        data = record.data
                        data.update(state="contract_changed", updated_at=time.time())
                        record.save(data)
                        result["terminal_unscored"] += 1
                continue
            result["attempted"] += 1
            with processing_origin("natural_continuation", current_refresh_id()):
                outcome = run_entry(
                    _entry(saved), DB.get_recent_entries(cfg=cfg), saved["prompt"], cfg,
                    feed_url=saved["feed_url"], few_shot_context=saved["few_shot_context"],
                )
            outcome_key = {
                DEFERRED: "deferred", TERMINAL_UNSCORED: "terminal_unscored",
            }.get(outcome[1], "completed")
            result[outcome_key] += 1
        except Exception:
            result["errors"] += 1
            logger.warning("[NaturalContinuation] execution failed; journal retained")
    try:
        result["remaining"] = store.overview(strategy)
    except OSError:
        result["errors"] += 1
        logger.warning("[NaturalContinuation] remaining work could not be counted")
    return result
