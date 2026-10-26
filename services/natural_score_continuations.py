"""Bounded private journal for continuing an unfinished natural-feed score.

This module does not import the processor, start workers, open the database, or
call a model. A caller must hold ``locked_record`` while reserving an attempt
and while using a frozen result. Missing records are read without creating any
files. All identity locks are nonblocking; contention is never a fresh budget.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path

SCHEMA = 1
MAX_ACTIVE_RECORDS = 128
MAX_RECORDS = 4096
MAX_RECORD_BYTES = 256 * 1024
MAX_ATTEMPTS = 3
TTL_SECONDS = 24 * 60 * 60
ACTIVE_STATES = frozenset({"pending", "running", "commit_pending"})
TERMINAL_STATES = frozenset({"completed", "expired", "contract_changed", "exhausted", "invalid"})
_PAYLOAD_KEYS = frozenset({
    "title", "link", "summary", "prompt", "feed_url", "few_shot_context", "published",
    "published_parsed", "updated_parsed",
})
_FORBIDDEN_KEYS = frozenset({"cfg", "config", "api_key", "authorization", "headers", "messages"})
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS = weakref.WeakValueDictionary()


class ContinuationBusy(OSError):
    """Another execution owns this journal or record; do not run the model."""


class ContinuationInvalid(OSError):
    """An unknown, malformed, oversized or inconsistent record is retained."""


def journal_directory() -> Path:
    fallback = Path(__file__).resolve().parents[1] / "data" / "cache.db"
    return Path(os.environ.get("AI_FILTER_DB_PATH") or fallback).parent / "natural_score_continuations"


def identity(strategy: str, link: str) -> tuple[str, str, str]:
    if not isinstance(strategy, str) or not isinstance(link, str):
        raise ContinuationInvalid("invalid identity type")
    strategy, link = strategy.strip(), link.strip()
    if not strategy or len(strategy) > 512 or not link or len(link) > 2048:
        raise ContinuationInvalid("invalid identity length")
    key = hashlib.sha256(f"{link}\0{strategy}".encode()).hexdigest()
    return strategy, link, key


def _now(value=None) -> float:
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ContinuationInvalid("invalid journal time")
    return float(value)


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ContinuationInvalid("journal directory is a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ContinuationInvalid("journal path is not a directory")
    path.chmod(0o700)


@contextmanager
def _lock(path: Path):
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.setdefault(str(path), threading.Lock())
    if not lock.acquire(blocking=False):
        raise ContinuationBusy("continuation thread lock busy")
    fd = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContinuationBusy("continuation process lock busy") from error
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lock.release()


def _safe_value(value, depth=0) -> None:
    if depth > 30:
        raise ContinuationInvalid("record nesting exceeds limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _safe_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_KEYS:
                raise ContinuationInvalid("unsupported or sensitive record key")
            _safe_value(item, depth + 1)
        return
    raise ContinuationInvalid("record must contain finite JSON values")


def _encoded(value) -> bytes:
    _safe_value(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ContinuationInvalid("record serialization failed") from error
    if len(raw) > MAX_RECORD_BYTES:
        raise ContinuationInvalid("record byte limit exceeded; input was not truncated")
    return raw


def _payload(value) -> None:
    if not isinstance(value, dict) or not set(value).issubset(_PAYLOAD_KEYS):
        raise ContinuationInvalid("invalid payload fields")
    for key in ("title", "summary", "prompt", "feed_url"):
        if not isinstance(value.get(key), str):
            raise ContinuationInvalid("missing or invalid payload string")
    if not value["title"].strip() or not value["prompt"].strip() or not value["feed_url"].strip():
        raise ContinuationInvalid("empty required payload string")
    if "few_shot_context" in value and not isinstance(value["few_shot_context"], str):
        raise ContinuationInvalid("invalid few-shot context")
    if "link" in value and not isinstance(value["link"], str):
        raise ContinuationInvalid("invalid payload link")
    for name in ("published_parsed", "updated_parsed"):
        if name not in value or value[name] is None:
            continue
        if not isinstance(value[name], list) or len(value[name]) != 9 or any(type(part) is not int for part in value[name]):
            raise ContinuationInvalid("invalid parsed publication time")
    _encoded(value)


def _validate(data, *, key=None) -> dict:
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != SCHEMA:
        raise ContinuationInvalid("unknown journal schema")
    strategy, link, actual_key = identity(data.get("strategy_version"), data.get("link"))
    if data.get("article_key") != actual_key or (key is not None and key != actual_key):
        raise ContinuationInvalid("journal identity mismatch")
    if strategy != data["strategy_version"] or link != data["link"]:
        raise ContinuationInvalid("noncanonical journal identity")
    if data.get("state") not in ACTIVE_STATES | TERMINAL_STATES:
        raise ContinuationInvalid("unknown journal state")
    if type(data.get("attempts")) is not int or not 0 <= data["attempts"] <= MAX_ATTEMPTS:
        raise ContinuationInvalid("invalid attempt counter")
    time_fields = ("created_at", "updated_at", "next_attempt_at")
    if any(name not in data or data[name] is None for name in time_fields):
        raise ContinuationInvalid("stored journal time is required")
    created, updated, due = (_now(data[name]) for name in time_fields)
    if updated < created or due < created:
        raise ContinuationInvalid("invalid journal time ordering")
    if data["state"] in ACTIVE_STATES:
        _payload(data.get("payload"))
        if "link" in data["payload"] and data["payload"]["link"] != link:
            raise ContinuationInvalid("payload link does not match journal identity")
    elif "payload" in data or "frozen_result" in data:
        raise ContinuationInvalid("terminal records must not retain article bodies")
    _encoded(data)
    return data


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContinuationInvalid("duplicate journal JSON key")
        value[key] = item
    return value


def _read(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ContinuationInvalid("journal record is not a regular file")
            raw = source.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ContinuationInvalid("stored record byte limit exceeded")
        data = json.loads(raw, object_pairs_hook=_unique_object)
        return _validate(data, key=path.stem)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ContinuationInvalid("invalid stored journal record") from error


def _write(path: Path, data: dict) -> None:
    raw = _encoded(_validate(data, key=path.stem))
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(name):
            os.unlink(name)


def _paths(directory: Path) -> list[Path]:
    if directory.is_symlink():
        raise ContinuationInvalid("journal directory is a symlink")
    if not directory.exists():
        return []
    result = []
    for path in directory.iterdir():
        if path.suffix != ".json":
            continue
        if len(path.stem) != 64 or any(char not in "0123456789abcdef" for char in path.stem):
            raise ContinuationInvalid("unknown journal filename retained")
        result.append(path)
        if len(result) > MAX_RECORDS:
            raise ContinuationInvalid("journal record capacity exceeded")
    return sorted(result)


class Record:
    """One identity-locked record; ``data`` is a defensive snapshot."""

    def __init__(self, path: Path, data: dict):
        self.path, self._data, self._open = path, data, True

    @property
    def data(self) -> dict:
        return copy.deepcopy(self._data)

    def save(self, data: dict) -> None:
        if not self._open:
            raise ContinuationInvalid("record lock has been released")
        updated = copy.deepcopy(data)
        for name in ("schema", "article_key", "strategy_version", "link", "created_at"):
            if updated.get(name) != self._data[name]:
                raise ContinuationInvalid("immutable journal identity changed")
        if type(updated.get("attempts")) is not int or updated["attempts"] < self._data["attempts"]:
            raise ContinuationInvalid("attempt budget cannot be reset")
        if self._data["state"] in TERMINAL_STATES:
            if updated != self._data:
                raise ContinuationInvalid("terminal journal record cannot be changed or revived")
            return
        if updated.get("state") in TERMINAL_STATES:
            updated.pop("payload", None)
            updated.pop("frozen_result", None)
        elif updated.get("payload") != self._data["payload"]:
            raise ContinuationInvalid("frozen input cannot be replaced")
        if _now(updated.get("updated_at")) < self._data["updated_at"]:
            raise ContinuationInvalid("journal update time cannot go backwards")
        _validate(updated, key=self.path.stem)
        _write(self.path, updated)
        self._data = updated


@contextmanager
def locked_record(strategy: str, link: str):
    """Yield ``None`` for absent work without creating a directory or lock file."""
    _strategy, _link, key = identity(strategy, link)
    directory = journal_directory()
    path = directory / f"{key}.json"
    if directory.is_symlink():
        raise ContinuationInvalid("journal directory is a symlink")
    if not path.exists() and not path.is_symlink():
        yield None
        return
    _private_directory(directory / ".locks")
    with _lock(directory / ".locks" / f"{key}.lock"):
        record = Record(path, _read(path))
        try:
            yield record
        finally:
            record._open = False


def enqueue(strategy: str, link: str, payload: dict, initial_attempts=1) -> str:
    """Freeze new input once; full/invalid never truncate or overwrite work."""
    try:
        strategy, link, key = identity(strategy, link)
        if type(initial_attempts) is not int or not 0 <= initial_attempts <= MAX_ATTEMPTS:
            raise ContinuationInvalid("invalid initial attempt counter")
        _payload(payload)
        now = _now()
        data = {
            "schema": SCHEMA, "article_key": key, "strategy_version": strategy, "link": link,
            "created_at": now, "updated_at": now,
            "next_attempt_at": now + 60 * max(1, initial_attempts),
            "attempts": initial_attempts, "state": "pending", "payload": copy.deepcopy(payload),
        }
        _validate(data, key=key)
        directory = journal_directory()
        _private_directory(directory)
        with _lock(directory / ".lock"):
            target = directory / f"{key}.json"
            if target.exists() or target.is_symlink():
                _read(target)
                return "duplicate"
            paths = _paths(directory)
            if len(paths) >= MAX_RECORDS:
                return "full"
            active = sum(_read(path)["state"] in ACTIVE_STATES for path in paths)
            if active >= MAX_ACTIVE_RECORDS:
                return "full"
            _private_directory(directory / ".locks")
            lock_path = directory / ".locks" / f"{key}.lock"
            if not lock_path.exists() and sum(1 for _ in (directory / ".locks").iterdir()) >= MAX_RECORDS:
                return "full"  # Failed initial writes must not leak unbounded lock files.
            with _lock(lock_path):
                _write(target, data)
        return "stored"
    except ContinuationInvalid:
        return "invalid"


def _terminalize(strategy: str, link: str, state: str, now: float) -> bool:
    with locked_record(strategy, link) as record:
        if record is None or record.data["state"] in TERMINAL_STATES:
            return False
        data = record.data
        data.update(state=state, updated_at=max(now, data["updated_at"]))
        record.save(data)
        return True


def pending(strategy, *, limit=20, now=None, terminal_counts=None) -> list[dict]:
    """Return at most 20 due, same-contract records; expiry never renews a budget."""
    if isinstance(strategy, dict):
        strategy = strategy.get("scoring_strategy_version")
    if not isinstance(strategy, str) or not strategy.strip():
        raise ContinuationInvalid("current strategy is required")
    if type(limit) is not int or not 0 <= limit <= 20:
        raise ContinuationInvalid("pending limit must be between 0 and 20")
    strategy, now = strategy.strip(), _now(now)
    due = []
    for path in _paths(journal_directory()):
        data = _read(path)
        if data["state"] in TERMINAL_STATES:
            continue
        terminal = "contract_changed" if data["strategy_version"] != strategy else (
            "expired" if now - data["created_at"] >= TTL_SECONDS else ""
        )
        if terminal:
            try:
                changed = _terminalize(data["strategy_version"], data["link"], terminal, now)
                if changed and terminal_counts is not None:
                    terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1
            except ContinuationBusy:
                pass
            continue
        if data["next_attempt_at"] <= now:
            due.append(data)
    return sorted(due, key=lambda data: (data["created_at"], data["article_key"]))[:limit]


def overview(strategy, *, now=None) -> dict:
    """Read bounded counts without creating files or mutating expired records.

    State counts describe stored states. ``due`` only counts same-contract,
    unexpired active records ready to run, including those beyond a 20-job batch.
    """
    if isinstance(strategy, dict):
        strategy = strategy.get("scoring_strategy_version")
    if not isinstance(strategy, str) or not strategy.strip():
        raise ContinuationInvalid("current strategy is required")
    strategy, now = strategy.strip(), _now(now)
    result = {"total": 0, "active": 0, "due": 0, "terminal": 0}
    result.update({state: 0 for state in sorted(TERMINAL_STATES)})
    for path in _paths(journal_directory()):
        data = _read(path)
        result["total"] += 1
        if data["state"] in TERMINAL_STATES:
            result["terminal"] += 1
            result[data["state"]] += 1
        else:
            result["active"] += 1
            if (data["strategy_version"] == strategy and now - data["created_at"] < TTL_SECONDS
                    and data["next_attempt_at"] <= now):
                result["due"] += 1
    return result
