"""Quarter durations must be parsed completely without weakening grounding."""

import json
from types import SimpleNamespace

import pytest

from core.scorer import (
    Scorer,
    _has_unsupported_numeric_expression,
    _numeric_fact_occurrences,
    _numeric_facts,
    _score_grounding_verdict,
)


@pytest.fixture
def validate_attempt(monkeypatch):
    monkeypatch.setattr("core.scorer.record_score_validation", lambda **kwargs: None)
    monkeypatch.setattr("core.scorer.capture_validation_attempt", lambda **kwargs: None)
    scorer = object.__new__(Scorer)

    def validate(source, logic):
        raw = json.dumps(
            dict(score=7, tag="公司业绩", logic=logic, sentiment="看多"),
            ensure_ascii=False,
        )
        return scorer._validate_scoring_attempt(
            raw, "甲公司披露季度利润", source, "initial", "", "stage1",
            SimpleNamespace(name="fixture", model="no-transport"), {},
        )

    return validate


@pytest.mark.parametrize("count", ["一", "两", "三", "1", "2", "12"])
def test_verbatim_quarter_duration_passes_complete_validator(validate_attempt, count):
    source = f"甲公司利润增长持续{count}个季度"
    parsed, reason, detail = validate_attempt(source, source)
    assert parsed == (7.0, "公司业绩", source, "📈")
    assert reason == detail == ""


def test_recorded_quarter_clause_is_supported_only_when_quoted(validate_attempt):
    source = "九个月的利润里，第三季度一个季度占了大头。"
    logic = "第三季度一个季度占了大头"
    parsed, reason, detail = validate_attempt(source, logic)
    assert parsed == (7.0, "公司业绩", logic, "📈")
    assert reason == detail == ""
    _, reason, _ = validate_attempt(source, "第三季度利润占大头")
    assert reason == "ungrounded_score_facts"


def test_recorded_paraphrase_does_not_gain_a_numeric_exemption(validate_attempt):
    source = (
        "甲公司业绩预增显著，mSAP工艺领先。"
        "九个月的利润里，第三季度一个季度占了大头。"
    )
    # The original rejected wording is not a direct quote of the numeric clause.
    logic = "业绩预增显著，第三季度利润占大头，mSAP工艺领先"
    _, reason, _ = validate_attempt(source, logic)
    assert reason == "ungrounded_score_facts"
    assert _score_grounding_verdict((7, "公司业绩", logic, "📈"), source, "") == (
        "ungrounded_score_facts", "numeric_fact_failure"
    )


@pytest.mark.parametrize("quarter", ["第一季度", "第三季度", "第1季度", "第3季度"])
def test_verbatim_ordinal_quarter_remains_supported(validate_attempt, quarter):
    source = f"甲公司{quarter}利润增长"
    _, reason, detail = validate_attempt(source, source)
    assert reason == detail == ""


@pytest.mark.parametrize(
    "text,kind,number",
    [("一个季度", "chinese", "一"), ("两个月", "chinese", "两"),
     ("三季度", "chinese", "三"), ("1个季度", "arabic", "1")],
)
def test_numeric_fact_covers_full_recognized_duration(text, kind, number):
    occurrences = _numeric_fact_occurrences(text)
    assert len(occurrences) == 1
    fact, start, end = occurrences[0]
    assert fact[0:2] == (kind, number)
    assert (start, end) == (0, len(text))
    assert _numeric_facts(text) == {fact}
    assert not _has_unsupported_numeric_expression(text)


@pytest.mark.parametrize(
    "source,logic",
    [
        ("甲公司利润增长持续一个季度", "甲公司利润增长持续两个季度"),
        ("甲公司利润增长持续1个季度", "甲公司利润增长持续2个季度"),
        ("甲公司利润增长持续一个季度", "甲公司利润增长持续一个月"),
        ("甲公司利润增长持续一个季度", "乙公司利润增长持续一个季度"),
        ("甲公司利润下降持续一个季度", "甲公司利润增长持续一个季度"),
        ("甲公司利润预计增长持续一个季度", "甲公司利润增长持续一个季度"),
        ("甲公司利润增长持续至少一个季度", "甲公司利润增长持续一个季度"),
        ("甲公司第一季度利润增长", "甲公司一个季度利润增长"),
        ("甲公司第三季度利润增长", "甲公司三个季度利润增长"),
        ("甲公司三个季度利润增长", "甲公司三季度利润增长"),
        ("甲公司第三季度一个季度利润增长", "甲公司第二季度一个季度利润增长"),
        ("甲公司三个季度利润增长", "三个季度利润增长"),
        ("甲公司一个季度利润增长", "甲公司一个季度利润增长；甲公司一个季度利润增长"),
        ("甲公司一个季度利润3亿元", "甲公司一个季度利润5亿元"),
        ("甲公司一个季度利润3亿元", "甲公司一个季度利润3万元"),
        ("甲公司一个季度利润增长3亿元", "甲公司一个季度利润下降3亿元"),
        ("甲公司一个季度利润3亿元；乙公司一个季度利润5亿元", "甲公司一个季度利润5亿元"),
    ],
)
def test_quarter_duration_keeps_fact_and_context_protections(validate_attempt, source, logic):
    _, reason, _ = validate_attempt(source, logic)
    assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize("duration", ["一余季度", "一多季度", "陆个季度"])
def test_other_unsupported_quarter_counts_are_not_whitelisted(validate_attempt, duration):
    source = f"甲公司利润增长持续{duration}"
    _, reason, _ = validate_attempt(source, source)
    assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize("numeral", ["壹", "贰", "叁", "肆", "伍", "陆", "柒", "捌", "玖", "拾", "佰", "仟"])
def test_financial_upper_numerals_stay_guarded_with_quarter_duration(validate_attempt, numeral):
    source = f"甲公司一个季度利润{numeral}万元"
    assert _has_unsupported_numeric_expression(source)
    _, reason, _ = validate_attempt(source, source)
    assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize("logic", ["一个季度", "1个季度", "一个季度，一个季度"])
def test_bare_or_repeated_quarter_count_is_not_a_fact_anchor(validate_attempt, logic):
    _, reason, _ = validate_attempt("甲公司利润增长持续一个季度", logic)
    assert reason == "ungrounded_score_facts"


def test_ordinary_soft_landing_and_other_guards_remain_independent():
    source = "甲公司披露电动汽车反补贴案软着陆，一个季度利润增长3亿元"
    assert _score_grounding_verdict((7, "公司业绩", source, "📈"), source, "") == ("", "")
    assert _score_grounding_verdict(
        (7, "公司业绩", source.replace("3亿元", "5亿元"), "📈"), source, ""
    ) == ("ungrounded_score_facts", "numeric_fact_failure")
