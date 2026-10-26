"""
跨文章主题聚合引擎（Cross-Article Theme Aggregation）

从多篇高分文章中提取核心主题，进行跨文章归纳与洞察生成。
输出结构化报告，支持 JSON / HTML / Markdown / RSS 多种格式。
"""

import json
import logging
import math
import re
from collections import defaultdict
from utils.beijing_time import beijing_now, beijing_fromtimestamp
from html import unescape as html_unescape
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit

from core.scorer import RateLimitTimeoutException, Scorer, select_llm_backends
from core.asset_mapping import format_mapping, map_article_to_assets
from core.briefing_assembly import (
    assemble_briefing_candidates,
    briefing_policy_fingerprint,
    normalize_briefing_grounding_policy,
    normalize_briefing_policy,
)
from core.briefing_grounding import ground_theme_commentary
from core.config import config
from scoring_explanation import EXPLANATION_FIELDS
from core.theme_rules import canonical_theme_name, theme_display_name
from services.quant_market_context import (
    quant_market_context_text,
    render_quant_market_context_html,
)
from utils.helpers import (
    escape_cdata,
    escape_html,
    escape_xml,
    normalize_direction,
    rfc822_date,
    safe_http_url,
)

logger = logging.getLogger(__name__)


_DUPLICATE_PREFIX_RE = re.compile(r"^[\[【（(]?\s*[⭐★]?[\d.]+\s*分?\s*[📈📉]?")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

# A deduplicated article has two independent owners: the highest-quality
# scoring result owns all semantic fields, while the freshest wire item owns
# delivery metadata.  Keeping the groups explicit prevents combinations such
# as a bearish direction paired with a bullish rationale from another item.
_DEDUPE_SEMANTIC_FIELDS = (
    "title", "raw_title", "score", "tag", "logic", "summary",
    "direction", "sentiment", "score_path", "prompt_key", "model_name",
    "source_tier", "strategy_version", "stage1_score", "stage1_model",
    "stage2_score", "stage2_model", "score_confidence", "review_reason",
    "background", "why_it_matters", "market_impact", "action", "assets",
    "sectors", "stocks", "rationale", "watch",
) + EXPLANATION_FIELDS
_DEDUPE_FRESHNESS_FIELDS = (
    "link", "time", "created_at", "time_ts", "feed_url", "source",
)


def _article_score(article: Dict[str, Any]) -> float:
    try:
        score = float(article.get("score") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return score if math.isfinite(score) else 0.0


def _bounded_float_value(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(maximum, parsed))


def _article_timestamp(article: Dict[str, Any]) -> int:
    try:
        return int(article.get("created_at") or article.get("time_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _replace_semantic_fields(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Replace one complete scoring payload without retaining stale fields."""
    for field in _DEDUPE_SEMANTIC_FIELDS:
        if field in source:
            target[field] = source[field]
        else:
            target.pop(field, None)


def _canonical_event_references(
    articles: List[Dict[str, Any]],
) -> Dict[str, tuple[str, int]]:
    """Resolve persisted event IDs once for a whole presentation batch."""
    event_ids = {
        str(article.get("event_id") or "").strip()
        for article in articles or []
        if str(article.get("event_id") or "").strip()
    }
    if not event_ids:
        return {}

    references: Dict[str, tuple[str, int]] = {}
    try:
        from db.database import get_connection
        from db.event_core import canonical_event_id

        # Read-only resolution deliberately does not hold the process-wide DB
        # write lock.  This helper is also used after queries which already
        # released that non-reentrant lock.
        with get_connection() as conn:
            for event_id in event_ids:
                try:
                    canonical_id = canonical_event_id(conn, event_id)
                    row = conn.execute(
                        "SELECT version FROM events WHERE event_id = ?",
                        (canonical_id,),
                    ).fetchone()
                    if row:
                        references[event_id] = (canonical_id, max(1, int(row[0])))
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception:
        # Legacy databases have no event core yet.  Their rows are handled by
        # the explicitly named title fallback below, never by an invented ID.
        logger.debug("Event-core dedupe resolution unavailable", exc_info=True)
    return references


def infer_theme_direction(articles: List[Dict[str, Any]]) -> str:
    """Infer a theme from per-article structured directions."""
    bull = bear = 0
    for article in articles or []:
        text = " ".join(
            str(article.get(key) or "")
            for key in ("title", "raw_title", "logic", "summary", "tag")
        )
        direction = normalize_direction(
            article.get("direction") or article.get("sentiment") or "",
            text=text,
        )
        if direction == "看空":
            bear += 1
        elif direction == "看多":
            bull += 1
    if bull > bear:
        return "看多"
    if bear > bull:
        return "看空"
    return "中性"


def _normalize_event_title(title: str, *, strip_bracket_labels: bool) -> str:
    text = html_unescape(str(title or ""))
    text = re.sub(
        r'</?(?:em|strong|b|i|mark)\b[^>]*>',
        "",
        text,
        flags=re.IGNORECASE,
    )
    if strip_bracket_labels:
        text = re.sub(r"\[[^\]]*\]", "", text)
        text = re.sub(r"【[^】]*】", "", text)
    text = _DUPLICATE_PREFIX_RE.sub("", text)
    text = re.sub(r"^(财联社|格隆汇|华尔街见闻|金十数据)?\s*\d{1,2}月\d{1,2}日电[，,]?", "", text)
    text = re.sub(r"^(财联社|格隆汇|华尔街见闻|金十数据)[：:，,\s]*", "", text)
    text = _NON_WORD_RE.sub("", text)
    return text.lower()


def normalize_event_identity_key(title: str) -> str:
    """Return the complete, conservative title used for identity checks.

    Bracket contents are retained because they can name a company or alter the
    fact being reported.  Identity-sensitive callers prefer a missed merge to
    an irreversible merge of two different events.
    """
    return _normalize_event_title(title, strip_bracket_labels=False)


def normalize_event_key(title: str) -> str:
    """Return the backward-compatible bounded digest grouping key."""
    return _normalize_event_title(title, strip_bracket_labels=True)[:80]


_SOURCE_PROVIDERS = (
    ("gelonghui", "格隆汇", ("gelonghui.com",), ("/gelonghui/",)),
    ("wallstreetcn", "华尔街见闻", ("wallstreetcn.com",), ("/wallstreetcn/",)),
    ("cls", "财联社", ("cls.cn",), ("/cls/",)),
    ("eastmoney", "东方财富", ("eastmoney.com",), ("/eastmoney/",)),
    ("10jqka", "同花顺", ("10jqka.com.cn", "10jqka.com"), ("/10jqka/",)),
    ("jin10", "金十数据", ("jin10.com",), ("/jin10/",)),
    ("caixin", "财新", ("caixin.com",), ("/caixin/",)),
)
_INTERNAL_RSS_HOSTS = {"rsshub", "127.0.0.1", "localhost", "::1"}


def article_source_key(article: Dict[str, Any]) -> str:
    """Return a stable publisher key across RSSHub host aliases."""
    feed_url = str(article.get("feed_url") or "").strip()
    link = str(article.get("link") or "").strip()
    parsed_candidates = []
    for candidate in (link, feed_url):
        try:
            parsed_candidates.append((candidate, urlsplit(candidate)))
        except (TypeError, ValueError):
            continue
    for _candidate, parsed in parsed_candidates:
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        for key, _label, domains, _routes in _SOURCE_PROVIDERS:
            if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
                return key

    try:
        parsed_feed = urlsplit(feed_url)
        feed_host = (parsed_feed.hostname or "").lower()
        feed_path = f"/{(parsed_feed.path or '').strip('/').lower()}/"
        is_rss_aggregator = feed_host in _INTERNAL_RSS_HOSTS or "rsshub" in feed_host
        if is_rss_aggregator:
            for key, _label, _domains, routes in _SOURCE_PROVIDERS:
                if any(route in feed_path for route in routes):
                    return key
    except (TypeError, ValueError):
        pass

    for _candidate, parsed in parsed_candidates:
        hostname = (parsed.hostname or "").lower().removeprefix("www.")
        if hostname and hostname not in _INTERNAL_RSS_HOSTS:
            return hostname

    # The same internal RSS route may be addressed as rsshub:1200 or through
    # loopback. Normalize by route so those aliases cannot fake source diversity.
    try:
        parsed_feed = urlsplit(feed_url)
        route = re.sub(r"/+", "/", parsed_feed.path or "").rstrip("/").lower()
        if route:
            return f"rss-route:{route}"
    except (TypeError, ValueError):
        pass
    return f"{feed_url} {link}".lower().strip()


def _article_source_keys(article: Dict[str, Any]) -> set[str]:
    keys = article.get("source_keys") or []
    if isinstance(keys, str):
        keys = [keys]
    result = {str(key).strip() for key in keys if str(key).strip()}
    key = article_source_key(article)
    if key:
        result.add(key)
    return result


def dedupe_articles(articles: List[Dict[str, Any]], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Merge by canonical persisted event ID; use titles only for legacy rows."""
    articles = list(articles or [])
    event_references = _canonical_event_references(articles)
    merged: Dict[str, Dict[str, Any]] = {}
    semantic_ranks: Dict[str, tuple[float, int]] = {}
    freshness_ranks: Dict[str, int] = {}
    for article in articles:
        raw_event_id = str(article.get("event_id") or "").strip()
        event_reference = event_references.get(raw_event_id)
        if event_reference:
            canonical_id, canonical_version = event_reference
            key = f"event:{canonical_id}"
        elif raw_event_id:
            # Only genuinely pre-migration rows (no ID at all) may use the
            # title fallback.  A broken/non-persisted reference stays isolated
            # so corruption or a transient DB fault cannot create an event.
            canonical_id, canonical_version = "", 0
            key = "unresolved-event-row:" + str(
                article.get("link") or id(article)
            )
        else:
            canonical_id, canonical_version = "", 0
            legacy_key = normalize_event_key(article.get("title", ""))
            key = (
                f"legacy-title:{legacy_key}"
                if legacy_key
                else f"legacy-link:{str(article.get('link') or id(article))}"
            )
        matched_key = None
        if key.startswith("legacy-title:"):
            title_key = key.removeprefix("legacy-title:")
            for existing_key in merged:
                if not existing_key.startswith("legacy-title:"):
                    continue
                existing_title_key = existing_key.removeprefix("legacy-title:")
                shorter, longer = sorted(
                    (title_key, existing_title_key), key=len
                )
                if len(shorter) >= 28 and shorter in longer:
                    matched_key = existing_key
                    break
        key = matched_key or key
        existing = merged.get(key)
        if not existing:
            cloned = dict(article)
            if canonical_id:
                cloned["event_id"] = canonical_id
                cloned["event_version"] = canonical_version
            else:
                cloned.pop("event_id", None)
                cloned.pop("event_version", None)
            try:
                cloned["duplicate_count"] = max(1, int(article.get("duplicate_count") or 1))
            except (TypeError, ValueError):
                cloned["duplicate_count"] = 1
            cloned["source_keys"] = sorted(_article_source_keys(article))
            merged[key] = cloned
            semantic_ranks[key] = (_article_score(article), _article_timestamp(article))
            freshness_ranks[key] = _article_timestamp(article)
            continue
        if canonical_id:
            existing["event_id"] = canonical_id
            existing["event_version"] = max(
                canonical_version,
                int(existing.get("event_version") or 0),
            )
        source_keys = _article_source_keys(existing) | _article_source_keys(article)
        try:
            incoming_count = max(1, int(article.get("duplicate_count") or 1))
        except (TypeError, ValueError):
            incoming_count = 1
        existing["duplicate_count"] = int(existing.get("duplicate_count") or 1) + incoming_count
        semantic_rank = (_article_score(article), _article_timestamp(article))
        if semantic_rank > semantic_ranks[key]:
            _replace_semantic_fields(existing, article)
            semantic_ranks[key] = semantic_rank
        timestamp = _article_timestamp(article)
        if timestamp > freshness_ranks[key]:
            for field in _DEDUPE_FRESHNESS_FIELDS:
                if field in article:
                    existing[field] = article[field]
            freshness_ranks[key] = timestamp
        existing["source_keys"] = sorted(source_keys)
    result = sorted(
        merged.values(),
        key=lambda x: (_article_timestamp(x), _article_score(x)),
        reverse=True,
    )
    return result[:limit] if limit else result


def infer_market_analysis(
    article: Dict[str, Any], *, trusted_only: bool = False
) -> Dict[str, str]:
    """Local analyst layer: assets, sectors, stocks, rationale and follow-up."""
    return format_mapping(
        map_article_to_assets(article, trusted_only=trusted_only)
    )


def _briefing_source_label(article: Dict[str, Any]) -> str:
    source_key = article_source_key(article)
    for key, label, _domains, _routes in _SOURCE_PROVIDERS:
        if source_key == key:
            return label
    try:
        return urlsplit(str(article.get("link") or "")).hostname or ""
    except (TypeError, ValueError):
        return ""


def enrich_article_analysis(
    article: Dict[str, Any], *, trusted_only: bool = False
) -> Dict[str, Any]:
    enriched = dict(article)
    analysis = infer_market_analysis(enriched, trusted_only=trusted_only)
    if trusted_only:
        # Briefing local/fallback commentary owns these fields.  Replacing them
        # prevents earlier model prose or model-derived mappings from crossing
        # the boundary under a ``local_derived`` grounding label.
        enriched.update(analysis)
    else:
        enriched.update({k: enriched.get(k) or v for k, v in analysis.items()})
    return enriched


def build_market_pulse_summary(articles: List[Dict[str, Any]], hours: int) -> str:
    if not articles:
        return "暂无明确市场主线"
    deduped = dedupe_articles(articles)
    themes = []
    for article in deduped[:3]:
        tag = str(article.get("tag") or "高分情报").strip()
        assets = infer_market_analysis(article)["assets"].split("、")[0]
        themes.append(f"{tag}→{assets}")
    return f"近{hours}小时主线：" + "；".join(themes)


def _briefing_article_theme(article: Dict[str, Any]) -> str:
    explicit = str(article.get("briefing_theme") or "").strip()
    if explicit:
        return canonical_theme_name(explicit)
    tag = str(article.get("tag") or "").strip()
    if tag and tag not in {"FastPass", "未知"} and not tag.isdigit():
        return canonical_theme_name(tag)
    return "其他高分情报"


def _annotate_assembled_articles(
    articles: List[Dict[str, Any]],
    assembly_audit: Dict[str, Any],
) -> List[Dict[str, Any]]:
    primary_themes = list(assembly_audit.get("selected_primary_themes") or [])
    annotated = []
    for index, article in enumerate(articles):
        cloned = dict(article)
        if index < len(primary_themes) and primary_themes[index]:
            cloned["briefing_theme"] = canonical_theme_name(primary_themes[index])
        else:
            cloned["briefing_theme"] = _briefing_article_theme(cloned)
        annotated.append(cloned)
    return annotated


def _prepare_prompt_articles(
    articles: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    theme_ids: Dict[str, str] = {}
    prepared = []
    for index, article in enumerate(articles, 1):
        cloned = dict(article)
        theme = _briefing_article_theme(cloned)
        if theme not in theme_ids:
            theme_ids[theme] = f"t{len(theme_ids) + 1}"
        cloned["briefing_theme"] = theme
        cloned["briefing_theme_id"] = theme_ids[theme]
        cloned["briefing_article_id"] = f"a{index}"
        prepared.append(cloned)
    return prepared


def _briefing_theme_groups(
    articles: List[Dict[str, Any]],
) -> List[tuple[str, str, List[Dict[str, Any]]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    names: Dict[str, str] = {}
    for article in articles:
        theme_id = str(article.get("briefing_theme_id") or "").strip()
        if not theme_id:
            continue
        names.setdefault(theme_id, _briefing_article_theme(article))
        grouped.setdefault(theme_id, []).append(article)
    return [(theme_id, names[theme_id], grouped[theme_id]) for theme_id in grouped]


def _theme_importance(articles: List[Dict[str, Any]]) -> float:
    if not articles:
        return 0.0
    scores = [_article_score(article) for article in articles]
    return round(max(scores) * 0.7 + (sum(scores) / len(scores)) * 0.3, 1)


def _theme_source_count(articles: List[Dict[str, Any]]) -> int:
    keys = set()
    for article in articles:
        keys.update(_article_source_keys(article))
    return len(keys)


def briefing_source_metrics(
    articles: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Separate theme coverage from confirmation of its top canonical event."""
    group = list(articles or [])
    if not group:
        return {"source_count": 0, "top_event_source_count": 0}
    top = max(
        group,
        key=lambda article: (_article_score(article), _article_timestamp(article)),
    )
    return {
        "source_count": _theme_source_count(group),
        "top_event_source_count": len(_article_source_keys(top)),
    }


def briefing_source_confirmation(articles: List[Dict[str, Any]]) -> str:
    """Describe selected-publisher coverage without claiming independence."""
    group = list(articles or [])
    if not group:
        return "暂无可核验的入选发布方"
    metrics = briefing_source_metrics(group)
    source_count = metrics["source_count"]
    top_event_source_count = metrics["top_event_source_count"]
    update_count = sum(
        max(1, int(article.get("duplicate_count") or 1)) for article in group
    )
    if top_event_source_count == 0:
        if source_count:
            return (
                "头部事件未识别到入选发布方；转载谱系未核验；"
                f"主题整体覆盖 {source_count} 个入选发布方"
            )
        return "头部事件未识别到入选发布方；转载谱系未核验"
    if top_event_source_count >= 2:
        return (
            f"头部事件覆盖 {top_event_source_count} 个入选发布方；"
            f"转载谱系未核验；主题整体覆盖 {source_count} 个入选发布方"
        )
    if source_count >= 2:
        return (
            "头部事件仅覆盖 1 个入选发布方；"
            f"转载谱系未核验；主题整体覆盖 {source_count} 个入选发布方"
        )
    if update_count >= 2:
        return f"累计 {update_count} 条同发布方入选更新；转载谱系未核验"
    return "头部事件仅覆盖 1 个入选发布方；转载谱系未核验"


def briefing_theme_sort_key(theme: Dict[str, Any]) -> tuple[float, int, int]:
    """One authoritative theme order for local and cached LLM Briefings."""
    try:
        source_count = max(0, int(theme.get("source_count") or 0))
    except (TypeError, ValueError, OverflowError):
        source_count = 0
    return (
        _bounded_float_value(theme.get("importance"), 0.0, 0.0, 10.0),
        _article_timestamp({"created_at": theme.get("latest_at")}),
        source_count,
    )


def briefing_representative_articles(
    articles: List[Dict[str, Any]],
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Pick the same score-ranked, source-diverse links on every path."""
    try:
        selected_limit = max(0, int(limit))
    except (TypeError, ValueError, OverflowError):
        selected_limit = 0
    if not selected_limit:
        return []
    ranked = sorted(
        articles,
        key=lambda article: (_article_score(article), _article_timestamp(article)),
        reverse=True,
    )
    selected = []
    seen_sources = set()
    for article in ranked:
        source_keys = _article_source_keys(article) or {
            str(article.get("link") or article.get("title") or "")
        }
        if source_keys & seen_sources:
            continue
        selected.append(article)
        seen_sources.update(source_keys)
        if len(selected) >= selected_limit:
            break
    return selected


def _display_briefing_article(
    article: Dict[str, Any],
    *,
    include_theme: bool = False,
) -> Dict[str, Any]:
    clean = {
        key: value for key, value in article.items()
        if key != "briefing_theme_id"
        and (include_theme or key != "briefing_theme")
    }
    # ``briefing_article_id`` is deliberately public: evidence references in
    # a cached result must remain resolvable against that result's own corpus.
    return enrich_article_analysis(clean, trusted_only=True)


def _deterministic_theme_commentary(
    articles: List[Dict[str, Any]],
) -> Dict[str, str]:
    if not articles:
        return {
            "insight": "暂无可用事件",
            "why_it_matters": "证据不足，暂不形成主题结论",
            "market_impact": "方向尚未确认",
            "action": "等待价格与后续事件验证",
            "source_confirmation": briefing_source_confirmation([]),
        }
    max_score = max(_article_score(article) for article in articles)
    event_count = len(articles)
    update_count = sum(
        max(1, int(article.get("duplicate_count") or 1)) for article in articles
    )
    insight = (
        f"单条 {max_score:.1f} 分信号，处于观察窗口"
        if event_count == 1
        else f"{event_count} 个去重事件、{update_count} 条更新，最高 {max_score:.1f} 分"
    )
    top_article = max(
        articles,
        key=lambda article: (_article_score(article), _article_timestamp(article)),
    )
    analysis = enrich_article_analysis(top_article, trusted_only=True)
    assets = str(analysis.get("assets") or "").strip()
    sectors = str(analysis.get("sectors") or "").strip()
    impact_parts = []
    if assets:
        impact_parts.append(f"影响资产：{assets}")
    if sectors:
        impact_parts.append(f"关注板块：{sectors}")
    return {
        "insight": insight,
        "why_it_matters": f"入选主题覆盖 {event_count} 个去重事件；仍需价格行为验证",
        "market_impact": "；".join(impact_parts) or "等待资产价格确认",
        "action": str(analysis.get("watch") or "等待价格与后续事件验证"),
        "source_confirmation": briefing_source_confirmation(articles),
    }


def _deterministic_overall_assessment(
    themes: List[Dict[str, Any]], period_hours: int
) -> str:
    if not themes:
        return "暂无有效高分情报"
    summary = "；".join(
        f"{theme_display_name(theme.get('theme', '高分情报'))}"
        f"（{theme.get('direction', '中性')}）"
        for theme in themes[:3]
    )
    return f"近{period_hours}小时主线：{summary}"


def _deterministic_market_impact(themes: List[Dict[str, Any]]) -> str:
    directions = {theme.get("direction") for theme in themes}
    if "看多" in directions and "看空" in directions:
        return "政策利好与市场风险信号并存，优先观察指数修复、成交额和市场广度"
    if "看空" in directions:
        return "风险信号占优，优先观察市场广度、成交额和政策对冲是否改善"
    if "看多" in directions:
        return "利好信号占优，仍需用成交放大和价格趋势验证持续性"
    return "方向尚未形成共识，等待更多事件与资产价格反馈"


def _apply_deterministic_briefing_summary(
    result: Dict[str, Any],
    themes: List[Dict[str, Any]],
    period_hours: int,
    article_count: int,
) -> None:
    """Make every public top-level narrative local for enhanced Briefings."""
    result["overall_assessment"] = _deterministic_overall_assessment(
        themes, period_hours
    )
    result["market_impact"] = _deterministic_market_impact(themes)
    result["background"] = ""
    result["why_it_matters"] = "仅汇总入选主题及其本地核验指标"
    bearish = next(
        (
            theme for theme in themes
            if theme.get("direction") == "看空"
            and float(theme.get("importance") or 0) >= 8.0
        ),
        None,
    )
    result["risk_alert"] = (
        f"{bearish.get('theme')}出现高分看空信号，注意下跌扩散与流动性压力"
        if bearish else ""
    )
    result["action_hint"] = "优先查看近期高分主题及其价格反馈"
    result["confidence"] = min(0.86, 0.5 + 0.03 * article_count)


def _event_evidence_by_id(
    articles: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Read immutable event evidence once; legacy rows fall back to raw title."""
    event_ids = sorted({
        str(article.get("event_id") or "").strip()
        for article in articles
        if str(article.get("event_id") or "").strip()
    })
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    try:
        from db.database import get_connection

        with get_connection() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_evidence'"
            ).fetchone()
            if not table:
                return {}
            rows = conn.execute(
                "SELECT event_id, snapshot_text "
                f"FROM event_evidence WHERE event_id IN ({placeholders}) "
                "AND mapping_id IS NULL AND source_type = 'article' "
                "AND claim_key GLOB 'article_snapshot:*' "
                "AND stance = 'supports' "
                "ORDER BY reliability DESC, created_at ASC",
                tuple(event_ids),
            ).fetchall()
    except Exception:
        logger.debug("Briefing event evidence unavailable", exc_info=True)
        return {}

    evidence: Dict[str, List[str]] = defaultdict(list)
    for event_id, snapshot_text in rows:
        event_key = str(event_id)
        if len(evidence[event_key]) >= 3:
            continue
        # ``_freeze_article_evidence`` writes ``raw headline\n\nAI summary``.
        # snapshot_text is DB-trigger immutable; consume only its first segment
        # so the generated summary can never prove another generated statement.
        text = str(snapshot_text or "").split("\n\n", 1)[0].strip()
        if text and text not in evidence[event_key]:
            evidence[event_key].append(text[:500])
    return dict(evidence)


def _source_evidence_texts(
    articles: List[Dict[str, Any]],
    evidence_by_id: Dict[str, List[str]],
) -> List[str]:
    texts = []
    seen = set()
    for article in articles:
        event_id = str(article.get("event_id") or "").strip()
        frozen = evidence_by_id.get(event_id) or []
        raw_title = str(article.get("raw_title") or article.get("title") or "").strip()
        for value in (*frozen, raw_title):
            text = str(value or "").strip()[:500]
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
    return texts


def _reconcile_model_themes(
    model_themes: List[Dict[str, Any]],
    articles: List[Dict[str, Any]],
    *,
    validate_direction: bool,
) -> List[Dict[str, Any]]:
    """Make local buckets and all public narrative authoritative.

    The model may choose valid evidence IDs and, when configured, a constrained
    direction enum.  Free text is never copied into the public Briefing, so it
    cannot manufacture source-confirmation claims in an unexpected field.
    """
    by_id = {
        str(theme.get("theme_id") or "").strip(): theme
        for theme in model_themes
        if isinstance(theme, dict) and str(theme.get("theme_id") or "").strip()
    }
    by_name = {
        canonical_theme_name(theme.get("theme")): theme
        for theme in model_themes
        if isinstance(theme, dict) and str(theme.get("theme") or "").strip()
    }
    reconciled = []
    for theme_id, theme_name, group in _briefing_theme_groups(articles):
        model = dict(by_id.get(theme_id) or by_name.get(theme_name) or {})
        raw_ids = model.get("evidence_article_ids")
        supplied_ids = isinstance(raw_ids, list)
        raw_ids = raw_ids if supplied_ids else []
        valid_ids = {
            str(article.get("briefing_article_id") or ""): article
            for article in group
        }
        requested_ids = []
        for value in raw_ids:
            article_id = str(value or "").strip()
            if article_id in valid_ids and article_id not in requested_ids:
                requested_ids.append(article_id)
        if not supplied_ids:
            requested_ids = list(valid_ids)
        grounding_articles = [valid_ids[article_id] for article_id in requested_ids]
        inferred_direction = infer_theme_direction(group)
        model_direction = (
            model.get("direction")
            if model.get("direction") in {"看多", "看空", "中性"}
            else "中性"
        )
        direction = inferred_direction if validate_direction else model_direction
        display_articles = [
            _display_briefing_article(article)
            for article in briefing_representative_articles(group, limit=3)
        ]
        source_metrics = briefing_source_metrics(group)
        source_count = source_metrics["source_count"]
        deterministic = _deterministic_theme_commentary(group)
        reconciled_theme = {
            "theme_id": theme_id,
            "theme": theme_name,
            "theme_display_name": theme_display_name(theme_name),
            "direction": direction,
            "importance": _theme_importance(group),
            "insight": deterministic["insight"],
            "action": deterministic["action"],
            "background": "",
            "why_it_matters": deterministic["why_it_matters"],
            "market_impact": deterministic["market_impact"],
            "source_confirmation": deterministic["source_confirmation"],
            "evidence_article_ids": requested_ids,
            "articles": display_articles,
            "signal_count": sum(
                max(1, int(article.get("duplicate_count") or 1))
                for article in group
            ),
            "source_count": source_count,
            "top_event_source_count": source_metrics["top_event_source_count"],
            "latest_at": max((_article_timestamp(article) for article in group), default=0),
            "_theme_articles": group,
            "_grounding_evidence_articles": grounding_articles,
            "direction_validation": {
                "status": "validated" if validate_direction else "disabled",
                "model_direction": model_direction,
                "evidence_direction": inferred_direction,
            },
        }
        reconciled.append(reconciled_theme)
    reconciled.sort(key=briefing_theme_sort_key, reverse=True)
    return reconciled

_LEGACY_THEME_AGGREGATION_PROMPT = """
你是一位顶级 A 股宏观策略师。请基于以下高分情报列表，生成一份结构化交易情报简报。

要求：
1. 归纳 1-4 个核心主题
2. 每个主题给出方向判断（看多/看空/中性）、重要度（0-10）、核心洞察、操作建议、背景知识、为什么重要、影响方向
3. 给出整体市场判断、市场影响、风险提示、操作关注点

输出格式（严格 JSON，不要有任何多余文字、markdown、代码块）：
{"themes":[{"theme":"主题名","direction":"看多","importance":8.5,"insight":"洞察","action":"建议","background":"背景知识","why_it_matters":"为什么重要","market_impact":"影响方向"}],"overall_assessment":"整体判断","market_impact":"市场影响","background":"背景补充","why_it_matters":"重要性说明","risk_alert":"风险","action_hint":"操作关注点","confidence":0.85}

约束：
- theme: 2-8 个中文字
- direction: 只能是 看多/看空/中性 之一
- importance: 0-10 数字
- insight/action/background/why_it_matters/market_impact/action_hint 均为中文短句，不要换行
- risk_alert 无风险则留空字符串
- confidence: 0-1 小数
"""


_THEME_AGGREGATION_PROMPT = """
你是一位 A 股情报整理员。主题、文章归属和文章 ID 已由本地规则确定；你只能在这些边界内写有证据支持的短评。

要求：
1. 每个输入主题都输出一次，theme_id、theme 必须原样复制，不得新建、合并或改名
2. evidence_article_ids 只能引用该主题桶中的文章 ID；每句事实必须能由“来源原标题”直接支持
3. 数字、百分比、金额、日期、公司名、股票代码和政策名不得推测；源文没有就不要写
4. insight/why_it_matters/market_impact 中的事实短句优先原样摘录来源原标题；不要把传闻、计划或可能性改写成既成事实
5. 给出方向判断（看多/看空/中性）、核心洞察、操作建议、背景、为什么重要、影响方向；本地程序会再次核验方向和事实锚点
6. 给出整体市场判断、市场影响、风险提示、操作关注点
7. 不得声称“多源确认、独立来源确认、交叉验证”；该计数只能由本地 canonical event 数据生成

输出格式（严格 JSON，不要有任何多余文字、markdown、代码块）：
{"themes":[{"theme_id":"t1","theme":"主题名","evidence_article_ids":["a1"],"direction":"看多","importance":8.5,"insight":"洞察","action":"建议","background":"背景知识","why_it_matters":"为什么重要","market_impact":"影响方向"}],"overall_assessment":"整体判断","market_impact":"市场影响","background":"背景补充","why_it_matters":"重要性说明","risk_alert":"风险","action_hint":"操作关注点","confidence":0.85}

约束：
- theme: 必须原样复制输入主题身份，不得按显示长度截断
- direction: 只能是 看多/看空/中性 之一
- importance: 0-10 数字
- insight/action/background/why_it_matters/market_impact/action_hint 均为中文短句，不要换行
- risk_alert 无风险则留空字符串
- confidence: 0-1 小数
"""


def _normalize_briefing(
    result: Dict[str, Any],
    articles: Optional[List[Dict[str, Any]]] = None,
    *,
    max_themes: int = 4,
    preserve_source_confirmation: bool = False,
) -> Dict[str, Any]:
    result = result or {}
    themes = result.get("themes") or []
    if not isinstance(themes, list):
        themes = []
    normalized_by_name: Dict[str, Dict[str, Any]] = {}
    for t in themes:
        if not isinstance(t, dict):
            continue
        theme_name = canonical_theme_name(t.get("theme"))
        raw_evidence_ids = t.get("evidence_article_ids") or []
        if not isinstance(raw_evidence_ids, list):
            raw_evidence_ids = []
        evidence_article_ids = []
        for value in raw_evidence_ids[:20]:
            article_id = str(value or "").strip()[:32]
            if article_id and article_id not in evidence_article_ids:
                evidence_article_ids.append(article_id)
        normalized = {
            "theme_id": str(t.get("theme_id") or "").strip()[:32],
            "theme": theme_name,
            "theme_display_name": theme_display_name(theme_name),
            "direction": t.get("direction") if t.get("direction") in {"看多", "看空", "中性"} else "中性",
            "importance": _bounded_float_value(t.get("importance"), 0.0, 0.0, 10.0),
            "insight": str(t.get("insight") or "")[:80],
            "action": str(t.get("action") or "关注后续验证")[:60],
            "background": str(t.get("background") or result.get("background") or "")[:140],
            "why_it_matters": str(t.get("why_it_matters") or result.get("why_it_matters") or "")[:140],
            "market_impact": str(t.get("market_impact") or result.get("market_impact") or "")[:120],
            "evidence_article_ids": evidence_article_ids,
            "articles": t.get("articles") or [],
        }
        if preserve_source_confirmation:
            normalized["source_confirmation"] = str(
                t.get("source_confirmation") or ""
            )[:120]
        for metric in (
            "signal_count", "source_count", "top_event_source_count", "latest_at"
        ):
            if metric in t:
                normalized[metric] = t[metric]
        if isinstance(t.get("grounding"), dict):
            normalized["grounding"] = t["grounding"]
        if isinstance(t.get("direction_validation"), dict):
            normalized["direction_validation"] = t["direction_validation"]
        existing = normalized_by_name.get(theme_name)
        if existing is None:
            normalized_by_name[theme_name] = normalized
            continue
        # Keep the more important narrative, but retain distinct supporting
        # articles. Direction merging is commutative: neutral yields to one
        # directional signal, while a bull/bear conflict degrades to neutral.
        directions = {
            direction
            for direction in (existing.get("direction"), normalized.get("direction"))
            if direction in {"看多", "看空"}
        }
        merged_direction = next(iter(directions)) if len(directions) == 1 else "中性"
        winner = normalized if normalized["importance"] > existing["importance"] else existing
        winner = dict(winner)
        merged_articles = []
        seen_articles = set()
        for article in existing.get("articles", []) + normalized.get("articles", []):
            if not isinstance(article, dict):
                continue
            event_id = str(article.get("event_id") or "").strip()
            link = str(article.get("link") or "").strip()
            article_key = (
                f"event:{event_id}"
                if event_id
                else f"legacy-link:{link}"
                if link
                else f"legacy-title:{normalize_event_key(article.get('title', ''))}"
            )
            if article_key in seen_articles:
                continue
            seen_articles.add(article_key)
            merged_articles.append(article)
        winner["articles"] = merged_articles
        winner["evidence_article_ids"] = list(dict.fromkeys(
            existing.get("evidence_article_ids", [])
            + normalized.get("evidence_article_ids", [])
        ))[:20]
        winner["direction"] = merged_direction
        winner["importance"] = max(existing["importance"], normalized["importance"])
        normalized_by_name[theme_name] = winner
    normalized_themes = list(normalized_by_name.values())[:max(1, min(32, int(max_themes)))]
    article_timestamps = [_article_timestamp(article) for article in (articles or [])]
    latest_article_ts = max(article_timestamps, default=0)
    normalized_result = {
        "themes": normalized_themes,
        "overall_assessment": str(result.get("overall_assessment") or "暂无明确主线")[:140],
        "market_impact": str(result.get("market_impact") or "等待更多高分信号确认")[:140],
        "background": str(result.get("background") or "")[:220],
        "why_it_matters": str(result.get("why_it_matters") or "高分情报可能影响资金风险偏好与板块轮动")[:180],
        "risk_alert": str(result.get("risk_alert") or "")[:140],
        "action_hint": str(result.get("action_hint") or "关注高分主题的持续性和多源验证")[:140],
        "confidence": _bounded_float_value(result.get("confidence"), 0.0, 0.0, 1.0),
        "article_count": len(articles or result.get("articles") or []),
        "data_as_of": result.get("data_as_of") or (
            beijing_fromtimestamp(latest_article_ts).isoformat() if latest_article_ts else ""
        ),
        "generated_at": result.get("generated_at") or beijing_now().isoformat(),
    }
    if result.get("source"):
        normalized_result["source"] = str(result["source"])
    return normalized_result


def _format_articles_for_aggregation(
    articles: List[Dict[str, Any]],
    *,
    source_only: bool = False,
) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        article_id = str(a.get("briefing_article_id") or f"a{i}")
        theme_id = str(a.get("briefing_theme_id") or "")
        theme_name = _briefing_article_theme(a)
        lines.append(f"--- 文章 {article_id} ---")
        lines.append(f"主题桶：{theme_id} | {theme_name}")
        lines.append(f"来源原标题：{a.get('raw_title') or a.get('title', '')}")
        lines.append(f"标签：{a.get('tag', '')} | 评分：{a.get('score', 0)}")
        if not source_only and a.get("logic"):
            lines.append(f"AI逻辑上下文（不可作为事实证据）：{a['logic']}")
        if not source_only and a.get("summary"):
            lines.append(f"AI摘要上下文（不可作为事实证据）：{a['summary'][:160]}")
        lines.append("")
    return "\n".join(lines)


def _format_articles_for_legacy_aggregation(
    articles: List[Dict[str, Any]],
) -> str:
    """Preserve the pre-PR-2 prompt contract for the kill switch."""
    lines = []
    for index, article in enumerate(articles, 1):
        lines.append(f"--- 文章 {index} ---")
        lines.append(f"标题：{article.get('title', '')}")
        lines.append(f"标签：{article.get('tag', '')} | 评分：{article.get('score', 0)}")
        if article.get("logic"):
            lines.append(f"逻辑：{article['logic']}")
        if article.get("summary"):
            lines.append(f"摘要：{article['summary'][:160]}")
        lines.append("")
    return "\n".join(lines)


def aggregate_themes(
    articles: List[Dict[str, Any]],
    scorer: Optional[Scorer] = None,
    period_hours: int = 24,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    limit: Optional[int] = None,
    assembly_audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = config.cfg if cfg is None else cfg
    assembly_policy = normalize_briefing_policy(cfg)
    grounding_policy = normalize_briefing_grounding_policy(cfg)
    assembly_enabled = bool(assembly_policy["enabled"])
    grounding_enabled = assembly_enabled and bool(grounding_policy["enabled"])
    source_articles = list(articles or [])
    if not source_articles:
        return _normalize_briefing({
            "themes": [],
            "overall_assessment": "暂无情报",
            "market_impact": "暂无影响",
            "risk_alert": "",
            "confidence": 0.0,
        }, source_articles)

    if assembly_enabled:
        if assembly_audit is None:
            selected, assembly_audit = assemble_briefing_candidates(
                dedupe_articles(source_articles),
                cfg,
                limit=min(20, len(source_articles)) if limit is None else limit,
            )
            selected = _annotate_assembled_articles(selected, assembly_audit)
        else:
            selected = _annotate_assembled_articles(source_articles, assembly_audit)
        prompt_articles = _prepare_prompt_articles(selected)
    else:
        # Full kill switch: preserve the pre-PR-2 input, prompt and output
        # contract.  The route already applies its historical query limit.
        prompt_articles = [
            {
                key: value for key, value in dict(article).items()
                if not key.startswith("briefing_")
            }
            for article in source_articles
        ]
    if not prompt_articles:
        empty = _normalize_briefing({
            "themes": [],
            "overall_assessment": "暂无情报",
            "market_impact": "暂无影响",
            "risk_alert": "",
            "confidence": 0.0,
        }, prompt_articles)
        if assembly_enabled:
            empty["briefing_assembly"] = assembly_audit
            empty["briefing_policy_fingerprint"] = briefing_policy_fingerprint(cfg)
        return empty

    if assembly_enabled:
        context = (
            f"时段：近{period_hours}小时 | 入选文章：{len(prompt_articles)}篇 | "
            f"主题桶：{len(_briefing_theme_groups(prompt_articles))}个\n\n"
        )
        context += _format_articles_for_aggregation(
            prompt_articles[:20],
            source_only=grounding_enabled,
        )
        system_prompt = _THEME_AGGREGATION_PROMPT
    else:
        context = f"时段：近{period_hours}小时 | 文章总数：{len(prompt_articles)}篇\n\n"
        context += _format_articles_for_legacy_aggregation(prompt_articles[:20])
        system_prompt = _LEGACY_THEME_AGGREGATION_PROMPT
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]
    scorer = scorer or Scorer()
    raw_response = ""
    backends = select_llm_backends(scorer, "digest")
    if not backends:
        logger.error("[DigestEngine] No LLM backend available")
        fallback = _fallback_aggregation(
            prompt_articles,
            period_hours=period_hours,
            legacy=not assembly_enabled,
        )
        if assembly_enabled:
            fallback["briefing_assembly"] = assembly_audit
            fallback["briefing_policy_fingerprint"] = briefing_policy_fingerprint(cfg)
            fallback["articles"] = [
                _display_briefing_article(article, include_theme=True)
                for article in prompt_articles
            ]
        return fallback

    for backend in backends:
        if not backend.is_healthy():
            continue
        try:
            raw_response = scorer._call_llm(
                backend, messages, max_tokens=900, timeout=backend.timeout, purpose="digest"
            )
            logger.info("[DigestEngine] Theme aggregation succeeded on %s", backend.name)
            break
        except RateLimitTimeoutException as e:
            logger.warning("[DigestEngine] %s local rate-limit timeout: %s", backend.name, str(e)[:80])
            continue
        except Exception as e:
            logger.warning("[DigestEngine] %s failed: %s", backend.name, str(e)[:80])
            backend.record_fail()

    if not raw_response:
        logger.error("[DigestEngine] All backends failed for theme aggregation")
        fallback = _fallback_aggregation(
            prompt_articles,
            period_hours=period_hours,
            legacy=not assembly_enabled,
        )
        if assembly_enabled:
            fallback["briefing_assembly"] = assembly_audit
            fallback["briefing_policy_fingerprint"] = briefing_policy_fingerprint(cfg)
            fallback["articles"] = [
                _display_briefing_article(article, include_theme=True)
                for article in prompt_articles
            ]
        return fallback

    parsed = _parse_json_response(raw_response)
    if parsed:
        # The model may describe themes, but it never owns links or publisher
        # identity. Supporting articles are rebound exclusively to this
        # request's database candidates below.
        parsed = dict(parsed)
        parsed["themes"] = [
            {key: value for key, value in theme.items() if key != "articles"}
            for theme in (parsed.get("themes") or [])
            if isinstance(theme, dict)
        ]
        parsed = _normalize_briefing(
            parsed,
            prompt_articles,
            max_themes=32 if assembly_enabled else 4,
        )
        if assembly_enabled:
            parsed["themes"] = _reconcile_model_themes(
                parsed.get("themes") or [],
                prompt_articles,
                validate_direction=(
                    grounding_enabled and grounding_policy["validate_direction"]
                ),
            )
        else:
            for theme in parsed.get("themes") or []:
                for field in (
                    "theme_id",
                    "evidence_article_ids",
                    "grounding",
                    "direction_validation",
                ):
                    theme.pop(field, None)
            _enrich_themes_with_articles(parsed, prompt_articles)

        if grounding_enabled:
            evidence_by_id = _event_evidence_by_id(prompt_articles)
            grounded_themes = []
            for theme in parsed.get("themes") or []:
                theme_articles = theme.pop(
                    "_theme_articles",
                    theme.get("articles") or [],
                )
                grounding_articles = theme.pop(
                    "_grounding_evidence_articles",
                    theme.get("articles") or [],
                )
                evidence_texts = _source_evidence_texts(
                    grounding_articles,
                    evidence_by_id,
                )
                deterministic = (
                    {}
                    if grounding_policy["on_fail"] == "blank_commentary"
                    else _deterministic_theme_commentary(theme_articles)
                )
                grounded = ground_theme_commentary(
                    theme,
                    evidence_texts,
                    grounding_policy,
                    deterministic,
                )
                grounding_meta = grounded.get("grounding") or {}
                field_states = grounding_meta.get("fields") or {}
                for state in field_states.values():
                    if not isinstance(state, dict):
                        continue
                    distinct_hits = tuple(state.pop("distinct_hits", ()) or ())
                    strong_tokens = tuple(state.pop("strong_claim_tokens", ()) or ())
                    matched_tokens = tuple(state.pop("matched_strong_tokens", ()) or ())
                    state["distinct_hit_count"] = len(distinct_hits)
                    state["strong_claim_count"] = len(strong_tokens)
                    state["matched_strong_count"] = len(matched_tokens)
                grounded["background"] = ""
                grounded_themes.append(grounded)
            parsed["themes"] = grounded_themes
        else:
            for theme in parsed.get("themes") or []:
                theme.pop("_theme_articles", None)
                theme.pop("_grounding_evidence_articles", None)

        if assembly_enabled:
            _apply_deterministic_briefing_summary(
                parsed,
                parsed.get("themes") or [],
                period_hours,
                len(prompt_articles),
            )
            parsed["article_count"] = len(prompt_articles)
            parsed["briefing_input_count"] = len(prompt_articles)
            parsed["briefing_assembly"] = assembly_audit
            parsed["briefing_policy_fingerprint"] = briefing_policy_fingerprint(cfg)
            parsed["articles"] = [
                _display_briefing_article(article, include_theme=True)
                for article in prompt_articles
            ]
            latest_article_ts = max(
                (_article_timestamp(article) for article in prompt_articles),
                default=0,
            )
            parsed["data_as_of"] = (
                beijing_fromtimestamp(latest_article_ts).isoformat()
                if latest_article_ts else ""
            )
            parsed["generated_at"] = beijing_now().isoformat()
        parsed["source"] = "llm"
        return parsed

    logger.warning("[DigestEngine] JSON parse failed, using fallback")
    fallback = _fallback_aggregation(
        prompt_articles,
        period_hours=period_hours,
        legacy=not assembly_enabled,
    )
    if assembly_enabled:
        fallback["briefing_assembly"] = assembly_audit
        fallback["briefing_policy_fingerprint"] = briefing_policy_fingerprint(cfg)
        fallback["articles"] = [
            _display_briefing_article(article, include_theme=True)
            for article in prompt_articles
        ]
    return fallback


def _parse_json_response(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    import re
    cleaned = re.sub(r'```json\s*', '', raw)
    cleaned = re.sub(r'```\s*', '', cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    try:
        start = cleaned.index('{')
        end = cleaned.rindex('}')
        candidate = cleaned[start:end + 1]
        candidate = re.sub(r'\s+', ' ', candidate)
        candidate = re.sub(r',\s*}', '}', candidate)
        candidate = re.sub(r',\s*]', ']', candidate)
        candidate = candidate.replace("'", '"')
        candidate = re.sub(r':\s*,', ':"",', candidate)
        candidate = re.sub(r':\s*}', ':""}', candidate)
        return json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        logger.warning("[DigestEngine] Could not parse JSON from response: %s...", cleaned[:300])
        return None


def _enrich_themes_with_articles(result: Dict[str, Any], articles: List[Dict[str, Any]]) -> None:
    for theme in result.get("themes", []):
        if theme.get("articles"):
            continue
        theme_name = theme.get("theme", "")
        keywords = {theme_name.lower()} | {x for x in theme_name.lower().split() if len(x) >= 2}
        matched = []
        for idx, a in enumerate(articles):
            text = (a.get("title", "") + " " + a.get("tag", "")).lower()
            if any(kw and kw in text for kw in keywords):
                matched.append(idx)
        if not matched:
            matched = list(range(min(3, len(articles))))
        theme["articles"] = [
            enrich_article_analysis(dict(articles[idx]))
            for idx in matched[:8]
        ]


def _fallback_aggregation(
    articles: List[Dict[str, Any]],
    period_hours: int = 24,
    *,
    legacy: bool = False,
) -> Dict[str, Any]:
    if legacy:
        tag_groups = defaultdict(list)
        for article in articles:
            tag_groups[canonical_theme_name(article.get("tag", "未分类"))].append(article)
        legacy_themes = []
        for tag, group in sorted(
            tag_groups.items(),
            key=lambda item: -len(item[1]),
        )[:4]:
            average_score = sum(_article_score(article) for article in group) / len(group)
            legacy_themes.append({
                "theme": tag,
                "importance": round(average_score, 1),
                "direction": infer_theme_direction(group),
                "insight": (
                    f"涉及 {len(group)} 篇相关报道，均值评分 {average_score:.1f} 分"
                ),
                "action": "关注相关板块",
                "background": "按标签降级聚合生成",
                "why_it_matters": "多篇高分情报集中出现，可能代表资金关注度提升",
                "market_impact": "关注主题持续性",
                "articles": group[:5],
            })
        return _normalize_briefing({
            "themes": legacy_themes,
            "overall_assessment": f"近期共 {len(articles)} 篇高分情报，降级聚合展示",
            "market_impact": "高分主题集中度决定短线参考价值",
            "background": "LLM 聚合不可用，按标签和评分进行降级简报",
            "risk_alert": "",
            "action_hint": "优先查看多篇高分共振主题",
            "confidence": 0.5,
            "source": "fallback",
        }, articles)

    tag_groups = defaultdict(list)
    for a in articles:
        tag_groups[_briefing_article_theme(a)].append(a)
    themes = []
    for tag, group in sorted(tag_groups.items(), key=lambda x: -len(x[1]))[:4]:
        commentary = _deterministic_theme_commentary(group)
        clean_articles = []
        for article in group:
            clean_articles.append({
                key: value for key, value in enrich_article_analysis(
                    dict(article), trusted_only=True
                ).items()
                if key not in {
                    "briefing_article_id", "briefing_theme_id", "briefing_theme"
                }
            })
        themes.append({
            "theme": tag,
            "theme_display_name": theme_display_name(tag),
            "importance": _theme_importance(group),
            "direction": infer_theme_direction(group),
            **commentary,
            "background": "本地规则降级聚合",
            "articles": clean_articles[:5],
            "grounding": {
                "status": "local_derived",
                "fields": {
                    field: {"status": "local_derived", "reason": "deterministic_rule"}
                    for field in ("insight", "why_it_matters", "market_impact", "action")
                },
            },
            **briefing_source_metrics(group),
        })
    return _normalize_briefing({
        "themes": themes,
        "overall_assessment": _deterministic_overall_assessment(themes, period_hours),
        "market_impact": _deterministic_market_impact(themes),
        "background": "LLM 聚合不可用，按本地主题和评分规则生成简报",
        "risk_alert": "",
        "action_hint": "优先查看多篇高分共振主题",
        "confidence": 0.5,
        "source": "fallback",
    }, articles, preserve_source_confirmation=True)


def render_themes_json(result: Dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def render_themes_markdown(result: Dict[str, Any], period_hours: int) -> str:
    enhanced_briefing = bool(
        isinstance(result.get("briefing_assembly"), dict)
        and result["briefing_assembly"].get("enabled")
    )
    lines = [
        f"# A 股情报简报（近{period_hours}小时）",
        "",
        f"> 整体判断：{result.get('overall_assessment', '')}",
        f"> 市场影响：{result.get('market_impact', '')}",
        "",
    ]
    if result.get("background"):
        lines.extend([f"**背景补充**：{result['background']}", ""])
    if result.get("why_it_matters"):
        lines.extend([f"**为什么重要**：{result['why_it_matters']}", ""])
    if "quant_context" in result:
        lines.extend(
            [quant_market_context_text(result.get("quant_context")), ""]
        )
    for i, theme in enumerate(result.get("themes", []), 1):
        display_theme = str(
            theme.get("theme_display_name")
            or theme_display_name(theme.get("theme", ""))
        )
        full_theme = str(theme.get("theme") or display_theme)
        lines.append(f"## {i}. {theme.get('direction', '中性')} {display_theme}（重要度 {theme.get('importance', 0)}/10）")
        if full_theme != display_theme:
            lines.append(f"- 完整主题：{full_theme}")
        if enhanced_briefing:
            commentary_added = False
            for label, field in (("洞察", "insight"), ("入选发布方覆盖", "source_confirmation"), ("为什么现在重要", "why_it_matters"), ("影响", "market_impact"), ("背景", "background"), ("关注", "action")):
                value = str(theme.get(field) or "").strip()
                if value:
                    lines.append(f"- {label}：{value}")
                    commentary_added = True
            if not commentary_added:
                lines.append("- 评论：证据不足，已隐藏")
        else:
            lines.append(f"- 洞察：{theme.get('insight', '')}")
            lines.append(f"- 影响：{theme.get('market_impact', '')}")
            lines.append(f"- 背景：{theme.get('background', '')}")
            lines.append(f"- 关注：{theme.get('action', '')}")
        if theme.get("articles"):
            lines.append("- 相关情报：")
            for a in theme["articles"][:5]:
                lines.append(f"  - [{a.get('score', 0)}分] {a.get('title', '')}")
        lines.append("")
    if result.get("risk_alert"):
        lines.append(f"**风险提示**：{result['risk_alert']}")
        lines.append("")
    lines.append(f"---\n置信度: {result.get('confidence', 0) * 100:.0f}% | 生成时间: {result.get('generated_at', '')}")
    return "\n".join(lines)


def render_themes_html(
    result: Dict[str, Any],
    period_hours: int,
) -> str:
    direction_colors = {"看多": "#f87171", "看空": "#60a5fa", "中性": "#a1a1aa"}
    enhanced_briefing = bool(
        isinstance(result.get("briefing_assembly"), dict)
        and result["briefing_assembly"].get("enabled")
    )
    overall_assessment = str(result.get("overall_assessment") or "").replace("近48小时共 ", "近48小时 · ")
    html_parts = [
        '<div style="font-family:system-ui,-apple-system,sans-serif;padding:4px 2px 8px;max-width:960px;margin:0 auto;">',
        f'<div style="font-size:12px;color:var(--color-text-subdue,inherit);margin:0 2px 12px;line-height:1.55;">{escape_html(overall_assessment)}</div>',
    ]
    assembly_audit = result.get("briefing_assembly") or {}
    if isinstance(assembly_audit, dict) and assembly_audit.get("enabled"):
        candidate_count = int(
            assembly_audit.get("deduplicated_count")
            or assembly_audit.get("input_count")
            or 0
        )
        selected_count = int(assembly_audit.get("selected_count") or 0)
        excluded = (
            assembly_audit.get("excluded")
            if isinstance(assembly_audit.get("excluded"), dict)
            else {}
        )
        quota_suppressed = int(
            assembly_audit.get("quota_suppressed_count")
            or int(excluded.get("theme_limit") or 0)
            + int(excluded.get("theme_cap") or 0)
        )
        limit_suppressed = int(
            assembly_audit.get("limit_suppressed_count")
            or excluded.get("target_limit")
            or 0
        )
        if result.get("source") == "llm":
            grounding_policy = assembly_audit.get("grounding_policy") or {}
            if not grounding_policy.get("enabled"):
                commentary_status = "事实锚定已关闭"
            else:
                field_states = [
                    field_state
                    for theme in (result.get("themes") or [])
                    for field_state in (
                        ((theme.get("grounding") or {}).get("fields") or {}).values()
                        if isinstance(theme, dict) else []
                    )
                    if isinstance(field_state, dict)
                ]
                llm_field_states = [
                    state for state in field_states
                    if state.get("reason") != "action_not_llm_owned"
                ]
                grounded_count = sum(
                    state.get("status") == "grounded" for state in llm_field_states
                )
                commentary_status = (
                    f"事实锚定 {grounded_count}/{len(llm_field_states)}"
                    if llm_field_states else "事实锚定待验证"
                )
                if any(
                    state.get("reason") == "action_not_llm_owned"
                    and state.get("status") == "local_derived"
                    for state in field_states
                ):
                    commentary_status += " · 操作本地规则"
        else:
            commentary_status = "评论由本地规则生成"
        selection_status = f"候选 {candidate_count} · 入选 {selected_count}"
        if quota_suppressed:
            selection_status += f" · 配额剔除 {quota_suppressed}"
        if limit_suppressed:
            selection_status += f" · 展示上限 {limit_suppressed}"
        html_parts.append(
            '<div style="font-size:11px;color:var(--color-text-subdue,inherit);margin:-6px 2px 10px;line-height:1.5;">'
            f'{escape_html(selection_status)} · '
            f'{escape_html(commentary_status)}</div>'
        )
    if result.get("market_impact"):
        html_parts.append(
            f'<div style="font-size:12px;color:var(--color-text-subdue,inherit);margin-bottom:12px;line-height:1.6;">市场影响：{escape_html(result.get("market_impact", ""))}</div>'
        )
    if "quant_context" in result:
        html_parts.append(
            render_quant_market_context_html(result.get("quant_context"))
        )
    for theme in result.get("themes", []):
        dir_val = theme.get("direction", "中性")
        color = direction_colors.get(dir_val, "#757575")
        display_theme = str(
            theme.get("theme_display_name")
            or theme_display_name(theme.get("theme", ""))
        )
        full_theme = str(theme.get("theme") or display_theme)
        html_parts.append(
            f'<div style="border-left:3px solid {color};padding:10px 11px 10px 10px;margin-bottom:9px;'
            f'border-radius:14px;background:linear-gradient(180deg,hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 7%),0.62),hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 2%),0.38));'
            f'border-top:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 22%),0.12);'
            f'border-right:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 22%),0.10);'
            f'border-bottom:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 22%),0.10);'
            f'box-shadow:0 12px 28px rgba(0,0,0,0.12);">'
        )
        html_parts.append(
            f'<div title="{escape_html(f"完整主题：{full_theme}")}" style="font-size:14px;font-weight:700;color:{color};margin-bottom:4px;">'
            f'{escape_html(dir_val)} {escape_html(display_theme)} <span style="font-size:12px;color:var(--color-text-subdue,inherit);">重要度 {escape_html(theme.get("importance", 0))}/10</span></div>'
        )
        if enhanced_briefing:
            commentary_added = False
            if theme.get("insight"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-base,inherit);line-height:1.5;">洞察：{escape_html(theme.get("insight", ""))}</div>')
                commentary_added = True
            if theme.get("source_confirmation"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-base,inherit);line-height:1.5;margin-top:3px;">入选发布方覆盖：{escape_html(theme.get("source_confirmation", ""))}</div>')
                commentary_added = True
            if theme.get("why_it_matters"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-base,inherit);line-height:1.5;margin-top:3px;">为什么现在重要：{escape_html(theme.get("why_it_matters", ""))}</div>')
                commentary_added = True
            if theme.get("market_impact"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-base,inherit);line-height:1.5;">{escape_html(theme.get("market_impact", ""))}</div>')
                commentary_added = True
            if theme.get("background"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-subdue,inherit);line-height:1.5;margin-top:3px;">评分理由：{escape_html(theme.get("background", ""))}</div>')
                commentary_added = True
            if theme.get("action"):
                html_parts.append(f'<div style="font-size:12px;color:{color};font-weight:600;margin-top:3px;">后续观察：{escape_html(theme.get("action", ""))}</div>')
                commentary_added = True
            if not commentary_added:
                html_parts.append('<div style="font-size:12px;color:var(--color-text-subdue,inherit);line-height:1.5;">评论证据不足，已隐藏</div>')
        else:
            html_parts.append(f'<div style="font-size:12px;color:var(--color-text-base,inherit);line-height:1.5;">洞察：{escape_html(theme.get("insight", ""))}</div>')
            if theme.get("market_impact"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-base,inherit);line-height:1.5;">{escape_html(theme.get("market_impact", ""))}</div>')
            if theme.get("background"):
                html_parts.append(f'<div style="font-size:12px;color:var(--color-text-subdue,inherit);line-height:1.5;margin-top:3px;">评分理由：{escape_html(theme.get("background", ""))}</div>')
            html_parts.append(f'<div style="font-size:12px;color:{color};font-weight:600;margin-top:3px;">后续观察：{escape_html(theme.get("action", ""))}</div>')
        articles = theme.get("articles", [])
        if articles:
            html_parts.append('<div style="margin-top:7px;">')
            html_parts.append('<div style="font-size:11px;color:var(--color-text-subdue,inherit);font-weight:700;margin:5px 0 3px;">代表情报</div>')
            for a in articles[:3]:
                duplicate_count = int(a.get("duplicate_count") or 1)
                duplicate_note = f' <span style="color:var(--color-text-subdue,inherit);font-size:11px;">合并{duplicate_count}条</span>' if duplicate_count > 1 else ""
                article_url = escape_html(safe_http_url(a.get("link")))
                source_label = _briefing_source_label(a)
                article_time = str(a.get("time") or "").strip()
                source_time = " · ".join(value for value in (source_label, article_time) if value)
                time_note = (
                    f'<span style="color:var(--color-text-subdue,inherit);font-size:10px;white-space:nowrap;">{escape_html(source_time)}</span>'
                    if source_time else ""
                )
                html_parts.append(
                    '<div style="display:flex;align-items:flex-start;gap:7px;font-size:12px;color:var(--color-text-base,inherit);margin:4px 0;padding:6px 7px;'
                    'background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 4%),0.30);'
                    'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),0.12);'
                    'border-radius:9px;line-height:1.4;">'
                    f'<span style="display:inline-block;color:#f59e0b;background:rgba(245,158,11,0.12);'
                    f'border:1px solid rgba(245,158,11,0.24);font-weight:750;'
                    f'padding:1px 6px;border-radius:999px;white-space:nowrap;">{escape_html(a.get("score", 0))}分</span>'
                    f'<a href="{article_url}" target="_blank" rel="noopener noreferrer" '
                    f'style="color:inherit;text-decoration:none;flex:1;min-width:0;display:-webkit-box;'
                    f'-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;">{escape_html(a.get("title", ""))}{duplicate_note}</a>'
                    f'{time_note}</div>'
                )
            html_parts.append('</div>')
        html_parts.append('</div>')
    if result.get("risk_alert"):
        html_parts.append(
            f'<div style="background:rgba(245,158,11,0.12);border:1px solid rgba(245,158,11,0.24);border-radius:8px;padding:8px;margin-top:10px;font-size:12px;color:#f59e0b;">风险提示：{escape_html(result["risk_alert"])}</div>'
        )
    data_as_of = str(result.get("data_as_of") or "")
    display_data_time = data_as_of[:16].replace("T", " ") if data_as_of else "暂无数据时间"
    generated_at = str(result.get("generated_at") or "")
    display_time = generated_at[:16].replace("T", " ") if generated_at else beijing_now().strftime("%Y-%m-%d %H:%M")
    html_parts.append(
        f'<div style="font-size:11px;color:var(--color-text-subdue,inherit);margin-top:12px;text-align:right;">'
        f'数据截至：{escape_html(display_data_time)} · 生成：{escape_html(display_time)}</div></div>'
    )
    return "\n".join(html_parts)


def render_themes_rss(result: Dict[str, Any], period_hours: int) -> str:
    enhanced_briefing = bool(
        isinstance(result.get("briefing_assembly"), dict)
        and result["briefing_assembly"].get("enabled")
    )
    title = f"A 股情报 Briefing 近{period_hours}小时"
    description = escape_html(result.get("overall_assessment", ""))
    if "quant_context" in result:
        description += "\n" + escape_html(
            quant_market_context_text(result.get("quant_context"))
        )
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f'<title>{escape_xml(title)}</title>',
        '<link>http://localhost:5000/digest</link>',
        # RSS readers commonly interpret description text as HTML. Double-encode
        # untrusted markup so the parsed RSS value remains inert HTML text.
        f'<description>{escape_xml(description)}</description>',
        '<language>zh-CN</language>',
        f'<lastBuildDate>{rfc822_date()}</lastBuildDate>',
    ]
    for idx, theme in enumerate(result.get("themes", []), 1):
        display_theme = str(
            theme.get("theme_display_name")
            or theme_display_name(theme.get("theme", ""))
        )
        full_theme = str(theme.get("theme") or display_theme)
        item_title = f'{theme.get("direction", "中性")} {display_theme}｜{theme.get("importance", 0)}分'
        if enhanced_briefing:
            desc_lines = (
                [f"完整主题：{full_theme}"] if full_theme != display_theme else []
            )
            for label, field in (("洞察", "insight"), ("入选发布方覆盖", "source_confirmation"), ("为什么现在重要", "why_it_matters"), ("影响", "market_impact"), ("背景", "background"), ("关注", "action")):
                value = str(theme.get(field) or "").strip()
                if value:
                    desc_lines.append(f"{label}：{value}")
            desc = "\n".join(desc_lines or ["评论：证据不足，已隐藏"])
        else:
            desc = "\n".join([
                f'洞察：{theme.get("insight", "")}',
                f'影响：{theme.get("market_impact", "")}',
                f'背景：{theme.get("background", "")}',
                f'关注：{theme.get("action", "")}',
            ])
        safe_item_title = escape_html(item_title)
        safe_desc = escape_html(desc)
        parts.extend([
            '<item>',
            f'<title><![CDATA[{escape_cdata(safe_item_title)}]]></title>',
            f'<link>http://localhost:5000/digest#theme-{idx}</link>',
            f'<guid isPermaLink="false">briefing-{escape_xml(result.get("generated_at", ""))}-{idx}</guid>',
            f'<description><![CDATA[{escape_cdata(safe_desc)}]]></description>',
            '</item>',
        ])
    parts.append('</channel></rss>')
    return "\n".join(parts)


def render_digest(result: Dict[str, Any], format_type: str = "json", period_hours: int = 24) -> tuple:
    if format_type == "html":
        return render_themes_html(result, period_hours), "text/html"
    if format_type == "markdown":
        return render_themes_markdown(result, period_hours), "text/plain"
    if format_type == "rss":
        return render_themes_rss(result, period_hours), "application/rss+xml"
    return render_themes_json(result), "application/json"
