"""Pure decomposition tests: zero, missing data and model boundaries differ."""

import math

import pytest

from scoring_explanation import (
    EXPLANATION_FIELDS,
    build_score_explanation,
    empty_score_explanation,
    normalize_score_explanation,
    rebase_score_explanation,
)


def test_decomposition_records_real_capped_adjustment_and_signed_margin():
    result = build_score_explanation(
        raw_score=9.8,
        score=10,
        score_path="llm",
        score_threshold=11,
    )
    assert result["raw_score"] == 9.8
    assert result["score_adjustment"] == pytest.approx(0.2)
    assert result["score_margin"] == -1
    assert result["stage1_stage2_delta"] is None
    assert result["fast_pass_hint_score"] is None


def test_real_zero_is_not_unknown():
    result = build_score_explanation(
        raw_score=0,
        score=0,
        score_path="stage2",
        score_threshold=0,
        stage1_score=0,
        stage2_score=0,
        stage1_model="first",
        stage2_model="second",
        fast_pass_hint={"score": 0},
    )
    assert result == dict.fromkeys(EXPLANATION_FIELDS, 0.0)


@pytest.mark.parametrize(
    "path",
    [
        "llm_safe_neutral_contract",
        "stage2_safe_neutral_grounding",
        "quality_quarantine",
        "parse_error",
        "similarity",
    ],
)
def test_non_evidence_paths_do_not_fabricate_raw_model_scores(path):
    assert (
        build_score_explanation(
            raw_score=4,
            score=4,
            score_path=path,
            score_threshold=7,
            fast_pass_hint={"score": 8.5},
        )
        == empty_score_explanation()
    )


@pytest.mark.parametrize(
    "stage1_model,stage2_model,path",
    [
        ("fallback_local", "second", "stage2"),
        ("first", "fallback_local", "fallback_local"),
        ("", "second", "stage2"),
        (None, "second", "stage2"),
        ("first", None, "stage2"),
    ],
)
def test_delta_requires_two_identified_model_stages(stage1_model, stage2_model, path):
    assert (
        build_score_explanation(
            raw_score=8,
            score=8,
            score_path=path,
            score_threshold=7,
            stage1_score=7,
            stage2_score=8,
            stage1_model=stage1_model,
            stage2_model=stage2_model,
        )["stage1_stage2_delta"]
        is None
    )


def test_rebase_preserves_original_pass_threshold_and_raw_score():
    original = build_score_explanation(
        raw_score=6,
        score=6.5,
        score_path="llm",
        score_threshold=7,
    )
    revised = rebase_score_explanation(original, score=8, score_path="radar")
    assert revised["raw_score"] == 6
    assert revised["score_threshold"] == 7
    assert revised["score_adjustment"] == 2
    assert revised["score_margin"] == 1
    assert (
        rebase_score_explanation({}, score=8, score_path="radar")
        == empty_score_explanation()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_score", math.nan),
        ("score_margin", math.inf),
        ("fast_pass_hint_score", True),
        ("score_adjustment", 3),
        ("score_threshold", None),
    ],
)
def test_invalid_or_inconsistent_explanation_is_rejected(field, value):
    result = build_score_explanation(
        raw_score=8, score=8, score_path="llm", score_threshold=7
    )
    result[field] = value
    with pytest.raises(ValueError):
        normalize_score_explanation(result, score=8, score_path="llm")
