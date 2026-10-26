"""Render the short-lived ``/glance-sse`` HTML polling widget.

The historical name is retained for the public route.  This module does not
implement Server-Sent Events; Glance polls SQLite-backed HTML every 30 seconds.
"""

import math
import time
import logging
import re as _re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from flask import Response, request
from core.config import load_config
from core.digest_engine import dedupe_articles, enrich_article_analysis
from scoring_provenance import (
    NON_SUBSTANTIVE_SCORE_PATHS,
    PUBLIC_SCORE_PATHS,
)
from services.intelligence_query import IRRELEVANT_TAGS
from services.news_reading import (
    attach_score_contract_labels,
    preferred_news_title,
    render_news_card,
    restore_reading_titles,
    score_contract_label,
)
from db.event_core import canonical_event_id
from utils.helpers import (
    escape_html,
    normalize_direction,
    plain_text_from_markup,
    safe_http_url,
)

logger = logging.getLogger(__name__)
_PUBLIC_REALTIME_SCORE_PATHS = tuple(sorted(
    PUBLIC_SCORE_PATHS - {"similarity"}
))

# ── 标签标准化 ──────────────────────────────────────────────

# 英文/系统标签 → 简洁显示
_TAG_DISPLAY_MAP = {
    "FastPass": "⚡",
    "重要异动": "",
    "未知": "",
    "主观标签": "",
}

# 重复前缀清洗（如 颠覆颠覆→颠覆）
_TAG_DEDUP_RE = _re.compile(r'(.{2,3})\1')

def _normalize_tag(tag: str) -> str:
    """标准化标签显示：英文→中文、长标签截断、空标签隐藏"""
    if not tag or not tag.strip():
        return ""
    tag = tag.strip()
    # 映射表
    mapped = _TAG_DISPLAY_MAP.get(tag)
    if mapped is not None:
        return mapped
    # 纯数字 → 隐藏
    if tag.isdigit():
        return ""
    # 去重前缀（颠覆颠覆→颠覆）
    tag = _TAG_DEDUP_RE.sub(r'\1', tag)
    # 截断：>5 字符 → 前5字 + …
    if len(tag) > 5:
        tag = tag[:5] + "…"
    return tag


_TITLE_MAX_LEN = 72
_LOGIC_MAX_LEN = 96
_DISPATCH_PREFIX_RE = _re.compile(r'^(?:财联社|格隆汇|华尔街见闻|金十数据)\s*\d{1,2}月\d{1,2}[日电]讯[，,]?\s*')
_QUESTION_SECTION_RE = _re.compile(r'\s+[一二三四五六七八九十]+、.*$')
_LONG_BRIEFING_SECTION_RE = _re.compile(r'(?:债市要闻|公开市场|一级市场|二级市场|海外债市|今日关注|24小时热点|相关情报)\s*[：:]?.*$', _re.S)
_BRACKET_SECTION_RE = _re.compile(r'【[^】]{12,}】')
_BREAKFAST_RADIO_RE = _re.compile(r"早餐\s*FM[\s-]*Radio", _re.IGNORECASE)
_FASTPASS_HINT_RE = _re.compile(
    r'(?:\s*[;；]\s*)?FastPass候选\s*[:：]\s*[^;；]*'
)


def _clip_text(text: str, max_len: int = _TITLE_MAX_LEN) -> str:
    text = _re.sub(r'\s+', ' ', str(text or '')).strip()
    max_len = max(0, int(max_len))
    if len(text) <= max_len:
        return text
    if max_len <= 0:
        return ""
    if max_len == 1:
        return "…"
    prefix = text[:max_len - 1].rstrip(' ，,。；;')
    return (prefix or text[:max_len - 1]) + '…'


def _extract_compact_title(text: str) -> str:
    text = _re.sub(r'\s+', ' ', str(text or '')).strip()
    if not text:
        return ""

    for pattern in (
        r'([^。！？；;]{6,70}?负责人[^。！？；;]{0,40}?解读)',
        r'([^。！？；;]{6,70}?就[^。！？；;]{0,40}?进行解读)',
        r'([^。！？；;]{6,70}?取得[^。！？；;]{0,30}?成果)',
    ):
        match = _re.search(pattern, text)
        if match:
            return _clip_text(match.group(1))

    first_sentence = _re.split(r'[。！？；;]', text, maxsplit=1)[0]
    first_sentence = _re.sub(r'^据[^，,]{2,30}[，,]', '', first_sentence).strip()
    return _clip_text(first_sentence or text)


def _sanitize_card_title(title: str) -> str:
    """清洗实时情报卡片标题；可见结果严格不超过 72 个字符。"""
    title_clean = plain_text_from_markup(title)
    title_clean = _re.sub(r'^\[⭐[\d.]+\s*[^\]]*\]\s*', '', title_clean)
    title_clean = _re.sub(r'\s*💡.*$', '', title_clean)
    title_clean = _QUESTION_SECTION_RE.sub('', title_clean)
    title_clean = _LONG_BRIEFING_SECTION_RE.sub('', title_clean)
    title_clean = _re.sub(r'\s+', ' ', title_clean).strip()
    if not title_clean:
        return ""

    if len(title_clean) > _TITLE_MAX_LEN or _DISPATCH_PREFIX_RE.match(title_clean):
        compact = _DISPATCH_PREFIX_RE.sub('', title_clean).strip()
        title_clean = _extract_compact_title(compact)

    return _clip_text(title_clean)


def _preferred_card_title(raw_title: str = "", title: str = "") -> str:
    """Choose between persisted title candidates without reconstruction.

    A provider/model-truncated title is never completed from the network or by
    guessing.  A cleaned scored title may win only when it strictly extends the
    cleaned source title (or vice versa); divergent candidates conservatively
    keep ``raw_title``.  The visible result is always bounded to 72 characters.
    """
    raw_clean = _sanitize_card_title(raw_title)
    title_clean = _sanitize_card_title(title)
    if not raw_clean:
        return title_clean
    if not title_clean or title_clean == raw_clean:
        return raw_clean

    raw_stem = raw_clean.removesuffix("…")
    title_stem = title_clean.removesuffix("…")
    if title_stem.startswith(raw_stem) and len(title_stem) > len(raw_stem):
        return title_clean
    if raw_stem.startswith(title_stem) and len(raw_stem) > len(title_stem):
        return raw_clean
    return raw_clean


def _sanitize_card_logic(logic: str) -> str:
    """将模型逻辑限制为一句可读摘要，防止整篇早报/研报正文进入实时卡片。"""
    logic_clean = _FASTPASS_HINT_RE.sub('', str(logic or ''))
    logic_clean = _re.sub(r'^[\s;；]+|[\s;；]+$', '', logic_clean)
    logic_clean = _re.sub(r'^\s*逻辑[：:]\s*', '', logic_clean)
    logic_clean = _re.sub(r'\s*相关情报\s*$', '', logic_clean)
    logic_clean = _LONG_BRIEFING_SECTION_RE.sub('', logic_clean)
    logic_clean = _QUESTION_SECTION_RE.sub('', logic_clean)
    logic_clean = _re.sub(r'\s+', ' ', logic_clean).strip()
    if not logic_clean:
        return ""

    bracket_count = len(_BRACKET_SECTION_RE.findall(logic_clean))
    if bracket_count >= 2 or len(logic_clean) > _LOGIC_MAX_LEN * 2:
        logic_clean = _extract_compact_title(logic_clean)

    return _clip_text(logic_clean, _LOGIC_MAX_LEN)

# ── Glance 卡片端点 ──────────────────────────────────────
# 先展示近 12 小时信号，不足时再用 48 小时内内容补位。
REALTIME_PRIMARY_HOURS = 12
REALTIME_FEED_HOURS = 48
REALTIME_MAX_PER_SOURCE = 2
REALTIME_CARD_LIMIT = 15
REALTIME_STRONG_SCORE = 8.5

_REALTIME_SECTION_LABELS = {
    "fresh_strong": "近12小时 · 强信号",
    "fresh_observe": "近12小时 · 重点观察",
    "fallback_strong": "12–48小时回看 · 强信号",
    "fallback_observe": "12–48小时回看 · 重点观察",
}

_SOURCE_BRAND_MARKERS = (
    (("wallstreetcn", "华尔街见闻"), "华尔街见闻"),
    (("gelonghui", "格隆汇"), "格隆汇"),
    (("36kr", "36氧", "36氪"), "36氪"),
    (("jin10", "金十"), "金十数据"),
    (("10jqka", "同花顺"), "同花顺"),
    (("/cls/", "财联社"), "财联社"),
    (("caixin", "财新"), "财新网"),
    (("eastmoney", "东方财富"), "东方财富"),
    (("/sse/", "上交所"), "上交所"),
    (("/szse/", "深交所"), "深交所"),
    (("/gov/csrc/", "证监会"), "证监会"),
)


def _cutoff_ts(hours: int = REALTIME_FEED_HOURS) -> int:
    return int(time.time()) - max(1, int(hours)) * 3600


def _is_recent_entry(entry: dict, hours: int = REALTIME_FEED_HOURS) -> bool:
    ts = entry.get("time", 0) or entry.get("created_at", 0)
    try:
        return int(ts) >= _cutoff_ts(hours)
    except (TypeError, ValueError):
        return False


def _entry_timestamp(entry: dict) -> int:
    """返回卡片时间戳；非法值按 0 处理。"""
    ts = entry.get("time", 0) or entry.get("created_at", 0) or entry.get("time_ts", 0)
    try:
        return int(ts)
    except (TypeError, ValueError):
        return 0


_CARD_TZ = ZoneInfo("Asia/Shanghai")


def _card_ingest_time_label(time_ts, *, now_ts: Optional[int] = None) -> str:
    """Format news_cache.created_at for Glance.

    This is ingest/storage time, not publisher time.  The visible label must
    say so, and must include a calendar day so overnight cards are not read
    as "this morning".
    """
    try:
        stamp = int(time_ts or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    moment = datetime.fromtimestamp(stamp, _CARD_TZ)
    if now_ts is None:
        now_stamp = time.time()
    else:
        try:
            now_stamp = int(now_ts)
        except (TypeError, ValueError):
            return ""
        if now_stamp <= 0:
            return ""
    now = datetime.fromtimestamp(now_stamp, _CARD_TZ)
    clock = moment.strftime("%H:%M")
    delta_days = (now.date() - moment.date()).days
    if delta_days == 0:
        day = "今天"
    elif delta_days == 1:
        day = "昨天"
    else:
        day = moment.strftime("%m-%d")
    return f"收录 {day} {clock}"



NEUTRAL_EVIDENCE_HINTS = {
    "theme": "保守处理说明：主题关联未通过现有来源校验",
    "facts": "保守处理说明：事实表述未通过现有来源校验",
    "contract": "保守处理说明：输出格式或字段未通过校验",
    "initial": "保守处理说明：初次输出为保守中性，具体原因未记录",
    "": "保守处理说明：具体原因未完整记录",
}


def _neutral_evidence_note(entry: dict) -> str:
    """Record-backed note for safe-neutral cards; never generated text."""
    path = str(entry.get("score_path") or "").strip().casefold()
    if path not in NON_SUBSTANTIVE_SCORE_PATHS:
        return ""
    reason = str(entry.get("safe_neutral_reason") or "").strip()
    return NEUTRAL_EVIDENCE_HINTS.get(reason, NEUTRAL_EVIDENCE_HINTS[""])


def _entry_importance(entry: dict) -> float:
    """Return the unsigned importance score used by the realtime card."""
    try:
        return float(entry.get("importance_score", entry.get("score", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_realtime_min_score(raw_value: Optional[str]) -> Optional[float]:
    """Parse the optional public score filter and reject non-finite values."""
    if raw_value is None or not str(raw_value).strip():
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_score must be a number from 0 through 10") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 10.0:
        raise ValueError("min_score must be a finite number from 0 through 10")
    return value


def _strategy_version_matches(cfg: dict, stored_version: object) -> bool:
    """Mirror the unified public-query strategy compatibility contract."""
    current = str((cfg or {}).get("scoring_strategy_version") or "").strip()
    if not current:
        return True
    stored = str(stored_version or "").strip()
    if not stored:
        return False
    if ":" in current:
        return stored.startswith(current.split(":", 1)[0] + ":")
    return stored == current


def _entry_matches_public_contract(entry: dict, cfg: dict) -> bool:
    """Apply the unified high-score public-read exclusions to cache entries."""
    if str(entry.get("score_path") or "") in {
        "similarity",
        "quality_quarantine",
    }:
        return False
    if str(entry.get("raw_tag", entry.get("tag")) or "") in IRRELEVANT_TAGS:
        return False
    return _strategy_version_matches(cfg, entry.get("strategy_version"))


def _source_name_from_url(feed_url: str) -> str:
    """配置缺少名称时生成一个可读的来源名。"""
    identity = str(feed_url or "").strip()
    lowered = identity.lower()
    for markers, label in _SOURCE_BRAND_MARKERS:
        if any(marker.lower() in lowered for marker in markers):
            return label
    try:
        parsed = urlparse(identity)
        path_parts = [part for part in parsed.path.split("/") if part]
        return (path_parts[0] if path_parts else parsed.netloc).strip()
    except Exception:
        return ""


def _brand_bucket(identity: str) -> str:
    """Return a canonical publisher only when this single identity proves it."""
    lowered = str(identity or "").strip().lower()
    if not lowered:
        return ""
    for markers, label in _SOURCE_BRAND_MARKERS:
        if any(marker.lower() in lowered for marker in markers):
            return label
    return ""


_RUNTIME_FEED_HOSTS = {"127.0.0.1", "::1", "localhost", "rsshub", "ai-filter-web"}


def _runtime_feed_url_equivalent(configured_url: str, candidate_url: str) -> bool:
    """Allow only known loopback/Docker host rewrites of the configured URL."""
    configured_url = str(configured_url or "").strip()
    candidate_url = str(candidate_url or "").strip()
    if not configured_url or not candidate_url:
        return False
    if configured_url == candidate_url:
        return True
    try:
        configured = urlparse(configured_url)
        candidate = urlparse(candidate_url)
        configured_port = configured.port or (443 if configured.scheme == "https" else 80)
        candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
    except (TypeError, ValueError):
        return False
    return (
        configured.hostname in _RUNTIME_FEED_HOSTS
        and candidate.hostname in _RUNTIME_FEED_HOSTS
        and configured_port == candidate_port
        and configured.path == candidate.path
        and configured.params == candidate.params
        and configured.query == candidate.query
    )


def _enabled_feed_config(cfg: dict, feed_url: str) -> dict:
    """Resolve only configured, enabled feeds; unknown/ad-hoc input fails closed."""
    from db.database import get_feed_config

    feed_url = str(feed_url or "").strip()
    feed_cfg = get_feed_config(cfg, feed_url)
    if not feed_cfg or not feed_cfg.get("enabled", True):
        return {}
    if not _runtime_feed_url_equivalent(feed_cfg.get("url", ""), feed_url):
        return {}
    return feed_cfg


def _source_bucket(entry: dict) -> str:
    """将同一出版方的多个子 feed 归入同一配额桶。

    Source identity is evaluated in trust order.  In particular, an article URL
    mentioning a different publisher must not override the configured feed/source
    identity and split one publisher across several quota buckets.
    """
    source = str(entry.get("source") or "").strip()
    feed_url = str(entry.get("feed_url") or "").strip()
    link = str(entry.get("link") or "").strip()
    source_brand = _brand_bucket(source)
    if source_brand:
        return source_brand
    feed_brand = _brand_bucket(feed_url)
    if feed_brand:
        return feed_brand
    if source:
        return source.casefold()
    if feed_url:
        return feed_url.casefold()
    link_brand = _brand_bucket(link)
    if link_brand:
        return link_brand
    try:
        domain = urlparse(link).netloc.casefold()
        if domain:
            return domain
    except Exception:
        pass
    return "unknown"


def _select_balanced_cards(
    entries: list[dict],
    limit: int = REALTIME_CARD_LIMIT,
    primary_hours: int = REALTIME_PRIMARY_HOURS,
    fallback_hours: int = REALTIME_FEED_HOURS,
    max_per_source: Optional[int] = REALTIME_MAX_PER_SOURCE,
    now_ts: Optional[int] = None,
) -> tuple[list[dict], int, int]:
    """Freshness first; within each window show strong signals before observations."""
    if limit <= 0:
        return [], 0, 0

    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    fallback_cutoff = now_ts - max(1, int(fallback_hours)) * 3600
    primary_cutoff = now_ts - max(1, int(primary_hours)) * 3600
    prepared = []
    for entry in entries or []:
        ts = _entry_timestamp(entry)
        if ts < fallback_cutoff:
            continue
        prepared.append({**entry, "time_ts": ts})

    deduped = dedupe_articles(prepared)
    fresh = [entry for entry in deduped if _entry_timestamp(entry) >= primary_cutoff]
    fallback = [entry for entry in deduped if _entry_timestamp(entry) < primary_cutoff]
    selected: list[dict] = []
    source_counts: dict[str, int] = {}
    cap = None if max_per_source is None else max(1, int(max_per_source))

    def _spread_group(group: list[dict]) -> list[dict]:
        """Interleave publishers inside one observation group only."""
        pending = list(group)
        spread: list[dict] = []
        while pending:
            pick_index = 0
            if spread:
                previous_bucket = _source_bucket(spread[-1])
                pick_index = next(
                    (
                        index
                        for index, entry in enumerate(pending)
                        if _source_bucket(entry) != previous_bucket
                    ),
                    0,
                )
            spread.append(pending.pop(pick_index))
        return spread

    def _take(pool: list[dict], section: str, *, spread: bool) -> None:
        added: list[dict] = []
        for entry in pool:
            if len(selected) + len(added) >= limit:
                break
            bucket = _source_bucket(entry)
            if cap is not None and source_counts.get(bucket, 0) >= cap:
                continue
            added.append({**entry, "_realtime_section": section})
            source_counts[bucket] = source_counts.get(bucket, 0) + 1
        selected.extend(_spread_group(added) if spread else added)

    def _split(pool: list[dict]) -> tuple[list[dict], list[dict]]:
        strong = [entry for entry in pool if _entry_importance(entry) >= REALTIME_STRONG_SCORE]
        observe = [entry for entry in pool if _entry_importance(entry) < REALTIME_STRONG_SCORE]
        strong.sort(
            key=lambda entry: (_entry_importance(entry), _entry_timestamp(entry)),
            reverse=True,
        )
        return strong, observe

    fresh_strong, fresh_observe = _split(fresh)
    fallback_strong, fallback_observe = _split(fallback)

    _take(fresh_strong, "fresh_strong", spread=False)
    _take(fresh_observe, "fresh_observe", spread=True)
    fresh_count = len(selected)
    _take(fallback_strong, "fallback_strong", spread=False)
    _take(fallback_observe, "fallback_observe", spread=True)
    fallback_count = len(selected) - fresh_count
    return selected, fresh_count, fallback_count


def _is_realtime_card_noise(entry: dict) -> bool:
    # Only the publisher title identifies this named roundup. Generated
    # titles/rationales and ordinary breakfast business news are not evidence.
    if _BREAKFAST_RADIO_RE.search(str(entry.get("raw_title") or "")):
        return True
    text = f"{entry.get('title', '')} {entry.get('logic', '')}"
    omnibus_markers = ("债市早参", "财经早知道", "早知道", "早报", "晨报", "24小时热点", "债市要闻")
    if any(marker in text for marker in omnibus_markers):
        return True
    if len(str(entry.get("logic") or "")) > 800 and len(_BRACKET_SECTION_RE.findall(str(entry.get("logic") or ""))) >= 3:
        return True
    return False


_UNMAPPED_TITLE_NOTE = "标题未匹配到现有主题规则，暂不展示资产关联"
_UNMAPPED_TITLE_READING_STYLE = (
    "body:has(.ai-home-institutional-masthead) #sse-container "
    "> .home-news-card:nth-of-type(1) > .home-news-unmapped-note "
    "{color:#d0dbe1 !important;}"
)


def _render_unmapped_title_note() -> str:
    """Single-line Glance copy when the title matches no theme rule."""
    return (
        '<div class="home-news-unmapped-note" '
        'style="margin-top:8px;color:var(--color-text-subdue,inherit);'
        'font-size:11px;line-height:1.5;">'
        f'{escape_html(_UNMAPPED_TITLE_NOTE)}</div>'
    )


def _render_rule_pool_details(
    assets: str,
    sectors: str,
    stocks: str,
    score_reason: str,
    watch: str,
    mapping_note: str,
    mapped_theme: str = "",
) -> str:
    """Collapsed Glance block for theme background / rule-pool examples."""
    theme_line = (
        f'标题匹配主题：{escape_html(mapped_theme)}（规则标签，不是本文点名）<br>'
        if str(mapped_theme or "").strip()
        else ""
    )
    return (
        f'<details style="margin-top:8px;">'
        f'<summary style="cursor:pointer;color:var(--color-text-subdue,inherit);'
        f'font-size:11px;font-weight:700;line-height:1.5;user-select:none;">'
        f'主题背景／规则池（非本文点名、不代表受益）</summary>'
        f'<div style="margin-top:6px;color:var(--color-text-subdue,inherit);font-size:11px;'
        f'line-height:1.5;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 4%),0.24);'
        f'border-radius:9px;padding:7px 8px;">{theme_line}'
        f'主题资产参考：{escape_html(assets)}<br>'
        f'相关板块：{escape_html(sectors)}<br>规则池示例：{escape_html(stocks)}<br>'
        f'主题背景（规则生成）：{escape_html(score_reason)}<br>'
        f'待核对事项：{escape_html(watch)}{mapping_note}</div>'
        f'</details>'
    )


def _render_cards(entries: list[dict], *, reading: bool = False) -> str:
    """将条目列表渲染为 HTML 卡片"""
    if not entries:
        return ""

    renderable = []
    for entry in entries:
        # Public cards may display model-derived ``tag``/``logic``, but those
        # fields are not evidence for an asset template.  Recompute every
        # mapping field from the bounded publisher-owned title fields so a
        # hallucinated label cannot turn an unrelated article into an AI card.
        enriched = enrich_article_analysis(entry, trusted_only=True)
        title = _preferred_card_title(
            enriched.get("raw_title", ""), enriched.get("title", "")
        )
        if not title:
            continue
        reading_title = enriched.get("_reading_title") or preferred_news_title(
            enriched.get("raw_title", ""), enriched.get("title", "")
        )
        renderable.append({**enriched, "title": title, "_reading_title": reading_title})
    if not renderable:
        return ""

    cards = []
    section_counts: dict[str, int] = {}
    for entry in renderable:
        section = str(entry.get("_realtime_section") or "")
        if section in _REALTIME_SECTION_LABELS:
            section_counts[section] = section_counts.get(section, 0) + 1
    last_section = ""
    for e in renderable:
        section = str(e.get("_realtime_section") or "")
        if section in _REALTIME_SECTION_LABELS and section != last_section:
            section_label = _REALTIME_SECTION_LABELS[section]
            section_count = section_counts.get(section, 0)
            cards.append(
                '<div style="display:flex;align-items:center;gap:8px;margin:12px 2px 8px;'
                'color:var(--color-text-subdue,inherit);font-size:11px;font-weight:850;'
                'letter-spacing:.04em;">'
                f'<span>{escape_html(section_label)}</span>'
                '<span style="height:1px;flex:1;background:rgba(148,163,184,.22);"></span>'
                f'<span>{section_count}条</span></div>'
            )
            last_section = section
        cards.append(_render_one_card(e, reading=reading))
    return "".join(cards)


def _render_one_card(e: dict, *, reading: bool = False) -> str:
    """Share trusted metadata and rule-pool construction across both layouts."""
    from services.news_mentions import render_title_mentions

    selection_threshold = e.get("_selection_threshold")
    score = float(e.get("importance_score", e.get("score", 0)) or 0)
    direction = normalize_direction(e.get("direction", ""), text=e.get("title", ""))
    if score >= 8.5:
        accent, badge_bg, level = "#f87171", "rgba(248,113,113,.13)", "强信号"
    elif score >= 7.5:
        accent, badge_bg, level = "#fbbf24", "rgba(251,191,36,.13)", "关注"
    else:
        accent, badge_bg, level = "#94a3b8", "rgba(148,163,184,.13)", "观察"
    direction_color = (
        "#f87171" if direction == "看多"
        else "#60a5fa" if direction == "看空"
        else "#94a3b8"
    )
    direction_border = direction_color + "4d"
    raw_link = e.get("link") or ""
    title = e["title"]
    source = str(e.get("source") or "").strip()
    # Only the trusted title mapping may supply the public taxonomy.
    mapped_theme = str(e.get("mapped_theme") or "").strip()
    try:
        mapping_confidence = float(e.get("mapping_confidence") or 0.0)
    except (TypeError, ValueError):
        mapping_confidence = 0.0
    has_trusted_mapping = bool(
        mapping_confidence > 0.0
        and mapped_theme not in {"", "主题"}
        and (str(e.get("assets") or "").strip() or str(e.get("sectors") or "").strip())
    )
    tag = _normalize_tag(mapped_theme) if has_trusted_mapping else ""
    if raw_link.startswith("jin10://") or not raw_link:
        link = "https://www.jin10.com/"
    else:
        link = safe_http_url(raw_link)
    time_ts = e.get("time", 0) or e.get("created_at", 0)
    time_str = _card_ingest_time_label(time_ts)
    if score >= REALTIME_STRONG_SCORE:
        selection_reason = f"统一强信号 ≥{REALTIME_STRONG_SCORE:.1f}"
    else:
        try:
            selection_reason = f"源门槛 ≥{float(selection_threshold):.1f}"
        except (TypeError, ValueError):
            selection_reason = "重点观察"
    meta = " · ".join(
        [str(x) for x in (source, level, selection_reason, time_str,
                         _neutral_evidence_note(e), score_contract_label(e)) if x]
    )
    duplicate_note = f" · 合并{e.get('duplicate_count')}条" if int(e.get("duplicate_count") or 1) > 1 else ""
    title_mentions_html, replaces_unmapped = render_title_mentions(e)
    if has_trusted_mapping:
        rule_pool_html = _render_rule_pool_details(
            e.get("assets", "") or "暂无可信映射",
            e.get("sectors", "") or "暂无可信映射",
            e.get("stocks", "") or "未提供个股示例",
            e.get("score_reason", ""),
            e.get("watch", ""),
            "<br>仅按标题匹配主题；背景不是本条新闻的评分依据，示例不代表本文提及或已确认受益。",
            tag,
        )
    elif replaces_unmapped:
        rule_pool_html = ""
    else:
        rule_pool_html = _render_unmapped_title_note()
    rule_pool_html = title_mentions_html + rule_pool_html
    if reading:
        return render_news_card(
            title=preferred_news_title(title=e.get("_reading_title", title)), link=link,
            source=source, time_label=time_str, score=score, direction=direction,
            selection_reason=selection_reason, level=level, evidence_note=_neutral_evidence_note(e),
            duplicate_note=duplicate_note, rule_pool_html=rule_pool_html,
            score_source_label=score_contract_label(e),
        )
    return (
        f'<article style="position:relative;overflow:hidden;border-radius:16px;'
        f'padding:14px 14px 12px;margin-bottom:11px;'
        f'background:linear-gradient(180deg,hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 7%),0.66),hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 2%),0.42));'
        f'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 22%),0.13);'
        f'box-shadow:0 12px 30px rgba(0,0,0,.14);backdrop-filter:blur(10px);">'
        f'<div style="position:absolute;left:0;top:0;bottom:0;width:3px;background:{accent};opacity:.9;"></div>'
        f'<div style="display:flex;gap:12px;align-items:flex-start;">'
        f'<div style="flex:1;min-width:0;">'
        f'<a href="{escape_html(link)}" target="_blank" rel="noopener noreferrer" style="display:-webkit-box;color:var(--color-text-base,inherit);'
        f'font-size:14px;font-weight:750;text-decoration:none;line-height:1.48;letter-spacing:-.01em;overflow:hidden;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word;">{escape_html(title)}</a>'
        f'<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">'
        f'<span style="display:inline-flex;padding:2px 7px;border-radius:999px;border:1px solid {direction_border};color:{direction_color};font-size:11px;font-weight:800;">{escape_html(direction)}</span>'
        f'<span style="font-size:11px;color:var(--color-text-subdue,inherit);font-weight:600;">{escape_html(meta)}{escape_html(duplicate_note)}</span>'
        f'</div>'
        + rule_pool_html
        + f'</div>'
        f'<div style="flex-shrink:0;text-align:right;">'
        f'<div style="background:{badge_bg};color:{accent};padding:4px 8px;border-radius:999px;'
        f'border:1px solid {accent}33;font-size:12px;font-weight:850;">重要度 {score:.1f}/10</div>'
        f'</div></div></article>'
    )




def _row_safe_neutral_reason(row) -> str:
    """Read safe_neutral_reason; legacy rows without the column stay unknown."""
    try:
        return str(row["safe_neutral_reason"] or "")
    except (KeyError, IndexError):
        return ""


def _resolve_event_version(conn, event_id: str):
    """Resolve canonical event id and current version; fail closed."""
    try:
        event_id = canonical_event_id(conn, event_id)
        version_row = conn.execute(
            "SELECT version FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return event_id, max(1, int(version_row[0])) if version_row else 0
    except Exception:
        return "", 0



def _cache_score_source_projection(columns, event_columns) -> str:
    """Describe the actual COALESCE owners without borrowing cache identity."""
    cache_version = "cache.strategy_version" if "strategy_version" in columns else "''"
    event_version = "event.strategy_version" if "strategy_version" in event_columns else "''"
    has_event = {"event_id", "event_version"}.issubset(columns)
    priority_version = event_version
    if "merged_into" in event_columns:
        # Merge/reconcile retains the descendant maximum, not its scoring
        # owner. The root's latest contract therefore cannot identify this
        # aggregate priority, even after a later root rescore. Keep the value
        # and selection unchanged; only its contract becomes unknown.
        priority_version = (
            "CASE WHEN EXISTS (SELECT 1 FROM events AS merged_member "
            "WHERE merged_member.merged_into = event.event_id) "
            f"THEN '' ELSE {event_version} END"
        )
    score_version = (
        f"CASE WHEN event.priority IS NOT NULL THEN {priority_version} ELSE {cache_version} END"
        if has_event else cache_version
    )
    direction_version = "''"
    if "direction" in columns:
        direction_version = (
            f"CASE WHEN event.direction IS NOT NULL THEN {event_version} ELSE {cache_version} END"
            if has_event else cache_version
        )
    return (f"{score_version} AS display_score_strategy_version, "
            f"{direction_version} AS display_direction_strategy_version, "
            f"{cache_version} AS display_logic_strategy_version")


def _cache_column_projections(columns, event_columns=()) -> tuple:
    """Legacy-safe SELECT projections for the realtime cache read path."""
    has_event_identity = {"event_id", "event_version"}.issubset(columns)
    return (
        has_event_identity,
        "cache.event_id, cache.event_version"
        if has_event_identity
        else "'' AS event_id, 0 AS event_version",
        "COALESCE(event.direction, cache.direction) AS direction"
        if has_event_identity and "direction" in columns
        else "cache.direction AS direction"
        if "direction" in columns
        else "'中性' AS direction",
        "COALESCE(event.priority, cache.score)"
        if has_event_identity else "cache.score",
        "cache.raw_title AS raw_title"
        if "raw_title" in columns else "'' AS raw_title",
        "cache.score_path AS score_path"
        if "score_path" in columns else "'' AS score_path",
        "cache.strategy_version AS strategy_version"
        if "strategy_version" in columns else "'' AS strategy_version",
        "cache.safe_neutral_reason AS safe_neutral_reason"
        if "safe_neutral_reason" in columns
        else "'' AS safe_neutral_reason",
        _cache_score_source_projection(columns, event_columns),
    )


def _cache_table_columns(conn, table: str) -> set[str]:
    """Inspect a bounded cache/event schema without opening another connection."""
    if table not in {"news_cache", "events"}:
        raise ValueError("Unsupported realtime table")
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _cache_card_entry(row, *, title, raw_title, link, event_id, event_version, feed_url, source):
    """Keep query/selection fields stable and carry the full stored display title."""
    entry = {
        "event_id": event_id,
        "event_version": event_version,
        "title": title.strip(),
        "raw_title": raw_title,
        "_reading_title": preferred_news_title(raw_title, row["title"] or ""),
        "link": link,
        "score": row["score"] or 0,
        "importance_score": row["score"] or 0,
        "direction": normalize_direction(row["direction"], text=title),
        "raw_tag": str(row["tag"] or ""),
        "tag": _normalize_tag(row["tag"]),
        "logic": _sanitize_card_logic(row["logic"] or ""),
        "time": row["created_at"],
        "feed_url": feed_url,
        "source": source,
        "score_path": str(row["score_path"] or ""),
        "safe_neutral_reason": _row_safe_neutral_reason(row),
        "strategy_version": str(row["strategy_version"] or ""),
    }
    entry["_score_sources"] = {
        field: {"version": str(row[f"display_{field}_strategy_version"] or ""),
                "value": entry["importance_score" if field == "score" else field]}
        for field in ("score", "direction", "logic")
    }
    return entry


def _realtime_identity_ctes(has_event_graph: bool) -> str:
    """Batch canonical resolution with the same 64-node bound as event core."""
    if not has_event_graph:
        return """
            identified AS (
                SELECT *, '' AS canonical_id, 0 AS canonical_version,
                       'link:' || COALESCE(link, '') AS selection_identity
                FROM eligible
            )
        """
    return """
        event_walk(original_id, event_id, version, merge_state, merged_into, depth) AS (
            SELECT DISTINCT event_id, event_id, resolved_event_version,
                            event_merge_state, event_merged_into, 0
            FROM eligible WHERE COALESCE(event_id, '') != ''
            UNION ALL
            SELECT walk.original_id, parent.event_id, parent.version,
                   parent.merge_state, parent.merged_into, walk.depth + 1
            FROM event_walk AS walk JOIN events AS parent ON parent.event_id = walk.merged_into
            WHERE walk.merge_state = 'merged' AND walk.depth < 63
        ), roots AS (
            SELECT original_id, event_id AS canonical_id, version AS canonical_version
            FROM event_walk WHERE merge_state = 'active' AND merged_into IS NULL
        ), identified AS (
            SELECT eligible.*, COALESCE(roots.canonical_id, '') AS canonical_id,
                   COALESCE(roots.canonical_version, 0) AS canonical_version,
                   CASE WHEN roots.canonical_id IS NOT NULL THEN 'event:' || roots.canonical_id
                        ELSE 'link:' || COALESCE(eligible.link, '') END AS selection_identity
            FROM eligible LEFT JOIN roots ON roots.original_id = eligible.event_id
        )
    """


def _realtime_candidate_rows(conn, query, params, *, cfg, feed_urls, min_score, limit,
                             has_event_graph):
    """Bound returned candidates per source/window after eligibility and identity.

    The 48-hour SQL population is scanned/ranked, but at most
    ``2 * enabled feed URLs * limit`` rows leave SQLite. Repeated copies and
    excluded titles cannot consume a source's candidate slots. Event resolution
    is batched inside this query, without a query per candidate or displayed card.
    """
    from db.database import get_feed_thresholds

    thresholds = {
        url: float(min_score) if min_score is not None
        else get_feed_thresholds(cfg, url)["aggregation"]
        for url in feed_urls
    }

    def eligible(raw_title, title, logic, score, feed_url):
        try:
            score = float(score)
        except (TypeError, ValueError, OverflowError):
            return False
        threshold = thresholds.get(feed_url)
        if (threshold is None or not math.isfinite(score)
                or not 0 <= score <= 10 or score < threshold):
            return False
        raw_title = plain_text_from_markup(raw_title or "")
        title = _preferred_card_title(raw_title, title or "")
        return bool(title) and not _is_realtime_card_noise({
            "raw_title": raw_title, "title": title, "logic": logic or "",
        })

    conn.create_function("realtime_eligible", 5, eligible)
    return conn.execute(f"""
        WITH RECURSIVE candidates AS ({query}), eligible AS (
            SELECT *, created_at >= ? AS fresh_window
            FROM candidates
            WHERE realtime_eligible(raw_title, title, logic, score, feed_url)
        ), {_realtime_identity_ctes(has_event_graph)}, owners AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY selection_identity
                ORDER BY score DESC, created_at DESC, link
            ) AS semantic_rank, ROW_NUMBER() OVER (
                PARTITION BY selection_identity
                ORDER BY created_at DESC, score DESC, link
            ) AS freshness_rank FROM identified
        ), representatives AS (
            SELECT semantic.title, semantic.raw_title, fresh.link, semantic.score,
                   semantic.tag, semantic.logic, fresh.created_at, fresh.feed_url,
                   semantic.direction, semantic.event_id, semantic.event_version,
                   semantic.score_path, semantic.strategy_version, semantic.safe_neutral_reason,
                   semantic.display_score_strategy_version, semantic.display_direction_strategy_version,
                   semantic.display_logic_strategy_version, semantic.canonical_id,
                   semantic.canonical_version, semantic.selection_identity, fresh.fresh_window
            FROM owners AS semantic JOIN owners AS fresh
              ON fresh.selection_identity = semantic.selection_identity
             AND fresh.freshness_rank = 1
            WHERE semantic.semantic_rank = 1
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY feed_url, fresh_window
                ORDER BY (score >= {REALTIME_STRONG_SCORE}) DESC,
                         CASE WHEN score >= {REALTIME_STRONG_SCORE} THEN score ELSE 0 END DESC,
                         created_at DESC, link
            ) AS candidate_rank FROM representatives
        )
        SELECT * FROM ranked WHERE candidate_rank <= ?
        ORDER BY score DESC, created_at DESC, link
    """, (*params, _cutoff_ts(REALTIME_PRIMARY_HOURS), max(0, int(limit)))).fetchall()


def _load_from_db(
    limit: int = 10,
    min_score: Optional[float] = None,
    hours: int = REALTIME_FEED_HOURS,
    feed_url: Optional[str] = None,
    cfg: Optional[dict] = None,
    *,
    strict: bool = False,
) -> list[dict]:
    """从 SQLite 加载高分条目作为卡片缓存回退"""
    conn = None
    try:
        import sqlite3
        from db.database import DB_PATH, get_feed_thresholds
        cfg = load_config() if cfg is None else cfg
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cutoff = _cutoff_ts(hours)

        target_feed_cfg = _enabled_feed_config(cfg, feed_url) if feed_url else {}
        if feed_url and not target_feed_cfg:
            return []
        target_config_url = str(target_feed_cfg.get("url") or "")
        allowed_feed_urls = []
        for row in conn.execute(
            "SELECT DISTINCT feed_url FROM news_cache WHERE created_at >= ?",
            (cutoff,),
        ).fetchall():
            row_feed_url = str(row[0] or "")
            row_feed_cfg = _enabled_feed_config(cfg, row_feed_url)
            if not row_feed_cfg:
                continue
            if target_config_url and str(row_feed_cfg.get("url") or "") != target_config_url:
                continue
            allowed_feed_urls.append(row_feed_url)
        if not allowed_feed_urls:
            return []

        columns = _cache_table_columns(conn, "news_cache")
        event_columns = _cache_table_columns(conn, "events") if "event_id" in columns else set()
        (
            has_event_identity,
            event_projection,
            direction_projection,
            importance_projection,
            raw_title_projection,
            score_path_projection,
            strategy_projection,
            safe_neutral_projection,
            score_sources_projection,
        ) = _cache_column_projections(columns, event_columns)
        # Reuse the unified public intelligence-query strategy contract.  A
        # current production configuration must fail closed if an old rolling
        # schema cannot prove either strategy or quarantine state.
        from services.intelligence_query import _strategy_clause

        current_strategy = str(cfg.get("scoring_strategy_version") or "").strip()
        if current_strategy and not {"score_path", "strategy_version"}.issubset(columns):
            return []
        public_score_path_filter = (
            "AND LOWER(TRIM(COALESCE(score_path, ''))) IN ("
            + ",".join("?" for _ in _PUBLIC_REALTIME_SCORE_PATHS)
            + ")"
            if "score_path" in columns
            else ""
        )
        public_score_path_params = (
            list(_PUBLIC_REALTIME_SCORE_PATHS)
            if "score_path" in columns
            else []
        )
        strategy_params: list[object] = []
        strategy_filter = (
            _strategy_clause(cfg, strategy_params)
            if "strategy_version" in columns else ""
        )
        event_join = (
            "LEFT JOIN events AS event ON event.event_id = cache.event_id"
            if has_event_identity else ""
        )
        placeholders = ",".join("?" for _ in allowed_feed_urls)
        irrelevant_tags = sorted(IRRELEVANT_TAGS)
        irrelevant_placeholders = ",".join("?" for _ in irrelevant_tags)
        params = [
            cutoff,
            *allowed_feed_urls,
            *irrelevant_tags,
            *public_score_path_params,
            *strategy_params,
        ]
        has_event_graph = has_event_identity and {"merge_state", "merged_into"}.issubset(event_columns)
        event_graph_projection = (
            "event.version AS resolved_event_version, event.merge_state AS event_merge_state, "
            "event.merged_into AS event_merged_into" if has_event_graph
            else "0 AS resolved_event_version, '' AS event_merge_state, NULL AS event_merged_into"
        )
        query = f"""
            SELECT cache.title, {raw_title_projection}, cache.link,
                   {importance_projection} AS score,
                   cache.tag, cache.logic, cache.created_at, cache.feed_url,
                   {direction_projection},
                   {event_projection},
                   {score_path_projection}, {strategy_projection},
                   {safe_neutral_projection}, {score_sources_projection},
                   {event_graph_projection}
            FROM (
                SELECT * FROM news_cache
                WHERE created_at >= ? AND feed_url IN ({placeholders})
                  AND score IS NOT NULL
                  AND COALESCE(tag, '') NOT IN ({irrelevant_placeholders})
                  {public_score_path_filter}
                  {strategy_filter}
            ) AS cache
            {event_join}
        """
        rows = _realtime_candidate_rows(
            conn, query, params, cfg=cfg, feed_urls=allowed_feed_urls,
            min_score=min_score, limit=limit, has_event_graph=has_event_graph,
        )
        clean = []
        for r in rows:
            row_feed_url = r["feed_url"] or ""
            feed_cfg = _enabled_feed_config(cfg, row_feed_url)
            if not feed_cfg:
                continue
            score = r["score"]
            threshold = float(min_score) if min_score is not None else get_feed_thresholds(cfg, r["feed_url"] or "")["aggregation"]
            if score is None or float(score) < threshold:
                continue
            raw_title = plain_text_from_markup(r["raw_title"] or "")
            title_clean = _preferred_card_title(raw_title, r["title"] or "")
            if _is_realtime_card_noise({"raw_title": raw_title, "title": title_clean, "logic": r["logic"] or ""}):
                continue
            link = r["link"] or ""
            # jin10:// 协议统一替换为金十首页（浏览器不可识别的自定义协议）
            if link.startswith("jin10://"):
                link = "https://www.jin10.com/"
            if not title_clean.strip():
                continue
            event_id = str(r["canonical_id"] or "")
            event_version = max(0, int(r["canonical_version"] or 0))
            # SQL representatives already bind scoring to its semantic owner
            # and source/time/link to the freshest copy before source quotas.
            source = str(feed_cfg.get("name") or "").strip() or _source_name_from_url(row_feed_url)
            clean.append(_cache_card_entry(
                r, title=title_clean, raw_title=raw_title, link=link,
                event_id=event_id, event_version=event_version,
                feed_url=row_feed_url, source=source,
            ))
        return attach_score_contract_labels(clean, cfg)
    except Exception:
        if strict:
            raise
        return []
    finally:
        if conn is not None:
            conn.close()


def glance_sse_widget() -> Response:
    """Glance extension widget: DB 回退渲染的实时重点情报卡片。

    Glance 通过 extension cache 定时刷新该 HTML。

    查询参数:
      feed_url — 仅展示指定 RSS 源的高分条目（与 config feeds.url 一致）
    """
    if not request.args:
        # Import lazily: app.py imports this module alongside the Dashboard
        # blueprint.  The production homepage reads the worker's last-good
        # artifact; test/local roles retain the live SQLite renderer below.
        from routes.dashboard_routes import _dashboard_snapshot_response

        snapshot_response = _dashboard_snapshot_response(
            "realtime",
            title="Priority Intelligence",
            cache_seconds=30,
        )
        if snapshot_response is not None:
            return snapshot_response
    cfg = load_config()
    feed_url = (request.args.get("feed_url") or "").strip() or None
    explicit_min_score = request.args.get("min_score")
    try:
        min_score = _parse_realtime_min_score(explicit_min_score)
    except ValueError as exc:
        return Response(str(exc), status=400, mimetype="text/plain")

    # 按源过滤时仅用 DB；未配置或 disabled 的来源请求直接 fail closed。
    request_feed_cfg = _enabled_feed_config(cfg, feed_url) if feed_url else {}
    loader_kwargs = {
        "limit": 30,
        "min_score": min_score,
        "feed_url": feed_url,
        "cfg": cfg,
    }
    # The worker publisher must distinguish an honestly empty card from a
    # failed SQLite read, otherwise a transient error would overwrite the
    # last-good artifact with a misleading empty-state card.  Existing Web,
    # local, and test callers keep the historical fail-neutral behavior.
    try:
        from services.dashboard_snapshots import snapshots_enabled
        from services.shared_cache import process_role

        strict_snapshot_build = snapshots_enabled() and process_role() == "worker"
    except Exception:
        strict_snapshot_build = False
    if strict_snapshot_build:
        loader_kwargs["strict"] = True
    db_entries = (
        _load_from_db(**loader_kwargs)
        if not feed_url or request_feed_cfg
        else []
    )
    merged = db_entries

    from db.database import get_feed_thresholds

    candidates = []
    for entry in merged:
        if _is_realtime_card_noise(entry):
            continue
        if not _entry_matches_public_contract(entry, cfg):
            continue
        if not _enabled_feed_config(cfg, entry.get("feed_url", "")):
            # This also rejects stale Redis/card-cache events from test/ad-hoc feeds.
            continue
        threshold = (
            float(min_score)
            if min_score is not None
            else get_feed_thresholds(cfg, entry.get("feed_url", ""))["aggregation"]
        )
        if _entry_importance(entry) < threshold:
            continue
        title = _preferred_card_title(
            entry.get("raw_title", ""), entry.get("title", "")
        )
        if not title:
            continue
        candidates.append({
            **entry, "title": title, "_selection_threshold": threshold,
            "_reading_title": entry.get("_reading_title") or preferred_news_title(
                entry.get("raw_title", ""), entry.get("title", "")
            ),
        })
    selected, fresh_count, fallback_count = _select_balanced_cards(
        candidates,
        limit=REALTIME_CARD_LIMIT,
        max_per_source=None if feed_url else REALTIME_MAX_PER_SOURCE,
    )
    # Dedupe may keep semantic fields from the highest-scored copy but delivery
    # metadata from the freshest publisher.  Rebind the explanation to that
    # final feed so the displayed source and source threshold cannot diverge.
    selected = [
        {
            **entry,
            "_selection_threshold": (
                min_score
                if min_score is not None
                else get_feed_thresholds(cfg, entry.get("feed_url", ""))["aggregation"]
            ),
        }
        for entry in selected
    ]
    selected = restore_reading_titles(selected, candidates)
    selected = attach_score_contract_labels(selected, cfg)
    from services.news_mentions import attach_title_mentions

    selected = attach_title_mentions(selected)
    cards_html = _render_cards(selected, reading=True)
    html = _render_realtime_widget(cards_html, selected, fresh_count, fallback_count, min_score)
    if not strict_snapshot_build:
        from routes.dashboard_routes import _prepend_realtime_ingestion_coverage

        html, _ = _prepend_realtime_ingestion_coverage(html)
    return Response(html, mimetype="text/html")


def _render_realtime_widget(cards_html, selected, fresh_count, fallback_count, min_score):
    """News reading shell using the existing counts and empty-state condition."""
    from services.news_mentions import TITLE_MENTIONS_READING_STYLE

    source_count = len({_source_bucket(entry) for entry in selected})
    if fallback_count and fresh_count:
        freshness_label = f"近{REALTIME_PRIMARY_HOURS}h {fresh_count}条 · {REALTIME_FEED_HOURS}h回看 {fallback_count}条"
    elif fallback_count:
        freshness_label = f"近{REALTIME_PRIMARY_HOURS}h暂无过阈 · {REALTIME_FEED_HOURS}h回看 {fallback_count}条"
    else:
        freshness_label = f"近{REALTIME_PRIMARY_HOURS}h {fresh_count}条"
    if source_count:
        freshness_label += f" · {source_count}个订阅源"
    empty_condition = (
        f"≥{min_score:.1f}分"
        if min_score is not None
        else "达到当前来源门槛的"
    )
    empty_html = (
        '<div style="color:var(--color-text-subdue,inherit);padding:28px 12px;text-align:center;'
        'border:1px dashed rgba(148,163,184,.5);border-radius:16px;line-height:1.6;">'
        f'近{REALTIME_FEED_HOURS}小时暂无{escape_html(empty_condition)}重点情报<br>'
        '<span style="font-size:12px;opacity:.85;">新评分会实时出现在此；也可查看「今日情报 Briefing」</span>'
        '</div>'
    )

    return f"""<div class="home-news-reading" style="padding:2px 2px 8px;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin:0 0 11px;gap:10px;">
    <div>
      <div class="home-news-window" style="font-size:12px;color:var(--color-text-subdue,inherit);line-height:1.7;">{freshness_label}</div>
      <div style="font-size:11px;color:var(--color-text-subdue,inherit);line-height:1.7;">阅读优先级，不代表买入强度</div>
    </div>
    <div style="font-size:11px;color:var(--color-text-subdue,inherit);white-space:nowrap;">视图缓存30s</div>
  </div>
  <div id="sse-container">
{cards_html or empty_html}
  </div>
  <style>{_UNMAPPED_TITLE_READING_STYLE}{TITLE_MENTIONS_READING_STYLE}</style>
</div>"""
