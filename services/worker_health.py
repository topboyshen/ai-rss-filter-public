"""File-backed liveness/readiness probe for the non-HTTP worker container.

The worker is the only process that performs dependency probes.  Docker's
short-lived health-check process only validates the most recent snapshot, so a
health check does not create another Redis connection or fsync a probe file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


def _bounded_env_int(
    name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


HEALTH_PATH = Path(
    os.environ.get("AI_FILTER_WORKER_HEALTH_PATH", "/tmp/ai-filter-worker-health.json")
)
HEALTH_WRITE_INTERVAL_SECONDS = _bounded_env_int(
    "AI_FILTER_WORKER_HEALTH_INTERVAL", 10, 5, 30
)
MAX_AGE_SECONDS = _bounded_env_int(
    "AI_FILTER_WORKER_HEALTH_MAX_AGE", 35, 20, 120
)
DEPENDENCY_PROBE_INTERVAL_SECONDS = _bounded_env_int(
    "AI_FILTER_WORKER_DEPENDENCY_PROBE_INTERVAL", 30, 10, 60
)
QUEUE_MAX_AGE_SECONDS = _bounded_env_int(
    "AI_FILTER_WORKER_QUEUE_MAX_AGE", 1800, 60, 86400
)
STARTUP_GRACE_SECONDS = _bounded_env_int(
    "AI_FILTER_WORKER_STARTUP_GRACE", 60, 0, 300
)

_WORKER_STARTED_AT = time.time()
_dependency_lock = threading.Lock()
_dependency_cache: dict[str, Any] = {
    "checked_at": 0.0,
    "spool_writable": False,
    "redis_ok": False,
}
_redis_client_lock = threading.Lock()
_redis_client: Any | None = None
_DEAD_LETTER_NAME_RE = re.compile(r"^[0-9a-f]{64}\.[0-9]+\.[0-9]+\.json$")


def _spool_dir() -> Path:
    configured = os.environ.get("WEBHOOK_SPOOL_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "data" / "webhook_spool"


def probe_spool_writable() -> bool:
    """Perform a durable write probe; callers should cache this expensive check."""
    directory = _spool_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".health-", dir=str(directory))
        try:
            os.write(fd, b"ok")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(name)
        return True
    except OSError:
        return False


def _close_redis_client_unlocked() -> None:
    global _redis_client
    client = _redis_client
    _redis_client = None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def probe_redis() -> bool:
    """Ping Redis through one reusable client, reconnecting only after failure."""
    global _redis_client
    from services import shared_cache

    if not shared_cache.redis_enabled():
        return False
    with _redis_client_lock:
        if _redis_client is None:
            # The shared cache owns the Redis URL and connection options. Keeping
            # this client here avoids a new TCP connection for every heartbeat.
            _redis_client = shared_cache.redis_client()
        if _redis_client is None:
            return False
        try:
            ok = bool(_redis_client.ping())
        except Exception:
            _close_redis_client_unlocked()
            return False
        if not ok:
            _close_redis_client_unlocked()
        return ok


def _dependency_status(
    *, force: bool = False, now_monotonic: float | None = None
) -> dict[str, Any]:
    """Return cached dependency health, sampling at most every 30 seconds."""
    now_monotonic = (
        time.monotonic() if now_monotonic is None else float(now_monotonic)
    )
    with _dependency_lock:
        checked_at = float(_dependency_cache.get("checked_at") or 0.0)
        age = now_monotonic - checked_at
        if (
            not force
            and checked_at > 0.0
            and 0.0 <= age < DEPENDENCY_PROBE_INTERVAL_SECONDS
        ):
            return dict(_dependency_cache)
        try:
            spool_writable = bool(probe_spool_writable())
        except Exception:
            spool_writable = False
        try:
            redis_ok = bool(probe_redis())
        except Exception:
            redis_ok = False
        _dependency_cache.update(
            checked_at=now_monotonic,
            spool_writable=spool_writable,
            redis_ok=redis_ok,
        )
        return dict(_dependency_cache)


def _is_task_path(path: Path) -> bool:
    stem = path.stem
    return (
        path.is_file()
        and path.suffix == ".json"
        and len(stem) == 64
        and all(char in "0123456789abcdef" for char in stem)
    )


def _task_created_at(path: Path) -> tuple[float, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return path.stat().st_mtime, False
    if not isinstance(payload, dict):
        return path.stat().st_mtime, False
    raw_created_at = payload.get("created_at")
    if raw_created_at is None:
        # Pre-envelope durable tasks legitimately use filesystem mtime.
        return path.stat().st_mtime, True
    try:
        created_at = float(raw_created_at)
    except (TypeError, ValueError):
        return path.stat().st_mtime, False
    if created_at <= 0.0:
        return path.stat().st_mtime, False
    return created_at, True


def collect_spool_metrics(now: float | None = None) -> dict[str, Any]:
    """Inspect the bounded durable spool without importing the worker routes."""
    now = time.time() if now is None else float(now)
    directory = _spool_dir()
    pending: list[Path] = []
    inflight: list[Path] = []
    dead_letter: list[Path] = []
    scan_ok = True
    invalid_files = 0
    try:
        if directory.exists():
            pending = [path for path in directory.iterdir() if _is_task_path(path)]
            inflight_dir = directory / "inflight"
            if inflight_dir.exists():
                inflight = [
                    path for path in inflight_dir.iterdir() if _is_task_path(path)
                ]
            dead_letter_dir = directory / "dead-letter"
            if dead_letter_dir.exists():
                dead_letter = [
                    path
                    for path in dead_letter_dir.iterdir()
                    if path.is_file()
                    and _DEAD_LETTER_NAME_RE.fullmatch(path.name)
                ]
    except OSError:
        scan_ok = False

    created_times: list[float] = []
    for path in pending + inflight:
        try:
            created_at, valid = _task_created_at(path)
            created_times.append(created_at)
            if not valid:
                invalid_files += 1
        except OSError:
            # A task completing during the scan is normal.  Do not turn that
            # race into a false worker failure.
            continue
    oldest_age = (
        max(0.0, now - min(created_times)) if created_times else 0.0
    )
    return {
        "pending": len(pending),
        "inflight": len(inflight),
        "dead_letter": len(dead_letter),
        "invalid_files": invalid_files,
        "oldest_task_age_seconds": round(oldest_age, 3),
        "task_max_age_seconds": QUEUE_MAX_AGE_SECONDS,
        "scan_ok": scan_ok,
    }


def _queue_status(metrics: dict[str, Any], now: float) -> dict[str, Any]:
    oldest_age = float(metrics.get("oldest_task_age_seconds") or 0.0)
    stale = oldest_age > QUEUE_MAX_AGE_SECONDS
    startup_grace = max(0.0, now - _WORKER_STARTED_AT) < STARTUP_GRACE_SECONDS
    scan_ok = metrics.get("scan_ok") is True
    # A DLQ entry needs operator remediation, but restarting the worker cannot
    # clear durable dead letters.  Keep DLQ as an alert/metric and reserve
    # liveness failure for states a restart can plausibly recover from.
    # Queue age is an operational alert, not a liveness failure: restarting a
    # healthy dispatcher cannot make old durable work younger and can prolong a
    # legitimate backlog. Liveness is reserved for an unreadable queue scan;
    # admin/readiness metrics still expose ``stale`` for alerting.
    healthy = scan_ok
    return {
        **metrics,
        "stale": stale,
        "startup_grace": startup_grace,
        "healthy": healthy,
    }


def write_worker_health(
    dispatcher_alive: bool,
    running: bool = True,
    *,
    subsystems: dict[str, bool] | None = None,
    now: float | None = None,
    force_dependency_probe: bool = False,
) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    dependencies = _dependency_status(force=force_dependency_probe)
    queue = _queue_status(collect_spool_metrics(now=now), now)
    payload = {
        "pid": os.getpid(),
        "started_at": _WORKER_STARTED_AT,
        "updated_at": now,
        "running": bool(running),
        "dispatcher_alive": bool(dispatcher_alive),
        "subsystems": {
            str(name): bool(alive)
            for name, alive in (subsystems or {}).items()
        },
        "spool_writable": dependencies["spool_writable"],
        "redis_ok": dependencies["redis_ok"],
        "dependency_checked_monotonic": dependencies["checked_at"],
        "queue": queue,
    }
    HEALTH_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".worker-health-", dir=str(HEALTH_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            fd = -1
            json.dump(payload, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, HEALTH_PATH)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return payload


def check_worker_health(now: float | None = None) -> tuple[bool, dict[str, Any]]:
    """Validate the worker snapshot without performing live dependency I/O."""
    now = time.time() if now is None else float(now)
    try:
        payload = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, {"error": f"health_file: {type(exc).__name__}"}
    try:
        age = now - float(payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        age = MAX_AGE_SECONDS + 1.0
    queue = payload.get("queue")
    subsystems = payload.get("subsystems", {})
    required = (
        payload.get("running") is True,
        payload.get("dispatcher_alive") is True,
        payload.get("spool_writable") is True,
        payload.get("redis_ok") is True,
        isinstance(subsystems, dict) and all(
            value is True for value in subsystems.values()
        ),
        isinstance(queue, dict) and queue.get("healthy") is True,
        -5.0 <= age <= MAX_AGE_SECONDS,
    )
    try:
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            raise ValueError("invalid pid")
        os.kill(pid, 0)
        pid_alive = True
    except (OSError, TypeError, ValueError):
        pid_alive = False
    diagnostic = dict(payload)
    diagnostic["health_age_seconds"] = round(age, 3)
    return all(required) and pid_alive, diagnostic


def close_worker_health_resources() -> None:
    """Close the reusable Redis client during graceful worker shutdown."""
    with _redis_client_lock:
        _close_redis_client_unlocked()


def _reset_health_state_for_tests() -> None:
    """Reset caches used by unit tests without exposing them as runtime API."""
    with _dependency_lock:
        _dependency_cache.update(
            checked_at=0.0,
            spool_writable=False,
            redis_ok=False,
        )
    close_worker_health_resources()


def main() -> int:
    healthy, payload = check_worker_health()
    if not healthy:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
