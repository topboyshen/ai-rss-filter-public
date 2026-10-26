from datetime import datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

import pytest

from services import backtest
from services.mapping_validation import (
    _mapping_optimization_state,
    render_mapping_validation_compact_html,
    render_mapping_validation_html,
)


TZ = ZoneInfo("Asia/Shanghai")


def _ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


@pytest.fixture(autouse=True)
def completed_governance_window(monkeypatch):
    monkeypatch.setattr(backtest.time, "time", lambda: _ts("2026-08-01 16:00"))


def _completed_close_price(trade_date: str) -> dict:
    """Governance fixtures represent observed closes, not live percentage ticks."""
    return {
        "trade_date": trade_date,
        "close_price": 201.0,
        "change_amt": 1.0,
        "change_pct": 0.5,
        "ts": _ts(f"{trade_date} 15:00"),
    }


def _article(
    *,
    link: str,
    event_id: str,
    available_at: int,
    score: float,
    theme: str = "AI算力",
) -> dict:
    return {
        "link": link,
        "event_id": event_id,
        "event_key": event_id,
        "independence_key": event_id,
        "score": score,
        "tag": theme,
        "direction": "看多",
        "created_at": available_at,
        "available_at": available_at,
        "score_confidence": 0.8,
        "strategy_version": "strategy-v1",
        "mapped_theme": theme,
        "backtest_symbol": "sz399006",
        "_signal_snapshot": True,
    }


def _run_with_rows(
    monkeypatch,
    rows: list[dict],
    *,
    snapshot_ledger: bool = True,
    min_score: float | None = None,
    cohort_history: dict | None = None,
    future_snapshot=None,
):
    history = {
        "count": len(rows),
        "first_available_at": min(row["available_at"] for row in rows),
        "collection_started_at": min(row["available_at"] for row in rows),
        "data_source": "signal_snapshots" if snapshot_ledger else "legacy_news_cache",
        "cohort_history": dict(cohort_history or {}),
    }
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )
    monkeypatch.setattr(
        backtest,
        "_load_backtest_samples",
        lambda *_args, **_kwargs: (list(rows), history),
    )
    monkeypatch.setattr(backtest, "dedupe_articles", lambda values: list(values))
    monkeypatch.setattr(backtest, "_same_day_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        backtest,
        "_future_snapshot",
        future_snapshot
        or (lambda *_args: _completed_close_price("2026-07-13")),
    )
    return backtest.run_score_backtest(days=90, min_score=min_score)


def test_score_control_settlement_clusters_globally_before_assigning_arms(monkeypatch):
    day = _ts("2026-07-10 10:00")
    shared_event = str(uuid.uuid4())
    selected_event = str(uuid.uuid4())
    unselected_event = str(uuid.uuid4())
    rows = [
        {
            **_article(
                link="shared-rejected-first",
                event_id=shared_event,
                available_at=day,
                score=6.5,
            ),
            "selected": False,
        },
        {
            **_article(
                link="shared-selected-later",
                event_id=shared_event,
                available_at=day + 3600,
                score=8.5,
            ),
            "selected": True,
        },
        {
            **_article(
                link="selected-only",
                event_id=selected_event,
                available_at=day,
                score=9.0,
            ),
            "selected": True,
        },
        {
            **_article(
                link="unselected-only",
                event_id=unselected_event,
                available_at=day,
                score=5.5,
            ),
            "selected": False,
        },
    ]

    def exact_revision_price(symbol, from_date, offset):
        return {
            **_completed_close_price(backtest._trading_day_after(from_date, offset)),
            "symbol": symbol,
            "revision_id": f"revision-{symbol}-{from_date}",
        }

    monkeypatch.setattr(backtest, "_get_future_price_at_offset", exact_revision_price)
    monkeypatch.setattr(backtest, "_MIN_STATISTICAL_SAMPLES", 1)
    monkeypatch.setattr(backtest, "_MIN_INDEPENDENT_RESULT_DAYS", 1)
    monkeypatch.setattr(backtest, "_OPTIMIZATION_MIN_INDEPENDENT_EVENTS", 1)
    monkeypatch.setattr(backtest, "_OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS", 1)
    monkeypatch.setattr(backtest, "_OPTIMIZATION_MIN_PROXY_COVERAGE", 1.0)

    result = backtest._settle_score_control_cohort(
        rows,
        strategy_version="strategy-v1",
        mapping_version="asset-map:test",
    )

    assert result["cohort_overlap"] == 0
    assert result["selected"]["recorded_signals"] == 2
    assert result["unselected"]["recorded_signals"] == 2
    # The shared event belongs only to the earlier rejected observation.
    assert result["selected"]["t1_settled_event_results"] == 1
    assert result["unselected"]["t1_settled_event_results"] == 2
    assert result["selected"]["candidate_ready"] is True
    assert result["unselected"]["candidate_ready"] is True
    assert result["ready_for_tuning"] is True
    assert result["auto_apply"] is False


def test_score_control_requires_exact_revision_backed_t1_prices(monkeypatch):
    day = _ts("2026-07-10 10:00")
    rows = [
        {
            **_article(
                link="selected",
                event_id=str(uuid.uuid4()),
                available_at=day,
                score=9.0,
            ),
            "selected": True,
        },
        {
            **_article(
                link="unselected",
                event_id=str(uuid.uuid4()),
                available_at=day,
                score=5.0,
            ),
            "selected": False,
        },
    ]

    monkeypatch.setattr(
        backtest,
        "_get_future_price_at_offset",
        lambda symbol, from_date, offset: {
            **_completed_close_price(backtest._trading_day_after(from_date, offset)),
            "symbol": symbol,
            # Missing revision_id is deliberately not a settled observation.
        },
    )

    result = backtest._settle_score_control_cohort(
        rows,
        strategy_version="strategy-v1",
        mapping_version="asset-map:test",
    )

    assert result["selected"]["t1_settled_event_results"] == 0
    assert result["unselected"]["t1_settled_event_results"] == 0
    assert result["selected"]["exclusions"]["missing_price_revision"] == 1
    assert result["unselected"]["exclusions"]["missing_price_revision"] == 1
    assert result["ready_for_tuning"] is False


def test_score_control_uses_raw_proxy_coverage_and_caches_price_reads(monkeypatch):
    day = _ts("2026-07-10 10:00")
    rows = []
    for index in range(4004):
        row = {
            **_article(
                link=f"selected-{index}",
                event_id=str(uuid.uuid4()),
                available_at=day,
                score=8.5,
            ),
            "selected": True,
        }
        if index >= 3203:
            row["backtest_symbol"] = None
        rows.append(row)
    rows.append(
        {
            **_article(
                link="unselected-control",
                event_id=str(uuid.uuid4()),
                available_at=day,
                score=5.0,
            ),
            "selected": False,
        }
    )
    calls = []

    def exact_revision_price(symbol, from_date, offset):
        calls.append((symbol, from_date, offset))
        return {
            **_completed_close_price(backtest._trading_day_after(from_date, offset)),
            "symbol": symbol,
            "revision_id": f"revision-{symbol}-{from_date}",
        }

    monkeypatch.setattr(backtest, "_get_future_price_at_offset", exact_revision_price)
    monkeypatch.setattr(backtest, "_OPTIMIZATION_MIN_INDEPENDENT_EVENTS", 1)
    monkeypatch.setattr(backtest, "_OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS", 1)
    monkeypatch.setattr(backtest, "_OPTIMIZATION_MIN_PROXY_COVERAGE", 80.0)

    result = backtest._settle_score_control_cohort(
        rows,
        strategy_version="strategy-v1",
        mapping_version="asset-map:test",
    )

    # 3203 / 4004 = 79.995...%, which is displayed as 80.00 but must not
    # pass an 80% admission gate.
    assert result["selected"]["proxy_coverage"] == 80.0
    assert result["selected"]["candidate_ready"] is False
    assert result["unselected"]["candidate_ready"] is True
    assert result["ready_for_tuning"] is False
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("events_per_arm", "result_day_count", "expected_ready"),
    [
        (99, 60, False),
        (100, 59, False),
        (100, 60, True),
    ],
)
def test_score_control_enforces_actual_event_and_result_day_thresholds(
    monkeypatch, events_per_arm, result_day_count, expected_ready
):
    first_day = "2026-01-05"
    signal_days = [first_day] + [
        backtest._trading_day_after(first_day, offset)
        for offset in range(1, 60)
    ]
    rows = []
    for selected in (True, False):
        for index in range(events_per_arm):
            signal_day = signal_days[index % result_day_count]
            rows.append(
                {
                    **_article(
                        link=f"{'selected' if selected else 'unselected'}-{index}",
                        event_id=str(uuid.uuid4()),
                        available_at=_ts(f"{signal_day} 10:00"),
                        score=8.5 if selected else 5.0,
                    ),
                    "selected": selected,
                }
            )

    monkeypatch.setattr(
        backtest,
        "_get_future_price_at_offset",
        lambda symbol, from_date, offset: {
            **_completed_close_price(backtest._trading_day_after(from_date, offset)),
            "symbol": symbol,
            "revision_id": f"revision-{symbol}-{from_date}",
        },
    )

    result = backtest._settle_score_control_cohort(
        rows,
        strategy_version="strategy-v1",
        mapping_version="asset-map:test",
    )

    assert result["selected"]["independent_events"] == events_per_arm
    assert result["unselected"]["independent_events"] == events_per_arm
    assert result["selected"]["independent_result_days"] == result_day_count
    assert result["unselected"]["independent_result_days"] == result_day_count
    assert result["ready_for_tuning"] is expected_ready


@pytest.mark.parametrize(
    ("events", "result_days", "coverage"),
    [
        (99, 60, 80.0),
        (100, 59, 80.0),
        (100, 60, 79.96),
    ],
)
def test_score_optimization_gate_fails_each_boundary(events, result_days, coverage):
    state = backtest._score_optimization_state(
        independent_events=events,
        independent_result_days=result_days,
        proxy_coverage=coverage,
        control_cohort_available=True,
    )

    assert state["stage"] == "insufficient"
    assert state["auto_apply"] is False


def test_score_optimization_gate_only_creates_a_candidate():
    state = backtest._score_optimization_state(
        independent_events=100,
        independent_result_days=60,
        proxy_coverage=80.0,
        control_cohort_available=True,
    )

    assert state["stage"] == "candidate"
    assert state["auto_apply"] is False
    assert state["can_tune_thresholds"] is True
    assert "留出验证" in state["recommendation"]


def test_selected_only_cohort_can_never_tune_score_thresholds():
    state = backtest._score_optimization_state(
        independent_events=100,
        independent_result_days=60,
        proxy_coverage=80.0,
        control_cohort_available=False,
    )

    assert state["stage"] == "posterior_ready"
    assert state["posterior_ready"] is True
    assert state["can_tune_thresholds"] is False
    assert "缺少全分数未入选对照" in state["recommendation"]


def test_recorded_control_cohort_is_not_misreported_as_ready():
    state = backtest._score_optimization_state(
        independent_events=3,
        independent_result_days=1,
        proxy_coverage=50.0,
        control_cohort_available=False,
        control_cohort_recorded=True,
        control_cohort_unselected_signals=921,
    )

    assert state["control_cohort_recorded"] is True
    assert state["control_cohort_ready"] is False
    assert state["control_cohort_available"] is False
    assert state["can_tune_thresholds"] is False
    assert state["cohort_label"] == "全分数对照已记录，待独立结算"
    assert "未入选921条" in state["recommendation"]


def test_explicit_full_score_view_does_not_unlock_control_candidate(monkeypatch):
    row = _article(
        link="exploratory-full-score",
        event_id=str(uuid.uuid4()),
        available_at=_ts("2026-07-11 10:00"),
        score=7.5,
    )
    result = _run_with_rows(
        monkeypatch,
        [row],
        min_score=0.0,
        cohort_history={"current_unselected_signals": 921},
    )
    state = result["optimization"]
    html = backtest.render_backtest_html(result)

    assert result["selection_policy"] == "explicit_min_score"
    assert state["control_query_mode"] == "exploratory_full_score"
    assert state["control_cohort_recorded"] is True
    assert state["control_cohort_ready"] is False
    assert state["can_tune_thresholds"] is False
    assert result["control_cohort"]["status"] == "exploratory_disabled"
    assert result["eligibility_funnel"]["formal_eligible"] is False
    assert "全分数去重信号 1" in html


def test_mature_exploratory_full_score_view_cannot_advance_production_gate(monkeypatch):
    start = datetime(2026, 1, 5, 10, 0, tzinfo=TZ)
    rows = [
        _article(
            link=f"exploratory-{index}",
            event_id=str(uuid.uuid4()),
            available_at=int((start + timedelta(days=index)).timestamp()),
            score=7.5,
        )
        for index in range(100)
    ]

    def exact_future_snapshot(_symbol, article_date, horizon):
        return _completed_close_price(backtest._trading_day_after(article_date, horizon))

    result = _run_with_rows(
        monkeypatch,
        rows,
        min_score=0.0,
        cohort_history={"current_unselected_signals": 100},
        future_snapshot=exact_future_snapshot,
    )
    state = result["optimization"]
    html = backtest.render_backtest_html(result)
    bucket = next(row for row in result["buckets"] if row["bucket"] == "7.0-7.9")

    assert bucket["clustered_asset_t1_direction_samples"] == 100
    assert bucket["clustered_asset_t1_direction_unique_days"] >= 60
    assert state["scope"] == "exploratory_full_score"
    assert state["scope_label"] == "探索性全分数视图（不用于调参门控）"
    assert state["independent_events"] == 0
    assert state["independent_result_days"] == 0
    assert state["posterior_ready"] is False
    assert state["can_tune_thresholds"] is False
    assert "全分数去重信号 100" in html


def test_custom_threshold_view_is_labelled_exploratory(monkeypatch):
    row = _article(
        link="custom-threshold",
        event_id=str(uuid.uuid4()),
        available_at=_ts("2026-07-11 10:00"),
        score=8.0,
    )
    result = _run_with_rows(monkeypatch, [row], min_score=7.0)
    html = backtest.render_backtest_html(result)

    assert result["optimization"]["control_query_mode"] == "exploratory_threshold"
    assert result["optimization"]["scope_label"] == "探索性分数阈值视图（不用于调参门控）"
    assert "查询去重信号 1" in html
    assert "入选去重信号 1" not in html


def test_mapping_optimization_gate_requires_all_dimensions():
    horizon = {
        "horizon": "T+1",
        "directional_complete_pairs": 100,
        "independent_events": 100,
        "independent_result_days": 60,
        "unique_securities": 10,
    }

    for field, short_value in (
        ("directional_complete_pairs", 99),
        ("independent_events", 99),
        ("independent_result_days", 59),
        ("unique_securities", 9),
    ):
        state = _mapping_optimization_state(
            [{**horizon, field: short_value}], 90.0
        )
        assert state["stage"] == "insufficient"
        assert state["auto_apply"] is False

    assert _mapping_optimization_state([horizon], 89.96)["stage"] == "insufficient"
    candidate = _mapping_optimization_state([horizon], 90.0)
    assert candidate["stage"] == "candidate"
    assert candidate["auto_apply"] is False
    assert candidate["semantic_audit_required"] is True


def test_mapping_optimization_accepts_any_fully_eligible_horizon():
    eligible_t1 = {
        "horizon": "T+1",
        "directional_complete_pairs": 100,
        "independent_events": 100,
        "independent_result_days": 60,
        "unique_securities": 10,
    }
    larger_but_ineligible_t3 = {
        "horizon": "T+3",
        "directional_complete_pairs": 120,
        "independent_events": 101,
        "independent_result_days": 59,
        "unique_securities": 12,
    }

    state = _mapping_optimization_state(
        [eligible_t1, larger_but_ineligible_t3], 90.0
    )

    assert state["stage"] == "candidate"
    assert state["basis_horizon"] == "T+1"


def test_event_result_day_cluster_is_global_before_score_buckets(monkeypatch):
    event_id = str(uuid.uuid4())
    rows = [
        _article(
            link="early-8x",
            event_id=event_id,
            available_at=_ts("2026-07-11 01:00"),
            score=8.8,
            theme="早期主题",
        ),
        _article(
            link="later-9x",
            event_id=event_id,
            available_at=_ts("2026-07-11 10:00"),
            score=9.2,
            theme="后续主题",
        ),
    ]

    result = _run_with_rows(monkeypatch, rows)
    bucket_8 = next(row for row in result["buckets"] if row["bucket"] == "8.0-8.9")
    bucket_9 = next(row for row in result["buckets"] if row["bucket"] == "9.0+")

    assert bucket_8["asset_t1_direction_samples"] == 1
    assert bucket_9["asset_t1_direction_samples"] == 1
    assert bucket_8["clustered_asset_t1_direction_samples"] == 1
    assert bucket_9["clustered_asset_t1_direction_samples"] == 0
    assert result["optimization"]["independent_events"] == 1
    assert result["optimization"]["independent_result_days"] == 1
    assert result["theme_win_rates"]["早期主题"]["clustered_t1_samples"] == 1
    assert result["theme_win_rates"]["后续主题"]["clustered_t1_samples"] == 0


@pytest.mark.parametrize(
    "earliest_patch",
    [
        {"direction": "中性"},
        {"backtest_symbol": None},
    ],
)
def test_later_direction_or_mapping_cannot_replace_earliest_signal(
    monkeypatch, earliest_patch
):
    event_id = str(uuid.uuid4())
    early = _article(
        link="early-ineligible",
        event_id=event_id,
        available_at=_ts("2026-07-11 01:00"),
        score=8.8,
        theme="早期主题",
    )
    early.update(earliest_patch)
    later = _article(
        link="later-convenient",
        event_id=event_id,
        available_at=_ts("2026-07-11 10:00"),
        score=9.2,
        theme="后续主题",
    )

    result = _run_with_rows(monkeypatch, [early, later])
    bucket_8 = next(row for row in result["buckets"] if row["bucket"] == "8.0-8.9")
    bucket_9 = next(row for row in result["buckets"] if row["bucket"] == "9.0+")

    assert bucket_9["asset_t1_direction_samples"] == 1
    assert bucket_8["clustered_asset_t1_direction_samples"] == 0
    assert bucket_9["clustered_asset_t1_direction_samples"] == 0
    assert result["optimization"]["independent_events"] == 0
    assert result["theme_win_rates"]["后续主题"]["clustered_t1_samples"] == 0


def test_repeated_articles_cannot_manufacture_publishable_sample(monkeypatch):
    event_id = str(uuid.uuid4())
    base = _ts("2026-07-11 10:00")
    rows = [
        _article(
            link=f"copy-{index}",
            event_id=event_id,
            available_at=base + index,
            score=9.2,
        )
        for index in range(30)
    ]

    result = _run_with_rows(monkeypatch, rows, snapshot_ledger=False)
    bucket = next(row for row in result["buckets"] if row["bucket"] == "9.0+")
    html = backtest.render_backtest_html(result)

    assert bucket["asset_t1_direction_samples"] == 30
    assert bucket["asset_t1_direction_sample_sufficient"] is False
    assert bucket["clustered_asset_t1_direction_samples"] == 1
    assert bucket["clustered_asset_t1_direction_sample_sufficient"] is False
    assert result["optimization"]["independent_events"] == 0
    assert "待积累" in html
    assert "映射资产 T+1方向</span><b>100.0%" not in html


def test_missing_result_date_is_diagnostic_only_not_a_price_outcome(monkeypatch):
    """F6 excludes undated prices from raw metrics too, not only inference.

    Previously the fixture counted as one descriptive direction sample. Without
    an exact result day or observed close evidence it now stays diagnostic-only;
    do not manufacture a timestamp or a result date to preserve that old count.
    """
    row = _article(
        link="missing-result-date",
        event_id=str(uuid.uuid4()),
        available_at=_ts("2026-07-11 10:00"),
        score=9.2,
    )
    monkeypatch.setattr(
        backtest,
        "_future_snapshot",
        lambda *_args: {"change_amt": 1.0, "change_pct": 0.5},
    )
    history = {
        "count": 1,
        "first_available_at": row["available_at"],
        "collection_started_at": row["available_at"],
        "data_source": "signal_snapshots",
    }
    monkeypatch.setattr(
        backtest,
        "load_config",
        lambda: {"scoring_strategy_version": "strategy-v1"},
    )
    monkeypatch.setattr(
        backtest,
        "_load_backtest_samples",
        lambda *_args, **_kwargs: ([row], history),
    )
    monkeypatch.setattr(backtest, "_same_day_snapshot", lambda *_args: None)

    result = backtest.run_score_backtest(days=90)
    bucket = next(row for row in result["buckets"] if row["bucket"] == "9.0+")

    assert bucket["asset_t1_direction_samples"] == 0
    assert bucket["benchmark_t1_direction_samples"] == 0
    assert bucket["t1_signal_return_samples"] == 0
    assert bucket["t1_excess_return_samples"] == 0
    assert bucket["t1_avg_signal_return_pct"] is None
    assert bucket["t1_avg_excess_return_pct"] is None
    assert bucket["clustered_asset_t1_direction_samples"] == 0
    assert result["unclusterable_metric_observations"] > 0
    assert result["optimization"]["independent_events"] == 0


def test_governance_state_is_visible_but_never_auto_applied():
    score_state = backtest._score_optimization_state(
        independent_events=0,
        independent_result_days=0,
        proxy_coverage=50.0,
    )
    score_result = {
        "history_status": "accumulating",
        "selection_policy": "stored_aggregation_threshold",
        "days": 90,
        "actual_window_days": 1,
        "deduped_samples": 0,
        "unique_signal_days": 0,
        "proxy_coverage": 0,
        "buckets": [],
        "optimization": score_state,
    }
    score_compact = backtest.render_backtest_compact_html(score_result)
    score_full = backtest.render_backtest_html(score_result)

    assert "调参研究资格：调参样本待积累" in score_compact
    assert "保持现行评分" in score_compact
    assert "不自动应用" in score_compact
    assert "调参样本待积累 → 入选样本达标，对照待补齐 → 调参研究样本条件满足 → 影子验证 → 可发布" in score_full
    assert "样本达标不代表模型质量合格" in score_full
    assert "后续研究流程</summary>" in score_full

    mapping_result = {
        "enabled": True,
        "status": "collecting",
        "status_label": "数据积累中",
        "formal_pairs": 0,
        "directional_pairs": 0,
        "relation_rows": [],
        "horizons": [],
        "optimization": _mapping_optimization_state([], None),
    }
    mapping_compact = render_mapping_validation_compact_html(mapping_result)
    mapping_full = render_mapping_validation_html(mapping_result)

    assert "映射策略：样本不足" in mapping_compact
    assert "保持现行映射关系" in mapping_compact
    assert "不自动应用" in mapping_compact
    assert "样本不足 → 候选优化 → 影子验证 → 可发布" in mapping_full


def test_score_history_explains_version_boundaries_and_unsettled_control():
    result = {
        "history_status": "accumulating",
        "selection_policy": "stored_aggregation_threshold",
        "days": 90,
        "actual_window_days": 1,
        "deduped_samples": 8,
        "unique_signal_days": 1,
        "proxy_coverage": 50.0,
        "buckets": [],
        "optimization": backtest._score_optimization_state(
            independent_events=3,
            independent_result_days=1,
            proxy_coverage=50.0,
            control_cohort_recorded=True,
            control_cohort_unselected_signals=921,
        ),
        "cohort_history": {
            "current_scored_signals": 929,
            "current_selected_signals": 8,
            "current_selected_signal_days": 1,
            "family_scored_signals": 10301,
            "family_selected_signals": 170,
            "family_selected_signal_days": 15,
            "version_count": 8,
            "archived_selected_signals": 162,
            "family": "2026-05-19-v2",
            "current_version": "2026-05-19-v2:current-fingerprint",
        },
    }

    html = backtest.render_backtest_html(result)

    assert "未入选921条" in html
    assert "每次评分契约变更都会建立新的正式队列" in html
    assert "需按全分数对照口径独立结算后" in html
    assert "不自动应用" in html
