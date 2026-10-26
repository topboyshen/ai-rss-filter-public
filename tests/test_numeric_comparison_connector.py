"""A clause-leading contrast may not change a quoted numeric comparison."""

import json
from types import SimpleNamespace

import pytest

from core.scorer import Scorer, _numeric_claims_are_grounded, _score_grounding_verdict


TITLE = "沪深两市成交额突破1万亿"
SOURCE = "36氪获悉，沪深两市成交额突破1万亿，较上一日此时缩量超1500亿。"
LOGIC = (
    "沪深两市成交额突破1万亿，但较上一日此时缩量超1500亿，"
    "属于常规行情播报，无行业增量信息。"
)


@pytest.fixture
def validate(monkeypatch):
    monkeypatch.setattr("core.scorer.record_score_validation", lambda **kwargs: None)
    monkeypatch.setattr("core.scorer.capture_validation_attempt", lambda **kwargs: None)
    scorer = object.__new__(Scorer)

    def validate_logic(logic, *, source=SOURCE, stage="stage1"):
        raw = json.dumps(
            dict(score=4, tag="A股市场", logic=logic, sentiment="中性"),
            ensure_ascii=False,
        )
        return scorer._validate_scoring_attempt(
            raw,
            TITLE,
            source,
            "initial",
            "",
            stage,
            SimpleNamespace(name="fixture", model="no-transport"),
            {},
        )

    return validate_logic


@pytest.mark.parametrize("stage", ["stage1", "stage2"])
def test_recorded_clause_leading_contrast_passes_full_validator(validate, stage):
    parsed, reason, detail = validate(LOGIC, stage=stage)
    assert parsed == (4.0, "A股市场", LOGIC, "⚖️")
    assert reason == detail == ""


@pytest.mark.parametrize("boundary", ["", "，", ",", "。", "；", ";", "！", "？", "、", "\n"])
def test_contrast_is_optional_only_at_a_comparison_clause_boundary(boundary):
    comparison = "较上一日此时缩量超1500亿"
    prefix = "市场成交播报" if boundary else ""
    assert _numeric_claims_are_grounded(
        f"{prefix}{boundary}但{comparison}", f"{prefix}{boundary}{comparison}"
    )


def test_source_contrast_can_also_be_omitted_without_changing_numeric_claim(validate):
    logic = LOGIC.replace("，但较", "，较")
    parsed, reason, detail = validate(logic, source=SOURCE.replace("，较", "，但较"))
    assert parsed == (4.0, "A股市场", logic, "⚖️")
    assert reason == detail == ""


@pytest.mark.parametrize(
    "logic",
    [
        LOGIC.replace("沪深两市", "港股市场"),
        LOGIC.replace("1500亿", "1600亿"),
        LOGIC.replace("1500亿", "1500万"),
        LOGIC.replace("缩量", "放量"),
        LOGIC.replace("超1500亿", "1500亿"),
        LOGIC.replace("此时", ""),
        LOGIC.replace("沪深两市", ""),
        LOGIC.replace("上一日", "上两日"),
        LOGIC.replace("但较", "但未较"),
        LOGIC.replace("但较", "但预计较"),
        LOGIC.replace("但较上一日此时", "较上一日但此时"),
        LOGIC.replace("，属于", "，但较上一日此时缩量超1500亿，属于"),
        # The shorter recorded fallback loses the market and same-time anchors.
        "成交额突破1万亿，较上一日缩量",
    ],
    ids=[
        "entity", "value", "unit", "direction", "threshold", "same-time",
        "missing-subject", "period", "negation", "modality", "nonboundary",
        "duplicate-claim", "shorter-recorded-fallback",
    ],
)
def test_contrast_never_exempts_changed_or_missing_numeric_context(validate, logic):
    parsed, reason, detail = validate(logic)
    assert parsed == (4.0, "A股市场", logic, "⚖️")
    assert reason == "ungrounded_score_facts"
    assert detail == ""
    assert _score_grounding_verdict(parsed, TITLE, SOURCE) == (
        "ungrounded_score_facts", "numeric_fact_failure"
    )


@pytest.mark.parametrize(
    "logic,source",
    [
        ("但未较上一日此时缩量超1500亿", "较上一日此时缩量超1500亿"),
        ("但预计较上一日此时缩量超1500亿", "较上一日此时缩量超1500亿"),
        ("但不较上一日此时缩量超1500亿", "较上一日此时缩量超1500亿"),
        ("较上一日但此时缩量超1500亿", "较上一日此时缩量超1500亿"),
        ("然而较上一日此时缩量超1500亿", "较上一日此时缩量超1500亿"),
        ("但较上一日利润增长20%营收下降10%", "较上一日利润增长10%营收下降20%"),
        ("甲公司但较上一日利润增长20%", "甲公司较上一日利润增长20%"),
        ("但较上一日利润增长20%", "但较上一日利润下降20%"),
        ("但较上一日利润增长20%", "但较上一日利润增长-20%"),
        ("但较上一日利润增长20%", "但较上一日利润增长±20%"),
        ("但较上一日利润增长20%", "但较上一日利润增长≈20%"),
        ("但较上一日合同金额陆万元", "较上一日合同金额陆万元"),
        ("但较上一日合同金额壹佰万元", "较上一日合同金额壹佰万元"),
    ],
)
def test_comparison_exception_keeps_binding_qualifiers_and_real_upper_numerals(logic, source):
    assert not _numeric_claims_are_grounded(logic, source)
