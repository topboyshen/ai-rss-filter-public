"""Opt-in, replayable evidence for the first rejected attempt of a scoring stage.

Placement: this module is outside the strategy-fingerprint files.  It rides on
the existing ``capture_article_score`` / ``persist_scoring_evidence`` hooks in
``services/score_evidence_capture.py`` (which the scorer already feeds with every
attempt) but has its own switch, its own gate and its own writer, so a full or
stopped legacy capture directory neither starts nor blocks it.

The SQLite write is asynchronous and bounded, not free: the row is built on the
scoring thread after the score is persisted and handed to a bounded in-memory
queue; a daemon thread performs the write with a short busy timeout.  A full
queue, a locked database, a failing or slow writer end in a counted skip rather
than a wait or an exception for scoring.  Building the row, recording attempts
and the enqueue still cost time on the scoring thread when the switch is on; that
cost has not been measured.  With the switch off no capture context, row, queue,
writer thread or connection is created; only a config read and an ``enabled``
check remain.

Stored by design: article title and source text (<= 20,000 characters), the
rejected raw model output and the parsed tuple.  Never stored: the article link
(only its hash), request messages (only their hash), keys, configuration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from db.first_rejection_evidence import (
    MAX_ROWS_HARD,
    MAX_TOTAL_BYTES_HARD,
    SCHEMA_VERSION,
    insert_evidence,
    safe_bounded_int,
)

logger = logging.getLogger(__name__)

PENDING_ATTR = "_first_rejection_pending"
WRITER_THREAD_NAME = "first-rejection-writer"
_RAW_LATER_ATTEMPT_CHARS = 2048
_TITLE_MAX_CHARS = 2000
_TITLE_MAX_BYTES = 8000
_RAW_MAX_CHARS = 4096
_RAW_MAX_BYTES = 16384
# Every project file the verdict path reads a function or constant from.  Audited
# by AST closure from Scorer._validate_scoring_attempt (see the guard test, which
# fails when the path starts reading a module that is not listed here):
#   core/scorer.py          validator, parser, theme rules, repair-safety checks
#   core/score_evidence.py  event/denial/money grounding helpers
#   scoring_provenance.py   SAFE_NEUTRAL_CONTRACT_VALUES (fixed-neutral bypass)
# Telemetry sinks on that path (record_score_validation, capture_validation_attempt,
# core.metrics) do not influence the verdict and are deliberately not listed.
VALIDATOR_FILES = ("core/scorer.py", "core/score_evidence.py", "scoring_provenance.py")
VALIDATOR_VERSION_PREFIX = "v2:"

STATS = {
    "inserted": 0,
    "duplicate": 0,
    "skipped_volume_cap": 0,
    "skipped_row_cap": 0,
    "dropped_queue_full": 0,
    "failed_locked": 0,
    "failed_other": 0,
}
_STATS_LOCK = threading.Lock()


def _bump(name: str) -> None:
    with _STATS_LOCK:
        STATS[name] += 1


def retention_options(cfg: Optional[Mapping]) -> Optional[dict]:
    """Normalized options, or None when the switch is not exactly ``true``.

    A non-finite, non-integer, boolean, negative, zero or out-of-range value falls
    back to the default; the row/volume maxima are hard ceilings (configuration can
    only lower them), never silently clamped to something larger.
    """
    raw = (cfg or {}).get("first_rejection_retention")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    return {
        "retention_days": safe_bounded_int(raw.get("retention_days"), 7, 7, 30),
        # 20,000 characters is the approved ceiling; configuration can only lower it.
        "source_max_chars": safe_bounded_int(raw.get("source_max_chars"), 20000, 1000, 20000),
        "source_max_bytes": safe_bounded_int(raw.get("source_max_bytes"), 80000, 4000, 80000),
        "row_max_bytes": safe_bounded_int(raw.get("row_max_bytes"), 262144, 16384, 262144),
        "max_rows": safe_bounded_int(raw.get("max_rows"), MAX_ROWS_HARD, 1, MAX_ROWS_HARD),
        "max_total_bytes": safe_bounded_int(
            raw.get("max_total_bytes"), MAX_TOTAL_BYTES_HARD, 1024 * 1024, MAX_TOTAL_BYTES_HARD
        ),
        "write_timeout_ms": safe_bounded_int(raw.get("write_timeout_ms"), 200, 10, 2000),
        "queue_max": safe_bounded_int(raw.get("queue_max"), 256, 1, 4096),
    }


def run_revision() -> str:
    """App revision bound to a row: build git SHA if exported, else the release tag."""
    for name in ("AI_FILTER_BUILD_GIT_SHA", "AI_FILTER_VERSION"):
        value = os.environ.get(name, "").strip()
        if value and value != "unknown":
            return value[:128]
    return "unknown"


_VALIDATOR_IDENTITY: Optional[tuple] = None


def validator_identity() -> tuple[str, dict]:
    """(version, {relative file: sha256}) of the code that judges a rejection."""
    global _VALIDATOR_IDENTITY
    if _VALIDATOR_IDENTITY is None:
        _VALIDATOR_IDENTITY = validator_identity_for(Path(__file__).resolve().parents[1])
    return _VALIDATOR_IDENTITY


def validator_identity_for(root: Path) -> tuple[str, dict]:
    """Identity of the validator files under ``root`` ("unknown" if any is unreadable)."""
    try:
        digests = {
            rel: hashlib.sha256((Path(root) / rel).read_bytes()).hexdigest()
            for rel in VALIDATOR_FILES
        }
    except OSError:
        return "unknown", {}
    blob = json.dumps(digests, sort_keys=True).encode()
    return VALIDATOR_VERSION_PREFIX + hashlib.sha256(blob).hexdigest()[:16], digests


def _is_fixed_neutral(parsed: Any) -> bool:
    try:
        from core.scorer import _is_fixed_safe_neutral_score

        return bool(_is_fixed_safe_neutral_score(tuple(parsed)))
    except (ImportError, TypeError):
        return False


def _utf8(text: str) -> int:
    return len(text.encode("utf-8"))


def _later_attempt(attempt: Mapping) -> dict:
    raw = str(attempt.get("raw_response") or "")
    return {
        "attempt": attempt.get("attempt"),
        "backend_name": attempt.get("backend_name"),
        "model_name": attempt.get("model_name"),
        "accepted": bool(attempt.get("accepted")),
        "failure_reason": attempt.get("failure_reason") or "",
        "fact_failure_kind": attempt.get("fact_failure_kind") or "",
        "repair_trigger": attempt.get("repair_trigger") or "",
        "parsed": list(attempt.get("parsed") or []),
        "raw_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "raw_response": raw[:_RAW_LATER_ATTEMPT_CHARS],
        "raw_truncated": len(raw) > _RAW_LATER_ATTEMPT_CHARS,
    }


def _replay_limits(capture: Mapping, first: Mapping, title: str, source: str,
                   raw: str, options: Mapping) -> list[str]:
    limits = []
    if not capture.get("evidence_complete", False):
        limits.append("capture_incomplete")
    if first.get("attempt") != "initial":
        limits.append("not_initial_attempt")
    if not first.get("source_in_request"):
        limits.append("source_not_in_request")
    if len(source) > options["source_max_chars"] or _utf8(source) > options["source_max_bytes"]:
        limits.append("source_over_cap")
    if len(title) > _TITLE_MAX_CHARS or _utf8(title) > _TITLE_MAX_BYTES:
        limits.append("title_over_cap")
    if len(raw) > _RAW_MAX_CHARS or _utf8(raw) > _RAW_MAX_BYTES:
        limits.append("raw_over_cap")
    if validator_identity()[0] == "unknown":
        limits.append("validator_unknown")
    return limits


def _signal_key(snapshot: Mapping) -> str:
    """Read-only reference to the ledger row; the link itself is not stored."""
    try:
        from db.database import _backtest_signal_key

        return _backtest_signal_key(
            snapshot["link"], snapshot["strategy_version"],
            snapshot.get("revision_key") or "base",
        )
    except (ImportError, KeyError, TypeError):
        return ""


def _stage_final(snapshot: Mapping, later: list[dict]) -> tuple[dict, int]:
    accepted = next((item for item in later if item["accepted"]), None)
    neutral = bool(accepted and _is_fixed_neutral(accepted["parsed"]))
    final = {
        "stage_outcome": (
            {
                "accepted": True,
                "backend_name": accepted["backend_name"],
                "model_name": accepted["model_name"],
                "attempt": accepted["attempt"],
                "parsed": accepted["parsed"],
                "is_fixed_safe_neutral": neutral,
            }
            if accepted
            else {"accepted": False}
        ),
        "stored": {
            key: snapshot.get(key)
            for key in ("score", "logic", "direction", "score_path", "selected",
                        "scored_at", "strategy_version", "event_id", "revision_key")
        },
    }
    return final, int(neutral)


def build_rows(capture: Mapping, options: Mapping, snapshot: Mapping) -> list[dict]:
    """One row per stage whose first attempt was rejected; no I/O."""
    event_id = str(snapshot.get("event_id") or "")
    if not event_id:
        return []  # A row that cannot name the persisted event is not bound.
    title = str(capture["source"]["raw_title"])
    source = str(capture["source"]["summary"])
    version, files = validator_identity()
    now = int(time.time())
    rows, stages_seen = [], set()
    attempts = capture["attempts"]
    for index, first in enumerate(attempts):
        stage = first.get("stage")
        if stage in stages_seen:
            continue
        stages_seen.add(stage)
        if first.get("accepted"):
            continue
        later = [_later_attempt(a) for a in attempts[index + 1:] if a.get("stage") == stage]
        raw = str(first.get("raw_response") or "")
        limits = _replay_limits(capture, first, title, source, raw, options)
        final, neutral = _stage_final(snapshot, later)
        stored_source = source[: options["source_max_chars"]]
        while _utf8(stored_source) > options["source_max_bytes"]:
            stored_source = stored_source[: len(stored_source) // 2]
        row = {
            "schema_version": SCHEMA_VERSION,
            "created_at": now,
            "article_id": capture["article_id"],
            "event_id": event_id,
            "signal_key": _signal_key(snapshot),
            "strategy_version": str(capture["strategy_version"]),
            "run_revision": run_revision(),
            "validator_version": version,
            "validator_files": json.dumps(files, sort_keys=True),
            "stage": stage,
            "input_title": title[:_TITLE_MAX_CHARS],
            "input_source": stored_source,
            "input_source_chars": len(source),
            "input_source_bytes": _utf8(source),
            "input_hash": capture["input_hash"],
            "request_hash": str(first.get("request_hash") or ""),
            "rejected_backend": str(first.get("backend_name") or ""),
            "rejected_model": str(first.get("model_name") or ""),
            "rejected_attempt": str(first.get("attempt") or ""),
            "rejected_raw": raw[:_RAW_MAX_CHARS],
            "rejected_parsed": json.dumps(list(first.get("parsed") or []), ensure_ascii=False),
            "failure_reason": str(first.get("failure_reason") or ""),
            "failure_detail": str(first.get("failure_detail") or ""),
            "fact_failure_kind": str(first.get("fact_failure_kind") or ""),
            "repair_trigger": str(first.get("repair_trigger") or ""),
            "later_attempts": json.dumps(later, ensure_ascii=False),
            "final_handling": json.dumps(final, ensure_ascii=False),
            "accepted_is_fixed_neutral": neutral,
            "replayable": 0 if limits else 1,
            "replay_limit": ",".join(limits),
        }
        row["dedupe_key"] = hashlib.sha256(
            "\x1f".join(
                (row["article_id"], row["strategy_version"], stage, row["request_hash"],
                 hashlib.sha256(raw.encode("utf-8")).hexdigest())
            ).encode()
        ).hexdigest()
        row["row_bytes"] = sum(
            _utf8(value) if isinstance(value, str) else 8 for value in row.values()
        )
        rows.append(row)
    return rows


def _write_row(db_path: str, options: Mapping, row: Mapping) -> None:
    """One bounded write.  Every failure is a counted skip, never an exception."""
    try:
        conn = sqlite3.connect(
            db_path, timeout=options["write_timeout_ms"] / 1000.0, check_same_thread=False
        )
    except sqlite3.Error:
        _bump("failed_other")
        return
    try:
        if insert_evidence(
            conn, row, max_rows=options["max_rows"], max_total_bytes=options["max_total_bytes"]
        ):
            _bump("inserted")
        elif conn.execute(
            "SELECT 1 FROM first_rejection_evidence WHERE dedupe_key = ?",
            (row["dedupe_key"],),
        ).fetchone():
            _bump("duplicate")
        else:
            _bump("skipped_volume_cap")
            logger.warning("[FirstRejection] volume cap reached; row skipped")
    except sqlite3.OperationalError as exc:
        _bump("failed_locked" if "locked" in str(exc) or "busy" in str(exc) else "failed_other")
        logger.warning("[FirstRejection] write skipped")
    except Exception:
        _bump("failed_other")
        logger.warning("[FirstRejection] write skipped")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


class _Writer:
    def __init__(self, maxsize: int) -> None:
        self.pid = os.getpid()
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self.thread = threading.Thread(target=self._run, name=WRITER_THREAD_NAME, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        from services.admission_fence import admission

        while True:
            db_path, options, row = self.queue.get()
            try:
                with admission("diagnostic_writer", kind="drain"):
                    _write_row(db_path, options, row)
            except Exception:  # pragma: no cover - defensive; _write_row never raises
                _bump("failed_other")
            finally:
                self.queue.task_done()


_WRITER: Optional[_Writer] = None
_WRITER_LOCK = threading.Lock()


def _writer(options: Mapping) -> _Writer:
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is None or _WRITER.pid != os.getpid():
            _WRITER = _Writer(options["queue_max"])  # also replaces a forked copy
        return _WRITER


def submit(db_path: str, options: Mapping, row: Mapping) -> bool:
    """Non-blocking hand-off; a full queue drops the row and counts it."""
    try:
        _writer(options).queue.put_nowait((db_path, options, row))
        return True
    except queue.Full:
        _bump("dropped_queue_full")
        return False


def flush(timeout: float = 5.0) -> bool:
    """Wait until queued rows are written (tests and orderly shutdown only)."""
    writer = _WRITER
    if writer is None:
        return True
    deadline = time.monotonic() + timeout
    with writer.queue.all_tasks_done:
        while writer.queue.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            writer.queue.all_tasks_done.wait(remaining)
    return True


def persist_first_rejection(entry: Any, signal_snapshot: Mapping) -> None:
    """Called after the score and event identity are persisted; never raises."""
    pending = getattr(entry, PENDING_ATTR, None)
    if not isinstance(pending, tuple) or len(pending) != 2:
        return
    try:
        delattr(entry, PENDING_ATTR)
        capture, options = pending
        if not capture["attempts"]:
            return  # Runtime cache reuse is not a new model attempt.
        from db import database

        for row in build_rows(capture, options, signal_snapshot):
            if row["row_bytes"] > options["row_max_bytes"]:
                _bump("skipped_row_cap")
                logger.warning("[FirstRejection] row over byte cap; skipped")
                continue
            submit(database.DB_PATH, options, row)
    except Exception:
        # Telemetry only: no source, model output, path or exception text.
        logger.warning("[FirstRejection] capture skipped")

