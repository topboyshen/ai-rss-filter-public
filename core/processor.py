import logging
import math
import re
import time
import calendar
import threading
import uuid
from copy import copy, deepcopy
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, Any, Tuple, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import feedparser

from db.database import DB
from db.event_core import EventCoreConflictError
from utils.helpers import (
    get_cached, strategy_cache_key, find_similar_article,
    extract_score_from_title, extract_tag_from_title,
    rfc822_date, escape_cdata, escape_html, escape_xml, safe_http_url,
    fetch_feed, bump_stat, clean_html,
    clean_title_for_compare, max_entries_for_feed_url,
    normalize_direction, direction_emoji, plain_text_from_markup,
)
from core.scorer import Scorer, capture_llm_calls, is_usable_score_result
from services.score_evidence_capture import (
    capture_article_score,
    persist_scoring_evidence,
)
from services.news_processing_trace import (
    mark_news_processing_reuse,
    trace_news_processing,
)
from scoring_explanation import EXPLANATION_FIELDS, build_score_explanation
from scoring_provenance import (
    ALLOWED_SEMANTIC_RESOLUTIONS,
    NON_SUBSTANTIVE_SCORE_PATHS,
    RECOGNIZED_SCORE_PATHS,
    SAFE_NEUTRAL_CONTRACT_VALUES,
    SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION,
    is_fixed_safe_neutral_fields,
    normalize_score_path,
)
from core.metrics import get_metrics
from core.prompts import prompt_name_for_body, resolve_prompt
from core.asset_mapping import get_mapping_version, map_article_to_assets
from core.notifier import send_telegram_notification
from core.user_feedback import resolve_scoring_contract
from services import natural_scoring

logger = logging.getLogger(__name__)
# Time windows in config (time_weight / quiet_mode) are Beijing wall-clock.
_SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")

# A semantic rejection followed by a backend that gave no verdict (timeout,
# API error, rate-limit wait) is not a final safe-neutral.  The same link may be
# re-scored at most this many extra times per strategy version and process;
# after that the safe-neutral is persisted as final. Uncommitted results keep
# their slot instead of receiving a fresh model budget after a failed write.
# Ordinary ingestion retains its process-local budget. Natural deferrals also
# have a durable budget shared by later arrivals, so restarts cannot reset it.
TRANSPORT_RETRY_MAX = 2
_TRANSPORT_RETRY_TRACKED_MAX = 4096
_transport_retry_lock = threading.Lock()
_transport_retry_counts: "OrderedDict[tuple[str, str], int]" = OrderedDict()
_transport_pending_writes: dict = {}
# process_entry reason for such a deferral: nothing was persisted.  Refresh
# sweeps save it for a later natural refresh; webhook spools retain their entry.
SCORE_DEFERRED_REASON = "score_deferred"
SCORE_TERMINAL_UNSCORED_REASON = "score_terminal_unscored"


class ScoringTransportRetryable(Exception):
    """Nothing was persisted; the caller should retry this entry later."""

    def __init__(self, link: str, attempt: int):
        message = (
            "score deferred: transport retry capacity is full; waiting for a commit slot"
            if attempt == 0 else
            f"score deferred: backup backend gave no verdict (retry {attempt}/{TRANSPORT_RETRY_MAX})"
        )
        super().__init__(message)
        self.link = link
        self.attempt = attempt


class _StageOutcome(tuple):
    """The historical 4-tuple, plus whether the stage lacked a backup verdict."""

    transport_unverdicted = False
_INTERNAL_WEB_HOSTS = {"ai-filter-web", "127.0.0.1", "localhost", "172.17.0.1"}


def _is_internal_jin10_feed(target_url: str) -> bool:
    try:
        parsed = urlsplit(str(target_url or ""))
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (parsed.hostname or "").lower() in _INTERNAL_WEB_HOSTS
        and parsed.path.rstrip("/") == "/jin10/rss"
    )


def _load_feed_bytes(target_url: str, cfg: dict, timeout: int, attempts: int) -> bytes:
    """Load a feed without calling back into the same Gunicorn worker pool."""
    from services.publisher_feeds import load_publisher_feed

    native = load_publisher_feed(target_url, cfg, timeout, attempts)
    if native is not None:
        return native
    if _is_internal_jin10_feed(target_url):
        # Import lazily: services.jin10_feed uses build_rss_xml from this module.
        from services.jin10_feed import build_jin10_rss

        configured_limit = max_entries_for_feed_url(target_url, cfg)
        limit = configured_limit or int(cfg.get("max_entries", 15) or 15)
        xml = build_jin10_rss(target_url, cfg, limit=max(1, min(50, limit)))
        return xml.encode("utf-8")
    return fetch_feed(target_url, timeout, max_attempts=attempts, cfg=cfg)


def _get_time_bonus(cfg: dict) -> Tuple[float, str]:
    """返回当前时间窗口的加分值和标签。"""
    tw = cfg.get("time_weight", {})
    if not tw.get("enabled", False):
        return 0.0, ""
    now = datetime.now(_SCHEDULE_TZ)
    hour = now.hour + now.minute / 60.0
    for rule in tw.get("rules", []):
        sh = rule.get("start_hour", 0)
        eh = rule.get("end_hour", 24)
        if sh <= hour < eh:
            return float(rule.get("bonus", 0)), rule.get("label", "")
    return 0.0, ""


def _is_quiet_mode(cfg: dict) -> Tuple[bool, float]:
    """检查当前是否在静默模式。返回 (is_quiet, threshold_raise)。"""
    qm = cfg.get("quiet_mode", {})
    if not qm.get("enabled", False):
        return False, 0.0
    now = datetime.now(_SCHEDULE_TZ)
    current = now.hour * 60 + now.minute
    default_raise = float(qm.get("notify_threshold_raise", 0))
    for slot in qm.get("schedule", []):
        sh, sm = map(int, slot["start"].split(":"))
        eh, em = map(int, slot["end"].split(":"))
        start_m = sh * 60 + sm
        end_m = eh * 60 + em
        threshold_raise = float(slot.get("notify_threshold_raise", default_raise))
        # 跨天处理（如 15:00 到 09:15）
        if end_m < start_m:
            if current >= start_m or current < end_m:
                return True, threshold_raise
        else:
            if start_m <= current < end_m:
                return True, threshold_raise
    return False, 0.0


def _get_feed_config(cfg: dict, feed_url: str) -> dict:
    return DB.get_feed_config(cfg, feed_url) if feed_url else {}


def _source_tier(cfg: dict, feed_url: str) -> str:
    return _get_feed_config(cfg, feed_url).get("tier", "")


def _contains_any(text: str, words: list) -> bool:
    return any(w and w in text for w in words)


def _watchlist_code_matches(title: str, configured_code) -> bool:
    """Match an exact six-digit holding code, not part of another identifier."""
    value = str(configured_code or "").strip()
    code = re.fullmatch(
        r"(?:(?:SH|SZ|BJ)[.:]?)?([0-9]{6})(?:\.(?:SH|SZ|BJ))?",
        value,
        flags=re.IGNORECASE,
    )
    if code is None:
        return False
    return re.search(
        r"(?<![A-Za-z\d])(?:(?:SH|SZ|BJ)[.:]?)?"
        + re.escape(code.group(1))
        + r"(?:\.(?:SH|SZ|BJ))?(?![A-Za-z\d])",
        title,
        flags=re.IGNORECASE,
    ) is not None


def _match_fast_pass(title: str, cfg: dict, feed_url: str = "") -> Optional[Tuple[float, str, str, str, bool]]:
    """FastPass 默认只产生关注提示；持仓命中永远不决定分数或方向。
    返回 (score, tag, logic, sentiment, skip_llm) 或 None。
    只有非持仓规则自身显式 skip_llm: true 才可绕过模型。
    支持按 feed 粒度关闭：feed 配置中 fast_pass: false 即可。
    """
    fp = cfg.get("fast_pass", {})
    if not fp.get("enabled", False):
        return None
    
    # 按 feed 粒度覆写：可针对特定源关闭 FastPass
    if feed_url:
        fdef = _get_feed_config(cfg, feed_url)
        if fdef.get("fast_pass") is False:
            return None

    for tier in fp.get("tiers", []):
        if _contains_any(title, tier.get("exclude_keywords", [])):
            continue
        require_any = tier.get("require_any", [])
        if require_any and not _contains_any(title, require_any):
            continue
        is_watchlist = str(tier.get("name") or "").strip().casefold() == "watchlist"
        for kw in tier.get("keywords", []):
            matched = _watchlist_code_matches(title, kw) if is_watchlist else bool(kw and kw in title)
            if matched:
                return (
                    float(tier.get("score", fp.get("score", 8.5))),
                    tier.get("tag", fp.get("tag", "FastPass")),
                    tier.get("logic", fp.get("logic", "早盘核心关键词命中")),
                    "⚖️" if is_watchlist else "📈",
                    False if is_watchlist else tier.get("skip_llm") is True,
                )
    
    keywords = fp.get("keywords", [])
    watchlist = fp.get("watchlist", [])
    for kw in keywords:
        if kw and kw in title:
            return (
                float(fp.get("score", 8.5)),
                fp.get("tag", "FastPass"),
                fp.get("logic", "早盘核心关键词命中"),
                "📈",
                fp.get("skip_llm") is True,
            )
    for code in watchlist:
        if _watchlist_code_matches(title, code):
            return (
                float(fp.get("watchlist_score", fp.get("score", 8.5))),
                fp.get("watchlist_tag", fp.get("tag", "FastPass")),
                f"持仓股{code}命中，仅作关注提示",
                "⚖️",
                False,
            )
    return None


def _should_rescore_similar(title: str, cfg: dict, feed_url: str = "") -> bool:
    tier = _source_tier(cfg, feed_url)
    if tier in {"official", "primary_news"}:
        return _contains_any(title, cfg.get("similarity_rescore_keywords", []))
    return False


def _match_local_reject(title: str, cfg: dict) -> Optional[Tuple[float, str, str, str]]:
    reject_cfg = cfg.get("local_reject_rules", {})
    if not reject_cfg.get("enabled", False):
        return None
    import re
    global_protect = reject_cfg.get("protect_keywords", [])
    for rule in reject_cfg.get("rules", []):
        pattern = rule.get("pattern", "")
        if not pattern:
            continue
        protect_keywords = list(global_protect) + list(rule.get("protect_keywords", []))
        if _contains_any(title, protect_keywords):
            continue
        try:
            if re.search(pattern, title):
                return (
                    float(rule.get("score", 2.0)),
                    rule.get("tag", "本地拒绝"),
                    rule.get("logic", "本地低价值规则命中"),
                    "⚖️",
                )
        except re.error:
            logger.warning("local_reject_rules 无效 pattern %r", pattern)
    return None


def _model_name(scorer: Optional[Scorer]) -> str:
    try:
        if scorer and getattr(scorer, "backends", None):
            return getattr(scorer, "last_backend_name", "") or scorer.backends[0].model or scorer.backends[0].name
    except Exception:
        pass
    return ""


def _score_stage_with_identity(
    scorer: Scorer,
    stage: str,
    *args: Any,
    **kwargs: Any,
) -> Tuple[Tuple[float, str, str, str], str, str, str]:
    """Return score fields and provenance from the same scoring invocation."""
    bound_name = f"score_{stage}_with_identity"
    # Inspect the type so MagicMock and dynamic proxy attributes do not invent
    # a bound-result API they do not actually implement.
    if callable(getattr(type(scorer), bound_name, None)):
        outcome = getattr(scorer, bound_name)(*args, **kwargs)
        values = getattr(outcome, "values", None)
        model_name = str(getattr(outcome, "model_name", "") or "").strip()
        semantic_resolution = str(
            getattr(outcome, "semantic_resolution", "") or ""
        ).strip()
        neutral_reason = str(getattr(outcome, "neutral_reason", "") or "").strip()
        if not isinstance(values, tuple) or len(values) != 4 or not model_name:
            raise TypeError(f"{bound_name} returned an invalid bound score")
        if semantic_resolution not in ALLOWED_SEMANTIC_RESOLUTIONS:
            raise TypeError(f"{bound_name} returned an invalid semantic resolution")
        if semantic_resolution and values != SAFE_NEUTRAL_CONTRACT_VALUES:
            raise TypeError(f"{bound_name} returned an invalid safe-neutral tuple")
        if neutral_reason and not semantic_resolution:
            raise TypeError(f"{bound_name} returned a reason without a resolution")
        stage_outcome = _StageOutcome(
            (values, model_name, semantic_resolution, neutral_reason)
        )
        stage_outcome.transport_unverdicted = bool(
            getattr(outcome, "transport_unverdicted", False)
        ) and bool(semantic_resolution)
        return stage_outcome

    public_method = getattr(scorer, f"score_{stage}")
    values = public_method(*args, **kwargs)
    if not isinstance(values, tuple) or len(values) != 4:
        raise TypeError(f"score_{stage} returned an invalid score tuple")
    return values, _model_name(scorer), "", ""


def _is_real_model_identity(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized) and normalized.casefold() not in {
        "fallback_local",
        "local_safe_neutral",
        "local",
        "none",
        "unknown",
    }


def _is_usable_quality_repair_result(
    *,
    score: Any,
    tag: str,
    logic: str,
    score_path: str,
    error_type: str,
    model_name: str,
    stage1_model: str = "",
    stage2_model: str = "",
    is_similarity_copy: bool = False,
) -> bool:
    """Fail closed before a replay can replace a quarantine marker."""
    if not is_usable_score_result(score, tag, logic) or str(error_type or "").strip():
        return False
    path = str(score_path or "").strip()
    selected_model = str(model_name or "").strip()
    if is_similarity_copy:
        return path == "similarity" and not selected_model
    if path == "llm":
        return (
            _is_real_model_identity(selected_model)
            and str(stage1_model or "").strip() == selected_model
        )
    if path == "stage2":
        return (
            _is_real_model_identity(selected_model)
            and str(stage2_model or "").strip() == selected_model
        )
    return False


def _set_scoring_meta(entry, **meta) -> None:
    explanation = meta.pop("score_explanation", {}) or {}
    try:
        entry.scoring_meta = {**meta, **{key: explanation.get(key) for key in EXPLANATION_FIELDS}}
    except Exception:
        pass


def _stable_event_id(value: Any) -> str:
    """Accept only opaque UUID identities from the persistent event core."""
    if not isinstance(value, (str, uuid.UUID)):
        return ""
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError):
        return ""


def _event_identity_from_value(value: Any) -> Tuple[str, int]:
    if isinstance(value, dict):
        raw_id = value.get("event_id")
        raw_version = value.get("event_version") or value.get("version")
    elif isinstance(value, (tuple, list)):
        raw_id = value[0] if value else None
        raw_version = value[1] if len(value) > 1 else None
    else:
        raw_id = value
        raw_version = None
    event_id = _stable_event_id(raw_id)
    try:
        event_version = int(raw_version or 0)
    except (TypeError, ValueError):
        event_version = 0
    return event_id, event_version if event_version > 0 else 0


def _entry_event_identity(entry: Any, frozen_signal: Optional[dict] = None) -> Tuple[str, int]:
    """Read a stable identity from a frozen signal or an already-written entry."""
    if frozen_signal:
        event_id, event_version = _event_identity_from_value(frozen_signal)
        if event_id:
            return event_id, event_version
    meta = getattr(entry, "scoring_meta", {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    raw_id = getattr(entry, "event_id", None)
    raw_version = getattr(entry, "event_version", None)
    if hasattr(entry, "get"):
        raw_id = raw_id or entry.get("event_id", None)
        raw_version = raw_version or entry.get("event_version", None)
    return _event_identity_from_value({
        "event_id": raw_id or meta.get("event_id"),
        "event_version": raw_version or meta.get("event_version"),
    })


def _assign_event_identity(entry: Any, event_id: str, event_version: int) -> None:
    """Attach persisted identity without assuming a mutable feed-entry class."""
    if not event_id:
        return
    try:
        entry.event_id = event_id
        entry.event_version = int(event_version or 0)
    except Exception:
        try:
            entry["event_id"] = event_id
            entry["event_version"] = int(event_version or 0)
        except Exception:
            pass
    meta = dict(getattr(entry, "scoring_meta", {}) or {})
    meta.update({"event_id": event_id, "event_version": int(event_version or 0)})
    try:
        entry.scoring_meta = meta
    except Exception:
        pass


def _persisted_event_identity(entry: Any, write_result: Any = None) -> Tuple[str, int]:
    """Capture the identity returned by DB writes, with a legacy-safe lookup."""
    event_id, event_version = _event_identity_from_value(write_result)
    if not event_id:
        event_id, event_version = _entry_event_identity(entry)
    link = str(getattr(entry, "link", "") or "").strip()
    if not event_id and link:
        lookup = getattr(DB, "get_event_identity_for_link", None)
        try:
            if callable(lookup):
                event_id, event_version = _event_identity_from_value(lookup(link))
            else:
                event_id, event_version = _event_identity_from_value(
                    DB.get_cached_entry_meta(link)
                )
        except Exception:
            logger.warning(
                "Failed to read stable event identity for %s", link, exc_info=True
            )
    _assign_event_identity(entry, event_id, event_version)
    return event_id, event_version


def _similar_article_direction(similar) -> str:
    """Prefer the DB direction; infer from title only for legacy six-field rows."""
    if len(similar) > 6:
        return normalize_direction(similar[6])
    return normalize_direction(text=similar[1])


def _pass_threshold_for_feed(cfg: dict, feed_url: str = "") -> float:
    return DB.get_feed_thresholds(cfg, feed_url)["pass"]


def _parsed_entry_timestamp(entry) -> Optional[int]:
    for attr in ("published_parsed", "updated_parsed"):
        value = getattr(entry, attr, None)
        if value:
            try:
                # feedparser normalizes RFC 822/Atom dates to UTC struct_time.
                # mktime() would reinterpret it in the container's Asia/Shanghai
                # timezone and move every RSS entry eight hours into the past.
                return int(calendar.timegm(value))
            except (OverflowError, TypeError, ValueError):
                continue
    return None


def _entry_timestamp(entry) -> int:
    parsed = _parsed_entry_timestamp(entry)
    if parsed is not None:
        return parsed
    return int(time.time())


def _repair_cached_feed_origins(entries: list, feed_url: str, cfg: dict) -> Dict[str, tuple]:
    """Repair metadata for cached RSS rows, including items beyond scoring cap."""
    links = [
        str(getattr(entry, "link", "") or entry.get("link", ""))
        for entry in entries
        if str(getattr(entry, "link", "") or entry.get("link", ""))
    ]
    cached = DB.get_cached_entries_by_links(links)
    canonical_url = DB.canonical_feed_url(cfg, feed_url)
    repairs = []
    source_only_sightings = []
    for entry in entries:
        link = str(getattr(entry, "link", "") or entry.get("link", ""))
        if link not in cached:
            continue
        published_at = _parsed_entry_timestamp(entry)
        if published_at is not None:
            repairs.append((link, published_at, canonical_url))
        else:
            # Some webhook/RSS entries omit publication timestamps. They are
            # still valid source sightings and must not disappear merely
            # because no news_cache origin timestamp can be repaired.
            source_only_sightings.append((link, canonical_url))
    if repairs:
        DB.update_cached_entry_origins(repairs)
    if source_only_sightings:
        DB.record_article_sources(source_only_sightings)
    return cached


def _aggregation_threshold_for_feed(cfg: dict, feed_url: str = "") -> float:
    return DB.get_feed_thresholds(cfg, feed_url)["aggregation"]


def _map_publisher_title_assets(article: dict) -> dict:
    """Keep generated scoring metadata out of forward mapping recognition."""
    return map_article_to_assets({**article, "tag": "", "logic": "", "summary": ""})


def _build_backtest_signal_snapshot(
    *,
    entry,
    raw_title: str,
    score: float,
    tag: str,
    logic: str,
    summary: str,
    direction: str,
    score_path: str,
    score_confidence: float,
    strategy_version: str,
    feed_url: str,
    scored_at: int,
    cfg: dict,
    is_similarity_copy: bool = False,
    revision_key: str = "base",
    signal_stage: str = "base",
    auto_revision: bool = False,
    require_insert: bool = False,
    frozen_signal: Optional[dict] = None,
    independence_key: str = "",
) -> dict:
    """Build a point-in-time signal snapshot without I/O or model calls."""
    article = {
        "title": raw_title,
        "raw_title": raw_title,
        "tag": tag,
        "logic": logic,
        "summary": summary,
        "direction": direction,
    }
    frozen_signal = frozen_signal or {}
    event_id, event_version = _entry_event_identity(entry, frozen_signal)
    if frozen_signal:
        mapping = {
            "mapped_theme": str(frozen_signal.get("mapped_theme") or ""),
            "backtest_symbol": frozen_signal.get("backtest_symbol"),
            "mapping_confidence": frozen_signal.get("mapping_confidence", 0.0),
            "mapping_relevance": frozen_signal.get("mapping_relevance", 0.0),
            "expected_gap": frozen_signal.get("expected_gap"),
            "security_candidates": frozen_signal.get("security_candidates") or [],
            "market_references": frozen_signal.get("market_references") or [],
        }
        event_key = str(frozen_signal.get("event_key") or "")
        mapping_version = str(
            frozen_signal.get("mapping_version") or get_mapping_version()
        )
    else:
        try:
            mapping = _map_publisher_title_assets(article)
        except Exception:
            logger.exception("[Backtest] asset mapping failed for %s", raw_title[:80])
            mapping = {
                "mapped_theme": str(tag or "").strip(),
                "backtest_symbol": None,
                "mapping_confidence": 0.0,
                "mapping_relevance": 0.0,
                "expected_gap": None,
                "security_candidates": [],
                "market_references": [],
            }
        try:
            from core.digest_engine import normalize_event_key

            event_key = normalize_event_key(raw_title)
        except Exception:
            event_key = ""
        mapping_version = get_mapping_version()
    aggregation_threshold = _aggregation_threshold_for_feed(cfg, feed_url)
    non_substantive = score_path in NON_SUBSTANTIVE_SCORE_PATHS
    published_at = _parsed_entry_timestamp(entry)
    available_at = int(time.time())
    snapshot = {
        "event_id": event_id or None,
        "event_version": int(event_version or 0),
        "event_key": event_key or str(getattr(entry, "link", "") or ""),
        "link": str(getattr(entry, "link", "") or "").strip(),
        "feed_url": feed_url,
        "raw_title": raw_title,
        "event_type": tag or "news",
        "logic": logic,
        "summary": summary,
        "source_tier": _source_tier(cfg, feed_url),
        # The score cannot be acted on before scoring completes and the local
        # write begins.  RSS publication time is retained separately and never
        # substitutes for actual signal availability.
        "available_at": max(int(scored_at), available_at),
        "scored_at": int(scored_at),
        "published_at": int(published_at) if published_at is not None else None,
        "score": float(score),
        "direction": normalize_direction(direction),
        # A fixed safe-neutral resolution is a fail-closed outcome, not model
        # confidence. Keep the configured threshold for audit, but never mark
        # this resolution actionable even when a feed threshold is <= 4.
        "score_confidence": (
            0.0 if non_substantive else float(score_confidence or 0.0)
        ),
        "strategy_version": str(strategy_version or ""),
        "mapping_version": mapping_version,
        "mapped_theme": str(mapping.get("mapped_theme") or ""),
        "backtest_symbol": mapping.get("backtest_symbol"),
        # These fields describe the event-to-asset relationship only.  They
        # are deliberately independent of score_confidence (distance from the
        # article selection threshold).  First-layer scoring does not invent a
        # signed expected gap, so it remains NULL unless a dedicated mapping
        # stage has supplied one.
        "mapping_confidence": float(mapping.get("mapping_confidence") or 0.0),
        "mapping_relevance": float(mapping.get("mapping_relevance") or 0.0),
        "expected_gap": (
            None
            if mapping.get("expected_gap") in (None, "")
            else float(mapping.get("expected_gap"))
        ),
        "security_candidates": list(mapping.get("security_candidates") or []),
        "market_references": list(mapping.get("market_references") or []),
        "score_path": score_path,
        "revision_key": str(revision_key or "base"),
        "signal_stage": str(signal_stage or score_path or "base"),
        "independence_key": str(
            event_id
            or independence_key
            or frozen_signal.get("independence_key")
            or event_key
            or getattr(entry, "link", "")
            or ""
        ),
        "aggregation_threshold": float(aggregation_threshold),
        "selected": (
            False
            if non_substantive
            else float(score) >= float(aggregation_threshold)
        ),
        "is_similarity_copy": bool(is_similarity_copy),
    }
    if auto_revision:
        snapshot["auto_revision"] = True
    if require_insert:
        snapshot["require_insert"] = True
    return snapshot


def _irrelevance_floor_for_feed(cfg: dict, feed_url: str = "") -> float:
    fdef = _get_feed_config(cfg, feed_url)
    policy = cfg.get("irrelevance_policy", {}) or {}
    return float(fdef.get("irrelevance_floor", policy.get("default_floor", 4.0)))


def _cap_irrelevant_high_score(score: float, tag: str, logic: str, cfg: Optional[dict] = None, feed_url: str = "") -> Tuple[float, str, str]:
    """防止 LLM 将明确无关内容打成异常高分；宏观源只降到底线，不一刀切打死。"""
    cfg = cfg or {}
    tag_text = (tag or "").strip()
    logic_text = (logic or "").strip()
    normalized_tag = re.sub(r"\s+", "", tag_text)
    normalized_logic = re.sub(r"\s+", "", logic_text)

    explicit_irrelevant_tags = {
        "无关",
        "不相关",
        "无相关",
        "无相关标签",
        "无A股相关",
        "非A股相关",
        "无市场相关性",
    }
    explicit_irrelevant_logic = (
        "无相关" in normalized_logic
        or "无相关性" in normalized_logic
        or "与A股无关" in normalized_logic
        or "与市场无关" in normalized_logic
        or "不具备A股相关性" in normalized_logic
    )
    irrelevant = normalized_tag in explicit_irrelevant_tags or explicit_irrelevant_logic
    floor = _irrelevance_floor_for_feed(cfg, feed_url)
    trigger_score = max(6.0, floor)
    if irrelevant and score > trigger_score:
        return floor, tag_text or "无相关", logic_text or f"明确无相关内容，按源策略降至{floor:.1f}"
    return score, tag, logic


def _apply_score_adjustments(score, tag, logic, cfg, feed_url, raw_title, non_substantive):
    """Apply the existing cap and time weight without changing their order."""
    if non_substantive:
        return score, tag, logic
    score, tag, logic = _cap_irrelevant_high_score(score, tag, logic, cfg, feed_url)
    # 时间窗口动态权重
    time_bonus, time_label = _get_time_bonus(cfg)
    if score >= 0 and time_bonus != 0:
        score = min(10.0, max(0.0, score + time_bonus))
        logger.info("[TimeWeight] '%s' %s bonus %+.1f -> %.1f", raw_title[:40], time_label, time_bonus, score)
    return score, tag, logic


def _effective_notify_threshold(cfg, feed_url, notify_threshold, raw_title):
    """Keep quiet-mode notification changes separate from the pass threshold."""
    is_quiet, threshold_raise = _is_quiet_mode(cfg)
    threshold = notify_threshold if notify_threshold is not None else DB.get_feed_thresholds(cfg, feed_url)["notify"]
    if is_quiet:
        threshold += threshold_raise
        logger.info("[QuietMode] '%s' threshold raised by +%.1f -> %.1f", raw_title[:40], threshold_raise, threshold)
    return threshold


def _refresh_cached_entry_origin(
    entry,
    cfg: dict,
    feed_url: str,
    *,
    score,
    score_path: str,
    strategy_version: str,
) -> None:
    """Repair cache provenance, requiring actionable selection to commit."""
    # Scoring can be reused, but publication metadata must follow the
    # current RSS item. This repairs legacy rows written with the UTC
    # struct_time interpreted as Asia/Shanghai and Radar rows whose
    # source label was previously stored in feed_url.
    published_at = _parsed_entry_timestamp(entry)
    if published_at is not None or feed_url:
        selection_upgrade = None
        try:
            aggregation_threshold = _aggregation_threshold_for_feed(cfg, feed_url)
            if (
                score_path != "similarity"
                and score_path not in NON_SUBSTANTIVE_SCORE_PATHS
                and strategy_version
                and score is not None
                and float(score) >= aggregation_threshold
            ):
                selection_upgrade = {
                    "strategy_version": strategy_version,
                    "aggregation_threshold": aggregation_threshold,
                }
            DB.update_cached_entry_origin(
                entry.link,
                created_at=published_at,
                feed_url=feed_url,
                backtest_selection=selection_upgrade,
            )
        except Exception:
            logger.warning(
                "Failed to refresh cached entry origin for %s",
                entry.link,
                exc_info=True,
            )
            # A cache score that first becomes actionable under this
            # feed's aggregation policy must not be emitted unless the
            # same transaction recorded that point-in-time transition.
            # Metadata-only repair remains best effort, but a failed
            # selection upgrade is fail-closed for ledger integrity.
            if selection_upgrade is not None:
                raise


@dataclass
class _EntryContext:
    """Per-entry inputs; only an authorized quarantine repair replaces source text."""

    entry: Any
    cfg: dict
    feed_url: str
    raw_title: str
    source_summary: str
    pass_threshold: float
    strategy_version: str
    prompt_name: str
    quality_repair: bool = False
    quality_repair_summary: str = ""
    similarity_identity: Any = None


class _SimilarityIdentitySnapshot:
    """Lazy, per-feed master snapshot; never query the full master per card."""

    def __init__(self):
        self.loaded = False
        self.index = None

    def compare(self, new_title, old_title):
        from core.score_evidence import prepare_reuse_identity_index, reuse_identity_comparison

        same_title = reuse_identity_comparison(new_title, old_title, None)
        if same_title is not None:
            return same_title
        if not self.loaded:
            self.loaded = True
            self.index = prepare_reuse_identity_index(DB.get_similarity_identity_rows())
        return reuse_identity_comparison(new_title, old_title, self.index)


@dataclass
class _EntryScore:
    """Incremental scoring evidence, including completed stages on later failure."""

    score: float = -1.0
    tag: str = ""
    logic: str = ""
    sentiment: str = ""
    score_path: str = "llm"
    error_type: str = ""
    score_latency_ms: int = 0
    stage1_score: Optional[float] = None
    stage1_model: str = ""
    stage1_resolution: str = ""
    stage2_score: Optional[float] = None
    stage2_model: str = ""
    stage2_resolution: str = ""
    selected_model: str = ""
    score_confidence: float = 0.0
    review_reason: str = ""
    neutral_reason: str = ""
    used_fast_pass: bool = False
    used_local_fast: bool = False
    fast_pass_hint: Optional[dict] = None
    raw_score: float = -1.0
    transport_unverdicted: bool = False


@dataclass
class _PendingTransportWrite:
    """A completed, terminal decision waiting only for its database commit."""

    ctx: _EntryContext
    result: _EntryScore
    meta: dict
    scored_at: int
    snapshot: dict


def _validate_entry_feedback_context(cfg, few_shot_context):
    if (cfg.get("few_shot_feedback", {}) or {}).get("enabled", False):
        if not cfg.get("_scoring_contract_resolved"):
            raise RuntimeError("few-shot scoring contract was not resolved")
        if not cfg.get("_scoring_contract_registered"):
            raise RuntimeError("few-shot scoring contract was not registered")
        contract_context = str(cfg.get("_few_shot_context") or "")
        if few_shot_context != contract_context:
            raise RuntimeError("few-shot context does not match registered contract")
        return contract_context
    return few_shot_context


def _normalize_entry_source(entry, cfg):
    """Reject poisoned input before normalizing the shared ingestion boundary."""
    raw_title = plain_text_from_markup(entry.title)
    if not raw_title:
        return raw_title, "", "empty_title"
    if raw_title.startswith(("[⭐", "[★")):
        return raw_title, "", "already_scored_title"
    source_summary = entry.get("summary", "")
    normalized_source_summary = plain_text_from_markup(source_summary)
    if "\ufffd" in raw_title or "\ufffd" in normalized_source_summary:
        logger.error(
            "[InputQuality] Refusing irreversibly decoded source text for %s",
            str(getattr(entry, "link", "") or "")[:160],
        )
        return raw_title, source_summary, "invalid_source_text"
    # Normalize before blacklist, similarity, scoring, persistence, and event
    # creation so every ingestion path shares the same clean write boundary.
    entry.title = raw_title
    entry._raw_title = raw_title
    blacklist_re = cfg.get("_blacklist_re")
    if blacklist_re is None:
        try:
            from core.config import config as _config_mod

            blacklist_re = _config_mod.blacklist_re
        except Exception:
            blacklist_re = None
    reason = "blacklist" if blacklist_re and blacklist_re.search(raw_title) else ""
    return raw_title, source_summary, reason


def _cached_entry_scoring_meta(entry, cached, strategy_version):
    """Decode both the current tuple and legacy metadata lookup without writes."""
    meta = {
        "strategy_version": "",
        "score_path": "",
        "score_confidence": 0.0,
        "direction": "",
        "event_id": "",
        "event_version": 0,
    }
    if len(cached) > 7:
        meta.update(
            {
                "strategy_version": str(cached[7] or ""),
                "score_path": str(cached[8] or "") if len(cached) > 8 else "",
                "score_confidence": float(cached[9] or 0.0) if len(cached) > 9 else 0.0,
                "direction": str(cached[10] or "") if len(cached) > 10 else "",
            }
        )
        event_id, event_version = _event_identity_from_value(
            {
                "event_id": cached[11] if len(cached) > 11 else None,
                "event_version": cached[12] if len(cached) > 12 else None,
            }
        )
        meta.update(event_id=event_id, event_version=event_version)
    elif strategy_version:
        cached_meta = DB.get_cached_entry_meta(entry.link)
        meta.update(
            {
                "strategy_version": str(cached_meta.get("strategy_version", "")),
                "score_path": str(cached_meta.get("score_path") or ""),
                "score_confidence": float(cached_meta.get("score_confidence") or 0.0),
                "direction": str(cached_meta.get("direction") or ""),
            }
        )
        event_id, event_version = _event_identity_from_value(cached_meta)
        meta.update(event_id=event_id, event_version=event_version)
    meta["score_path"] = normalize_score_path(meta["score_path"])
    return meta


def _prepare_entry_quality_repair(ctx, raw_title_cached, summary, meta):
    """Bind explicit replay to the clean source and already frozen event identity."""
    repair_raw_title = plain_text_from_markup(raw_title_cached or ctx.raw_title)
    ctx.quality_repair_summary = str(summary or "")
    if (
        not repair_raw_title
        or "\ufffd" in repair_raw_title
        or "\ufffd" in ctx.quality_repair_summary
    ):
        logger.error(
            "[QualityRepair] Refusing poisoned quarantine source for %s",
            ctx.entry.link,
        )
        return None, "quality_quarantine"
    event_id, event_version = meta["event_id"], meta["event_version"]
    if event_id:
        _assign_event_identity(ctx.entry, event_id, event_version)
    if not event_id or event_version < 1:
        logger.error(
            "[QualityRepair] Missing frozen event identity for %s",
            ctx.entry.link,
        )
        return None, "quality_quarantine"
    ctx.raw_title = repair_raw_title
    ctx.entry.title = repair_raw_title
    ctx.entry._raw_title = repair_raw_title
    ctx.quality_repair = True
    return None


def _reuse_cached_entry(ctx, cached, meta):
    title, raw_title_cached, score, _tag, _logic, _created_at, summary = cached[:7]
    path = meta["score_path"]
    if path not in RECOGNIZED_SCORE_PATHS:
        return None, "filtered"
    _refresh_cached_entry_origin(
        ctx.entry,
        ctx.cfg,
        ctx.feed_url,
        score=score,
        score_path=path,
        strategy_version=meta["strategy_version"],
    )
    # An exact-link hit on a similarity copy remains a duplicate, not a new
    # independent article or a fresh backtest sample.
    if path == "similarity":
        return None, "similarity"
    if path in NON_SUBSTANTIVE_SCORE_PATHS:
        return None, "filtered"
    if score is not None and score >= ctx.pass_threshold:
        ctx.entry.title = plain_text_from_markup(title)
        ctx.entry.ai_summary = summary or ""
        ctx.entry._raw_title = plain_text_from_markup(raw_title_cached or ctx.raw_title)
        _set_scoring_meta(
            ctx.entry,
            score_path=meta["score_path"],
            score_confidence=meta["score_confidence"],
            strategy_version=meta["strategy_version"],
            direction=normalize_direction(meta["direction"], text=title),
            event_id=meta["event_id"],
            event_version=meta["event_version"],
            score_explanation=dict(zip(EXPLANATION_FIELDS, cached[13:19])),
        )
        if meta["event_id"]:
            _assign_event_identity(ctx.entry, meta["event_id"], meta["event_version"])
        else:
            _persisted_event_identity(ctx.entry)
        return ctx.entry, "cache"
    return None, "filtered"


def _process_entry_cache(ctx, link_cache):
    """Return a terminal cache decision, or None to continue with scoring."""
    # A batch-level ``link_cache`` is prefetched before this entry's
    # same-link turn; a concurrent source may have committed this link since.
    # Re-read only this link so reuse follows the committed row.  The snapshot
    # remains the fallback only when no current row exists.
    cached = DB.get_cached_entry(ctx.entry.link)
    if cached is None and link_cache is not None:
        cached = link_cache.get(ctx.entry.link)
    if cached is None:
        return None
    # Keep tuple validation ahead of metadata lookup and every repair side effect.
    _title, raw_title_cached, _score, _tag, _logic, _created_at, summary = cached[:7]
    meta = _cached_entry_scoring_meta(ctx.entry, cached, ctx.strategy_version)
    if meta["score_path"] == "quality_quarantine":
        if not bool(ctx.cfg.get("_allow_quality_quarantine_repair", False)):
            # Ordinary polling cannot partially unquarantine a failed batch.
            return None, "quality_quarantine"
        if ctx.strategy_version and meta["strategy_version"] != ctx.strategy_version:
            return _prepare_entry_quality_repair(ctx, raw_title_cached, summary, meta)
    if ctx.strategy_version and meta["strategy_version"] != ctx.strategy_version:
        return None
    return _reuse_cached_entry(ctx, cached, meta)


def _quality_snapshot_options(ctx):
    return (
        {
            "revision_key": "quality_repair:auto",
            "signal_stage": "quality_repair",
            "auto_revision": True,
            "require_insert": True,
        }
        if ctx.quality_repair
        else {}
    )


def _persist_similar_entry(ctx, similar_info, notify_threshold):
    entry, cfg, raw_title, feed_url = ctx.entry, ctx.cfg, ctx.raw_title, ctx.feed_url
    score, tag, logic = (similar_info[key] for key in ("score", "tag", "logic"))
    direction = similar_info["direction"]
    if ctx.quality_repair and not _is_usable_quality_repair_result(
        score=score,
        tag=tag,
        logic=logic,
        score_path="similarity",
        error_type="",
        model_name="",
        is_similarity_copy=True,
    ):
        logger.error(
            "[QualityRepair] Refusing unusable similarity repair for %s",
            entry.link,
        )
        return None, "quality_quarantine"
    similar_scored_at = int(time.time())
    formatted_title = f"[⭐{score} {direction_emoji(direction)} {tag}] {raw_title}"
    summary = ctx.quality_repair_summary if ctx.quality_repair else ""
    signal_snapshot = _build_backtest_signal_snapshot(
        entry=entry,
        raw_title=raw_title,
        score=score,
        tag=tag,
        logic=logic,
        summary=summary,
        direction=direction,
        score_path="similarity",
        score_confidence=0.0,
        strategy_version=str(cfg.get("scoring_strategy_version", "")),
        feed_url=feed_url,
        scored_at=similar_scored_at,
        cfg=cfg,
        is_similarity_copy=True,
        **_quality_snapshot_options(ctx),
    )
    write_result = DB.set_cached_entry(
        entry.link,
        formatted_title,
        raw_title,
        score,
        tag,
        logic,
        summary,
        feed_url=feed_url,
        score_path="similarity",
        prompt_key=ctx.prompt_name,
        model_name="",
        latency_ms=0,
        source_tier=_source_tier(cfg, feed_url),
        strategy_version=str(cfg.get("scoring_strategy_version", "")),
        direction=direction,
        created_at=_entry_timestamp(entry),
        backtest_signal=signal_snapshot,
    )
    event_id, event_version = _persisted_event_identity(entry, write_result)
    if event_id:
        signal_snapshot.update(
            {
                "event_id": event_id,
                "event_version": event_version,
                "independence_key": event_id,
            }
        )
    effective_notify = (
        notify_threshold
        if notify_threshold is not None
        else DB.get_feed_thresholds(cfg, feed_url)["notify"]
    )
    if score >= effective_notify:
        send_telegram_notification(
            raw_title, entry.link, score, tag, logic, cfg, effective_notify
        )
    _set_scoring_meta(
        entry,
        score_path="similarity",
        prompt_key=ctx.prompt_name,
        model_name="",
        latency_ms=0,
        error_type="",
        source_tier=_source_tier(cfg, feed_url),
        direction=direction,
        event_id=event_id,
        event_version=event_version,
    )
    return None, "similarity"


def _process_entry_similarity(ctx, recent_entries, notify_threshold):
    similar, sim_score = find_similar_article(
        ctx.raw_title, recent_entries, ctx.cfg["threshold"]["similarity"]
    )
    similar_info = None
    if similar is not None:
        _, _similar_title, _, score, tag, logic = similar[:6]
        direction = _similar_article_direction(similar)
        if is_usable_score_result(
            score, tag, logic
        ) and not is_fixed_safe_neutral_fields(score, tag, logic, direction):
            similar_info = {
                "score": score,
                "tag": tag,
                "logic": logic,
                "similarity": sim_score,
                "direction": direction,
            }
        else:
            logger.warning(
                "[Similarity] Refusing unusable cached score for %s; rescoring %s",
                str(similar[0] or "")[:160],
                str(getattr(ctx.entry, "link", "") or "")[:160],
            )
        old_title = str(similar[2] or similar[1] or "")
        from core.score_evidence import event_reuse_conflicts, similar_logic_ungrounded
        identity = getattr(ctx, "similarity_identity", None) or _SimilarityIdentitySnapshot()
        compared = identity.compare(ctx.raw_title, old_title) if similar_info is not None else None
        if similar_info is not None and (
            compared is None
            or event_reuse_conflicts(ctx.raw_title, old_title)
            or similar_logic_ungrounded(
                ctx.raw_title,
                str(getattr(ctx, "source_summary", "") or ""),
                similar_info,
            )
            or _should_rescore_similar(ctx.raw_title, ctx.cfg, ctx.feed_url)
        ):
            logger.info(
                "[Similarity] candidate only; rescoring '%s'",
                str(ctx.raw_title or "")[:80],
            )
            return similar_info, None
        if similar_info is not None:
            return similar_info, _persist_similar_entry(
                ctx, similar_info, notify_threshold
            )
    return similar_info, None


def _match_entry_scoring_rules(ctx):
    """Preserve hint metrics and explicit fast/local rule precedence before LLM."""
    cfg, raw_title = ctx.cfg, ctx.raw_title
    fp = _match_fast_pass(raw_title, cfg, feed_url=ctx.feed_url)
    result = _EntryScore()
    if fp is not None:
        result.score, result.tag, result.logic, result.sentiment, skip_llm = fp
        result.fast_pass_hint = {
            "score": result.score,
            "tag": result.tag,
            "logic": result.logic,
        }
        get_metrics().record_fast_pass(str(result.tag or "FastPass")[:64])
        if skip_llm:
            result.used_fast_pass = True
            result.score_path = "fast_pass"
            logger.info(
                "[FastPass] '%s' -> %.1f (%s)",
                raw_title[:50],
                result.score,
                result.logic,
            )
        else:
            logger.info(
                "[FastPass:hint] '%s' matched, but skip_llm=false", raw_title[:50]
            )
    if not result.used_fast_pass:
        loc = _match_local_reject(raw_title, cfg)
        if loc is None:
            fr_max = float(cfg.get("local_fast_reject_max_rule_score") or 0)
            if fr_max > 0:
                old_loc = Scorer.best_explicit_local_rule(raw_title, cfg)
                if old_loc is not None and old_loc[0] <= fr_max:
                    loc = old_loc
        if loc is not None:
            result.score, result.tag, result.logic, result.sentiment = loc
            result.used_local_fast = True
            result.score_path = "local_reject"
    return result


def _entry_stage2_reasons(ctx, result, similar_info):
    cfg = ctx.cfg.get("two_stage_scoring", {}) or {}
    reasons = []
    if cfg.get("enabled", False) and not result.stage1_resolution:
        lo = float(cfg.get("stage2_min_score", 6.5) or 6.5)
        hi = float(cfg.get("stage2_max_score", 8.5) or 8.5)
        if lo <= result.score < hi:
            reasons.append("boundary_score")
        if result.fast_pass_hint and cfg.get("stage2_on_fast_pass_hint", True):
            reasons.append("fast_pass_hint")
        if (
            similar_info
            and similar_info.get("score", 0) > result.score
            and cfg.get("stage2_on_similar_higher_score", True)
        ):
            reasons.append("similar_higher_score")
        high_tiers = set(cfg.get("high_value_source_tiers", []) or [])
        if _source_tier(ctx.cfg, ctx.feed_url) in high_tiers and lo <= result.score:
            reasons.append("high_value_source")
    return reasons


def _review_entry_score(
    ctx, result, scorer, raw_summary, prompt_text, few_shot_context, reasons
):
    t0 = time.time()
    stage2_outcome = _score_stage_with_identity(
        scorer,
        "stage2",
        ctx.raw_title,
        raw_summary,
        prompt_text,
        ctx.cfg,
        few_shot_context=few_shot_context,
    )
    values, result.stage2_model, result.stage2_resolution, stage2_reason = stage2_outcome
    # Stage 2 decides the final score, so its verdict status replaces stage 1's.
    result.transport_unverdicted = bool(
        getattr(stage2_outcome, "transport_unverdicted", False)
    )
    score, tag, logic, sentiment = values
    result.score_latency_ms += int((time.time() - t0) * 1000)
    result.stage2_score = score
    result.selected_model = result.stage2_model
    result.review_reason = ",".join(dict.fromkeys(reasons))
    result.score, result.tag, result.logic, result.sentiment = (
        score,
        tag,
        logic,
        sentiment,
    )
    if result.stage2_model == "fallback_local":
        result.score_path = "fallback_local"
        result.error_type = "llm_backends_exhausted"
        result.neutral_reason = ""
    elif result.stage2_resolution:
        result.score_path = SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION[
            result.stage2_resolution
        ]["stage2"]
        result.error_type = ""
        result.neutral_reason = stage2_reason
    else:
        result.score_path = "stage2"
        result.error_type = ""
        result.neutral_reason = ""


def _annotate_entry_model_score(ctx, result, similar_info):
    non_substantive = result.score_path in NON_SUBSTANTIVE_SCORE_PATHS
    result.score_confidence = (
        0.0
        if non_substantive
        else min(1.0, max(0.0, abs(float(result.score) - ctx.pass_threshold) / 3.0))
    )
    if result.score < 0:
        result.score_path = "parse_error"
        result.error_type = "parse_error"
    if (
        not non_substantive
        and result.fast_pass_hint
        and result.score < result.fast_pass_hint["score"]
    ):
        result.logic = f"{result.logic}; FastPass候选:{result.fast_pass_hint['tag']}"
    if not non_substantive and similar_info and result.score < similar_info["score"]:
        result.logic = f"{result.logic}; 相似旧分:{similar_info['score']}"


@capture_article_score
def _score_entry_with_llm(
    ctx, result, scorer, raw_summary, prompt_text, few_shot_context, similar_info
):
    """Keep both stages inside the same exception boundary and retain partial evidence."""
    try:
        t0 = time.time()
        stage1_outcome = _score_stage_with_identity(
            scorer,
            "stage1",
            ctx.raw_title,
            raw_summary,
            prompt_text,
            ctx.cfg,
        )
        values, result.stage1_model, result.stage1_resolution, stage1_reason = (
            stage1_outcome
        )
        result.transport_unverdicted = bool(
            getattr(stage1_outcome, "transport_unverdicted", False)
        )
        result.score, result.tag, result.logic, result.sentiment = values
        result.stage1_score = result.score
        result.selected_model = result.stage1_model
        if result.stage1_model == "fallback_local":
            result.score_path = "fallback_local"
            result.error_type = "llm_backends_exhausted"
            result.neutral_reason = ""
        elif result.stage1_resolution:
            result.score_path = SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION[
                result.stage1_resolution
            ]["stage1"]
            result.neutral_reason = stage1_reason
        result.score_latency_ms = int((time.time() - t0) * 1000)
        reasons = _entry_stage2_reasons(ctx, result, similar_info)
        if reasons:
            _review_entry_score(
                ctx,
                result,
                scorer,
                raw_summary,
                prompt_text,
                few_shot_context,
                reasons,
            )
        _annotate_entry_model_score(ctx, result, similar_info)
    except (ValueError, TypeError) as error:
        logger.warning("Scoring parsing error for '%s': %s", ctx.raw_title[:50], error)
        result.score, result.tag, result.logic, result.sentiment = (
            -1.0,
            "解析异常",
            "LLM输出格式无法解析",
            "⚖️",
        )
        result.score_path = "parse_error"
        result.error_type = "parse_error"
        result.transport_unverdicted = False
    except Exception:
        logger.exception("Scoring failed for: %s", ctx.raw_title[:50])
        result.score, result.tag, result.logic, result.sentiment = (
            scorer._fallback_local_score(ctx.raw_title, ctx.cfg)
        )
        result.score_path = "fallback_local"
        result.error_type = "score_error"
        result.selected_model = "fallback_local"
        result.transport_unverdicted = False


def _entry_quality_repair_is_usable(ctx, result):
    if ctx.quality_repair and not _is_usable_quality_repair_result(
        score=result.score,
        tag=result.tag,
        logic=result.logic,
        score_path=result.score_path,
        error_type=result.error_type,
        model_name=result.selected_model,
        stage1_model=result.stage1_model,
        stage2_model=result.stage2_model,
        is_similarity_copy=False,
    ):
        logger.error(
            "[QualityRepair] Refusing non-model or unusable repair for %s "
            "(path=%s, error=%s, model=%s)",
            ctx.entry.link,
            result.score_path,
            result.error_type,
            result.selected_model,
        )
        return False
    return True


def _entry_score_metadata(ctx, result):
    # DB.log_llm_call owns the global attempts counter; non-substantive paths
    # do not enter the substantive score distribution.
    if result.score_path not in NON_SUBSTANTIVE_SCORE_PATHS:
        DB.record_score(result.score)
    direction = normalize_direction(
        result.sentiment, text=f"{ctx.raw_title} {result.logic}"
    )
    scored_at = int(time.time())
    meta = {
        "score_path": result.score_path,
        "prompt_key": ctx.prompt_name,
        "model_name": result.selected_model,
        "latency_ms": result.score_latency_ms,
        "error_type": result.error_type,
        "safe_neutral_reason": result.neutral_reason,
        "source_tier": _source_tier(ctx.cfg, ctx.feed_url),
        "strategy_version": ctx.strategy_version,
        "stage1_score": result.stage1_score,
        "stage1_model": result.stage1_model,
        "stage2_score": result.stage2_score,
        "stage2_model": result.stage2_model,
        "score_confidence": result.score_confidence,
        "review_reason": result.review_reason,
        "direction": direction,
        "score_explanation": build_score_explanation(
            raw_score=result.raw_score,
            score=result.score,
            score_path=result.score_path,
            score_threshold=ctx.pass_threshold,
            stage1_score=result.stage1_score,
            stage2_score=result.stage2_score,
            stage1_model=result.stage1_model,
            stage2_model=result.stage2_model,
            fast_pass_hint=result.fast_pass_hint,
        ),
    }
    return meta, scored_at


def _entry_signal_snapshot(ctx, result, meta, scored_at, summary):
    snapshot = _build_backtest_signal_snapshot(
        entry=ctx.entry,
        raw_title=ctx.raw_title,
        score=result.score,
        tag=result.tag,
        logic=result.logic,
        summary=summary,
        direction=meta["direction"],
        score_path=result.score_path,
        score_confidence=result.score_confidence,
        strategy_version=ctx.strategy_version,
        feed_url=ctx.feed_url,
        scored_at=scored_at,
        cfg=ctx.cfg,
        **_quality_snapshot_options(ctx),
    )
    snapshot.update(meta["score_explanation"])
    return snapshot


def _bind_entry_persisted_identity(entry, write_result, meta, signal_snapshot):
    event_id, event_version = _persisted_event_identity(entry, write_result)
    if event_id:
        meta.update({"event_id": event_id, "event_version": event_version})
        signal_snapshot.update(
            {
                "event_id": event_id,
                "event_version": event_version,
                "independence_key": event_id,
            }
        )
    persist_scoring_evidence(entry, signal_snapshot)


def _formatted_entry_score_title(ctx, result, meta):
    return (
        f"[⭐{result.score} {direction_emoji(meta['direction'])} {result.tag}] "
        f"{ctx.raw_title} 💡{result.logic}"
    )


def _persist_filtered_entry(ctx, result, meta, scored_at):
    summary = ctx.quality_repair_summary if ctx.quality_repair else ""
    snapshot = _entry_signal_snapshot(ctx, result, meta, scored_at, summary)
    natural_scoring.freeze_write({
        "kind": "filtered", "result": asdict(result), "meta": meta,
        "scored_at": scored_at, "snapshot": snapshot,
    })
    if result.transport_unverdicted and not ctx.quality_repair:
        pending = _remember_transport_write(ctx, result, meta, scored_at, snapshot)
        return _commit_transport_write(pending, ctx.entry)
    return _commit_filtered_entry(ctx, result, meta, scored_at, snapshot)


def _commit_filtered_entry(ctx, result, meta, scored_at, snapshot):
    summary = ctx.quality_repair_summary if ctx.quality_repair else ""
    if ctx.quality_repair or ctx.cfg.get("cache_rejected_entries", True):
        formatted_title = _formatted_entry_score_title(ctx, result, meta)
        write_result = DB.set_cached_entry(
            ctx.entry.link,
            formatted_title,
            ctx.raw_title,
            result.score,
            result.tag,
            result.logic,
            summary,
            feed_url=ctx.feed_url,
            created_at=_entry_timestamp(ctx.entry),
            backtest_signal=snapshot,
            **meta,
        )
    else:
        # Rejected-entry caching never controls retention of the long-lived
        # scored population needed for future monotonicity analysis.
        write_result = DB.store_backtest_signal(snapshot)
    _bind_entry_persisted_identity(ctx.entry, write_result, meta, snapshot)
    _set_scoring_meta(ctx.entry, **meta)
    return None, "filtered"


def _summarize_scored_entry(ctx, result, scorer, raw_summary, fp_cfg):
    min_sum = float(ctx.cfg.get("summary_min_score", 0) or 0)
    # Repairs must reuse the quarantine transaction's frozen clean snapshot;
    # re-summarizing would violate its cache/ledger compare-and-swap contract.
    want_sum = (
        not ctx.quality_repair
        and bool(ctx.cfg.get("generate_summary", False))
        and (min_sum <= 0 or result.score + 1e-9 >= min_sum)
    )
    if result.used_fast_pass and fp_cfg.get("skip_summary", False):
        want_sum = False
    if ctx.quality_repair:
        return ctx.quality_repair_summary
    if want_sum:
        try:
            return scorer.summarize(ctx.raw_title, raw_summary, ctx.cfg)
        except (ValueError, TypeError) as error:
            logger.warning("Summarize parsing error: %s", error)
        except Exception:
            logger.exception("Summarize failed for: %s", ctx.raw_title[:50])
    return ""


def _persist_scored_entry(
    ctx, result, meta, scored_at, scorer, raw_summary, fp_cfg, notify_threshold
):
    summary = _summarize_scored_entry(ctx, result, scorer, raw_summary, fp_cfg)
    snapshot = _entry_signal_snapshot(ctx, result, meta, scored_at, summary)
    natural_scoring.freeze_write({
        "kind": "scored", "result": asdict(result), "meta": meta,
        "scored_at": scored_at, "snapshot": snapshot, "summary": summary,
        "notify_threshold": notify_threshold,
    })
    return _commit_scored_entry(
        ctx, result, meta, scored_at, snapshot, summary, notify_threshold,
    )


def _commit_scored_entry(ctx, result, meta, scored_at, snapshot, summary, notify_threshold):
    formatted_title = _formatted_entry_score_title(ctx, result, meta)
    write_result = DB.set_cached_entry(
        ctx.entry.link,
        formatted_title,
        ctx.raw_title,
        result.score,
        result.tag,
        result.logic,
        summary,
        feed_url=ctx.feed_url,
        created_at=_entry_timestamp(ctx.entry),
        backtest_signal=snapshot,
        **meta,
    )
    _bind_entry_persisted_identity(ctx.entry, write_result, meta, snapshot)
    ctx.entry.title = formatted_title
    ctx.entry.ai_summary = summary
    ctx.entry._raw_title = ctx.raw_title
    _set_scoring_meta(ctx.entry, **meta)
    if result.score >= notify_threshold:
        send_telegram_notification(
            ctx.raw_title,
            ctx.entry.link,
            result.score,
            result.tag,
            result.logic,
            ctx.cfg,
            notify_threshold,
        )
    if result.used_fast_pass:
        return ctx.entry, "fast_pass"
    return ctx.entry, "local_fast" if result.used_local_fast else "scored"


# One article link can arrive from several feeds in parallel webhook/refresh
# tasks.  The cache/ledger pre-checks below are deliberately read-only, so two
# concurrent tasks could both miss them, both call the model, and the slower
# writer would then (correctly) be rejected by the frozen ledger/cache guards.
# Serializing per link inside this process lets the second task observe the
# first task's committed result through the unchanged pre-checks instead.
_ENTRY_LINK_WAIT_SECONDS = 120.0
_entry_link_slots: Dict[str, list] = {}
_entry_link_slots_guard = threading.Lock()


@contextmanager
def _entry_link_single_flight(link: str):
    key = str(link or "").strip()
    if not key:
        yield
        return
    with _entry_link_slots_guard:
        slot = _entry_link_slots.get(key)
        if slot is None:
            slot = _entry_link_slots[key] = [threading.RLock(), 0]
        slot[1] += 1
    acquired = slot[0].acquire(timeout=_ENTRY_LINK_WAIT_SECONDS)
    if not acquired:
        # Fall back to the previous unserialized behavior; the persistence
        # guards still reject a conflicting write and the task stays retryable.
        logger.warning(
            "[Process] same-link wait timed out; continuing unserialized: %s",
            key[:160],
        )
    try:
        yield
    finally:
        if acquired:
            slot[0].release()
        with _entry_link_slots_guard:
            slot[1] -= 1
            if slot[1] <= 0 and _entry_link_slots.get(key) is slot:
                del _entry_link_slots[key]


def _transport_retry_key(ctx):
    return (str(ctx.strategy_version or ""), str(getattr(ctx.entry, "link", "") or ""))


def _clear_transport_retry(ctx):
    with _transport_retry_lock:
        key = _transport_retry_key(ctx)
        _transport_retry_counts.pop(key, None)
        _transport_pending_writes.pop(key, None)


def _reserve_transport_retry_slot(ctx):
    """Never evict uncommitted work and accidentally grant it another budget."""
    key = _transport_retry_key(ctx)
    if natural_scoring.durable_attempt() is not None:
        with _transport_retry_lock:
            _transport_retry_counts.pop(key, None)
        return
    if ctx.quality_repair or not key[1]:
        return
    with _transport_retry_lock:
        if key not in _transport_retry_counts:
            if len(_transport_retry_counts) >= _TRANSPORT_RETRY_TRACKED_MAX:
                logger.warning("[Score] transport retry capacity full; deferring before scoring")
                raise ScoringTransportRetryable(key[1], 0)
            _transport_retry_counts[key] = 0


def _remember_transport_write(ctx, result, meta, scored_at, snapshot):
    key = _transport_retry_key(ctx)
    with _transport_retry_lock:
        pending = _transport_pending_writes.get(key)
        if pending is None:
            frozen_ctx = copy(ctx)
            frozen_ctx.entry = copy(ctx.entry)
            frozen_ctx.cfg = {"cache_rejected_entries": ctx.cfg.get("cache_rejected_entries", True)}
            frozen_ctx.source_summary = ""
            frozen_ctx.similarity_identity = None
            pending = _PendingTransportWrite(
                frozen_ctx, deepcopy(result), deepcopy(meta), scored_at, deepcopy(snapshot),
            )
            _transport_pending_writes[key] = pending
        return pending


def _commit_transport_write(pending, entry):
    snapshot, meta = deepcopy(pending.snapshot), deepcopy(pending.meta)
    # Preserve the scoring decision, but never backdate first availability to a
    # write that failed. A committed previous attempt exits through cache/ledger.
    snapshot["available_at"] = max(pending.scored_at, int(time.time()))
    outcome = _commit_filtered_entry(
        pending.ctx, pending.result, meta, pending.scored_at, snapshot,
    )
    _set_scoring_meta(entry, **meta)
    if meta.get("event_id"):
        _assign_event_identity(entry, meta["event_id"], meta["event_version"])
    _clear_transport_retry(pending.ctx)
    return outcome


def _resume_transport_write(ctx):
    with _transport_retry_lock:
        pending = _transport_pending_writes.get(_transport_retry_key(ctx))
    return _commit_transport_write(pending, ctx.entry) if pending is not None else None


def _defer_unverdicted_safe_neutral(ctx, result) -> None:
    """Raise before any write when a safe-neutral still lacks a backup verdict.

    Bounded by ``TRANSPORT_RETRY_MAX`` per (strategy, link): once exhausted
    the safe-neutral is persisted as final, exactly as before this change.
    """
    key = _transport_retry_key(ctx)
    unverdicted = (
        result.transport_unverdicted
        and result.score_path in NON_SUBSTANTIVE_SCORE_PATHS
        and not ctx.quality_repair
        and bool(key[1])
    )
    persisted_attempt = natural_scoring.durable_attempt()
    if persisted_attempt is not None:
        if unverdicted and persisted_attempt <= TRANSPORT_RETRY_MAX:
            raise ScoringTransportRetryable(key[1], persisted_attempt)
        return
    with _transport_retry_lock:
        if not unverdicted:
            _transport_retry_counts.pop(key, None)
            return
        if key not in _transport_retry_counts and len(_transport_retry_counts) >= _TRANSPORT_RETRY_TRACKED_MAX:
            raise ScoringTransportRetryable(key[1], 0)
        attempt = min(
            _transport_retry_counts.get(key, 0) + 1, TRANSPORT_RETRY_MAX + 1
        )
        _transport_retry_counts[key] = attempt
        if attempt > TRANSPORT_RETRY_MAX:
            logger.warning(
                "[Score] unverdicted safe-neutral retries exhausted; persisting as final"
            )
            return
    raise ScoringTransportRetryable(key[1], attempt)


def process_entry(
    entry,
    recent_entries: list,
    prompt_text: str,
    cfg: dict,
    scorer: Optional[Scorer] = None,
    *,
    few_shot_context: str = "",
    link_cache: Optional[Dict[str, tuple]] = None,
    feed_url: str = "",
    notify_threshold: Optional[float] = None,
    similarity_identity=None,
    persist_deferred: bool = False,
) -> Tuple[Optional[Any], str]:
    """Process one RSS item through ordered phases, preserving each early exit."""
    few_shot_context = _validate_entry_feedback_context(cfg, few_shot_context)
    with _entry_link_single_flight(getattr(entry, "link", "")):
        def run(original, prompt, source, context):
            threshold = notify_threshold
            if natural_scoring.bound_feed():
                threshold = DB.get_feed_thresholds(cfg, source)["notify"]
            return _traced_process_entry(
                original, recent_entries, prompt, cfg, scorer,
                few_shot_context=context, link_cache=link_cache, feed_url=source,
                notify_threshold=threshold, similarity_identity=similarity_identity,
            )

        return natural_scoring.run_with_continuation(
            entry, prompt_text, cfg, DB.canonical_feed_url(cfg, feed_url),
            few_shot_context, run, capture=persist_deferred,
        )


def _traced_process_entry(entry, recent_entries, prompt_text, cfg, scorer, **kwargs):
    with trace_news_processing(entry, cfg, kwargs["feed_url"]) as trace:
        try:
            outcome = _process_entry_phases(
                entry, recent_entries, prompt_text, cfg, scorer, **kwargs,
            )
        except ScoringTransportRetryable as exc:
            natural_scoring.note_deferred_attempt(exc.attempt)
            logger.info("[Score] entry deferred before any write: %s", exc)
            outcome = None, SCORE_DEFERRED_REASON
        except natural_scoring.ContinuationDeferred:
            outcome = None, SCORE_DEFERRED_REASON
        except natural_scoring.ContinuationTerminalUnscored:
            with _transport_retry_lock:
                key = (str(cfg.get("scoring_strategy_version") or ""), str(entry.link or ""))
                _transport_retry_counts.pop(key, None)
                _transport_pending_writes.pop(key, None)
            outcome = None, SCORE_TERMINAL_UNSCORED_REASON
        trace["result"] = outcome
        return outcome


def _process_entry_phases(
    entry,
    recent_entries: list,
    prompt_text: str,
    cfg: dict,
    scorer: Optional[Scorer],
    *,
    few_shot_context: str,
    link_cache: Optional[Dict[str, tuple]],
    feed_url: str,
    notify_threshold: Optional[float],
    similarity_identity=None,
) -> Tuple[Optional[Any], str]:
    few_shot_context = _validate_entry_feedback_context(cfg, few_shot_context)
    # Persist one canonical identity across polling URLs and pre-rewrite URLs.
    feed_url = DB.canonical_feed_url(cfg, feed_url)
    raw_title, source_summary, reason = _normalize_entry_source(entry, cfg)
    if reason:
        natural_scoring.before_source_rejection()
        return None, reason
    ctx = _EntryContext(
        entry=entry,
        cfg=cfg,
        feed_url=feed_url,
        raw_title=raw_title,
        source_summary=source_summary,
        pass_threshold=_pass_threshold_for_feed(cfg, feed_url),
        strategy_version=str(cfg.get("scoring_strategy_version", "")),
        # Persisted prompt_key names the text this call hands to the model.
        prompt_name=prompt_name_for_body(prompt_text, feed_url, cfg),
        similarity_identity=similarity_identity,
    )
    cached_result = _process_entry_cache(ctx, link_cache)
    if cached_result is not None:
        mark_news_processing_reuse("cache")
        _clear_transport_retry(ctx)
        return cached_result
    # Cache expiry does not authorize a second score for an already frozen
    # strategy. Only explicitly authorized quality replay bypasses this exit.
    if not ctx.quality_repair and ctx.strategy_version:
        latest_path = normalize_score_path(
            DB.get_latest_signal_score_path(entry.link, ctx.strategy_version)
        )
        if latest_path:
            mark_news_processing_reuse("ledger")
            _clear_transport_retry(ctx)
            return None, "similarity" if latest_path == "similarity" else "filtered"
    try:
        natural_scoring.before_uncommitted_work()
    except (natural_scoring.ContinuationDeferred, natural_scoring.ContinuationTerminalUnscored):
        _clear_transport_retry(ctx)
        raise
    pending_result = _resume_transport_write(ctx)
    if pending_result is not None:
        return pending_result
    pending_result = _resume_natural_write(ctx)
    if pending_result is not None:
        return pending_result
    similar_info, similar_result = _process_entry_similarity(
        ctx, recent_entries, notify_threshold
    )
    if similar_result is not None:
        if similar_result[1] == "similarity":
            mark_news_processing_reuse("similarity")
            _clear_transport_retry(ctx)
        return similar_result
    raw_summary = (
        ctx.quality_repair_summary if ctx.quality_repair else ctx.source_summary
    )
    if raw_summary and ("<" in raw_summary and ">" in raw_summary):
        raw_summary = clean_html(raw_summary)
    scorer = scorer or Scorer()
    fp_cfg = cfg.get("fast_pass", {})
    result = _match_entry_scoring_rules(ctx)
    if not result.used_local_fast and not result.used_fast_pass:
        _reserve_transport_retry_slot(ctx)
        natural_scoring.before_score()
        _score_entry_with_llm(
            ctx,
            result,
            scorer,
            raw_summary,
            prompt_text,
            few_shot_context,
            similar_info,
        )
    _defer_unverdicted_safe_neutral(ctx, result)
    non_substantive = result.score_path in NON_SUBSTANTIVE_SCORE_PATHS
    result.raw_score = result.score
    result.score, result.tag, result.logic = _apply_score_adjustments(
        result.score,
        result.tag,
        result.logic,
        cfg,
        feed_url,
        ctx.raw_title,
        non_substantive,
    )
    if not _entry_quality_repair_is_usable(ctx, result):
        return None, "quality_quarantine"
    effective_notify = _effective_notify_threshold(
        cfg, feed_url, notify_threshold, ctx.raw_title
    )
    meta, scored_at = _entry_score_metadata(ctx, result)
    if non_substantive or result.score < ctx.pass_threshold:
        return _persist_filtered_entry(ctx, result, meta, scored_at)
    return _persist_scored_entry(
        ctx,
        result,
        meta,
        scored_at,
        scorer,
        raw_summary,
        fp_cfg,
        effective_notify,
    )


def _resume_natural_write(ctx):
    pending = natural_scoring.pending_write()
    if pending is None:
        return None
    result = _EntryScore(**pending["result"])
    meta, snapshot = deepcopy(pending["meta"]), deepcopy(pending["snapshot"])
    scored_at = pending["scored_at"]
    snapshot["available_at"] = max(scored_at, int(time.time()))
    if pending["kind"] == "filtered":
        outcome = _commit_filtered_entry(ctx, result, meta, scored_at, snapshot)
    else:
        outcome = _commit_scored_entry(
            ctx, result, meta, scored_at, snapshot, pending["summary"],
            pending["notify_threshold"],
        )
    _clear_transport_retry(ctx)
    return outcome


def _validated_feed_entries(feed) -> list:
    """Return usable entries or reject an unrecognizable/malformed empty feed."""
    raw_entries = list(getattr(feed, "entries", None) or [])
    valid_entries = [
        entry
        for entry in raw_entries
        if (entry.get("title") or "").strip()
        and (entry.get("link") or "").strip()
    ]
    # feedparser reports malformed input through ``bozo`` instead of raising.
    # A CDN/SSO HTML error page can otherwise look like a valid empty feed and
    # incorrectly advance source freshness. Preserve tolerant parsing when at
    # least one usable entry survived, and allow genuinely empty feeds only
    # when RSS/Atom metadata is recognizable.
    bozo = bool(getattr(feed, "bozo", False))
    feed_metadata = getattr(feed, "feed", None) or {}
    feed_version = str(getattr(feed, "version", "") or "").strip()
    if not valid_entries and (bozo or (not feed_metadata and not feed_version)):
        detail = str(getattr(feed, "bozo_exception", "invalid feed document"))
        raise ValueError(detail.replace("\n", " ")[:100])
    return valid_entries


def feed_processing_complete(stats: dict, error=None) -> bool:
    """A pending score or missing publisher item cannot populate a full cache."""
    return not (
        error or stats.get("entry_errors", 0)
        or stats.get("score_deferred", 0) or stats.get("source_unrecognized", 0)
        or stats.get("score_terminal_unscored", 0)
    )


def _feed_health_warning(stats, entry_count, error):
    warnings = []
    if stats["entry_errors"]:
        warnings.append((
            f"部分条目处理失败: {stats['entry_errors']}/{entry_count}; "
            f"{error or 'unknown error'}"
        )[:300])
    if stats["source_unrecognized"]:
        warnings.append(f"来源内容缺口: {stats['source_unrecognized']}条未识别")
    if stats["score_deferred"]:
        warnings.append(f"待评分: {stats['score_deferred']}条，未计入过滤")
    if stats.get("score_terminal_unscored", 0):
        warnings.append(f"终止未评分: {stats['score_terminal_unscored']}条，不再续作，未计入过滤")
    return "; ".join(warnings)[:500] or None


def _unfinished_score_stat(reason):
    return {
        SCORE_DEFERRED_REASON: "score_deferred",
        SCORE_TERMINAL_UNSCORED_REASON: "score_terminal_unscored",
    }.get(reason)


def process_feed(
    target_url: str,
    prompt_text: Optional[str] = None,
    feed_name: Optional[str] = None,
    cfg: Optional[dict] = None,
    *,
    check_output_cache: bool = True, persist_deferred: bool = False,
) -> Tuple[list, Dict[str, Any], Optional[str], Optional[str]]:
    """处理单个 RSS 源，返回 (passed_entries, 统计字典, 错误信息, feed 频道标题或 None)"""
    start_time = time.time()
    if cfg is None:
        from core.config import config as _config_mod
        cfg = _config_mod.cfg
    if not cfg.get("_blacklist_re"):
        from core.config import config as _config_mod
        cfg = {**cfg, "_blacklist_re": _config_mod.blacklist_re}
    cfg = resolve_scoring_contract(cfg, register_snapshot=True)
    prompt_text = prompt_text or resolve_prompt(target_url, cfg)[1]
    feed_name = feed_name or target_url

    output_cache_key = strategy_cache_key(target_url, cfg)
    if check_output_cache:
        cached_xml = get_cached(output_cache_key, cfg["cache_ttl"])
        if cached_xml is not None:
            bump_stat(DB.update_stats, total_requests=1, cache_hits=1)
            return [], {
                "total": 0, "passed": 0, "cache_hits": 1,
                "similarity_hits": 0, "blacklist_hits": 0,
                "api_calls": 0, "duration_ms": 0
            }, None, None

    stats = {
        "total": 0, "passed": 0, "cache_hits": 0,
        "similarity_hits": 0, "blacklist_hits": 0,
        "fast_pass_hits": 0, "api_calls": 0, "entry_errors": 0,
        "duration_ms": 0, "score_deferred": 0, "score_terminal_unscored": 0, "source_unrecognized": 0,
    }
    error_msg = None
    passed_entries = []
    scores = []

    try:
        rss_start = time.time()
        attempts = max(1, int(cfg.get("rss_max_fetch_attempts", 3) or 1))
        raw = _load_feed_bytes(
            target_url, cfg, cfg.get("rss_timeout", 10), attempts
        )
        feed = feedparser.parse(raw)
        valid_entries = _validated_feed_entries(feed)
        from services.publisher_feeds import publisher_source_coverage

        stats.update(publisher_source_coverage(feed))
        rss_duration = int((time.time() - rss_start) * 1000)
    except Exception as e:
        error_msg = f"RSS解析失败: {str(e)[:100]}"
        logger.warning("Feed fetch failed [%s]: %s", feed_name, error_msg)
        DB.update_feed_health(target_url, feed_name, 0, False, 0, 0, None, error_msg)
        return [], stats, error_msg, None

    recent_entries = DB.get_recent_entries(cfg=cfg)
    me_global = int(cfg.get("max_entries", 15) or 15)
    me_cap = max_entries_for_feed_url(target_url, cfg)
    max_entries = me_cap if me_cap is not None else me_global
    try:
        repair_limit = int(cfg.get("metadata_repair_max_entries", 100) or 100)
    except (TypeError, ValueError):
        repair_limit = 100
    repair_limit = max(max_entries, min(300, max(1, repair_limit)))
    link_cache = _repair_cached_feed_origins(
        valid_entries[:repair_limit], target_url, cfg
    )
    entries = valid_entries[:max_entries]
    stats["total"] = len(entries)

    few_shot_context = str(cfg.get("_few_shot_context") or "")

    scorer, similarity_identity = Scorer(), _SimilarityIdentitySnapshot()
    # 确保 scorer 已初始化
    if not scorer.backends:
        scorer._init_backends()

    # Qwen 7B 单后端 + 3s rate_limit，实际串行，去掉线程开销
    notify_threshold = DB.get_feed_thresholds(cfg, target_url)["notify"]

    # Count real outbound attempts at the transport boundary instead of
    # inferring them from the final scoring reason. This includes filtered
    # articles, stage-2 review, summaries, and backend failover while excluding
    # local/runtime-cache paths. ContextVar isolation keeps parallel feeds apart.
    with capture_llm_calls() as llm_calls:
        for e in entries:
            try:
                result_entry, reason = process_entry(
                    e,
                    recent_entries,
                    prompt_text,
                    cfg,
                    scorer,
                    few_shot_context=few_shot_context,
                    link_cache=link_cache,
                    feed_url=target_url, persist_deferred=persist_deferred,
                    notify_threshold=notify_threshold, similarity_identity=similarity_identity,
                )
            except EventCoreConflictError as exc:
                logger.info(
                    "Frozen score-path conflict for %s: %s",
                    str(getattr(e, "link", "") or "")[:160],
                    type(exc).__name__,
                )
                continue
            except Exception as exc:
                stats["entry_errors"] += 1
                error_msg = f"{type(exc).__name__}: {exc}"[:160]
                logger.exception("Entry processing failed")
                continue

            if unfinished := _unfinished_score_stat(reason):
                stats[unfinished] += 1
            elif reason == "blacklist":
                stats["blacklist_hits"] += 1
            elif reason == "cache":
                stats["cache_hits"] += 1
                passed_entries.append(result_entry)
                scores.append(0)
            elif reason in ("similarity", "scored", "local_fast", "fast_pass"):
                if reason == "similarity":
                    stats["similarity_hits"] += 1
                    # 相似度命中的文章不输出到 RSS（避免重复噪音）
                    continue
                if reason == "fast_pass":
                    stats["fast_pass_hits"] += 1
                passed_entries.append(result_entry)
                s = extract_score_from_title(result_entry.title)
                scores.append(s)
    stats["api_calls"] = llm_calls.total

    # \u6700\u7ec8\u53bb\u91cd\uff1a\u6309\u539f\u59cb\u6807\u9898\u53bb\u91cd\uff0c\u4fdd\u7559 score \u6700\u9ad8\u7684\uff08\u9632\u6b62\u591a\u6e90\u5e76\u53d1\u5904\u7406\u65f6\u7684\u8fb9\u754c\u6761\u4ef6\u7ade\u6001\uff09
    if entries and stats["entry_errors"] == len(entries):
        duration_ms = int((time.time() - start_time) * 1000)
        stats["duration_ms"] = duration_ms
        error_msg = (
            f"条目处理全部失败: {stats['entry_errors']}/{len(entries)}; "
            f"{error_msg or 'unknown error'}"
        )[:300]
        DB.update_feed_health(
            target_url,
            feed_name,
            rss_duration,
            True,
            stats["total"],
            0,
            None,
            error_msg,
        )
        bump_stat(
            DB.update_stats,
            total_requests=1,
        )
        logger.error("Feed processing failed [%s]: %s", feed_name, error_msg)
        feed_title = (feed.feed.get("title") or "").strip() or None
        return [], stats, error_msg, feed_title

    seen_titles = {}
    for e in passed_entries:
        clean = clean_title_for_compare(e.title)
        score = extract_score_from_title(e.title)
        if clean not in seen_titles or score > seen_titles[clean][0]:
            seen_titles[clean] = (score, e)
    passed_entries = [e for _, e in seen_titles.values()]

    stats["passed"] = len(passed_entries)
    duration_ms = int((time.time() - start_time) * 1000)
    stats["duration_ms"] = duration_ms

    avg_score = sum(scores) / len(scores) if scores else None
    health_warning = _feed_health_warning(stats, len(entries), error_msg)
    if health_warning:
        logger.warning("Feed processing degraded [%s]: %s", feed_name, health_warning)
    # Keep usable entries without disguising a source gap or pending score as a
    # complete refresh. Downstream consumers must also inspect completeness stats.
    error_msg = None
    DB.update_feed_health(
        target_url, feed_name, rss_duration, not stats["source_unrecognized"],
        stats["total"], stats["passed"], avg_score, health_warning
    )

    bump_stat(
        DB.update_stats,
        total_requests=1,
        cache_hits=stats["cache_hits"],
        similarity_hits=stats["similarity_hits"],
        blacklist_hits=stats["blacklist_hits"],
        fast_pass_hits=stats["fast_pass_hits"],
        filtered_count=max(
            0, stats["total"] - stats["passed"] - stats["entry_errors"]
            - stats["score_deferred"] - stats["score_terminal_unscored"]
        ),
        passed_count=stats["passed"]
    )

    feed_title = (feed.feed.get("title") or "").strip() or None
    return passed_entries, stats, error_msg, feed_title


def build_json_feed(
    passed_entries: list,
    channel_title: str,
    target_url: str,
    cfg: dict,
    *,
    channel_description: str | None = None,
) -> str:
    import json
    items = []
    for entry in passed_entries:
        event_id, event_version = _entry_event_identity(entry)
        meta = getattr(entry, "scoring_meta", {}) or {}
        structured_score = meta.get("score") if isinstance(meta, dict) else None
        try:
            if isinstance(structured_score, bool):
                raise ValueError("boolean is not a score")
            score = float(structured_score)
            if not math.isfinite(score):
                raise ValueError("score must be finite")
        except (TypeError, ValueError):
            score = extract_score_from_title(getattr(entry, "title", ""))
        items.append({
            "title": getattr(entry, "title", ""),
            "link": getattr(entry, "link", ""),
            "summary": getattr(entry, "ai_summary", ""),
            "score": score,
            "event_id": event_id or None,
            "event_version": event_version,
        })
    payload = {
        "title": channel_title,
        "link": target_url,
        "strategy_version": str(cfg.get("scoring_strategy_version") or ""),
        "items": items,
    }
    if channel_description is not None:
        payload["description"] = str(channel_description)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_html_feed(
    passed_entries: list,
    channel_title: str,
    target_url: str,
    cfg: dict,
    *,
    channel_description: str | None = None,
) -> str:
    cards = []
    for entry in passed_entries:
        title = escape_html(getattr(entry, "title", ""))
        link = escape_html(safe_http_url(getattr(entry, "link", "")))
        summary = escape_html(getattr(entry, "ai_summary", ""))
        cards.append(
            f'<div style="background:#151525;border-left:4px solid #f0c040;border-radius:10px;padding:14px;margin:10px 0;">'
            f'<a href="{link}" target="_blank" rel="noopener noreferrer" style="color:#f0c040;font-weight:700;text-decoration:none;">{title}</a>'
            f'<div style="color:#cbd5e1;font-size:13px;line-height:1.6;margin-top:8px;">{summary}</div></div>'
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>' + escape_html(channel_title) + '</title></head>'
        '<body style="background:#0f0f1a;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:860px;margin:0 auto;padding:24px;">'
        f'<h1 style="color:#f0c040;">{escape_html(channel_title)}</h1>'
        f'<div style="color:#94a3b8;font-size:12px;margin-bottom:18px;">{escape_html(target_url)} · 策略 {escape_html(cfg.get("scoring_strategy_version") or "")}</div>'
        + (
            '<div style="color:#cbd5e1;background:#151525;border:1px solid #293247;border-radius:10px;padding:10px 12px;margin-bottom:18px;font-size:12px;line-height:1.6;">'
            + escape_html(channel_description)
            + '</div>'
            if channel_description is not None
            else ''
        )
        + ("".join(cards) if cards else '<div style="color:#94a3b8;">暂无情报</div>') + '</body></html>'
    )


def build_rss_xml(
    passed_entries: list,
    channel_title: str,
    target_url: str,
    request_url: str,
    cfg: dict,
    *,
    channel_description: str | None = None,
) -> str:
    """构建标准 RSS 2.0 XML"""
    description = (
        "AI Filtered RSS Feed for A-Share Macro Intelligence"
        if channel_description is None
        else str(channel_description)
    )
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        '<channel>',
        f'<title>{escape_xml(channel_title)}</title>',
        f'<link>{escape_xml(safe_http_url(target_url, fallback=""))}</link>',
        f'<description>{escape_xml(description)}</description>',
        '<language>zh-CN</language>',
        f'<lastBuildDate>{rfc822_date()}</lastBuildDate>',
        f'<atom:link href="{escape_xml(safe_http_url(request_url, fallback=""))}" rel="self" type="application/rss+xml"/>'
    ]

    for entry in passed_entries:
        pub_date = ""
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            pub_date = f'<pubDate>{rfc822_date(calendar.timegm(entry.published_parsed))}</pubDate>'
        elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
            pub_date = f'<pubDate>{rfc822_date(calendar.timegm(entry.updated_parsed))}</pubDate>'

        # 使用预编译正则提取标签（替代 re.search）
        tag = extract_tag_from_title(entry.title) or ""
        if "｜" in tag:
            # Compact dashboard titles begin with "📈 8.5｜..."; the score and
            # headline are presentation, not an RSS category.
            tag = ""
        tag_xml = f'<category><![CDATA[{escape_cdata(tag)}]]></category>' if tag else ""

        title_text = escape_xml(getattr(entry, "title", ""))
        description_value = getattr(entry, "ai_summary", "")
        if not description_value and hasattr(entry, "get"):
            description_value = entry.get("summary", "")
        description = escape_xml(description_value or "")
        entry_url = safe_http_url(getattr(entry, "link", ""), fallback="")

        xml_parts.extend([
            '<item>',
            f'<title>{title_text}</title>',
            f'<link>{escape_xml(entry_url)}</link>',
            f'<guid isPermaLink="{"true" if entry_url else "false"}">{escape_xml(entry_url)}</guid>',
            f'<description>{description}</description>',
        ])
        if pub_date:
            xml_parts.append(pub_date)
        if tag_xml:
            xml_parts.append(tag_xml)
        xml_parts.append('</item>')

    xml_parts.extend(['</channel>', '</rss>'])
    return "\n".join(xml_parts)
