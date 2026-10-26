"""Small Redis boundary for shared cache, rate limits, and process roles."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

SHARED_CACHE_PREFIX = os.environ.get(
    "AI_FILTER_SHARED_CACHE_PREFIX", "ai-filter:cache:"
)
_REDIS_URL = (
    os.environ.get("REDIS_URL")
    or os.environ.get("AI_FILTER_REDIS_URL")
    or ""
).strip()


def redis_enabled() -> bool:
    return bool(_REDIS_URL)


def redis_client():
    if not _REDIS_URL:
        return None
    try:
        import redis
        from redis.maint_notifications import MaintNotificationsConfig
    except ImportError:
        logger.error("[SharedCache] redis package missing")
        return None
    return redis.Redis.from_url(
        _REDIS_URL,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=3,
        health_check_interval=30,
        maint_notifications_config=MaintNotificationsConfig(enabled=False),
    )


def redis_ping() -> bool:
    client = redis_client()
    if client is None:
        return not redis_enabled()
    try:
        return bool(client.ping())
    except Exception:
        logger.exception("[SharedCache] Redis ping failed")
        return False
    finally:
        client.close()


def set_shared_json(name: str, value: dict, ttl_seconds: int) -> bool:
    client = redis_client()
    if client is None:
        return False
    try:
        client.setex(
            f"{SHARED_CACHE_PREFIX}{name}",
            max(1, int(ttl_seconds)),
            json.dumps(value, ensure_ascii=False),
        )
        return True
    except Exception:
        logger.exception("[SharedCache] write failed for %s", name)
        return False
    finally:
        client.close()


def get_shared_json(name: str) -> dict | None:
    client = redis_client()
    if client is None:
        return None
    try:
        raw = client.get(f"{SHARED_CACHE_PREFIX}{name}")
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else None
    except Exception:
        logger.exception("[SharedCache] read failed for %s", name)
        return None
    finally:
        client.close()


def process_role() -> str:
    """Return the supported runtime role: web, worker, or local all-in-one."""
    role = (os.environ.get("AI_FILTER_ROLE") or "all").strip().lower()
    return role if role in {"web", "worker", "all"} else "all"
