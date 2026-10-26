"""
AI-Filter Dashboard 路由：首页、Dashboard、行情、小部件、Digest
"""
import time
import json
import hashlib
import math
import logging
import re
import os
import threading
from datetime import datetime
from utils.beijing_time import beijing_now, beijing_fromtimestamp
from html import escape as html_escape
from flask import Blueprint, Response, request, render_template_string, redirect

from routes.security import rate_limit
from routes.ops_feed_layout import OPS_FEED_LAYOUT_STYLE
from routes.browser_auth import (
    apply_browser_security_headers,
    browser_error,
    dashboard_login_redirect,
    get_dashboard_auth,
    request_prefers_html,
    verify_session_csrf,
)
from routes.shared import (
    _cfg,
    check_api_key,
    scorer,
    DB,
    flush_stats,
    is_refresh_job_running,
    _staleness_info,
    DASHBOARD_TEMPLATE,
    _quotes_cache_lock,
    _quotes_refresh_lock,
    _quotes_cache,
    QUOTES_CACHE_TTL_SEC,
)
from db import database
from db.database import db_lock, get_connection
from scoring_provenance import (
    FORMAL_SIGNAL_SCORE_PATHS,
    SAFE_NEUTRAL_CONTRACT_SCORE_PATHS,
    SAFE_NEUTRAL_GROUNDING_SCORE_PATHS,
    normalize_score_path,
)
from utils.helpers import (
    escape_xml,
    feed_data_health,
    format_article_display_title,
    normalize_direction,
    safe_http_url,
)
from core.digest_engine import (
    aggregate_themes,
    briefing_representative_articles,
    briefing_source_confirmation,
    briefing_source_metrics,
    briefing_theme_sort_key,
    canonical_theme_name,
    render_digest,
    render_themes_html,
    dedupe_articles,
    enrich_article_analysis,
)
from core.briefing_assembly import (
    assemble_briefing_candidates,
    briefing_candidate_multiplier,
    briefing_policy_fingerprint,
    normalize_briefing_grounding_policy,
    normalize_briefing_policy,
)
from core.asset_mapping import (
    classify_policy_region,
    get_mapping_version,
    is_a_share_market_stress,
    is_consumption_policy,
)
from core.config import load_config
from core.user_feedback import resolve_scoring_contract
from core.theme_rules import (
    article_primary_theme_text,
    is_broad_macro_theme_tag,
    is_subscription_only_theme,
    theme_display_name,
)
from services.digest import _push_digest_to_telegram
from services.quant_market_context import attach_quant_market_context
from services.backtest import (
    render_backtest_compact_html,
    render_backtest_html,
    run_score_backtest,
)
from services.mapping_validation import run_mapping_validation
from services.quality_bridge_preview import bridge_preview_data_version
from services.release_info import release_version
from services.dashboard_ops import build_dashboard_ops
from services.briefing_reading import render_briefing_reading_html
from core.processor import _is_quiet_mode

logger = logging.getLogger(__name__)
dash_bp = Blueprint("dash", __name__)
_BACKTEST_CACHE: dict = {}
_BACKTEST_CACHE_LOCK = threading.Lock()
_BACKTEST_COMPUTE_LOCK = threading.Lock()
_BACKTEST_CACHE_TTL_SEC = 300.0
_BACKTEST_CACHE_MAX_ENTRIES = 16
_BRIEFING_LLM_CACHE_TTL_SEC = 600
_BRIEFING_CACHE_VERSION = 4
_OPS_WIDGET_CACHE: dict = {}
_OPS_WIDGET_CACHE_LOCK = threading.Lock()
_OPS_SUMMARY_CACHE_TTL_SEC = 30.0
_OPS_FEEDS_CACHE_TTL_SEC = 60.0
_OPS_WIDGET_CACHE_MAX_ENTRIES = 8


_SNAPSHOT_DEFAULT_MAX_AGE_SECONDS = 14400.0


def _snapshot_max_age_seconds() -> float:
    """Bound for a missed expected refresh, not a mere age hint.

    Refresh runs leave a ~2h planned gap overnight and on weekends, so the
    default follows the existing ``staleness_warn_seconds`` convention
    (14400s) instead of the 300s task coalescing window; a normal wait for
    the next cycle must not read as stale.  Non-finite values (inf/nan) or
    non-positive values fall back to the default so the check can never be
    permanently disabled by configuration.
    """
    import math

    raw = str(os.environ.get("AI_FILTER_DASHBOARD_SNAPSHOT_MAX_AGE_SECONDS") or "").strip()
    if raw:
        try:
            candidate = float(raw)
        except ValueError:
            candidate = 0.0
        if math.isfinite(candidate) and candidate > 0:
            return max(60.0, min(candidate, 86400.0))
    try:
        value = float(load_config().get("staleness_warn_seconds") or 0)
    except Exception:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        value = _SNAPSHOT_DEFAULT_MAX_AGE_SECONDS
    return max(60.0, min(value, 86400.0))


def _snapshot_age_seconds(generated_at: str) -> float | None:
    """Seconds since generation; None when the timestamp cannot be trusted.

    An unparseable timestamp must never read as "fresh by default" through a
    silent arithmetic failure, so parse errors are distinguished from real
    ages by returning ``None`` and the caller keeps the legacy ready state.
    """
    from datetime import datetime, timezone

    raw = str(generated_at or "").strip()
    if not raw:
        return None
    try:
        generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        return None
    try:
        return (datetime.now(timezone.utc) - generated).total_seconds()
    except (TypeError, ValueError, OSError):
        return None

_QUALITY_CARD_ENTRY_PATTERN = re.compile(
    r'<span class="quality-card-entry">.*?</span>', re.DOTALL
)


def _without_quality_card_entry(html: str) -> str:
    """The full page already exposes the inline 完整市场指标 entry."""
    return _QUALITY_CARD_ENTRY_PATTERN.sub("", str(html or ""), count=1)


_READING_WINDOW_START = re.compile(
    r'\s*<div class="home-news-reading"[^>]*>\s*<div\b[^>]*>\s*<div>'
    r'\s*<div class="home-news-window"[^>]*>[^<]*(?=</div>|<span\b)'
)


def _prepend_stale_banner(
    html: str,
    generated_at: str,
    age_seconds: float,
    *,
    reason: str = "expired",
) -> str:
    """Keep stale content, without adding a positional homepage grid item."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    raw = str(generated_at or "").strip()
    try:
        local = (
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .strftime("%m-%d %H:%M")
        )
    except (ValueError, OSError):
        local = "时间未知"
    if reason == "future":
        claim = f'快照时间异常：生成于 {escape_xml(local)}（晚于当前时间）；'
    else:
        claim = (
            f'数据已过期：快照生成于 {escape_xml(local)}，'
            '已超过预期刷新间隔；'
        )
    banner = (
        '<div class="dashboard-snapshot-warning" role="status" '
        'style="padding:10px 12px;margin-bottom:10px;border:1px solid '
        'rgba(148,163,184,.4);border-radius:10px;font-size:12px;line-height:1.5;'
        'color:var(--color-text-subdue,inherit);overflow-wrap:anywhere;">'
        f'{claim}当前数据状态未知，以下为最近一次成功快照，仅供参考。</div>'
    )
    html = str(html or "")
    window = _READING_WINDOW_START.match(html)
    if window is not None:
        # The reading shell is flattened with display:contents in Glance.
        # A new root sibling shifts the sidebar's row and separates feature
        # cards. Nest the notice in the existing full-width edition header.
        return html[:window.end()] + banner + html[window.end():]
    return banner + html


def _event_digest_calendar_state(html: str) -> tuple[str, bool]:
    """A yesterday digest is not today's news even inside the age allowance.

    Inspect only published HTML. Never query data or rewrite its date/week
    links to the current date while continuing to show the previous content.
    Legacy radar artifacts have no digest marker and retain their semantics.
    """
    from zoneinfo import ZoneInfo

    marker = re.search(r'\bdata-report-date="([^"]*)"', html)
    if marker is None:
        return html, False
    day = marker.group(1)
    today = datetime.fromtimestamp(time.time(), ZoneInfo("Asia/Shanghai")).date().isoformat()
    if day == today:
        return html, False
    warning = (
        '<div class="dashboard-snapshot-warning" role="status" '
        'style="padding:10px;overflow-wrap:anywhere;font-size:12px;">'
        '事件日期与今天不符：以下保留原日期快照，今日变化待刷新；'
        '当前数据状态未知。请核对卡片日期与截止时间。</div>'
    )
    return warning + html, True


def _prepend_realtime_snapshot_time(html: str, generated_at: str) -> str:
    """Annotate the existing reading header, never add a top-level sibling.

    Glance applies positional styles to the widget's root elements.  Unknown
    or legacy markup is therefore left byte-for-byte unchanged; its timestamp
    remains available in the response header.
    """
    from zoneinfo import ZoneInfo

    window = _READING_WINDOW_START.match(html)
    if window is None:
        return html
    try:
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("snapshot timestamp must have a timezone")
        local = stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        local = "时间未知"
    annotation = (
        '<span class="dashboard-snapshot-time" style="display:block;font-size:11px;'
        'line-height:1.7;color:var(--color-text-subdue,inherit);">'
        f'页面数据快照：{escape_xml(local)}（北京时间；非新闻发布时间）</span>'
    )
    return html[:window.end()] + annotation + html[window.end():]


_INGESTION_COVERAGE = re.compile(
    r'<span class="dashboard-ingestion-coverage"(?P<attributes>[^>]*)>[^<]*</span>'
)
_INGESTION_STATE = re.compile(r'\bdata-state="([^"]*)"')


def _insert_realtime_ingestion_coverage(html: str, state: str, text: str) -> tuple[str, str]:
    """Publish coverage inside the edition header, not a new grid sibling."""
    window = _READING_WINDOW_START.match(html)
    if window is None:
        return html, "unknown"
    annotation = (
        f'<span class="dashboard-ingestion-coverage" data-state="{state}" role="status" '
        'style="display:block;font-size:12px;line-height:1.7;max-width:100%;'
        'white-space:normal;overflow-wrap:anywhere;'
        'color:var(--color-text-subdue,inherit);">'
        f'{escape_xml(text)}（随页面快照发布）</span>'
    )
    return html[:window.end()] + annotation + html[window.end():], state


def _prepend_realtime_ingestion_coverage(html: str) -> tuple[str, str]:
    """Worker/live builds read once; production Web uses the published marker."""
    from services.ingestion_status import read_refresh_coverage

    state, text = read_refresh_coverage()
    return _insert_realtime_ingestion_coverage(html, state, text)


def _published_realtime_ingestion_coverage(html: str) -> tuple[str, str]:
    """Only infer whitelisted state from the digest-validated snapshot HTML."""
    from services.ingestion_status import describe_refresh_coverage

    markers = list(_INGESTION_COVERAGE.finditer(html))
    if len(markers) == 1:
        states = list(_INGESTION_STATE.finditer(markers[0].group("attributes")))
        if len(states) == 1 and states[0].group(1) in {"complete", "incomplete", "unknown", "noop"}:
            return html, states[0].group(1)
    # Old/invalid artifacts have no trustworthy coverage. Never query DB on
    # the Web path or preserve a contradictory completeness claim.
    html = _INGESTION_COVERAGE.sub("", html)
    state, text = describe_refresh_coverage(None)
    return _insert_realtime_ingestion_coverage(html, state, text)


def _dashboard_snapshot_response(
    card: str,
    *,
    title: str,
    cache_seconds: int,
    expected_contract: tuple[str, str] | None = None,
    drop_card_entry_link: bool = False,
) -> Response | None:
    """Return a worker-published card snapshot in the Web role.

    Tests and the local all-in-one process retain the existing live behavior.
    Production Web requests never compute a snapshot: a missing, corrupt, or
    wrong-contract artifact becomes a fast 200 placeholder while the durable
    worker keeps the previous successful artifact untouched.
    """
    from services.shared_cache import process_role

    configured = str(
        os.environ.get("AI_FILTER_DASHBOARD_SNAPSHOTS") or ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if not configured or process_role() != "web":
        return None

    try:
        from services.dashboard_snapshots import read_dashboard_snapshot

        snapshot = read_dashboard_snapshot(
            card,
            expected_contract=expected_contract,
        )
    except Exception:
        logger.exception("Dashboard snapshot read failed for %s", card)
        snapshot = None

    if snapshot is None:
        html = (
            '<div style="padding:22px;text-align:center;border:1px dashed '
            'rgba(148,163,184,.35);border-radius:14px;color:'
            'var(--color-text-subdue,inherit);font-size:12px;line-height:1.6;">'
            '后台快照正在生成；最近成功版本暂不可用。'
            '</div>'
        )
        state = "unavailable"
        generated_at = ""
    else:
        html = str(snapshot["html"])
        if drop_card_entry_link:
            html = _without_quality_card_entry(html)
        generated_at = str(snapshot.get("generated_at") or "")
        if card == "realtime":
            html = _prepend_realtime_snapshot_time(html, generated_at)
            html, ingestion_state = _published_realtime_ingestion_coverage(html)
        age_seconds = _snapshot_age_seconds(generated_at)
        if age_seconds is None:
            state = "ready"
        elif age_seconds < 0:
            # A future timestamp is not freshness evidence; the writer and
            # reader both reject normal future stamps, so reaching here means
            # an untrustworthy artifact and it must not read as ready.
            html = _prepend_stale_banner(
                html, generated_at, age_seconds, reason="future"
            )
            state = "stale"
        elif age_seconds > _snapshot_max_age_seconds():
            html = _prepend_stale_banner(
                html, generated_at, age_seconds, reason="expired"
            )
            state = "stale"
        else:
            state = "ready"
        if card == "event_radar":
            html, date_stale = _event_digest_calendar_state(html)
            if date_stale:
                state = "stale"

    response = Response(html, mimetype="text/html")
    response.headers["Widget-Title"] = title
    response.headers["Widget-Content-Type"] = "html"
    response.headers["Cache-Control"] = (
        f"public, max-age={max(1, int(cache_seconds))}"
    )
    response.headers["X-Card-Snapshot-State"] = state
    if card == "realtime" and snapshot is not None:
        response.headers["X-Card-Ingestion-State"] = ingestion_state
    if generated_at:
        response.headers["X-Card-Snapshot-Generated-At"] = generated_at
    return response


def _backtest_cache_contract() -> tuple[str, str] | None:
    """Return the live score/mapping contract used to isolate cached audits.

    A contract lookup failure disables the server cache for that request.  A
    result from an unidentified contract must never be reused as if it belonged
    to a later scoring fingerprint or asset-mapping implementation.
    """
    try:
        cfg = resolve_scoring_contract(load_config(), register_snapshot=False)
        strategy_version = str(cfg.get("scoring_strategy_version") or "").strip()
        mapping_version = str(get_mapping_version() or "").strip()
    except Exception:
        logger.warning(
            "Model-quality cache contract unavailable; bypassing cache",
            exc_info=True,
        )
        return None
    if not strategy_version or not mapping_version:
        return None
    return strategy_version, mapping_version


def _computed_backtest_contract(result: dict) -> tuple[str, str] | None:
    """Bind a completed score result to the exact contracts it evaluated.

    Either contract can change while a request is running. Cache and mapping
    assembly therefore follow both versions returned by the computation, never
    an optimistic pre-compute lookup or a later live mapping version.
    """
    strategy_version = str(result.get("strategy_version") or "").strip()
    mapping_version = str(result.get("mapping_version") or "").strip()
    if not strategy_version or not mapping_version:
        return None
    return strategy_version, mapping_version


def _computed_bridge_data_version(result: dict) -> str:
    """Bind a cached result to the bridge manifest it actually rendered."""
    comparison = result.get("audited_compatibility")
    if not isinstance(comparison, dict):
        return "none"
    preview = comparison.get("bridge_preview")
    if not isinstance(preview, dict):
        return "none"
    value = str(preview.get("data_version") or "").strip()
    return value or "none"


def _auth_error():
    return browser_error("Unauthorized", 401)


def _ops_cached_value(key, ttl_seconds, loader):
    """Bound repeated public widget reads without caching request objects."""
    now = time.monotonic()
    with _OPS_WIDGET_CACHE_LOCK:
        expired = [
            cache_key
            for cache_key, (expires_at, _value) in _OPS_WIDGET_CACHE.items()
            if expires_at <= now
        ]
        for cache_key in expired:
            _OPS_WIDGET_CACHE.pop(cache_key, None)

        cached = _OPS_WIDGET_CACHE.get(key)
        if cached is not None:
            return cached[1]

        # Compute under the same lock: these widgets are small and this avoids
        # a thundering herd issuing identical SQLite scans after expiry.
        value = loader()
        if len(_OPS_WIDGET_CACHE) >= _OPS_WIDGET_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _OPS_WIDGET_CACHE,
                key=lambda item: _OPS_WIDGET_CACHE[item][0],
            )
            _OPS_WIDGET_CACHE.pop(oldest_key, None)
        _OPS_WIDGET_CACHE[key] = (now + max(1.0, float(ttl_seconds)), value)
        return value


def _bool_arg(name: str) -> bool:
    return request.args.get(name, "false").strip().lower() == "true"


def _bounded_int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float_arg(name: str, default, minimum: float, maximum: float):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(minimum, min(maximum, value))


def _briefing_default_hours(cfg=None):
    """Return the one bounded default window shared by every Briefing view."""
    cfg = _cfg() if cfg is None else cfg
    section = cfg.get("briefing") if isinstance(cfg, dict) else None
    section = section if isinstance(section, dict) else {}
    raw = section.get("default_hours", 24)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        hours = 24
    return max(1, min(168, hours))


def _briefing_theme_rules(cfg=None):
    cfg = _cfg() if cfg is None else cfg
    rules = cfg.get("briefing_themes", []) or []
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        # Compatibility-only union themes remain valid RSS subscriptions, but
        # must never become a display/quota bucket in 今日情报 Briefing.
        if bool(rule.get("subscription_only")):
            continue
        name = str(rule.get("name") or "").strip()
        keywords = [str(k).strip() for k in (rule.get("keywords") or []) if str(k).strip()]
        if name and keywords:
            normalized.append((name, keywords))
    return normalized


def _briefing_article_text(article):
    return " ".join(
        str(article.get(key) or "")
        for key in ("title", "raw_title", "tag", "logic", "summary")
    )


def _briefing_source_text(article):
    """Publisher-owned text used for geographic and factual boundaries."""
    return " ".join(
        str(article.get(key) or "") for key in ("title", "raw_title")
    )


_BRIEFING_DOMESTIC_MACRO_SOURCE_ANCHORS = (
    "中国央行", "中国人民银行", "人民银行", "央行", "逆回购", "MLF", "LPR",
    "降准", "降息", "社融", "M1", "M2", "净投放", "货币政策", "人民币",
    "国务院", "国常会", "政治局", "财政部", "证监会", "外汇管理局",
    "国家发展改革委", "发改委", "国家金融监督管理总局", "金融监管总局",
    "中央金融工作会议", "中央汇金", "资本市场", "稳增长", "宏观调控",
    "公开市场操作", "基础货币", "存款准备金", "利率互换", "财政政策",
    "跨境人民币", "互联互通", "国债期货",
)


def _has_broad_macro_tag(article):
    return is_broad_macro_theme_tag(article.get("tag"))


def _has_supported_briefing_source_evidence(article, cfg=None):
    """Validate broad model tags against publisher-owned title fields."""
    source_view = {
        key: article.get(key)
        for key in ("title", "raw_title", "tag")
    }
    if classify_policy_region(source_view) in {"foreign", "mixed"}:
        return False
    if _is_a_share_market_stress(source_view) or _is_consumption_policy(source_view):
        return True
    source_text = " ".join(
        str(article.get(key) or "") for key in ("title", "raw_title")
    )
    if any(anchor in source_text for anchor in _BRIEFING_DOMESTIC_MACRO_SOURCE_ANCHORS):
        return True
    return any(
        keyword in source_text
        for _theme, keywords in _briefing_theme_rules(cfg)
        for keyword in keywords
    )


def _is_unsupported_broad_macro_tag(article, cfg=None):
    return (
        _has_broad_macro_tag(article)
        and not _has_supported_briefing_source_evidence(article, cfg=cfg)
    )


def _is_a_share_market_stress(article):
    return is_a_share_market_stress(article)


def _is_consumption_policy(article):
    return is_consumption_policy(article)


def _briefing_direction_for_article(article, raw_direction=None):
    raw = article.get("direction") if raw_direction is None else raw_direction
    direction = normalize_direction(raw or "", text=_briefing_article_text(article))
    # “指数跌超/集体收跌/普跌”是客观价格方向。仅在模型给出中性或旧数据
    # 缺失方向时做受限纠偏；显式看多/看空仍由结构化结果优先。
    if direction == "中性" and _is_a_share_market_stress(article):
        return "看空"
    return direction


def _briefing_theme_for_article(article, cfg=None):
    # Market-wide moves and one national policy event often mention several
    # sectors.  Resolve these structural themes before broad configurable
    # keywords such as “涨停” or “AI”, otherwise one event is split or mislabeled.
    source_view = {
        key: article.get(key)
        for key in ("title", "raw_title")
    }
    if _is_a_share_market_stress(source_view):
        return "A股极端行情"
    if _is_consumption_policy(source_view):
        return "扩内需政策"
    # Geographic identity is authoritative before broad terms such as 央行、
    # 降息、财政政策 are compared with configured domestic theme keywords.
    policy_region = classify_policy_region(source_view)
    if policy_region == "foreign":
        return "海外宏观政策"
    if policy_region == "mixed":
        return "海外宏观与市场联动"
    text = article_primary_theme_text(article, cfg)
    for theme, keywords in _briefing_theme_rules(cfg):
        if any(keyword in text for keyword in keywords):
            return canonical_theme_name(theme)
    tag = str(article.get("tag") or "").strip()
    if tag and tag not in {"FastPass", "未知"} and not tag.isdigit():
        if is_subscription_only_theme(cfg, tag):
            return "其他高分情报"
        compact_tag = tag.replace(" ", "")
        if any(alias in compact_tag for alias in ("AI算力", "算力基建", "数字中国")):
            return canonical_theme_name("AI 算力链")
        if "国家级" in compact_tag and "政策" in compact_tag:
            return canonical_theme_name("国家产业政策")
        if _has_broad_macro_tag(article):
            return "国内宏观政策"
        return canonical_theme_name(tag)
    return "其他高分情报"


_BRIEFING_STRONG_OMNIBUS_PATTERNS = [
    "财经早知道", "早餐FM-Radio", "早餐FM Radio",
]

_BRIEFING_SOFT_OMNIBUS_PATTERNS = [
    "早知道", "早上好，今天是", "24小时热点", "过去24小时", "新闻汇总",
    "一网打尽", "今日要闻", "隔夜要闻", "早间新闻精选", "新闻精选",
    "盘前必读", "晨报", "早报", "早餐",
]

_BRIEFING_OMNIBUS_STRUCTURE_PATTERNS = [
    "十大消息", "十件大事", "要闻一览", "消息一览", "新闻汇总", "多条消息",
]

_BRIEFING_OMNIBUS_TOPIC_MARKERS = [
    "中俄", "中美", "商务部", "财政", "税收", "个税", "印花税", "央行", "美联储",
    "英伟达", "AI", "人工智能", "算力", "黄金", "原油", "房地产", "股市", "债券",
]


def _is_omnibus_briefing_article(article):
    title = f"{article.get('title', '')} {article.get('raw_title', '')}"
    if any(pattern in title for pattern in _BRIEFING_STRONG_OMNIBUS_PATTERNS):
        return True
    text = f"{title} {article.get('summary', '')}"
    if not any(pattern in text for pattern in _BRIEFING_SOFT_OMNIBUS_PATTERNS):
        return False
    if any(pattern in text for pattern in _BRIEFING_OMNIBUS_STRUCTURE_PATTERNS):
        return True
    marker_count = sum(1 for marker in _BRIEFING_OMNIBUS_TOPIC_MARKERS if marker in text)
    # One monetary event can naturally contain two broad markers.  “央行购金”
    # is not an omnibus merely because both 央行 and 黄金 occur in its title.
    if (
        marker_count == 2
        and "央行" in text
        and "黄金" in text
        and any(pattern in text for pattern in ("购金", "增持黄金", "黄金储备"))
    ):
        return False
    return marker_count >= 2


def _article_has_theme_contamination(article):
    title = str(article.get("title") or "")
    logic = str(article.get("logic") or "")
    summary = str(article.get("summary") or "")
    ai_hit = any(k in f"{title} {logic} {summary}" for k in ["英伟达", "NVIDIA", "AI", "人工智能", "算力", "GPU", "光模块"])
    macro_hit = any(k in f"{title} {logic} {summary}" for k in ["中俄", "中美", "商务部", "财政", "税收", "个税", "印花税", "央行", "美联储"])
    return ai_hit and macro_hit and _is_omnibus_briefing_article(article)


_BRIEFING_A_SHARE_ANCHORS = [
    "A股", "中国央行", "中国人民银行", "人民银行", "央行今日", "央行开展",
    "北向资金", "社融", "M2", "M1", "人民币", "沪深", "上证", "深证", "创业板",
    "科创板", "港股", "中概", "国常会", "政治局", "证监会", "财政部", "商务部",
]

_BRIEFING_FOREIGN_MACRO_NOISE_PATTERNS = [
    "美联储隔夜逆回购", "隔夜逆回购协议", "固定利率逆回购", "RRP", "交易对手",
    "美联储", "美国5月CPI", "美国CPI", "美核心通胀", "核心通胀", "CPI创",
    "加息概率大于降息", "短期内加息概率", "不降息、不加息", "不加息也不降息",
    "既不加息也不降息", "降息概率", "加息概率", "降息梦", "能源冲击延续",
    "加拿大央行", "欧洲央行", "高盛：", "Apollo联席总裁", "宏观脱水",
]


def _has_a_share_anchor(text):
    return any(anchor in text for anchor in _BRIEFING_A_SHARE_ANCHORS)


def _is_foreign_macro_briefing_noise(article):
    text = _briefing_source_text(article)
    region = classify_policy_region(article)
    if region == "mixed" or _has_a_share_anchor(text):
        return False
    if region == "foreign":
        return True
    return any(
        pattern in text for pattern in _BRIEFING_FOREIGN_MACRO_NOISE_PATTERNS
    )


_BRIEFING_AI_CHAIN_ANCHORS = [
    "算力", "光模块", "光电芯片", "芯片", "半导体", "国产替代", "服务器",
    "资本开支", "数据中心", "液冷", "PCB", "CPO", "英伟达", "NVIDIA", "甲骨文",
]

_BRIEFING_PURE_FOREIGN_TECH_PATTERNS = [
    "OpenAI据悉", "俄亥俄州", "租赁", "海外数据中心",
]


def _is_pure_foreign_tech_noise(article):
    text = " ".join(str(article.get(k) or "") for k in ("title", "raw_title", "tag", "logic", "summary"))
    if _has_a_share_anchor(text):
        return False
    if not any(pattern in text for pattern in _BRIEFING_PURE_FOREIGN_TECH_PATTERNS):
        return False
    return not any(anchor in text for anchor in _BRIEFING_AI_CHAIN_ANCHORS)


def _is_briefing_noise(article, cfg=None):
    if not isinstance(article, dict):
        return False
    if _is_omnibus_briefing_article(article):
        return True
    if _is_unsupported_broad_macro_tag(article, cfg=cfg):
        return True
    if _article_has_theme_contamination(article):
        return True
    if _is_foreign_macro_briefing_noise(article):
        return True
    if _is_pure_foreign_tech_noise(article):
        return True
    text = f"{article.get('title', '')} {article.get('tag', '')} {article.get('logic', '')}"
    hard_noise_patterns = ["ETF", "LOF", "赎回业务", "申购", "中签", "可转债", "股权登记", "除权除息"]
    if any(pattern in text for pattern in hard_noise_patterns):
        return True
    noise_patterns = [
        "减持", "停牌", "复牌", "撤销退市风险警示", "风险警示", "简称变更",
        "ST", "*ST", "回购进展",
    ]
    protect_patterns = ["存储", "SSD", "长鑫", "兆易创新", "大普微", "华工科技", "黄金", "债券", "央行"]
    return any(pattern in text for pattern in noise_patterns) and not any(pattern in text for pattern in protect_patterns)


def _briefing_representative_articles(group, limit=3):
    return briefing_representative_articles(group, limit=limit)


def _briefing_theme_insight(group, max_score):
    event_count = len(group)
    update_count = sum(max(1, int(article.get("duplicate_count") or 1)) for article in group)
    if event_count == 1:
        return f"单条 {max_score:.1f} 分信号，处于观察窗口"
    return f"{event_count} 个去重事件、{update_count} 条更新，最高 {max_score:.1f} 分"


def _briefing_why_now(group, hours):
    """Build a source-title-anchored reason without another model call."""
    top = group[0]
    title = format_article_display_title(
        str(top.get("title") or ""),
        str(top.get("raw_title") or "") or None,
    ).strip()
    if len(title) > 54:
        title = title[:53].rstrip() + "…"
    title = title or "该高分事件"
    return f"近{hours}小时出现“{title}”，已进入主题观察窗口"


def _briefing_overall_assessment(themes, hours):
    if not themes:
        return "暂无有效高分情报"
    summary = "；".join(
        f"{theme_display_name(theme.get('theme', '高分情报'))}"
        f"（{theme.get('direction', '中性')}）"
        for theme in themes[:3]
    )
    return f"近{hours}小时主线：{summary}"


def _briefing_market_impact(themes):
    directions = {theme.get("direction") for theme in themes}
    if "看多" in directions and "看空" in directions:
        return "政策利好与市场风险信号并存，优先观察指数修复、成交额和市场广度"
    if "看空" in directions:
        return "风险信号占优，优先观察市场广度、成交额和政策对冲是否改善"
    if "看多" in directions:
        return "利好信号占优，仍需用成交放大和价格趋势验证持续性"
    return "方向尚未形成共识，等待更多事件与资产价格反馈"


def _prepare_briefing_candidates(articles_info, *, limit, cfg=None):
    """Build the one shared, read-side corpus used by local and LLM Briefing."""
    cfg = _cfg() if cfg is None else cfg
    filtered_articles = []
    for article in articles_info:
        if _is_briefing_noise(article, cfg=cfg):
            continue
        normalized = dict(article)
        normalized["direction"] = _briefing_direction_for_article(normalized)
        filtered_articles.append(normalized)
    deduped = [
        enrich_article_analysis(article, trusted_only=True)
        for article in dedupe_articles(filtered_articles)
    ]
    selected, audit = assemble_briefing_candidates(
        deduped,
        cfg,
        limit=limit,
        theme_resolver=_briefing_theme_for_article,
    )
    primary_themes = list(audit.get("selected_primary_themes") or [])
    assembly_enabled = bool(audit.get("enabled"))
    prepared = []
    for index, article in enumerate(selected):
        cloned = dict(article)
        if assembly_enabled:
            cloned["briefing_article_id"] = f"a{index + 1}"
            cloned["briefing_theme"] = canonical_theme_name(
                primary_themes[index]
                if index < len(primary_themes) and primary_themes[index]
                else _briefing_theme_for_article(cloned, cfg=cfg)
            )
        else:
            for key in tuple(cloned):
                if key.startswith("briefing_"):
                    cloned.pop(key, None)
        prepared.append(cloned)
    audit = dict(audit)
    excluded = audit.get("excluded") if isinstance(audit.get("excluded"), dict) else {}
    quota_suppressed = int(excluded.get("theme_limit") or 0) + int(
        excluded.get("theme_cap") or 0
    )
    audit.update({
        "raw_input_count": len(articles_info or []),
        "noise_excluded_count": len(articles_info or []) - len(filtered_articles),
        "deduplicated_count": len(deduped),
        "quota_suppressed_count": quota_suppressed,
        "limit_suppressed_count": int(excluded.get("target_limit") or 0),
        "candidate_multiplier": briefing_candidate_multiplier(cfg),
        "grounding_policy": normalize_briefing_grounding_policy(cfg),
        "scope": "briefing_presentation_only",
    })
    return prepared, audit


def _local_briefing_from_articles(articles_info, hours=24, limit=None, cfg=None):
    from collections import defaultdict
    cfg = _cfg() if cfg is None else cfg
    requested_limit = len(articles_info or []) if limit is None else max(0, int(limit))
    filtered_articles, assembly_audit = _prepare_briefing_candidates(
        articles_info,
        limit=requested_limit,
        cfg=cfg,
    )
    assembly_enabled = bool(assembly_audit.get("enabled"))
    groups = defaultdict(list)
    for article in filtered_articles:
        theme = (
            str(article.get("briefing_theme") or "其他高分情报")
            if assembly_enabled
            else _briefing_theme_for_article(article, cfg=cfg)
        )
        groups[theme].append(article)
    themes = []
    for tag, group in groups.items():
        group = sorted(
            group,
            key=lambda article: (
                float(article.get("score") or 0),
                int(article.get("created_at") or 0),
            ),
            reverse=True,
        )
        avg_score = sum(float(a.get("score") or 0) for a in group) / max(1, len(group))
        max_score = max(float(a.get("score") or 0) for a in group)
        top = group[0]
        analysis = enrich_article_analysis(top, trusted_only=True)
        source_metrics = briefing_source_metrics(group)
        source_count = source_metrics["source_count"]
        top_event_source_count = source_metrics["top_event_source_count"]
        impact_parts = []
        if analysis.get("assets"):
            impact_parts.append(f"影响资产：{analysis['assets']}")
        if analysis.get("sectors"):
            impact_parts.append(f"关注板块：{analysis['sectors']}")
        from core.digest_engine import infer_theme_direction
        theme_result = {
            "theme": tag,
            "theme_display_name": theme_display_name(tag),
            "direction": infer_theme_direction(group),
            "importance": round(max_score * 0.7 + avg_score * 0.3, 1),
            "insight": _briefing_theme_insight(group, max_score),
            "source_confirmation": briefing_source_confirmation(group),
            "action": analysis.get("watch", "关注后续验证"),
            # The score rationale belongs to the scoring card.  Repeating it
            # here made “为什么现在重要” and “评分理由” indistinguishable.
            "background": "",
            "why_it_matters": _briefing_why_now(group, hours),
            "market_impact": "；".join(impact_parts) or "等待资产价格确认",
            "articles": _briefing_representative_articles(group, limit=3),
            "signal_count": sum(max(1, int(a.get("duplicate_count") or 1)) for a in group),
            "source_count": source_count,
            "top_event_source_count": top_event_source_count,
            "latest_at": max(int(a.get("created_at") or 0) for a in group),
        }
        if assembly_enabled:
            theme_result["grounding"] = {
                "status": "local_derived",
                "fields": {
                    field: {"status": "local_derived", "reason": "deterministic_rule"}
                    for field in ("insight", "why_it_matters", "market_impact", "action")
                },
            }
            theme_result["direction_validation"] = {
                "status": "local_consensus",
                "evidence_direction": infer_theme_direction(group),
            }
        themes.append(theme_result)
    themes.sort(key=briefing_theme_sort_key, reverse=True)
    theme_limit = normalize_briefing_policy(cfg)["max_themes"] if assembly_enabled else 4
    themes = themes[:theme_limit]
    bearish = next(
        (
            theme for theme in themes
            if theme.get("direction") == "看空" and float(theme.get("importance") or 0) >= 8.0
        ),
        None,
    )
    result = {
        "themes": themes,
        "overall_assessment": _briefing_overall_assessment(themes, hours),
        "market_impact": _briefing_market_impact(themes) if filtered_articles else "",
        "background": "",
        "why_it_matters": (
            "仅汇总入选主题及其来源支持情况" if assembly_enabled else ""
        ),
        "risk_alert": (
            f"{bearish.get('theme')}出现高分看空信号，注意下跌扩散与流动性压力"
            if bearish else ""
        ),
        "action_hint": "优先查看近期高分主题及其价格反馈",
        "confidence": min(0.86, 0.5 + 0.03 * len(filtered_articles)) if filtered_articles else 0.0,
        "article_count": len(filtered_articles),
        "data_as_of": (
            beijing_fromtimestamp(max(int(a.get("created_at") or 0) for a in filtered_articles)).isoformat()
            if filtered_articles else ""
        ),
        "generated_at": beijing_now().isoformat(),
    }
    if assembly_enabled:
        result.update({
            "briefing_input_count": len(filtered_articles),
            "candidate_count": int(assembly_audit.get("deduplicated_count") or 0),
            "quota_suppressed_count": int(
                assembly_audit.get("quota_suppressed_count") or 0
            ),
            "briefing_assembly": assembly_audit,
            "briefing_policy_fingerprint": briefing_policy_fingerprint(cfg),
            "source": "local",
            "articles": filtered_articles,
        })
    return result


def _is_clean_summary_article(row):
    link, title, raw_title, score, tag, logic, summary, created_at = row[:8]
    tag_text = str(tag or "").strip()
    summary_text = str(summary or "").strip()
    title_text = str(title or "")
    if not summary_text or len(summary_text) < 18:
        return False
    if tag_text in {"无", "无关", "无相关", "无相关标签"}:
        return False
    if tag_text.isdigit() or tag_text in {"|`", "未知"}:
        return False
    if summary_text.count("1") > max(8, len(summary_text) // 5):
        return False
    hard_noise = ["ETF", "LOF", "赎回业务", "申购", "中签", "股权登记", "除权除息"]
    if any(x in title_text for x in hard_noise):
        return False
    return True


def _briefing_article_from_row(row):
    raw_direction = row[9] if len(row) > 9 else ""
    article = {
        "link": row[0],
        "title": format_article_display_title(row[1], row[2]),
        "raw_title": row[2],
        "score": row[3],
        "tag": row[4],
        "logic": row[5],
        "summary": row[6],
        "created_at": row[7],
        "feed_url": row[8] if len(row) > 8 else "",
        "event_id": row[10] if len(row) > 10 else "",
        "event_version": int(row[11] or 0) if len(row) > 11 else 0,
    }
    article["direction"] = _briefing_direction_for_article(article, raw_direction=raw_direction)
    return article


def _briefing_rows_fingerprint(rows):
    payload = [
        [str(value or "") for value in row[:12]]
        for row in rows
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _briefing_llm_cache_name(hours, min_score, limit, feed_url, _rows=None):
    cfg = _cfg()
    identity = {
        "cache_version": _BRIEFING_CACHE_VERSION,
        "cache_mode": "stable-ttl-v1",
        "hours": int(hours),
        "min_score": min_score,
        "limit": int(limit),
        "feed_url": str(feed_url or ""),
        "strategy": str(cfg.get("scoring_strategy_version") or ""),
        "briefing_policy": briefing_policy_fingerprint(cfg),
    }
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "briefing:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _get_cached_briefing_llm(cache_name):
    from services.shared_cache import get_shared_json

    cached = get_shared_json(cache_name)
    if cached and isinstance(cached.get("result"), dict):
        return cached["result"]
    return None


def _queue_briefing_llm_refresh(hours, min_score, limit, feed_url, _rows=None):
    from routes.webhook_routes import _submit_background_task

    task = {
        "task_type": "briefing_refresh",
        "cache_version": _BRIEFING_CACHE_VERSION,
        "request_window": int(time.time() // _BRIEFING_LLM_CACHE_TTL_SEC),
        "hours": int(hours),
        "min_score": min_score,
        "limit": int(limit),
        "feed_url": str(feed_url or ""),
        "briefing_policy": briefing_policy_fingerprint(_cfg()),
    }
    outcome = _submit_background_task(task)
    if outcome == "full":
        logger.warning("[Briefing] durable background queue is full")
    return outcome


def _is_allowed_briefing_feed(feed_url):
    feed_url = str(feed_url or "").strip()
    if not feed_url:
        return True
    cfg = _cfg()
    allowed = {
        str(feed.get("url") or "").strip()
        for feed in (cfg.get("feeds") or [])
        if str(feed.get("url") or "").strip()
    }
    aliases = cfg.get("_runtime_feed_url_aliases") or {}
    if isinstance(aliases, dict):
        for canonical, originals in aliases.items():
            allowed.add(str(canonical or "").strip())
            if isinstance(originals, (list, tuple)):
                allowed.update(str(value or "").strip() for value in originals)
    return feed_url in allowed


def _select_briefing_result(rows, articles_info, *, hours, min_score, limit, feed_url, use_llm):
    local = _local_briefing_from_articles(
        articles_info,
        hours=hours,
        limit=limit,
        cfg=_cfg(),
    )
    if not use_llm:
        return local, "local"
    if not rows:
        return local, "empty"
    if feed_url and not _is_allowed_briefing_feed(feed_url):
        logger.warning("[Briefing] rejected unconfigured feed filter from LLM queue")
        return local, "invalid-feed"
    cache_name = _briefing_llm_cache_name(hours, min_score, limit, feed_url, rows)
    cached = _get_cached_briefing_llm(cache_name)
    if cached is not None:
        return cached, "hit"
    outcome = _queue_briefing_llm_refresh(hours, min_score, limit, feed_url, rows)
    return local, outcome


def run_briefing_refresh_command(task):
    from services.admission_fence import admission, current_kind

    with admission("briefing_worker", kind=current_kind()):
        return _run_briefing_refresh_admitted(task)


def _run_briefing_refresh_admitted(task):
    """Worker-only command: compute LLM Briefing and publish it via Redis."""
    from services.shared_cache import set_shared_json

    if int(task.get("cache_version") or 0) != _BRIEFING_CACHE_VERSION:
        logger.info("[Briefing] discarded stale cache-version task")
        return True
    cfg = _cfg()
    task_policy = str(task.get("briefing_policy") or "").strip()
    current_policy = briefing_policy_fingerprint(cfg)
    if task_policy and task_policy != current_policy:
        logger.info("[Briefing] discarded stale policy-fingerprint task")
        return True
    hours = max(
        1,
        min(168, int(task.get("hours") or _briefing_default_hours(cfg))),
    )
    limit = max(1, min(50, int(task.get("limit") or 12)))
    raw_min_score = task.get("min_score")
    min_score = None if raw_min_score is None else max(0.0, min(10.0, float(raw_min_score)))
    feed_url = str(task.get("feed_url") or "")[:2048]
    if feed_url and not _is_allowed_briefing_feed(feed_url):
        logger.warning("[Briefing] rejected worker task with unconfigured feed filter")
        return True
    rows = _get_briefing_articles(
        min_score=min_score,
        limit=limit,
        hours=hours,
        feed_url=feed_url or None,
        overfetch=True,
    )
    articles = []
    for row in rows:
        article = _briefing_article_from_row(row)
        article["time"] = (
            beijing_fromtimestamp(article["created_at"]).strftime("%m-%d %H:%M")
            if article.get("created_at") else ""
        )
        articles.append(article)
    enriched, assembly_audit = _prepare_briefing_candidates(
        articles,
        limit=limit,
        cfg=cfg,
    )
    cache_name = _briefing_llm_cache_name(hours, min_score, limit, feed_url, rows)
    if _get_cached_briefing_llm(cache_name) is not None:
        return True
    result = aggregate_themes(
        enriched,
        scorer=scorer,
        period_hours=hours,
        cfg=cfg,
        limit=limit,
        assembly_audit=assembly_audit,
    )
    if result.get("source") != "llm":
        logger.warning("[Briefing] LLM aggregation degraded; retrying without caching fallback")
        return False
    result.update({
        "period_hours": hours,
        "min_score": min_score,
        "status": "ok",
        "candidate_fingerprint": _briefing_rows_fingerprint(rows),
    })
    return set_shared_json(
        cache_name,
        {"result": result},
        _BRIEFING_LLM_CACHE_TTL_SEC,
    )


def _briefing_tuple_from_article(article):
    return (
        article.get("link", ""),
        article.get("title", ""),
        article.get("raw_title", ""),
        article.get("score"),
        article.get("tag", ""),
        article.get("logic", ""),
        article.get("summary", ""),
        article.get("created_at"),
        article.get("feed_url", ""),
        normalize_direction(article.get("direction", ""), text=article.get("title", "")),
        article.get("event_id", ""),
        int(article.get("event_version") or 0),
    )


def _get_dashboard_summary_articles(min_score=None, limit=12, hours=48):
    """Dashboard AI 智能总结：跨策略展示近 48 小时可读高分总结。"""
    rows = DB.get_high_score_articles_with_summary(
        min_score=min_score, limit=limit * 4, hours=hours, cfg=_cfg()
    )
    clean_rows = [tuple(r) for r in rows if _is_clean_summary_article(tuple(r))]
    return clean_rows[:limit]


def _safe_dashboard_summary_articles(rows):
    """Return display-only copies whose link field is safe for an href."""
    safe_rows = []
    for row in rows or []:
        values = tuple(row)
        if values:
            values = (safe_http_url(values[0]), *values[1:])
        safe_rows.append(values)
    return safe_rows


def _get_recent_high_score_count(min_score=None, hours=48):
    """Dashboard 顶部统计：近 48 小时真实高分情报数，不要求已有 summary。"""
    cutoff = int(time.time()) - hours * 3600
    irrelevant_tags = {"无", "无关", "无相关", "无相关标签"}
    placeholders = ",".join("?" for _ in irrelevant_tags)
    score_paths = tuple(sorted(FORMAL_SIGNAL_SCORE_PATHS))
    path_placeholders = ",".join("?" for _ in score_paths)
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT score, feed_url FROM news_cache WHERE created_at > ? "
                f"AND COALESCE(tag, '') NOT IN ({placeholders}) "
                "AND LOWER(TRIM(COALESCE(score_path, ''))) IN "
                f"({path_placeholders})",
                (cutoff, *irrelevant_tags, *score_paths),
            ).fetchall()
    cfg = _cfg()
    count = 0
    for score, feed_url in rows:
        if score is None:
            continue
        threshold = float(min_score) if min_score is not None else DB.get_feed_thresholds(cfg, feed_url or "")["aggregation"]
        if float(score) >= threshold:
            count += 1
    return count


def _get_briefing_articles(
    min_score=None,
    limit=12,
    hours=24,
    feed_url=None,
    *,
    overfetch=False,
):
    """Briefing 专用数据源：统一情报查询层高分视图。"""
    from services.intelligence_query import query_briefing_tuples

    cfg = _cfg()
    multiplier = (
        briefing_candidate_multiplier(cfg)
        if overfetch and normalize_briefing_policy(cfg)["enabled"]
        else 1
    )
    query_limit = min(500, max(1, int(limit)) * multiplier)
    # Keep the SQL scan budget approximately unchanged: the enlarged Python
    # candidate pool replaces, rather than multiplies, the historical x80 scan.
    sql_candidate_multiplier = max(1, 80 // multiplier)
    return query_briefing_tuples(
        cfg,
        min_score=min_score,
        limit=query_limit,
        hours=hours,
        feed_url=feed_url,
        noise_filter=lambda article: _is_briefing_noise(article, cfg=cfg),
        candidate_multiplier=sql_candidate_multiplier,
    )


_FEED_SCORE_REASON_LABELS = (
    ("passed", "正式达标"),
    ("below_threshold", "低于阈值"),
    ("similarity", "重复稿"),
    ("safe_contract", "格式/字段中性"),
    ("safe_grounding", "来源依据中性"),
    ("model_failure", "模型链路失败"),
    ("processing_error", "条目处理异常"),
    ("other_excluded", "其他未计入"),
)


def _feed_score_category(score_path, score, threshold, error_type=""):
    """Explain one cache row; model-failure paths are not formal acceptance.

    ``fallback_local`` means every LLM backend failed before the local
    fallback scored the item, so it must not be reported as 正式达标.
    """
    path = normalize_score_path(score_path)
    error = str(error_type or "").strip().casefold()
    if path == "similarity":
        return "similarity"
    if path in SAFE_NEUTRAL_CONTRACT_SCORE_PATHS.values():
        return "safe_contract"
    if path in SAFE_NEUTRAL_GROUNDING_SCORE_PATHS.values():
        return "safe_grounding"
    if path == "fallback_local" or error == "llm_backends_exhausted":
        return "model_failure"
    if path in {"parse_error", "quality_quarantine"} or error in {
        "parse_error",
        "score_error",
    }:
        return "processing_error"
    if path in FORMAL_SIGNAL_SCORE_PATHS:
        if score is None:
            return "other_excluded"
        return "passed" if float(score) >= threshold else "below_threshold"
    return "other_excluded"


def _summarize_feed_score_rows(rows, cfg):
    """Count only the current scoring contract; other contracts stay separate.

    Old-contract rejection counts must not be used to judge the running
    version, so rows whose ``strategy_version`` differs from the configured
    one go to ``other_contract_total`` and never enter the reason counts.
    """
    current_version = str(cfg.get("scoring_strategy_version") or "").strip()
    stats_map = {}
    for feed_url, score, created_at, score_path, error_type, strategy_version in rows:
        feed_url = feed_url or ""
        feed_cfg = DB.get_feed_config(cfg, feed_url)
        canonical_url = feed_cfg.get("url") or feed_url
        bucket = stats_map.setdefault(canonical_url, {
            "articles_total": 0, "articles_passed": 0,
            "other_contract_total": 0,
            "score_sum": 0.0, "score_count": 0, "last_check": 0,
            "counts": dict.fromkeys(dict(_FEED_SCORE_REASON_LABELS), 0),
        })
        version = str(strategy_version or "").strip()
        if current_version and version != current_version:
            bucket["other_contract_total"] += 1
            continue
        bucket["articles_total"] += 1
        category = _feed_score_category(
            score_path, score,
            DB.get_feed_thresholds(cfg, canonical_url)["aggregation"],
            error_type,
        )
        bucket["counts"][category] += 1
        if score is not None and normalize_score_path(score_path) in FORMAL_SIGNAL_SCORE_PATHS:
            bucket["score_sum"] += float(score)
            bucket["score_count"] += 1
        bucket["articles_passed"] = bucket["counts"]["passed"]
        bucket["last_check"] = max(bucket["last_check"], int(created_at or 0))
    for bucket in stats_map.values():
        count = bucket.pop("score_count")
        score_sum = bucket.pop("score_sum")
        bucket["avg_score"] = score_sum / count if count else None
    return stats_map


def _feed_zero_output_reason(bucket, health):
    """Say why a source produced nothing; unknown stays explicit.

    Old-contract rows are not "no new articles": the current contract has
    simply not scored anything yet, and that must be named as such.
    """
    if bucket.get("articles_passed"):
        return ""
    counts = bucket.get("counts") or {}
    total = int(bucket.get("articles_total") or 0)
    other_contract = int(bucket.get("other_contract_total") or 0)
    health = health or {}
    empty_checks = int(health.get("consecutive_empty_checks") or 0)
    last_nonempty = int(health.get("last_nonempty_at") or 0)
    reasons = []
    if total == 0:
        if other_contract:
            reasons.append(
                f"近24h暂无当前契约样本（其他评分契约 {other_contract} 条未计入）"
            )
            if empty_checks > 0:
                reasons.append(f"空源连击 {empty_checks} 次")
        elif empty_checks > 0:
            reasons.append(f"近24h无新文章（连续空检查 {empty_checks} 次）")
        elif last_nonempty > 0:
            reasons.append("近24h无新文章")
        else:
            reasons.append("近24h无新文章；原因未完整记录")
    else:
        for key, label in (
            ("below_threshold", "未达阈值"),
            ("similarity", "重复稿"),
            ("safe_contract", "格式/字段中性"),
            ("safe_grounding", "来源依据中性"),
            ("model_failure", "模型链路失败"),
            ("processing_error", "条目处理异常"),
            ("other_excluded", "其他未计入"),
        ):
            if counts.get(key):
                reasons.append(f"{label} {counts[key]}")
        if not reasons:
            reasons.append("原因未完整记录")
        if other_contract:
            reasons.append(f"其他评分契约 {other_contract} 条未计入")
    return "；".join(reasons)


def _feed_metric_detail(row, bucket, threshold, health=None):
    total, passed = row[6:8]
    counts = bucket.get("counts") or dict.fromkeys(dict(_FEED_SCORE_REASON_LABELS), 0)
    return {
        "threshold": threshold,
        "counts": counts,
        "rate_text": f"{round(passed / total * 100, 1)}%" if total else "暂无24h样本",
        "other_contract_total": int(bucket.get("other_contract_total") or 0),
        "zero_output_reason": _feed_zero_output_reason(bucket, health),
        "breakdown_text": " · ".join(
            f"{label} {counts[key]}" for key, label in _FEED_SCORE_REASON_LABELS
        ) if total else "未计入近24h主来源样本，不代表抓取失败",
    }


def _get_feed_health_snapshot():
    """Return source health and score explanations from the same cache rows.
    
    第一项保持与 DB.get_feed_health() 兼容的元组列表，第二项为按源解释：
    (url, name, last_check, response_time_ms, success, fail, articles_total,
     articles_passed, avg_score, last_error, consecutive_empty_checks,
     last_nonempty_at)
    """
    from db.database import db_lock, get_connection
    from core.config import config as _cfg
    
    cfg = _cfg.cfg
    feeds = cfg.get("feeds", [])
    
    # 获取各源24小时统计；通过数使用统一阈值网关在 Python 侧计算。
    # 同时读取 feed_health 作为“最近拉源是否成功”的兜底信号：官方/交易所源
    # 可能长期没有新文章或全部低分，不能因此被误报为源不可用。
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute('''
                SELECT feed_url, score, created_at, score_path, error_type,
                       strategy_version
                FROM news_cache
                WHERE created_at > strftime("%s", "now", "-24 hours")
            ''').fetchall()
            health_rows = conn.execute('''
                SELECT url, last_check, response_time_ms, success, fail, last_error,
                       consecutive_empty_checks, last_nonempty_at
                FROM feed_health
                ORDER BY last_check DESC
            ''').fetchall()

    def _safe_int(value, default=0):
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return default

    stats_map = _summarize_feed_score_rows(rows, cfg)
    health_map = {}
    for (
        health_url,
        last_check,
        response_ms,
        success,
        fail,
        last_error,
        consecutive_empty_checks,
        last_nonempty_at,
    ) in health_rows:
        health_url = health_url or ""
        feed_cfg = DB.get_feed_config(cfg, health_url)
        canonical_url = feed_cfg.get("url") or health_url
        if canonical_url in health_map:
            continue
        health_map[canonical_url] = {
            "last_check": _safe_int(last_check),
            "response_time_ms": _safe_int(response_ms),
            "success": _safe_int(success),
            "fail": _safe_int(fail),
            "last_error": last_error,
            "consecutive_empty_checks": _safe_int(consecutive_empty_checks),
            "last_nonempty_at": _safe_int(last_nonempty_at),
        }
    # 按 config 中 enabled 的 feeds 顺序构建返回列表，忽略已禁用或历史旧路由缓存。
    results = []
    for feed in feeds:
        url = feed.get("url", "")
        name = feed.get("name", url)
        if not url or not feed.get("enabled", True):
            continue
        s = stats_map.get(url, {})
        h = health_map.get(url, {})
        avg_score = s.get("avg_score", None)
        articles_total = s.get("articles_total", 0)
        last_check = max(s.get("last_check", 0), h.get("last_check", 0))
        success = h.get("success", 0) or (1 if articles_total else 0)
        results.append((
            url,
            name,
            last_check,
            h.get("response_time_ms", 0),
            success,
            h.get("fail", 0),
            articles_total,
            s.get("articles_passed", 0),
            round(avg_score, 2) if avg_score is not None else None,
            h.get("last_error"),
            h.get("consecutive_empty_checks", 0),
            h.get("last_nonempty_at", 0),
        ))
    
    empty_bucket = {
        "articles_total": 0,
        "articles_passed": 0,
        "other_contract_total": 0,
        "counts": dict.fromkeys(dict(_FEED_SCORE_REASON_LABELS), 0),
    }
    details = {
        row[0]: _feed_metric_detail(
            row,
            stats_map.get(row[0]) or empty_bucket,
            DB.get_feed_thresholds(cfg, row[0])["aggregation"],
            health_map.get(row[0], {}),
        ) for row in results
    }
    return results, details


def _get_health_from_news_cache():
    """Keep the existing 12-field health tuple contract for API consumers."""
    return _get_feed_health_snapshot()[0]


@dash_bp.route("/")
def index():
    cfg = _cfg()
    html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>AI-Filter</title>'
    html += '<style>body{font-family:sans-serif;max-width:600px;margin:40px auto;padding:20px;background:#0f0f1a;color:#e0e0e0}'
    html += 'h1{color:#f0c040}a{color:#4dabf7;text-decoration:none}a:hover{text-decoration:underline}'
    html += 'ul{line-height:2}</style></head><body>'
    html += '<h1>AI-Filter ' + html_escape(release_version()) + '</h1>'
    html += '<p>A-Share Macro Intelligence Filter | Local/Qwen first · Kimi deep review · configured fallback</p><ul>'
    html += '<li><a href="/health">/health</a> - Health Check</li>'
    html += '<li><a href="/dashboard">/dashboard</a> - Dashboard（含今日情报 Briefing）</li>'
    html += '<li><a href="/dashboard/events">/dashboard/events</a> - 统一事件雷达</li>'
    html += '<li><a href="/briefing?format=html">/briefing</a> - 今日情报 Briefing</li>'
    html += '<li><a href="/quotes">/quotes</a> - Market Data</li>'
    sample = (cfg.get("feeds") or [{}])[0].get("url", "http://127.0.0.1:1200/cls/depth")
    html += '<li><code>/filter?url=...</code> — 单源过滤（需 <code>X-API-Key</code> 或 <code>key=</code>，示例源：' + escape_xml(sample) + '）</li>'
    html += '<li><code>/filter/multi?urls=url1,url2</code> — 多源合并</li>'
    html += '</ul><p style="color:#888;font-size:12px;margin-top:40px">'
    thresholds = DB.get_feed_thresholds(cfg, "")
    similarity = (cfg.get("threshold") or {}).get("similarity", "N/A")
    html += 'Notify Threshold: ' + escape_xml(thresholds["notify"]) + ' | '
    html += 'Pass Threshold: ' + escape_xml(thresholds["pass"]) + ' | '
    html += 'Similarity: ' + escape_xml(similarity)
    html += '</p></body></html>'
    return Response(html, mimetype="text/html")


def _quotes_response(html):
    if request.args.getlist("view") == ["reading"]:
        from services.reading_market import fold_market_quotes

        html = fold_market_quotes(html)
    rsp = Response(html, mimetype="text/html")
    rsp.headers["Widget-Title"] = "Market Quotes"
    rsp.headers["Widget-Content-Type"] = "html"
    rsp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    rsp.headers["Pragma"] = "no-cache"
    rsp.headers["Expires"] = "0"
    return rsp


def _quotes_placeholder_html():
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;">'
        + '<div style="padding:14px 16px;border-radius:16px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 5%),.66);'
        + 'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.18);color:var(--color-text-subdue,inherit);font-size:12px;">'
        + '行情数据后台刷新中，不阻塞总控台加载。'
        + '</div></div>'
    )


_MARKET_QUOTE_SYMBOLS = ("sh000001", "sz399001", "sz399006", "usQQQ")
_MARKET_QUOTE_SYMBOL_KEYS = {symbol.casefold() for symbol in _MARKET_QUOTE_SYMBOLS}


def _quote_refresh_role_allowed() -> bool:
    """Production Web processes are cache readers and must never fetch quotes."""
    from services.shared_cache import process_role

    return process_role() in {"worker", "all"}


def _validated_market_snapshot(quotes, captured_at: int):
    """Return ordered quotes and DB rows only when the complete batch is valid."""
    import math

    if not isinstance(quotes, dict):
        raise ValueError("quote response must be a mapping")

    by_symbol = {}
    for quote in quotes.values():
        if not isinstance(quote, dict):
            raise ValueError("quote record must be a mapping")
        symbol = quote.get("symbol")
        if not isinstance(symbol, str):
            raise ValueError("quote symbol is missing")
        key = symbol.casefold()
        if key not in _MARKET_QUOTE_SYMBOL_KEYS or key in by_symbol:
            raise ValueError("unexpected or duplicate quote symbol")
        by_symbol[key] = quote

    if set(by_symbol) != _MARKET_QUOTE_SYMBOL_KEYS:
        raise ValueError("incomplete quote response")

    trade_date = beijing_fromtimestamp(captured_at).strftime("%Y-%m-%d")
    ordered = []
    rows = []
    for expected_symbol in _MARKET_QUOTE_SYMBOLS:
        quote = by_symbol[expected_symbol.casefold()]
        name = quote.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("quote name is missing")

        numeric = {}
        for field in ("price", "last_close", "change_amt", "change_pct"):
            value = quote.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid quote field: {field}")
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"non-finite quote field: {field}")
            numeric[field] = value
        if numeric["price"] <= 0 or numeric["last_close"] <= 0:
            raise ValueError("quote prices must be positive")

        normalized = {
            **quote,
            "symbol": expected_symbol,
            "name": name.strip(),
            **numeric,
        }
        ordered.append(normalized)
        rows.append(
            {
                "symbol": expected_symbol,
                "name": normalized["name"],
                "price": numeric["price"],
                "prev_close": numeric["last_close"],
                "change_amt": numeric["change_amt"],
                "change_pct": numeric["change_pct"],
                "ts": captured_at,
                "trade_date": trade_date,
            }
        )
    return ordered, rows


def _refresh_quotes_cache():
    from services.admission_fence import AdmissionClosed, admission

    try:
        with admission("quotes_poll", kind="intake"):
            return _refresh_quotes_admitted()
    except AdmissionClosed:
        return False


def _refresh_quotes_admitted():
    if not _quote_refresh_role_allowed():
        logger.error("[Quotes] web role attempted to start market refresh")
        return False
    if not _quotes_refresh_lock.acquire(blocking=False):
        return False
    try:
        from db.database import store_market_prices
        from modules.a_stock_data import tencent_quote
        from services.refresh import refresh_execution_lock
        from services.shared_cache import redis_enabled, set_shared_json

        captured_at = int(time.time())
        quotes = tencent_quote(list(_MARKET_QUOTE_SYMBOLS))
        ordered_quotes, snapshot_rows = _validated_market_snapshot(quotes, captured_at)

        cells = []
        for quote in ordered_quotes:
            is_up = quote["change_amt"] >= 0
            color_style = "color:#e74c3c" if is_up else "color:#3498db"
            sign = "\u25b2" if is_up else "\u25bc"
            cells.append(
                '<div style="background:linear-gradient(135deg,hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 7%),0.74),hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 2%),0.46));'
                "border-radius:18px;padding:13px 14px;"
                "border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 22%),0.16);"
                "box-shadow:0 10px 28px rgba(0,0,0,.16);"
                'min-width:0;backdrop-filter:blur(10px);">'
                '<div style="font-size:11px;color:var(--color-text-subdue,inherit);'
                'margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                + html_escape(quote["name"])
                + "</div>"
                '<div style="font-size:18px;font-weight:800;color:var(--color-text-base,inherit);'
                'letter-spacing:-0.035em;line-height:1.05;">'
                + html_escape(f'{quote["price"]:.2f}')
                + "</div>"
                '<div style="font-size:12px;margin-top:7px;font-weight:700;'
                + color_style
                + '">'
                + sign
                + " "
                + html_escape(f'{quote["change_amt"]:+.2f}')
                + ' <span style="opacity:0.78">('
                + html_escape(f'{quote["change_pct"]:+.2f}')
                + "%)</span></div></div>"
            )

        html = (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;">'
            + '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;">'
            + "".join(cells)
            + "</div></div>"
        )

        shared_ok = set_shared_json(
            "quotes",
            {"html": html, "ts": captured_at},
            int(QUOTES_CACHE_TTL_SEC * 2),
        )
        if redis_enabled() and not shared_ok:
            logger.warning("[Quotes] Redis shared-cache write failed")
            return False

        # Persist only after the full response and shared-cache publication pass.
        # Event enrichment holds this cross-process writer lock around its long
        # BEGIN IMMEDIATE transaction.  The compatibility table is a replaceable
        # latest snapshot, so defer one frame instead of waiting on SQLite and
        # turning an otherwise fresh Redis publication into a refresh failure.
        with refresh_execution_lock() as persistence_lock_acquired:
            if persistence_lock_acquired:
                store_market_prices(snapshot_rows)
            else:
                logger.info(
                    "[Quotes] SQLite persistence deferred: refresh writer busy"
                )
        with _quotes_cache_lock:
            _quotes_cache["html"] = html
            _quotes_cache["ts"] = captured_at
        return True
    except Exception as e:
        logger.warning("[Quotes] 行情后台刷新失败: %s", str(e)[:120])
        return False
    finally:
        _quotes_refresh_lock.release()


# 常驻行情刷新线程（每 worker 一条）：启动即预热，TTL 过半即续期，失败短间隔重试。
# 请求路径只读缓存，永不发起外部请求。
_quotes_refresher_started = False
_quotes_refresher_start_lock = threading.Lock()
_quotes_refresher_stop = threading.Event()
_quotes_refresher_thread: threading.Thread | None = None
QUOTES_REFRESH_INTERVAL_SEC = QUOTES_CACHE_TTL_SEC / 2
QUOTES_RETRY_INTERVAL_SEC = 15.0


def _quotes_refresher_loop():
    while not _quotes_refresher_stop.is_set():
        ok = _refresh_quotes_cache()
        _quotes_refresher_stop.wait(
            QUOTES_REFRESH_INTERVAL_SEC if ok else QUOTES_RETRY_INTERVAL_SEC
        )


def _ensure_quotes_refresher():
    global _quotes_refresher_started, _quotes_refresher_thread
    if not _quote_refresh_role_allowed():
        return None

    if (
        _quotes_refresher_started
        and _quotes_refresher_thread is not None
        and _quotes_refresher_thread.is_alive()
    ):
        return _quotes_refresher_thread
    with _quotes_refresher_start_lock:
        if (
            _quotes_refresher_started
            and _quotes_refresher_thread is not None
            and _quotes_refresher_thread.is_alive()
        ):
            return _quotes_refresher_thread
        if _quotes_refresher_started:
            logger.error("[Quotes] refresher thread died; starting replacement")
            _quotes_refresher_started = False
            _quotes_refresher_thread = None
        _quotes_refresher_stop.clear()
        _quotes_refresher_thread = threading.Thread(
            target=_quotes_refresher_loop, name="quotes-refresher", daemon=True
        )
        _quotes_refresher_thread.start()
        _quotes_refresher_started = True
        return _quotes_refresher_thread


def stop_quotes_refresher(timeout: float = 10.0) -> bool:
    global _quotes_refresher_started, _quotes_refresher_thread
    _quotes_refresher_stop.set()
    thread = _quotes_refresher_thread
    if thread is not None:
        thread.join(timeout=timeout)
    stopped = thread is None or not thread.is_alive()
    if stopped:
        with _quotes_refresher_start_lock:
            _quotes_refresher_started = False
            _quotes_refresher_thread = None
    return stopped


@dash_bp.route("/quotes")
def quotes():
    from services.shared_cache import get_shared_json

    shared = get_shared_json("quotes")
    if shared and isinstance(shared.get("html"), str):
        return _quotes_response(shared["html"])
    with _quotes_cache_lock:
        cached_html = _quotes_cache["html"]
    if cached_html is not None:
        return _quotes_response(cached_html)
    return _quotes_response(_quotes_placeholder_html())


def _feedback_strategy_version() -> str:
    resolved = resolve_scoring_contract(load_config(), register_snapshot=False)
    return str(resolved.get("scoring_strategy_version") or "").strip()


def _enqueue_dashboard_snapshot_refresh(invalidation_token: str) -> str:
    from routes.webhook_routes import _submit_background_task

    return _submit_background_task(
        {
            "task_type": "dashboard_snapshot_refresh",
            "snapshot_version": 1,
            "release_version": release_version(),
            "invalidation_token": invalidation_token,
        }
    )


@dash_bp.route("/feedback", methods=["POST"])
def feedback():
    auth = get_dashboard_auth()
    if auth is None:
        return _auth_error()
    if not verify_session_csrf(auth):
        return browser_error("Forbidden", 403)
    link = request.form.get("link", "").strip()[:2048]
    label = request.form.get("label", "").strip()[:32]
    target_score = request.form.get("target_score", "").strip()[:32]
    note = request.form.get("note", "").strip()[:1000]
    if link and label in {"good", "bad", "missed", "duplicate"}:
        try:
            score_val = float(target_score) if target_score else None
        except ValueError:
            score_val = None
        if score_val is not None:
            score_val = max(0.0, min(10.0, score_val)) if math.isfinite(score_val) else None
        from services.dashboard_snapshots import (
            model_quality_feedback_invalidation,
            snapshots_enabled,
        )

        if not snapshots_enabled():
            DB.add_article_feedback(link, label, score_val, note)
        else:
            # The marker is durable before the feedback commit.  Contract
            # lookup is allowed on this authenticated mutation path; the
            # public model-quality GET remains file-only.
            with model_quality_feedback_invalidation() as invalidation:
                try:
                    before = _feedback_strategy_version()
                except Exception:
                    logger.warning(
                        "Feedback pre-mutation contract lookup failed",
                        exc_info=True,
                    )
                    before = ""
                DB.add_article_feedback(link, label, score_val, note)
                invalidation.keep_invalidated()
                try:
                    after = _feedback_strategy_version()
                except Exception:
                    logger.warning(
                        "Feedback post-mutation contract lookup failed",
                        exc_info=True,
                    )
                    after = ""
                if before and after and before == after:
                    invalidation.restore_previous_state()
                else:
                    try:
                        outcome = _enqueue_dashboard_snapshot_refresh(
                            invalidation.token
                        )
                        logger.info(
                            "Feedback dashboard snapshot refresh=%s", outcome
                        )
                    except Exception:
                        # The marker must remain fail-closed after a committed
                        # contract mutation even if durable enqueue is down.
                        logger.exception(
                            "Feedback dashboard snapshot enqueue failed"
                        )
    return apply_browser_security_headers(redirect("/dashboard", code=303))


@dash_bp.route("/dashboard")
def dashboard():
    auth = get_dashboard_auth()
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            return dashboard_login_redirect()
        return _auth_error()
    cfg = _cfg()
    flush_stats(DB.update_stats)
    stats_row = DB.get_stats()
    dist_rows = DB.get_score_distribution(days=7)
    quality_metrics = DB.get_quality_metrics(hours=24, cfg=cfg)
    # Share the same window and reason counts as the Feed widget.
    health_rows, feed_metric_details = _get_feed_health_snapshot()
    log_rows = DB.get_request_logs(limit=20)

    total_req = stats_row[0] if stats_row else 0
    api_calls = stats_row[1] if stats_row else 0
    cache_hits = stats_row[2] if stats_row else 0
    sim_hits = stats_row[3] if stats_row else 0
    blacklist_hits = stats_row[4] if stats_row else 0
    fast_pass_hits = stats_row[5] if stats_row else 0
    filtered = stats_row[6] if stats_row else 0
    passed = stats_row[7] if stats_row else 0
    total_processed = filtered + passed
    savings_rate = round((cache_hits + sim_hits + blacklist_hits + fast_pass_hits) / total_processed * 100, 1) if total_processed > 0 else 0
    pass_rate = round(passed/total_processed*100, 1) if total_processed else 0

    high_score_count = _get_recent_high_score_count(hours=48)
    summary_articles = _safe_dashboard_summary_articles(
        _get_dashboard_summary_articles(limit=20, hours=48)
    )
    briefing_hours = _briefing_default_hours(cfg)
    briefing_rows = _get_briefing_articles(
        limit=12,
        hours=briefing_hours,
        overfetch=True,
    )
    briefing_articles = []
    for row in briefing_rows:
        article = _briefing_article_from_row(row)
        article["link"] = safe_http_url(article.get("link"))
        article["time"] = (
            beijing_fromtimestamp(article["created_at"]).strftime("%m-%d %H:%M")
            if article.get("created_at") else ""
        )
        briefing_articles.append(article)
    briefing = attach_quant_market_context(
        _local_briefing_from_articles(
            briefing_articles,
            hours=briefing_hours,
            limit=12,
            cfg=cfg,
        )
    )
    briefing_html = render_themes_html(briefing, briefing_hours)
    llm_cost_stats = DB.get_llm_cost_stats(hours=24, cfg=cfg, all_strategies=True)
    quiet_enabled, quiet_raise = _is_quiet_mode(cfg)

    si = _staleness_info(cfg)
    refresh_running = is_refresh_job_running()
    dashboard_ops = build_dashboard_ops(
        cfg, health_rows, si, refresh_running=refresh_running, db_path=database.DB_PATH,
    )

    with open(DASHBOARD_TEMPLATE, "r", encoding="utf-8") as f:
        dash_tmpl = f.read()
    rendered = render_template_string(
        dash_tmpl,
        dashboard_ops=dashboard_ops,
        now=beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        total_req=total_req,
        savings_rate=savings_rate,
        pass_rate=pass_rate,
        passed=passed,
        total_processed=total_processed,
        api_calls=api_calls,
        dist_rows=dist_rows,
        quality_metrics=quality_metrics,
        health_rows=health_rows,
        feed_metric_details=feed_metric_details,
        log_rows=log_rows,
        high_score_count=high_score_count,
        summary_articles=summary_articles,
        briefing=briefing,
        briefing_html=briefing_html,
        llm_cost_stats=llm_cost_stats,
        quiet_enabled=quiet_enabled,
        quiet_raise=quiet_raise,
        strategy_version=cfg.get("scoring_strategy_version", ""),
        cfg=cfg,
        last_full_refresh=si.get("last_full_refresh_at"),
        refresh_stale=si.get("stale", False),
        staleness_threshold_seconds=si.get("staleness_threshold_seconds", 0),
        refresh_running=refresh_running,
        datetime=datetime,
        feed_data_health=feed_data_health,
        format_article_display_title=format_article_display_title,
        safe_http_url=safe_http_url,
        dashboard_auth_kind=auth.kind,
        dashboard_csrf_token=auth.csrf_token,
        release_version=release_version(),
    )
    return apply_browser_security_headers(Response(rendered, mimetype="text/html"))


@dash_bp.route("/ops-summary")
def ops_summary_widget():
    """Glance 运维面板摘要小组件。"""
    def load_summary_state():
        cfg = _cfg()
        return (
            _staleness_info(cfg),
            is_refresh_job_running(),
            DB.get_stats(),
        )

    si, refresh_running, stats_row = _ops_cached_value(
        "ops-summary",
        _OPS_SUMMARY_CACHE_TTL_SEC,
        load_summary_state,
    )
    stale = bool(si.get("stale", False))
    degraded = bool(si.get("degraded", False))
    last_refresh = si.get("last_full_refresh_at") or 0
    now_ts = int(time.time())
    last_refresh_ts = 0
    if last_refresh:
        try:
            last_refresh_ts = int(last_refresh)
        except (TypeError, ValueError):
            try:
                last_refresh_ts = int(datetime.fromisoformat(str(last_refresh)).timestamp())
            except (TypeError, ValueError):
                last_refresh_ts = 0
    age_seconds = max(0, now_ts - last_refresh_ts) if last_refresh_ts else None

    def fmt_age(seconds):
        if seconds is None:
            return "无记录"
        if seconds < 60:
            return f"{seconds}秒前"
        if seconds < 3600:
            return f"{seconds // 60}分钟前"
        if seconds < 86400:
            return f"{seconds // 3600}小时前"
        return f"{seconds // 86400}天前"

    api_calls = stats_row[1] if stats_row else 0
    cache_hits = stats_row[2] if stats_row else 0
    sim_hits = stats_row[3] if stats_row else 0
    blacklist_hits = stats_row[4] if stats_row else 0
    fast_pass_hits = stats_row[5] if stats_row else 0
    filtered = stats_row[6] if stats_row else 0
    passed = stats_row[7] if stats_row else 0
    total_processed = filtered + passed
    pass_rate = round(passed / total_processed * 100, 1) if total_processed else 0
    savings_rate = round(
        (cache_hits + sim_hits + blacklist_hits + fast_pass_hits) / total_processed * 100,
        1,
    ) if total_processed else 0

    if refresh_running:
        status_text, status_color, status_bg = (
            "刷新中", "#2563eb", "rgba(37,99,235,.12)"
        )
    elif stale:
        status_text, status_color, status_bg = (
            "数据过期", "#dc2626", "rgba(220,38,38,.12)"
        )
    elif degraded:
        status_text, status_color, status_bg = (
            "部分源异常", "#d97706", "rgba(217,119,6,.12)"
        )
    else:
        status_text, status_color, status_bg = (
            "运行正常", "#16a34a", "rgba(22,163,74,.12)"
        )

    cards = [
        ("运行状态", status_text, "刷新任务正在执行" if refresh_running else "Feed 数据新鲜度", status_color),
        ("最近刷新", fmt_age(age_seconds), beijing_fromtimestamp(last_refresh_ts).strftime("%m-%d %H:%M") if last_refresh_ts else "等待首次全量刷新", "#475569"),
        ("通过率", f"{pass_rate}%", f"{passed}/{total_processed} 篇", "#7c3aed"),
        ("API 节省率", f"{savings_rate}%", f"LLM 调用 {api_calls} 次", "#ea580c"),
    ]

    cards_html = "".join(
        f'<div class="ops-summary-card" style="background:linear-gradient(135deg,hsla(var(--bgh),var(--bgs),calc(var(--bgl) - 2%),.98),hsla(var(--bgh),var(--bgs),calc(var(--bgl) - 6%),.88));'
        f'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) - 15%),.45);border-radius:16px;padding:13px;box-shadow:0 10px 24px rgba(15,23,42,.06);">'
        f'<div class="ops-summary-label" style="font-size:11px;color:var(--color-text-subdue,inherit);font-weight:700;letter-spacing:.06em;text-transform:uppercase;">{html_escape(label)}</div>'
        f'<div class="ops-summary-value" style="margin-top:7px;color:{color};font-size:20px;font-weight:850;letter-spacing:-.03em;">{html_escape(value)}</div>'
        f'<div class="ops-summary-note" style="margin-top:4px;color:var(--color-text-subdue,inherit);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{html_escape(sub)}</div>'
        f'</div>'
        for label, value, sub, color in cards
    )

    html = (
        '<div class="ops-summary" style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:4px 2px 8px;">'
        '<div class="ops-summary-head" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">'
        '<div><div class="ops-summary-title" style="font-size:18px;font-weight:850;letter-spacing:-.03em;color:var(--color-text-base,inherit);">运维巡检摘要</div>'
        '<div class="ops-summary-subtitle" style="font-size:12px;color:var(--color-text-subdue,inherit);margin-top:2px;">AI-Filter / Feed / LLM 关键指标</div></div>'
        f'<div class="ops-summary-status" style="font-size:11px;font-weight:800;color:{status_color};background:{status_bg};padding:5px 10px;border-radius:999px;">{html_escape(status_text)}</div>'
        '</div>'
        f'<div class="ops-summary-grid" style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;">{cards_html}</div>'
        '</div>'
    )
    rsp = Response(html, mimetype="text/html")
    rsp.headers["Widget-Title"] = "Ops Summary"
    rsp.headers["Widget-Content-Type"] = "html"
    rsp.headers["Cache-Control"] = (
        f"public, max-age={int(_OPS_SUMMARY_CACHE_TTL_SEC)}"
    )
    return rsp


def _feed_health_badge(last_error, age, data_health, total, success):
    """Transport/data health stays separate from score acceptance."""
    if str(last_error or "").startswith("部分条目处理失败"):
        state, color, bg = "条目退化", "#ea580c", "rgba(234,88,12,.11)"
    elif str(last_error or "").startswith("条目处理全部失败"):
        state, color, bg = "处理失败", "#dc2626", "rgba(220,38,38,.1)"
    elif last_error:
        state, color, bg = "拉取失败", "#dc2626", "rgba(220,38,38,.1)"
    elif age is None:
        state, color, bg = "无数据", "#dc2626", "rgba(220,38,38,.1)"
    elif age > 6 * 3600:
        state, color, bg = "过久未更", "#ea580c", "rgba(234,88,12,.11)"
    elif data_health["data_degraded"]:
        state, color, bg = "连续空源", "#dc2626", "rgba(220,38,38,.1)"
    elif data_health["consecutive_empty_checks"] > 0:
        state, color, bg = "暂时为空", "#ea580c", "rgba(234,88,12,.11)"
    elif total > 0 or success:
        state, color, bg = "正常", "#16a34a", "rgba(22,163,74,.11)"
    else:
        state, color, bg = "无数据", "#dc2626", "rgba(220,38,38,.1)"
    return state, color, bg


@dash_bp.route("/ops-feeds")
def ops_feeds_widget():
    """Glance 运维面板 Feed 源明细小组件。"""
    cfg = _cfg()
    pass_threshold = "按源配置"
    now_ts = int(time.time())
    cache_signature = hashlib.sha256(
        json.dumps(
            {
                "feeds": cfg.get("feeds", []),
                "threshold": cfg.get("threshold", {}),
                "feed_health": cfg.get("feed_health", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    rows, metric_details = _ops_cached_value(
        ("ops-feeds", cache_signature),
        _OPS_FEEDS_CACHE_TTL_SEC,
        _get_feed_health_snapshot,
    )

    def fmt_age(ts):
        if not ts:
            return "无数据"
        seconds = max(0, now_ts - int(ts))
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{seconds // 60}分钟前"
        if seconds < 86400:
            return f"{seconds // 3600}小时前"
        return f"{seconds // 86400}天前"

    feed_cards = []
    for r in rows:
        _url, name, last_check, _response_ms, success, _fail, total, passed, avg_score, _last_error = r[:10]
        data_health = feed_data_health(r, cfg)
        metric = metric_details[_url]
        age = now_ts - int(last_check) if last_check else None
        state, color, bg = _feed_health_badge(
            _last_error, age, data_health, total, success
        )
        avg_text = f"{avg_score:.1f}" if avg_score is not None else "-"
        if data_health["consecutive_empty_checks"]:
            detail_text = (
                f"空源连击 {data_health['consecutive_empty_checks']} · "
                f"上次非空 {fmt_age(data_health['last_nonempty_at'])}"
            )
        else:
            detail_text = f"正式评分均值 {avg_text} · 成功检查 {success}"
        display_name = str(name or "").strip()
        if (
            not display_name
            or display_name == str(_url or "").strip()
            or display_name.lower().startswith(("http://", "https://"))
        ):
            display_name = "未命名源"
        reason_line = (
            f' · <span style="color:#c98a00;">零输出原因：'
            f'{html_escape(metric["zero_output_reason"])}</span>'
            if metric.get("zero_output_reason") else ""
        )
        feed_cards.append(
            f'<div class="ops-feed-row" style="display:grid;grid-template-columns:minmax(150px,1.3fr) 82px 52px 100px 88px;gap:10px;align-items:center;'
            f'padding:10px 12px;border-radius:13px;margin-bottom:8px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) - 2%),.82);'
            f'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) - 13%),.42);">'
            f'<div style="min-width:0;"><div style="font-size:13px;font-weight:750;color:var(--color-text-base,inherit);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{html_escape(display_name)}</div>'
            f'<div style="margin-top:3px;font-size:11px;color:var(--color-text-subdue,inherit);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{detail_text}</div></div>'
            f'<div style="font-size:12px;font-weight:800;color:{color};background:{bg};border-radius:999px;padding:4px 8px;text-align:center;">{state}</div>'
            f'<div style="font-size:12px;color:var(--color-text-base,inherit);font-weight:700;text-align:right;">{total}</div>'
            f'<div style="font-size:12px;color:var(--color-text-base,inherit);font-weight:700;text-align:right;">{metric["rate_text"]}</div>'
            f'<div style="font-size:12px;color:var(--color-text-subdue,inherit);text-align:right;">{fmt_age(last_check)}</div>'
            f'<div style="grid-column:1/-1;font-size:11px;line-height:1.7;color:var(--color-text-subdue,inherit);">'
            f'达标阈值 {metric["threshold"]:g} 分 · {html_escape(metric["breakdown_text"])}{reason_line}'
            f'</div>'
            f'</div>'
        )

    empty = '<div style="padding:24px;text-align:center;color:var(--color-text-subdue,inherit);border:1px dashed rgba(148,163,184,.45);border-radius:16px;">暂无 Feed 统计数据</div>'
    html = OPS_FEED_LAYOUT_STYLE + (
        '<div class="ops-feeds" style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:4px 2px 8px;">'
        '<div style="display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:12px;gap:12px;">'
        '<div><div style="font-size:18px;font-weight:850;letter-spacing:-.03em;color:var(--color-text-base,inherit);">Feed 源健康明细</div>'
        f'<div style="font-size:12px;color:var(--color-text-subdue,inherit);margin-top:2px;">近24h新闻时间 · 主来源统计 · 阈值{pass_threshold}</div></div>'
        '<div style="font-size:11px;color:var(--color-text-subdue,inherit);">样本 / 新增有效信号率 / 最近检查</div>'
        '</div>'
        '<div style="font-size:11px;line-height:1.7;margin-bottom:10px;color:var(--color-text-subdue,inherit);">'
        '新增有效信号率＝正式达标 / 全部样本，不是抓取成功率。重复稿不计为新增；'
        '中性处理区分格式/字段校验与来源依据校验，不等于新闻没有价值。多源同链接仅计主来源。'
        '模型链路失败与条目处理异常单独计数，不算正式达标；仅其他评分契约的样本不计入，'
        '无法归因时明确写“原因未完整记录”。'
        '</div>'
        f'{"".join(feed_cards) if feed_cards else empty}'
        '</div>'
    )
    rsp = Response(html, mimetype="text/html")
    rsp.headers["Widget-Title"] = "Feed Health"
    rsp.headers["Widget-Content-Type"] = "html"
    rsp.headers["Cache-Control"] = (
        f"public, max-age={int(_OPS_FEEDS_CACHE_TTL_SEC)}"
    )
    return rsp


@dash_bp.route("/briefing-card")
@rate_limit(max_requests=30, window_seconds=60)
def briefing_card_widget():
    """Glance 总控台专用 Briefing 卡片：默认本地聚合快速返回；llm=true 时显式调用 LLM 主题聚合。"""
    use_llm = _bool_arg("llm")
    if use_llm and not check_api_key():
        return _auth_error()
    hours = _bounded_int_arg("hours", _briefing_default_hours(), 1, 168)
    limit = _bounded_int_arg("limit", 12, 1, 50)
    min_score = _bounded_float_arg("min_score", None, 0.0, 10.0)
    feed_url = request.args.get("feed_url", request.args.get("url", "")).strip()[:2048]
    # The Glance homepage uses one fixed, public profile.  In production Web
    # role it is a constant-time read of the worker-published last-good card.
    if (
        set(request.args) == {"min_score", "limit"}
        and request.args.getlist("min_score") == ["7"]
        and request.args.getlist("limit") == ["12"]
        and not use_llm
        and min_score == 7.0
        and limit == 12
        and not feed_url
    ):
        snapshot_response = _dashboard_snapshot_response(
            "briefing",
            title="Briefing",
            cache_seconds=30,
        )
        if snapshot_response is not None:
            return snapshot_response
    rows = _get_briefing_articles(
        min_score=min_score,
        limit=limit,
        hours=hours,
        feed_url=feed_url or None,
        overfetch=True,
    )
    articles_info = []
    for row in rows:
        article = _briefing_article_from_row(row)
        article["time"] = (
            beijing_fromtimestamp(article["created_at"]).strftime("%m-%d %H:%M")
            if article.get("created_at") else ""
        )
        articles_info.append(article)
    briefing, refresh_state = _select_briefing_result(
        rows,
        articles_info,
        hours=hours,
        min_score=min_score,
        limit=limit,
        feed_url=feed_url,
        use_llm=use_llm,
    )
    briefing = attach_quant_market_context(briefing)
    html = render_briefing_reading_html(briefing, hours)
    rsp = Response(html, mimetype="text/html")
    rsp.headers["Widget-Title"] = "Briefing"
    rsp.headers["Widget-Content-Type"] = "html"
    if use_llm:
        rsp.headers["X-Briefing-Refresh"] = refresh_state
    return rsp


def _default_quality_snapshot_request() -> bool:
    """Serve the standard full and compact pages from the same worker HTML."""
    args = request.args
    return (
        set(args).issubset({"days", "view"})
        and args.getlist("days") in ([], ["90"])
        and args.getlist("view") in ([], ["compact"])
    )


@dash_bp.route("/score-backtest")
@rate_limit(max_requests=10, window_seconds=60)
def score_backtest_widget():
    days = _bounded_int_arg("days", 90, 1, 365)
    min_score = _bounded_float_arg("min_score", None, 0.0, 10.0)
    if _default_quality_snapshot_request():
        snapshot_response = _dashboard_snapshot_response(
            "model_quality",
            title="Score Backtest",
            cache_seconds=60,
            # The full page carries the inline 完整市场指标 entry; only the
            # lightweight card links out to /quality.
            drop_card_entry_link=(
                request.args.get("view", "").strip().lower() != "compact"
            ),
        )
        if snapshot_response is not None:
            return snapshot_response
    cache_contract = _backtest_cache_contract()
    bridge_data_version = (
        bridge_preview_data_version(*cache_contract)
        if cache_contract is not None
        else "none"
    )
    cache_key = (
        (days, min_score, *cache_contract)
        if cache_contract is not None
        else None
    )
    now = time.time()
    with _BACKTEST_CACHE_LOCK:
        cached = _BACKTEST_CACHE.get(cache_key) if cache_key is not None else None
        cached_bridge_version = (
            str(cached[3]) if cached is not None and len(cached) > 3 else "none"
        )
        if (
            cached
            and now - cached[0] < _BACKTEST_CACHE_TTL_SEC
            and cached_bridge_version == bridge_data_version
        ):
            result = cached[1]
            mapping_validation = cached[2]
        else:
            result = None
            mapping_validation = None
    if result is None:
        if not _BACKTEST_COMPUTE_LOCK.acquire(blocking=False):
            response = Response(
                json.dumps({"error": "backtest_busy", "retry_after": 30}),
                status=503,
                mimetype="application/json",
            )
            response.headers["Retry-After"] = "30"
            return response
        try:
            with _BACKTEST_CACHE_LOCK:
                cached = (
                    _BACKTEST_CACHE.get(cache_key)
                    if cache_key is not None
                    else None
                )
                cached_bridge_version = (
                    str(cached[3])
                    if cached is not None and len(cached) > 3
                    else "none"
                )
                if (
                    cached
                    and time.time() - cached[0] < _BACKTEST_CACHE_TTL_SEC
                    and cached_bridge_version == bridge_data_version
                ):
                    result = cached[1]
                    mapping_validation = cached[2]
            if result is None:
                result = run_score_backtest(days=days, min_score=min_score)
                computed_contract = _computed_backtest_contract(result)
                mapping_validation = run_mapping_validation(
                    days=days,
                    current_contract=computed_contract,
                )
                if computed_contract is not None:
                    computed_cache_key = (days, min_score, *computed_contract)
                    computed_bridge_version = _computed_bridge_data_version(result)
                    with _BACKTEST_CACHE_LOCK:
                        _BACKTEST_CACHE[computed_cache_key] = (
                            time.time(),
                            result,
                            mapping_validation,
                            computed_bridge_version,
                        )
                        while len(_BACKTEST_CACHE) > _BACKTEST_CACHE_MAX_ENTRIES:
                            _BACKTEST_CACHE.pop(next(iter(_BACKTEST_CACHE)))
        finally:
            _BACKTEST_COMPUTE_LOCK.release()
    if request.args.get("format", "html").lower() == "json":
        payload = dict(result)
        payload["mapping_validation"] = mapping_validation
        return payload
    compact_view = (
        request.args.get("view", "").strip().lower() == "compact"
        or _bool_arg("compact")
    )
    renderer = render_backtest_compact_html if compact_view else render_backtest_html
    rsp = Response(renderer(result, mapping_validation), mimetype="text/html")
    # Widget-Title 必须为 ASCII：gunicorn/Werkzeug 以 latin-1 编码响应头，中文会导致 400/502。
    # 面板可见标题由 glance.yml 与 render_backtest_html 内的中文徽标提供。
    rsp.headers["Widget-Title"] = "Score Backtest"
    rsp.headers["Widget-Content-Type"] = "html"
    rsp.headers["Cache-Control"] = "public, max-age=60"
    return rsp


@dash_bp.route("/briefing", methods=["GET", "POST"])
def briefing():
    """跨文章主题聚合情报简报
    """
    use_llm = _bool_arg("llm")
    push_requested = _bool_arg("push")
    if (use_llm or push_requested) and not check_api_key():
        return _auth_error()
    if push_requested and request.method != "POST":
        response = Response(
            json.dumps({"error": "push requires POST"}),
            status=405,
            mimetype="application/json",
        )
        response.headers["Allow"] = "POST"
        return response

    hours = _bounded_int_arg("hours", _briefing_default_hours(), 1, 168)
    min_score = _bounded_float_arg("min_score", None, 0.0, 10.0)
    limit = _bounded_int_arg("limit", 20, 1, 50)
    format_type = request.args.get('format', 'json', type=str).lower()

    rows = _get_briefing_articles(
        min_score=min_score,
        limit=limit,
        hours=hours,
        overfetch=True,
    )

    if not rows:
        empty_result = {
            "status": "ok",
            "message": f"近{hours}小时内无符合当前源阈值的高分文章",
            "themes": [],
            "overall_assessment": "暂无情报",
            "generated_at": beijing_now().isoformat()
        }
        empty_result = attach_quant_market_context(empty_result)
        content, mimetype = render_digest(empty_result, format_type, hours)
        return Response(content, mimetype=mimetype)

    articles_info = []
    for r in rows:
        article = _briefing_article_from_row(r)
        article["time"] = (
            beijing_fromtimestamp(article["created_at"]).strftime("%m-%d %H:%M")
            if article.get("created_at") else ""
        )
        articles_info.append(article)

    enriched_articles = dedupe_articles([
        enrich_article_analysis(article, trusted_only=True)
        for article in articles_info
    ])
    aggregation, refresh_state = _select_briefing_result(
        rows,
        articles_info,
        hours=hours,
        min_score=min_score,
        limit=limit,
        feed_url="",
        use_llm=use_llm,
    )
    aggregation["period_hours"] = hours
    aggregation["min_score"] = min_score
    if normalize_briefing_policy(_cfg())["enabled"]:
        aggregation.setdefault("article_count", min(limit, len(articles_info)))
        aggregation.setdefault("candidate_count", len(enriched_articles))
        # A cache hit owns its own article-ID snapshot.  Never rebind ``a1``
        # etc. to a newer rolling query while the stable TTL entry is visible.
        aggregation.setdefault("articles", enriched_articles[:limit])
    else:
        # Pre-PR-2 response contract used the current query snapshot.
        aggregation["article_count"] = len(articles_info)
        aggregation["articles"] = enriched_articles
    aggregation.setdefault("generated_at", beijing_now().isoformat())
    aggregation["status"] = "ok"
    aggregation = attach_quant_market_context(aggregation)

    if push_requested and use_llm and refresh_state != "hit":
        if refresh_state == "invalid-feed":
            status = 400
        elif refresh_state == "full":
            status = 503
        else:
            status = 202
        payload = {
            "status": (
                "invalid_feed"
                if status == 400
                else "queue_full"
                if status == 503
                else "queued"
            ),
            "message": (
                "feed_url 未在当前配置中"
                if status == 400
                else "后台持久队列已满，请稍后重试"
                if status == 503
                else "LLM Briefing 正在后台生成；缓存命中后才能推送"
            ),
            "refresh_state": refresh_state,
        }
        response = Response(
            json.dumps(payload, ensure_ascii=False),
            status=status,
            mimetype="application/json",
        )
        response.headers["X-Briefing-Refresh"] = refresh_state
        response.headers["Retry-After"] = "30" if status == 503 else "10"
        return response

    content, mimetype = render_digest(aggregation, format_type, hours)

    if push_requested:
        if not _push_digest_to_telegram(aggregation, _cfg()):
            return Response(
                json.dumps({"error": "telegram_push_failed"}),
                status=502,
                mimetype="application/json",
            )

    resp = Response(content, mimetype=mimetype)
    if format_type == "html":
        resp.headers["Widget-Content-Type"] = "html"
    if use_llm:
        resp.headers["X-Briefing-Refresh"] = refresh_state
    return resp


@dash_bp.route("/glance")
def glance_widget():
    """A股情报卡片，专为 glance extension widget 设计"""
    rows = _get_briefing_articles(limit=15, hours=48)
    summary_articles = [tuple(row[:8]) for row in rows]

    if not summary_articles:
        rsp = Response(
            '<div style="color:var(--color-text-subdue,inherit);text-align:center;padding:28px 16px;'
            'font-family:-apple-system,BlinkMacSystemFont,sans-serif;line-height:1.6;">'
            '<div style="font-size:28px;margin-bottom:8px;opacity:0.5">\u231b</div>'
            '<div style="font-size:14px;color:var(--color-text-base,inherit);font-weight:500;">'
            '\u6682\u65e0\u9ad8\u5206\u60c5\u62a5</div>'
            '<div style="font-size:12px;margin-top:6px;">\u7b49\u5f85\u5b9a\u65f6 /refresh \u6216\u624b\u52a8\u9884\u70ed\u7f13\u5b58\u540e\u5237\u65b0\u672c\u9875</div></div>',
            mimetype="text/html",
        )
        rsp.headers["Widget-Title"] = "AI Intelligence"
        rsp.headers["Widget-Content-Type"] = "html"
        return rsp

    n = len(summary_articles)
    cards = []
    summary_clamp = (
        "display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;"
        "overflow:hidden;word-break:break-word;"
    )
    for r in summary_articles:
        link, title, raw_title, score, tag, logic, summary, created_at = r
        score_color = "#f87171" if score >= 8 else "#fbbf24" if score >= 7 else "#34d399"
        tag_display = tag or "未分类"
        ts = beijing_fromtimestamp(created_at).strftime("%m-%d %H:%M") if created_at else "N/A"
        clean_title = format_article_display_title(title, raw_title)
        safe_href = html_escape(safe_http_url(link), quote=True)
        summary_html = escape_xml(summary or "暂\u65e0\u603b\u7ed3")

        cards.append(
            f'<div style="background:linear-gradient(180deg,hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 7%),0.70),hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 2%),0.45));'
            f'border-radius:16px;padding:14px 14px;margin-bottom:11px;'
            f'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 22%),0.14);'
            f'border-left:3px solid {score_color};font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
            f'box-shadow:0 12px 30px rgba(0,0,0,0.14);backdrop-filter:blur(10px);">'
            f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:8px;">'
            f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer" '
            f'style="color:var(--color-text-base,inherit);text-decoration:none;font-size:14px;'
            f'font-weight:720;flex:1;line-height:1.45;min-width:0;">'
            f'{escape_xml(clean_title)}</a>'
            f'<span style="background:rgba(248,113,113,0.13);color:{score_color};padding:3px 8px;'
            f'border:1px solid rgba(248,113,113,0.24);border-radius:999px;'
            f'font-size:11px;font-weight:800;white-space:nowrap;flex-shrink:0;">{score}</span></div>'
            f'<div style="margin-bottom:8px;display:flex;flex-wrap:wrap;align-items:center;gap:6px;">'
            f'<span style="background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 10%),0.38);'
            f'color:var(--color-text-subdue,inherit);padding:2px 8px;border-radius:999px;font-size:11px;">'
            f'{escape_xml(tag_display)}</span>'
            f'<span style="color:var(--color-text-subdue,inherit);font-size:11px;">{ts}</span></div>'
            f'<div style="color:var(--color-text-base,inherit);font-size:12px;line-height:1.6;'
            f'background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 4%),0.30);'
            f'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),0.10);'
            f'border-radius:10px;padding:10px 11px;{summary_clamp}">'
            f"{summary_html}</div></div>"
        )

    html = (
        '<div style="padding:4px 2px 8px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;">'
        f'<div style="font-size:11px;color:var(--color-text-subdue,inherit);margin:0 2px 11px;'
        f'letter-spacing:0.03em;text-transform:uppercase;">共 {n} 条 · 48h 内过阈情报</div>'
        f"{''.join(cards)}"
        '</div>'
    )
    rsp = Response(html, mimetype="text/html")
    rsp.headers["Widget-Title"] = "AI Intelligence"
    rsp.headers["Widget-Content-Type"] = "html"
    return rsp
