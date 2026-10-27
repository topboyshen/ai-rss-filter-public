#!/usr/bin/env python3
"""
AI-Filter 后台 Worker（从 Gunicorn 拆出）。

职责：
- 持久 webhook spool 调度与评分
- 行情刷新与 Quant 上下文轮询
- 通过 Redis 共享短期缓存与 Web 进程交换只读卡片状态
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from services.admission_fence import AdmissionClosed, guarded
from services.controlled_startup import initialize_runtime_database
from services import production_startup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("worker_main")
_dashboard_retry_lock = threading.Lock()
_dashboard_retry_thread: threading.Thread | None = None


def _queue_pending_dashboard_publication() -> None:
    from services.dashboard_snapshot_updates import enqueue_pending_realtime_snapshot

    try:
        outcome = enqueue_pending_realtime_snapshot()
    except AdmissionClosed:
        return
    if outcome in {"full", "unavailable"}:
        logger.warning("[Worker] dashboard publication remains pending: %s", outcome)


def _retry_pending_dashboard_publication() -> None:
    """Never block health writes on SQLite or the cross-process spool lock."""
    global _dashboard_retry_thread

    if not _dashboard_retry_lock.acquire(blocking=False):
        return
    try:
        if _dashboard_retry_thread is not None and _dashboard_retry_thread.is_alive():
            return
        _dashboard_retry_thread = threading.Thread(
            target=_queue_pending_dashboard_publication,
            name="dashboard-publication-recovery", daemon=True,
        )
        _dashboard_retry_thread.start()
    finally:
        _dashboard_retry_lock.release()


@guarded("worker_bootstrap", kind="intake")
def _bootstrap_dashboard_publication() -> None:
    """Initialize scheduled statistics separately from cheap dirty-input cards."""
    try:
        from routes.webhook_routes import _submit_background_task
        from services.dashboard_snapshots import snapshots_enabled
        from services.release_info import release_version

        if snapshots_enabled():
            outcome = _submit_background_task({
                "task_type": "dashboard_snapshot_refresh",
                "snapshot_version": 1,
                "release_version": release_version(),
                "request_window": int(time.time() // 300),
            })
            logger.info("[Worker] dashboard snapshot bootstrap=%s", outcome)
    except Exception:
        logger.exception("[Worker] dashboard snapshot bootstrap failed")
    _retry_pending_dashboard_publication()


def main() -> int:
    from routes.shared import _security_cfg, init_db
    from services.shared_cache import process_role, redis_enabled, redis_ping

    role = process_role()
    if role not in {"worker", "all"}:
        logger.error("AI_FILTER_ROLE=%s cannot run worker_main", role)
        return 2

    initialize_runtime_database("worker", init_db, "worker_database_initialize")
    if not (_security_cfg().get("api_key") or "").strip():
        logger.error("Effective security.api_key is required before worker starts")
        return 2

    if production_startup.enabled():
        return production_startup.closed_worker_loop()

    if not redis_enabled() or not redis_ping():
        logger.error("Redis is required and must be reachable before worker starts")
        return 2

    from routes.webhook_routes import (
        start_webhook_dispatcher,
        stop_webhook_dispatcher,
        webhook_dispatcher_alive,
    )
    from routes.dashboard_routes import _ensure_quotes_refresher, stop_quotes_refresher
    from services.worker_health import (
        HEALTH_WRITE_INTERVAL_SECONDS,
        close_worker_health_resources,
        write_worker_health,
    )
    from services.quant_market_context import (
        quant_context_enabled,
        start_quant_market_context_poller,
        stop_quant_market_context_poller,
    )

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("[Worker] received signal %s; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    quotes_thread = _ensure_quotes_refresher()
    dispatcher_thread = start_webhook_dispatcher()
    quant_context_thread = start_quant_market_context_poller()

    # Bootstrap the fixed Glance cards in the single maintenance lane.  The
    # durable task survives a worker restart, coalesces with refresh-triggered
    # publication, and never makes the Web process compute on a public GET.
    try:
        _bootstrap_dashboard_publication()
    except AdmissionClosed:
        logger.info("[Worker] bootstrap held by admission fence")

    def _ensure_subsystems() -> dict[str, bool]:
        nonlocal quotes_thread, dispatcher_thread, quant_context_thread

        if quotes_thread is None or not quotes_thread.is_alive():
            stop_quotes_refresher(timeout=0.0)
            quotes_thread = _ensure_quotes_refresher()
        if dispatcher_thread is None or not dispatcher_thread.is_alive():
            dispatcher_thread = start_webhook_dispatcher()
        if quant_context_enabled() and (
            quant_context_thread is None or not quant_context_thread.is_alive()
        ):
            stop_quant_market_context_poller(timeout=0.0)
            quant_context_thread = start_quant_market_context_poller()
        subsystems = {
            "quotes": bool(quotes_thread is not None and quotes_thread.is_alive()),
            "dispatcher": bool(
                dispatcher_thread is not None and dispatcher_thread.is_alive()
            ),
        }
        # An operator can leave this optional integration disabled.  Once a URL
        # is configured, a dead poller is a real worker subsystem failure; a
        # remote outage itself is handled inside the live poller fail-neutrally.
        if quant_context_enabled():
            subsystems["quant_context"] = bool(
                quant_context_thread is not None and quant_context_thread.is_alive()
            )
        return subsystems
    logger.info("[Worker] background services started (role=%s)", role)
    try:
        write_worker_health(
            webhook_dispatcher_alive(),
            subsystems=_ensure_subsystems(),
            force_dependency_probe=True,
        )
    except Exception:
        logger.exception("[Worker] failed to write initial health snapshot")

    while not stop.wait(HEALTH_WRITE_INTERVAL_SECONDS):
        _retry_pending_dashboard_publication()
        try:
            write_worker_health(
                webhook_dispatcher_alive(), subsystems=_ensure_subsystems()
            )
        except Exception:
            # A stale/missing snapshot will make the container unhealthy, but
            # observability I/O must not crash task execution and cause a
            # restart storm by itself.
            logger.exception("[Worker] failed to write health snapshot")

    try:
        write_worker_health(webhook_dispatcher_alive(), running=False)
    except Exception:
        logger.exception("[Worker] failed to write shutdown health snapshot")
    stop_quotes_refresher(timeout=15.0)
    stop_quant_market_context_poller(timeout=12.0)
    drained = stop_webhook_dispatcher(timeout=60.0)
    if not drained:
        logger.error("[Worker] shutdown deadline reached; claimed tasks remain leased")
    close_worker_health_resources()
    logger.info("[Worker] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
