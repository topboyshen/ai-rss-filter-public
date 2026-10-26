"""Historical association validation for AI score quality.

The long-retained ``backtest_signals`` ledger freezes each score, selection
decision, direction and asset mapping at scoring time.  ``future_prices`` then
supports descriptive direction and gross-return association statistics for the
mapped proxy and broad-market benchmark.  These are not execution-aware trading
backtests.  Installations without ledger history use an explicitly labelled,
short-retention legacy-cache fallback while the new cohort accumulates.
"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time as datetime_time
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from core.asset_mapping import get_mapping_version, map_article_to_assets
from core.digest_engine import dedupe_articles
from core.config import load_config
from core.user_feedback import resolve_scoring_contract
from db.database import (
    db_lock, get_connection, get_feed_thresholds,
    get_future_price,
    _get_future_price_at_offset, _trading_day_after, _is_trading_day_local,
    get_backtest_signals, get_backtest_signal_stats,
)
from utils.helpers import escape_html
from services.mapping_validation import (
    render_mapping_validation_compact_html,
    render_mapping_validation_html,
)
from services.quality_bridge_preview import load_bridge_preview
from services.event_outcomes import is_completed_close_price
from services.model_quality_overview import render_quality_overview
from scoring_explanation import EXPLANATION_FIELDS
from scoring_provenance import (
    NON_SUBSTANTIVE_SCORE_PATHS,
    RECOGNIZED_SCORE_PATHS,
    normalize_score_path,
)


# The benchmark remains the broad-market comparison series.  A mapped theme's
# own direction/return is measured on its explicit proxy instead of silently
# substituting this benchmark for every theme.
_BENCHMARK_SYMBOL = "sh000001"
_SUPPORTED_BACKTEST_SYMBOLS = {"sh000001", "sz399001", "sz399006"}
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = datetime_time(15, 0)
_MIN_STATISTICAL_SAMPLES = 30
_MIN_INDEPENDENT_RESULT_DAYS = 30
_OPTIMIZATION_MIN_INDEPENDENT_EVENTS = 100
_OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS = 60
_OPTIMIZATION_MIN_PROXY_COVERAGE = 80.0
_WIRE_DUPLICATE_WINDOW_SECONDS = 6 * 3600
_CLOCK_SKEW_SECONDS = 5
_THEME_OBSERVATION_SECONDS = 72 * 3600


def _stable_event_id(value: Any) -> str:
    """Reject legacy headline keys and accept only event-core UUIDs."""
    if not isinstance(value, (str, uuid.UUID)):
        return ""
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError):
        return ""


def _wilson_interval(hits: int, total: int, z: float = 1.96) -> dict[str, float] | None:
    """Return a 95% Wilson score interval in percentage points."""
    if total <= 0:
        return None
    p = hits / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
        / denominator
    )
    return {
        "lower": round(max(0.0, center - margin) * 100.0, 1),
        "upper": round(min(1.0, center + margin) * 100.0, 1),
    }


def _mapping_for_article(article: dict[str, Any]) -> dict[str, Any]:
    """Use the point-in-time mapping snapshot when one is available."""
    if article.get("_signal_snapshot"):
        return {
            # Preserve an explicit missing mapping for honest coverage.  Callers
            # that need a display label can still fall back to ``tag``.
            "mapped_theme": article.get("mapped_theme") or "",
            "backtest_symbol": article.get("backtest_symbol"),
        }
    return map_article_to_assets(article)


def _mapped_backtest_symbol(mapping: dict[str, Any]) -> str | None:
    """Resolve a validated close-price proxy from an asset mapping.

    Mappings produced before ``backtest_symbol`` existed fall back to the
    benchmark for compatibility.  An explicit ``None`` means that the current
    collector has no defensible proxy and the asset metric must be skipped.
    """
    if "backtest_symbol" not in mapping:
        return _BENCHMARK_SYMBOL
    raw_symbol = mapping.get("backtest_symbol")
    if not raw_symbol:
        return None
    symbol = str(raw_symbol).strip().casefold()
    return symbol if symbol in _SUPPORTED_BACKTEST_SYMBOLS else None


def _snapshot_direction(row: dict[str, Any] | None) -> int | None:
    """Extract price direction (1/-1/None) from snapshot row."""
    if not row or row.get("change_amt") is None:
        return None
    try:
        value = float(row["change_amt"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return 1 if value > 0 else -1 if value < 0 else 0


def _snapshot_return_pct(row: dict[str, Any] | None) -> float | None:
    """Extract percentage return from snapshot row."""
    if not row or row.get("change_pct") is None:
        return None
    try:
        value = float(row["change_pct"])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _snapshot_trade_date(row: dict[str, Any] | None) -> str | None:
    """Extract trade date (YYYY-MM-DD) from snapshot row."""
    if not row:
        return None
    value = str(row.get("trade_date") or "").strip()
    return value or None


def _sample_sufficient(total: int, unique_result_days: int) -> bool:
    """Check if sample meets minimum threshold (30 samples, 30 days)."""
    return (
        total >= _MIN_STATISTICAL_SAMPLES
        and unique_result_days >= _MIN_INDEPENDENT_RESULT_DAYS
    )


def _signal_cluster_identity(article: dict[str, Any]) -> str:
    """Return the frozen event identity used for posterior clustering."""
    event_id = _stable_event_id(article.get("event_id"))
    if event_id:
        return f"event:{event_id}"
    # Legacy rows must never merge merely because two headlines normalize to
    # the same text.  A stable link is the narrowest defensible fallback.
    link = str(article.get("link") or "").strip()
    return f"legacy-link:{link}" if link else ""


def _score_optimization_state(
    *,
    independent_events: int,
    independent_result_days: int,
    proxy_coverage: float,
    control_cohort_available: bool = False,
    control_cohort_recorded: bool = False,
    control_cohort_unselected_signals: int = 0,
    control_query_mode: str = "production_selected",
) -> dict[str, Any]:
    """Describe tuning admission without changing any production parameter."""
    posterior_ready = (
        independent_events >= _OPTIMIZATION_MIN_INDEPENDENT_EVENTS
        and independent_result_days >= _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS
        and proxy_coverage >= _OPTIMIZATION_MIN_PROXY_COVERAGE
    )
    control_cohort_ready = bool(control_cohort_available)
    recorded_unselected = max(0, int(control_cohort_unselected_signals))
    control_cohort_recorded = bool(control_cohort_recorded or recorded_unselected > 0)
    can_tune_thresholds = posterior_ready and control_cohort_ready
    stage = (
        "candidate"
        if can_tune_thresholds
        else "posterior_ready"
        if posterior_ready
        else "insufficient"
    )
    query_mode = str(control_query_mode or "production_selected")
    exploratory_query = query_mode.startswith("exploratory_")
    return {
        "stage": stage,
        "stage_label": {
            "candidate": "调参研究样本条件满足",
            "posterior_ready": "入选样本达标，对照待补齐",
            "insufficient": "调参样本待积累",
        }[stage],
        "recommendation": (
            "仅具备研究候选参数的样本条件，不代表模型质量合格；仍须留出验证。"
            if can_tune_thresholds
            else "入选研究样本已达标；全分数对照尚未独立结算，保持现行评分阈值。"
            if posterior_ready and control_cohort_recorded
            else "入选研究样本已达标；缺少全分数未入选对照，保持现行评分阈值。"
            if posterior_ready
            else (
                f"全分数对照已记录，未入选{recorded_unselected}条；"
                "调参样本仍在积累；各指标是否可展示另行判断。"
            )
            if control_cohort_recorded
            else "保持现行评分、来源门槛与筛选标准。"
        ),
        "auto_apply": False,
        "posterior_ready": posterior_ready,
        "can_tune_thresholds": can_tune_thresholds,
        # Compatibility: ``available`` means an independently settled control
        # result is ready for tuning, not merely that rejected scores exist.
        "control_cohort_available": control_cohort_ready,
        "control_cohort_ready": control_cohort_ready,
        "control_cohort_recorded": control_cohort_recorded,
        "control_cohort_unselected_signals": recorded_unselected,
        "control_query_mode": str(control_query_mode or "production_selected"),
        "cohort_label": (
            "全分数入选/未入选对照"
            if control_cohort_ready
            else "全分数对照已记录，待独立结算"
            if control_cohort_recorded
            else "仅生产入选信号"
        ),
        "independent_events": int(independent_events),
        "independent_result_days": int(independent_result_days),
        "proxy_coverage": round(float(proxy_coverage), 2),
        "scope": (
            query_mode
            if exploratory_query
            else "current_query_window"
        ),
        "scope_label": (
            "探索性全分数视图（不用于调参门控）"
            if query_mode == "exploratory_full_score"
            else "探索性分数阈值视图（不用于调参门控）"
            if exploratory_query
            else "当前统计窗口"
        ),
        "metric_basis": "mapped_asset_t1_event_result_day",
        "requirements": {
            "independent_events": _OPTIMIZATION_MIN_INDEPENDENT_EVENTS,
            "independent_result_days": _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS,
            "proxy_coverage": _OPTIMIZATION_MIN_PROXY_COVERAGE,
            "full_score_control_cohort": True,
        },
        "roadmap": [
            {"stage": "insufficient", "label": "调参样本待积累"},
            {"stage": "posterior_ready", "label": "入选样本达标，对照待补齐"},
            {"stage": "candidate", "label": "调参研究样本条件满足"},
            {"stage": "shadow", "label": "影子验证"},
            {"stage": "releasable", "label": "可发布"},
        ],
    }


@lru_cache(maxsize=8192)
def _same_day_snapshot(symbol: str, trade_date: str) -> dict[str, Any] | None:
    """Read the date-keyed ledger; live quotes are not posterior observations."""
    return get_future_price(symbol, trade_date)


@lru_cache(maxsize=16384)
def _future_snapshot(symbol: str, from_date: str, offset_days: int) -> dict[str, Any] | None:
    """Return only the exact calendar-defined future horizon snapshot."""
    return _get_future_price_at_offset(symbol, from_date, offset_days)


@lru_cache(maxsize=16384)
def _strict_revision_price_snapshot(
    symbol: str, from_date: str, offset_days: int
) -> dict[str, Any] | None:
    """Cache the immutable-ledger lookup without the legacy price fallback."""
    return _get_future_price_at_offset(symbol, from_date, offset_days)


def _is_t0_evaluable(ts: int) -> bool:
    """Whether a signal existed before its same-day A-share close.

    Daily closes cannot evaluate a signal published at or after 15:00 without
    look-ahead.  Such a signal keeps its T+1/T+2 observations but contributes no
    T0 sample.
    """
    published = datetime.fromtimestamp(ts, _MARKET_TZ)
    try:
        is_trading_day = _is_trading_day_local(published.date().isoformat())
    except ValueError:
        return False
    return is_trading_day and published.time().replace(tzinfo=None) < _MARKET_CLOSE


def _cluster_rank(article: dict[str, Any]) -> tuple[int, str, int]:
    """Return sort key for article clustering (newest first)."""
    return (
        int(article.get("available_at") or article.get("created_at") or 0),
        str(article.get("link") or ""),
        int(article.get("revision_seq") or 0),
    )


def _expected_result_trade_date(
    article: dict[str, Any], horizon: int
) -> str | None:
    try:
        timestamp = int(article.get("created_at") or article.get("available_at") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    signal_date = _trade_date(timestamp)
    if horizon == 0:
        return signal_date if _is_t0_evaluable(timestamp) else None
    try:
        return _trading_day_after(signal_date, horizon)
    except (TypeError, ValueError, OverflowError):
        return None


def _event_result_representatives(
    articles: list[dict[str, Any]], horizons: tuple[int, ...]
) -> dict[int, dict[tuple[str, str], tuple[tuple[int, str, int], dict[str, Any]]]]:
    """Freeze one earliest signal before inspecting direction, mapping or prices."""
    representatives = {horizon: {} for horizon in horizons}
    for article in articles:
        identity = _signal_cluster_identity(article)
        if not identity:
            continue
        rank = _cluster_rank(article)
        for horizon in horizons:
            result_date = _expected_result_trade_date(article, horizon)
            if not result_date:
                continue
            key = (identity, result_date)
            existing = representatives[horizon].get(key)
            if existing is None or rank < existing[0]:
                representatives[horizon][key] = (rank, article)
    return representatives


def _exact_result_snapshot(
    row: dict[str, Any] | None, expected_date: str | None, *, evaluation_at: int,
) -> dict[str, Any] | None:
    """Share V5 close eligibility at the fixed evaluation boundary of this run."""
    return row if (
        row is not None
        and expected_date is not None
        and is_completed_close_price(row, expected_date, evaluation_at=evaluation_at)
    ) else None


def _verified_future_pair(
    asset_symbol: str, article_date: str, horizon: int, expected_date: str | None,
    *, evaluation_at: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Keep asset and benchmark admission identical without merging coverage."""
    asset = _exact_result_snapshot(
        _future_snapshot(asset_symbol, article_date, horizon),
        expected_date, evaluation_at=evaluation_at,
    )
    benchmark = asset if asset_symbol == _BENCHMARK_SYMBOL else _exact_result_snapshot(
        _future_snapshot(_BENCHMARK_SYMBOL, article_date, horizon),
        expected_date, evaluation_at=evaluation_at,
    )
    return asset, benchmark


# ── 新增：主题级后验胜率（把 AI 前置预测主题与真实后验价格关联）────────────────
def _theme_future_win_rate(
    articles: list[dict], *, evaluation_at: int,
) -> dict[str, dict]:
    """统计 mapped theme 代理资产的 T+1/T+2 方向和信号收益。"""
    by_theme: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "t1_hits": 0, "t1_total": 0, "t2_hits": 0, "t2_total": 0,
        "avg_score": 0.0, "avg_confidence": 0.0,
        "asset_symbols": set(),
        "t1_unique_days": set(), "t2_unique_days": set(),
        "t1_return_sum": 0.0, "t1_return_total": 0,
        "t2_return_sum": 0.0, "t2_return_total": 0,
        "t1_excess_sum": 0.0, "t1_excess_total": 0,
        "t2_excess_sum": 0.0, "t2_excess_total": 0,
    })
    clustered: dict[int, dict[tuple[str, str], tuple]] = {1: {}, 2: {}}
    for article in articles:
        mapping = _mapping_for_article(article)
        theme = mapping.get("mapped_theme") or article.get("tag") or "未分类"
        score = float(article.get("score") or 0)
        conf = float(article.get("score_confidence") or 0)
        ts = int(article.get("created_at") or 0)
        article_date = _trade_date(ts)
        signal_dir = _article_signal_direction(article)
        if signal_dir == 0:
            continue
        b = by_theme[theme]
        b["count"] += 1
        b["avg_score"] += score
        b["avg_confidence"] += conf
        asset_symbol = _mapped_backtest_symbol(mapping)
        if asset_symbol is None:
            continue
        b["asset_symbols"].add(asset_symbol)
        for horizon in (1, 2):
            expected_date = _expected_result_trade_date(article, horizon)
            asset_row, benchmark_row = _verified_future_pair(
                asset_symbol, article_date, horizon, expected_date,
                evaluation_at=evaluation_at,
            )
            result_date = _snapshot_trade_date(asset_row)
            market_dir = _snapshot_direction(asset_row)
            direction_hit = None
            if market_dir is not None:
                b[f"t{horizon}_total"] += 1
                if result_date:
                    b[f"t{horizon}_unique_days"].add(result_date)
                direction_hit = signal_dir == market_dir
                if direction_hit:
                    b[f"t{horizon}_hits"] += 1
            asset_return = _snapshot_return_pct(asset_row)
            signed_return = None
            if asset_return is not None:
                signed_return = signal_dir * asset_return
                b[f"t{horizon}_return_sum"] += signed_return
                b[f"t{horizon}_return_total"] += 1
            benchmark_return = _snapshot_return_pct(benchmark_row)
            signed_excess = None
            if asset_return is not None and benchmark_return is not None:
                signed_excess = signal_dir * (asset_return - benchmark_return)
                b[f"t{horizon}_excess_sum"] += signed_excess
                b[f"t{horizon}_excess_total"] += 1
    representatives = _event_result_representatives(articles, (1, 2))
    for horizon, event_rows in representatives.items():
        for key, (rank, article) in event_rows.items():
            expected_date = key[1]
            mapping = _mapping_for_article(article)
            theme = mapping.get("mapped_theme") or article.get("tag") or "未分类"
            asset_symbol = _mapped_backtest_symbol(mapping)
            signal_dir = _article_signal_direction(article)
            direction_hit = None
            signed_return = None
            signed_excess = None
            if signal_dir != 0 and asset_symbol is not None:
                article_date = _trade_date(int(article.get("created_at") or 0))
                asset_row, benchmark_row = _verified_future_pair(
                    asset_symbol, article_date, horizon, expected_date,
                    evaluation_at=evaluation_at,
                )
                market_dir = _snapshot_direction(asset_row)
                if market_dir is not None:
                    direction_hit = signal_dir == market_dir
                asset_return = _snapshot_return_pct(asset_row)
                benchmark_return = _snapshot_return_pct(benchmark_row)
                if asset_return is not None:
                    signed_return = signal_dir * asset_return
                if asset_return is not None and benchmark_return is not None:
                    signed_excess = signal_dir * (asset_return - benchmark_return)
            clustered[horizon][key] = (
                rank,
                theme,
                asset_symbol,
                direction_hit,
                signed_return,
                signed_excess,
            )
    result = {}
    for theme, b in by_theme.items():
        if b["count"] == 0:
            continue
        row = {
            "count": b["count"],
            "avg_score": round(b["avg_score"] / b["count"], 2),
            "avg_confidence": round(b["avg_confidence"] / b["count"], 3),
            "t1_win_rate": round(b["t1_hits"] / b["t1_total"] * 100, 1) if b["t1_total"] else None,
            "t2_win_rate": round(b["t2_hits"] / b["t2_total"] * 100, 1) if b["t2_total"] else None,
            "t1_samples": b["t1_total"],
            "t2_samples": b["t2_total"],
            "t1_unique_days": len(b["t1_unique_days"]),
            "t2_unique_days": len(b["t2_unique_days"]),
            "t1_win_rate_ci95": _wilson_interval(b["t1_hits"], b["t1_total"]),
            "t2_win_rate_ci95": _wilson_interval(b["t2_hits"], b["t2_total"]),
            "t1_sample_sufficient": _sample_sufficient(
                b["t1_total"], len(b["t1_unique_days"])
            ),
            "t2_sample_sufficient": _sample_sufficient(
                b["t2_total"], len(b["t2_unique_days"])
            ),
            "asset_symbols": sorted(b["asset_symbols"]),
            "benchmark_symbol": _BENCHMARK_SYMBOL,
            "t1_avg_signal_return_pct": round(b["t1_return_sum"] / b["t1_return_total"], 3) if b["t1_return_total"] else None,
            "t2_avg_signal_return_pct": round(b["t2_return_sum"] / b["t2_return_total"], 3) if b["t2_return_total"] else None,
            "t1_avg_excess_return_pct": round(b["t1_excess_sum"] / b["t1_excess_total"], 3) if b["t1_excess_total"] else None,
            "t2_avg_excess_return_pct": round(b["t2_excess_sum"] / b["t2_excess_total"], 3) if b["t2_excess_total"] else None,
        }
        for horizon in (1, 2):
            entries = [
                (key, stored)
                for key, stored in clustered[horizon].items()
                if stored[1] == theme
            ]
            direction_entries = [
                (key, bool(stored[3]))
                for key, stored in entries
                if stored[3] is not None
            ]
            hits = sum(hit for _key, hit in direction_entries)
            samples = len(direction_entries)
            unique_days = len({key[1] for key, _hit in direction_entries})
            returns = [
                float(stored[4]) for _key, stored in entries if stored[4] is not None
            ]
            excess = [
                float(stored[5]) for _key, stored in entries if stored[5] is not None
            ]
            row.update(
                {
                    f"clustered_t{horizon}_samples": samples,
                    f"clustered_t{horizon}_hits": hits,
                    f"clustered_t{horizon}_unique_days": unique_days,
                    f"clustered_t{horizon}_win_rate": (
                        round(hits / samples * 100.0, 1) if samples else None
                    ),
                    f"clustered_t{horizon}_win_rate_ci95": _wilson_interval(
                        hits, samples
                    ),
                    f"clustered_t{horizon}_sample_sufficient": _sample_sufficient(
                        samples, unique_days
                    ),
                    f"clustered_t{horizon}_avg_signal_return_pct": (
                        round(sum(returns) / len(returns), 3) if returns else None
                    ),
                    f"clustered_t{horizon}_avg_excess_return_pct": (
                        round(sum(excess) / len(excess), 3) if excess else None
                    ),
                }
            )
        result[theme] = row
    return result


def _market_direction(trade_date: str, symbol: str = _BENCHMARK_SYMBOL) -> int | None:
    """Return T0 direction only for a completed exact-date ledger close."""
    return _snapshot_direction(_exact_result_snapshot(
        _same_day_snapshot(symbol, trade_date), trade_date,
        evaluation_at=int(time.time()),
    ))


def _article_signal_direction(article: dict) -> int:
    """Return an explicit signal direction without inventing one from score.

    New rows carry a structured ``direction`` field.  Tags are only a legacy
    fallback; a high score by itself expresses confidence, not direction.
    """
    direction = str(article.get("direction") or "").strip().lower()
    if direction:
        if direction in {"中性", "neutral", "持平", "无方向"}:
            return 0
        if any(token in direction for token in ("看空", "利空", "bear", "down", "下跌")):
            return -1
        if any(token in direction for token in ("看多", "利多", "bull", "up", "上涨")):
            return 1

    tag = str(article.get("tag") or "").lower()
    if "空" in tag or "跌" in tag or "down" in tag or "bear" in tag:
        return -1
    if "多" in tag or "涨" in tag or "up" in tag or "bull" in tag:
        return 1
    return 0


def _trade_date(ts: int) -> str:
    """Convert Unix timestamp to trade date (YYYY-MM-DD)."""
    return datetime.fromtimestamp(ts, _MARKET_TZ).date().isoformat()


@dataclass(frozen=True)
class ScoreBucket:
    label: str
    min_score: float
    max_score: float


BUCKETS = (
    ScoreBucket("9.0+", 9.0, 10.0),
    ScoreBucket("8.0-8.9", 8.0, 8.9999),
    ScoreBucket("7.0-7.9", 7.0, 7.9999),
    ScoreBucket("6.0-6.9", 6.0, 6.9999),
    ScoreBucket("0.0-5.9", 0.0, 5.9999),
)

INVALID_TAGS = {"", "未知", "无", "无关", "无相关", "无相关标签", "无相关性标签"}
INVALID_TEXT_PATTERNS = (
    "无相关标签", "无相关性标签", "无相关新闻", "无相关性", "无直接影响",
    "未发现明显", "暂未发现", "相关性低",
)


def _bucket_for(score: float) -> ScoreBucket | None:
    """Map score to appropriate score bucket."""
    for bucket in BUCKETS:
        if bucket.min_score <= score <= bucket.max_score:
            return bucket
    return None


def _load_articles(days: int, min_score: float | None, cfg: dict | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    cutoff = int(time.time()) - max(1, days) * 86400
    with db_lock:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT link, title, raw_title, score, tag, logic, summary, created_at, "
                "score_confidence, feed_url, direction, score_path, strategy_version, "
                "event_id, event_version, " + ", ".join(EXPLANATION_FIELDS)
                + " FROM news_cache WHERE created_at >= ? "
                "ORDER BY created_at DESC, score DESC",
                (cutoff,),
            ).fetchall()
    articles = []
    for row in rows:
        article = dict(row)
        score = article.get("score")
        path = normalize_score_path(article.get("score_path"))
        threshold = float(min_score) if min_score is not None else get_feed_thresholds(cfg, article.get("feed_url") or "")["aggregation"]
        if (
            score is not None
            and float(score) >= threshold
            and path in RECOGNIZED_SCORE_PATHS
            and path != "similarity"
            and path not in NON_SUBSTANTIVE_SCORE_PATHS
        ):
            articles.append(article)
    return articles


def _strategy_family_label(strategy_version: str) -> str:
    """Return the operator-controlled family segment of a strategy identity."""
    value = str(strategy_version or "").strip()
    return value.split(":", 1)[0] if ":" in value else value


def _signal_cohort_history(
    strategy_version: str,
    mapping_version: str,
    cutoff: int,
    evaluation_at: int | None = None,
) -> dict[str, Any]:
    """Summarize exact and same-family ledgers without mixing their outcomes.

    A strategy fingerprint is the formal inference boundary.  The manual prefix
    remains useful as a transparent historical reference, so the UI can show
    that older samples were retained instead of making every fingerprint change
    look like data loss.  Family totals keep only the latest row per URL across
    fingerprints to avoid counting a re-scored article more than once.
    """
    exact_version = str(strategy_version or "").strip()
    family = _strategy_family_label(exact_version)
    empty = {
        "family": family,
        "current_version": exact_version,
        "current_mapping_version": str(mapping_version or "").strip(),
        "version_count": 0,
        "current_scored_signals": 0,
        "current_selected_signals": 0,
        "current_unselected_signals": 0,
        "current_selected_signal_days": 0,
        "family_scored_signals": 0,
        "family_selected_signals": 0,
        "family_unselected_signals": 0,
        "family_selected_signal_days": 0,
        "archived_selected_signals": 0,
        "archived_version_count": 0,
        "first_available_at": 0,
        "last_available_at": 0,
    }
    if not exact_version:
        return empty

    upper_bound = int(time.time() if evaluation_at is None else evaluation_at)
    if ":" in exact_version:
        clause = (
            "(strategy_version IN (?, ?) OR (instr(strategy_version, ':') > 0 "
            "AND substr(strategy_version, 1, instr(strategy_version, ':') - 1) = ?))"
        )
        params: tuple[Any, ...] = (
            exact_version,
            family,
            family,
            int(cutoff),
            upper_bound + _CLOCK_SKEW_SECONDS,
        )
    else:
        clause = "substr(strategy_version, 1, 14) = ?"  # Match 2026-05-19-v2 family
        params = (
            exact_version,
            int(cutoff),
            upper_bound + _CLOCK_SKEW_SECONDS,
        )
    try:
        with db_lock:
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT id, link, strategy_version, available_at, selected, "
                    "is_similarity_copy, score_path FROM backtest_signals WHERE "
                    + clause
                    + " AND mapping_version = ? "
                    + "AND available_at >= ? AND available_at <= ? "
                    "ORDER BY available_at, id",
                    (*params[:-2], str(mapping_version), *params[-2:]),
                ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return empty
        raise

    raw = [dict(row) for row in rows]
    versions = {
        str(row.get("strategy_version") or "")
        for row in raw
        if (
            normalize_score_path(row.get("score_path"))
            in RECOGNIZED_SCORE_PATHS
            and normalize_score_path(row.get("score_path"))
            not in NON_SUBSTANTIVE_SCORE_PATHS
        )
    }

    def latest_independent(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            latest[str(item.get("link") or "")] = item
        return [
            item for item in latest.values()
            if (
                not bool(item.get("is_similarity_copy"))
                and normalize_score_path(item.get("score_path"))
                in RECOGNIZED_SCORE_PATHS
                and normalize_score_path(item.get("score_path"))
                not in NON_SUBSTANTIVE_SCORE_PATHS
            )
        ]

    current = latest_independent(
        [
            row
            for row in raw
            if str(row.get("strategy_version") or "") == exact_version
        ]
    )
    family_rows = latest_independent(raw)
    current_selected = [row for row in current if bool(row.get("selected"))]
    family_selected = [row for row in family_rows if bool(row.get("selected"))]
    archived_selected = [
        row
        for row in family_selected
        if str(row.get("strategy_version") or "") != exact_version
    ]

    def signal_days(items: list[dict[str, Any]]) -> int:
        return len({
            _trade_date(int(row.get("available_at") or 0))
            for row in items if int(row.get("available_at") or 0) > 0
        })

    return {
        **empty,
        "version_count": len(versions),
        "current_scored_signals": len(current),
        "current_selected_signals": len(current_selected),
        "current_unselected_signals": len(current) - len(current_selected),
        "current_selected_signal_days": signal_days(current_selected),
        "family_scored_signals": len(family_rows),
        "family_selected_signals": len(family_selected),
        "family_unselected_signals": len(family_rows) - len(family_selected),
        "family_selected_signal_days": signal_days(family_selected),
        "archived_selected_signals": len(archived_selected),
        "archived_version_count": max(0, len(versions) - (1 if exact_version in versions else 0)),
        "first_available_at": min(
            (int(row.get("available_at") or 0) for row in family_rows), default=0
        ),
        "last_available_at": max(
            (int(row.get("available_at") or 0) for row in family_rows), default=0
        ),
    }


def _snapshot_articles(
    days: int,
    min_score: float | None,
    cfg: dict,
    mapping_version: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    evaluation_at = int(time.time())
    cutoff = evaluation_at - max(1, days) * 86400
    strategy_version = str(cfg.get("scoring_strategy_version") or "")
    stats = get_backtest_signal_stats(
        strategy_version=strategy_version,
        mapping_version=mapping_version,
    )
    ledger_stats = get_backtest_signal_stats()
    stats["ledger_count"] = int(ledger_stats.get("count") or 0)
    stats["ledger_revision_count"] = int(ledger_stats.get("revision_count") or 0)
    stats["cohort_history"] = _signal_cohort_history(
        strategy_version, mapping_version, cutoff, evaluation_at
    )
    rows = get_backtest_signals(
        cutoff=cutoff,
        strategy_version=strategy_version,
        mapping_version=mapping_version,
        min_score=min_score,
        # The production view and its rejected-signal control must come from
        # the same final-revision population.  Selection is split only after
        # this single exact-contract read.
        selected_only=False,
        include_similarity=False,
    )
    population: list[dict[str, Any]] = []
    for row in rows:
        path = normalize_score_path(row.get("score_path"))
        if (
            path not in RECOGNIZED_SCORE_PATHS
            or path in NON_SUBSTANTIVE_SCORE_PATHS
        ):
            continue
        available_at = int(row.get("available_at") or 0)
        # Corrupt/future-dated ledger rows are not knowledge available to this
        # evaluation.  A tiny tolerance covers second-boundary test/clock skew;
        # the durable DB write still raises available_at to commit-path time.
        if available_at <= 0 or available_at > evaluation_at + _CLOCK_SKEW_SECONDS:
            continue
        event_id = _stable_event_id(row.get("event_id"))
        try:
            event_version = int(row.get("event_version") or 0)
        except (TypeError, ValueError):
            event_version = 0
        population.append({
            "link": row.get("link") or row.get("signal_key") or "",
            "title": row.get("event_key") or row.get("mapped_theme") or "",
            "raw_title": "",
            "score": row.get("score"),
            "tag": row.get("mapped_theme") or "未分类",
            "logic": "",
            "summary": "",
            # Backtest time semantics are based on when the completed score was
            # actually available, never on the publisher's timestamp.
            "created_at": available_at,
            "available_at": available_at,
            "scored_at": int(row.get("scored_at") or 0),
            "published_at": row.get("published_at"),
            "score_confidence": float(row.get("score_confidence") or 0.0),
            **{field: row.get(field) for field in EXPLANATION_FIELDS},
            "feed_url": row.get("feed_url") or "",
            "direction": row.get("direction") or "中性",
            "score_path": row.get("score_path") or "",
            "revision_key": row.get("revision_key") or "base",
            "signal_stage": row.get("signal_stage") or "base",
            "revision_seq": int(row.get("revision_seq") or 0),
            "revision_state_key": row.get("revision_state_key") or "",
            "strategy_version": row.get("strategy_version") or "",
            "mapping_version": row.get("mapping_version") or "",
            "mapped_theme": row.get("mapped_theme") or "",
            "backtest_symbol": row.get("backtest_symbol"),
            "event_id": event_id or None,
            "event_version": event_version if event_version > 0 else 0,
            "event_key": row.get("event_key") or row.get("link") or "",
            "independence_key": event_id or row.get("independence_key") or row.get("event_key") or row.get("link") or "",
            "aggregation_threshold": row.get("aggregation_threshold"),
            "selected": bool(row.get("selected")),
            "_signal_snapshot": True,
        })
    if min_score is None:
        articles = [article for article in population if bool(article.get("selected"))]
        control_population = population
    else:
        articles = population
        # Explicit threshold views are exploratory and cannot furnish the
        # independently settled production control used by tuning admission.
        control_population = []
    return articles, control_population, stats


def _dedupe_snapshot_signals(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse near-simultaneous wire copies, never recurring daily events."""
    selected: list[dict[str, Any]] = []
    last_kept_at: dict[tuple[str, str, str], int] = {}
    for article in sorted(
        articles,
        key=lambda item: (int(item.get("available_at") or 0), str(item.get("link") or "")),
    ):
        key = (
            str(
                _stable_event_id(article.get("event_id"))
                or article.get("independence_key")
                or article.get("event_key")
                or article.get("link")
                or ""
            ),
            str(article.get("backtest_symbol") or ""),
            str(article.get("strategy_version") or ""),
        )
        available_at = int(article.get("available_at") or 0)
        previous_at = last_kept_at.get(key)
        if (
            previous_at is not None
            and 0 <= available_at - previous_at <= _WIRE_DUPLICATE_WINDOW_SECONDS
        ):
            continue
        selected.append(article)
        last_kept_at[key] = available_at
    return selected


def _empty_score_control_arm(recorded: int = 0) -> dict[str, Any]:
    return {
        "recorded_signals": int(recorded),
        "deduped_signals": 0,
        "valid_score_signals": 0,
        "proxy_mapped_signals": 0,
        "proxy_coverage": 0.0,
        "representative_events": 0,
        "t1_settled_event_results": 0,
        "independent_events": 0,
        "independent_result_days": 0,
        "direction_hits": 0,
        "direction_hit_rate": None,
        "direction_hit_rate_ci95": None,
        "avg_signed_return_pct": None,
        "avg_signed_excess_pct": None,
        "descriptive_ready": False,
        "candidate_ready": False,
        "exclusions": {
            "missing_event_identity": 0,
            "neutral_direction": 0,
            "missing_proxy": 0,
            "missing_exact_asset_t1": 0,
            "missing_exact_benchmark_t1": 0,
            "missing_price_revision": 0,
            "invalid_price_observation": 0,
        },
    }


def _settle_score_control_cohort(
    population: list[dict[str, Any]],
    *,
    strategy_version: str,
    mapping_version: str,
    evaluation_at: int | None = None,
) -> dict[str, Any]:
    """Settle disjoint selected/rejected T+1 cohorts from one frozen population.

    Representatives are chosen globally before reading the frozen ``selected``
    bit.  A canonical event/result day can therefore belong to exactly one arm,
    even when different source articles crossed the production threshold.
    Only revision-backed ``future_prices`` rows on the exact target day qualify;
    the compatibility ``market_prices`` fallback is intentionally excluded.
    """

    if evaluation_at is None:
        evaluation_at = int(time.time())

    # This cache is scoped to one control computation.  Many events share the
    # same proxy and result date, so price reads grow with unique proxy/dates
    # instead of with every event representative.
    _strict_revision_price_snapshot.cache_clear()
    recorded = {
        "selected": sum(bool(row.get("selected")) for row in population),
        "unselected": sum(not bool(row.get("selected")) for row in population),
    }
    arms = {name: _empty_score_control_arm(recorded[name]) for name in ("selected", "unselected")}
    deduped = _dedupe_snapshot_signals(population)
    valid = [article for article in deduped if not _is_invalid_score(article)]
    proxy_coverage_raw: dict[str, float] = {}
    for name in arms:
        arm_rows = [
            article
            for article in deduped
            if bool(article.get("selected")) == (name == "selected")
        ]
        valid_rows = [
            article
            for article in valid
            if bool(article.get("selected")) == (name == "selected")
        ]
        proxy_rows = [
            article
            for article in valid_rows
            if _mapped_backtest_symbol(_mapping_for_article(article)) is not None
        ]
        arms[name]["deduped_signals"] = len(arm_rows)
        arms[name]["valid_score_signals"] = len(valid_rows)
        arms[name]["proxy_mapped_signals"] = len(proxy_rows)
        proxy_coverage_raw[name] = (
            len(proxy_rows) / len(valid_rows) * 100.0 if valid_rows else 0.0
        )
        arms[name]["proxy_coverage"] = round(proxy_coverage_raw[name], 2)

    representatives = _event_result_representatives(valid, (1,))[1]
    outcome_keys: dict[str, set[tuple[str, str]]] = {
        "selected": set(),
        "unselected": set(),
    }
    event_ids: dict[str, set[str]] = {"selected": set(), "unselected": set()}
    result_days: dict[str, set[str]] = {"selected": set(), "unselected": set()}
    signed_returns: dict[str, list[float]] = {"selected": [], "unselected": []}
    signed_excess: dict[str, list[float]] = {"selected": [], "unselected": []}

    for key, (_rank, article) in representatives.items():
        name = "selected" if bool(article.get("selected")) else "unselected"
        arm = arms[name]
        identity, expected_date = key
        arm["representative_events"] += 1
        event_id = _stable_event_id(article.get("event_id"))
        if not event_id or identity != f"event:{event_id}":
            arm["exclusions"]["missing_event_identity"] += 1
            continue
        signal_direction = _article_signal_direction(article)
        if signal_direction == 0:
            arm["exclusions"]["neutral_direction"] += 1
            continue
        symbol = _mapped_backtest_symbol(_mapping_for_article(article))
        if not symbol:
            arm["exclusions"]["missing_proxy"] += 1
            continue
        article_date = _trade_date(int(article.get("available_at") or 0))
        asset_row = _strict_revision_price_snapshot(symbol, article_date, 1)
        benchmark_row = _strict_revision_price_snapshot(
            _BENCHMARK_SYMBOL, article_date, 1
        )
        if _snapshot_trade_date(asset_row) != expected_date:
            arm["exclusions"]["missing_exact_asset_t1"] += 1
            continue
        if _snapshot_trade_date(benchmark_row) != expected_date:
            arm["exclusions"]["missing_exact_benchmark_t1"] += 1
            continue
        if not str(asset_row.get("revision_id") or "").strip() or not str(
            benchmark_row.get("revision_id") or ""
        ).strip():
            arm["exclusions"]["missing_price_revision"] += 1
            continue
        if any(
            _exact_result_snapshot(row, expected_date, evaluation_at=evaluation_at) is None
            for row in (asset_row, benchmark_row)
        ):
            arm["exclusions"]["invalid_price_observation"] += 1
            continue
        asset_direction = _snapshot_direction(asset_row)
        asset_return = _snapshot_return_pct(asset_row)
        benchmark_return = _snapshot_return_pct(benchmark_row)
        if (
            asset_direction is None
            or asset_return is None
            or benchmark_return is None
        ):
            arm["exclusions"]["invalid_price_observation"] += 1
            continue
        outcome_keys[name].add((event_id, expected_date))
        event_ids[name].add(event_id)
        result_days[name].add(expected_date)
        arm["direction_hits"] += int(signal_direction == asset_direction)
        signed_returns[name].append(signal_direction * asset_return)
        signed_excess[name].append(
            signal_direction * (asset_return - benchmark_return)
        )

    overlap = outcome_keys["selected"] & outcome_keys["unselected"]
    for name in arms:
        arm = arms[name]
        samples = len(outcome_keys[name])
        days = len(result_days[name])
        arm["t1_settled_event_results"] = samples
        arm["independent_events"] = len(event_ids[name])
        arm["independent_result_days"] = days
        arm["descriptive_ready"] = (
            samples >= _MIN_STATISTICAL_SAMPLES
            and days >= _MIN_INDEPENDENT_RESULT_DAYS
        )
        arm["candidate_ready"] = (
            len(event_ids[name]) >= _OPTIMIZATION_MIN_INDEPENDENT_EVENTS
            and days >= _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS
            and proxy_coverage_raw[name] >= _OPTIMIZATION_MIN_PROXY_COVERAGE
        )
        if arm["descriptive_ready"] and samples:
            hits = int(arm["direction_hits"])
            arm["direction_hit_rate"] = round(hits / samples * 100.0, 1)
            arm["direction_hit_rate_ci95"] = _wilson_interval(hits, samples)
            arm["avg_signed_return_pct"] = round(
                sum(signed_returns[name]) / len(signed_returns[name]), 3
            )
            arm["avg_signed_excess_pct"] = round(
                sum(signed_excess[name]) / len(signed_excess[name]), 3
            )

    ready = (
        not overlap
        and arms["selected"]["candidate_ready"]
        and arms["unselected"]["candidate_ready"]
    )
    return {
        "method_version": "score-control-t1-v1",
        "strategy_version": str(strategy_version),
        "mapping_version": str(mapping_version),
        "status": (
            "ready"
            if ready
            else "settling"
            if recorded["unselected"] > 0
            else "not_recorded"
        ),
        "status_label": (
            "独立对照已就绪"
            if ready
            else "独立对照结算中"
            if recorded["unselected"] > 0
            else "尚无未入选对照"
        ),
        "ready_for_tuning": ready,
        "cohort_overlap": len(overlap),
        "selected": arms["selected"],
        "unselected": arms["unselected"],
        "requirements": {
            "descriptive_event_results": _MIN_STATISTICAL_SAMPLES,
            "descriptive_result_days": _MIN_INDEPENDENT_RESULT_DAYS,
            "candidate_independent_events_per_arm": _OPTIMIZATION_MIN_INDEPENDENT_EVENTS,
            "candidate_result_days_per_arm": _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS,
            "candidate_proxy_coverage_per_arm": _OPTIMIZATION_MIN_PROXY_COVERAGE,
            "disjoint_event_result_keys": True,
        },
        "auto_apply": False,
    }


def _load_backtest_samples(
    days: int,
    min_score: float | None,
    cfg: dict,
    mapping_version: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Prefer durable snapshots; use the short cache only as labelled legacy data."""
    snapshots, control_population, stats = _snapshot_articles(
        days, min_score, cfg, mapping_version
    )
    if int(stats.get("ledger_revision_count") or 0) > 0:
        return (
            snapshots,
            control_population,
            {**stats, "data_source": "signal_snapshots"},
        )
    legacy = _load_articles(days=days, min_score=min_score, cfg=cfg)
    return legacy, [], {**stats, "data_source": "legacy_news_cache"}


def _is_invalid_sample(article: dict[str, Any]) -> bool:
    tag = str(article.get("tag") or "").strip()
    text = " ".join(str(article.get(key) or "") for key in ("title", "raw_title", "logic", "summary"))
    if tag in INVALID_TAGS:
        return True
    return any(pattern in text for pattern in INVALID_TEXT_PATTERNS)


def _is_invalid_score(article: dict[str, Any]) -> bool:
    try:
        score = float(article.get("score"))
    except (TypeError, ValueError):
        return True
    return not math.isfinite(score) or not 0.0 <= score <= 10.0




def _initialize_metric_accumulators() -> dict[str, Any]:
    """Initialize all metric accumulation data structures for backtest.
    
    Returns:
        dict containing all initialized accumulator structures:
        - by_bucket: Articles grouped by score bucket
        - future_theme_hits: Theme reappearance tracking
        - direction_hits/total: Asset direction accuracy by horizon
        - benchmark_hits/total: Benchmark direction accuracy by horizon
        - signal_return_sum/total: Signal return accumulation by horizon
        - excess_return_sum/total: Excess return accumulation by horizon
        - direction_unique_days: Independent result days for asset direction
        - benchmark_unique_days: Independent result days for benchmark
        - clustered_*: Deduplicated cluster-level metrics
        - unclusterable_metric_observations: Count of non-clusterable signals
    """
    return {
        "by_bucket": defaultdict(list),
        "future_theme_hits": defaultdict(int),
        "direction_hits": {horizon: defaultdict(int) for horizon in (0, 1, 2)},
        "direction_total": {horizon: defaultdict(int) for horizon in (0, 1, 2)},
        "benchmark_hits": {horizon: defaultdict(int) for horizon in (0, 1, 2)},
        "benchmark_total": {horizon: defaultdict(int) for horizon in (0, 1, 2)},
        "signal_return_sum": {horizon: defaultdict(float) for horizon in (0, 1, 2)},
        "signal_return_total": {horizon: defaultdict(int) for horizon in (0, 1, 2)},
        "excess_return_sum": {horizon: defaultdict(float) for horizon in (0, 1, 2)},
        "excess_return_total": {horizon: defaultdict(int) for horizon in (0, 1, 2)},
        "direction_unique_days": {horizon: defaultdict(set) for horizon in (0, 1, 2)},
        "benchmark_unique_days": {horizon: defaultdict(set) for horizon in (0, 1, 2)},
        "clustered_asset_direction": {horizon: {} for horizon in (0, 1, 2)},
        "clustered_benchmark_direction": {horizon: {} for horizon in (0, 1, 2)},
        "clustered_signal_return": {horizon: {} for horizon in (0, 1, 2)},
        "clustered_excess_return": {horizon: {} for horizon in (0, 1, 2)},
        "unclusterable_metric_observations": 0,
    }


def _partition_valid_invalid_articles(
    raw_articles: list[dict[str, Any]],
    data_source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition articles into valid, invalid, and deduplicated sets.
    
    Signal snapshots skip text-based validation (ex-ante property).
    Live signals apply both score and sample validation rules.
    
    Args:
        raw_articles: Unfiltered article list.
        data_source: "signal_snapshots" or other source identifier.
    
    Returns:
        (valid_articles, invalid_articles, deduped_all) tuple.
    """
    if data_source == "signal_snapshots":
        deduped = _dedupe_snapshot_signals(raw_articles)
        invalid = [article for article in deduped if _is_invalid_score(article)]
        valid = [article for article in deduped if not _is_invalid_score(article)]
    else:
        deduped = dedupe_articles(raw_articles)
        invalid = [
            article
            for article in deduped
            if _is_invalid_score(article) or _is_invalid_sample(article)
        ]
        valid = [
            article
            for article in deduped
            if not _is_invalid_score(article) and not _is_invalid_sample(article)
        ]
    return valid, invalid, deduped


def _build_score_eligibility_funnel(
    *,
    data_source: str,
    min_score: float | None,
    history: dict[str, Any],
    cohort_history: dict[str, Any],
    query_rows: int,
    wire_deduped: int,
    valid_score: int,
    proxy_mapped: int,
    optimization_state: dict[str, Any],
    control_cohort: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the score eligibility funnel without mutating its inputs."""
    control_unselected = control_cohort.get("unselected", {})
    return {
        "scope": "current_exact_contract",
        "formal_eligible": (
            data_source == "signal_snapshots" and min_score is None
        ),
        "note": (
            "资格过滤不代表删除数据；账本信号与事件结果是不同计量单位，历史契约不合并。"
        ),
        "ledger": {
            "all_contract_revisions": int(
                history.get("ledger_revision_count") or 0
            ),
            "all_contract_latest_independent": int(
                history.get("ledger_count") or 0
            ),
            "current_contract_scored": int(
                cohort_history.get("current_scored_signals") or 0
            ),
            "current_contract_selected": int(
                cohort_history.get("current_selected_signals") or 0
            ),
            "current_contract_unselected": int(
                cohort_history.get("current_unselected_signals") or 0
            ),
        },
        "selected_analysis": {
            "query_rows": query_rows,
            "wire_deduped": wire_deduped,
            "valid_score": valid_score,
            "proxy_mapped": proxy_mapped,
            "t1_independent_events": int(
                optimization_state.get("independent_events") or 0
            ),
            "t1_independent_result_days": int(
                optimization_state.get("independent_result_days") or 0
            ),
        },
        "control_analysis": {
            "recorded": int(control_unselected.get("recorded_signals", 0)),
            "wire_deduped": int(control_unselected.get("deduped_signals", 0)),
            "valid_score": int(control_unselected.get("valid_score_signals", 0)),
            "proxy_mapped": int(
                control_unselected.get("proxy_mapped_signals", 0)
            ),
            "t1_settled_event_results": int(
                control_unselected.get("t1_settled_event_results", 0)
            ),
            "t1_independent_result_days": int(
                control_unselected.get("independent_result_days", 0)
            ),
        },
    }


@dataclass(frozen=True)
class _ScoreBacktestRun:
    """One query's frozen inputs; price accumulators remain separately scoped."""

    days: int
    min_score: float | None
    evaluation_at: int
    cfg: dict[str, Any]
    mapping_version: str
    raw_articles: list[dict[str, Any]]
    control_population: list[dict[str, Any]]
    history: dict[str, Any]
    articles: list[dict[str, Any]]
    invalid_articles: list[dict[str, Any]]
    deduped_all: list[dict[str, Any]]


def _load_score_backtest_run(days: int, min_score: float | None) -> _ScoreBacktestRun:
    """Resolve one exact contract and preserve the legacy loader shim."""
    # One cutoff for the entire 72-hour observation calculation.
    evaluation_at = int(time.time())
    # Cache exact price lookups only for this computation.  This collapses the
    # article-level N+1 pattern while still observing newly collected closes on
    # the next Dashboard refresh.
    cache_clear = getattr(_same_day_snapshot, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    cache_clear = getattr(_future_snapshot, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    cfg = resolve_scoring_contract(load_config(), register_snapshot=False)
    mapping_version = str(get_mapping_version() or "").strip()
    if not mapping_version:
        raise RuntimeError("mapping contract unavailable")
    loaded = _load_backtest_samples(days, min_score, cfg, mapping_version)
    # Keep small test/integration shims that still return the historical
    # two-tuple compatible; only the production loader supplies a control
    # population and therefore only it can unlock control readiness.
    if len(loaded) == 3:
        raw_articles, control_population, history = loaded
    else:
        raw_articles, history = loaded
        control_population = []
    # Partition articles into valid and invalid
    articles, invalid_articles, deduped_all = _partition_valid_invalid_articles(
        raw_articles, history.get("data_source", "")
    )
    return _ScoreBacktestRun(
        days, min_score, evaluation_at, cfg, mapping_version, raw_articles,
        control_population, history, articles, invalid_articles, deduped_all,
    )


def _accumulate_backtest_articles(
    articles: list[dict[str, Any]], evaluation_at: int, accum: dict[str, Any]
) -> None:
    """Track ordered theme recurrence, then descriptive article outcomes."""
    theme_mature_cutoff = evaluation_at - _THEME_OBSERVATION_SECONDS
    # Theme follow-through proxy: after a signal appears, does the same mapped
    # theme reappear within the next 72 hours? This is not price return, but it
    # tests whether the score captures persistent market narratives.
    # Walk newest -> oldest. The cache then contains only observations that are
    # in the current article's future, never its past.
    ordered = sorted(articles, key=lambda x: int(x.get("created_at") or 0), reverse=True)
    # Iterating newest -> oldest means the stored timestamp is the nearest
    # later observation for that theme.  A dict preserves the exact 72-hour
    # recurrence result while avoiding the former O(n²) scan that could exceed
    # the Dashboard proxy timeout on a cold 90-day cache.
    next_seen_by_theme: dict[str, int] = {}
    next_distinct_by_theme: dict[str, int] = {}
    for article in ordered:
        score = float(article.get("score") or 0)
        bucket = _bucket_for(score)
        if not bucket:
            continue
        mapping = _mapping_for_article(article)
        theme = mapping.get("mapped_theme") or article.get("tag") or "未分类"
        ts = int(article.get("created_at") or 0)
        later_ts = next_seen_by_theme.get(theme)
        # Same-second signals cannot validate each other, but must all retain
        # access to the nearest strictly later observation.
        if later_ts == ts:
            later_ts = next_distinct_by_theme.get(theme)
        has_follow_through = bool(
            later_ts is not None
            and 0 < later_ts - ts <= _THEME_OBSERVATION_SECONDS
        )
        if 0 < ts <= theme_mature_cutoff and has_follow_through:
            accum["future_theme_hits"][bucket.label] += 1
        # Pending articles remain evidence for older signals. Future-dated
        # articles are not evidence available at this evaluation cutoff.
        if 0 < ts <= evaluation_at and next_seen_by_theme.get(theme) != ts:
            if theme in next_seen_by_theme:
                next_distinct_by_theme[theme] = next_seen_by_theme[theme]
            next_seen_by_theme[theme] = ts
        enriched = {**article, "mapped_theme": theme, "mapping": mapping}
        accum["by_bucket"][bucket.label].append(enriched)

        _accumulate_backtest_article_outcomes(
            article, mapping, bucket.label, accum, evaluation_at=evaluation_at,
        )


def _accumulate_backtest_article_outcomes(
    article: dict[str, Any], mapping: dict[str, Any],
    bucket_label: str, accum: dict[str, Any], *, evaluation_at: int,
) -> None:
    """Keep article-level asset and benchmark counters independent."""
    ts = int(article.get("created_at") or 0)
    # Direction accuracy and signed return on the mapped asset.  The broad
    # benchmark is tracked separately, so theme mapping changes the asset
    # result without losing the old market-comparison semantics.
    signal_dir = _article_signal_direction(article)
    if signal_dir != 0:
        article_date = _trade_date(ts)
        asset_symbol = _mapped_backtest_symbol(mapping)
        for horizon in (0, 1, 2):
            # A close published before the signal is known must never be a
            # T0 outcome.  Future horizons remain available.
            if horizon == 0 and not _is_t0_evaluable(ts):
                continue
            if horizon == 0:
                benchmark_row = _same_day_snapshot(_BENCHMARK_SYMBOL, article_date)
                asset_row = (
                    benchmark_row
                    if asset_symbol == _BENCHMARK_SYMBOL
                    else _same_day_snapshot(asset_symbol, article_date)
                    if asset_symbol
                    else None
                )
            else:
                benchmark_row = _future_snapshot(_BENCHMARK_SYMBOL, article_date, horizon)
                asset_row = (
                    benchmark_row
                    if asset_symbol == _BENCHMARK_SYMBOL
                    else _future_snapshot(asset_symbol, article_date, horizon)
                    if asset_symbol
                    else None
                )

            expected_date = _expected_result_trade_date(article, horizon)
            benchmark_row = _exact_result_snapshot(
                benchmark_row, expected_date, evaluation_at=evaluation_at,
            )
            asset_row = _exact_result_snapshot(
                asset_row, expected_date, evaluation_at=evaluation_at,
            )
            benchmark_result_date = _snapshot_trade_date(benchmark_row)
            asset_result_date = _snapshot_trade_date(asset_row)
            benchmark_dir = _snapshot_direction(benchmark_row)
            if benchmark_dir is not None:
                accum["benchmark_total"][horizon][bucket_label] += 1
                if benchmark_result_date:
                    accum["benchmark_unique_days"][horizon][bucket_label].add(
                        benchmark_result_date
                    )
                if signal_dir == benchmark_dir:
                    accum["benchmark_hits"][horizon][bucket_label] += 1

            asset_dir = _snapshot_direction(asset_row)
            if asset_dir is not None:
                accum["direction_total"][horizon][bucket_label] += 1
                if asset_result_date:
                    accum["direction_unique_days"][horizon][bucket_label].add(
                        asset_result_date
                    )
                if signal_dir == asset_dir:
                    accum["direction_hits"][horizon][bucket_label] += 1

            asset_return = _snapshot_return_pct(asset_row)
            benchmark_return = _snapshot_return_pct(benchmark_row)
            if asset_return is not None:
                signed_return = signal_dir * asset_return
                accum["signal_return_sum"][horizon][bucket_label] += signed_return
                accum["signal_return_total"][horizon][bucket_label] += 1
            if asset_return is not None and benchmark_return is not None:
                signed_excess = signal_dir * (
                    asset_return - benchmark_return
                )
                accum["excess_return_sum"][horizon][bucket_label] += signed_excess
                accum["excess_return_total"][horizon][bucket_label] += 1


def _accumulate_backtest_clusters(
    articles: list[dict[str, Any]], accum: dict[str, Any], *, evaluation_at: int,
) -> tuple[int, int]:
    """Select earliest representatives before reading outcomes or missing prices."""
    unclusterable_metric_observations = 0
    # Select the representative before reading its direction, mapping or price
    # outcome.  This prevents a later, more convenient revision from replacing
    # an earlier neutral/unmapped/unevaluable signal in another score bucket.
    representatives = _event_result_representatives(articles, (0, 1, 2))
    cluster_result_date_unavailable_observations = sum(
        _expected_result_trade_date(article, horizon) is None
        for article in articles
        if _signal_cluster_identity(article)
        for horizon in (1, 2)
    )
    for horizon, event_rows in representatives.items():
        for key, (rank, article) in event_rows.items():
            expected_date = key[1]
            score_bucket = _bucket_for(float(article.get("score") or 0))
            if score_bucket is None:
                continue
            bucket_label = score_bucket.label
            mapping = _mapping_for_article(article)
            asset_symbol = _mapped_backtest_symbol(mapping)
            signal_dir = _article_signal_direction(article)
            article_date = _trade_date(int(article.get("created_at") or 0))
            if horizon == 0:
                raw_benchmark_row = _same_day_snapshot(
                    _BENCHMARK_SYMBOL, article_date
                )
                raw_asset_row = (
                    raw_benchmark_row
                    if asset_symbol == _BENCHMARK_SYMBOL
                    else _same_day_snapshot(asset_symbol, article_date)
                    if asset_symbol
                    else None
                )
            else:
                raw_benchmark_row = _future_snapshot(
                    _BENCHMARK_SYMBOL, article_date, horizon
                )
                raw_asset_row = (
                    raw_benchmark_row
                    if asset_symbol == _BENCHMARK_SYMBOL
                    else _future_snapshot(asset_symbol, article_date, horizon)
                    if asset_symbol
                    else None
                )
            benchmark_row = _exact_result_snapshot(
                raw_benchmark_row, expected_date, evaluation_at=evaluation_at,
            )
            asset_row = _exact_result_snapshot(
                raw_asset_row, expected_date, evaluation_at=evaluation_at,
            )
            if raw_benchmark_row and benchmark_row is None:
                unclusterable_metric_observations += sum(
                    value is not None
                    for value in (
                        _snapshot_direction(raw_benchmark_row),
                        _snapshot_return_pct(raw_benchmark_row),
                    )
                )
            if raw_asset_row and asset_row is None:
                unclusterable_metric_observations += sum(
                    value is not None
                    for value in (
                        _snapshot_direction(raw_asset_row),
                        _snapshot_return_pct(raw_asset_row),
                    )
                )

            benchmark_hit = None
            asset_hit = None
            signed_return = None
            signed_excess = None
            if signal_dir != 0:
                benchmark_dir = _snapshot_direction(benchmark_row)
                asset_dir = _snapshot_direction(asset_row)
                if benchmark_dir is not None:
                    benchmark_hit = signal_dir == benchmark_dir
                if asset_dir is not None:
                    asset_hit = signal_dir == asset_dir
                asset_return = _snapshot_return_pct(asset_row)
                benchmark_return = _snapshot_return_pct(benchmark_row)
                if asset_return is not None:
                    signed_return = signal_dir * asset_return
                if asset_return is not None and benchmark_return is not None:
                    signed_excess = signal_dir * (
                        asset_return - benchmark_return
                    )
            stored_prefix = (rank, bucket_label)
            accum["clustered_benchmark_direction"][horizon][key] = (
                *stored_prefix,
                benchmark_hit,
            )
            accum["clustered_asset_direction"][horizon][key] = (
                *stored_prefix,
                asset_hit,
            )
            accum["clustered_signal_return"][horizon][key] = (
                *stored_prefix,
                signed_return,
            )
            accum["clustered_excess_return"][horizon][key] = (
                *stored_prefix,
                signed_excess,
            )

    return (
        unclusterable_metric_observations,
        cluster_result_date_unavailable_observations,
    )


def _clustered_bucket_metrics(bucket_label: str, accum: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "cluster_method": "earliest_signal_per_event_result_day",
    }
    for owner, store in (
        ("asset", accum["clustered_asset_direction"]),
        ("benchmark", accum["clustered_benchmark_direction"]),
    ):
        for horizon in (0, 1, 2):
            horizon_label = "" if horizon == 0 else f"t{horizon}_"
            prefix = f"clustered_{owner}_{horizon_label}direction"
            entries = [
                (key, bool(stored[2]))
                for key, stored in store[horizon].items()
                if stored[1] == bucket_label and stored[2] is not None
            ]
            samples = len(entries)
            hits = sum(hit for _key, hit in entries)
            unique_days = len({key[1] for key, _hit in entries})
            metrics.update(
                {
                    f"{prefix}_samples": samples,
                    f"{prefix}_hits": hits,
                    f"{prefix}_unique_days": unique_days,
                    f"{prefix}_win_rate": (
                        round(hits / samples * 100.0, 1) if samples else None
                    ),
                    f"{prefix}_win_rate_ci95": _wilson_interval(hits, samples),
                    f"{prefix}_sample_sufficient": _sample_sufficient(
                        samples, unique_days
                    ),
                }
            )
    for metric_name, store in (
        ("signal_return", accum["clustered_signal_return"]),
        ("excess_return", accum["clustered_excess_return"]),
    ):
        for horizon in (0, 1, 2):
            horizon_label = "" if horizon == 0 else f"t{horizon}_"
            entries = [
                (key, float(stored[2]))
                for key, stored in store[horizon].items()
                if stored[1] == bucket_label and stored[2] is not None
            ]
            values = [value for _key, value in entries]
            unique_days = len({key[1] for key, _value in entries})
            metrics.update(
                {
                    f"clustered_{horizon_label}avg_{metric_name}_pct": (
                        round(sum(values) / len(values), 3) if values else None
                    ),
                    f"clustered_{horizon_label}{metric_name}_samples": len(values),
                    f"clustered_{horizon_label}{metric_name}_unique_days": unique_days,
                }
            )
    return metrics


def _empty_backtest_bucket(bucket_label: str, accum: dict[str, Any]) -> dict[str, Any]:
    """Preserve the complete zero-sample JSON shape, including null metrics."""
    empty_row = {
        "bucket": bucket_label,
        "samples": 0,
        "avg_score": 0,
        "theme_mapping_coverage": 0,
        "mapping_coverage": 0,
        "proxy_coverage": 0,
        "theme_follow_rate": None,
        "theme_follow_samples": 0,
        "theme_follow_pending_samples": 0,
        "theme_follow_unique_days": 0,
        "theme_follow_sample_sufficient": False,
        "avg_confidence": 0,
        "unique_signal_days": 0,
        "direction_win_rate": None,
        "t1_direction_win_rate": None,
        "t2_direction_win_rate": None,
        "direction_samples": 0,
        "t1_direction_samples": 0,
        "t2_direction_samples": 0,
        "direction_unique_days": 0,
        "t1_direction_unique_days": 0,
        "t2_direction_unique_days": 0,
        "direction_win_rate_ci95": None,
        "t1_direction_win_rate_ci95": None,
        "t2_direction_win_rate_ci95": None,
        "direction_sample_sufficient": False,
        "t1_direction_sample_sufficient": False,
        "t2_direction_sample_sufficient": False,
        "asset_direction_win_rate": None,
        "asset_t1_direction_win_rate": None,
        "asset_t2_direction_win_rate": None,
        "asset_direction_samples": 0,
        "asset_t1_direction_samples": 0,
        "asset_t2_direction_samples": 0,
        "asset_direction_unique_days": 0,
        "asset_t1_direction_unique_days": 0,
        "asset_t2_direction_unique_days": 0,
        "asset_direction_win_rate_ci95": None,
        "asset_t1_direction_win_rate_ci95": None,
        "asset_t2_direction_win_rate_ci95": None,
        "asset_direction_sample_sufficient": False,
        "asset_t1_direction_sample_sufficient": False,
        "asset_t2_direction_sample_sufficient": False,
        "benchmark_direction_win_rate": None,
        "benchmark_t1_direction_win_rate": None,
        "benchmark_t2_direction_win_rate": None,
        "benchmark_direction_samples": 0,
        "benchmark_t1_direction_samples": 0,
        "benchmark_t2_direction_samples": 0,
        "benchmark_direction_unique_days": 0,
        "benchmark_t1_direction_unique_days": 0,
        "benchmark_t2_direction_unique_days": 0,
        "benchmark_direction_win_rate_ci95": None,
        "benchmark_t1_direction_win_rate_ci95": None,
        "benchmark_t2_direction_win_rate_ci95": None,
        "benchmark_direction_sample_sufficient": False,
        "benchmark_t1_direction_sample_sufficient": False,
        "benchmark_t2_direction_sample_sufficient": False,
        "avg_signal_return_pct": None,
        "t1_avg_signal_return_pct": None,
        "t2_avg_signal_return_pct": None,
        "avg_excess_return_pct": None,
        "t1_avg_excess_return_pct": None,
        "t2_avg_excess_return_pct": None,
        "signal_return_samples": 0,
        "t1_signal_return_samples": 0,
        "t2_signal_return_samples": 0,
        "excess_return_samples": 0,
        "t1_excess_return_samples": 0,
        "t2_excess_return_samples": 0,
        "top_themes": [],
    }
    empty_row.update(_clustered_bucket_metrics(bucket_label, accum))
    return empty_row


def _populated_backtest_bucket(
    bucket_label: str, items: list[dict[str, Any]],
    theme_mature_cutoff: int, accum: dict[str, Any],
) -> dict[str, Any]:
    """Render one score bucket without dropping missing or negative outcomes."""
    sample_count = len(items)
    theme_mapped_count = sum(1 for x in items if x.get("mapping", {}).get("mapped_theme"))
    proxy_mapped_count = sum(
        1 for x in items if _mapped_backtest_symbol(x.get("mapping", {})) is not None
    )
    confidence_values = [float(x.get("score_confidence") or 0) for x in items]
    unique_signal_days = {
        _trade_date(int(item.get("created_at") or 0)) for item in items
        if int(item.get("created_at") or 0) > 0
    }
    mature_theme_items = [
        item for item in items
        if 0 < int(item.get("created_at") or 0) <= theme_mature_cutoff
    ]
    mature_theme_count = len(mature_theme_items)
    mature_theme_days = {
        _trade_date(int(item["created_at"])) for item in mature_theme_items
    }
    theme_counts: dict[str, int] = defaultdict(int)
    for item in items:
        theme_counts[str(item.get("mapped_theme") or "未分类")] += 1
    top_themes = sorted(theme_counts.items(), key=lambda kv: kv[1], reverse=True)[:4]
    bucket_row = {
        "bucket": bucket_label,
        "samples": sample_count,
        "avg_score": round(sum(float(x.get("score") or 0) for x in items) / sample_count, 2),
        "theme_mapping_coverage": round(theme_mapped_count / sample_count * 100, 1),
        # Compatibility field now has the honest, outcome-capable meaning.
        "mapping_coverage": round(proxy_mapped_count / sample_count * 100, 1),
        "proxy_coverage": round(proxy_mapped_count / sample_count * 100, 1),
        "theme_follow_rate": (
            round(accum["future_theme_hits"][bucket_label] / mature_theme_count * 100, 1)
            if mature_theme_count else None
        ),
        "theme_follow_samples": mature_theme_count,
        "theme_follow_pending_samples": sample_count - mature_theme_count,
        "theme_follow_unique_days": len(mature_theme_days),
        "theme_follow_sample_sufficient": _sample_sufficient(
            mature_theme_count, len(mature_theme_days)
        ),
        "avg_confidence": round(sum(confidence_values) / sample_count, 3),
        "unique_signal_days": len(unique_signal_days),
        # Compatibility: the original generic direction fields remain the
        # broad-market benchmark.  Explicit asset_* fields carry the fixed
        # mapped-theme result.
        "direction_win_rate": round(accum["benchmark_hits"][0][bucket_label] / accum["benchmark_total"][0][bucket_label] * 100, 1) if accum["benchmark_total"][0][bucket_label] else None,
        "t1_direction_win_rate": round(accum["benchmark_hits"][1][bucket_label] / accum["benchmark_total"][1][bucket_label] * 100, 1) if accum["benchmark_total"][1][bucket_label] else None,
        "t2_direction_win_rate": round(accum["benchmark_hits"][2][bucket_label] / accum["benchmark_total"][2][bucket_label] * 100, 1) if accum["benchmark_total"][2][bucket_label] else None,
        "direction_samples": accum["benchmark_total"][0][bucket_label],
        "t1_direction_samples": accum["benchmark_total"][1][bucket_label],
        "t2_direction_samples": accum["benchmark_total"][2][bucket_label],
        "direction_unique_days": len(accum["benchmark_unique_days"][0][bucket_label]),
        "t1_direction_unique_days": len(accum["benchmark_unique_days"][1][bucket_label]),
        "t2_direction_unique_days": len(accum["benchmark_unique_days"][2][bucket_label]),
        "direction_win_rate_ci95": _wilson_interval(accum["benchmark_hits"][0][bucket_label], accum["benchmark_total"][0][bucket_label]),
        "t1_direction_win_rate_ci95": _wilson_interval(accum["benchmark_hits"][1][bucket_label], accum["benchmark_total"][1][bucket_label]),
        "t2_direction_win_rate_ci95": _wilson_interval(accum["benchmark_hits"][2][bucket_label], accum["benchmark_total"][2][bucket_label]),
        "direction_sample_sufficient": _sample_sufficient(accum["benchmark_total"][0][bucket_label], len(accum["benchmark_unique_days"][0][bucket_label])),
        "t1_direction_sample_sufficient": _sample_sufficient(accum["benchmark_total"][1][bucket_label], len(accum["benchmark_unique_days"][1][bucket_label])),
        "t2_direction_sample_sufficient": _sample_sufficient(accum["benchmark_total"][2][bucket_label], len(accum["benchmark_unique_days"][2][bucket_label])),
        "asset_direction_win_rate": round(accum["direction_hits"][0][bucket_label] / accum["direction_total"][0][bucket_label] * 100, 1) if accum["direction_total"][0][bucket_label] else None,
        "asset_t1_direction_win_rate": round(accum["direction_hits"][1][bucket_label] / accum["direction_total"][1][bucket_label] * 100, 1) if accum["direction_total"][1][bucket_label] else None,
        "asset_t2_direction_win_rate": round(accum["direction_hits"][2][bucket_label] / accum["direction_total"][2][bucket_label] * 100, 1) if accum["direction_total"][2][bucket_label] else None,
        "asset_direction_samples": accum["direction_total"][0][bucket_label],
        "asset_t1_direction_samples": accum["direction_total"][1][bucket_label],
        "asset_t2_direction_samples": accum["direction_total"][2][bucket_label],
        "asset_direction_unique_days": len(accum["direction_unique_days"][0][bucket_label]),
        "asset_t1_direction_unique_days": len(accum["direction_unique_days"][1][bucket_label]),
        "asset_t2_direction_unique_days": len(accum["direction_unique_days"][2][bucket_label]),
        "asset_direction_win_rate_ci95": _wilson_interval(accum["direction_hits"][0][bucket_label], accum["direction_total"][0][bucket_label]),
        "asset_t1_direction_win_rate_ci95": _wilson_interval(accum["direction_hits"][1][bucket_label], accum["direction_total"][1][bucket_label]),
        "asset_t2_direction_win_rate_ci95": _wilson_interval(accum["direction_hits"][2][bucket_label], accum["direction_total"][2][bucket_label]),
        "asset_direction_sample_sufficient": _sample_sufficient(accum["direction_total"][0][bucket_label], len(accum["direction_unique_days"][0][bucket_label])),
        "asset_t1_direction_sample_sufficient": _sample_sufficient(accum["direction_total"][1][bucket_label], len(accum["direction_unique_days"][1][bucket_label])),
        "asset_t2_direction_sample_sufficient": _sample_sufficient(accum["direction_total"][2][bucket_label], len(accum["direction_unique_days"][2][bucket_label])),
        "benchmark_direction_win_rate": round(accum["benchmark_hits"][0][bucket_label] / accum["benchmark_total"][0][bucket_label] * 100, 1) if accum["benchmark_total"][0][bucket_label] else None,
        "benchmark_t1_direction_win_rate": round(accum["benchmark_hits"][1][bucket_label] / accum["benchmark_total"][1][bucket_label] * 100, 1) if accum["benchmark_total"][1][bucket_label] else None,
        "benchmark_t2_direction_win_rate": round(accum["benchmark_hits"][2][bucket_label] / accum["benchmark_total"][2][bucket_label] * 100, 1) if accum["benchmark_total"][2][bucket_label] else None,
        "benchmark_direction_samples": accum["benchmark_total"][0][bucket_label],
        "benchmark_t1_direction_samples": accum["benchmark_total"][1][bucket_label],
        "benchmark_t2_direction_samples": accum["benchmark_total"][2][bucket_label],
        "benchmark_direction_unique_days": len(accum["benchmark_unique_days"][0][bucket_label]),
        "benchmark_t1_direction_unique_days": len(accum["benchmark_unique_days"][1][bucket_label]),
        "benchmark_t2_direction_unique_days": len(accum["benchmark_unique_days"][2][bucket_label]),
        "benchmark_direction_win_rate_ci95": _wilson_interval(accum["benchmark_hits"][0][bucket_label], accum["benchmark_total"][0][bucket_label]),
        "benchmark_t1_direction_win_rate_ci95": _wilson_interval(accum["benchmark_hits"][1][bucket_label], accum["benchmark_total"][1][bucket_label]),
        "benchmark_t2_direction_win_rate_ci95": _wilson_interval(accum["benchmark_hits"][2][bucket_label], accum["benchmark_total"][2][bucket_label]),
        "benchmark_direction_sample_sufficient": _sample_sufficient(accum["benchmark_total"][0][bucket_label], len(accum["benchmark_unique_days"][0][bucket_label])),
        "benchmark_t1_direction_sample_sufficient": _sample_sufficient(accum["benchmark_total"][1][bucket_label], len(accum["benchmark_unique_days"][1][bucket_label])),
        "benchmark_t2_direction_sample_sufficient": _sample_sufficient(accum["benchmark_total"][2][bucket_label], len(accum["benchmark_unique_days"][2][bucket_label])),
        "avg_signal_return_pct": round(accum["signal_return_sum"][0][bucket_label] / accum["signal_return_total"][0][bucket_label], 3) if accum["signal_return_total"][0][bucket_label] else None,
        "t1_avg_signal_return_pct": round(accum["signal_return_sum"][1][bucket_label] / accum["signal_return_total"][1][bucket_label], 3) if accum["signal_return_total"][1][bucket_label] else None,
        "t2_avg_signal_return_pct": round(accum["signal_return_sum"][2][bucket_label] / accum["signal_return_total"][2][bucket_label], 3) if accum["signal_return_total"][2][bucket_label] else None,
        "avg_excess_return_pct": round(accum["excess_return_sum"][0][bucket_label] / accum["excess_return_total"][0][bucket_label], 3) if accum["excess_return_total"][0][bucket_label] else None,
        "t1_avg_excess_return_pct": round(accum["excess_return_sum"][1][bucket_label] / accum["excess_return_total"][1][bucket_label], 3) if accum["excess_return_total"][1][bucket_label] else None,
        "t2_avg_excess_return_pct": round(accum["excess_return_sum"][2][bucket_label] / accum["excess_return_total"][2][bucket_label], 3) if accum["excess_return_total"][2][bucket_label] else None,
        "signal_return_samples": accum["signal_return_total"][0][bucket_label],
        "t1_signal_return_samples": accum["signal_return_total"][1][bucket_label],
        "t2_signal_return_samples": accum["signal_return_total"][2][bucket_label],
        "excess_return_samples": accum["excess_return_total"][0][bucket_label],
        "t1_excess_return_samples": accum["excess_return_total"][1][bucket_label],
        "t2_excess_return_samples": accum["excess_return_total"][2][bucket_label],
        "top_themes": [{"theme": theme, "count": count} for theme, count in top_themes],
    }
    bucket_row.update(_clustered_bucket_metrics(bucket_label, accum))
    return bucket_row


def _build_backtest_bucket_rows(
    evaluation_at: int, accum: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    theme_mature_cutoff = evaluation_at - _THEME_OBSERVATION_SECONDS
    for bucket in BUCKETS:
        items = accum["by_bucket"].get(bucket.label, [])
        if items:
            rows.append(_populated_backtest_bucket(
                bucket.label, items, theme_mature_cutoff, accum
            ))
        else:
            rows.append(_empty_backtest_bucket(bucket.label, accum))
    return rows


def _backtest_sample_counts(inputs: _ScoreBacktestRun) -> dict[str, Any]:
    """Summarize coverage without admitting archived contracts into this query."""
    total = len(inputs.articles)
    invalid_count = len(inputs.invalid_articles)
    invalid_score_count = sum(1 for article in inputs.invalid_articles if _is_invalid_score(article))
    deduped_total = len(inputs.deduped_all)
    theme_mapped_total = sum(
        1 for article in inputs.articles if _mapping_for_article(article).get("mapped_theme")
    )
    proxy_mapped_total = sum(
        1
        for article in inputs.articles
        if _mapped_backtest_symbol(_mapping_for_article(article)) is not None
    )
    proxy_coverage_raw = proxy_mapped_total / total * 100.0 if total else 0.0
    proxy_coverage = round(proxy_coverage_raw, 1)
    noise_rate = round(invalid_count / deduped_total * 100, 1) if deduped_total else 0
    cluster_identity_fallback_samples = sum(
        _signal_cluster_identity(article).startswith("legacy-link:")
        for article in inputs.articles
    )
    return {
        "total": total, "invalid_count": invalid_count,
        "invalid_score_count": invalid_score_count, "deduped_total": deduped_total,
        "theme_mapped_total": theme_mapped_total, "proxy_mapped_total": proxy_mapped_total,
        "proxy_coverage_raw": proxy_coverage_raw, "proxy_coverage": proxy_coverage,
        "noise_rate": noise_rate,
        "cluster_identity_fallback_samples": cluster_identity_fallback_samples,
    }


def _backtest_optimization(
    inputs: _ScoreBacktestRun, accum: dict[str, Any], counts: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Keep formal, exploratory and legacy control-cohort admission separate."""
    # Tuning admission is stricter than descriptive display: only the durable
    # point-in-time signal ledger may qualify.  Legacy-cache rows can still be
    # clustered for an honest preview, but can never unlock optimization.
    # Only the frozen production-selected query may advance the production
    # posterior gate.  An explicit min_score view can contain rejected rows and
    # is exploratory even when it happens to span many events/result days.
    optimization_keys = (
        {
            key
            for key, stored in accum["clustered_asset_direction"][1].items()
            if str(key[0]).startswith("event:") and stored[2] is not None
        }
        if inputs.history.get("data_source") == "signal_snapshots" and inputs.min_score is None
        else set()
    )
    cohort_history = inputs.history.get("cohort_history")
    if not isinstance(cohort_history, dict):
        cohort_history = {}
    recorded_unselected = (
        int(cohort_history.get("current_unselected_signals") or 0)
        if inputs.history.get("data_source") == "signal_snapshots"
        else 0
    )
    if inputs.history.get("data_source") == "signal_snapshots" and inputs.min_score is None:
        control_cohort = _settle_score_control_cohort(
            inputs.control_population,
            strategy_version=str(inputs.cfg.get("scoring_strategy_version") or ""),
            mapping_version=inputs.mapping_version,
            evaluation_at=inputs.evaluation_at,
        )
    else:
        control_cohort = _settle_score_control_cohort(
            [],
            strategy_version=str(inputs.cfg.get("scoring_strategy_version") or ""),
            mapping_version=inputs.mapping_version,
            evaluation_at=inputs.evaluation_at,
        )
        control_cohort.update(
            {
                "status": (
                    "exploratory_disabled"
                    if inputs.min_score is not None
                    else "legacy_unavailable"
                ),
                "status_label": (
                    "探索视图不参与独立对照"
                    if inputs.min_score is not None
                    else "旧缓存无冻结入选状态"
                ),
                "ready_for_tuning": False,
            }
        )
    optimization_state = _score_optimization_state(
        independent_events=len({key[0] for key in optimization_keys}),
        independent_result_days=len({key[1] for key in optimization_keys}),
        proxy_coverage=counts["proxy_coverage_raw"],
        # A broad min_score query is an exploratory view, not an independently
        # settled selected-vs-unselected control result.  It must never unlock
        # a tuning candidate by itself.
        control_cohort_available=bool(control_cohort.get("ready_for_tuning")),
        control_cohort_recorded=recorded_unselected > 0,
        control_cohort_unselected_signals=recorded_unselected,
        control_query_mode=(
            "exploratory_full_score"
            if inputs.min_score is not None and float(inputs.min_score) == 0.0
            else "exploratory_threshold"
            if inputs.min_score is not None
            else "production_selected"
        ),
    )
    return optimization_state, control_cohort, cohort_history


def _backtest_history_presentation(
    inputs: _ScoreBacktestRun, bucket_rows: list[dict[str, Any]],
    cohort_history: dict[str, Any],
) -> dict[str, Any]:
    """Describe observed history, with archived versions explicitly separate."""
    has_direction_data = any(
        row.get("direction_win_rate") is not None
        or row.get("t1_direction_win_rate") is not None
        or row.get("t2_direction_win_rate") is not None
        or row.get("asset_direction_win_rate") is not None
        or row.get("asset_t1_direction_win_rate") is not None
        or row.get("asset_t2_direction_win_rate") is not None
        or row.get("benchmark_direction_win_rate") is not None
        or row.get("benchmark_t1_direction_win_rate") is not None
        or row.get("benchmark_t2_direction_win_rate") is not None
        for row in bucket_rows
    )

    sample_timestamps = sorted(
        int(article.get("created_at") or 0)
        for article in inputs.articles
        if int(article.get("created_at") or 0) > 0
    )
    actual_start_date = (
        datetime.fromtimestamp(sample_timestamps[0], _MARKET_TZ).date().isoformat()
        if sample_timestamps else None
    )
    actual_end_date = (
        datetime.fromtimestamp(sample_timestamps[-1], _MARKET_TZ).date().isoformat()
        if sample_timestamps else None
    )
    actual_window_days = 0
    if actual_start_date and actual_end_date:
        actual_window_days = (
            datetime.strptime(actual_end_date, "%Y-%m-%d").date()
            - datetime.strptime(actual_start_date, "%Y-%m-%d").date()
        ).days + 1
    data_source = str(inputs.history.get("data_source") or "legacy_news_cache")
    collection_started_at = int(inputs.history.get("collection_started_at") or 0)
    strategy_history_started_at = int(inputs.history.get("first_available_at") or 0)
    requested_cutoff = int(time.time()) - max(1, inputs.days) * 86400
    if data_source == "signal_snapshots":
        history_status = (
            "ready"
            if strategy_history_started_at and strategy_history_started_at <= requested_cutoff
            else "accumulating"
        )
        validation_mode = "historical_association" if has_direction_data else "snapshot_accumulating"
    else:
        history_status = "legacy"
        validation_mode = "legacy_accumulating"
    # Preserve the existing JSON contract for callers that branch on ``mode``;
    # the precise, non-promotional semantics live in validation_mode/label/note.
    mode = "realized_market_direction" if has_direction_data else "proxy_news_corpus_cleaned"
    if data_source == "signal_snapshots":
        note = (
            "历史关联验证使用评分当时冻结的方向、阈值与资产代理；T0 仅描述当日收盘相对前收的方向，"
            "不是信号发布后的收益；T+1/T+2 是目标交易日的单日涨跌，"
            "不是从信号时点开始的累计收益。主题到资产代理是规则映射，存在主观假设；"
            "收益均为毛收益，价格变化包含隔夜跳空，但未计交易成本、滑点和成交约束，"
            "非交易策略回测。"
            "方向展示采用同一冻结事件/结果日只保留最早信号的聚类口径，"
            "并报告 Wilson 95%CI；该区间未校正同一结果日跨事件相关性，"
            "调参仍须按结果日分块留出验证。样本充分须同时满足聚类事件数和独立结果交易日门槛。"
        )
        if history_status == "accumulating":
            note += " 当前精确策略的前瞻样本仍在积累，实际覆盖区间短于请求窗口。"
        archived_selected = int(cohort_history.get("archived_selected_signals") or 0)
        version_count = int(cohort_history.get("version_count") or 0)
        if archived_selected > 0:
            note += (
                f" 同模型家族的历史账本仍保留，共{version_count}个策略指纹；"
                f"其中{archived_selected}条旧指纹入选信号仅作分版本参考，"
                "不并入当前策略的正式胜率。"
            )
    else:
        note = (
            "长期信号快照尚无当前策略样本；当前仅展示七天短缓存的 legacy 关联结果，"
            "不代表请求的完整历史窗口，也不用于正式胜率结论。"
        )
    return {
        "actual_start_date": actual_start_date, "actual_end_date": actual_end_date,
        "actual_window_days": actual_window_days,
        "collection_started_at": collection_started_at,
        "strategy_history_started_at": strategy_history_started_at,
        "data_source": data_source, "history_status": history_status,
        "validation_mode": validation_mode, "mode": mode, "note": note,
    }


def _backtest_audited_compatibility(
    inputs: _ScoreBacktestRun, data_source: str, cohort_history: dict[str, Any]
) -> dict[str, Any] | None:
    """Load the existing side-by-side preview without promoting its samples."""
    bridge_preview = load_bridge_preview(
        str(inputs.cfg.get("scoring_strategy_version") or ""),
        inputs.mapping_version,
    )
    audited_compatibility = None
    if bridge_preview is not None:
        exact_formal_eligible = (
            data_source == "signal_snapshots" and inputs.min_score is None
        )
        audited_compatibility = {
            "scope": "side_by_side_preview",
            "formal_gate_scope": "exact_current_only",
            "formal_gate_effect": False,
            "auto_apply": False,
            "exact_current": {
                "strategy_version": str(
                    inputs.cfg.get("scoring_strategy_version") or ""
                ),
                "mapping_version": inputs.mapping_version,
                "scored": int(
                    cohort_history.get("current_scored_signals") or 0
                ),
                "selected": int(
                    cohort_history.get("current_selected_signals") or 0
                ),
                "unselected": int(
                    cohort_history.get("current_unselected_signals") or 0
                ),
                "selected_signal_days": int(
                    cohort_history.get("current_selected_signal_days") or 0
                ),
                "formal_eligible": exact_formal_eligible,
                "included_in_current_gate": exact_formal_eligible,
            },
            "bridge_preview": bridge_preview,
        }
    return audited_compatibility


def _assemble_score_backtest_result(
    inputs: _ScoreBacktestRun, accum: dict[str, Any], counts: dict[str, Any],
    presentation: dict[str, Any], cohort_history: dict[str, Any],
    optimization_state: dict[str, Any], control_cohort: dict[str, Any],
    audited_compatibility: dict[str, Any] | None,
    bucket_rows: list[dict[str, Any]], theme_win_rates: dict[str, Any],
) -> dict[str, Any]:
    """Preserve the public descriptive-result contract and generation timing."""
    return {
        "mode": presentation["mode"],
        "validation_mode": presentation["validation_mode"],
        "label": "历史关联验证（毛收益，非交易策略）",
        "history_status": presentation["history_status"],
        "data_source": presentation["data_source"],
        "note": presentation["note"],
        "days": inputs.days,
        "requested_days": inputs.days,
        "actual_start_date": presentation["actual_start_date"],
        "actual_end_date": presentation["actual_end_date"],
        "actual_window_days": presentation["actual_window_days"],
        "collection_started_at": presentation["collection_started_at"],
        "strategy_history_started_at": presentation["strategy_history_started_at"],
        "strategy_version": str(inputs.cfg.get("scoring_strategy_version") or ""),
        "mapping_version": inputs.mapping_version,
        "cohort_history": cohort_history,
        "selection_policy": (
            "stored_aggregation_threshold" if inputs.min_score is None and presentation["data_source"] == "signal_snapshots"
            else "explicit_min_score" if inputs.min_score is not None
            else "legacy_current_threshold"
        ),
        "min_statistical_samples": _MIN_STATISTICAL_SAMPLES,
        "min_independent_result_days": _MIN_INDEPENDENT_RESULT_DAYS,
        "ci_method": "article_level_wilson_unadjusted_for_intraday_clustering",
        "clustered_ci_method": "event_result_day_clustered_wilson_earliest_signal",
        "clustered_ci_dependence": "not_result_day_cluster_robust",
        "inference_unit": "canonical_event_result_day",
        "preferred_metric_prefix": "clustered_",
        "cluster_identity_fallback_samples": counts["cluster_identity_fallback_samples"],
        "unclusterable_metric_observations": accum["unclusterable_metric_observations"],
        "cluster_result_date_unavailable_observations": (
            accum["cluster_result_date_unavailable_observations"]
        ),
        "min_score": inputs.min_score,
        "theme_observation_as_of": inputs.evaluation_at,
        "scored_signal_samples": int(inputs.history.get("count") or 0),
        "raw_samples": len(inputs.raw_articles),
        "deduped_samples": counts["total"],
        "deduped_total_before_filter": counts["deduped_total"],
        "invalid_samples": counts["invalid_count"],
        "invalid_score_samples": counts["invalid_score_count"],
        "noise_rate": counts["noise_rate"],
        "dedupe_removed": max(0, len(inputs.raw_articles) - counts["deduped_total"]),
        "theme_mapping_coverage": round(counts["theme_mapped_total"] / counts["total"] * 100, 1) if counts["total"] else 0,
        "mapping_coverage": counts["proxy_coverage"],
        "proxy_coverage": counts["proxy_coverage"],
        "proxy_mapped_samples": counts["proxy_mapped_total"],
        "unique_signal_days": len({
            _trade_date(int(article.get("created_at") or 0))
            for article in inputs.articles if int(article.get("created_at") or 0) > 0
        }),
        "benchmark_symbol": _BENCHMARK_SYMBOL,
        "optimization": optimization_state,
        "control_cohort": control_cohort,
        **(
            {"audited_compatibility": audited_compatibility}
            if audited_compatibility is not None
            else {}
        ),
        "eligibility_funnel": _build_score_eligibility_funnel(
            data_source=presentation["data_source"],
            min_score=inputs.min_score,
            history=inputs.history,
            cohort_history=cohort_history,
            query_rows=len(inputs.raw_articles),
            wire_deduped=counts["deduped_total"],
            valid_score=counts["total"],
            proxy_mapped=counts["proxy_mapped_total"],
            optimization_state=optimization_state,
            control_cohort=control_cohort,
        ),
        "buckets": bucket_rows,
        "theme_win_rates": theme_win_rates,
        "generated_at": int(time.time()),
    }


def run_score_backtest(days: int = 90, min_score: float | None = None) -> dict[str, Any]:
    """Run descriptive association analysis on AI score quality.

    Computes historical direction accuracy and gross return association between
    AI-scored signals and subsequent market price movements. This is NOT an
    execution-aware trading backtest - it measures association, not causality.

    The analysis uses frozen point-in-time signal snapshots from the
    backtest_signals ledger, bound to exact scoring strategy fingerprint x
    mapping version contracts. Results are never merged across contracts.

    Args:
        days: Lookback window in calendar days (default: 90). Actual coverage
            depends on signal availability and may be shorter.
        min_score: Optional score threshold filter. When None (default), uses
            the stored aggregation threshold. Explicit 0 creates an exploratory
            view that cannot advance optimization gates.

    Returns:
        dict with keys:
            - history_status: accumulating | mature | insufficient
            - selection_policy: How the score threshold was determined
            - buckets: List of score bucket results with direction/return stats
            - optimization: Gate readiness for score threshold tuning
            - control_cohort: Independent T+1 selected vs unselected comparison
            - deduped_samples: Number of analyzed signals
            - unique_signal_days: Independent signal calendar days
            - proxy_coverage: Percent of signals with valid market proxy
            - cohort_history: Contract family statistics

    Statistical thresholds:
        - Descriptive: >=30 samples, >=30 independent result days
        - Optimization gate: >=100 independent events, >=60 result days,
          >=80 percent proxy coverage, control cohort recorded

    Raises:
        RuntimeError: If mapping contract is unavailable.

    See Also:
        docs/model-quality.md for the complete governance framework.
    """

    inputs = _load_score_backtest_run(days, min_score)
    accum = _initialize_metric_accumulators()
    _accumulate_backtest_articles(inputs.articles, inputs.evaluation_at, accum)
    unclusterable, unavailable_dates = _accumulate_backtest_clusters(
        inputs.articles, accum, evaluation_at=inputs.evaluation_at,
    )
    accum["unclusterable_metric_observations"] = unclusterable
    accum["cluster_result_date_unavailable_observations"] = unavailable_dates
    bucket_rows = _build_backtest_bucket_rows(inputs.evaluation_at, accum)
    counts = _backtest_sample_counts(inputs)
    optimization, control, cohort_history = _backtest_optimization(inputs, accum, counts)
    theme_win_rates = _theme_future_win_rate(inputs.articles, evaluation_at=inputs.evaluation_at)
    presentation = _backtest_history_presentation(inputs, bucket_rows, cohort_history)
    audited_compatibility = _backtest_audited_compatibility(
        inputs, presentation["data_source"], cohort_history
    )
    return _assemble_score_backtest_result(
        inputs, accum, counts, presentation, cohort_history, optimization, control,
        audited_compatibility, bucket_rows, theme_win_rates,
    )


_BACKTEST_DIRECTION_PREFIXES = (
    "asset_direction",
    "asset_t1_direction",
    "asset_t2_direction",
    "benchmark_direction",
    "benchmark_t1_direction",
    "benchmark_t2_direction",
)


def _effective_metric_prefix(row: dict[str, Any], prefix: str) -> str:
    clustered = f"clustered_{prefix}"
    return clustered if f"{clustered}_samples" in row else prefix


def _active_backtest_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    active_rows = []
    for row in result.get("buckets") or []:
        if not isinstance(row, dict):
            continue
        try:
            sample_count = int(row.get("samples") or 0)
        except (TypeError, ValueError):
            sample_count = 0
        if sample_count > 0:
            active_rows.append(row)
    return active_rows


def _backtest_has_publishable_metric(active_rows: list[dict[str, Any]]) -> bool:
    return any(row.get("theme_follow_sample_sufficient") for row in active_rows) or any(
        bool(row.get(f"{_effective_metric_prefix(row, prefix)}_sample_sufficient"))
        for row in active_rows
        for prefix in _BACKTEST_DIRECTION_PREFIXES
    )


def _render_score_optimization_html(result: dict[str, Any], *, compact: bool) -> str:
    state = result.get("optimization") or _score_optimization_state(
        independent_events=0,
        independent_result_days=0,
        proxy_coverage=float(result.get("proxy_coverage") or 0),
    )
    requirements = state.get("requirements") or {}
    stage = str(state.get("stage") or "insufficient")
    color = "#60a5fa" if stage in {"candidate", "posterior_ready"} else "#fbbf24"
    progress = (
        f"{str(state.get('scope_label') or '当前统计窗口')} · 有效事件 {int(state.get('independent_events') or 0)}/"
        f"{int(requirements.get('independent_events') or _OPTIMIZATION_MIN_INDEPENDENT_EVENTS)} · "
        f"结果日 {int(state.get('independent_result_days') or 0)}/"
        f"{int(requirements.get('independent_result_days') or _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS)} · "
        f"代理覆盖 {float(state.get('proxy_coverage') or 0):.2f}%/"
        f"{float(requirements.get('proxy_coverage') or _OPTIMIZATION_MIN_PROXY_COVERAGE):.2f}% · "
        f"样本组 {str(state.get('cohort_label') or '仅生产入选信号')}"
    )
    roadmap = " → ".join(
        str(item.get("label") or "") for item in state.get("roadmap") or []
    )
    spacing = "margin:8px 0 0;" if compact else "margin:0 0 10px;"
    return (
        f'<div class="score-optimization-state" style="{spacing}padding:8px 10px;border-radius:10px;'
        f'background:{color}12;border:1px solid {color}2e;font-size:11px;line-height:1.55;">'
        f'<div><b style="color:{color};margin-right:6px;">调参研究资格：{escape_html(state.get("stage_label") or "调参样本待积累")}</b>'
        f'{escape_html(state.get("recommendation") or "保持现行策略。")} '
        '<span style="color:var(--color-text-subdue,inherit);">不自动应用</span></div>'
        f'<div style="color:var(--color-text-subdue,inherit);">{escape_html(progress)}'
        + '</div><div>指标展示与调参资格分开判断；样本达标不代表模型质量合格。</div>'
        + (f'<details><summary>后续研究流程</summary>{escape_html(roadmap)}</details>' if roadmap and not compact else "")
        + '</div>'
    )


def _render_score_cohort_history_html(
    result: dict[str, Any], *, compact: bool
) -> str:
    history = result.get("cohort_history")
    if not isinstance(history, dict):
        return ""
    current_scored = int(history.get("current_scored_signals") or 0)
    family_scored = int(history.get("family_scored_signals") or 0)
    if current_scored <= 0 and family_scored <= 0:
        return ""
    current_selected = int(history.get("current_selected_signals") or 0)
    current_days = int(history.get("current_selected_signal_days") or 0)
    family_selected = int(history.get("family_selected_signals") or 0)
    family_days = int(history.get("family_selected_signal_days") or 0)
    versions = int(history.get("version_count") or 0)
    archived_selected = int(history.get("archived_selected_signals") or 0)
    family = str(history.get("family") or "当前模型家族")
    current_version = str(history.get("current_version") or "")
    fingerprint = current_version.split(":", 1)[-1] if current_version else "—"

    if compact:
        archived = (
            f"；旧指纹入选 {archived_selected} 条仅作版本参考"
            if archived_selected > 0 else ""
        )
        text = (
            f"账本分层：当前指纹 {fingerprint}，全分数 {current_scored} / 入选 {current_selected} / {current_days}日；"
            f"{family} 家族全分数 {family_scored} / 入选 {family_selected} / {family_days}日"
            f"（{versions}个指纹）{archived}。"
        )
        return (
            '<div class="score-cohort-compact" style="margin:0 0 8px;padding:7px 9px;border-radius:9px;'
            'background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.22);'
            'font-size:10px;line-height:1.5;color:var(--color-text-subdue,inherit);">'
            f'{escape_html(text)}</div>'
        )

    archived_note = (
        f"旧指纹仍保留 {archived_selected} 条入选信号，供版本对比和漂移检查；"
        "它们不会被混入当前策略胜率。"
        if archived_selected > 0
        else "当前尚无需要隔离的旧指纹入选信号。"
    )
    contract_note = (
        "每次评分契约变更都会建立新的正式队列；新规则应先走影子验证，避免频繁重置当前队列。"
        if versions > 1
        else ""
    )
    return (
        '<section class="score-cohort-history">'
        '<div class="score-cohort-title">样本账本分层</div>'
        '<div class="score-cohort-grid">'
        '<div class="score-cohort-card"><span>当前策略正式队列</span>'
        f'<b>全分数 {escape_html(current_scored)} · 入选 {escape_html(current_selected)}</b>'
        f'<small>指纹 {escape_html(fingerprint)} · 入选信号日 {escape_html(current_days)}</small></div>'
        '<div class="score-cohort-card"><span>同模型家族历史参考</span>'
        f'<b>全分数 {escape_html(family_scored)} · 入选 {escape_html(family_selected)}</b>'
        f'<small>{escape_html(family)} · {escape_html(versions)}个指纹 · 入选信号日 {escape_html(family_days)}</small></div>'
        '</div>'
        f'<div class="score-cohort-note">{escape_html(archived_note)} '
        f'{escape_html(contract_note)} '
        '未入选评分同样保留在全分数账本中；需按全分数对照口径独立结算后，才可用于阈值候选。</div>'
        '</section>'
    )


def _render_audited_compatibility_html(
    result: dict[str, Any], *, compact: bool
) -> str:
    comparison = result.get("audited_compatibility")
    if not isinstance(comparison, dict):
        return ""
    exact = comparison.get("exact_current")
    bridge = comparison.get("bridge_preview")
    if not isinstance(exact, dict) or not isinstance(bridge, dict):
        return ""
    # Fail closed if a caller constructs a bridge payload that could be
    # mistaken for formal evidence.  The renderer never promotes such a row.
    if (
        comparison.get("formal_gate_effect") is not False
        or comparison.get("auto_apply") is not False
        or bridge.get("formal_eligible") is not False
        or bridge.get("included_in_current_gate") is not False
        or bridge.get("formal_gate_effect") is not False
        or bridge.get("auto_apply") is not False
    ):
        return ""

    def count(source: dict[str, Any], key: str) -> int:
        try:
            return max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    exact_scored = count(exact, "scored")
    exact_selected = count(exact, "selected")
    exact_unselected = count(exact, "unselected")
    exact_days = count(exact, "selected_signal_days")
    source_total = count(bridge, "source_total")
    candidate = count(bridge, "candidate")
    explicit_gpt = count(bridge, "explicit_gpt")
    unknown = count(bridge, "unknown")
    selected_candidate = count(bridge, "selected_candidate")
    candidate_days = count(bridge, "candidate_signal_days")
    manifest_count = count(bridge, "manifest_count")
    approval_required = bridge.get("approval_required") is True

    if compact:
        text = (
            f"契约分列：Exact current 全分数 {exact_scored} / 入选 {exact_selected}；"
            f"Bridge preview 源总数 {source_total} → 候选 {candidate}"
            f"（其中入选 {selected_candidate}），明确 GPT 参与 {explicit_gpt}，未知 {unknown}。"
            "Bridge 仅作旁路预览，正式门槛贡献为 0。"
        )
        return (
            '<div class="score-bridge-compact" style="margin:0 0 8px;padding:7px 9px;'
            'border-radius:9px;border:1px solid rgba(96,165,250,.22);'
            'background:rgba(96,165,250,.07);font-size:10px;line-height:1.5;'
            'color:var(--color-text-subdue,inherit);">'
            f'{escape_html(text)}</div>'
        )

    exact_strategy = str(exact.get("strategy_version") or "")
    bridge_sources = bridge.get("source_contracts")
    source_labels = []
    if isinstance(bridge_sources, list):
        for source in bridge_sources:
            if not isinstance(source, dict):
                continue
            value = str(source.get("strategy_version") or "")
            if value:
                source_labels.append(value.split(":", 1)[-1])
    source_label = "、".join(source_labels) or "固定历史契约"
    exact_label = exact_strategy.split(":", 1)[-1] if exact_strategy else "—"
    note = str(bridge.get("note") or "")
    return (
        '<section class="score-bridge-preview">'
        '<div class="score-bridge-title"><b>经审计兼容历史子集</b>'
        '<span>Exact current 与 Bridge preview 分列；统计人口绝不合并。</span></div>'
        '<div class="score-bridge-grid">'
        '<div class="score-bridge-card"><span>Exact current · 正式队列</span>'
        f'<b>全分数 {escape_html(exact_scored)} · 入选 {escape_html(exact_selected)}</b>'
        f'<small>未入选 {escape_html(exact_unselected)} · 入选信号日 {escape_html(exact_days)} · '
        f'指纹 {escape_html(exact_label)}</small></div>'
        '<div class="score-bridge-card is-preview"><span>Bridge preview · 旁路预览</span>'
        f'<b>源总数 {escape_html(source_total)} → 候选 {escape_html(candidate)}</b>'
        f'<small>候选中入选 {escape_html(selected_candidate)} · 明确 GPT 参与 {escape_html(explicit_gpt)} · '
        f'未知 {escape_html(unknown)} · 候选信号日 {escape_html(candidate_days)}</small></div>'
        '</div>'
        '<div class="score-bridge-note">'
        f'来源 {escape_html(source_label)} · 审计清单 {escape_html(manifest_count)} 个'
        f'{"（待审批）" if approval_required else "（已审批）"}。'
        '<b>formal_gate_effect=false · included_in_current_gate=false · auto_apply=false。</b> '
        f'{escape_html(note)}</div></section>'
    )


def _render_score_sample_explanation_html(result: dict[str, Any]) -> str:
    """Explain existing cohorts and maturity without changing their eligibility."""
    history = result.get("cohort_history")
    funnel = result.get("eligibility_funnel")
    if not isinstance(history, dict) or not isinstance(funnel, dict):
        return ""
    ledger = funnel.get("ledger") or {}
    state = result.get("optimization") or {}
    requirements = state.get("requirements") or {}
    scored = int(ledger.get("current_contract_scored", history.get("current_scored_signals")) or 0)
    selected = int(ledger.get("current_contract_selected", history.get("current_selected_signals")) or 0)
    revisions = int(ledger.get("all_contract_revisions") or 0)
    events = int(state.get("independent_events") or 0)
    days = int(state.get("independent_result_days") or 0)
    need_events = int(requirements.get("independent_events") or _OPTIMIZATION_MIN_INDEPENDENT_EVENTS)
    need_days = int(requirements.get("independent_result_days") or _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS)
    missing = []
    if events < need_events:
        missing.append(f"已结算独立事件 {events}/{need_events}")
    if days < need_days:
        missing.append(f"结果交易日 {days}/{need_days}")
    coverage = float(state.get("proxy_coverage") or 0)
    need_coverage = float(requirements.get("proxy_coverage") or _OPTIMIZATION_MIN_PROXY_COVERAGE)
    if coverage < need_coverage:
        missing.append(f"行情代理覆盖 {coverage:.2f}%/{need_coverage:.2f}%")
    if not state.get("control_cohort_ready"):
        missing.append("入选与未入选对照尚未独立结算充分")
    if missing:
        progress = "；".join(missing)
    elif state.get("can_tune_thresholds") is True and funnel.get("formal_eligible") is True:
        progress = "调参研究样本条件已满足；不代表模型质量合格，仍不自动调整策略"
    else:
        progress = "展示计数已达标，但实际优化资格尚未就绪；展示值有四舍五入，以原有优化状态为准"
    current = f"当前契约全分数 {scored} 条，其中入选 {selected} 条。"
    retained = (
        f"全库历史修订 {revisions} 条仅表示保留规模，含同一信号的修订，不等同独立样本。"
        if "all_contract_revisions" in ledger else "历史保留规模未提供。"
    )
    scope = (
        "当前为探索或旧缓存视图，不推进正式优化条件。"
        if funnel.get("formal_eligible") is not True else ""
    )
    return (
        '<details class="score-sample-explanation" style="margin:0 0 8px;font-size:11px;line-height:1.6;">'
        '<summary>样本口径与积累进度</summary>'
        '<div>“调参样本待积累”不等于所有指标不可看，也不等于历史记录丢失。</div>'
        f'<div>{escape_html(current)}{escape_html(retained)}</div>'
        '<div>历史样本按评分与映射契约分开保留，不自动计入当前队列；'
        '同模型家族也不等于同一契约，可复用范围需单独确认。</div>'
        f'<div>调参研究尚缺或当前状态：{escape_html(progress)}。{escape_html(scope)}</div>'
        '<div>指标展示门槛与策略优化条件不同，以各指标标注为准。'
        '信号自然日不等于结果交易日；反复刷新不会让 T+1/T+2 提前结算。'
        '镜像版本本身不决定策略指纹，评分源码、配置或反馈样本变化才可能改变契约。'
        '不通过降低门槛或混入旧样本消除提示。</div></details>'
    )


def _render_score_eligibility_funnel_html(
    result: dict[str, Any], *, compact: bool
) -> str:
    funnel = result.get("eligibility_funnel")
    if not isinstance(funnel, dict):
        return ""
    ledger = funnel.get("ledger") if isinstance(funnel.get("ledger"), dict) else {}
    selected = (
        funnel.get("selected_analysis")
        if isinstance(funnel.get("selected_analysis"), dict)
        else {}
    )
    control = (
        funnel.get("control_analysis")
        if isinstance(funnel.get("control_analysis"), dict)
        else {}
    )
    control_state = result.get("control_cohort")
    if not isinstance(control_state, dict):
        control_state = {}
    if compact:
        text = (
            f"资格漏斗：全库修订 {int(ledger.get('all_contract_revisions') or 0)} → "
            f"当前契约全分数 {int(ledger.get('current_contract_scored') or 0)}；"
            f"入选 {int(ledger.get('current_contract_selected') or 0)} / "
            f"未入选 {int(ledger.get('current_contract_unselected') or 0)}；"
            f"对照 T+1 已结算事件 {int(control.get('t1_settled_event_results') or 0)}。"
        )
        return (
            '<div class="score-funnel-compact" style="margin:0 0 8px;padding:7px 9px;border-radius:9px;'
            'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.12);'
            'font-size:10px;line-height:1.5;color:var(--color-text-subdue,inherit);">'
            f'{escape_html(text)}</div>'
        )

    def card(label: str, value: object, note: str) -> str:
        return (
            '<div class="score-funnel-card">'
            f'<span>{escape_html(label)}</span><b>{escape_html(int(value or 0))}</b>'
            f'<small>{escape_html(note)}</small></div>'
        )

    return (
        '<section class="score-funnel">'
        '<div class="score-funnel-title"><b>样本资格漏斗</b>'
        '<span>先看同一精确契约的人口，再分别查看入选与未入选的后验结算。</span></div>'
        '<div class="score-funnel-label">账本人口（信号单位）</div>'
        '<div class="score-funnel-grid">'
        + card("全库历史修订", ledger.get("all_contract_revisions"), "仅作历史规模参考")
        + card("当前契约全分数", ledger.get("current_contract_scored"), "禁止并入旧契约")
        + card("冻结入选", ledger.get("current_contract_selected"), "生产入选口径")
        + card("冻结未入选", ledger.get("current_contract_unselected"), "独立对照总体")
        + '</div><div class="score-funnel-label">入选分析（信号 → 事件结果）</div>'
        '<div class="score-funnel-grid">'
        + card("查询入选", selected.get("query_rows"), "当前窗口最终修订")
        + card("有效分数", selected.get("valid_score"), "去重并排除无效分数")
        + card("代理可用", selected.get("proxy_mapped"), "冻结行情代理")
        + card(
            "T+1独立事件",
            selected.get("t1_independent_events"),
            f"{int(selected.get('t1_independent_result_days') or 0)}个结果日",
        )
        + '</div><div class="score-funnel-label">未入选独立对照（信号 → 严格T+1结果）</div>'
        '<div class="score-funnel-grid">'
        + card("对照记录", control.get("recorded"), "冻结 selected=false")
        + card("有效分数", control.get("valid_score"), "全局去重后分组")
        + card("代理可用", control.get("proxy_mapped"), "不使用今日映射回填")
        + card(
            "T+1已结算事件",
            control.get("t1_settled_event_results"),
            f"{int(control.get('t1_independent_result_days') or 0)}个结果日",
        )
        + '</div><div class="score-funnel-note">'
        f'{escape_html(funnel.get("note") or "")} '
        f'当前对照状态：{escape_html(control_state.get("status_label") or "待积累")}；'
        '未成熟时不展示组间胜率差，也不生成阈值候选。</div></section>'
    )


def _score_sample_label(result: dict[str, Any]) -> str:
    if str(result.get("selection_policy") or "") != "explicit_min_score":
        return "入选去重信号"
    try:
        min_score = float(result.get("min_score"))
    except (TypeError, ValueError):
        min_score = math.nan
    return "全分数去重信号" if min_score == 0.0 else "查询去重信号"


def render_backtest_compact_html(
    result: dict[str, Any], mapping_validation: dict[str, Any] | None = None
) -> str:
    """Render the homepage status strip for production-selection history.

    Full bucket, horizon and confidence-interval details belong on the model
    quality page.  This view answers only whether the evidence is mature and
    how much retained production-selected history currently exists.
    """
    try:
        active_rows = _active_backtest_rows(result)
    except (TypeError, ValueError):
        active_rows = []
    has_publishable_metric = _backtest_has_publishable_metric(active_rows)
    history_status = str(result.get("history_status") or "")
    if has_publishable_metric:
        badge = "已达统计门槛"
        badge_color = "#34d399"
    elif history_status == "legacy":
        badge = "短期缓存"
        badge_color = "#a1a1aa"
    else:
        badge = "当前策略样本积累中"
        badge_color = "#fbbf24"

    selection_policy = str(result.get("selection_policy") or "")
    if selection_policy == "stored_aggregation_threshold":
        policy_label = "生产入选口径"
    elif selection_policy == "explicit_min_score":
        policy_label = "自定义分数口径"
    else:
        policy_label = "当前可用口径"

    start_date = result.get("actual_start_date") or "尚无快照"
    end_date = result.get("actual_end_date") or "—"
    sample_count = result.get("deduped_samples", 0)
    has_samples = bool(sample_count) if isinstance(sample_count, (int, float)) else False
    proxy_coverage = (
        result.get("proxy_coverage", result.get("mapping_coverage", 0))
        if has_samples else "—"
    )
    proxy_display = f"{proxy_coverage}%" if has_samples else "—"
    if has_publishable_metric:
        maturity = "已有指标达到展示门槛；不代表调参研究样本条件满足。"
    elif history_status == "legacy":
        maturity = "当前仅有短期缓存，不代表请求的完整历史窗口。"
    elif not has_samples:
        maturity = "尚无有效信号快照；胜率与收益结论暂不展示。"
    else:
        maturity = "样本仍在积累，暂不据此展示胜率或收益结论。"
    min_samples = result.get("min_statistical_samples", _MIN_STATISTICAL_SAMPLES)
    min_days = result.get("min_independent_result_days", _MIN_INDEPENDENT_RESULT_DAYS)
    score_html = (
        '<div class="score-validity-compact" style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:2px;">'
        '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:9px;">'
        '<div><div style="font-size:13px;font-weight:850;color:var(--color-text-base,inherit);">AI评分有效性</div>'
        f'<div style="font-size:11px;color:var(--color-text-subdue,inherit);margin-top:2px;">{escape_html(policy_label)} · '
        f'请求{escape_html(result.get("requested_days", result.get("days", 0)))}天，实际覆盖{escape_html(result.get("actual_window_days", 0))}天 · '
        f'{escape_html(start_date)} 至 {escape_html(end_date)}</div></div>'
        f'<div style="font-size:10px;font-weight:750;white-space:nowrap;color:{badge_color};border:1px solid {badge_color};'
        f'border-radius:999px;padding:3px 7px;opacity:.9;">{escape_html(badge)}</div></div>'
        f'{_render_score_optimization_html(result, compact=True)}'
        f'{_render_score_cohort_history_html(result, compact=True)}'
        f'{_render_audited_compatibility_html(result, compact=True)}'
        f'{_render_score_eligibility_funnel_html(result, compact=True)}'
        f'{_render_score_sample_explanation_html(result)}'
        '<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-bottom:8px;">'
        '<div style="padding:7px 8px;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.3);">'
        f'<span style="display:block;font-size:10px;color:var(--color-text-subdue,inherit);">{escape_html(_score_sample_label(result))}</span><b>{escape_html(result.get("deduped_samples", 0))}</b></div>'
        '<div style="padding:7px 8px;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.3);">'
        f'<span style="display:block;font-size:10px;color:var(--color-text-subdue,inherit);">信号自然日</span><b>{escape_html(result.get("unique_signal_days", 0))}</b></div>'
        '<div style="padding:7px 8px;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.3);">'
        f'<span style="display:block;font-size:10px;color:var(--color-text-subdue,inherit);">行情代理覆盖</span><b>{escape_html(proxy_display)}</b></div>'
        '</div>'
        '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:10px;line-height:1.45;">'
        f'<span style="color:var(--color-text-subdue,inherit);">{escape_html(maturity)} 价格指标需 n≥{escape_html(min_samples)} 且独立结果交易日≥{escape_html(min_days)}；主题复现需观察满72小时的样本≥{escape_html(min_samples)}、信号自然日≥{escape_html(min_days)}。历史关联，非交易回测。</span>'
        '<span class="quality-card-entry"><a href="/quality" style="color:var(--color-primary,inherit);text-decoration:none;white-space:nowrap;">完整统计 →</a></span>'
        '</div></div>'
    )
    if mapping_validation is not None:
        score_html += render_mapping_validation_compact_html(mapping_validation)
    # Only the complete worker snapshot opts into full details. Other compact
    # callers retain their lightweight input contract (including sparse buckets).
    if result.get("_include_full_research") is True:
        score_html += (
            '<details class="quality-full-statistics"><summary>完整市场指标</summary>'
            + render_backtest_html(result, mapping_validation, include_overview=False)
            + '</details>'
        )
    return _quality_page(result, score_html)


def _quality_page(result: dict[str, Any], research_html: str) -> str:
    return (
        render_quality_overview(result)
        + '<details class="quality-market-research" style="margin:16px 0;line-height:1.7;">'
        '<summary style="cursor:pointer;font-weight:650;">市场研究与历史统计（仅事后参考）</summary>'
        '<p>以下门槛只用于市场统计与调参研究，不代表解释质量；'
        '无需等到这些门槛满足，就可以核对具体新闻。历史契约保留但不混算。</p>'
        + research_html + '</details>'
    )


def _backtest_percentage(value: Any, digits: int = 1) -> str:
    return f"{float(value):.{digits}f}%" if isinstance(value, (int, float)) else "—"


def render_backtest_html(
    result: dict[str, Any], mapping_validation: dict[str, Any] | None = None,
    *, include_overview: bool = True,
) -> str:
    min_samples = int(result.get("min_statistical_samples") or _MIN_STATISTICAL_SAMPLES)
    min_result_days = int(
        result.get("min_independent_result_days") or _MIN_INDEPENDENT_RESULT_DAYS
    )
    active_rows = _active_backtest_rows(result)
    has_publishable_metric = _backtest_has_publishable_metric(active_rows)
    if has_publishable_metric:
        badge = "已达统计门槛"
    elif result.get("history_status") == "legacy":
        badge = "短期缓存"
    else:
        badge = "当前策略样本积累中"

    def metric_detail(row: dict[str, Any], prefix: str) -> str:
        samples = int(row.get(f"{prefix}_samples") or 0)
        unique_days = int(row.get(f"{prefix}_unique_days") or 0)
        ci = row.get(f"{prefix}_win_rate_ci95") or {}
        parts = [f"n={samples}", f"{unique_days}个独立交易日"]
        if bool(row.get(f"{prefix}_sample_sufficient")) and (
            isinstance(ci, dict) and isinstance(ci.get("lower"), (int, float))
        ):
            parts.append(f"95%CI {ci['lower']:.1f}–{ci['upper']:.1f}%")
        return " · ".join(parts)

    def rate_metric(row: dict[str, Any], label: str, prefix: str) -> str:
        effective_prefix = _effective_metric_prefix(row, prefix)
        sufficient = bool(row.get(f"{effective_prefix}_sample_sufficient"))
        display_value = (
            _backtest_percentage(row.get(f"{effective_prefix}_win_rate")) if sufficient else "待积累"
        )
        return (
            f'<div class="score-validity-metric{"" if sufficient else " is-pending"}">'
            f'<span>{escape_html(label)}</span>'
            f'<b>{escape_html(display_value)}</b>'
            f'<small>{escape_html(metric_detail(row, effective_prefix))}</small>'
            '</div>'
        )

    def return_metric(
        row: dict[str, Any],
        label: str,
        value_key: str,
        samples_key: str,
        result_days_key: str,
    ) -> str:
        clustered_value_key = f"clustered_{value_key}"
        clustered_samples_key = f"clustered_{samples_key}"
        clustered_days_key = (
            f"clustered_{samples_key.removesuffix('_samples')}_unique_days"
        )
        if clustered_samples_key in row:
            value_key = clustered_value_key
            samples_key = clustered_samples_key
            result_days_key = clustered_days_key
        samples = int(row.get(samples_key) or 0)
        result_days = int(row.get(result_days_key) or 0)
        sufficient = samples >= min_samples and result_days >= min_result_days
        display_value = _backtest_percentage(row.get(value_key), 3) if sufficient else "待积累"
        return (
            f'<div class="score-validity-metric{"" if sufficient else " is-pending"}">'
            f'<span>{escape_html(label)}</span><b>{escape_html(display_value)}</b>'
            f'<small>n={escape_html(samples)} · {escape_html(result_days)}个独立交易日</small>'
            '</div>'
        )

    rows = []
    for row in active_rows:
        themes = "、".join(f"{x['theme']}({x['count']})" for x in row.get("top_themes", [])[:3]) or "-"
        # Old cached results without a maturity denominator cannot publish a
        # recurrence rate under the corrected observation contract.
        sample_count = int(row.get("theme_follow_samples") or 0)
        signal_days = int(row.get("theme_follow_unique_days") or 0)
        pending_count = row.get("theme_follow_pending_samples")
        pending_label = "未知" if pending_count is None else str(int(pending_count))
        recurrence_sufficient = (
            sample_count >= min_samples and signal_days >= min_result_days
        )
        recurrence_value = (
            _backtest_percentage(row.get("theme_follow_rate")) if recurrence_sufficient else "待积累"
        )
        direction_specs = (
            ("映射资产 T0方向", "asset_direction"),
            ("映射资产 T+1方向", "asset_t1_direction"),
            ("映射资产 T+2方向", "asset_t2_direction"),
            ("上证基准 T0方向", "benchmark_direction"),
            ("上证基准 T+1方向", "benchmark_t1_direction"),
            ("上证基准 T+2方向", "benchmark_t2_direction"),
        )
        direction_cards = "".join(
            rate_metric(row, label, prefix)
            for label, prefix in direction_specs
            if int(
                row.get(f"{_effective_metric_prefix(row, prefix)}_samples") or 0
            ) > 0
        )
        direction_html = (
            '<div class="score-validity-group-title">方向关联</div>'
            '<div class="score-validity-metrics">'
            f'{direction_cards}</div>'
            if direction_cards
            else '<div class="score-validity-wait">行情结果尚未形成；收盘数据到齐后展示 T0/T+1/T+2 方向关联。</div>'
        )
        return_specs = (
            ("T+1目标日单日毛收益", "t1_avg_signal_return_pct", "t1_signal_return_samples", "asset_t1_direction_unique_days"),
            ("T+1目标日单日毛超额", "t1_avg_excess_return_pct", "t1_excess_return_samples", "asset_t1_direction_unique_days"),
            ("T+2目标日单日毛超额", "t2_avg_excess_return_pct", "t2_excess_return_samples", "asset_t2_direction_unique_days"),
        )
        return_cards = "".join(
            return_metric(row, label, value_key, samples_key, days_key)
            for label, value_key, samples_key, days_key in return_specs
            if int(
                row.get(f"clustered_{samples_key}")
                if f"clustered_{samples_key}" in row
                else row.get(samples_key) or 0
            ) > 0
        )
        return_html = (
            '<div class="score-validity-group-title">目标日收益关联</div>'
            f'<div class="score-validity-metrics">{return_cards}</div>'
            if return_cards else ""
        )
        rows.append(
            '<div class="score-validity-row" style="padding:10px 11px;margin-bottom:8px;border-radius:13px;'
            'background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 5%),0.34);'
            'border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),0.12);font-size:12px;">'
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;">'
            f'<div style="font-weight:850;color:var(--color-text-base,inherit);font-size:13px;">{escape_html(row["bucket"])}</div>'
            f'<div style="font-size:11px;color:var(--color-text-subdue,inherit);">样本 {escape_html(row["samples"])} · 信号自然日 {escape_html(row.get("unique_signal_days", 0))}</div>'
            '</div>'
            '<div class="score-validity-metrics">'
            f'<div class="score-validity-metric"><span>均分</span><b>{escape_html(row["avg_score"])}</b></div>'
            f'<div class="score-validity-metric"><span>行情代理覆盖</span><b>{escape_html(row["mapping_coverage"])}%</b></div>'
            f'<div class="score-validity-metric{"" if recurrence_sufficient else " is-pending"}"><span>72小时主题复现</span><b>{escape_html(recurrence_value)}</b><small>已观察满72小时 n={escape_html(sample_count)} · {escape_html(signal_days)}个成熟信号自然日 · 待观察 {escape_html(pending_label)}条（不计成功或失败）</small></div>'
            '</div>'
            f'{direction_html}{return_html}'
            '<div class="score-validity-themes">'
            '<span style="display:block;margin-bottom:3px;color:var(--color-text-subdue,inherit);font-size:11px;font-weight:650;">高频主题</span>'
            f'<div style="color:var(--color-text-base,inherit);font-size:12px;line-height:1.45;white-space:normal;overflow-wrap:anywhere;word-break:break-word;">{escape_html(themes)}</div>'
            '</div>'
            '</div>'
        )
    rows_html = "".join(rows) if rows else '<div style="color:var(--color-text-subdue,inherit);padding:18px;text-align:center;">暂无足够样本</div>'
    theme_rows = []
    def clustered_theme_sample_count(theme_row: dict[str, Any]) -> int:
        if "clustered_t1_samples" in theme_row:
            return max(
                int(theme_row.get("clustered_t1_samples") or 0),
                int(theme_row.get("clustered_t2_samples") or 0),
            )
        return max(
            int(theme_row.get("t1_samples") or 0),
            int(theme_row.get("t2_samples") or 0),
        )

    ranked_themes = sorted(
        result.get("theme_win_rates", {}).items(),
        key=lambda item: (
            clustered_theme_sample_count(item[1]),
            int(item[1].get("count") or 0),
        ),
        reverse=True,
    )[:6]
    for theme, t in ranked_themes:
        posterior_samples = clustered_theme_sample_count(t)
        if posterior_samples <= 0:
            continue
        t1_prefix = "clustered_t1" if "clustered_t1_samples" in t else "t1"
        t2_prefix = "clustered_t2" if "clustered_t2_samples" in t else "t2"
        t1_sufficient = bool(t.get(f"{t1_prefix}_sample_sufficient"))
        t2_sufficient = bool(t.get(f"{t2_prefix}_sample_sufficient"))
        t1 = _backtest_percentage(t.get(f"{t1_prefix}_win_rate")) if t1_sufficient else "待积累"
        t2 = _backtest_percentage(t.get(f"{t2_prefix}_win_rate")) if t2_sufficient else "待积累"
        t1_ci = t.get(f"{t1_prefix}_win_rate_ci95") or {}
        t2_ci = t.get(f"{t2_prefix}_win_rate_ci95") or {}
        t1_detail = f"n={int(t.get(f'{t1_prefix}_samples') or 0)}/{int(t.get(f'{t1_prefix}_unique_days') or 0)}日"
        t2_detail = f"n={int(t.get(f'{t2_prefix}_samples') or 0)}/{int(t.get(f'{t2_prefix}_unique_days') or 0)}日"
        if t1_sufficient and isinstance(t1_ci, dict) and isinstance(t1_ci.get("lower"), (int, float)):
            t1_detail += f" CI {t1_ci['lower']:.1f}–{t1_ci['upper']:.1f}%"
        if t2_sufficient and isinstance(t2_ci, dict) and isinstance(t2_ci.get("lower"), (int, float)):
            t2_detail += f" CI {t2_ci['lower']:.1f}–{t2_ci['upper']:.1f}%"
        symbols = "/".join(str(item) for item in t.get("asset_symbols", [])) or "无行情代理"
        theme_rows.append(
            '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:7px 8px;'
            'border-radius:8px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl)+4%),0.26);margin-bottom:5px;'
            'font-size:12px;">'
            f'<div style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
            f'<span style="font-weight:700;color:var(--color-text-base,inherit);">{escape_html(theme)}</span>'
            f'<span style="color:var(--color-text-subdue,inherit);font-size:11px;margin-left:5px;">信号n={escape_html(t.get("count"))} · 后验n={escape_html(posterior_samples)} · {escape_html(symbols)} · 均分{escape_html(t.get("avg_score"))}</span></div>'
            f'<div style="flex-shrink:0;color:var(--color-text-subdue,inherit);font-size:11px;text-align:right;line-height:1.45;">'
            f'<div>T+1 {escape_html(t1)} · {escape_html(t1_detail)}</div>'
            f'<div>T+2 {escape_html(t2)} · {escape_html(t2_detail)}</div></div></div>'
        )
    theme_html = (
        '<div style="margin-top:12px;">'
        '<div style="font-size:12px;color:var(--color-text-subdue,inherit);margin-bottom:6px;font-weight:650;">'
        'AI 主题 → 历史后验方向关联</div>'
        + "".join(theme_rows)
        + '</div>'
    ) if theme_rows else ""
    maturity_message = (
        "已有指标达到展示门槛；不代表调参研究样本条件满足。"
        if has_publishable_metric
        else f"价格指标需 n≥{min_samples} 且独立结果交易日≥{min_result_days}；主题复现需观察满72小时的样本≥{min_samples}、信号自然日≥{min_result_days}。未满观察期不算失败。"
    )
    score_html = (
        '<div class="score-validity" style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:2px 2px 8px;max-width:100%;overflow:hidden;">'
        '<style>'
        '.score-validity *{box-sizing:border-box;}'
        '.score-validity-summary{font-size:12px;color:var(--color-text-subdue,inherit);line-height:1.45;min-width:0;}'
        '.score-validity-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:10px;min-width:0;}'
        '.score-validity-badge{font-size:11px;color:#fbbf24;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.22);padding:4px 8px;border-radius:999px;white-space:nowrap;flex-shrink:0;}'
        '.score-validity-maturity{margin-bottom:10px;padding:8px 10px;border-radius:10px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.16);font-size:11px;line-height:1.5;color:var(--color-text-subdue,inherit);}'
        '.score-validity-maturity b{color:#fbbf24;margin-right:5px;}'
        '.score-validity-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-bottom:8px;}'
        '.score-validity-metric{min-width:0;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 2%),0.28);padding:6px 7px;}'
        '.score-validity-metric.is-pending b{color:var(--color-text-subdue,inherit);font-weight:650;}'
        '.score-validity-metric span{display:block;color:var(--color-text-subdue,inherit);font-size:11px;margin-bottom:2px;}'
        '.score-validity-metric b{display:block;color:var(--color-text-base,inherit);font-size:13px;line-height:1.2;overflow-wrap:anywhere;}'
        '.score-validity-metric small{display:block;color:var(--color-text-subdue,inherit);font-size:10px;line-height:1.4;margin-top:3px;overflow-wrap:anywhere;}'
        '.score-validity-group-title{font-size:11px;color:var(--color-text-subdue,inherit);font-weight:650;margin:9px 0 5px;}'
        '.score-validity-wait{font-size:11px;color:var(--color-text-subdue,inherit);padding:7px 8px;margin-bottom:8px;border-radius:8px;border:1px dashed hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),0.18);}'
        '.score-validity-note{font-size:11px;color:var(--color-text-subdue,inherit);line-height:1.55;margin-top:8px;overflow-wrap:anywhere;}'
        '.score-validity-note summary{cursor:pointer;font-weight:650;}'
        '.score-validity-note div{margin-top:6px;}'
        '.score-cohort-history{margin:0 0 10px;padding:9px 10px;border-radius:11px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.2);border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.12);}'
        '.score-cohort-title{font-size:11px;color:var(--color-text-subdue,inherit);font-weight:700;margin-bottom:6px;}'
        '.score-cohort-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;}'
        '.score-cohort-card{min-width:0;padding:7px 8px;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 5%),.28);}'
        '.score-cohort-card span,.score-cohort-card small{display:block;color:var(--color-text-subdue,inherit);font-size:10px;line-height:1.45;overflow-wrap:anywhere;}'
        '.score-cohort-card b{display:block;color:var(--color-text-base,inherit);font-size:13px;margin:2px 0;overflow-wrap:anywhere;}'
        '.score-cohort-note{font-size:10px;color:var(--color-text-subdue,inherit);line-height:1.55;margin-top:6px;}'
        '.score-bridge-preview{margin:0 0 10px;padding:9px 10px;border-radius:11px;background:rgba(96,165,250,.06);border:1px solid rgba(96,165,250,.2)}'
        '.score-bridge-title{display:flex;justify-content:space-between;gap:8px;font-size:11px;line-height:1.45}.score-bridge-title span,.score-bridge-note{color:var(--color-text-subdue,inherit)}'
        '.score-bridge-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:7px}.score-bridge-card{min-width:0;padding:7px 8px;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 5%),.28)}.score-bridge-card.is-preview{border:1px dashed rgba(96,165,250,.3)}'
        '.score-bridge-card span,.score-bridge-card small{display:block;color:var(--color-text-subdue,inherit);font-size:10px;line-height:1.45;overflow-wrap:anywhere}.score-bridge-card b{display:block;color:var(--color-text-base,inherit);font-size:13px;margin:2px 0;overflow-wrap:anywhere}.score-bridge-note{font-size:10px;line-height:1.55;margin-top:7px}.score-bridge-note b{color:#60a5fa}'
        '.score-funnel{margin:0 0 10px;padding:9px 10px;border-radius:11px;border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.12);}'
        '.score-funnel-title{display:flex;justify-content:space-between;gap:8px;font-size:11px;line-height:1.45}.score-funnel-title span,.score-funnel-label,.score-funnel-note{color:var(--color-text-subdue,inherit)}'
        '.score-funnel-label{font-size:10px;font-weight:650;margin:8px 0 4px}.score-funnel-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}'
        '.score-funnel-card{min-width:0;padding:6px 7px;border-radius:8px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.24)}.score-funnel-card span,.score-funnel-card small{display:block;font-size:10px;line-height:1.4;color:var(--color-text-subdue,inherit);overflow-wrap:anywhere}.score-funnel-card b{display:block;font-size:13px;color:var(--color-text-base,inherit);margin:2px 0}.score-funnel-note{font-size:10px;line-height:1.5;margin-top:7px}'
        '@media (max-width:520px){.score-funnel-title,.score-bridge-title{display:block}.score-funnel-title span,.score-bridge-title span{display:block;margin-top:2px}.score-funnel-grid,.score-bridge-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}'
        '@media (max-width:380px){.score-validity-top{flex-direction:column;}.score-validity-badge{align-self:flex-start;}.score-validity-metrics{grid-template-columns:repeat(2,minmax(0,1fr))!important;}}'
        '</style>'
        '<div class="score-validity-top">'
        f'<div class="score-validity-summary">请求窗口 {escape_html(result.get("requested_days", result.get("days")))}天 · '
        f'实际 {escape_html(result.get("actual_start_date") or "尚无快照")} 至 {escape_html(result.get("actual_end_date") or "—")} '
        f'({escape_html(result.get("actual_window_days", 0))}天) · {escape_html(_score_sample_label(result))} {escape_html(result.get("deduped_samples"))} · '
        f'信号自然日 {escape_html(result.get("unique_signal_days", 0))}</div>'
        f'<div class="score-validity-badge">{escape_html(badge)}</div>'
        '</div>'
        f'{_render_score_optimization_html(result, compact=False)}'
        f'{_render_score_cohort_history_html(result, compact=False)}'
        f'{_render_audited_compatibility_html(result, compact=False)}'
        f'{_render_score_eligibility_funnel_html(result, compact=False)}'
        f'{_render_score_sample_explanation_html(result)}'
        f'<div class="score-validity-maturity"><b>{escape_html(badge)}</b>{escape_html(maturity_message)}</div>'
        f'{rows_html}'
        f'{theme_html}'
        '<details class="score-validity-note"><summary>口径与限制</summary><div>'
        f'{escape_html(result.get("note"))} '
        f'数据源：{escape_html(result.get("data_source", "legacy_news_cache"))}；原始{escape_html(result.get("raw_samples"))}条，'
        f'原子事件去重{escape_html(result.get("dedupe_removed"))}条，事后剔除{escape_html(result.get("invalid_samples"))}条。'
        f'行情代理覆盖率 {escape_html(result.get("proxy_coverage", result.get("mapping_coverage", 0)))}%；'
        f'聚类身份回退 {escape_html(result.get("cluster_identity_fallback_samples", 0))} 条，'
        f'不可聚类指标观察 {escape_html(result.get("unclusterable_metric_observations", 0))} 条，'
        f'结果日无法确定 {escape_html(result.get("cluster_result_date_unavailable_observations", 0))} 条。</div></details>'
        '</div>'
    )
    if mapping_validation is not None:
        score_html += render_mapping_validation_html(mapping_validation)
    return _quality_page(result, score_html) if include_overview else score_html
