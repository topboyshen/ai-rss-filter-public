import hashlib
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from core.processor import build_rss_xml
from utils.helpers import FeedEntry, plain_text_from_markup, safe_http_url

logger = logging.getLogger(__name__)

DEFAULT_FEED_TITLE = "金十数据"
DEFAULT_FEED_URL = "jin10://rss"
_JIN10_CACHE_LOCK = threading.Lock()
_JIN10_FETCH_LOCK = threading.Lock()
_JIN10_CACHE: dict[str, Any] = {"source_url": "", "ts": 0.0, "items": []}
_JIN10_INDEX_LINK_RE = re.compile(r"jin10:index:([0-9]{20})\Z")


def _source_url() -> str:
    return os.getenv("JIN10_SOURCE_URL", "").strip()


def _feed_title() -> str:
    return os.getenv("JIN10_FEED_TITLE", DEFAULT_FEED_TITLE).strip() or DEFAULT_FEED_TITLE


def _feed_url() -> str:
    return os.getenv("JIN10_FEED_URL", DEFAULT_FEED_URL).strip() or DEFAULT_FEED_URL


def _items_from_json_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    # RSSHub JSON Feed 常见结构：{"items": [{"title", "content_html", "url", ...}]}
    for key in ("data", "items", "list", "news"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _text_of(element: ET.Element, child_name: str) -> str:
    child = element.find(child_name)
    return (child.text or "").strip() if child is not None else ""


def _items_from_rss_xml(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("金十 RSS XML 解析失败: %s", exc)
        return []

    items: list[dict[str, Any]] = []
    for item in root.findall(".//channel/item"):
        title = _text_of(item, "title")
        link = _text_of(item, "link")
        guid = _text_of(item, "guid")
        description = _text_of(item, "description")
        pub_date = _text_of(item, "pubDate")
        if not title and not description:
            continue
        items.append(
            {
                "id": guid or link or hashlib.sha256((title + description).encode("utf-8")).hexdigest()[:24],
                "title": title or description,
                "url": link or guid,
                "content": description or title,
                "pubDate": pub_date,
            }
        )
    return items


def _fetch_jin10_uncached(url: str) -> list[dict[str, Any]]:
    with httpx.Client(timeout=10.0, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        text = response.text

    looks_like_xml = "xml" in content_type or text.lstrip().startswith("<?xml") or text.lstrip().startswith("<rss")
    if looks_like_xml:
        return _items_from_rss_xml(text)

    try:
        payload = response.json()
    except ValueError:
        logger.warning("金十源既不是 RSS XML 也不是 JSON: content-type=%s", content_type)
        return []

    items = _items_from_json_payload(payload)
    if items:
        return items
    logger.warning("金十源返回 JSON 结构无法识别: %s", type(payload).__name__)
    return []


def _jin10_cache_ttl() -> int:
    try:
        value = int(os.getenv("JIN10_CACHE_TTL_SECONDS", "30"))
    except (TypeError, ValueError):
        value = 30
    return max(5, min(300, value))


def fetch_jin10_latest(source_url: str | None = None) -> list[dict[str, Any]]:
    """Fetch Jin10 with bounded shared caching and stale-on-error fallback."""
    url = (source_url if source_url is not None else _source_url()).strip()
    if not url:
        logger.warning("JIN10_SOURCE_URL 未配置，返回空 RSS")
        return []

    now = time.time()
    ttl = _jin10_cache_ttl()
    with _JIN10_CACHE_LOCK:
        if (
            _JIN10_CACHE["source_url"] == url
            and now - float(_JIN10_CACHE["ts"]) < ttl
        ):
            return [dict(item) for item in _JIN10_CACHE["items"]]

    # Collapse simultaneous misses into one upstream request. Other callers
    # return stale/empty immediately instead of occupying gthreads on this lock.
    if not _JIN10_FETCH_LOCK.acquire(blocking=False):
        with _JIN10_CACHE_LOCK:
            return (
                [dict(item) for item in _JIN10_CACHE["items"]]
                if _JIN10_CACHE["source_url"] == url
                else []
            )
    try:
        now = time.time()
        with _JIN10_CACHE_LOCK:
            if (
                _JIN10_CACHE["source_url"] == url
                and now - float(_JIN10_CACHE["ts"]) < ttl
            ):
                return [dict(item) for item in _JIN10_CACHE["items"]]
            stale = (
                [dict(item) for item in _JIN10_CACHE["items"]]
                if _JIN10_CACHE["source_url"] == url
                else []
            )
        try:
            items = _fetch_jin10_uncached(url)
        except Exception:
            # Negative-cache an outage as well, so a public request burst cannot
            # turn one upstream failure into dozens of 10-second connections.
            with _JIN10_CACHE_LOCK:
                _JIN10_CACHE.update(source_url=url, ts=now, items=stale)
            if stale:
                logger.warning("金十源拉取失败，返回缓存数据", exc_info=True)
                return stale
            raise
        with _JIN10_CACHE_LOCK:
            _JIN10_CACHE.update(
                source_url=url,
                ts=time.time(),
                items=[dict(item) for item in items],
            )
        return [dict(item) for item in items]
    finally:
        _JIN10_FETCH_LOCK.release()


def stable_item_id(item: dict[str, Any]) -> str:
    for key in ("id", "guid", "news_id", "time", "datetime", "timestamp", "date_published", "pubDate"):
        value = item.get(key)
        if value:
            return str(value)
    identity = "|".join(
        str(item.get(key) or "")
        for key in ("title", "content", "content_html", "summary", "description", "text", "url", "link")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _normalized_item_link(item: dict[str, Any], item_id: str) -> str:
    """Return a safe public URL for a Jin10 item.

    RSSHub's Jin10 route currently exposes stable internal identifiers such as
    ``jin10:index:20260722060338499800`` as the item link.  They are not
    browser URLs, but their numeric suffix is also the public Jin10 detail ID.
    Only that exact, fixed-width form is translated; arbitrary schemes and
    credential-bearing URLs remain rejected.
    """
    candidates = [
        str(item.get(key) or "").strip()
        for key in ("url", "link", "external_url")
    ]
    # Prefer the stable Jin10 identity over any incidental/shared upstream URL
    # so the generated RSS GUID cannot drift between refreshes.
    for candidate in (item_id, *candidates):
        match = _JIN10_INDEX_LINK_RE.fullmatch(candidate)
        if match:
            return f"https://flash.jin10.com/detail/{match.group(1)}"

    for candidate in candidates:
        public_url = safe_http_url(candidate, fallback="")
        if public_url:
            return public_url
    return ""


def _parse_timestamp(value: Any) -> time.struct_time | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000
        return time.gmtime(ts)

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_timestamp(int(text))

    for parser in (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        parsedate_to_datetime,
    ):
        try:
            parsed = parser(text)
            if parsed.tzinfo is not None:
                return time.gmtime(parsed.timestamp())
            return time.gmtime(time.mktime(parsed.timetuple()))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return time.gmtime(time.mktime(parsed.timetuple()))
        except ValueError:
            continue
    return None


def normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    # Keep identity derivation on the untouched payload.  Changing this order
    # would alter de-duplication keys for upstream items that have no stable ID.
    item_id = stable_item_id(item)
    title = plain_text_from_markup(
        item.get("title")
        or item.get("content")
        or item.get("content_html")
        or item.get("summary")
        or item.get("description")
        or item.get("text")
        or ""
    )
    link = _normalized_item_link(item, item_id)
    if not item_id or not title or not link:
        return None

    content = plain_text_from_markup(
        item.get("content")
        or item.get("content_html")
        or item.get("summary")
        or item.get("description")
        or item.get("text")
        or title
    )
    published = None
    for key in ("published", "pubDate", "date_published", "date_modified", "datetime", "time", "timestamp", "created_at"):
        published = _parse_timestamp(item.get(key))
        if published:
            break

    return {
        "id": item_id,
        "title": title,
        "url": link,
        "content": content,
        "published_parsed": published,
    }


def build_jin10_entries(source_url: str | None = None, limit: int = 50) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    seen: set[str] = set()
    for raw_item in fetch_jin10_latest(source_url):
        item = normalize_item(raw_item)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        entries.append(
            FeedEntry(
                item["title"],
                item["url"],
                item["content"],
                published_parsed=item.get("published_parsed"),
            )
        )
        if len(entries) >= limit:
            break
    return entries


def build_jin10_rss(request_url: str, cfg: dict, source_url: str | None = None, limit: int = 50) -> str:
    entries = build_jin10_entries(source_url=source_url, limit=limit)
    return build_rss_xml(entries, _feed_title(), _feed_url(), request_url, cfg)
