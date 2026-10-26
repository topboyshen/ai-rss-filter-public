"""
AI-Filter 管理路由：刷新、统计、日志、健康检查、状态
"""
import json
import logging
import os
import time
from utils.beijing_time import beijing_now, beijing_fromtimestamp
from flask import Blueprint, Response, request
from services.release_info import release_version
from core.user_feedback import resolve_scoring_contract
from services.score_quality import score_quality_prometheus, score_quality_snapshot
from utils.helpers import feed_data_health

from routes.security import check_refresh_rate_limit
from routes.shared import (
    _cfg,
    check_api_key,
    cfg_lock,
    DB,
    flush_stats,
    is_refresh_job_running,
    _staleness_info,
)

logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__)


def _worker_queue_metrics() -> dict:
    """Read the bounded spool directly from the shared data volume."""
    try:
        from services.worker_health import collect_spool_metrics

        metrics = collect_spool_metrics()
    except Exception:
        logger.exception("Could not inspect worker spool")
        metrics = {
            "pending": 0,
            "inflight": 0,
            "dead_letter": 0,
            "invalid_files": 0,
            "oldest_task_age_seconds": 0.0,
            "task_max_age_seconds": 0,
            "scan_ok": False,
        }
    metrics["stale"] = bool(
        metrics.get("oldest_task_age_seconds", 0)
        > metrics.get("task_max_age_seconds", 0)
    )
    metrics["alert"] = bool(
        not metrics.get("scan_ok")
        or metrics.get("dead_letter", 0) > 0
        or metrics.get("invalid_files", 0) > 0
        or metrics["stale"]
    )
    return metrics


def _cold_filter_metrics() -> dict:
    """Read the one-worker API cold-path capacity guard."""
    try:
        from routes.feed_routes import cold_filter_capacity_metrics

        return cold_filter_capacity_metrics()
    except Exception:
        logger.exception("Could not inspect cold-filter capacity")
        return {"limit": 0, "inflight": 0, "rejections_total": 0}


_SCORE_FALLBACK_ALERT_RATIO = 0.50
_SCORE_FALLBACK_ALERT_MIN_SAMPLES = 20


def _score_fallback_metrics() -> dict:
    """Read persistent fallback health and observation-only semantic outcomes."""
    try:
        metrics = dict(DB.get_score_fallback_stats(hours=24))
    except Exception as exc:
        logger.warning("Could not read scoring fallback health: %s", type(exc).__name__)
        metrics = {
            "window_hours": 24,
            "fallback_local": 0,
            "model_scored": 0,
            "eligible": 0,
            "fallback_ratio": 0.0,
            "safe_neutral": 0,
            "safe_neutral_denominator": 0,
            "safe_neutral_ratio": 0.0,
            "available": False,
        }
    try:
        recent = dict(DB.get_score_fallback_stats(hours=2))
    except Exception as exc:
        logger.warning(
            "Could not read 2h safe-neutral observations: %s", type(exc).__name__
        )
        recent = {
            "safe_neutral": 0,
            "safe_neutral_denominator": 0,
            "safe_neutral_ratio": 0.0,
        }
    metrics["safe_neutral_2h"] = int(recent.get("safe_neutral") or 0)
    metrics["safe_neutral_denominator_2h"] = int(
        recent.get("safe_neutral_denominator") or 0
    )
    metrics["safe_neutral_ratio_2h"] = float(
        recent.get("safe_neutral_ratio") or 0.0
    )
    eligible = int(metrics.get("eligible") or 0)
    ratio = float(metrics.get("fallback_ratio") or 0.0)
    metrics.setdefault("available", True)
    metrics["alert_threshold"] = _SCORE_FALLBACK_ALERT_RATIO
    metrics["alert"] = bool(
        metrics["available"]
        and eligible >= _SCORE_FALLBACK_ALERT_MIN_SAMPLES
        and ratio >= _SCORE_FALLBACK_ALERT_RATIO
    )
    return metrics


def _quality_strategy_version(cfg):
    return str(resolve_scoring_contract(cfg, register_snapshot=False).get("scoring_strategy_version") or "")


@admin_bp.route("/score-quality")
def score_quality():
    """Authenticated diagnostic snapshot, separate from posterior quality gates."""
    if not check_api_key():
        return Response(json.dumps({"error": "Unauthorized"}), status=401, mimetype="application/json")
    version = _quality_strategy_version(_cfg())
    response = Response(json.dumps({
        "windows": [score_quality_snapshot(version, hours) for hours in (2, 24)]
    }, ensure_ascii=False), mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.route("/health")
def health():
    # Docker should probe the already-running application instead of importing
    # the full Flask/auth stack in a fresh Python process.  Keep the production
    # browser-auth fail-closed check here so the lighter external probe does not
    # weaken the readiness contract.
    if (os.environ.get("AI_FILTER_ROLE") or "").strip().casefold() == "web":
        from routes.browser_auth import browser_login_ready

        if not browser_login_ready():
            return "NOT READY", 503
    return "OK", 200


@admin_bp.route("/config/reload", methods=["POST"])
def reload_config():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    from core.config import config
    from routes.shared import scorer
    with cfg_lock:
        config.reload()
        scorer.reload_backends()
    return json.dumps({"status": "ok", "message": "配置已重新加载，LLM后端与运行态缓存已刷新"}, ensure_ascii=False)


@admin_bp.route("/stats")
def stats():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    flush_stats(DB.update_stats)
    row = DB.get_stats()
    if row is None:
        return json.dumps({"error": "no stats"}), 500
    (total_req, api_calls, cache_hits, sim_hits, blacklist_hits,
     fast_pass_hits, filtered, passed, updated_at) = row
    total_processed = filtered + passed
    savings_rate = 0.0
    if total_processed > 0:
        savings_rate = (cache_hits + sim_hits + blacklist_hits + fast_pass_hits) / total_processed * 100
    estimated_saved = (cache_hits + sim_hits + blacklist_hits + fast_pass_hits) * 0.0002

    dist_rows = DB.get_score_distribution(days=7)
    distribution = {}
    for r in dist_rows:
        distribution[r[0]] = {}
        for i in range(11):
            key = str(i) + "分"
            distribution[r[0]][key] = r[11-i]

    return Response(json.dumps({
        "total_requests": total_req,
        "api_calls_made": api_calls,
        "cache_hits": cache_hits,
        "similarity_hits": sim_hits,
        "blacklist_hits": blacklist_hits,
        "fast_pass_hits": fast_pass_hits,
        "filtered_out": filtered,
        "passed_through": passed,
        "savings_rate_percent": round(savings_rate, 2),
        "estimated_yuan_saved": round(estimated_saved, 4),
        "score_distribution": distribution,
        "config": {
            "thresholds": DB.get_feed_thresholds(_cfg(), ""),
            "pass_threshold": DB.get_feed_thresholds(_cfg(), "")["pass"],
            "notify_threshold": DB.get_feed_thresholds(_cfg(), "")["notify"],
            "similarity_threshold": _cfg()["threshold"]["similarity"],
            "telegram_enabled": _cfg()["telegram"]["enabled"]
        },
        "updated_at": beijing_fromtimestamp(updated_at).isoformat() if updated_at else None
    }, ensure_ascii=False, indent=2), mimetype="application/json")


@admin_bp.route("/stats/reset", methods=["POST"])
def reset_stats_endpoint():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    DB.reset_stats()
    return json.dumps({"status": "ok", "message": "统计数据已重置"}, ensure_ascii=False)


@admin_bp.route("/logs")
def logs():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    limit = request.args.get('limit', 50, type=int)
    rows = DB.get_request_logs(limit=limit)
    result = []
    for r in rows:
        result.append({
            "timestamp": beijing_fromtimestamp(r[0]).isoformat(),
            "url": r[1],
            "articles_total": r[2],
            "articles_passed": r[3],
            "cache_hits": r[4],
            "similarity_hits": r[5],
            "blacklist_hits": r[6],
            "fast_pass_hits": r[7],
            "api_calls": r[8],
            "duration_ms": r[9],
            "error": r[10]
        })
    return Response(json.dumps(result, ensure_ascii=False, indent=2), mimetype="application/json")


@admin_bp.route("/health/feeds")
def feed_health():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    rows = DB.get_feed_health()
    cfg = _cfg()
    result = []
    for r in rows:
        data_health = feed_data_health(r, cfg)
        total_checks = r[4] + r[5]
        success_rate = round(r[4] / total_checks * 100, 1) if total_checks > 0 else 0
        pass_rate = round(r[7] / r[6] * 100, 1) if r[6] > 0 else 0
        result.append({
            "url": r[0],
            "name": r[1],
            "last_check": beijing_fromtimestamp(r[2]).isoformat() if r[2] else None,
            "last_response_time_ms": r[3],
            "success_rate_percent": success_rate,
            "total_checks": total_checks,
            "articles_total": r[6],
            "articles_passed": r[7],
            "pass_rate_percent": pass_rate,
            "avg_score": round(r[8], 2) if r[8] else None,
            "last_error": r[9],
            "consecutive_empty_checks": data_health["consecutive_empty_checks"],
            "last_nonempty_at": (
                beijing_fromtimestamp(data_health["last_nonempty_at"]).isoformat()
                if data_health["last_nonempty_at"] else None
            ),
            "data_state": data_health["data_state"],
            "data_degraded": data_health["data_degraded"],
        })
    return Response(json.dumps(result, ensure_ascii=False, indent=2), mimetype="application/json")


@admin_bp.route("/refresh", methods=["POST"])
def refresh():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    ok, msg = check_refresh_rate_limit()
    if not ok:
        return Response(json.dumps({"error": msg}, ensure_ascii=False), status=429, mimetype="application/json")

    cfg = _cfg()

    feeds = cfg.get("feeds", [])
    enabled_feeds = [feed for feed in feeds if feed.get("enabled", True)]
    if not enabled_feeds:
        return json.dumps({"status": "ok", "message": "config.yaml 中没有启用的 feeds", "results": []}, ensure_ascii=False)

    from routes.webhook_routes import _submit_background_task

    outcome = _submit_background_task({
        "task_type": "refresh",
        "request_window": int(time.time() // 60),
    })
    if outcome == "full":
        response = Response(
            json.dumps({"error": "background_queue_full"}, ensure_ascii=False),
            status=503,
            mimetype="application/json",
        )
        response.headers["Retry-After"] = "30"
        return response
    return json.dumps({
        "status": "queued" if outcome == "stored" else "duplicate",
        "message": "刷新任务已持久化，等待专用 Worker 执行",
        "feeds_count": len(enabled_feeds)
    }, ensure_ascii=False)


@admin_bp.route("/status")
def status():
    """Authenticated, read-only system status overview."""
    if not check_api_key():
        return Response(
            json.dumps({"error": "Unauthorized"}),
            status=401,
            mimetype="application/json",
        )
    stats_row = DB.get_stats()
    result = {
        "service": "AI-Filter",
        "version": release_version(),
        "timestamp": beijing_now().isoformat(),
        "config": {
            "thresholds": DB.get_feed_thresholds(_cfg(), ""),
            "pass_threshold": DB.get_feed_thresholds(_cfg(), "")["pass"],
            "notify_threshold": DB.get_feed_thresholds(_cfg(), "")["notify"],
            "similarity_threshold": _cfg()["threshold"]["similarity"],
            "sources_count": len(_cfg().get("feeds", [])),
            "generate_summary": _cfg().get("generate_summary", False)
        },
        "stats": {},
        "feeds": []
    }

    if stats_row:
        total_req, api_calls, cache_hits, sim_hits, blacklist_hits, fast_pass_hits, filtered, passed, updated_at = stats_row
        total = filtered + passed
        result["stats"] = {
            "total_requests": total_req,
            "api_calls": api_calls,
            "cache_hits": cache_hits,
            "similarity_hits": sim_hits,
            "blacklist_hits": blacklist_hits,
            "fast_pass_hits": fast_pass_hits,
            "passed": passed,
            "filtered": filtered,
            "pass_rate": round(passed/total*100, 1) if total else 0,
            "savings_rate": round((cache_hits + sim_hits + blacklist_hits + fast_pass_hits)/total*100, 1) if total else 0,
            "last_updated": beijing_fromtimestamp(updated_at).isoformat() if updated_at else None
        }

    c = _cfg()
    si = _staleness_info(c)
    result["refresh"] = {
        "in_progress": is_refresh_job_running(),
        "last_full_refresh_at": si["last_full_refresh_at"],
        "seconds_since_full_refresh": si["seconds_since_full_refresh"],
        "staleness_threshold_seconds": si["staleness_threshold_seconds"],
        "stale": si["stale"],
        "degraded": si["degraded"],
        "last_attempt": si["last_attempt"],
        "last_ingestion_at": si["last_ingestion_at"],
    }
    result["worker_queue"] = _worker_queue_metrics()
    result["scoring_health"] = _score_fallback_metrics()

    # 极速路由器模式：从 news_cache 实时统计 feed 健康状态
    try:
        from routes.dashboard_routes import _get_health_from_news_cache
        health_rows = _get_health_from_news_cache()
    except Exception:
        health_rows = []
    for r in health_rows[:10]:
        data_health = feed_data_health(r, c)
        total_checks = r[4] + r[5]
        success_rate = round(r[4] / total_checks * 100, 1) if total_checks else 0
        result["feeds"].append({
            "name": r[1] or r[0],
            "success_rate": success_rate,
            "total_checks": total_checks,
            "articles_total": r[6],
            "articles_passed": r[7],
            "avg_score": round(r[8], 2) if r[8] else None,
            "last_check": beijing_fromtimestamp(r[2]).isoformat() if r[2] else None,
            "last_error": r[9],
            "consecutive_empty_checks": data_health["consecutive_empty_checks"],
            "last_nonempty_at": (
                beijing_fromtimestamp(data_health["last_nonempty_at"]).isoformat()
                if data_health["last_nonempty_at"] else None
            ),
            "data_state": data_health["data_state"],
            "data_degraded": data_health["data_degraded"],
        })

    response = Response(
        json.dumps(result, ensure_ascii=False, indent=2),
        mimetype="application/json",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.route("/readiness")
def readiness():
    """编排/探活用：strict 模式下陈旧或最近刷新降级时返回 503。"""
    c = _cfg()
    si = _staleness_info(c)
    payload = {
        "ok": True,
        "timestamp": beijing_now().isoformat(),
        "refresh_in_progress": is_refresh_job_running(),
        "last_full_refresh_at": si["last_full_refresh_at"],
        "seconds_since_full_refresh": si["seconds_since_full_refresh"],
        "staleness_threshold_seconds": si["staleness_threshold_seconds"],
        "stale": si["stale"],
        "degraded": si["degraded"],
        "last_attempt": si["last_attempt"],
        "last_ingestion_at": si["last_ingestion_at"],
    }
    if (si["stale"] or si["degraded"]) and request.args.get(
        "strict", ""
    ).lower() in ("1", "true", "yes"):
        payload["ok"] = False
        payload["reason"] = "stale" if si["stale"] else "degraded"
        return (
            json.dumps(payload, ensure_ascii=False, indent=2),
            503,
            {"Content-Type": "application/json; charset=utf-8"},
        )
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2), mimetype="application/json"
    )


@admin_bp.route("/metrics")
def metrics():
    """Authenticated, read-only Prometheus metrics endpoint."""
    if not check_api_key():
        return Response(
            json.dumps({"error": "Unauthorized"}),
            status=401,
            mimetype="application/json",
        )
    stats_row = DB.get_stats()

    total_req = stats_row[0] if stats_row else 0
    api_calls = stats_row[1] if stats_row else 0
    cache_hits = stats_row[2] if stats_row else 0
    sim_hits = stats_row[3] if stats_row else 0
    blacklist_hits = stats_row[4] if stats_row else 0
    fast_pass_hits = stats_row[5] if stats_row else 0
    filtered = stats_row[6] if stats_row else 0
    passed = stats_row[7] if stats_row else 0
    total = filtered + passed

    c = _cfg()
    si = _staleness_info(c)
    worker_queue = _worker_queue_metrics()
    cold_filter = _cold_filter_metrics()
    score_fallback = _score_fallback_metrics()
    empty_degraded_urls = set()
    for row in DB.get_feed_health():
        feed_cfg = DB.get_feed_config(c, row[0])
        if not feed_cfg or not feed_cfg.get("enabled", True):
            continue
        if feed_data_health(row, c)["data_degraded"]:
            empty_degraded_urls.add(str(feed_cfg.get("url") or row[0]))
    empty_degraded_sources = len(empty_degraded_urls)
    last_attempt = si.get("last_attempt") or {}
    last_attempt_status = str(last_attempt.get("status") or "unknown")
    seconds_since_refresh = si.get("seconds_since_full_refresh")
    if seconds_since_refresh is None:
        seconds_since_refresh = -1
    try:
        last_succeeded_sources = int(last_attempt.get("succeeded_sources") or 0)
        last_failed_sources = int(last_attempt.get("failed_sources") or 0)
    except (TypeError, ValueError):
        last_succeeded_sources = 0
        last_failed_sources = 0

    lines = [
        "# HELP ai_filter_requests_total Total number of filter requests",
        "# TYPE ai_filter_requests_total counter",
        f"ai_filter_requests_total {total_req}",
        "# HELP ai_filter_api_calls_total Outbound LLM SDK attempts across the application",
        "# TYPE ai_filter_api_calls_total counter",
        f"ai_filter_api_calls_total {api_calls}",
        "# HELP ai_filter_cache_hits_total Total cache hits",
        "# TYPE ai_filter_cache_hits_total counter",
        f"ai_filter_cache_hits_total {cache_hits}",
        "# HELP ai_filter_similarity_hits_total Total similarity dedup hits",
        "# TYPE ai_filter_similarity_hits_total counter",
        f"ai_filter_similarity_hits_total {sim_hits}",
        "# HELP ai_filter_fast_pass_hits_total Total fast-pass hits",
        "# TYPE ai_filter_fast_pass_hits_total counter",
        f"ai_filter_fast_pass_hits_total {fast_pass_hits}",
        "# HELP ai_filter_articles_filtered_total Articles filtered out",
        "# TYPE ai_filter_articles_filtered_total counter",
        f"ai_filter_articles_filtered_total {filtered}",
        "# HELP ai_filter_articles_passed_total Articles passed through",
        "# TYPE ai_filter_articles_passed_total counter",
        f"ai_filter_articles_passed_total {passed}",
        "# HELP ai_filter_savings_rate Current savings rate (0-100)",
        "# TYPE ai_filter_savings_rate gauge",
        f"ai_filter_savings_rate {round((cache_hits + sim_hits + blacklist_hits + fast_pass_hits) / total * 100, 1) if total else 0}",
        "# HELP ai_filter_refresh_stale Is data stale (1=stale, 0=fresh)",
        "# TYPE ai_filter_refresh_stale gauge",
        f"ai_filter_refresh_stale {1 if si.get('stale') else 0}",
        "# HELP ai_filter_refresh_degraded Latest refresh attempt was partial or failed",
        "# TYPE ai_filter_refresh_degraded gauge",
        f"ai_filter_refresh_degraded {1 if si.get('degraded') else 0}",
        "# HELP ai_filter_feed_empty_degraded_sources Sources degraded after consecutive valid-but-empty polls",
        "# TYPE ai_filter_feed_empty_degraded_sources gauge",
        f"ai_filter_feed_empty_degraded_sources {empty_degraded_sources}",
        "# HELP ai_filter_seconds_since_refresh Seconds since last full refresh (-1=never)",
        "# TYPE ai_filter_seconds_since_refresh gauge",
        f"ai_filter_seconds_since_refresh {seconds_since_refresh}",
        "# HELP ai_filter_refresh_in_progress Is refresh running (1=yes)",
        "# TYPE ai_filter_refresh_in_progress gauge",
        f"ai_filter_refresh_in_progress {1 if is_refresh_job_running() else 0}",
        "# HELP ai_filter_refresh_last_attempt_status Last refresh attempt status",
        "# TYPE ai_filter_refresh_last_attempt_status gauge",
        *[
            f'ai_filter_refresh_last_attempt_status{{status="{status}"}} '
            f'{1 if last_attempt_status == status else 0}'
            for status in ("success", "partial", "failed", "noop", "unknown")
        ],
        "# HELP ai_filter_refresh_sources_succeeded Sources that succeeded in the last attempt",
        "# TYPE ai_filter_refresh_sources_succeeded gauge",
        f"ai_filter_refresh_sources_succeeded {last_succeeded_sources}",
        "# HELP ai_filter_refresh_sources_failed Sources that failed in the last attempt",
        "# TYPE ai_filter_refresh_sources_failed gauge",
        f"ai_filter_refresh_sources_failed {last_failed_sources}",
        "# HELP ai_filter_score_fallback_local_24h Scores degraded to local fallback in 24 hours",
        "# TYPE ai_filter_score_fallback_local_24h gauge",
        f"ai_filter_score_fallback_local_24h {int(score_fallback.get('fallback_local') or 0)}",
        "# HELP ai_filter_score_model_scored_24h Scores completed by an LLM in 24 hours",
        "# TYPE ai_filter_score_model_scored_24h gauge",
        f"ai_filter_score_model_scored_24h {int(score_fallback.get('model_scored') or 0)}",
        "# HELP ai_filter_score_fallback_ratio_24h Local fallback share among model-eligible scores",
        "# TYPE ai_filter_score_fallback_ratio_24h gauge",
        f"ai_filter_score_fallback_ratio_24h {float(score_fallback.get('fallback_ratio') or 0.0):.6f}",
        "# HELP ai_filter_score_safe_neutral_count_2h Non-substantive safe-neutral scoring outcomes in 2 hours",
        "# TYPE ai_filter_score_safe_neutral_count_2h gauge",
        f"ai_filter_score_safe_neutral_count_2h {int(score_fallback.get('safe_neutral_2h') or 0)}",
        "# HELP ai_filter_score_safe_neutral_denominator_2h Direct model, local fallback, and safe-neutral outcomes in 2 hours",
        "# TYPE ai_filter_score_safe_neutral_denominator_2h gauge",
        f"ai_filter_score_safe_neutral_denominator_2h {int(score_fallback.get('safe_neutral_denominator_2h') or 0)}",
        "# HELP ai_filter_score_safe_neutral_ratio_2h Safe-neutral share of observed model-resolution outcomes in 2 hours",
        "# TYPE ai_filter_score_safe_neutral_ratio_2h gauge",
        f"ai_filter_score_safe_neutral_ratio_2h {float(score_fallback.get('safe_neutral_ratio_2h') or 0.0):.6f}",
        "# HELP ai_filter_score_safe_neutral_count_24h Non-substantive safe-neutral scoring outcomes in 24 hours",
        "# TYPE ai_filter_score_safe_neutral_count_24h gauge",
        f"ai_filter_score_safe_neutral_count_24h {int(score_fallback.get('safe_neutral') or 0)}",
        "# HELP ai_filter_score_safe_neutral_denominator_24h Direct model, local fallback, and safe-neutral outcomes in 24 hours",
        "# TYPE ai_filter_score_safe_neutral_denominator_24h gauge",
        f"ai_filter_score_safe_neutral_denominator_24h {int(score_fallback.get('safe_neutral_denominator') or 0)}",
        "# HELP ai_filter_score_safe_neutral_ratio_24h Safe-neutral share of observed model-resolution outcomes in 24 hours",
        "# TYPE ai_filter_score_safe_neutral_ratio_24h gauge",
        f"ai_filter_score_safe_neutral_ratio_24h {float(score_fallback.get('safe_neutral_ratio') or 0.0):.6f}",
        "# HELP ai_filter_score_fallback_alert Local fallback ratio crossed the alert threshold",
        "# TYPE ai_filter_score_fallback_alert gauge",
        f"ai_filter_score_fallback_alert {1 if score_fallback.get('alert') else 0}",
        "# HELP ai_filter_worker_queue_pending Durable tasks waiting to run",
        "# TYPE ai_filter_worker_queue_pending gauge",
        f"ai_filter_worker_queue_pending {worker_queue['pending']}",
        "# HELP ai_filter_worker_queue_inflight Durable tasks currently claimed",
        "# TYPE ai_filter_worker_queue_inflight gauge",
        f"ai_filter_worker_queue_inflight {worker_queue['inflight']}",
        "# HELP ai_filter_worker_queue_dead_letter Tasks requiring operator remediation",
        "# TYPE ai_filter_worker_queue_dead_letter gauge",
        f"ai_filter_worker_queue_dead_letter {worker_queue['dead_letter']}",
        "# HELP ai_filter_worker_queue_invalid_files Malformed active spool files",
        "# TYPE ai_filter_worker_queue_invalid_files gauge",
        f"ai_filter_worker_queue_invalid_files {worker_queue.get('invalid_files', 0)}",
        "# HELP ai_filter_worker_queue_oldest_age_seconds Age of the oldest active durable task",
        "# TYPE ai_filter_worker_queue_oldest_age_seconds gauge",
        f"ai_filter_worker_queue_oldest_age_seconds {worker_queue['oldest_task_age_seconds']}",
        "# HELP ai_filter_worker_queue_stale Oldest task exceeded the configured age limit",
        "# TYPE ai_filter_worker_queue_stale gauge",
        f"ai_filter_worker_queue_stale {1 if worker_queue['stale'] else 0}",
        "# HELP ai_filter_worker_queue_alert Queue needs attention due to scan failure, DLQ, or stale work",
        "# TYPE ai_filter_worker_queue_alert gauge",
        f"ai_filter_worker_queue_alert {1 if worker_queue['alert'] else 0}",
        "# HELP ai_filter_cold_filter_limit Maximum concurrent RSS/LLM cold paths in the API worker",
        "# TYPE ai_filter_cold_filter_limit gauge",
        f"ai_filter_cold_filter_limit {cold_filter['limit']}",
        "# HELP ai_filter_cold_filter_inflight Current RSS/LLM cold paths in the API worker",
        "# TYPE ai_filter_cold_filter_inflight gauge",
        f"ai_filter_cold_filter_inflight {cold_filter['inflight']}",
        "# HELP ai_filter_cold_filter_rejections_total Cold requests rejected to preserve API thread headroom",
        "# TYPE ai_filter_cold_filter_rejections_total counter",
        f"ai_filter_cold_filter_rejections_total {cold_filter['rejections_total']}",
    ]

    response = Response(
        "\n".join(lines + score_quality_prometheus(_quality_strategy_version(c))) + "\n", mimetype="text/plain; version=0.0.4"
    )
    response.headers["Cache-Control"] = "no-store"
    return response
