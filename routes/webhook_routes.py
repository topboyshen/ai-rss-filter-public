"""
Miniflux Webhook 接收器 — 持久化异步评分

签名验证由 app.py @before_request 统一处理。
本模块负责：解析 payload → 原子写入 durable outbox → 202 Accepted →
固定 dispatcher 提交有界线程池 → 成功后删除 outbox 任务。
"""
import json
import base64
import hashlib
import fcntl
import logging
import math
import os
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, Response, g, request

from routes.shared import _cfg, DB
from core.processor import (
    SCORE_DEFERRED_REASON, SCORE_TERMINAL_UNSCORED_REASON, process_entry,
)
from core.prompts import resolve_prompt
from core.scorer import Scorer
from core.user_feedback import resolve_scoring_contract
from services.news_processing_trace import processing_origin
from services.webhook_receipts import ReceiptError, ReceiptStore, public_record
from services import webhook_receipts as receipt_storage
from services.admission_fence import (
    AdmissionClosed, admission, current_kind, guarded, reserve_lease,
)
from utils.helpers import clean_html, plain_text_from_markup

logger = logging.getLogger(__name__)
webhook_bp = Blueprint("webhook", __name__)

# 全局线程池和信号量：同时限制执行中任务和排队任务，防止突发请求无限占用内存。
_webhook_executor: ThreadPoolExecutor | None = None
_webhook_executor_lock = threading.Lock()
_refresh_executor: ThreadPoolExecutor | None = None
_refresh_executor_lock = threading.Lock()
_briefing_executor: ThreadPoolExecutor | None = None
_briefing_executor_lock = threading.Lock()


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_webhook_executor_max_workers = _bounded_env_int("WEBHOOK_WORKERS", 2, 1, 8)
_webhook_pending_max = _bounded_env_int("WEBHOOK_PENDING", 32, 0, 128)
_webhook_max_entries = _bounded_env_int("MAX_ENTRIES_PER_WEBHOOK", 50, 1, 100)
_webhook_task_deadline_seconds = _bounded_env_int(
    "WEBHOOK_TASK_DEADLINE_SECONDS", 300, 30, 1800
)
_webhook_slots = threading.BoundedSemaphore(
    _webhook_executor_max_workers + _webhook_pending_max
)

_MAX_TITLE_CHARS = 512
_MAX_URL_CHARS = 2048
_MAX_CONTENT_BYTES = 20 * 1024

_webhook_replay_ttl = _bounded_env_int("WEBHOOK_REPLAY_TTL_SECONDS", 600, 30, 86400)
_webhook_replay_max = _bounded_env_int("WEBHOOK_REPLAY_MAX_ENTRIES", 2048, 128, 8192)
_webhook_replay_lock = threading.Lock()
_webhook_replay_seen: OrderedDict[str, float] = OrderedDict()

# Durable outbox.  The default lives under the bind-mounted data directory so
# accepted webhooks survive both Gunicorn worker and container restarts.
_webhook_spool_max_tasks = _bounded_env_int("WEBHOOK_SPOOL_MAX_TASKS", 128, 1, 128)
_briefing_spool_max_tasks = _bounded_env_int("BRIEFING_SPOOL_MAX_TASKS", 1, 1, 4)
_refresh_busy_delay_seconds = _bounded_env_int("REFRESH_BUSY_DELAY_SECONDS", 5, 1, 60)
_dashboard_snapshot_retry_delay_seconds = 5
_webhook_max_attempts = _bounded_env_int("WEBHOOK_MAX_ATTEMPTS", 5, 1, 20)
_webhook_lease_seconds = _bounded_env_int("WEBHOOK_LEASE_SECONDS", 120, 30, 3600)
_webhook_dlq_max_tasks = _bounded_env_int("WEBHOOK_DLQ_MAX_TASKS", 256, 16, 4096)
_webhook_spool_dir = Path(
    os.environ.get(
        "WEBHOOK_SPOOL_DIR",
        str(Path(__file__).resolve().parents[1] / "data" / "webhook_spool"),
    )
)
_webhook_spool_lock = threading.Lock()
_SPOOL_LOCK_FILE = ".lock"
_SPOOL_RECEIPTS_FILE = ".completed-digests"
_SPOOL_INFLIGHT_DIR = "inflight"
_SPOOL_DLQ_DIR = "dead-letter"
_DASHBOARD_SNAPSHOT_INPUTS_CHANGED = "dashboard_snapshot_inputs_changed"
# An entry whose safe-neutral still lacks a backup verdict is retried later
# without consuming an attempt: 60/120/240/300 s, at most 4 times per task.
# The processor caps re-scores per link separately (TRANSPORT_RETRY_MAX).
_SCORE_TRANSPORT_RETRY = "score_transport_retry"
_WEBHOOK_PARTIAL = "webhook_terminal_unscored"
_DELIVERY_CONTRACT_CHANGED = "delivery_contract_changed"
_SCORE_TRANSPORT_DEFERRAL_MAX = 4
_SCORE_TRANSPORT_BASE_DELAY_SECONDS = 60.0
# SQLite writer contention that outlived the connection busy timeout.  It is
# deferred without consuming an attempt, but only ``_SQLITE_LOCK_DEFERRAL_MAX``
# times per task (5+10+20+40+80+160 s, about 5 minutes in total); after that
# the ordinary attempt counter and dead-letter path take over again.
_SQLITE_LOCKED = "sqlite_database_locked"
_SQLITE_LOCK_DEFERRAL_MAX = 6
_SQLITE_LOCK_BASE_DELAY_SECONDS = 5.0
_SQLITE_LOCK_MAX_DELAY_SECONDS = 300.0
_DASHBOARD_TASK_TYPES = frozenset(
    {"dashboard_snapshot_refresh", "dashboard_realtime_refresh"}
)

_webhook_dispatcher_lock = threading.Lock()
_webhook_dispatcher_thread: threading.Thread | None = None
_webhook_dispatcher_stop = threading.Event()
_webhook_dispatcher_wake = threading.Event()
_webhook_inflight_lock = threading.Lock()
_webhook_inflight: set[str] = set()
_webhook_claims: dict[str, tuple[Path, str]] = {}
_webhook_retry_state: dict[str, tuple[int, float]] = {}
# Spool lock guards this bounded, process-local archive backoff (not model budget).
_archive_retry_at: OrderedDict[str, float] = OrderedDict()
_webhook_instance_id = f"{socket.gethostname()}-{uuid.uuid4().hex}"
_task_execution_context = threading.local()


def _get_executor() -> ThreadPoolExecutor:
    global _webhook_executor
    if _webhook_executor is None:
        with _webhook_executor_lock:
            if _webhook_executor is None:
                _webhook_executor = ThreadPoolExecutor(
                    max_workers=_webhook_executor_max_workers,
                    thread_name_prefix="webhook-score",
                )
    return _webhook_executor


def _get_briefing_executor() -> ThreadPoolExecutor:
    """Single analytics lane so slow Briefing/Band LLM calls cannot take scoring threads."""
    global _briefing_executor
    if _briefing_executor is None:
        with _briefing_executor_lock:
            if _briefing_executor is None:
                _briefing_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="analytics-command",
                )
    return _briefing_executor


def _get_refresh_executor() -> ThreadPoolExecutor:
    """One refresh lane so a multi-minute feed sweep cannot consume scoring capacity."""
    global _refresh_executor
    if _refresh_executor is None:
        with _refresh_executor_lock:
            if _refresh_executor is None:
                _refresh_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="feed-refresh",
                )
    return _refresh_executor


def _clip_text(value: Any, max_chars: int) -> str:
    return str(value or "").strip()[:max_chars]


def _clip_utf8(value: Any, max_bytes: int) -> str:
    raw = str(value or "").strip().encode("utf-8")
    if len(raw) <= max_bytes:
        return raw.decode("utf-8")
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _reserve_webhook_digest(body: bytes) -> tuple[str, bool]:
    """Reserve an authenticated payload digest to suppress exact replays."""
    digest = hashlib.sha256(body).hexdigest()
    now = time.time()
    with _webhook_replay_lock:
        while _webhook_replay_seen:
            _key, seen_at = next(iter(_webhook_replay_seen.items()))
            if now - seen_at < _webhook_replay_ttl:
                break
            _webhook_replay_seen.popitem(last=False)
        if digest in _webhook_replay_seen:
            _webhook_replay_seen.move_to_end(digest)
            return digest, False
        _webhook_replay_seen[digest] = now
        while len(_webhook_replay_seen) > _webhook_replay_max:
            _webhook_replay_seen.popitem(last=False)
    return digest, True


def _forget_webhook_digest(digest: str) -> None:
    with _webhook_replay_lock:
        _webhook_replay_seen.pop(digest, None)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _task_digest(task: dict) -> str:
    """Stable idempotency key for the normalized task, not raw JSON layout."""
    return hashlib.sha256(_canonical_json(task)).hexdigest()


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _new_spool_envelope(
    task: dict, digest: str, *, created_at: float | None = None
) -> dict:
    created_at = time.time() if created_at is None else float(created_at)
    return {
        "version": 1,
        "digest": digest,
        "created_at": created_at,
        # Queue position: refreshed on every requeue; created_at stays for audit.
        "enqueued_at": created_at,
        "attempts": 0,
        "transient_deferrals": 0,
        "next_attempt_at": 0.0,
        "owner": "",
        "lease_until": 0.0,
        "progress": {"passed": 0, "filtered": 0},
        "task": task,
    }


def _ensure_spool_dir() -> Path:
    directory = Path(_webhook_spool_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / _SPOOL_INFLIGHT_DIR).mkdir(mode=0o700, exist_ok=True)
    (directory / _SPOOL_DLQ_DIR).mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    _fsync_directory(directory)
    _fsync_directory(directory.parent)
    return directory


@contextmanager
def _locked_spool():
    """Serialize capacity checks and atomic mutations across worker processes."""
    with admission("spool_mutation", kind=current_kind(default="drain")):
        with _locked_spool_admitted() as directory:
            yield directory


@contextmanager
def _locked_spool_admitted():
    with _webhook_spool_lock:
        directory = _ensure_spool_dir()
        lock_fd = os.open(
            str(directory / _SPOOL_LOCK_FILE),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


@contextmanager
def locked_transfer_spool():
    """The SAME spool locks, with bounded waits for the transfer controller."""
    from services import admission_fence as gate
    from services.receiver_drain_storage import create_lock
    with admission("transfer_spool", kind="drain"):
        gate._require(_webhook_spool_lock.acquire(timeout=1), "transfer spool thread lock busy")
        try:
            directory = Path(_webhook_spool_dir)
            gate._directory(directory)
            create_lock(directory / _SPOOL_LOCK_FILE)
            with gate._locked(directory / _SPOOL_LOCK_FILE, True, 1):
                yield directory
        finally:
            _webhook_spool_lock.release()


def _fsync_directory(directory: Path, *, strict: bool = True) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        if strict:
            raise
        logger.warning("[Webhook] could not fsync spool directory")


def _atomic_write(directory: Path, target: Path, payload: bytes) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, target)
        _fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _pending_paths_unlocked(directory: Path) -> list[Path]:
    paths = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json" and _is_digest(path.stem)
    ]
    # Realtime scoring/refresh stays ahead of optional analytical commands.
    return sorted(paths, key=_pending_sort_key)


def _pending_sort_key(path: Path) -> tuple[int, int, float, str]:
    """Priority, then FIFO by (re)enqueue time, then by envelope enqueue stamp.

    File mtimes can be coarse (about 4 ms where measured), so tasks written back
    to back may share one.  The envelope's ``enqueued_at`` (``time.time()`` at
    first enqueue and at every requeue; older envelopes fall back to
    ``created_at``) keeps FIFO order within such a tie, so a requeued task goes
    behind already-queued work.  The digest name is only a last resort.
    """
    task_type, enqueued_at = _spool_task_meta(path)
    mtime_ns = path.stat().st_mtime_ns
    if enqueued_at is None:
        enqueued_at = mtime_ns / 1e9
    return (_task_type_priority(task_type), mtime_ns, enqueued_at, path.name)


def _inflight_paths_unlocked(directory: Path) -> list[Path]:
    inflight = directory / _SPOOL_INFLIGHT_DIR
    return sorted(
        path
        for path in inflight.iterdir()
        if path.is_file() and path.suffix == ".json" and _is_digest(path.stem)
    )


def _active_task_count_unlocked(directory: Path) -> int:
    return len(_pending_paths_unlocked(directory)) + len(
        _inflight_paths_unlocked(directory)
    )


def _spool_task_meta(path: Path) -> tuple[str, float | None]:
    """Return (task_type, enqueue stamp or None) from one read.

    The stamp is ``enqueued_at``, falling back to ``created_at`` for envelopes
    written before requeues refreshed it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        task = raw.get("task") if isinstance(raw, dict) else None
        enqueued_at = None
        if isinstance(task, dict):
            for key in ("enqueued_at", "created_at"):
                value = raw.get(key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                ):
                    enqueued_at = float(value)
                    break
        else:
            task = raw if isinstance(raw, dict) else {}
        return str(task.get("task_type") or "score"), enqueued_at
    except Exception:
        return "score", None


def _spool_task_type(path: Path) -> str:
    return _spool_task_meta(path)[0]


def _spool_task_priority(path: Path) -> int:
    return _task_type_priority(_spool_task_type(path))


def _task_type_priority(task_type: str) -> int:
    if task_type == "dashboard_realtime_refresh":
        # A continuous stream of newer score tasks must not starve publication
        # of already-committed news.  FIFO mtime shares the primary priority.
        return 0
    if task_type == "refresh":
        return 1
    if task_type == "dashboard_snapshot_refresh":
        return 2
    if task_type == "briefing_refresh":
        return 3
    return 0


def _active_task_type_count_unlocked(directory: Path, task_type: str) -> int:
    return sum(
        1
        for path in (_pending_paths_unlocked(directory) + _inflight_paths_unlocked(directory))
        if _spool_task_type(path) == task_type
    )


def _move_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_parent = source.parent
    os.replace(source, target)
    _fsync_directory(source_parent)
    if target.parent != source_parent:
        _fsync_directory(target.parent)


def _load_receipts_unlocked(directory: Path) -> OrderedDict[str, float]:
    path = directory / _SPOOL_RECEIPTS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OrderedDict()
    except Exception:
        logger.exception("[Webhook] completed-digest ledger is unreadable")
        raise

    receipts: OrderedDict[str, float] = OrderedDict()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, list) or len(item) != 2:
                continue
            digest, completed_at = item
            if _is_digest(str(digest)):
                try:
                    receipts[str(digest)] = float(completed_at)
                except (TypeError, ValueError):
                    continue
    while len(receipts) > _webhook_replay_max:
        receipts.popitem(last=False)
    _confirm_spool_file(path)
    return receipts


def _write_receipts_unlocked(
    directory: Path, receipts: OrderedDict[str, float]
) -> None:
    while len(receipts) > _webhook_replay_max:
        receipts.popitem(last=False)
    _atomic_write(
        directory,
        directory / _SPOOL_RECEIPTS_FILE,
        _canonical_json(list(receipts.items())),
    )


@guarded("persist_task", kind="inherit")
def _persist_webhook_task(task: dict) -> str:
    """Atomically persist a normalized task; return stored/duplicate/full."""
    digest = _task_digest(task)
    with _locked_spool() as directory:
        receipts = _load_receipts_unlocked(directory)
        if digest in receipts:
            return "duplicate"

        target = directory / f"{digest}.json"
        if target.exists():
            _confirm_spool_file(target)
            return "duplicate"
        inflight = directory / _SPOOL_INFLIGHT_DIR / f"{digest}.json"
        if inflight.exists():
            _confirm_spool_file(inflight)
            return "duplicate"
        archives = list((directory / _SPOOL_DLQ_DIR).glob(f"{digest}.*.json"))
        if archives:
            for archived in archives:
                _confirm_spool_file(archived)
            return "duplicate"
        if (
            task.get("task_type") == "briefing_refresh"
            and _active_task_type_count_unlocked(directory, "briefing_refresh")
            >= _briefing_spool_max_tasks
        ):
            return "full"
        if (
            task.get("task_type") == "refresh"
            and _active_task_type_count_unlocked(directory, "refresh") >= 1
        ):
            # Refresh requests are level-triggered: one pending/running command
            # already represents the desired state, so newer requests coalesce.
            return "duplicate"
        if (
            task.get("task_type") in {"dashboard_snapshot_refresh", "dashboard_realtime_refresh"}
            and (
                sum(
                    1
                    for path in _pending_paths_unlocked(directory)
                    if _spool_task_type(path) == task.get("task_type")
                )
                >= 1
                or sum(
                    1
                    for path in _inflight_paths_unlocked(directory)
                    if _spool_task_type(path) == task.get("task_type")
                )
                >= 2
            )
        ):
            # Keep at most one pending successor.  An in-flight publisher may
            # already have rendered its old contract when feedback invalidates
            # it, so treating inflight as a duplicate could lose the only
            # post-feedback rebuild.  The single maintenance executor still
            # guarantees that the two tasks never run concurrently.  Count a
            # second inflight envelope as the one queued executor successor so
            # the dispatcher cannot turn repeated triggers into an unbounded
            # chain of inflight-but-not-yet-running work.
            return "duplicate"
        if _active_task_count_unlocked(directory) >= _webhook_spool_max_tasks:
            return "full"

        envelope = _new_spool_envelope(task, digest)
        _atomic_write(directory, target, _canonical_json(envelope))

    _webhook_dispatcher_wake.set()
    return "stored"


def _submit_webhook_task(task: dict) -> str:
    """Durably enqueue before acknowledging the webhook request."""
    try:
        return _persist_webhook_task(task)
    except Exception:
        logger.exception("[Webhook] failed to persist task in durable spool")
        return "full"


def _delivery_strategy() -> str:
    return str(resolve_scoring_contract(_cfg()).get("scoring_strategy_version") or "unknown")


def _confirm_spool_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())
    _fsync_directory(path.parent)


def _partial_queue_record(directory: Path, path: Path, delivery_id: str) -> dict | None:
    """Read an owned bounded context, never the lenient legacy queue loader."""
    receipt_storage._private_directory(path.parent)
    envelope = receipt_storage._decode(receipt_storage._read(path))
    if not isinstance(envelope, dict):
        raise ReceiptError("invalid partial receipt queue context")
    context = envelope.get("receipt_context")
    if context is None:
        return None  # A non-webhook command cannot prove a webhook identity.
    if not isinstance(context, dict) or not isinstance(context.get("raw_body_b64"), str):
        raise ReceiptError("invalid partial receipt queue context")
    raw = base64.b64decode(context["raw_body_b64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != delivery_id:
        return None
    task, digest = envelope.get("task"), envelope.get("digest")
    if (type(envelope.get("version")) is not int or envelope["version"] != 1
            or not isinstance(task, dict) or not _is_digest(str(digest))
            or _task_digest(task) != digest
            or not (path.stem == digest or path.name.startswith(digest + "."))):
        raise ReceiptError("partial receipt queue identity mismatch")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ReceiptError("partial receipt original body mismatch")
    feed = payload.get("feed") or {}
    if not isinstance(feed, dict):
        feed = {}
    expected = {"feed_url": DB.canonical_feed_url(_cfg(), _clip_text(feed.get("feed_url", "unknown"), _MAX_URL_CHARS)),
                "entries": _normalize_webhook_entries(payload["entries"], _cfg())}
    if task != expected:
        raise ReceiptError("partial receipt body and normalized task disagree")
    record = receipt_storage.accepted_record(
        raw, task, strategy=context.get("strategy"), revision=context.get("revision"),
        accepted_at=context.get("accepted_at"),
    )
    # A context visible in cache is not a durable proof; reconfirm it strictly.
    ReceiptStore(directory)._barrier(path)
    return record


def _resolve_partial_receipt(directory: Path, delivery_id: str, hint: dict | None) -> dict | None:
    if hint is not None:
        digest = hint["task_digest"]
        paths = [directory / (digest + ".json"), directory / _SPOOL_INFLIGHT_DIR / (digest + ".json")]
        paths.extend((directory / _SPOOL_DLQ_DIR).glob(digest + ".*.json"))
        paths = [path for path in paths if path.exists() or path.is_symlink()]
    else:
        # An interrupted header supplies no lookup key. Only this exceptional
        # case scans the already bounded live/archive queue, never all receipts.
        paths = []
        for parent in (directory, directory / _SPOOL_INFLIGHT_DIR, directory / _SPOOL_DLQ_DIR):
            receipt_storage._private_directory(parent)
            for path in parent.glob("*.json"):
                paths.append(path)
                if len(paths) > _webhook_spool_max_tasks + _webhook_dlq_max_tasks:
                    raise ReceiptError("partial context lookup exceeds bounded queue")
    original = None
    for path in paths:
        candidate = _partial_queue_record(directory, path, delivery_id)
        if candidate is not None:
            if original is not None and original != candidate:
                raise ReceiptError("ambiguous partial receipt queue context")
            original = candidate
    return original


def _receipt_store(directory: Path) -> ReceiptStore:
    store = ReceiptStore(directory)
    store.partial_context = lambda delivery_id, hint: _resolve_partial_receipt(directory, delivery_id, hint)
    return store


def _delivery_receipt(directory: Path, envelope: dict) -> dict | None:
    context = envelope.get("receipt_context")
    if context is None:
        return None
    required = {"raw_body_b64", "strategy", "revision", "accepted_at"}
    if not isinstance(context, dict) or not required.issubset(context):
        raise ValueError("invalid delivery receipt context")
    if not isinstance(context["raw_body_b64"], str):
        raise ValueError("invalid delivery receipt body")
    body = base64.b64decode(context["raw_body_b64"], validate=True)
    record, _ = _receipt_store(directory).accept(
        body, envelope["task"], strategy=context["strategy"],
        revision=context["revision"], accepted_at=context["accepted_at"],
    )
    if (record["task_digest"] != envelope["digest"]
            or record["accepted_strategy"] != context["strategy"]
            or record["accepted_revision"] != context["revision"]
            or record["accepted_at"] != context["accepted_at"]):
        raise ValueError("delivery receipt task or acceptance identity mismatch")
    return record


def _public_delivery(directory: Path, record: dict) -> dict:
    primary = _receipt_store(directory).find_task(record["task_digest"])
    if primary is None:
        raise ValueError("delivery primary receipt missing")
    result = public_record(record)
    result["state"] = primary["status"]
    result["completion"] = primary.get("completion")
    return result


@guarded("persist_delivery", kind="inherit")
def _persist_webhook_delivery(task: dict, body: bytes) -> tuple[str, dict]:
    """Acknowledge only a synced queue AND an independently retained raw receipt."""
    with _locked_spool() as directory:
        result = persist_delivery_locked(directory, task, body)
    _webhook_dispatcher_wake.set()
    return result


@guarded("persist_delivery_locked", kind="inherit")
def persist_delivery_locked(directory: Path, task: dict, body: bytes) -> tuple[str, dict]:
    """Shared durable path; caller holds the real admission AND spool locks."""
    digest = _task_digest(task)
    store = _receipt_store(directory)
    existing = store.find_task(digest)
    if existing:
        receipt, _ = store.accept(body, task, strategy=existing["accepted_strategy"], revision=existing["accepted_revision"])
        return "duplicate", _public_delivery(directory, receipt)
    paths = [directory / f"{digest}.json", directory / _SPOOL_INFLIGHT_DIR / f"{digest}.json"]
    paths.extend((directory / _SPOOL_DLQ_DIR).glob(f"{digest}.*.json"))
    present = [path for path in paths if path.exists()]
    if len(present) > 1:
        raise ValueError("delivery has ambiguous queue state")
    if present:
        envelope = _load_spooled_task(present[0])
        if envelope is None or "receipt_context" not in envelope:
            raise ValueError("legacy task has no verifiable delivery receipt")
        _confirm_spool_file(present[0])
        primary = _delivery_receipt(directory, envelope)
        receipt, _ = store.accept(body, task, strategy=primary["accepted_strategy"], revision=primary["accepted_revision"])
        return "duplicate", _public_delivery(directory, receipt)
    if digest in _load_receipts_unlocked(directory):
        raise ValueError("legacy completion cannot prove durable delivery")
    if _active_task_count_unlocked(directory) >= _webhook_spool_max_tasks:
        raise OSError("delivery queue is full")
    envelope = _new_spool_envelope(task, digest)
    envelope["receipt_context"] = {
        "raw_body_b64": base64.b64encode(body).decode("ascii"),
        "strategy": _delivery_strategy(), "revision": os.environ.get("AI_FILTER_REVISION", "unknown"),
        "accepted_at": envelope["created_at"],
    }
    _atomic_write(directory, paths[0], _canonical_json(envelope))
    receipt = _delivery_receipt(directory, envelope)
    return "stored", _public_delivery(directory, receipt)


def _submit_webhook_delivery(task: dict, body: bytes) -> tuple[str, dict | None]:
    try:
        return _persist_webhook_delivery(task, body)
    except Exception:
        logger.exception("[Webhook] durable delivery not confirmed; sender must retain message")
        return "full", None


def _delivery_completed_unlocked(directory: Path, path: Path, envelope: dict) -> bool:
    if "receipt_context" not in envelope:
        return False
    _confirm_spool_file(path)
    record = _delivery_receipt(directory, envelope)
    if record["status"] == "terminal":
        _try_dead_letter_unlocked(directory, path, envelope, envelope.get("last_error", "delivery_terminal"))
        return True  # Archived or held; a terminal receipt can never return to scoring.
    if record["status"] != "completed":
        return False
    if envelope.get("terminal_unscored") or "archive_pending" in envelope:
        raise ValueError("completion conflicts with terminal evidence")
    path.unlink()
    _fsync_directory(path.parent)
    return True


def _record_delivery_outcome(directory: Path, envelope: dict, status: str) -> None:
    record = _delivery_receipt(directory, envelope)
    if record is None:
        return
    if record["status"] == status:
        return  # Reconfirmed immutable outcome; do not replace its original finish time.
    _receipt_store(directory).update_status(
        record["delivery_id"], status=status, completion={
            "finished_at": time.time(), "task_digest": envelope["digest"],
            "next_entry_index": int(envelope.get("next_entry_index") or 0),
            "entry_count": len(envelope["task"].get("entries", [])),
            "progress": envelope.get("progress", {}),
            "terminal_count": len(envelope.get("terminal_unscored") or []),
            "attempts": int(envelope.get("attempts") or 0),
        },
    )


@guarded("background_task", kind="inherit")
def _submit_background_task(task: dict) -> str:
    """Stable API for non-webhook commands sharing the durable bounded spool."""
    return _submit_webhook_task(task)


def _load_spooled_task(path: Path) -> dict | None:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        digest = envelope.get("digest")
        task = envelope.get("task")
        if (
            not isinstance(envelope, dict)
            or not isinstance(task, dict)
            or (
                not path.name.startswith(f"{digest}.")
                and digest != path.stem
            )
            or not _is_digest(str(digest))
            or _task_digest(task) != digest
        ):
            raise ValueError("invalid spool envelope")
        return envelope
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("[Webhook] invalid durable task %s; retaining it", path.name)
        return None


def _load_or_migrate_pending_task_unlocked(
    directory: Path, path: Path
) -> dict | None:
    """Load a pending envelope, atomically upgrading pre-envelope task files.

    The first durable spool release wrote the normalized webhook task directly
    to ``<task-digest>.json``.  Those files were already acknowledged with 202,
    so they must remain executable across this state-machine upgrade.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("[Webhook] invalid pending task %s; retaining it", path.name)
        return None

    if isinstance(value, dict):
        digest = value.get("digest")
        task = value.get("task")
        if (
            isinstance(task, dict)
            and digest == path.stem
            and _is_digest(str(digest))
            and _task_digest(task) == digest
        ):
            return value

    # Only the known legacy webhook shape is eligible.  A malformed envelope
    # must not be reinterpreted as an executable task merely because its bytes
    # happen to match the file name.
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("feed_url"), str)
        or not isinstance(value.get("entries"), list)
        or not _is_digest(path.stem)
        or _task_digest(value) != path.stem
    ):
        logger.error("[Webhook] invalid pending task %s; retaining it", path.name)
        return None

    try:
        created_at = path.stat().st_mtime
    except OSError:
        created_at = time.time()
    envelope = _new_spool_envelope(value, path.stem, created_at=created_at)
    _atomic_write(directory, path, _canonical_json(envelope))
    logger.warning("[Webhook] upgraded legacy pending task %s", path.name)
    return envelope


def _claim_owner() -> str:
    return f"{_webhook_instance_id}:{os.getpid()}"


def _trim_dlq_unlocked(directory: Path, *, preserve: Path | None = None) -> None:
    paths = sorted(
        (directory / _SPOOL_DLQ_DIR).glob("*.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    excess = max(0, len(paths) - _webhook_dlq_max_tasks)
    for path in paths:
        if not excess:
            break
        envelope = _load_spooled_task(path)
        if path == preserve or (envelope and envelope.get("terminal_unscored")):
            # These rows are the durable negative receipt for skipped entries.
            # Removing them would silently forget the batch's partial outcome.
            continue
        try:
            path.unlink()
            excess -= 1
        except OSError:
            logger.warning("[Webhook] could not trim dead letter %s", path.name)


def _terminal_archive_paths(directory: Path, envelope: dict) -> tuple[Path, Path]:
    """Bind a frozen archive intent to this spool, never to an arbitrary path."""
    intent = envelope.get("archive_pending")
    if (
        not isinstance(intent, dict) or not isinstance(intent.get("source"), str)
        or intent["source"] not in {"pending", "inflight"}
    ):
        raise ValueError("invalid terminal archive intent")
    if type(envelope.get("next_entry_index")) is not int:
        raise ValueError("invalid terminal archive cursor")
    if not _terminal_outcomes(envelope):
        raise ValueError("terminal archive has no negative receipt")
    frozen = {key: value for key, value in envelope.items() if key != "archive_pending"}
    if hashlib.sha256(_canonical_json(frozen)).hexdigest() != intent.get("sha256"):
        raise ValueError("terminal archive intent does not match its frozen body")
    digest, attempts = str(envelope["digest"]), envelope.get("attempts")
    if not _is_digest(digest) or type(attempts) is not int or attempts < 0:
        raise ValueError("invalid terminal archive identity")
    parent = directory if intent["source"] == "pending" else directory / _SPOOL_INFLIGHT_DIR
    return parent / f"{digest}.json", directory / _SPOOL_DLQ_DIR / f"{digest}.{attempts}.terminal-pending.json"


def _archive_terminal_unlocked(directory: Path, path: Path, envelope: dict, error: str) -> None:
    """Durably copy the negative receipt before deleting its active checkpoint."""
    if "archive_pending" not in envelope:
        if path.parent not in {directory, directory / _SPOOL_INFLIGHT_DIR}:
            raise ValueError("terminal archive intent must originate in the active spool")
        envelope = {
            **envelope, "owner": "", "lease_until": 0.0,
            "dead_at": time.time(), "last_error": str(error or "unknown")[:1000],
        }
        envelope["archive_pending"] = {
            "source": "pending" if path.parent == directory else "inflight",
            "sha256": hashlib.sha256(_canonical_json(envelope)).hexdigest(),
        }
        _atomic_write(directory, path, _canonical_json(envelope))
        _fsync_directory(path.parent, strict=True)
    source, target = _terminal_archive_paths(directory, envelope)
    completed = target.with_name(target.name.replace(".terminal-pending.json", ".terminal.json"))
    if path not in {source, target, completed}:
        raise ValueError("terminal archive path does not match its intent")
    if source.exists():
        if _canonical_json(_load_spooled_task(source)) != _canonical_json(envelope):
            raise ValueError("terminal archive active checkpoint changed")
        _fsync_directory(source.parent, strict=True)
    if completed.exists():
        if target.exists():
            raise ValueError("terminal archive has conflicting completion paths")
        target = completed
    if target.exists():
        archived = _load_spooled_task(target)
        if _canonical_json(archived) != _canonical_json(envelope):
            raise ValueError("terminal archive target conflicts with frozen evidence")
    else:
        if len(list((directory / _SPOOL_DLQ_DIR).glob("*.json"))) >= _webhook_dlq_max_tasks:
            raise OSError("outcome archive is full; retaining active checkpoint")
        _atomic_write(directory, target, _canonical_json(envelope))
    # Reconfirm an existing target too: a crash may have followed its rename
    # before directory sync. The active file remains until both are durable.
    with target.open("rb") as archived_file:
        os.fsync(archived_file.fileno())
    _fsync_directory(target.parent, strict=True)
    _record_delivery_outcome(directory, envelope, "terminal")
    try:
        source.unlink()
    except FileNotFoundError:
        pass
    _fsync_directory(source.parent, strict=True)
    if target != completed:
        os.replace(target, completed)
    _fsync_directory(target.parent, strict=True)
    _trim_dlq_unlocked(directory, preserve=completed)
    logger.error("[Webhook] durably archived partial task %s", envelope["digest"])


def _recover_terminal_archives_unlocked(directory: Path) -> None:
    """Finish a copy/delete interrupted after the active pathname disappeared."""
    for path in (directory / _SPOOL_DLQ_DIR).glob("*.terminal-pending.json"):
        envelope = _load_spooled_task(path)
        if envelope and "archive_pending" in envelope:
            _try_dead_letter_unlocked(directory, path, envelope, envelope.get("last_error", _WEBHOOK_PARTIAL))


def _dead_letter_unlocked(
    directory: Path,
    path: Path,
    envelope: dict | None,
    error: str,
) -> None:
    digest = str((envelope or {}).get("digest") or path.stem.split(".", 1)[0])
    terminal = bool((envelope or {}).get("terminal_unscored"))
    if terminal or "archive_pending" in (envelope or {}):
        _archive_terminal_unlocked(directory, path, envelope, error)
        return
    attempts = int((envelope or {}).get("attempts") or 0)
    archives = list((directory / _SPOOL_DLQ_DIR).glob("*.json"))
    if len(archives) >= _webhook_dlq_max_tasks:
        ordinary_archive = any(
            not (_load_spooled_task(item) or {}).get("terminal_unscored") for item in archives
        )
        if not ordinary_archive:
            raise OSError("outcome archive is full; retaining active checkpoint")
    if envelope is not None:
        envelope = {
            **envelope,
            "owner": "",
            "lease_until": 0.0,
            "dead_at": time.time(),
            "last_error": str(error or "unknown")[:1000],
        }
        _atomic_write(directory, path, _canonical_json(envelope))
        _record_delivery_outcome(directory, envelope, "terminal")
    target = (
        directory
        / _SPOOL_DLQ_DIR
        / f"{digest}.{attempts}.{time.time_ns()}.json"
    )
    _move_atomic(path, target)
    _trim_dlq_unlocked(directory, preserve=target)
    logger.error(
        "[Webhook] moved task %s to dead-letter after %d attempts: %s",
        digest,
        attempts,
        str(error)[:200],
    )


def _try_dead_letter_unlocked(directory, path, envelope, error) -> bool:
    """An unarchivable item must not block claiming unrelated pending work."""
    key, now = str(path), time.monotonic()
    if (envelope or {}).get("archive_pending"):
        try:
            key = str(_terminal_archive_paths(directory, envelope)[0])
        except (ValueError, TypeError, OverflowError):
            pass
    if _archive_retry_at.get(key, 0) > now:
        return False
    try:
        _dead_letter_unlocked(directory, path, envelope, error)
        _archive_retry_at.pop(key, None)
        return True
    except (OSError, ValueError, TypeError, OverflowError, ReceiptError):
        _archive_retry_at[key] = now + 60.0
        _archive_retry_at.move_to_end(key)
        while len(_archive_retry_at) > 128:
            _archive_retry_at.popitem(last=False)
        logger.warning("[Webhook] retaining unarchived task %s; archive retry in 60s", path.name)
        return False


def _recover_expired_claims_unlocked(
    directory: Path, now: float | None = None
) -> int:
    """Return expired leases to pending, or quarantine exhausted/corrupt claims."""
    now = time.time() if now is None else float(now)
    recovered = 0
    for path in _inflight_paths_unlocked(directory):
        envelope = _load_spooled_task(path)
        if envelope is None:
            _try_dead_letter_unlocked(directory, path, None, "invalid_claim_envelope")
            continue
        try:
            if _delivery_completed_unlocked(directory, path, envelope):
                continue
        except (OSError, ValueError, ReceiptError):
            logger.warning("[Webhook] retaining unconfirmed delivery %s", path.name)
            continue
        if "archive_pending" in envelope:
            _try_dead_letter_unlocked(directory, path, envelope, envelope.get("last_error", _WEBHOOK_PARTIAL))
            continue
        if float(envelope.get("lease_until") or 0.0) > now:
            continue
        attempts = int(envelope.get("attempts") or 0)
        if attempts >= _webhook_max_attempts:
            _try_dead_letter_unlocked(directory, path, envelope, "lease_expired")
            continue
        envelope.update(
            owner="",
            lease_until=0.0,
            claimed_at=0.0,
            next_attempt_at=now,
            last_error="lease_expired",
            enqueued_at=time.time(),
        )
        _atomic_write(directory, path, _canonical_json(envelope))
        _move_atomic(path, directory / f"{envelope['digest']}.json")
        recovered += 1
    return recovered


def _claim_next_spooled_task(now: float | None = None) -> tuple[Path, dict] | None:
    """Atomically claim one eligible task across all processes sharing the spool."""
    now = time.time() if now is None else float(now)
    owner = _claim_owner()
    with _locked_spool() as directory:
        _recover_terminal_archives_unlocked(directory)
        _recover_expired_claims_unlocked(directory, now)
        receipts = None
        for path in _pending_paths_unlocked(directory):
            digest = path.stem
            envelope = _load_or_migrate_pending_task_unlocked(directory, path)
            if envelope and "archive_pending" in envelope:
                _try_dead_letter_unlocked(directory, path, envelope, envelope.get("last_error", _WEBHOOK_PARTIAL))
                continue
            if envelope and "receipt_context" in envelope:
                try:
                    if _delivery_completed_unlocked(directory, path, envelope):
                        continue
                except (OSError, ValueError, ReceiptError):
                    logger.warning("[Webhook] retaining unconfirmed delivery %s", path.name)
                    continue
                if envelope["receipt_context"]["strategy"] != _delivery_strategy():
                    logger.warning("[Webhook] retaining delivery with different scoring contract: %s", digest)
                    continue
            elif receipts is None:
                receipts = _load_receipts_unlocked(directory)
            if "receipt_context" not in (envelope or {}) and digest in (receipts or {}):
                try:
                    path.unlink()
                    _fsync_directory(directory)
                except FileNotFoundError:
                    pass
                continue
            if envelope is None:
                _try_dead_letter_unlocked(directory, path, None, "invalid_pending_envelope")
                continue
            if float(envelope.get("next_attempt_at") or 0.0) > now:
                continue
            attempts = int(envelope.get("attempts") or 0) + 1
            if attempts > _webhook_max_attempts:
                _try_dead_letter_unlocked(directory, path, envelope, "attempts_exhausted")
                continue
            envelope.update(
                attempts=attempts,
                owner=owner,
                claimed_at=now,
                lease_until=now + _webhook_lease_seconds,
            )
            _atomic_write(directory, path, _canonical_json(envelope))
            claimed = directory / _SPOOL_INFLIGHT_DIR / path.name
            _move_atomic(path, claimed)
            return claimed, envelope
    return None


def _complete_spooled_task(
    path: Path, digest: str, owner: str | None = None
) -> bool:
    """Record completion durably, then and only then remove the pending file."""
    try:
        with _locked_spool() as directory:
            current = _load_spooled_task(path)
            if owner and (
                current is None or current.get("owner") != owner
            ):
                logger.warning("[Webhook] stale owner cannot complete %s", digest)
                return False
            if current and current.get("terminal_unscored"):
                logger.error("[Webhook] partial task cannot receive a success receipt: %s", digest)
                return False
            if current and "receipt_context" in current:
                if "archive_pending" in current or current.get("next_entry_index") != len(current["task"]["entries"]):
                    return False
                _record_delivery_outcome(directory, current, "completed")
            else:
                receipts = _load_receipts_unlocked(directory)
                receipts.pop(digest, None)
                receipts[digest] = time.time()
                _write_receipts_unlocked(directory, receipts)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            _fsync_directory(path.parent)
        return True
    except Exception:
        logger.exception("[Webhook] could not commit completion for %s", digest)
        return False


def _retry_or_dead_letter(
    path: Path,
    digest: str,
    owner: str | None,
    error: str,
) -> str:
    try:
        with _locked_spool() as directory:
            if not path.exists():
                return "stale"
            envelope = _load_spooled_task(path)
            if envelope is None:
                _dead_letter_unlocked(directory, path, None, "invalid_retry_envelope")
                return "dead"
            if owner and envelope.get("owner") != owner:
                return "stale"
            attempts = int(envelope.get("attempts") or 0)
            if attempts >= _webhook_max_attempts:
                _dead_letter_unlocked(directory, path, envelope, error)
                return "dead"
            delay = min(60.0, float(2 ** min(attempts, 6)))
            envelope.pop("pending_ack", None)
            envelope.update(
                owner="",
                lease_until=0.0,
                claimed_at=0.0,
                next_attempt_at=time.time() + delay,
                last_error=str(error or "task_failed")[:1000],
                enqueued_at=time.time(),
            )
            _atomic_write(directory, path, _canonical_json(envelope))
            pending = directory / f"{digest}.json"
            if path != pending:
                _move_atomic(path, pending)
            return "retry"
    except Exception:
        logger.exception("[Webhook] failed to persist retry state for %s", digest)
        return "error"


def _is_sqlite_locked(exc: BaseException | None) -> bool:
    """True for SQLite ``database is locked`` anywhere in the cause chain."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


class _DashboardAcknowledgeLocked(Exception):
    """Snapshots were rendered and published; only the revision ack was locked."""

    def __init__(self, pending_ack: dict):
        super().__init__("dashboard revision acknowledgement is waiting for SQLite")
        self.pending_ack = pending_ack


def _defer_spooled_task_without_attempt(
    path: Path,
    digest: str,
    owner: str | None,
    error: str,
    *,
    delay_seconds: float | None = None,
    pending_ack: dict | None = None,
) -> bool:
    """Requeue transient maintenance contention without consuming an attempt.

    ``pending_ack`` is kept only when given; any other requeue drops it so a
    later attempt renders again instead of replaying a stale acknowledgement.
    Returns False when a SQLite-lock deferral would exceed its cap.
    """
    try:
        with _locked_spool() as directory:
            envelope = _load_spooled_task(path)
            if envelope is None or (owner and envelope.get("owner") != owner):
                return False
            lock_deferrals = max(0, int(envelope.get("lock_deferrals") or 0))
            if error == _SQLITE_LOCKED:
                if lock_deferrals >= _SQLITE_LOCK_DEFERRAL_MAX:
                    return False
                lock_deferrals += 1
                envelope["lock_deferrals"] = lock_deferrals
                delay_seconds = min(
                    _SQLITE_LOCK_MAX_DELAY_SECONDS,
                    _SQLITE_LOCK_BASE_DELAY_SECONDS * float(2 ** (lock_deferrals - 1)),
                )
            if pending_ack is not None:
                envelope["pending_ack"] = pending_ack
            else:
                envelope.pop("pending_ack", None)
            retry_delay = (
                float(_refresh_busy_delay_seconds)
                if delay_seconds is None
                else max(0.0, float(delay_seconds))
            )
            transient_deferrals = max(
                0, int(envelope.get("transient_deferrals") or 0)
            )
            if error == _SCORE_TRANSPORT_RETRY:
                entry_index = max(0, int(envelope.get("next_entry_index") or 0))
                deferral_index = envelope.get("score_transport_entry_index", entry_index)
                score_deferrals = max(
                    0, int(envelope.get("score_transport_deferrals") or 0)
                ) if deferral_index == entry_index else 0
                if score_deferrals >= _SCORE_TRANSPORT_DEFERRAL_MAX:
                    return False
                envelope["score_transport_entry_index"] = entry_index
                envelope["score_transport_deferrals"] = score_deferrals + 1
                retry_delay = min(
                    300.0,
                    _SCORE_TRANSPORT_BASE_DELAY_SECONDS * float(2 ** score_deferrals),
                )
            if error == _DASHBOARD_SNAPSHOT_INPUTS_CHANGED:
                transient_deferrals += 1
                retry_delay = min(
                    300.0,
                    retry_delay * float(2 ** min(transient_deferrals - 1, 6)),
                )
            envelope.update(
                attempts=max(0, int(envelope.get("attempts") or 0) - 1),
                transient_deferrals=transient_deferrals,
                owner="",
                lease_until=0.0,
                claimed_at=0.0,
                next_attempt_at=time.time() + retry_delay,
                last_error=str(error)[:1000],
                enqueued_at=time.time(),
            )
            _atomic_write(directory, path, _canonical_json(envelope))
            pending = directory / f"{digest}.json"
            if path != pending:
                _move_atomic(path, pending)
            return True
    except Exception:
        logger.exception("[Webhook] failed to defer busy task %s", digest)
        return False


def _finish_spooled_task(
    future, path: Path, digest: str, owner: str | None = None
) -> None:
    success = False
    partial = False
    deferred = False
    defer_error = ""
    error = "task_failed"
    pending_ack = None
    try:
        result = future.result()
        if result == "task_busy":
            defer_error = "refresh_lock_busy"
        elif result == _DASHBOARD_SNAPSHOT_INPUTS_CHANGED:
            defer_error = _DASHBOARD_SNAPSHOT_INPUTS_CHANGED
        elif result == _SCORE_TRANSPORT_RETRY:
            defer_error = _SCORE_TRANSPORT_RETRY
        elif result == _DELIVERY_CONTRACT_CHANGED:
            defer_error = _DELIVERY_CONTRACT_CHANGED
        deferred = bool(defer_error)
        success = result is True
        partial = result == _WEBHOOK_PARTIAL
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if _is_sqlite_locked(exc):
            defer_error = _SQLITE_LOCKED
            deferred = True
            pending_ack = getattr(exc, "pending_ack", None)
            logger.warning(
                "[Webhook] SQLite busy for %s; deferring without an attempt%s",
                digest, " (acknowledgement only)" if pending_ack else "",
            )
        else:
            logger.exception("[Webhook] durable task worker crashed for %s", digest)

    if deferred:
        defer_delay = (
            _refresh_busy_delay_seconds
            if defer_error == "refresh_lock_busy"
            else _dashboard_snapshot_retry_delay_seconds
        )
        deferred = _defer_spooled_task_without_attempt(
            path, digest, owner, defer_error, delay_seconds=defer_delay,
            pending_ack=pending_ack,
        )
        if not deferred and defer_error == _SQLITE_LOCKED:
            # Cap reached (or stale owner): fall back to the attempt counter.
            _retry_or_dead_letter(path, digest, owner, error)
        elif not deferred:
            defer_failure = (
                "refresh_busy_defer_failed"
                if defer_error == "refresh_lock_busy"
                else f"{defer_error}_defer_failed"
            )
            _retry_or_dead_letter(path, digest, owner, defer_failure)
    elif partial:
        partial = _archive_partial_task(path, digest, owner)
    elif success:
        success = _complete_spooled_task(path, digest, owner)
    else:
        _retry_or_dead_letter(path, digest, owner, error)

    with _webhook_inflight_lock:
        _webhook_inflight.discard(digest)
        claim = _webhook_claims.get(digest)
        if claim is None or owner is None or claim[1] == owner:
            _webhook_claims.pop(digest, None)
        if success or deferred or partial:
            _webhook_retry_state.pop(digest, None)
        else:
            attempts, _retry_at = _webhook_retry_state.get(digest, (0, 0.0))
            attempts += 1
            delay = min(60.0, float(2 ** min(attempts, 6)))
            _webhook_retry_state[digest] = (attempts, time.monotonic() + delay)

    try:
        _webhook_slots.release()
    except ValueError:
        logger.error("[Webhook] queue slot accounting overflow")
    _webhook_dispatcher_wake.set()


def _archive_partial_task(path: Path, digest: str, owner: str | None) -> bool:
    """Keep the negative receipt, without retrying scored entries or claiming success."""
    try:
        with _locked_spool() as directory:
            envelope = _load_spooled_task(path)
            if envelope is None or (owner and envelope.get("owner") != owner):
                return False
            rows = _terminal_outcomes(envelope)
            if not rows or envelope.get("next_entry_index") != len(envelope["task"]["entries"]):
                return False
            _dead_letter_unlocked(directory, path, envelope, _WEBHOOK_PARTIAL)
        return True
    except Exception:
        # Leave the checkpoint in place. Lease recovery can retry archival, but
        # cannot rerun entries before its cursor or increase their model budget.
        logger.exception("[Webhook] could not archive partial task %s", digest)
        return False


def _execute_dashboard_snapshot_task(task: dict) -> bool | str:
    from services.dashboard_snapshots import (
        build_all_dashboard_snapshots, build_realtime_dashboard_snapshots,
    )
    from services.dashboard_snapshot_updates import (
        capture_realtime_revision,
    )

    if int(task.get("snapshot_version") or 0) != 1:
        logger.info("[DashboardSnapshot] discarded stale task version")
        return True
    revision = capture_realtime_revision()
    realtime_only = task.get("task_type") == "dashboard_realtime_refresh"
    result = (
        build_realtime_dashboard_snapshots() if realtime_only
        else build_all_dashboard_snapshots()
    )
    pending_ack = {
        "revision": int(revision),
        "realtime_only": bool(realtime_only),
        "ok": sorted(str(card) for card in result.get("ok") or ()),
        "failed": sorted(str(card) for card in result.get("failed") or ()),
        "deferred": sorted(str(card) for card in result.get("deferred") or ()),
    }
    _acknowledge_dashboard_render(pending_ack)
    return _dashboard_snapshot_outcome(pending_ack)


def _acknowledge_dashboard_render(pending_ack: dict) -> None:
    from services.dashboard_snapshot_updates import acknowledge_published_realtime

    try:
        acknowledge_published_realtime(pending_ack, pending_ack["revision"])
    except Exception as exc:
        if _is_sqlite_locked(exc):
            raise _DashboardAcknowledgeLocked(pending_ack) from exc
        raise


def _dashboard_snapshot_outcome(pending_ack: dict) -> bool | str:
    from services.dashboard_snapshot_updates import REALTIME_CARDS

    expected = set(REALTIME_CARDS)
    if not pending_ack["realtime_only"]:
        expected.add("model_quality")
    published = set(pending_ack["ok"])
    failed = set(pending_ack["failed"])
    deferred = set(pending_ack["deferred"])
    if (
        not failed and deferred == {"model_quality"}
        and published == expected - deferred
    ):
        return _DASHBOARD_SNAPSHOT_INPUTS_CHANGED
    return published == expected and not failed and not deferred


def _valid_pending_ack(task: dict, value: Any) -> dict | None:
    """Return a well-formed stored acknowledgement for this task, else None."""
    if task.get("task_type") not in _DASHBOARD_TASK_TYPES or not isinstance(value, dict):
        return None
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None
    if value.get("realtime_only") is not (
        task.get("task_type") == "dashboard_realtime_refresh"
    ):
        return None
    cards = {}
    for key in ("ok", "failed", "deferred"):
        items = value.get(key)
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            return None
        cards[key] = list(items)
    return {"revision": revision, "realtime_only": value["realtime_only"], **cards}


def _resume_dashboard_acknowledge(pending_ack: dict) -> bool | str:
    """Finish a rendered snapshot task without rendering it again."""
    _acknowledge_dashboard_render(pending_ack)
    return _dashboard_snapshot_outcome(pending_ack)


def _execute_spooled_task(task: dict) -> bool | str:
    if task.get("task_type") == "refresh":
        from services.refresh import REFRESH_BUSY, run_refresh_command

        result = run_refresh_command()
        if result is True:
            _submit_background_task(
                {
                    "task_type": "dashboard_snapshot_refresh",
                    "snapshot_version": 1,
                    "release_version": str(
                        os.environ.get("AI_FILTER_VERSION") or "unknown"
                    ),
                    "request_window": int(time.time() // 300),
                }
            )
        return "task_busy" if result == REFRESH_BUSY else result
    if task.get("task_type") in {"dashboard_snapshot_refresh", "dashboard_realtime_refresh"}:
        return _execute_dashboard_snapshot_task(task)
    if task.get("task_type") == "briefing_refresh":
        from routes.dashboard_routes import run_briefing_refresh_command

        return run_briefing_refresh_command(task)
    return _score_in_background(task)


def _terminal_outcomes(envelope: dict) -> list[dict]:
    """Validate bounded audit rows against immutable task identity; fail closed."""
    rows = envelope.get("terminal_unscored", [])
    entries = envelope["task"].get("entries", [])
    cursor = int(envelope.get("next_entry_index") or 0)
    if not isinstance(rows, list) or len(rows) > len(entries):
        raise ValueError("invalid terminal outcome list")
    indexes = set()
    for row in rows:
        index = row.get("entry_index") if isinstance(row, dict) else None
        if type(index) is not int or not 0 <= index < min(cursor, len(entries)) or index in indexes:
            raise ValueError("invalid terminal entry index")
        strategy = row.get("strategy_version")
        link = entries[index].get("url", "") or entries[index].get("link", "")
        if not isinstance(strategy, str) or not strategy.strip() or not link:
            raise ValueError("missing terminal identity")
        key = hashlib.sha256(f"{link.strip()}\0{strategy.strip()}".encode()).hexdigest()
        if row.get("article_key") != key or row.get("outcome") != SCORE_TERMINAL_UNSCORED_REASON:
            raise ValueError("mismatched terminal identity")
        indexes.add(index)
    return rows


def _persist_claim_progress(
    path: Path,
    digest: str,
    owner: str,
    next_entry_index: int,
    passed_count: int,
    filtered_count: int,
    *,
    terminal_outcome: dict | None = None,
) -> bool:
    """Persist entry cursor and cumulative stats outside the task digest."""
    try:
        with _locked_spool() as directory:
            envelope = _load_spooled_task(path)
            if envelope is None or envelope.get("owner") != owner:
                return False
            previous = max(0, int(envelope.get("next_entry_index") or 0))
            terminal_rows = list(_terminal_outcomes(envelope))
            if terminal_outcome is not None:
                index = terminal_outcome.get("entry_index")
                if index != previous or next_entry_index != previous + 1:
                    raise ValueError("terminal checkpoint must advance exactly one entry")
                terminal_rows.append(dict(terminal_outcome))
                envelope["terminal_unscored"] = terminal_rows
            envelope["next_entry_index"] = max(previous, int(next_entry_index))
            _terminal_outcomes(envelope)
            if envelope["next_entry_index"] > previous:
                # A completed entry cannot spend the next entry's score budget.
                # Repeated/stale progress callbacks must not renew this budget.
                envelope.pop("score_transport_deferrals", None)
                envelope.pop("score_transport_entry_index", None)
            previous_progress = envelope.get("progress")
            if not isinstance(previous_progress, dict):
                previous_progress = {}
            envelope["progress"] = {
                "passed": max(
                    0,
                    int(previous_progress.get("passed") or 0),
                    int(passed_count),
                ),
                "filtered": max(
                    0,
                    int(previous_progress.get("filtered") or 0),
                    int(filtered_count),
                ),
            }
            if terminal_rows:
                envelope["progress"]["terminal_unscored"] = len(terminal_rows)
            _atomic_write(directory, path, _canonical_json(envelope))
            if terminal_rows:
                # A negative outcome must be durable before a later entry runs.
                # Unlike legacy best-effort directory sync, failure stops here.
                _fsync_directory(path.parent, strict=True)
        return True
    except Exception:
        logger.exception("[Webhook] failed to persist entry cursor for %s", digest)
        return False


def _execute_claimed_spooled_task(path: Path, envelope: dict) -> bool | str:
    """Execute one claimed task with a durable per-entry progress cursor."""
    task = envelope["task"]
    if task.get("task_type"):
        pending_ack = _valid_pending_ack(task, envelope.get("pending_ack"))
        if pending_ack is not None:
            return _resume_dashboard_acknowledge(pending_ack)
        return _execute_spooled_task(task)

    start_index = max(0, int(envelope.get("next_entry_index") or 0))
    digest = str(envelope["digest"])
    owner = str(envelope.get("owner") or "")

    progress = envelope.get("progress")
    if not isinstance(progress, dict):
        progress = {}

    def persist(
        next_entry_index: int, passed_count: int, filtered_count: int,
        *, terminal_outcome: dict | None = None,
    ) -> None:
        if not _persist_claim_progress(
            path,
            digest,
            owner,
            next_entry_index,
            passed_count,
            filtered_count,
            **({"terminal_outcome": terminal_outcome} if terminal_outcome is not None else {}),
        ):
            raise RuntimeError("could not persist webhook entry progress")

    terminal_count = len(_terminal_outcomes(envelope))
    if terminal_count:
        # A previous attempt may have renamed but failed directory sync.
        _fsync_directory(path.parent, strict=True)
    _task_execution_context.start_entry_index = start_index
    _task_execution_context.start_terminal_count = terminal_count
    _task_execution_context.start_passed_count = max(
        0, int(progress.get("passed") or 0)
    )
    _task_execution_context.start_filtered_count = max(
        0, int(progress.get("filtered") or 0)
    )
    _task_execution_context.progress_callback = persist
    _task_execution_context.deadline = (
        time.monotonic() + _webhook_task_deadline_seconds
    )
    _task_execution_context.receipt_contract = (envelope.get("receipt_context") or {}).get("strategy")
    try:
        return _execute_spooled_task(task)
    finally:
        for name in (
            "start_entry_index",
            "start_terminal_count",
            "start_passed_count",
            "start_filtered_count",
            "progress_callback",
            "deadline",
            "receipt_contract",
        ):
            try:
                delattr(_task_execution_context, name)
            except AttributeError:
                pass


def _submit_claimed_task(
    path: Path, envelope: dict, *, slot_acquired: bool, admission_lease=None
) -> str:
    try:
        lease = admission_lease or reserve_lease("claimed_task", kind="drain")
    except BaseException:
        if slot_acquired:
            _webhook_slots.release()
        raise
    digest = str(envelope["digest"])
    owner = str(envelope.get("owner") or "")
    with _webhook_inflight_lock:
        if digest in _webhook_inflight:
            if slot_acquired:
                _webhook_slots.release()
            lease.finish(success=True)
            return "skipped"
        _webhook_inflight.add(digest)
        _webhook_claims[digest] = (path, owner)

    try:
        task_type = envelope["task"].get("task_type")
        if task_type in {"refresh", "dashboard_snapshot_refresh", "dashboard_realtime_refresh"}:
            executor = _get_refresh_executor()
        elif task_type == "briefing_refresh":
            executor = _get_briefing_executor()
        else:
            executor = _get_executor()
        future = executor.submit(_execute_with_admission_lease, lease, path, envelope)
    except Exception:
        with _webhook_inflight_lock:
            _webhook_inflight.discard(digest)
            _webhook_claims.pop(digest, None)
        try:
            with lease.activate():
                _retry_or_dead_letter(path, digest, owner, "executor_submit_failed")
        except BaseException:
            lease.finish(success=False)
            raise
        else:
            lease.finish(success=True)
        finally:
            if slot_acquired:
                _webhook_slots.release()
        logger.exception("[Webhook] failed to dispatch durable task")
        return "busy"

    future.add_done_callback(
        lambda completed, task_path=path, task_digest=digest, task_owner=owner: _finish_with_admission_lease(
            lease, completed, task_path, task_digest, task_owner
        )
    )
    return "submitted"


def _execute_with_admission_lease(lease, path, envelope):
    with lease.activate():
        return _execute_claimed_spooled_task(path, envelope)


def _finish_with_admission_lease(lease, future, path, digest, owner):
    try:
        with lease.activate():
            _finish_spooled_task(future, path, digest, owner)
    except BaseException:
        lease.finish(success=False)
        raise
    lease.finish(success=True)


def _renew_owned_claims_once(now: float | None = None) -> int:
    now = time.time() if now is None else float(now)
    with _webhook_inflight_lock:
        claims = list(_webhook_claims.items())
    renewed = 0
    with _locked_spool() as directory:
        for digest, (path, owner) in claims:
            if not path.exists():
                continue
            envelope = _load_spooled_task(path)
            if envelope is None or envelope.get("owner") != owner:
                continue
            if float(envelope.get("lease_until") or 0.0) - now > _webhook_lease_seconds / 2:
                continue
            envelope["lease_until"] = now + _webhook_lease_seconds
            _atomic_write(directory, path, _canonical_json(envelope))
            renewed += 1
    return renewed


def _dispatch_spooled_tasks_once() -> int:
    """Atomically claim recovered tasks and fill only bounded executor capacity."""
    enforced = os.environ.get("AI_FILTER_ADMISSION_MODE", "disabled") != "disabled"
    with _webhook_inflight_lock:
        has_claims = bool(_webhook_claims)
    if not enforced or has_claims:
        try:
            with admission("claim_renewal", kind="drain"):
                _renew_owned_claims_once()
        except AdmissionClosed:
            return 0
    submitted = 0
    while _webhook_slots.acquire(blocking=False):
        if enforced and not _spool_may_need_activity():
            _webhook_slots.release()
            break
        try:
            lease = reserve_lease("queue_claim", kind="drain")
        except AdmissionClosed:
            _webhook_slots.release()
            break
        except BaseException:
            _webhook_slots.release()
            raise
        try:
            with lease.activate():
                claimed = _claim_next_spooled_task()
        except BaseException:
            try:
                lease.finish(success=False)
            finally:
                _webhook_slots.release()
            raise
        if claimed is None:
            try:
                lease.finish(success=True)
            finally:
                _webhook_slots.release()
            break
        path, envelope = claimed
        outcome = _submit_claimed_task(path, envelope, slot_acquired=True, admission_lease=lease)
        if outcome == "submitted":
            submitted += 1
    return submitted


def _spool_may_need_activity() -> bool:
    """Read-only bounded hint, never a queue-empty or recovery confirmation.

    Skip idle admission records. Any mutation/recovery still takes the real
    activity lease and existing spool lock. A race to empty may defer work by
    one dispatcher tick; a race to nonempty cannot bypass the guarded claim.
    """
    with _webhook_inflight_lock:
        owned = set(_webhook_claims)
    inspected = 0
    for folder in (_webhook_spool_dir, _webhook_spool_dir / _SPOOL_INFLIGHT_DIR,
                   _webhook_spool_dir / _SPOOL_DLQ_DIR):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    inspected += 1
                    if inspected > 4096:
                        return True
                    if folder == _webhook_spool_dir:
                        if entry.name.endswith(".json"):
                            return True
                    elif folder.name == _SPOOL_INFLIGHT_DIR:
                        if entry.name.endswith(".json") and entry.name[:-5] not in owned:
                            return True
                    elif entry.name.endswith(".terminal-pending.json"):
                        return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _webhook_dispatch_loop() -> None:
    while not _webhook_dispatcher_stop.is_set():
        _webhook_dispatcher_wake.clear()
        try:
            _dispatch_spooled_tasks_once()
        except Exception:
            logger.exception("[Webhook] durable dispatcher iteration failed")
        _webhook_dispatcher_wake.wait(timeout=1.0)


def start_webhook_dispatcher() -> threading.Thread:
    """Start the single recovery/dispatch loop; safe to call repeatedly."""
    global _webhook_dispatcher_thread
    with _webhook_dispatcher_lock:
        if (
            _webhook_dispatcher_thread is not None
            and _webhook_dispatcher_thread.is_alive()
        ):
            return _webhook_dispatcher_thread
        _webhook_dispatcher_stop.clear()
        _webhook_dispatcher_thread = threading.Thread(
            target=_webhook_dispatch_loop,
            name="webhook-spool-dispatcher",
            daemon=True,
        )
        _webhook_dispatcher_thread.start()
        thread = _webhook_dispatcher_thread
        logger.info("[Webhook] durable spool dispatcher started")
    _webhook_dispatcher_wake.set()
    return thread


def wait_for_webhook_idle(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _webhook_inflight_lock:
            if not _webhook_inflight:
                return True
        if time.monotonic() >= deadline:
            return False
        try:
            _renew_owned_claims_once()
        except AdmissionClosed:
            # A frozen/uncertain fence may not renew durable queue state.
            # Waiting is not proof that work stopped; retain the original
            # deadline and return False if existing ownership does not drain.
            pass
        time.sleep(0.05)


def webhook_dispatcher_alive() -> bool:
    with _webhook_dispatcher_lock:
        return bool(
            _webhook_dispatcher_thread is not None
            and _webhook_dispatcher_thread.is_alive()
        )


def webhook_inflight_count() -> int:
    with _webhook_inflight_lock:
        return len(_webhook_inflight)


def stop_webhook_dispatcher(timeout: float = 5.0) -> bool:
    """Stop claiming, renew leases while draining, then close the executor."""
    global _webhook_dispatcher_thread, _webhook_executor, _refresh_executor, _briefing_executor
    started = time.monotonic()
    with _webhook_dispatcher_lock:
        thread = _webhook_dispatcher_thread
        if thread is not None:
            _webhook_dispatcher_stop.set()
            _webhook_dispatcher_wake.set()
    if thread is not None:
        thread.join(timeout=max(0.0, timeout))
    with _webhook_dispatcher_lock:
        if _webhook_dispatcher_thread is thread and (
            thread is None or not thread.is_alive()
        ):
            _webhook_dispatcher_thread = None
    remaining = max(0.0, timeout - (time.monotonic() - started))
    idle = wait_for_webhook_idle(remaining)
    with _webhook_executor_lock:
        executor = _webhook_executor
        _webhook_executor = None
    if executor is not None:
        executor.shutdown(wait=idle, cancel_futures=True)
    with _refresh_executor_lock:
        refresh_executor = _refresh_executor
        _refresh_executor = None
    if refresh_executor is not None:
        refresh_executor.shutdown(wait=idle, cancel_futures=True)
    with _briefing_executor_lock:
        briefing_executor = _briefing_executor
        _briefing_executor = None
    if briefing_executor is not None:
        briefing_executor.shutdown(wait=idle, cancel_futures=True)
    return idle


def _queue_committed_dashboard_inputs() -> None:
    from services.dashboard_snapshot_updates import enqueue_pending_realtime_snapshot

    enqueue_pending_realtime_snapshot()


def _record_webhook_feed_health(
    feed_url: str, feed_name: str, passed_count: int, filtered_count: int,
    terminal_count: int = 0,
) -> None:
    """极速路由器模式：更新 feed_health（累加本次批次统计）。"""
    try:
        DB.update_feed_health(
            url=feed_url,
            name=feed_name,
            response_time_ms=0,
            success=not terminal_count,
            articles_total=passed_count + filtered_count + terminal_count,
            articles_passed=passed_count,
            avg_score=None,
            error=f"终止未评分 {terminal_count} 条" if terminal_count else None,
        )
    except Exception as e:
        logger.error("[Webhook] Failed to update feed_health: %s", e)


def _checkpoint_terminal_entry(callback, entry_index, link, cfg, passed, filtered) -> None:
    if getattr(_task_execution_context, "progress_callback", None) is not callback:
        raise RuntimeError("terminal entry requires a durable batch checkpoint")
    strategy = str(cfg.get("scoring_strategy_version") or "").strip()
    callback(entry_index + 1, passed, filtered, terminal_outcome={
        "entry_index": entry_index,
        "article_key": hashlib.sha256(f"{link.strip()}\0{strategy}".encode()).hexdigest(),
        "strategy_version": strategy,
        "outcome": SCORE_TERMINAL_UNSCORED_REASON,
        "recorded_at": time.time(),
    })


def _finish_webhook_batch(feed_url, feed_name, passed, filtered, terminal) -> bool | str:
    # Ingestion is not a full source sweep and cannot clear a failed refresh.
    if passed + filtered > 0:
        try:
            DB.set_app_meta("last_ingestion", str(int(time.time())))
        except Exception as exc:
            logger.error("[Webhook] Failed to update ingestion timestamp: %s", exc)
    if terminal:
        _record_webhook_feed_health(feed_url, feed_name, passed, filtered, terminal)
    else:
        _record_webhook_feed_health(feed_url, feed_name, passed, filtered)
    logger.info("[Webhook] Batch done: %d passed, %d filtered, %d terminal from %s",
                passed, filtered, terminal, feed_url)
    return _WEBHOOK_PARTIAL if terminal else True


def _score_in_background(task: dict) -> bool | str:
    """后台线程池任务：不阻塞 Flask 主线程，独立完成评分→入库"""
    try:
        cfg = resolve_scoring_contract(_cfg(), register_snapshot=True)
        expected = getattr(_task_execution_context, "receipt_contract", None)
        if expected and expected != str(cfg.get("scoring_strategy_version") or "unknown"):
            return _DELIVERY_CONTRACT_CHANGED
        scorer = Scorer()
        few_shot_context = str(cfg.get("_few_shot_context") or "")
        feed_url = DB.canonical_feed_url(cfg, task["feed_url"])
        prompt_text = resolve_prompt(feed_url, cfg)[1]
        notify_threshold = _find_notify_threshold(cfg, feed_url)
        feed_cfg = DB.get_feed_config(cfg, feed_url)
        feed_name = (
            str(feed_cfg.get("name") or "").strip()
            or feed_url.rsplit("/", 1)[-1]
        )
        passed_count = max(
            0,
            int(getattr(_task_execution_context, "start_passed_count", 0) or 0),
        )
        filtered_count = max(
            0,
            int(getattr(_task_execution_context, "start_filtered_count", 0) or 0),
        )
        terminal_count = int(getattr(_task_execution_context, "start_terminal_count", 0))
        entries = task["entries"]
        start_index = max(
            0, int(getattr(_task_execution_context, "start_entry_index", 0) or 0)
        )
        progress_callback = getattr(
            _task_execution_context,
            "progress_callback",
            lambda _index, _passed, _filtered: None,
        )
        deadline = float(
            getattr(
                _task_execution_context,
                "deadline",
                time.monotonic() + _webhook_task_deadline_seconds,
            )
        )

        for entry_index, entry_data in enumerate(
            entries[start_index:], start=start_index
        ):
            if time.monotonic() >= deadline:
                logger.warning(
                    "[Webhook] task deadline reached before entry %d", entry_index
                )
                return False
            title = plain_text_from_markup(entry_data.get("title", ""))
            link = entry_data.get("url", "") or entry_data.get("link", "")
            content = entry_data.get("content", "") or entry_data.get("summary", "")

            if content and ("<" in content and ">" in content):
                content = clean_html(content)
            # A link can still identify terminated work even when this
            # delivery's title is empty. Let the processor check that identity
            # before its normal invalid-source rejection; never skip its audit.
            if not link:
                progress_callback(entry_index + 1, passed_count, filtered_count)
                continue

            wrapper = _WebhookEntry(title, link, content)

            try:
                with processing_origin("webhook"):
                    scored_entry, reason = process_entry(
                        wrapper,
                        recent_entries=[],
                        prompt_text=prompt_text,
                        cfg=cfg,
                        scorer=scorer,
                        feed_url=feed_url,
                        notify_threshold=notify_threshold,
                        few_shot_context=few_shot_context,
                    )
            except Exception as e:
                logger.warning("[Webhook] process_entry failed for '%s...': %s", title[:40], e)
                # The durable task is the retry boundary.  A partial batch must
                # remain in the spool when any entry fails, otherwise a 202
                # acknowledgement could silently lose that entry.
                return False

            if reason == SCORE_DEFERRED_REASON:
                return _SCORE_TRANSPORT_RETRY  # nothing persisted; cursor stays
            if reason == SCORE_TERMINAL_UNSCORED_REASON:
                _checkpoint_terminal_entry(
                    progress_callback, entry_index, link, cfg, passed_count, filtered_count,
                )
                terminal_count += 1
                continue
            status = "passed" if scored_entry is not None else "filtered"
            if status == "passed":
                passed_count += 1
            else:
                filtered_count += 1

            logger.info(
                "[Webhook] %s → %s (%s) | '%s'",
                status,
                reason,
                feed_url.rsplit("/", 1)[-1] if "/" in feed_url else feed_url,
                title[:60],
            )

            # Atomic input revisions survive a later batch failure.
            progress_callback(entry_index + 1, passed_count, filtered_count)
        return _finish_webhook_batch(feed_url, feed_name, passed_count, filtered_count, terminal_count)
    except Exception as e:
        logger.exception("[Webhook] background scorer crashed: %s", e)
        return False
    finally:
        _queue_committed_dashboard_inputs()


# Publisher section pages that some upstream feeds (e.g. RSSHub
# ``/cls/telegraph``, which emits no <link>) hand to Miniflux as the URL of
# every item.  ``link`` is the article identity everywhere downstream (score
# cache, event identity, evidence), so distinct items sharing one section URL
# silently reused one cached score and one event.  An explicit list is used on
# purpose instead of a heuristic; extend it via
# ``webhook_shared_section_urls`` in the config.
_DEFAULT_SHARED_SECTION_URLS = ("https://www.cls.cn/telegraph",)
_SYNTHETIC_ITEM_PARAM = "ai_filter_item"
_ITEM_KEY_CHARS = 128


def _section_url_key(url: Any) -> str | None:
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except (TypeError, ValueError):
        return None
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return None
    if parts.query or parts.fragment or parts.username or parts.password:
        return None
    if ":" in host:
        host = f"[{host}]"
    default_port = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    if port is not None and not default_port:
        host = f"{host}:{port}"
    return f"{host}{parts.path.rstrip('/') or '/'}"


def _shared_section_urls(cfg: Any) -> dict[str, str]:
    """Map equivalent section spellings to the first configured canonical URL."""
    configured = (cfg or {}).get("webhook_shared_section_urls") if isinstance(cfg, dict) else None
    urls = configured if isinstance(configured, (list, tuple)) else _DEFAULT_SHARED_SECTION_URLS
    shared = {}
    for url in urls:
        key = _section_url_key(url)
        if key:
            scheme = urlsplit(str(url).strip()).scheme.lower()
            shared.setdefault(key, f"{scheme}://{key}")
    return shared


def _upstream_item_key(entry: dict, title: str) -> str:
    """Stable per-item key: Miniflux entry hash, else entry id, else title digest."""
    for field in ("hash", "id"):
        value = entry.get(field)
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return f"{field[0]}-{text[:_ITEM_KEY_CHARS]}"
    digest = hashlib.sha256(str(title or "").strip().encode("utf-8")).hexdigest()
    return f"t-{digest[:32]}"


def _article_link(entry: dict, url: str, title: str, shared: dict[str, str]) -> str:
    """Return ``url`` or, for a shared section page, a stable per-item link.

    A query parameter is used rather than a ``#fragment`` because article
    identity normalisation (``db.database._normalized_article_identity``)
    drops fragments, which would collapse distinct items into one event again.
    """
    key = _section_url_key(url)
    if not key or key not in shared:
        return url
    item = _upstream_item_key(entry, title)
    return f"{shared[key]}?{urlencode({_SYNTHETIC_ITEM_PARAM: item})}"


def _normalize_webhook_entries(entries: list, cfg: Any) -> list:
    """Clip/clean Miniflux entries into the durable task shape."""
    shared = _shared_section_urls(cfg)
    normalized_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content", "") or entry.get("summary", "")
        if content and "<" in str(content) and ">" in str(content):
            content = clean_html(str(content))
        source_title = plain_text_from_markup(entry.get("title", ""))
        title = _clip_text(source_title, _MAX_TITLE_CHARS)
        url = _clip_text(entry.get("url", "") or entry.get("link", ""), _MAX_URL_CHARS)
        normalized_entries.append({
            "title": title,
            "url": _clip_text(_article_link(entry, url, source_title, shared), _MAX_URL_CHARS),
            "content": _clip_utf8(content, _MAX_CONTENT_BYTES),
        })
    return normalized_entries


class _WebhookEntry:
    """Minimal entry wrapper compatible with process_entry"""

    def __init__(self, title: str, link: str, summary: str = ""):
        self.title = title
        self.link = link
        self.summary = summary

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


# ── 路由 ─────────────────────────────────────────────────────


class WebhookPayloadError(ValueError):
    def __init__(self, message, status=400, **details):
        super().__init__(message)
        self.payload = {"error": message, **details}
        self.status = status


def normalize_webhook_delivery(body: bytes) -> dict:
    """The one normalization path, frozen once for a controlled transfer."""
    try:
        payload = json.loads(body)
    except Exception as error:
        raise WebhookPayloadError("Invalid JSON") from error
    if not isinstance(payload, dict) or "entries" not in payload:
        raise WebhookPayloadError("Missing entries")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise WebhookPayloadError("entries must be an array")
    if len(entries) > _webhook_max_entries:
        raise WebhookPayloadError("Too many entries", 413, max_entries=_webhook_max_entries)
    feed_info = payload.get("feed", {})
    if not isinstance(feed_info, dict):
        feed_info = {}
    normalized_entries = _normalize_webhook_entries(entries, _cfg())
    raw_feed_url = _clip_text(
        feed_info.get("feed_url", "unknown"), _MAX_URL_CHARS
    )
    return {
        # Canonicalize before hashing/persisting the durable task.  Otherwise
        # Miniflux's pre-rewrite URL and the container URL can occupy two queue
        # slots and emit the same article twice.
        "feed_url": DB.canonical_feed_url(_cfg(), raw_feed_url),
        "entries": normalized_entries,
    }


@webhook_bp.route("/webhook", methods=["POST"])
def webhook_receiver():
    """验签后严格同步任务及原始消息回执；202只表示接收保全。"""
    body = request.get_data()
    if not _delivery_header_valid(body):
        return _delivery_response({"error": "delivery_identity_mismatch"}, 400)
    try:
        task = getattr(g, "_admission_frozen_delivery_task", None)
        if task is None:
            task = normalize_webhook_delivery(body)
    except WebhookPayloadError as error:
        return _delivery_response(error.payload, error.status)
    submission, receipt = _submit_webhook_delivery(task, body)
    if not submission or submission == "full":
        response = Response(
            json.dumps({"error": "durable_handoff_unconfirmed", "retry_after": 30}),
            status=503,
            mimetype="application/json",
        )
        response.headers["Retry-After"] = "30"
        return response

    return _delivery_response({
        **receipt, "status": "duplicate" if submission == "duplicate" else "received",
        "processed": receipt["state"] == "completed",
    }, 202)


def _delivery_header_valid(body: bytes) -> bool:
    supplied = request.headers.get("X-AI-Filter-Delivery-ID")
    return supplied is None or supplied == hashlib.sha256(body).hexdigest()


def _delivery_response(payload: dict, status: int) -> Response:
    response = Response(json.dumps(payload), status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


@webhook_bp.route("/webhook/receipt", methods=["POST"])
def webhook_receipt():
    """Use the original signed body, never a credential-bearing query string."""
    body = request.get_data()
    if not _delivery_header_valid(body):
        return _delivery_response({"error": "delivery_identity_mismatch"}, 400)
    try:
        with _locked_spool() as directory:
            record = _receipt_store(directory).load(hashlib.sha256(body).hexdigest())
            if record is None:
                return _delivery_response({"error": "receipt_unknown", "durable": False}, 404)
            receipt = _public_delivery(directory, record)
        return _delivery_response({**receipt, "status": "receipt"}, 200)
    except Exception:
        logger.exception("[Webhook] durable receipt cannot be confirmed")
        return _delivery_response({"error": "receipt_unconfirmed", "durable": False}, 503)


# ── helpers ──────────────────────────────────────────────────


def _find_notify_threshold(cfg: dict, feed_url: str) -> float:
    return DB.get_feed_thresholds(cfg, feed_url)["notify"]
