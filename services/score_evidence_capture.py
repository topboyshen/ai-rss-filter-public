"""Opt-in bounded scoring evidence. Capture exact source, never display summaries.

No automatic retention, upload, DB migration or model calls. Failure to capture
must not affect scoring, but incomplete records are never usable as Gold.
"""

from __future__ import annotations

import contextvars
import fcntl
import logging
import os
import time
from functools import wraps
from pathlib import Path

from services.first_rejection_retention import (
    PENDING_ATTR,
    persist_first_rejection,
    retention_options,
)
from utils.score_artifacts import (
    canonical_json,
    content_hash,
    private_directory,
    write_private_bytes,
)

logger = logging.getLogger(__name__)
_CAPTURE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "score_evidence", default=None
)
SCHEMA = "score-evidence-v1"
_COMPLETE_FILE = ".capture.complete"


def _capture_stopped(options) -> bool:
    try:
        marker = Path(options["directory"]) / _COMPLETE_FILE
        return marker.exists() or marker.is_symlink()
    except (KeyError, TypeError, ValueError, OSError):
        return True  # Invalid capture configuration must not break scoring.


def _isolated_retention_options(cfg):
    """Retention init runs before scoring: any failure means "off", never an error."""
    try:
        return retention_options(cfg)
    except Exception:
        logger.warning("[FirstRejection] retention disabled for this entry: bad configuration")
        return None


def capture_article_score(function):
    @wraps(function)
    def wrapped(ctx, result, scorer, raw_summary, *args, **kwargs):
        options = ctx.cfg.get("score_evidence_capture") or {}
        legacy = (
            isinstance(options, dict)
            and options.get("enabled") is True
            and not _capture_stopped(options)
        )
        # The first-rejection retention has its own switch and does not look at
        # the legacy directory: a full/stopped cohort must not stop it.
        retention = _isolated_retention_options(ctx.cfg)
        if not legacy and retention is None:
            return function(ctx, result, scorer, raw_summary, *args, **kwargs)
        capture = {
            "schema": SCHEMA,
            "strategy_version": ctx.strategy_version,
            "article_id": content_hash(str(getattr(ctx.entry, "link", "") or "")),
            "source": {"raw_title": ctx.raw_title, "summary": raw_summary},
            "captured_at": int(time.time()),
            "attempts": [],
            "evidence_complete": True,
        }
        capture["input_hash"] = content_hash(capture["source"])
        token = _CAPTURE.set(capture)
        try:
            return function(ctx, result, scorer, raw_summary, *args, **kwargs)
        finally:
            _CAPTURE.reset(token)
            # Pending memory is attached only to this entry, never a global last-call slot.
            if legacy:
                setattr(ctx.entry, "_score_evidence_pending", (capture, dict(options)))
            if retention is not None:
                setattr(ctx.entry, PENDING_ATTR, (capture, retention))

    return wrapped


def capture_validation_attempt(
    *,
    raw,
    parsed,
    title,
    summary,
    backend,
    stage,
    attempt,
    reason,
    detail,
    repair_trigger,
    fact_failure_kind="",
    messages=None,
) -> None:
    capture = _CAPTURE.get()
    if capture is None:
        return
    if (
        capture["source"] != {"raw_title": title, "summary": summary}
        or len(capture["attempts"]) >= 16
    ):
        capture["evidence_complete"] = False
        return
    if messages is None:
        capture["evidence_complete"] = False
    source_in_request = any(
        f"标题：{title}\n摘要：{summary}\n" in message.get("content", "")
        for message in (messages or [])
    )
    if attempt == "initial" and not source_in_request:
        capture["evidence_complete"] = False
    capture["attempts"].append(
        {
            "request_hash": content_hash(messages),
            "source_in_request": source_in_request,
            "backend_name": backend.name,
            "model_name": backend.model,
            "stage": stage,
            "attempt": attempt,
            "attempted_at": int(time.time()),
            "raw_response": raw,
            "parsed": list(parsed),
            "accepted": not reason,
            "failure_reason": reason,
            "failure_detail": detail,
            "fact_failure_kind": fact_failure_kind,
            "repair_trigger": repair_trigger,
        }
    )


def _publish_capture(capture, options) -> None:
    directory = private_directory(Path(options["directory"]))
    maximum = int(options.get("max_records", 500))
    size_limit = int(options.get("max_record_bytes", 262144))
    if not 1 <= maximum <= 5000 or not 1024 <= size_limit <= 1048576:
        raise ValueError("invalid evidence capture budget")
    # Count the actual UTF-8 file bytes, including the terminal newline.
    payload = canonical_json(capture) + b"\n"
    if len(payload) > size_limit:
        raise ValueError("oversized evidence; omitted, not truncated")
    lock_fd = os.open(
        directory / ".capture.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = directory / _COMPLETE_FILE
        if marker.exists() or marker.is_symlink():
            raise ValueError("evidence capture cohort is complete")
        count = sum(1 for _ in directory.glob("*.json"))
        if count >= maximum:
            write_private_bytes(marker, b"complete\n")
            raise ValueError("evidence capture budget exhausted")
        write_private_bytes(directory / f"{capture['evidence_id']}.json", payload)
        if count + 1 >= maximum:
            write_private_bytes(marker, b"complete\n")


def persist_scoring_evidence(entry, signal_snapshot) -> None:
    """Called after the DB write binds event identity; never repairs historical rows."""
    # Retention reads the shared capture before the legacy path mutates it.
    persist_first_rejection(entry, signal_snapshot)
    pending = getattr(entry, "_score_evidence_pending", None)
    if not isinstance(pending, tuple) or len(pending) != 2:
        return
    delattr(entry, "_score_evidence_pending")
    capture, options = pending
    if not capture["attempts"]:
        return  # Runtime cache reuse is not a new model attempt.
    try:
        if not signal_snapshot.get("event_id") or not signal_snapshot.get("event_key"):
            raise ValueError("missing persisted event identity")
        capture.update(
            {
                "event_id": str(signal_snapshot["event_id"]),
                "event_group": content_hash(str(signal_snapshot["event_key"])),
                "final_decision": {
                    key: signal_snapshot.get(key)
                    for key in (
                        "score",
                        "direction",
                        "score_path",
                        "selected",
                        "scored_at",
                        "strategy_version",
                    )
                },
            }
        )
        if capture["final_decision"]["strategy_version"] != capture["strategy_version"]:
            raise ValueError("evidence/decision contract mismatch")
        capture["evidence_id"] = content_hash(capture)
        _publish_capture(capture, options)
    except (KeyError, TypeError, ValueError, OSError):
        # No source, model output, configured path or exception text in telemetry.
        logger.warning(
            "[ScoreEvidence] capture incomplete or skipped; inspect local configuration/budget"
        )
