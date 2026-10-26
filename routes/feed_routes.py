"""
AI-Filter Feed 路由：单源过滤、多源合并、缓存读取
"""
import time
import calendar
import json
import logging
import math
import os
import re
import threading
import feedparser
from urllib.parse import unquote, urlsplit, urlunsplit
from flask import Blueprint, Response, request

from routes.security import rate_limit, validate_target_url
from routes.shared import (
    _cfg,
    check_api_key,
    get_cached,
    set_cached,
    flush_stats,
    extract_score_from_title,
    FeedEntry,
    resolve_feed_url_to_cache_key,
    strategy_cache_key,
    DB,
    resolve_prompt,
)
from core.processor import (
    process_feed, build_rss_xml, build_json_feed, build_html_feed,
    feed_processing_complete,
)
from core.theme_rules import (
    ConfiguredTheme,
    InvalidThemeError,
    resolve_configured_theme,
)
from core.user_feedback import resolve_scoring_contract
from utils.helpers import (
    direction_emoji,
    format_article_display_title,
    normalize_direction,
    plain_text_from_markup,
)


def _retired_rsshub_source_response(urls: list[str]) -> Response | None:
    """A retired category must not be resurrected from an old RSS/DB cache."""
    for url in urls:
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"}:
            continue
        if unquote(parsed.path).rstrip("/") == "/wallstreetcn/news/china":
            response = Response(
                "华尔街见闻中国入口已停用：上游不支持 china 分类。请使用全球资讯或其他受支持分类。",
                status=410, mimetype="text/plain",
            )
            response.headers["X-RSSHub-Source-State"] = "retired"
            response.headers["Cache-Control"] = "no-store"
            return response
    return None


def _entry_time_for_sort(entry) -> int:
    value = getattr(entry, "published_parsed", None)
    if value:
        try:
            return int(calendar.timegm(value))
        except (OverflowError, TypeError, ValueError):
            return 0
    return 0


def _sort_entries_by_timeline(entries: list) -> None:
    entries.sort(key=lambda e: (_entry_time_for_sort(e), extract_score_from_title(e.title)), reverse=True)


def _published_parsed_from_ts(ts) -> object:
    try:
        # FeedEntry parsed timestamps are UTC throughout the RSS pipeline.
        return time.gmtime(int(ts)) if ts else None
    except (OverflowError, TypeError, ValueError):
        return None


def _cached_rss_has_item_dates(xml: str) -> bool:
    if not xml or "<item>" not in xml:
        return False
    item_count = xml.count("<item>")
    if item_count <= 0:
        return False
    dated_count = xml.count("<pubDate>")
    return dated_count >= item_count


def _bounded_query_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = request.args.get(name, default=default, type=int)
    if value is None:
        value = default
    return max(minimum, min(maximum, int(value)))


def _finite_query_float(
    name: str,
    *,
    default: float | None = None,
    minimum: float = 0.0,
    maximum: float = 10.0,
) -> float | None:
    raw = request.args.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in {minimum:g}..{maximum:g}")
    return value


def _default_pass_score(cfg: dict) -> float:
    try:
        value = float((cfg.get("threshold", {}) or {}).get("pass", 7.0))
    except (TypeError, ValueError):
        value = 7.0
    return value if math.isfinite(value) and 0 <= value <= 10 else 7.0


def _feed_entry_from_cached_row(row: tuple) -> FeedEntry:
    """Build a public feed entry without exposing markup stored in its title."""
    link, title, raw_title, score, _tag, _logic, summary, created_at = row[:8]
    event_id = row[8] if len(row) > 8 else ""
    try:
        event_version = max(0, int(row[9] or 0)) if len(row) > 9 else 0
    except (TypeError, ValueError):
        event_version = 0
    display_title = plain_text_from_markup(
        format_article_display_title(title, raw_title)
    ) or "未命名情报"
    entry = FeedEntry(
        display_title,
        link,
        summary or "",
        published_parsed=_published_parsed_from_ts(created_at),
    )
    entry.scoring_meta = {
        "score": score,
        "event_id": str(event_id or ""),
        "event_version": event_version,
    }
    return entry


def _cached_feed_entry(row: tuple, *, observation: bool = False) -> FeedEntry:
    link, title, raw_title, score, tag, logic, summary, created_at = row
    display_title = plain_text_from_markup(
        format_article_display_title(title, raw_title)
    )
    if not display_title:
        display_title = plain_text_from_markup(raw_title or title) or "未命名情报"
    if len(display_title) > 96:
        display_title = display_title[:95].rstrip() + "…"
    if observation:
        compact_title = f"[观察] {display_title}"
    else:
        try:
            score_text = f"{float(score):.1f}"
        except (TypeError, ValueError):
            score_text = "--"
        compact_title = (
            f"{direction_emoji(normalize_direction(text=title))} "
            f"{score_text}｜{display_title}"
        )
    return FeedEntry(
        compact_title,
        link,
        summary or logic or "",
        published_parsed=_published_parsed_from_ts(created_at),
    )


def _cached_feed_source_name(cfg: dict, target_url: str) -> str:
    try:
        feed_cfg = DB.get_feed_config(cfg, target_url) or {}
        name = str(feed_cfg.get("name") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return "当前来源"


def _cached_feed_public_home(entries: list[FeedEntry]) -> str:
    """Use the first public article origin instead of an internal rsshub link."""
    for entry in entries:
        try:
            parsed = urlsplit(str(entry.link or ""))
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        except (TypeError, ValueError):
            continue
    return ""

logger = logging.getLogger(__name__)
feed_bp = Blueprint("feed", __name__)


def _contains_replacement_character(value) -> bool:
    """Return whether a nested public-output value contains U+FFFD."""
    if isinstance(value, str):
        return "\ufffd" in value
    if isinstance(value, dict):
        return any(
            _contains_replacement_character(item)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_replacement_character(item) for item in value)
    return False


def _safe_public_rows(rows, *, context: str) -> list:
    """Drop polluted DB rows before any public text cleanup or fallback."""
    materialized = [] if rows is None else list(rows)
    safe_rows = [
        row for row in materialized if not _contains_replacement_character(row)
    ]
    excluded_count = len(materialized) - len(safe_rows)
    if excluded_count:
        logger.warning(
            "[Public output] excluded replacement-character rows context=%s count=%d",
            context,
            excluded_count,
        )
    return safe_rows


def _safe_public_event_tag(value: object) -> str:
    """Fail closed on malformed legacy taxonomy without dropping the event."""
    tag = str(value or "").strip()
    if (
        not tag
        or tag == "囸级政策、国家级政策"
        or tag.casefold() in {"unknown", "none", "null", "未知"}
        or re.fullmatch(r"-?\d+(?:\.\d+)?", tag)
    ):
        return "其他事件"
    return tag


def _event_feed_unavailable_response(format_type: str, cfg: dict) -> Response:
    """Return an explicit retryable failure instead of a plausible empty feed."""
    message = "事件缓存暂不可用，请稍后重试"
    if format_type == "json":
        body = json.dumps(
            {
                "error": "event_feed_temporarily_unavailable",
                "events": None,
            },
            ensure_ascii=False,
        )
        mimetype = "application/json"
    elif format_type == "rss":
        body = build_rss_xml(
            [],
            "A 股事件雷达 · 暂不可用",
            "events",
            request.url,
            cfg,
            channel_description=message,
        )
        mimetype = "application/rss+xml"
    else:
        body = build_html_feed(
            [],
            "A 股事件雷达 · 暂不可用",
            "events",
            cfg,
            channel_description=message,
        )
        mimetype = "text/html"
    response = Response(body, status=503, mimetype=mimetype)
    response.headers["Retry-After"] = "30"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Event-Radar-State"] = "unknown"
    return response


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# A cache miss can synchronously fetch RSSHub and invoke multiple LLM stages.
# Keep most Gunicorn threads available for Dashboard and cached read routes even
# when authenticated callers issue several cold requests at once.
_COLD_FILTER_LIMIT = _bounded_env_int(
    "AI_FILTER_COLD_FILTER_MAX_CONCURRENCY", 2, 1, 4
)
_COLD_FILTER_SLOTS = threading.BoundedSemaphore(_COLD_FILTER_LIMIT)
_COLD_FILTER_STATE_LOCK = threading.Lock()
_COLD_FILTER_INFLIGHT = 0
_COLD_FILTER_REJECTIONS = 0


def _try_acquire_cold_filter_slot() -> bool:
    global _COLD_FILTER_INFLIGHT, _COLD_FILTER_REJECTIONS
    acquired = _COLD_FILTER_SLOTS.acquire(blocking=False)
    with _COLD_FILTER_STATE_LOCK:
        if acquired:
            _COLD_FILTER_INFLIGHT += 1
        else:
            _COLD_FILTER_REJECTIONS += 1
    return acquired


def _release_cold_filter_slot() -> None:
    global _COLD_FILTER_INFLIGHT
    with _COLD_FILTER_STATE_LOCK:
        _COLD_FILTER_INFLIGHT = max(0, _COLD_FILTER_INFLIGHT - 1)
        # Publish capacity while the gauge lock is held.  A newly admitted
        # request then waits to increment until this decrement is visible, so
        # /metrics cannot observe an impossible transient ``inflight > limit``.
        _COLD_FILTER_SLOTS.release()


def cold_filter_capacity_metrics() -> dict[str, int]:
    """Return process-local gauges; production intentionally has one API worker."""
    with _COLD_FILTER_STATE_LOCK:
        return {
            "limit": _COLD_FILTER_LIMIT,
            "inflight": _COLD_FILTER_INFLIGHT,
            "rejections_total": _COLD_FILTER_REJECTIONS,
        }


def _cold_filter_busy_response() -> Response:
    response = Response(
        json.dumps(
            {
                "error": "cold_filter_capacity_reached",
                "retry_after": 5,
            },
            ensure_ascii=False,
        ),
        status=503,
        mimetype="application/json",
    )
    response.headers["Retry-After"] = "5"
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_feed_entries(
    entries,
    format_type: str,
    channel_title: str,
    target_url: str,
    request_url: str,
    cfg: dict,
    *,
    channel_description: str | None = None,
) -> Response:
    if format_type == "json":
        return Response(
            build_json_feed(
                entries,
                channel_title,
                target_url,
                cfg,
                channel_description=channel_description,
            ),
            mimetype="application/json",
        )
    if format_type == "html":
        return Response(
            build_html_feed(
                entries,
                channel_title,
                target_url,
                cfg,
                channel_description=channel_description,
            ),
            mimetype="text/html",
        )
    return Response(
        build_rss_xml(
            entries,
            channel_title,
            target_url,
            request_url,
            cfg,
            channel_description=channel_description,
        ),
        mimetype="application/rss+xml",
    )


def _slice_channel_metadata(
    cfg: dict,
    target_url: str,
    min_score: float | None,
    theme: ConfiguredTheme | None,
    *,
    requested_theme: str = "",
) -> tuple[str, str]:
    parts = ["AI-Filter"]
    if target_url and target_url != "all":
        parts.append(_cached_feed_source_name(cfg, target_url))
    theme_label = theme.name if theme is not None else requested_theme.strip()
    parts.append(theme_label or "全部主题")
    if min_score is not None:
        parts.append(f"≥{min_score:.1f}")
    else:
        parts.append("按源门槛")
    details = ["只读数据库缓存切片"]
    if theme_label:
        details.append(f"主题={theme_label}")
    if min_score is not None:
        details.append(f"最低分={min_score:.1f}")
    else:
        details.append("保留来源聚合门槛")
    details.append("不触发抓取或评分")
    return " · ".join(parts), "；".join(details)


@feed_bp.route("/feed/json")
@rate_limit(max_requests=60, window_seconds=60)
def feed_json():
    return _feed_from_cache("json")


@feed_bp.route("/feed/html")
@rate_limit(max_requests=60, window_seconds=60)
def feed_html():
    return _feed_from_cache("html")


@feed_bp.route("/feed/rss")
@rate_limit(max_requests=60, window_seconds=60)
def feed_rss():
    return _feed_from_cache("rss", default_min_score=True)


def _feed_from_cache(format_type: str, *, default_min_score: bool = False):
    target_url = request.args.get("url", "all") or "all"
    retired = _retired_rsshub_source_response([target_url])
    if retired is not None:
        return retired
    cfg = _cfg()
    hours = _bounded_query_int("hours", 48 if default_min_score else 24, 1, 168)
    limit = _bounded_query_int("limit", 30, 1, 50)
    try:
        min_score = _finite_query_float(
            "min_score",
            default=_default_pass_score(cfg) if default_min_score else None,
        )
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype="text/plain")

    requested_theme = str(request.args.get("theme") or "").strip()
    requested_theme_display = requested_theme[:64]
    unknown_theme = False
    try:
        theme = resolve_configured_theme(cfg, requested_theme)
    except InvalidThemeError as exc:
        return Response(str(exc), status=400, mimetype="text/plain")
    except KeyError:
        theme = None
        unknown_theme = True

    query_kwargs = {
        "hours": hours,
        "limit": limit,
        "feed_url": None if target_url == "all" else target_url,
    }
    if min_score is not None:
        query_kwargs["min_score"] = min_score
    if theme is not None:
        query_kwargs["theme_terms"] = theme.terms
    if format_type == "json":
        query_kwargs["include_event_identity"] = True
    rows = (
        []
        if unknown_theme
        else DB.get_entries_for_cached_rss(cfg, **query_kwargs)
    )
    rows = _safe_public_rows(rows, context=f"/feed/{format_type}")
    entries = [_feed_entry_from_cached_row(row) for row in rows]
    filtered_slice = default_min_score or min_score is not None or bool(requested_theme)
    if filtered_slice:
        channel_title, channel_description = _slice_channel_metadata(
            cfg,
            target_url,
            min_score,
            theme,
            requested_theme=requested_theme_display,
        )
    else:
        channel_title = "AI 智能过滤: 综合情报"
        channel_description = None
    channel_home = target_url
    if target_url == "all" and format_type == "rss":
        channel_home = _cached_feed_public_home(entries) or request.url_root.rstrip("/")
    return _render_feed_entries(
        entries,
        format_type,
        channel_title,
        channel_home,
        request.url,
        cfg,
        channel_description=channel_description,
    )


@feed_bp.route("/events/json")
@feed_bp.route("/events/rss")
@feed_bp.route("/events/html")
@rate_limit(max_requests=60, window_seconds=60)
def events_feed():
    cfg = _cfg()
    format_type = request.path.rsplit("/", 1)[-1]
    hours = request.args.get("hours", 24, type=int)
    limit = request.args.get("limit", 30, type=int)
    hours = max(1, min(168, hours if hours is not None else 24))
    limit = max(1, min(50, limit if limit is not None else 30))
    try:
        min_score = _finite_query_float("min_score")
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype="text/plain")
    raw_direction = request.args.get("direction")
    direction = str(raw_direction or "").strip()
    if raw_direction is not None and direction not in {"看多", "看空", "中性"}:
        return Response(
            "direction must be 看多, 看空, or 中性",
            status=400,
            mimetype="text/plain",
        )
    event_query = {"cfg": cfg, "hours": hours, "limit": limit}
    if min_score is not None:
        event_query["min_score"] = min_score
    if direction:
        event_query["direction"] = direction
    try:
        raw_events = DB.get_events(**event_query)
    except Exception as exc:
        logger.warning(
            "[Public events] event cache read unavailable: %s",
            type(exc).__name__,
        )
        return _event_feed_unavailable_response(format_type, cfg)
    events = _safe_public_rows(raw_events, context="/events")
    title_parts = ["A 股事件雷达"]
    description_parts = ["只读事件缓存切片"]
    if direction:
        title_parts.append(direction)
        description_parts.append(f"方向={direction}")
    if min_score is not None:
        title_parts.append(f"重要度≥{min_score:.1f}")
        description_parts.append(f"最低事件重要度={min_score:.1f}")
    filtered_title = " · ".join(title_parts)
    filtered_description = "；".join(description_parts)

    def event_response(body: str, mimetype: str) -> Response:
        return Response(body, mimetype=mimetype)

    if format_type == "json":
        fields = [
            "event_id", "canonical_title", "canonical_link", "summary",
            "score", "tag", "source_count", "first_seen_at", "last_seen_at",
            "direction",
        ]
        records = []
        for raw in events:
            record = dict(zip(fields, raw))
            record["canonical_title"] = (
                plain_text_from_markup(record.get("canonical_title", ""))
                or "未命名事件"
            )
            record["importance_score"] = record.get("score")
            record["tag"] = _safe_public_event_tag(record.get("tag"))
            record["direction"] = normalize_direction(
                record.get("direction", ""), text=record.get("canonical_title", "")
            )
            records.append(record)
        return event_response(
            json.dumps(
                {"events": records},
                ensure_ascii=False,
                indent=2,
            ),
            "application/json",
        )
    entries = []
    for event in events:
        canonical_title = plain_text_from_markup(event[1]) or "未命名事件"
        entries.append(
            FeedEntry(
                f"[事件 {event[6]}源｜重要度 {event[4]}/10｜"
                f"{normalize_direction(event[9] if len(event) > 9 else '', text=canonical_title)}｜"
                f"{_safe_public_event_tag(event[5])}] {canonical_title}",
                event[2],
                event[3] or "",
            )
        )
    if format_type == "html":
        return event_response(
            build_html_feed(
                entries,
                filtered_title,
                "events",
                cfg,
                channel_description=filtered_description,
            ),
            "text/html",
        )
    return event_response(
        build_rss_xml(
            entries,
            filtered_title,
            (
                request.url_root.rstrip("/")
                if direction or min_score is not None
                else "events"
            ),
            request.url,
            cfg,
            channel_description=filtered_description,
        ),
        "application/rss+xml",
    )


def _single_source_response(xml: str, *, complete: bool) -> Response:
    response = Response(xml, mimetype="application/rss+xml")
    response.headers["X-Filter-State"] = "complete" if complete else "partial"
    if not complete:
        response.headers["Cache-Control"] = "no-store"
    return response


@feed_bp.route("/filter")
def filter_rss():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    target_url = request.args.get('url')
    if not target_url:
        return "\u8bf7\u63d0\u4f9b url \u53c2\u6570", 400

    ok, err = validate_target_url(target_url, _cfg())
    if not ok:
        return json.dumps({"error": err}, ensure_ascii=False), 400

    retired = _retired_rsshub_source_response([target_url])
    if retired is not None:
        return retired
    cfg = resolve_scoring_contract(_cfg(), register_snapshot=False)
    cache_url = strategy_cache_key(resolve_feed_url_to_cache_key(target_url, cfg), cfg)

    cached_xml = get_cached(cache_url, cfg["cache_ttl"])
    if cached_xml is not None and _cached_rss_has_item_dates(cached_xml):
        return _single_source_response(cached_xml, complete=True)

    # 内\u5b58\u7f13\u5b58\u4e0d\u5b58\u5728\uff0c\u4ece\u6570\u636e\u5e93\u964d\u7ea7\u91cd\u5efa（\u6309\u6765\u6e90\u8fc7\u6ee4）
    raw_rows = list(
        DB.get_entries_for_cached_rss(
            cfg,
            hours=24,
            limit=cfg.get("output", {}).get("max_items", 30) or 30,
            feed_url=target_url,
        )
        or []
    )
    rows = _safe_public_rows(raw_rows, context="/filter DB fallback")
    if raw_rows:
        passed = [_feed_entry_from_cached_row(row) for row in rows]
        _sort_entries_by_timeline(passed)
        max_items = cfg.get("output", {}).get("max_items", 0)
        if max_items > 0:
            passed = passed[:max_items]
        title_suffix = target_url.split("/")[-1] or "\u672a\u77e5\u6e90"
        channel_title = "AI \u667a\u80fd\u8fc7\u6ee4: " + title_suffix
        xml_out = build_rss_xml(passed, channel_title, target_url, request.url, cfg)
        # Stored rows prove availability, not that the last source processing
        # finished. Never promote a previously partial cold result to complete.
        return _single_source_response(xml_out, complete=False)

    # \u6570\u636e\u5e93\u4e5f\u65e0\u6570\u636e\uff0c\u5b9e\u65f6\u5904\u7406（\u964d\u7ea7）
    if not _try_acquire_cold_filter_slot():
        return _cold_filter_busy_response()
    prompt_text = resolve_prompt(target_url, cfg)[1]
    req_start = time.time()
    try:
        passed_entries, stats, error, src_title = process_feed(
            target_url, prompt_text, cfg=cfg, check_output_cache=False
        )
    finally:
        _release_cold_filter_slot()
    duration_ms = int((time.time() - req_start) * 1000)

    DB.log_request(
        timestamp=int(req_start),
        url=target_url,
        total=stats["total"],
        passed=stats["passed"],
        cache_hits=stats["cache_hits"],
        similarity_hits=stats["similarity_hits"],
        blacklist_hits=stats["blacklist_hits"],
        fast_pass_hits=stats.get("fast_pass_hits", 0),
        api_calls=stats["api_calls"],
        duration_ms=duration_ms,
        error=error
    )

    flush_stats(DB.update_stats)

    _sort_entries_by_timeline(passed_entries)
    max_items = cfg.get("output", {}).get("max_items", 0)
    if max_items > 0:
        passed_entries = passed_entries[:max_items]

    title_suffix = src_title or target_url.split("/")[-1] or "\u672a\u77e5\u6e90"
    channel_title = "AI \u667a\u80fd\u8fc7\u6ee4: " + title_suffix

    xml_out = build_rss_xml(passed_entries, channel_title, target_url, request.url, cfg)
    complete = feed_processing_complete(stats, error)
    if passed_entries and complete:
        set_cached(cache_url, xml_out)
    return _single_source_response(xml_out, complete=complete)


def _multi_source_cached_entries(cache_key: str, cfg: dict) -> list | None:
    """An empty valid RSS is a resolved source, not a cold-cache miss."""
    xml = get_cached(cache_key, cfg["cache_ttl"])
    if xml is None:
        return None
    parsed = feedparser.parse(xml)
    version = str(getattr(parsed, "version", "") or "")
    if getattr(parsed, "bozo", False) or not version.startswith(("rss", "atom")):
        logger.warning("[filter/multi] invalid single-source RSS cache; using fallback")
        return None
    return [FeedEntry(
        title=plain_text_from_markup(entry.get("title", "")) or "未命名情报",
        link=entry.get("link", ""),
        summary=entry.get("summary", ""),
        published_parsed=entry.get("published_parsed"),
    ) for entry in parsed.entries]


def _multi_source_warm_entries(urls: list, cfg: dict) -> tuple[list, list, bool]:
    """Resolve each source independently through XML cache, then its DB rows."""
    entries, db_rows, pending = [], [], []
    complete = True
    seen_cache_keys = set()
    fallback_limit = cfg.get("output", {}).get("max_items", 30) or 30
    for url in urls:
        cache_key = strategy_cache_key(resolve_feed_url_to_cache_key(url, cfg), cfg)
        if cache_key in seen_cache_keys:
            continue
        seen_cache_keys.add(cache_key)
        cached_entries = _multi_source_cached_entries(cache_key, cfg)
        if cached_entries is not None:
            entries.extend(cached_entries)
            continue
        rows = DB.get_entries_for_cached_rss(
            cfg, hours=24, limit=fallback_limit, feed_url=url,
        )
        if rows:
            # Existing but excluded rows must not trigger a refill or expanded
            # query. Preserve that safety boundary separately from no DB rows.
            db_rows.extend(_safe_public_rows(rows, context="/filter/multi DB fallback"))
            # No full-feed completion proof accompanies these stored rows.
            complete = False
        else:
            pending.append(url)
    for row in sorted(db_rows, key=lambda r: (r[7] or 0, r[3] or 0), reverse=True):
        entries.append(_feed_entry_from_cached_row(row))
    return entries, pending, complete


def _multi_source_event_references(entries: list) -> dict[str, str]:
    """Resolve known event IDs once; unavailable identity stays conservative."""
    from db.database import get_read_only_connection
    from db.event_core import EventCoreConflictError, canonical_event_id

    event_ids = {
        str((getattr(entry, "scoring_meta", None) or {}).get("event_id") or "").strip()
        for entry in entries
    } - {""}
    resolved = {}
    if not event_ids:
        return resolved
    try:
        with get_read_only_connection() as conn:
            for event_id in event_ids:
                try:
                    resolved[event_id] = canonical_event_id(conn, event_id)
                except (EventCoreConflictError, KeyError, TypeError, ValueError):
                    continue
    except Exception:
        logger.debug("[filter/multi] event identity unavailable", exc_info=True)
    return resolved


def _dedupe_multi_source_entries(entries: list) -> list:
    """Use persisted events, or exact link/title pairs; never title alone.

    XML caches do not carry event IDs. Different links without that proof stay
    separate even if their text matches; dates and company names are facts.
    An unresolved explicit ID also cannot collapse into another ID or XML row.
    """
    references = _multi_source_event_references(entries)
    selected = {}
    for entry in entries:
        meta = getattr(entry, "scoring_meta", None) or {}
        event_id = str(meta.get("event_id") or "").strip()
        link = str(getattr(entry, "link", "") or "").strip()
        if event_id in references:
            key = ("event", references[event_id])
        elif event_id:
            key = ("unresolved-event", event_id, link or id(entry), entry.title)
        else:
            title = format_article_display_title(entry.title)
            key = ("link-title", link, title) if link and title else ("unidentified", id(entry))
        try:
            score = float(meta.get("score", extract_score_from_title(entry.title)) or 0)
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        rank = (score if math.isfinite(score) else 0.0, _entry_time_for_sort(entry))
        if key not in selected or rank > selected[key][0]:
            selected[key] = (rank, entry)
    return [entry for _rank, entry in selected.values()]


def _multi_source_cold_entries(urls: list, cfg: dict) -> tuple[list, bool]:
    """One admitted request processes cold sources serially, never in fan-out."""
    entries = []
    complete = True
    for url in urls:
        prompt = resolve_prompt(url, cfg)[1]
        passed, stats, error, _title = process_feed(
            url, prompt, cfg=cfg, check_output_cache=False,
        )
        entries.extend(passed)
        # process_feed reports partial entry failures in stats while retaining
        # a successful transport result. They must not populate a full cache.
        if not feed_processing_complete(stats, error):
            complete = False
            logger.warning(
                "[filter/multi] %s incomplete: error=%s entry_errors=%s "
                "score_deferred=%s source_unrecognized=%s",
                url, error, stats.get("entry_errors", 0),
                stats.get("score_deferred", 0), stats.get("source_unrecognized", 0),
            )
    return entries, complete


def _multi_source_response(xml: str, *, complete: bool) -> Response:
    response = Response(xml, mimetype="application/rss+xml")
    response.headers["X-Filter-Multi-State"] = "complete" if complete else "partial"
    if not complete:
        response.headers["Cache-Control"] = "no-store"
    return response


@feed_bp.route("/filter/multi")
def filter_multi():
    if not check_api_key():
        return json.dumps({"error": "Unauthorized"}), 401
    urls_param = request.args.get('urls', '')
    if not urls_param:
        return "\u8bf7\u63d0\u4f9b urls \u53c2\u6570\uff08\u9017\u53f7\u5206\u9694\uff09", 400

    urls = list(dict.fromkeys(u.strip() for u in urls_param.split(',') if u.strip()))
    cfg = resolve_scoring_contract(_cfg(), register_snapshot=False)
    for u in urls:
        ok, err = validate_target_url(u, _cfg())
        if not ok:
            return json.dumps({"error": f"\u975e\u6cd5URL [{u}]: {err}"}, ensure_ascii=False), 400

    retired = _retired_rsshub_source_response(urls)
    if retired is not None:
        return retired
    # Bypass old caches that omitted sources or merged distinct dated events.
    cache_key = strategy_cache_key("multi:source-complete-v1:event-identity-v2:" + urls_param, cfg)
    cached_xml = get_cached(cache_key, cfg["cache_ttl"])
    if cached_xml is not None:
        return _multi_source_response(cached_xml, complete=True)

    all_passed, pending, complete = _multi_source_warm_entries(urls, cfg)
    if pending:
        if not _try_acquire_cold_filter_slot():
            return _cold_filter_busy_response()
        try:
            cold_entries, cold_complete = _multi_source_cold_entries(pending, cfg)
            complete = complete and cold_complete
            all_passed.extend(cold_entries)
        finally:
            _release_cold_filter_slot()

    all_passed = _dedupe_multi_source_entries(all_passed)

    _sort_entries_by_timeline(all_passed)
    max_items = cfg.get("output", {}).get("max_items", 0)
    if max_items > 0:
        all_passed = all_passed[:max_items]

    channel_title = "AI \u667a\u80fd\u8fc7\u6ee4: \u7efc\u5408\u60c5\u62a5\u6e90"
    xml_out = build_rss_xml(all_passed, channel_title, urls_param, request.url, cfg)
    if all_passed and complete:
        set_cached(cache_key, xml_out)
    return _multi_source_response(xml_out, complete=complete)


@feed_bp.route("/cached_feed")
def cached_feed():
    """返\u56de\u9884\u7f13\u5b58\u7684\u8fc7\u6ee4RSS\uff0c\u6781\u901f（\u7528\u4e8eGlance\u7b49\u9762\u677f）
    直接从本地数据库重建，不实时抓源或调用模型。Glance 自身负责 30 秒共享缓存；
    避免使用 Web 进程内缓存，否则独立 Worker 刷新后页面仍可能陈旧 30 分钟。
    """
    target_url = request.args.get("url")
    if not target_url:
        return Response("Missing ?url= parameter", status=400)

    ok, err = validate_target_url(target_url, _cfg())
    if not ok:
        return Response(f"Invalid URL: {err}", status=400)

    retired = _retired_rsshub_source_response([target_url])
    if retired is not None:
        return retired
    cfg = _cfg()
    source_name = _cached_feed_source_name(cfg, target_url)
    hours = _bounded_query_int("hours", 48, 1, 24 * 7)
    limit = _bounded_query_int("limit", 20, 1, 50)
    force_preview = request.args.get("preview", "false").lower() == "true"

    if force_preview:
        try:
            raw_rows = list(DB.get_recent_entries_for_feed_preview(
                cfg, hours=min(hours, 24), limit=limit,
                feed_url=target_url, order_by="recent"
            ) or [])
            rows = _safe_public_rows(raw_rows, context="/cached_feed preview")
            if raw_rows:
                passed = [_cached_feed_entry(row, observation=True) for row in rows]
                xml = build_rss_xml(
                    passed,
                    "AI 智能过滤 (观察流): " + source_name,
                    _cached_feed_public_home(passed),
                    request.url,
                    cfg,
                )
                return Response(xml, mimetype="application/rss+xml")
        except Exception as e:
            logger.warning("[cached_feed] preview failed for %s: %s", target_url, str(e)[:160])

    try:
        raw_rows = list(DB.get_entries_for_cached_rss(
            cfg, hours=hours, limit=limit, feed_url=target_url
        ) or [])
        rows = _safe_public_rows(raw_rows, context="/cached_feed")
        if raw_rows:
            passed = [_cached_feed_entry(row) for row in rows]
            _sort_entries_by_timeline(passed)
            xml = build_rss_xml(
                passed,
                "AI \u667a\u80fd\u8fc7\u6ee4: " + source_name,
                _cached_feed_public_home(passed),
                request.url,
                cfg,
            )
            return Response(xml, mimetype="application/rss+xml")
    except Exception:
        pass

    # 通用观察流兜底：不按高分阈值过滤，避免新增源或低频源完全不可读。
    try:
        fallback_rows = DB.get_recent_entries_for_feed_preview(
            cfg, hours=24 * 7, limit=min(3, limit),
            feed_url=target_url, order_by="recent"
        )
        fallback_rows = _safe_public_rows(
            fallback_rows,
            context="/cached_feed fallback preview",
        )
        if fallback_rows:
            passed = [
                _cached_feed_entry(row, observation=True)
                for row in fallback_rows[: min(3, limit)]
            ]
            xml = build_rss_xml(
                passed,
                "AI \u667a\u80fd\u8fc7\u6ee4 (\u89c2\u5bdf\u6d41): " + source_name,
                _cached_feed_public_home(passed),
                request.url,
                cfg,
            )
            return Response(xml, mimetype="application/rss+xml")
    except Exception as e:
        logger.warning("[cached_feed] fallback preview failed for %s: %s", target_url, str(e)[:160])
    # 若 DB 和 7天降级均无数据，返回空 RSS
    empty_rss = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>AI-Filter 暂无高分情报</title>
<link>http://localhost:5000</link>
<description>当前源暂无达到阈值的高分情报</description>
</channel>
</rss>'''
    return Response(empty_rss, mimetype="application/rss+xml")
