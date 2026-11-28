"""Refresh readiness predicates shared by the writer and read models."""

from __future__ import annotations


def deferred_only_partial(attempt: dict | None) -> bool:
    """A partial refresh caused only by durably queued scoring is not degraded.

    Deferred entries are retried by the continuation worker.  They remain
    visible in ``score_deferred_entries`` and the continuation journal, but
    they should not page as a failed collection while no terminal or source
    error exists.  The full source pull did complete, so the refresh timestamp
    may advance even though the scoring status stays partial.
    """
    if not isinstance(attempt, dict) or attempt.get("status") != "partial":
        return False
    if int(attempt.get("failed_sources") or 0) != 0:
        return False
    if int(attempt.get("source_unrecognized_entries") or 0) != 0:
        return False
    if int(attempt.get("score_terminal_unscored_entries") or 0) != 0:
        return False
    continuation = attempt.get("score_continuations")
    if not isinstance(continuation, dict):
        return False
    if int(continuation.get("errors") or 0) != 0:
        return False
    if int(continuation.get("terminal_unscored") or 0) != 0:
        return False
    remaining = continuation.get("remaining")
    active = int(remaining.get("active") or 0) if isinstance(remaining, dict) else 0
    return bool(
        int(attempt.get("score_deferred_entries") or 0)
        or int(continuation.get("deferred") or 0)
        or active
    )
