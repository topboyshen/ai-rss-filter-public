from copy import deepcopy

import pytest

from services.backtest import _build_score_eligibility_funnel


def _inputs(*, data_source="signal_snapshots", min_score=None):
    return {
        "data_source": data_source,
        "min_score": min_score,
        "history": {
            "ledger_revision_count": "12",
            "ledger_count": 9,
        },
        "cohort_history": {
            "current_scored_signals": "8",
            "current_selected_signals": 5,
            "current_unselected_signals": 3,
        },
        "query_rows": 5,
        "wire_deduped": 4,
        "valid_score": 3,
        "proxy_mapped": 2,
        "optimization_state": {
            "independent_events": "2",
            "independent_result_days": 1,
        },
        "control_cohort": {
            "unselected": {
                "recorded_signals": "3",
                "deduped_signals": 3,
                "valid_score_signals": 2,
                "proxy_mapped_signals": "1",
                "t1_settled_event_results": 1,
                "independent_result_days": "1",
            }
        },
    }


def test_build_score_eligibility_funnel_preserves_result_and_inputs():
    inputs = _inputs()
    before = deepcopy(inputs)

    result = _build_score_eligibility_funnel(**inputs)

    assert result == {
        "scope": "current_exact_contract",
        "formal_eligible": True,
        "note": "资格过滤不代表删除数据；账本信号与事件结果是不同计量单位，历史契约不合并。",
        "ledger": {
            "all_contract_revisions": 12,
            "all_contract_latest_independent": 9,
            "current_contract_scored": 8,
            "current_contract_selected": 5,
            "current_contract_unselected": 3,
        },
        "selected_analysis": {
            "query_rows": 5,
            "wire_deduped": 4,
            "valid_score": 3,
            "proxy_mapped": 2,
            "t1_independent_events": 2,
            "t1_independent_result_days": 1,
        },
        "control_analysis": {
            "recorded": 3,
            "wire_deduped": 3,
            "valid_score": 2,
            "proxy_mapped": 1,
            "t1_settled_event_results": 1,
            "t1_independent_result_days": 1,
        },
    }
    assert inputs == before


@pytest.mark.parametrize(
    ("data_source", "min_score", "formal_eligible"),
    [
        ("signal_snapshots", None, True),
        ("signal_snapshots", 0.0, False),
        ("legacy_news_cache", None, False),
    ],
    ids=("production", "exploratory", "legacy"),
)
def test_build_score_eligibility_funnel_formal_eligibility(
    data_source, min_score, formal_eligible
):
    result = _build_score_eligibility_funnel(
        **_inputs(data_source=data_source, min_score=min_score)
    )

    assert result["scope"] == "current_exact_contract"
    assert result["formal_eligible"] is formal_eligible
