"""Authenticated, server-rendered Event Radar page."""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, redirect, render_template, request, url_for

from core.digest_engine import article_source_key
from routes.browser_auth import (
    _safe_next,
    apply_browser_security_headers,
    browser_error,
    dashboard_login_redirect,
    get_dashboard_auth,
    request_prefers_html,
)
from routes.security import rate_limit
from routes.shared import _cfg
from services.event_radar_query import (
    EventRadarNotFoundError,
    EventRadarQueryError,
    EventRadarUnavailableError,
    load_event_stream,
    load_event_radar,
)
from services.event_daily_report import load_daily_report
from services.event_home_digest import load_home_event_digest
from services.event_weekly_report import load_weekly_report
from services.release_info import release_version
from utils.report_paging import parse_report_paging, parse_weekly_paging


logger = logging.getLogger(__name__)
event_radar_bp = Blueprint("event_radar", __name__)
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_ALLOWED_QUERY_KEYS = frozenset(
    {"event_id", "cursor", "limit", "tab", "has_stocks"}
)
_CARD_ALLOWED_QUERY_KEYS = frozenset({"limit"})
_DAILY_ALLOWED_QUERY_KEYS = frozenset({"date", "block", "page"})
_WEEKLY_ALLOWED_QUERY_KEYS = frozenset({"week", "block", "page"})
_CARD_DEFAULT_LIMIT = 8
_CARD_MAX_LIMIT = 30
# 主页摘要卡只承担“高价值事件 -> 有原文证据的非概念个股映射”速览。低分事件、
# 海外无关消息和纯概念池映射仍保留在登录后的完整雷达页。
_CARD_MIN_PRIORITY = 6.0
_CARD_EXCLUDED_EVENT_TYPES = ("海外无关消息",)
_CARD_EVIDENCE_RELATIONS = frozenset(
    {"direct", "direct_impact", "affected", "upstream", "downstream", "ecosystem"}
)
_DASHBOARD_ORIGIN = "https://ai.example.invalid"
_SOURCE_LABELS = {
    "gelonghui": "格隆汇",
    "wallstreetcn": "华尔街见闻",
    "cls": "财联社",
    "eastmoney": "东方财富",
    "10jqka": "同花顺",
    "jin10": "金十数据",
    "caixin": "财新",
}
_PROVENANCE_SHADOW_COUNT_FIELDS = (
    "raw_evidence_count",
    "projected_evidence_count",
    "publisher_group_count",
    "lineage_unit_count",
    "first_party_evidence_count",
    "official_record_evidence_count",
    "claim_count",
)
_PROVENANCE_CLAIM_STATE_LABELS = (
    ("single_support", "单方支持"),
    ("multi_independent_support", "规则判定多发布方支持"),
    ("contested", "支持与反驳并存"),
    ("contradicted_only", "仅反驳"),
    ("context_only", "仅背景"),
)


def _auth_error() -> Response:
    return browser_error("Unauthorized", 401)


def _query_values() -> dict[str, str]:
    unknown = set(request.args) - _ALLOWED_QUERY_KEYS
    if unknown:
        raise EventRadarQueryError("unsupported query parameter")
    values: dict[str, str] = {}
    for key in _ALLOWED_QUERY_KEYS:
        supplied = request.args.getlist(key)
        if len(supplied) > 1:
            raise EventRadarQueryError("duplicate query parameter")
        values[key] = supplied[0] if supplied else ""
    return values


def _card_limit() -> int:
    unknown = set(request.args) - _CARD_ALLOWED_QUERY_KEYS
    supplied = request.args.getlist("limit")
    if unknown or len(supplied) > 1:
        raise EventRadarQueryError("invalid event radar card query")
    raw = supplied[0].strip() if supplied else ""
    if not raw:
        return _CARD_DEFAULT_LIMIT
    if not re.fullmatch(r"[1-9][0-9]?", raw):
        raise EventRadarQueryError("card limit must be an integer")
    parsed = int(raw)
    if parsed > _CARD_MAX_LIMIT:
        raise EventRadarQueryError("card limit is out of range")
    return parsed


def _time_view(value: object) -> dict[str, str]:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return {"label": "时间未知", "iso": ""}
    try:
        current = datetime.fromtimestamp(timestamp, _MARKET_TZ)
    except (OSError, OverflowError, ValueError):
        return {"label": "时间未知", "iso": ""}
    return {
        "label": current.strftime("%Y-%m-%d %H:%M"),
        "iso": current.isoformat(),
    }


def _event_view(raw: dict[str, Any]) -> dict[str, Any]:
    event = dict(raw)
    event_type = str(event.get("event_type") or "").strip()
    event["event_type_raw"] = event_type
    if (
        not event_type
        or "�" in event_type
        or event_type.casefold() in {"unknown", "none", "null", "未知"}
        or re.fullmatch(r"-?\d+(?:\.\d+)?", event_type)
        or event_type in {"囸级政策、国家级政策"}
    ):
        event["event_type"] = "其他事件"
    occurred = _time_view(event.get("occurred_at"))
    event["occurred_at_label"] = occurred["label"]
    event["occurred_at_iso"] = occurred["iso"]
    available = _time_view(event.get("available_at"))
    event["available_at_label"] = available["label"]
    event["available_at_iso"] = available["iso"]
    try:
        importance = float(
            event.get("importance_score")
            if event.get("importance_score") is not None
            else event.get("priority") or 0
        )
        event["importance_score"] = importance
        event["priority_label"] = f"{importance:.1f}"
    except (TypeError, ValueError):
        event["priority_label"] = "0.0"
    return event


def _feed_name_map() -> dict[str, str]:
    result: dict[str, str] = {}
    for feed in (_cfg().get("feeds") or []):
        if not isinstance(feed, dict):
            continue
        url = str(feed.get("url") or "").strip()
        name = str(feed.get("name") or "").strip()
        if url and name:
            result[url] = name[:255]
    return result


def _source_label(source: dict[str, Any], feed_names: dict[str, str]) -> str:
    configured = str(source.get("source_name") or "").strip()
    if configured in feed_names:
        return feed_names[configured]
    link = str(source.get("url") or "").strip()
    key = article_source_key({"feed_url": configured, "link": link})
    if key in _SOURCE_LABELS:
        return _SOURCE_LABELS[key]
    if key.startswith("rss-route:"):
        route = key.partition(":")[2].strip("/")
        return (route.rsplit("/", 1)[-1] or "RSS 来源")[:80]
    if key and "://" not in key:
        return key[:80]
    for candidate in (link, configured):
        try:
            host = (urlsplit(candidate).hostname or "").removeprefix("www.")
        except ValueError:
            host = ""
        if host:
            return host[:80]
    return "来源"


def _source_view(raw: dict[str, Any], feed_names: dict[str, str]) -> dict[str, Any]:
    source = dict(raw)
    source["publisher"] = _source_label(source, feed_names)
    source["safe_url"] = str(source.get("url") or "")
    observed = _time_view(source.get("announcement_at") or source.get("observed_at"))
    source["announcement_at_label"] = observed["label"]
    source["announcement_at_iso"] = observed["iso"]
    source_confidence = source.get("source_confidence", source.get("reliability"))
    try:
        parsed_confidence = float(source_confidence)
        source["source_confidence_display"] = (
            f"{parsed_confidence * 100:.0f}%（规则先验）"
            if math.isfinite(parsed_confidence)
            and source.get("source_confidence_basis") == "rule_prior"
            else f"{parsed_confidence * 100:.0f}%"
            if math.isfinite(parsed_confidence)
            else "未知"
        )
    except (TypeError, ValueError):
        source["source_confidence_display"] = "未知"
    observations = []
    for raw_observation in source.get("observations", []):
        observation = dict(raw_observation)
        observed = _time_view(observation.get("observed_at"))
        observation["observed_at_label"] = observed["label"]
        observation["observed_at_iso"] = observed["iso"]
        observation["stance_label"] = {
            "supports": "支持记录", "contradicts": "否认记录", "context": "背景记录",
        }.get(observation.get("stance"), "立场未记录")
        observations.append(observation)
    source["observations"] = observations
    return source


def _provenance_shadow_view(raw: object) -> dict[str, Any]:
    """Translate only a complete aggregate projection into fixed UI fields."""
    pending = {"status": "pending", "ready": False}
    if not isinstance(raw, dict) or raw.get("status") != "ready":
        return pending
    counts: dict[str, int] = {}
    for field in _PROVENANCE_SHADOW_COUNT_FIELDS:
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return pending
        counts[field] = value
    state_counts = raw.get("claim_state_counts")
    expected_states = {key for key, _label in _PROVENANCE_CLAIM_STATE_LABELS}
    if not isinstance(state_counts, dict) or set(state_counts) != expected_states:
        return pending
    claim_states = []
    for key, label in _PROVENANCE_CLAIM_STATE_LABELS:
        value = state_counts.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return pending
        claim_states.append({"state": key, "label": label, "count": value})
    if sum(item["count"] for item in claim_states) != counts["claim_count"]:
        return pending
    latest = _time_view(raw.get("latest_evidence_at"))
    computed = _time_view(raw.get("computed_at"))
    if not computed["iso"]:
        return pending
    return {
        "status": "ready",
        "ready": True,
        **{
            field: value
            for field, value in counts.items()
            if field != "first_party_evidence_count"
        },
        # The current projector does not expose an audited declaration that
        # subject binding/classification is complete.  A numeric zero would be
        # mistaken for verified absence, so presentation remains unclassified.
        "first_party_state": "unclassified",
        "claim_states": claim_states,
        "latest_evidence_at_label": latest["label"],
        "latest_evidence_at_iso": latest["iso"],
        "computed_at_label": computed["label"],
        "computed_at_iso": computed["iso"],
    }


def _signed_number(value: object, *, scale: float = 1.0, suffix: str = "") -> str | None:
    try:
        parsed = float(value) * scale
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    rendered = f"{parsed:+.2f}".replace("-", "−")
    return rendered + suffix


def _percent(value: object, digits: int = 0) -> str | None:
    try:
        parsed = float(value) * 100.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return f"{parsed:.{digits}f}%"


def _equity_views(
    stocks: list[dict[str, Any]], feed_names: dict[str, str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in stocks[:3]:
        stock = dict(raw)
        exchange = str(stock.get("exchange") or "").upper()
        ticker = str(stock.get("ticker") or "")
        stock["history_url"] = (
            url_for(
                "security_history.security_history_page",
                exchange=exchange,
                ticker=ticker,
            )
            if exchange in {"SSE", "SZSE", "BSE"}
            and re.fullmatch(r"[0-9]{6}", ticker)
            else ""
        )
        relations: list[dict[str, Any]] = []
        for raw_relation in raw.get("relations") or []:
            relation = dict(raw_relation)
            expected_gap = relation.get("expected_gap")
            relation["expected_gap_display"] = _signed_number(expected_gap)
            try:
                parsed_gap = float(expected_gap)
            except (TypeError, ValueError):
                parsed_gap = None
            relation["expected_gap_class"] = (
                "positive" if parsed_gap is not None and parsed_gap > 0
                else "negative" if parsed_gap is not None and parsed_gap < 0
                else "neutral" if parsed_gap is not None
                else "unknown"
            )
            mapping_confidence = relation.get(
                "mapping_confidence", relation.get("confidence")
            )
            try:
                parsed_mapping_confidence = float(mapping_confidence)
            except (TypeError, ValueError):
                parsed_mapping_confidence = None
            relation["mapping_confidence_display"] = (
                "未评估"
                if parsed_mapping_confidence == 0
                else _percent(parsed_mapping_confidence)
            )
            relation["evidence"] = [
                _source_view(dict(item), feed_names)
                for item in (raw_relation.get("evidence") or [])
            ]
            relations.append(relation)
        stock["relations"] = relations
        result.append(stock)
    return result


def _outcome_views(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in outcomes:
        item = dict(raw)
        security = raw.get("security") or {}
        item["security_name"] = security.get("name") or "市场代理"
        item["security_id"] = security.get("security_id") or raw.get("security_id")
        item["exchange"] = security.get("exchange") or ""
        item["ticker"] = security.get("ticker") or ""
        signal_available_at = raw.get("signal_available_at")
        item["signal_available_at_label"] = (
            _time_view(signal_available_at)["label"]
            if signal_available_at is not None
            else "未知（旧记录未冻结）"
        )
        item["baseline_at_label"] = _time_view(raw.get("baseline_at"))["label"]
        item["tradable_baseline_at_label"] = _time_view(
            raw.get("tradable_baseline_at")
        )["label"]
        item["measured_at_label"] = _time_view(raw.get("measured_at"))["label"]
        item["gross_return_display"] = _signed_number(
            raw.get("gross_return"), scale=100.0, suffix="%"
        )
        benchmark = raw.get("benchmark") or {}
        is_market_standard = bool(benchmark.get("is_market_standard"))
        item["csi300_return_display"] = (
            _signed_number(raw.get("market_benchmark_return"), scale=100.0, suffix="%")
            if is_market_standard else None
        )
        item["csi300_excess_return_display"] = (
            _signed_number(raw.get("market_excess_return"), scale=100.0, suffix="%")
            if is_market_standard else None
        )
        item["legacy_benchmark_name"] = benchmark.get("name") or "旧方法基准"
        item["legacy_benchmark_return_display"] = (
            None
            if is_market_standard
            else _signed_number(raw.get("benchmark_return"), scale=100.0, suffix="%")
        )
        item["legacy_excess_return_display"] = (
            None
            if is_market_standard
            else _signed_number(raw.get("excess_return"), scale=100.0, suffix="%")
        )
        item["industry_return_display"] = None
        item["industry_excess_return_display"] = None
        item["direction_hit_label"] = (
            "命中" if raw.get("direction_hit") is True
            else "未命中" if raw.get("direction_hit") is False
            else "不适用"
        )
        try:
            baseline_price = float(raw.get("baseline_price"))
            item["baseline_price_display"] = (
                f"{baseline_price:.4f}" if math.isfinite(baseline_price) else None
            )
        except (TypeError, ValueError):
            item["baseline_price_display"] = None
        try:
            tradable_price = float(raw.get("tradable_baseline_price"))
            item["tradable_baseline_price_display"] = (
                f"{tradable_price:.4f}" if math.isfinite(tradable_price) else None
            )
        except (TypeError, ValueError):
            item["tradable_baseline_price_display"] = None
        item["mfe_display"] = _signed_number(raw.get("mfe"), scale=100.0, suffix="%")
        item["mae_display"] = _signed_number(raw.get("mae"), scale=100.0, suffix="%")
        item["data_quality_display"] = _percent(raw.get("data_quality"))
        item["data_status_label"] = {
            "complete_close_only": "收盘字段完整",
            "complete_pending_materialization": "收盘字段完整，结果待物化",
            "not_due": "尚未到期",
            "missing_asset_close": "缺少个股/证券收盘价",
            "missing_benchmark_close": "缺少沪深300收盘价",
            "legacy_unknown": "旧记录状态未知",
        }.get(str(raw.get("data_status") or ""), "状态未知")
        item["tradability_status_label"] = {
            "trading_ohlc": "有完整日线，开盘基准可用",
            "limit_price_close": "收于涨跌停价，成交可达性不确定",
            "suspended": "停牌，不可交易",
            "missing_open": "缺少开盘价",
            "unknown_historical": "历史状态未知",
            "unknown": "状态未知",
        }.get(str(raw.get("tradability_status") or ""), "状态未知")
        item["suspension_status_label"] = {
            "trading": "正常交易",
            "suspended": "停牌",
            "unknown": "未知",
        }.get(str(raw.get("suspension_status") or ""), "未知")
        item["limit_status_label"] = {
            "not_at_limit": "未收于涨跌停",
            "closed_limit_up": "收于涨停价",
            "closed_limit_down": "收于跌停价",
            "not_applicable": "停牌，不适用",
            "unknown_historical": "历史涨跌停状态未知",
            "unknown": "未知",
        }.get(str(raw.get("limit_status") or ""), "未知")
        low = _percent(raw.get("confidence_interval_low"), 1)
        high = _percent(raw.get("confidence_interval_high"), 1)
        item["confidence_interval_display"] = (
            f"{low}–{high}" if low is not None and high is not None else "样本不足"
        )
        item["direction_hit_rate_display"] = _percent(
            raw.get("direction_hit_rate"), 1
        )
        mapping_current = raw.get("mapping_current") is not False
        item["mapping_current"] = mapping_current
        item["method_label"] = (
            "冻结信号结果 · 映射已退役，仅供审计"
            if not mapping_current
            else "首个信号后收盘 · 沪深300基准 · 结果待物化"
            if raw.get("method_supported") and not raw.get("materialized", True)
            else "首个信号后收盘 · 沪深300基准"
            if raw.get("method_supported")
            else "旧版或无效契约（不满足当前统计标准）"
        )
        item["statistics_available"] = bool(
            mapping_current
            and raw.get("materialized", True)
            and raw.get("method_supported")
        )
        result.append(item)
    return result


def _similarity_views(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        event = raw.get("event") or {}
        item.update(
            {
                "title": event.get("title") or "未命名事件",
                "event_type": event.get("event_type") or "其他事件",
                "direction": event.get("direction") or "中性",
                "priority": event.get("priority"),
                "occurred_at": event.get("occurred_at"),
            }
        )
        occurred = _time_view(item.get("occurred_at"))
        item["occurred_at_label"] = occurred["label"]
        item["method_label"] = (
            "标题候选 · 未据此合并"
            if item.get("method") == "title_candidate"
            else item.get("method")
        )
        result.append(item)
    return result


def _investment_horizon_views(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group existing observations without inferring trades or combining returns."""
    views = [
        {
            "key": "short",
            "label": "短线关注",
            "period": "几个交易日",
            "horizons": ("T+1", "T+3", "T+5"),
            "question": "这条消息是否构成近期催化，发现时还有没有参与空间？",
            "checks": (
                "核对原文中的新增事实、实际可用时间，以及是否只是旧闻重复。",
                "结合该股行情核对消息前后的价格反应和停牌、涨跌停等参与限制。",
            ),
            "withdraw_if": "原始消息被撤回或更正、个股影响路径被证伪，或催化窗口已过时，重新评估关注理由。",
        },
        {
            "key": "swing",
            "label": "波段关注",
            "period": "几周",
            "horizons": ("T+20",),
            "question": "这条消息对应的经营或产业逻辑，是否有持续验证的依据？",
            "checks": (
                "核对原文是否有订单、业绩、政策落地等可验证信息；没有就保持待验证。",
                "沿相关股票的事件历史查找后续进展与反证，不把转载次数当作逻辑兑现。",
            ),
            "withdraw_if": "关键事实被更正、落地进度未满足事先记录的条件，或后续证据推翻传导路径时，重新评估关注理由。",
        },
    ]
    known_horizons = {horizon for view in views for horizon in view["horizons"]}
    for view in views:
        view["outcomes"] = [
            item for item in outcomes if item.get("horizon") in view["horizons"]
        ]
    other = [item for item in outcomes if item.get("horizon") not in known_horizons]
    if other:
        views.append({
            "key": "other",
            "label": "其他期限 / 历史记录",
            "period": "按原记录保留，不并入双周期结论",
            "horizons": (),
            "outcomes": other,
        })
    return views


def _news_kind_view(detail: dict[str, Any]) -> dict[str, str]:
    """Describe a source-bound headline, never infer novelty or future returns."""
    views = {
        "catalyst_candidate": (
            "新催化候选（首发未核验）",
            "标题含可核对的经营事项；仍需核验原文、首次披露时间和个股影响，不代表利好或可交易。",
        ),
        "price_recap": (
            "已涨跌复述",
            "标题描述已经发生的价格表现，不代表后续还会同向涨跌，也不证明消息已被价格消化。",
        ),
        "mixed": (
            "催化与行情混合",
            "标题同时提及经营事项和价格表现，两部分需分开核对；首发与先后因果均未核验。",
        ),
        "background": (
            "政策 / 宏观 / 观点背景",
            "背景或观点不能直接当作具体公司的已确认催化，个股传导仍待核对。",
        ),
        "unknown": (
            "信息不足，消息类型未确认",
            "缺少匹配的支持性原文标题，或标题存在预测、否认、旧闻等歧义；不从摘要或收益补造依据。",
        ),
    }
    title = str((detail.get("event") or {}).get("title") or "").strip()
    sources = detail.get("sources") or []
    bound = any(
        str(source.get("claim_text") or "").strip() == title
        and source.get("stance") == "supports"
        and source.get("evidence_id")
        and source.get("url")
        for source in sources
    )
    key = "unknown"
    if (
        title
        and bound
        and not detail.get("sources_truncated")
        and not any(
            source.get("stance") in {"contradicts", "refutes"}
            for source in sources
        )
    ):
        uncertain = re.search(
            r"传闻|据传|网传|有望|预计|预测|可能|或将|拟|计划|将于|有意|如果|"
            r"是否|尚未|未获|未签|未能|不再|不会|"
            r"(?:未|没有)(?:曾|实际|正式)?(?:获得|中标|取得|收到|签署|签订|投产|开工|获批)|"
            r"(?:消息|报道|传言)(?:严重|完全)?(?:不实|失实)|没有此事|"
            r"否认|辟谣|澄清|不属实|取消|终止|复盘|回顾|旧闻|去年曾|"
            r"曾(?:经)?(?:获|中标|签|投产)",
            title,
        )
        background = re.search(
            r"央行|国务院|财政部|国家统计局|宏观|货币政策|财政政策|降准|降息|"
            r"展望后市|研报|认为|建议|看好|市场观点|政策解读|新规(?!格|划)|条例|监管办法",
            title,
        )
        catalyst = re.search(
            r"(?:获得|获|签署|签订|中标|取得|收到).{0,40}(?:订单|合同|批复|许可)|"
            r"(?:公告|披露).{0,20}(?:业绩|财报|净利润|回购|收购|增持|减持|分红)|"
            r"发布(?:业绩预告|财报)|(?:项目|产线|基地).{0,8}(?:投产|开工)|"
            r"(?:产品|药物|新药).{0,8}获批",
            title,
        )
        price = any(
            re.search(
                r"涨停|跌停|股价.{0,12}(?:上涨|下跌|大涨|大跌|新高|新低)",
                clause,
            )
            or (
                not re.search(
                    r"营收|利润|收入|产量|销量|订单|业绩|产品|价格|铜价|锂价|售价|运价|"
                    r"ROE|ROA|ROIC|EPS|净资产收益率|资产收益率|毛利率|净利率|现金流|"
                    r"期铜|期铝|期锌|期镍|期锡|期铅|原油|黄金期货|白银期货|"
                    r"现货黄金|现货白银|金价|银价|油价",
                    clause,
                    re.IGNORECASE,
                )
                and re.search(
                    r"[涨跌](?:超|逾|近)?\d+(?:\.\d+)?[%％]|"
                    r"(?:创|刷新)(?:历史|年内|阶段|近期)新(?:高|低)|"
                    r"(?:板块|股指|沪指|深成指|创业板指).{0,12}"
                    r"(?:上涨|下跌|走强|走弱|拉升|回落|反弹|跳水)",
                    clause,
                )
            )
            # Keep colon-qualified figures with their financial/product subject.
            for clause in re.split(r"[，。；!?！？]", title)
        )
        if background:
            key = "background"
        elif not uncertain:
            key = (
                "mixed" if catalyst and price
                else "catalyst_candidate" if catalyst
                else "price_recap" if price
                else "unknown"
            )
    label, note = views[key]
    return {"key": key, "label": label, "note": note}


def _render_model(model: dict[str, Any], auth: Any) -> Response:
    events = [_event_view(item) for item in model["events"]]
    detail = model.get("detail") or {}
    selected = None
    sources: list[dict[str, Any]] = []
    equities: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    similarities: list[dict[str, Any]] = []
    market_references: list[dict[str, Any]] = []
    mentions_unspecified_etf = False
    provenance_shadow = _provenance_shadow_view(detail.get("provenance_shadow"))
    feed_names = _feed_name_map()
    if detail:
        selected = _event_view(detail["event"])
        selected["summary"] = detail.get("summary") or selected.get("title")
        selected["source_count"] = int(detail.get("source_count") or 0)
        selected["group_size"] = int(detail.get("group_size") or 1)
        sources = [_source_view(item, feed_names) for item in detail.get("sources") or []]
        equities = _equity_views(detail.get("stocks") or [], feed_names)
        market_references = [
            dict(item) for item in (detail.get("market_references") or [])
        ]
        mentions_unspecified_etf = bool(detail.get("mentions_unspecified_etf"))
        outcomes = _outcome_views(detail.get("outcomes") or [])
        similarities = _similarity_views(detail.get("similar_events") or [])

    pagination = model["pagination"]
    has_stocks = bool((model.get("filters") or {}).get("has_stocks"))
    active_tab = "reaction" if model["tab"] == "market" else model["tab"]
    read_at = _time_view(model.get("read_at"))
    rendered = render_template(
        "event_radar.html",
        events=events,
        selected=selected,
        active_tab=active_tab,
        sources=sources,
        equities=equities,
        market_references=market_references,
        mentions_unspecified_etf=mentions_unspecified_etf,
        outcomes=outcomes,
        investment_views=_investment_horizon_views(outcomes),
        news_kind=_news_kind_view(detail),
        similarities=similarities,
        provenance_shadow=provenance_shadow,
        sources_truncated=bool(detail.get("sources_truncated")),
        outcomes_truncated=bool(detail.get("outcomes_truncated")),
        similarities_truncated=bool(detail.get("similarities_truncated")),
        page_limit=int(pagination.get("limit") or 30),
        next_cursor=pagination.get("next_cursor") or "",
        current_cursor=pagination.get("cursor") or "",
        has_stocks=has_stocks,
        read_at=read_at,
        auth=auth,
        release_version=release_version(),
    )
    response = apply_browser_security_headers(
        Response(rendered, mimetype="text/html")
    )
    return response


def _event_radar_card_placeholder() -> str:
    return (
        '<div style="padding:22px;text-align:center;border:1px dashed '
        'rgba(148,163,184,.35);border-radius:14px;color:'
        'var(--color-text-subdue,inherit);font-size:12px;">'
        '事件摘要暂不可用，请稍后重试。</div>'
    )


def _event_radar_card_items(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose source-bound mapping chips; never label them as posterior proof."""
    events: list[dict[str, Any]] = []
    for raw in raw_events:
        event = _event_view(raw)
        stocks = [
            stock
            for stock in (event.get("stocks") or [])
            if str(stock.get("relation_type") or "").casefold()
            in _CARD_EVIDENCE_RELATIONS
        ]
        if not stocks:
            continue
        event["stocks"] = stocks
        event["has_stocks"] = True
        events.append(event)
    return events


def _home_event_changes_response() -> Response:
    """Worker/live projection; production Web returns its artifact before here."""
    from services.ingestion_status import read_refresh_coverage

    try:
        digest = load_home_event_digest()
    except EventRadarUnavailableError:
        logger.warning("Home event changes are unavailable", exc_info=True)
        response = Response(_event_radar_card_placeholder(), status=503, mimetype="text/html")
        response.headers.update({
            "Widget-Title": "Event Changes", "Widget-Content-Type": "html",
            "Cache-Control": "no-store", "Retry-After": "30",
            "X-Event-Radar-State": "unavailable",
        })
        return response
    coverage_state, coverage_text = read_refresh_coverage()
    rendered = render_template(
        "event_home_digest.html", digest=digest, dashboard_origin=_DASHBOARD_ORIGIN,
        coverage_state=coverage_state, coverage_text=coverage_text,
    )
    response = Response(rendered, mimetype="text/html")
    response.headers.update({
        "Widget-Title": "Event Changes", "Widget-Content-Type": "html",
        "Cache-Control": "public, max-age=10",
        "X-Card-Ingestion-State": coverage_state,
    })
    return response


@event_radar_bp.route("/event-radar-card", methods=["GET"])
@rate_limit(max_requests=120, window_seconds=60)
def event_radar_card() -> Response:
    """Public, summary-only Glance card backed by the stable event core."""
    try:
        limit = _card_limit()
    except EventRadarQueryError:
        return browser_error("Invalid event radar card query", 400)

    if (
        set(request.args) == {"limit"}
        and request.args.getlist("limit") == ["8"]
        and limit == 8
    ):
        from routes.dashboard_routes import _dashboard_snapshot_response

        snapshot_response = _dashboard_snapshot_response(
            "event_radar",
            title="Event Changes",
            cache_seconds=60,
        )
        if snapshot_response is not None:
            return snapshot_response
        return _home_event_changes_response()

    try:
        model = load_event_stream(
            limit=limit,
            has_stocks=True,
            min_priority=_CARD_MIN_PRIORITY,
            require_non_concept_stock=True,
            excluded_event_types=_CARD_EXCLUDED_EVENT_TYPES,
        )
    except EventRadarUnavailableError:
        logger.warning("Event Radar card is unavailable", exc_info=True)
        response = Response(
            _event_radar_card_placeholder(),
            status=503,
            mimetype="text/html",
        )
        response.headers["Widget-Title"] = "Event Radar"
        response.headers["Widget-Content-Type"] = "html"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = "30"
        response.headers["X-Event-Radar-State"] = "unavailable"
        return response

    raw_events = model.get("events") or []
    events = _event_radar_card_items(raw_events)
    for event in events:
        event["occurred_at_short_label"] = (
            event.get("occurred_at_label", "时间未知")[5:]
            if event.get("occurred_at_iso")
            else "时间未知"
        )
    rendered = render_template(
        "event_radar_card.html",
        events=events,
        has_more=bool((model.get("pagination") or {}).get("has_more")),
        dashboard_origin=_DASHBOARD_ORIGIN,
    )
    response = Response(rendered, mimetype="text/html")
    response.headers["Widget-Title"] = "Event Radar"
    response.headers["Widget-Content-Type"] = "html"
    # Homepage and Glance radar use different URLs/cache entries.  Keep the
    # stale window short so an event revision or canonical merge cannot leave
    # two visible importance scores out of sync for a full dashboard cycle.
    response.headers["Cache-Control"] = "public, max-age=10"
    return response


@event_radar_bp.route("/dashboard/events", methods=["GET"])
@rate_limit(max_requests=180, window_seconds=60)
def event_radar_page() -> Response:
    auth = get_dashboard_auth()
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            # Only carry the selected event and its view through login.  Keep
            # duplicate values intact so the return-target validator rejects
            # ambiguous input instead of silently selecting one value.
            next_args = {
                key: request.args.getlist(key)
                for key in ("event_id", "has_stocks", "tab")
                if key in request.args
            }
            return dashboard_login_redirect(
                next_path=url_for("event_radar.event_radar_page", **next_args)
            )
        return _auth_error()

    try:
        values = _query_values()
        model = load_event_radar(
            event_id=values["event_id"],
            cursor=values["cursor"],
            limit=values["limit"],
            tab=values["tab"],
            has_stocks=values["has_stocks"],
        )
    except EventRadarQueryError:
        return browser_error("Invalid event radar query", 400)
    except EventRadarNotFoundError:
        return browser_error("Event not found", 404)
    except EventRadarUnavailableError:
        logger.warning("Event Radar is unavailable", exc_info=True)
        return browser_error("Event radar unavailable", 503)

    detail = model.get("detail") or {}
    requested = str(detail.get("requested_event_id") or "")
    canonical = str(model.get("selected_event_id") or "")
    if values["event_id"] and requested and canonical and requested != canonical:
        tab = "reaction" if model["tab"] == "market" else model["tab"]
        redirect_args: dict[str, object] = {"event_id": canonical, "tab": tab}
        if model["pagination"].get("cursor"):
            redirect_args["cursor"] = model["pagination"]["cursor"]
        if model["pagination"]["limit"] != 30:
            redirect_args["limit"] = model["pagination"]["limit"]
        if (model.get("filters") or {}).get("has_stocks"):
            redirect_args["has_stocks"] = 1
        target = url_for("event_radar.event_radar_page", **redirect_args)
        return apply_browser_security_headers(redirect(target, code=308))
    return _render_model(model, auth)


def _daily_report_source_labels(report: dict[str, Any]) -> None:
    feed_names = _feed_name_map()
    for block in report.get("blocks") or []:
        for item in block.get("entries") or []:
            for source in item.get("sources") or []:
                source["publisher"] = _source_label(
                    {"source_name": source.get("source_name"), "url": source.get("url")},
                    feed_names,
                )


@event_radar_bp.route("/dashboard/events/daily", methods=["GET"])
@rate_limit(max_requests=60, window_seconds=60)
def event_daily_report_page() -> Response:
    """Deterministic daily change report; no model call and no write."""
    auth = get_dashboard_auth()
    supplied = request.args.getlist("date")
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            next_path = "/dashboard/events/daily"
            if request.args:
                candidate = f"{next_path}?{urlencode(list(request.args.items(multi=True)))}"
                safe_candidate = _safe_next(candidate)
                if safe_candidate != "/dashboard":
                    next_path = safe_candidate
            return dashboard_login_redirect(next_path=next_path)
        return _auth_error()
    if (
        set(request.args) - _DAILY_ALLOWED_QUERY_KEYS
        or any(len(request.args.getlist(key)) > 1 for key in _DAILY_ALLOWED_QUERY_KEYS)
    ):
        return browser_error("Invalid event daily report query", 400)
    try:
        block, page = parse_report_paging(request.args.get("block"), request.args.get("page"))
    except ValueError:
        return browser_error("Invalid event daily report query", 400)
    if block is not None and not supplied:
        return browser_error("Invalid event daily report query", 400)
    try:
        report = load_daily_report(
            supplied[0] if supplied else None, block=block, page=page if block else None,
        )
    except EventRadarQueryError:
        return browser_error("Invalid event daily report query", 400)
    except EventRadarUnavailableError:
        logger.warning("Event daily report is unavailable", exc_info=True)
        return browser_error("Event daily report unavailable", 503)
    _daily_report_source_labels(report)
    rendered = render_template(
        "event_radar_daily.html",
        report=report,
        auth=auth,
        release_version=release_version(),
    )
    return apply_browser_security_headers(Response(rendered, mimetype="text/html"))


@event_radar_bp.route("/dashboard/events/weekly", methods=["GET"])
@rate_limit(max_requests=60, window_seconds=60)
def event_weekly_report_page() -> Response:
    """Read-only weekly reconstruction from existing event evidence."""
    auth = get_dashboard_auth()
    supplied = request.args.getlist("week")
    if auth is None:
        if request.headers.get("X-API-Key") is None and request_prefers_html():
            next_path = "/dashboard/events/weekly"
            if request.args:
                candidate = f"{next_path}?{urlencode(list(request.args.items(multi=True)))}"
                safe_candidate = _safe_next(candidate)
                if safe_candidate != "/dashboard":
                    next_path = safe_candidate
            return dashboard_login_redirect(next_path=next_path)
        return _auth_error()
    if (
        set(request.args) - _WEEKLY_ALLOWED_QUERY_KEYS
        or any(len(request.args.getlist(key)) > 1 for key in _WEEKLY_ALLOWED_QUERY_KEYS)
    ):
        return browser_error("Invalid event weekly report query", 400)
    try:
        block, page = parse_weekly_paging(request.args.get("block"), request.args.get("page"))
    except ValueError:
        return browser_error("Invalid event weekly report query", 400)
    if block is not None and not supplied:
        return browser_error("Invalid event weekly report query", 400)
    try:
        week = supplied[0] if supplied else None
        report = (
            load_weekly_report(week, block=block, page=page)
            if block is not None else load_weekly_report(week)
        )
    except EventRadarQueryError:
        return browser_error("Invalid event weekly report query", 400)
    except EventRadarUnavailableError:
        logger.warning("Event weekly report is unavailable", exc_info=True)
        response = browser_error("Event weekly report unavailable", 503)
        response.headers["Retry-After"] = "5"
        return response
    _daily_report_source_labels(report)
    rendered = render_template(
        "event_radar_weekly.html", report=report, auth=auth,
        release_version=release_version(),
    )
    return apply_browser_security_headers(Response(rendered, mimetype="text/html"))


__all__ = ["event_radar_bp"]
