"""Associate existing physical model calls with one news-processing execution.

Only identities and processing outcomes are retained here. Article text, model
messages and responses are deliberately excluded from the execution record.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar


logger = logging.getLogger(__name__)
_ACTIVE_PROCESSING: ContextVar[dict | None] = ContextVar(
    "active_news_processing", default=None
)
_REUSE_KINDS = frozenset({"cache", "ledger", "similarity", "score_cache"})
_PROCESSING_ORIGIN: ContextVar[tuple[str, str]] = ContextVar(
    "news_processing_origin", default=("unknown", "")
)


@contextmanager
def processing_origin(source: str, refresh_id: str = ""):
    """Bind the executing entry point, never an article's original arrival.

    Callers supply a refresh ID only inside the actual refresh. Webhook and
    standalone processing explicitly clear it, even inside a nested context.
    """
    token = _PROCESSING_ORIGIN.set((source, refresh_id))
    try:
        yield
    finally:
        _PROCESSING_ORIGIN.reset(token)


def current_refresh_id() -> str:
    return _PROCESSING_ORIGIN.get()[1]


def _new_processing_state(entry, cfg, feed_url):
    # Import only while processing an entry: the scorer and database import
    # each other during startup, but no tracing connection is needed then.
    from db.database import _backtest_signal_key

    link = str(getattr(entry, "link", "") or "").strip()
    strategy = str(cfg.get("scoring_strategy_version") or "").strip()
    source, refresh_id = _PROCESSING_ORIGIN.get()
    return {
        "processing_id": uuid.uuid4().hex,
        "article_key": _backtest_signal_key(link, strategy) if link else "",
        "link": link,
        "feed_url": str(feed_url or ""),
        "strategy_version": strategy,
        "trigger_source": source,
        "refresh_id": refresh_id,
        "started_at": int(time.time()),
        "model_attempts": 0,
        "reuse_kind": "",
        "signal_key_hint": "",
        "error_type": "",
    }


def get_news_processing_call_metadata() -> dict:
    """Return empty identities for model calls outside news processing."""
    try:
        state = _ACTIVE_PROCESSING.get()
        if state is None:
            return {}
        return {
            "processing_id": state["processing_id"],
            "article_key": state["article_key"],
        }
    except Exception:
        return {}


def record_news_model_attempt() -> None:
    """Count an SDK dispatch independently of the existing per-feed capture."""
    try:
        state = _ACTIVE_PROCESSING.get()
        if state is not None:
            state["model_attempts"] += 1
    except Exception:
        # Observability must never suppress a prepared model request.
        logger.warning("[NewsProcessing] model attempt accounting unavailable")


def mark_news_processing_reuse(kind: str) -> None:
    try:
        state = _ACTIVE_PROCESSING.get()
        if state is not None and kind in _REUSE_KINDS:
            state["reuse_kind"] = kind
    except Exception:
        logger.warning("[NewsProcessing] reuse accounting unavailable")


def record_news_processing_signal(link, strategy_version, signal_key) -> None:
    """Capture an inserted signal identity only for this execution's article."""
    try:
        state = _ACTIVE_PROCESSING.get()
        if state is None:
            return
        if (
            str(link or "").strip() != state["link"]
            or str(strategy_version or "").strip() != state["strategy_version"]
        ):
            return
        key = str(signal_key or "").strip()
        if key:
            state["signal_key_hint"] = key
    except Exception:
        logger.warning("[NewsProcessing] signal accounting unavailable")


def _processing_row(state):
    from services.natural_scoring import current_processing_metadata

    result = state.get("result")
    has_result = isinstance(result, tuple) and len(result) == 2
    outcome = str(result[1] or "") if has_result else "unknown"
    error_type = state.get("error_type", "")
    status = (
        "error" if error_type or outcome == "score_terminal_unscored"
        else "deferred" if outcome == "score_deferred" else "completed"
    )
    return {
        key: state[key]
        for key in (
            "processing_id", "article_key", "link", "feed_url",
            "strategy_version", "trigger_source", "refresh_id",
            "started_at", "model_attempts", "reuse_kind",
            "signal_key_hint",
        )
    } | {
        "finished_at": int(time.time()),
        "outcome": "error" if error_type else outcome,
        "state": status,
        "error_type": error_type,
        "result_returned": bool(has_result and result[0] is not None),
    } | current_processing_metadata()


@contextmanager
def trace_news_processing(entry, cfg, feed_url):
    """Keep execution context isolated and preserve returns and exceptions."""
    state = {}
    token = None
    initialized = False
    try:
        # Clear an outer execution even if this execution cannot initialize.
        token = _ACTIVE_PROCESSING.set(None)
        state = _new_processing_state(entry, cfg, feed_url)
        _ACTIVE_PROCESSING.set(state)
        initialized = True
    except Exception:
        logger.warning("[NewsProcessing] execution accounting unavailable")
    try:
        yield state
    except BaseException as error:
        state["error_type"] = type(error).__name__
        raise
    finally:
        if token is not None:
            try:
                _ACTIVE_PROCESSING.reset(token)
            except Exception:
                logger.warning("[NewsProcessing] execution context cleanup unavailable")
        if initialized:
            try:
                from db.database import DB

                DB.record_news_processing(_processing_row(state))
            except Exception:
                logger.warning("[NewsProcessing] execution result was not recorded")
