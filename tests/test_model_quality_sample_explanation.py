from copy import deepcopy

import pytest

from services import backtest


def _result():
    return {
        "history_status": "accumulating",
        "selection_policy": "stored_aggregation_threshold",
        "days": 90,
        "actual_window_days": 1,
        "deduped_samples": 2,
        "unique_signal_days": 1,
        "proxy_coverage": 50,
        "buckets": [],
        "cohort_history": {
            "current_scored_signals": 19,
            "current_selected_signals": 2,
            "current_selected_signal_days": 1,
            "family_scored_signals": 1000,
            "family_selected_signals": 100,
            "archived_selected_signals": 98,
            "version_count": 2,
        },
        "eligibility_funnel": {
            "formal_eligible": True,
            "ledger": {
                "all_contract_revisions": 42713,
                "current_contract_scored": 19,
                "current_contract_selected": 2,
                "current_contract_unselected": 17,
            },
        },
        "optimization": backtest._score_optimization_state(
            independent_events=0,
            independent_result_days=0,
            proxy_coverage=50,
            control_cohort_recorded=True,
            control_cohort_unselected_signals=17,
        ),
    }


@pytest.mark.parametrize(
    "render", [backtest.render_backtest_compact_html, backtest.render_backtest_html]
)
def test_explanation_separates_retained_revisions_from_current_eligible_samples(render):
    result = _result()
    before = deepcopy(result)
    html = render(result)
    assert "当前契约全分数 19 条，其中入选 2 条" in html
    assert "全库历史修订 42713 条仅表示保留规模" in html
    assert "不等同独立样本" in html
    assert "不自动计入当前队列" in html
    assert "不等于历史记录丢失" in html
    assert "已结算独立事件 0/100" in html
    assert "结果交易日 0/60" in html
    assert "行情代理覆盖 50.00%/80.00%" in html
    assert "反复刷新不会让 T+1/T+2 提前结算" in html
    assert "镜像版本本身不决定策略指纹" in html
    assert result == before
    assert result["optimization"]["can_tune_thresholds"] is False


def test_explanation_uses_declared_requirements_and_does_not_hide_control_deficit():
    result = _result()
    result["optimization"]["requirements"].update(
        independent_events=150, independent_result_days=75, proxy_coverage=85
    )
    html = backtest._render_score_sample_explanation_html(result)
    assert "0/150" in html and "0/75" in html and "50.00%/85.00%" in html
    assert "对照尚未独立结算充分" in html


def test_mature_explanation_does_not_claim_missing_results_or_apply_changes():
    result = _result()
    result["optimization"] = backtest._score_optimization_state(
        independent_events=150,
        independent_result_days=75,
        proxy_coverage=95,
        control_cohort_available=True,
    )
    before = deepcopy(result)
    html = backtest._render_score_sample_explanation_html(result)
    assert "调参研究样本条件已满足；不代表模型质量合格，仍不自动调整策略" in html
    assert "对照尚未独立结算充分" not in html
    assert result == before


def test_exploratory_explanation_cannot_promote_formal_eligibility():
    result = _result()
    result["eligibility_funnel"]["formal_eligible"] = False
    assert "不推进正式优化条件" in backtest._render_score_sample_explanation_html(
        result
    )
    assert result["eligibility_funnel"]["formal_eligible"] is False


def test_rounded_coverage_cannot_promote_optimization_readiness():
    result = _result()
    result["optimization"] = backtest._score_optimization_state(
        independent_events=150,
        independent_result_days=75,
        proxy_coverage=79.999,
        control_cohort_available=True,
    )
    assert result["optimization"]["proxy_coverage"] == 80
    assert result["optimization"]["can_tune_thresholds"] is False
    html = backtest._render_score_sample_explanation_html(result)
    assert "调参研究样本条件已满足" not in html
    assert "以原有优化状态为准" in html


def test_missing_cohort_metadata_is_not_reported_as_zero_retained_history():
    assert backtest._render_score_sample_explanation_html({}) == ""
    result = _result()
    del result["eligibility_funnel"]["ledger"]["all_contract_revisions"]
    html = backtest._render_score_sample_explanation_html(result)
    assert "历史保留规模未提供" in html
    assert "全库历史修订 0" not in html
