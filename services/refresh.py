"""AI-Filter 拉源服务。

从配置读取已启用的 feeds，逐源执行评分与筛选，写入 RSS 缓存，
并记录刷新健康状态。稳定事件、映射与相似关系由独立的事件投影链维护。
"""

import time
import logging
import fcntl
import os
import uuid
from contextlib import contextmanager

from core.prompts import resolve_prompt
from core.metrics import get_metrics
from core.scorer import capture_llm_calls
from core.processor import process_feed, process_entry, build_rss_xml
from services.natural_scoring import resume_continuations
from services.news_processing_trace import processing_origin
from services.admission_fence import guarded
from db.database import (
    DB,
    db_lock,
    get_connection,
    cleanup_old_entries,
)
from utils.helpers import (
    set_cached,
    flush_stats,
    extract_score_from_title,
    strategy_cache_key,
)

from routes.shared import (
    _cfg,
    set_refresh_running,
    try_acquire_refresh_lock,
)

from core.user_feedback import resolve_scoring_contract

logger = logging.getLogger(__name__)

REFRESH_BUSY = "refresh_busy"


@contextmanager
def refresh_execution_lock():
    """Try to hold the cross-process refresh writer lock without waiting.

    The host enrichment job and the worker see the same bind-mounted inode.
    Callers that only persist replaceable snapshots can therefore defer their
    SQLite write while a long event-core transaction owns the writer lane.
    """
    execution_lock = None
    try:
        lock_path = os.environ.get(
            "AI_FILTER_REFRESH_LOCK", _runtime_lock_path(".refresh-execution.lock")
        )
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        execution_lock = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(
                execution_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if execution_lock is not None:
            try:
                execution_lock.close()
            except OSError:
                pass


# ---------- Feed refresh ----------

def _incomplete_feed_reason(stats):
    missing = int(stats.get("source_unrecognized", 0))
    deferred = int(stats.get("score_deferred", 0))
    terminal = int(stats.get("score_terminal_unscored", 0))
    errors = int(stats.get("entry_errors", 0))
    if missing or deferred or terminal or errors:
        return (f"采集或评分未完整完成: source_unrecognized={missing}, "
                f"score_deferred={deferred}, score_terminal_unscored={terminal}, entry_errors={errors}")
    return None


def _refresh_one_feed(feed, cfg, name):
    feed_start = time.time()
    url = feed["url"]
    prompt = resolve_prompt(url, cfg)[1]
    passed, stats, error, _ = process_feed(
        url, prompt, name, cfg, check_output_cache=False, persist_deferred=True
    )
    incomplete = _incomplete_feed_reason(stats)
    if passed and not error and not incomplete:
        passed.sort(key=lambda entry: extract_score_from_title(entry.title), reverse=True)
        max_items = cfg.get("output", {}).get("max_items", 0)
        if max_items > 0:
            passed = passed[:max_items]
        xml = build_rss_xml(
            passed, "AI 智能过滤: " + name, url,
            "http://localhost:5000/filter?url=" + url, cfg,
        )
        set_cached(strategy_cache_key(url, cfg), xml)
    logger.info(
        "[Refresh] %s done in %.1fs | total=%d passed=%d api=%d error=%s",
        name, time.time() - feed_start, stats["total"], stats["passed"],
        stats["api_calls"], error or incomplete or "None",
    )
    return stats, error, incomplete


@guarded("execute_feed_refresh", kind="inherit")
def _execute_feed_refresh():
    """处理全部启用源并返回可持久化的刷新结果。

    完整抓取并完成评分、但没有文章通过筛选的源仍算成功。未识别来源行或
    待评分条目归入 ``partial_sources``，保留可用内容但不写完整结果缓存。
    ``succeeded_sources``、``partial_sources``、``failed_sources`` 分别计完整、
    部分完成、失败的源，三者之和为启用源总数；未识别行与待评分条目另行汇总。
    ``last_full_refresh`` 仅在全部启用源完整完成时更新；
    存在可用内容时返回 ``partial``，全部拉取/解析失败才返回 ``failed``。
    """
    get_metrics()
    cfg = resolve_scoring_contract(_cfg(), register_snapshot=True)
    feeds = cfg.get("feeds", [])
    enabled_feeds = [feed for feed in feeds if feed.get("enabled", True)]
    started_at = int(time.time())
    refresh_id = uuid.uuid4().hex
    succeeded_sources = partial_sources = source_unrecognized = score_deferred = 0
    score_terminal_unscored = 0
    failures: list[dict[str, str]] = []
    logger.info("[Refresh] 后台刷新开始，共 %d 个启用 feeds", len(enabled_feeds))
    logger.info("[Refresh] refresh_id=%s", refresh_id)
    try:
        with capture_llm_calls() as continuation_calls:
            with processing_origin("natural_continuation", refresh_id):
                continuations = resume_continuations(cfg, run_entry=process_entry)
        continuations["api_calls"] = continuation_calls.total
        for feed in enabled_feeds:
            name = str(feed.get("name") or feed.get("url") or "invalid-feed")
            try:
                with processing_origin("natural_feed", refresh_id):
                    stats, error, incomplete = _refresh_one_feed(feed, cfg, name)
                source_unrecognized += int(stats.get("source_unrecognized", 0))
                score_deferred += int(stats.get("score_deferred", 0))
                score_terminal_unscored += int(stats.get("score_terminal_unscored", 0))
                if incomplete and not error:
                    partial_sources += 1
                if error or incomplete:
                    failures.append({"source": str(name)[:200], "error": str(error or incomplete)[:500]})
                else:
                    succeeded_sources += 1
            except Exception as exc:
                failures.append(
                    {
                        "source": str(name)[:200],
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
                logger.exception("[Refresh] %s failed", name)

        try:
            flush_stats(DB.update_stats)
        except Exception:
            logger.exception("[Refresh] Stats flush failed")

        enabled_sources = len(enabled_feeds)
        failed_sources = enabled_sources - succeeded_sources - partial_sources
        if enabled_sources == 0:
            status = "noop"
        elif succeeded_sources == enabled_sources:
            status = "success"
        elif succeeded_sources > 0 or partial_sources > 0:
            status = "partial"
        else:
            status = "failed"
        if status == "success" and (
            continuations["deferred"] or continuations["errors"]
            or continuations["terminal_unscored"]
            or continuations.get("remaining", {}).get("active", 0)
        ):
            status = "partial"
        finished_at = int(time.time())
        result = {
            "version": 1,
            "refresh_id": refresh_id,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "enabled_sources": enabled_sources,
            "succeeded_sources": succeeded_sources,
            "failed_sources": failed_sources,
            "partial_sources": partial_sources,
            "source_unrecognized_entries": source_unrecognized,
            "score_deferred_entries": score_deferred,
            "score_terminal_unscored_entries": score_terminal_unscored,
            "failures": failures,
            "score_continuations": continuations,
        }
        DB.record_refresh_attempt(
            result,
            successful_at=finished_at if status == "success" else None,
        )

        if succeeded_sources > 0:
            # 压缩历史低分噪音，防止 SQLite 表膨胀
            try:
                with db_lock:
                    with get_connection() as conn:
                        cleanup_old_entries(conn, cfg)
            except Exception:
                logger.exception("[Refresh] Cleanup failed")

        if status == "partial":
            logger.warning(
                "[Refresh] partial: %d/%d sources succeeded; failed=%s",
                succeeded_sources,
                enabled_sources,
                ", ".join(item["source"] for item in failures),
            )
        elif status == "failed":
            logger.error("[Refresh] failed: no enabled source completed successfully")
        return result
    finally:
        logger.info("[Refresh] 全部完成")


@guarded("refresh_command", kind="inherit")
def run_refresh_command() -> bool | str:
    """Execute one refresh inside the dedicated worker process."""
    if not try_acquire_refresh_lock():
        logger.info("[Refresh] skipped: another refresh is running")
        return REFRESH_BUSY
    try:
        with refresh_execution_lock() as acquired:
            if not acquired:
                logger.info("[Refresh] another worker owns the execution lock")
                return REFRESH_BUSY
            result = _execute_feed_refresh()
            return result.get("status") in {"success", "partial", "noop"}
    finally:
        set_refresh_running(False)


def _runtime_lock_path(name: str) -> str:
    data_dir = os.path.dirname(
        os.environ.get("AI_FILTER_DB_PATH", "/app/data/cache.db")
    )
    return os.path.join(data_dir or "/tmp", name)
