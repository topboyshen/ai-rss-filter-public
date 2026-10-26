"""
AI-Filter 共享状态与业务逻辑

存放跨 Blueprint 共享的工具函数、全局状态、后台任务逻辑。
注意：刷新、摘要、偏好提取相关操作已拆分到 services/refresh.py 和 services/digest.py。
"""

import hmac
import json
import os
import time
import threading
import logging
from utils.beijing_time import beijing_fromtimestamp
from flask import request

from core.config import config
from core.scorer import Scorer
from core.prompts import get_prompt_for_url as get_prompt_for_url
from core.prompts import resolve_prompt as resolve_prompt

from db.database import (
    DB,
    init_db as init_db,
    get_last_full_refresh_ts,
)
# These names are shared compatibility exports used by routes and workers.
from utils.helpers import (
    get_cached as get_cached,
    set_cached as set_cached,
    flush_stats as flush_stats,
    extract_score_from_title as extract_score_from_title,
    extract_tag_from_title as extract_tag_from_title,
    FeedEntry as FeedEntry,
    resolve_feed_url_to_cache_key as resolve_feed_url_to_cache_key,
    strategy_cache_key as strategy_cache_key,
    clean_title_for_compare as clean_title_for_compare,
)

logger = logging.getLogger(__name__)

# Scorer（全局单例）
scorer = Scorer()

# Refresh 并发控制
_refresh_state_lock = threading.Lock()
_refresh_running = False

# Quotes 短时缓存
_quotes_cache_lock = threading.Lock()
_quotes_refresh_lock = threading.Lock()
_quotes_cache: dict = {"html": None, "ts": 0.0}
QUOTES_CACHE_TTL_SEC = 300.0
# 拉取在常驻后台线程执行，不在请求路径上，超时可放宽
QUOTES_FETCH_TIMEOUT_SEC = 5.0

# Config 并发锁
cfg_lock = threading.Lock()

# Dashboard 模板路径
DASHBOARD_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates",
    "dashboard.html",
)


# ---------- Config helpers ----------

def _cfg():
    return config.cfg


def _security_cfg():
    return (_cfg().get("security", {}) or {})


def _allowed_cors_origin():
    allowed = _security_cfg().get("cors_allowed_origins", "*")
    if isinstance(allowed, str):
        allowed = [x.strip() for x in allowed.split(",") if x.strip()]
    if not allowed:
        return None
    if "*" in allowed:
        return "*"
    origin = request.headers.get("Origin", "")
    return origin if origin in allowed else None


def _blacklist_re():
    return config.blacklist_re


# ---------- API Key ----------

def credential_text(value):
    """Normalize credentials without coercing unexpected config types."""
    return value.strip() if isinstance(value, str) else ""


def constant_time_text_equal(left, right):
    """Compare arbitrary Unicode text in constant time without TypeError."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def check_api_key_header():
    sec = _security_cfg()
    expected = credential_text(sec.get("api_key"))
    if not expected:
        # Protected endpoints must fail closed when deployment is incomplete.
        return False
    header_key = credential_text(request.headers.get("X-API-Key"))
    return bool(header_key) and constant_time_text_equal(header_key, expected)


def check_api_key():
    if check_api_key_header():
        return True
    sec = _security_cfg()
    expected = credential_text(sec.get("api_key"))
    if not expected:
        return False
    if sec.get("allow_query_api_key", False):
        query_key = credential_text(request.args.get("key"))
        return bool(query_key) and constant_time_text_equal(query_key, expected)
    return False


# ---------- Refresh state ----------

def is_refresh_job_running() -> bool:
    with _refresh_state_lock:
        local_running = _refresh_running
    if local_running:
        return True
    try:
        from services.shared_cache import get_shared_json

        shared = get_shared_json("refresh-status")
        return bool(shared and shared.get("running") is True)
    except Exception:
        return False


def set_refresh_running(val: bool):
    global _refresh_running
    with _refresh_state_lock:
        _refresh_running = val
    try:
        from services.shared_cache import set_shared_json

        set_shared_json(
            "refresh-status",
            {"running": bool(val), "updated_at": time.time()},
            3600,
        )
    except Exception:
        logger.debug("Could not update shared refresh status", exc_info=True)


def try_acquire_refresh_lock() -> bool:
    """原子尝试获取 refresh 锁，返回是否成功。"""
    global _refresh_running
    with _refresh_state_lock:
        if _refresh_running:
            return False
        _refresh_running = True
    try:
        from services.shared_cache import set_shared_json

        set_shared_json(
            "refresh-status",
            {"running": True, "updated_at": time.time()},
            3600,
        )
    except Exception:
        logger.debug("Could not update shared refresh status", exc_info=True)
    return True


# ---------- Staleness ----------

def _staleness_info(cfg: dict) -> dict:
    """可观测性：全量拉源时间是否超出预期（用于 /status、Dashboard）。"""
    last_ts = get_last_full_refresh_ts()
    now = int(time.time())
    sec = int(cfg.get("staleness_warn_seconds") or 0)
    if sec <= 0:
        ct = int(cfg.get("cache_ttl", 600) or 600)
        sec = ct * 2
    age = (now - last_ts) if last_ts else None
    has_feeds = any(feed.get("enabled", True) for feed in cfg.get("feeds", []))
    stale = bool(
        has_feeds
        and (not last_ts or (age is not None and age > sec))
    )
    last_attempt = None
    last_ingestion_ts = 0
    try:
        raw_attempt = DB.get_app_meta("last_refresh_result")
        parsed_attempt = json.loads(raw_attempt) if raw_attempt else None
        if isinstance(parsed_attempt, dict):
            last_attempt = parsed_attempt
    except Exception:
        logger.warning("Could not read last refresh result", exc_info=True)
    try:
        raw_ingestion = DB.get_app_meta("last_ingestion")
        last_ingestion_ts = int(raw_ingestion) if raw_ingestion else 0
    except (TypeError, ValueError):
        logger.warning("Invalid last ingestion timestamp")
    except Exception:
        logger.warning("Could not read last ingestion timestamp", exc_info=True)
    last_attempt_status = str((last_attempt or {}).get("status") or "unknown")
    degraded = bool(has_feeds and last_attempt_status in {"partial", "failed"})
    return {
        "last_full_refresh_at": beijing_fromtimestamp(last_ts).isoformat() if last_ts else None,
        "seconds_since_full_refresh": age,
        "staleness_threshold_seconds": sec,
        "stale": stale,
        "degraded": degraded,
        "last_attempt": last_attempt,
        "last_ingestion_at": (
            beijing_fromtimestamp(last_ingestion_ts).isoformat()
            if last_ingestion_ts
            else None
        ),
    }
