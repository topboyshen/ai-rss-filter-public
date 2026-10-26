"""Two frozen independent denial tails must not hide substantive claims."""

import json
from types import SimpleNamespace

import pytest

from core.score_evidence import strip_low_score_denial_clauses
from core.scorer import Scorer, _DENIAL_SCOPE_THEME_GROUPS, _score_grounding_verdict


TAILS = ("非宏观流动性焦点", "无宏观政策增量")


@pytest.fixture
def validate_attempt(monkeypatch):
    # Run real parsing and all grounding gates, without a client or persistence.
    monkeypatch.setattr("core.scorer.record_score_validation", lambda **kwargs: None)
    monkeypatch.setattr("core.scorer.capture_validation_attempt", lambda **kwargs: None)
    scorer = object.__new__(Scorer)

    def validate(source, logic, *, score=2, tag="行业数据", sentiment="中性"):
        raw = json.dumps(
            dict(score=score, tag=tag, logic=logic, sentiment=sentiment),
            ensure_ascii=False,
        )
        return scorer._validate_scoring_attempt(
            raw, source, "", "initial", "", "stage1",
            SimpleNamespace(name="frozen-fixture", model="no-transport"), {},
        )

    return validate


@pytest.mark.parametrize(
    "source,logic,score,tag",
    [
        (
            "9月新能源乘用车批发销量达到166.6万辆，同比增长11.3%",
            "9月新能源乘用车批发销量达到166.6万辆，同比增长11.3%，非宏观流动性焦点",
            3,
            "行业数据",
        ),
        (
            "22种产品价格上涨，23种下降，5种持平",
            "22种产品价格上涨，23种下降，5种持平，无宏观政策增量",
            2,
            "生产资料价格",
        ),
    ],
)
def test_supported_facts_with_recorded_denial_tail_pass_complete_validator(
    validate_attempt, source, logic, score, tag
):
    parsed, reason, detail = validate_attempt(source, logic, score=score, tag=tag)
    assert parsed == (float(score), tag, logic, "⚖️")
    assert reason == detail == ""


@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize("boundary", ["，", ",", "；", ";", "。"])
@pytest.mark.parametrize("ending", ["", "。", "；", "！"])
@pytest.mark.parametrize("score", [0, 4])
def test_terminal_independent_tail_keeps_low_score(
    validate_attempt, tail, boundary, ending, score
):
    source = "甲公司举行例行会议"
    logic = f"甲公司举行例行会议{boundary}{tail}{ending}"
    parsed, reason, detail = validate_attempt(source, logic, score=score)
    assert parsed[0] == score
    assert reason == detail == ""


@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize(
    "score,sentiment", [(4.5, "中性"), (6, "中性"), (2, "看多"), (2, "看空")]
)
def test_high_or_directional_scores_keep_theme_protection(
    validate_attempt, tail, score, sentiment
):
    _, reason, _ = validate_attempt(
        "甲公司举行例行会议", f"甲公司举行例行会议，{tail}",
        score=score, sentiment=sentiment,
    )
    assert reason == "ungrounded_score_theme"


@pytest.mark.parametrize("whitespace", ["\n", "\t"])
@pytest.mark.parametrize("tail", TAILS)
def test_tail_does_not_relax_invalid_reason_format(validate_attempt, tail, whitespace):
    parsed, reason, _ = validate_attempt(
        "甲公司举行例行会议", f"甲公司举行例行会议{whitespace}{tail}"
    )
    assert parsed[0] < 0
    assert reason == "invalid_score_contract"


@pytest.mark.parametrize("ending", [" ", "。 \u3000"])
@pytest.mark.parametrize("tail", TAILS)
def test_tail_does_not_relax_reason_trailing_whitespace(validate_attempt, tail, ending):
    parsed, reason, _ = validate_attempt(
        "甲公司举行例行会议", f"甲公司举行例行会议，{tail}{ending}"
    )
    assert parsed[0] < 0
    assert reason == "invalid_score_contract"


@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize("tag", ["宏观政策", "流动性", "AI算力"])
def test_tag_claims_are_never_exempted(validate_attempt, tail, tag):
    _, reason, _ = validate_attempt(
        "甲公司举行例行会议", f"甲公司举行例行会议，{tail}", tag=tag,
    )
    assert reason == "ungrounded_score_theme"


@pytest.mark.parametrize(
    "tail",
    [
        "央行降准，非宏观流动性焦点",
        "AI政策改善，无宏观政策增量",
        "非宏观流动性焦点但央行降准",
        "非宏观流动性焦点，央行降准",
        "无宏观政策增量，AI订单增长",
        "无宏观政策增量；宏观政策改善",
        "无宏观政策增量。央行降准",
        "无宏观政策增量，同时央行降准",
        "并非无宏观政策增量",
        "并非 无宏观政策增量",
        "不是无宏观政策增量",
        "不是非宏观流动性焦点",
        "非宏观流动性焦点公司获批",
        "无宏观政策增量公司获得订单",
        "无AI宏观政策增量",
        "非AI宏观流动性焦点",
        "无宏观政策3次增量",
        "无宏观政策公司裁撤增量",
        "甲公司非宏观流动性焦点",
        "甲公司无宏观政策增量",
    ],
)
def test_ambiguous_non_independent_or_following_claims_are_checked(validate_attempt, tail):
    _, reason, _ = validate_attempt(
        "甲公司举行例行会议", f"甲公司举行例行会议，{tail}"
    )
    assert reason == "ungrounded_score_theme"


@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize(
    "source,logic,kind",
    [
        ("甲公司利润增长3亿元", "乙公司利润增长3亿元", "numeric_fact_failure"),
        ("甲公司利润增长3亿元", "甲公司利润增长5亿元", "numeric_fact_failure"),
        ("甲公司利润增长3亿元", "甲公司利润增长3万元", "numeric_fact_failure"),
        ("甲公司利润下降3亿元", "甲公司利润增长3亿元", "numeric_fact_failure"),
        ("甲公司预计利润增长3亿元", "甲公司利润增长3亿元", "numeric_fact_failure"),
        ("甲公司利润至少增长3亿元", "甲公司利润增长3亿元", "numeric_fact_failure"),
        ("甲公司未获得订单", "甲公司获得订单", "event_fact_failure"),
        ("甲公司计划获得订单", "甲公司获得订单", "event_fact_failure"),
        ("甲公司获得乙公司订单", "甲公司获得丙公司订单", "event_fact_failure"),
        ("甲公司披露利润陆万元", "甲公司披露利润陆万元", "numeric_fact_failure"),
    ],
)
def test_denial_tail_never_exempts_subject_number_unit_direction_or_negation(
    validate_attempt, tail, source, logic, kind
):
    logic = f"{logic}，{tail}"
    _, reason, detail = validate_attempt(source, logic)
    assert reason == "ungrounded_score_facts"
    assert detail == ""
    assert _score_grounding_verdict((2, "行业数据", logic, "⚖️"), source, "") == (
        "ungrounded_score_facts", kind,
    )


@pytest.mark.parametrize(
    "source,logic",
    [
        (
            "乘联分会：9月新能源乘用车批发销量达到166.6万辆，同比增长11.3%",
            "乘联分会9月新能源乘用车批发销量166.6万辆，同比增长11.3%，属常规行业数据，非宏观流动性焦点",
        ),
        (
            "全国流通领域9大类50种产品中，22种产品价格上涨，23种下降，5种持平",
            "国家统计局例行旬度价格监测，50种产品中22涨23降5持平，属常规数据发布，无宏观政策增量。",
        ),
    ],
)
def test_original_recorded_wording_still_requires_other_fact_checks(
    validate_attempt, source, logic
):
    # The exact old attempts have additional attribution/numeric differences.
    # Fixing their theme tail is not evidence that the whole old output is valid.
    _, reason, _ = validate_attempt(source, logic)
    assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize("tail", TAILS)
def test_strip_keeps_all_other_text_and_delimiters(tail):
    assert strip_low_score_denial_clauses(
        f"甲公司举行例行会议，{tail}。", _DENIAL_SCOPE_THEME_GROUPS
    ) == "甲公司举行例行会议，。"
    unchanged = f"甲公司举行例行会议，{tail}，属行业动态"
    assert strip_low_score_denial_clauses(unchanged, _DENIAL_SCOPE_THEME_GROUPS) == unchanged
    assert strip_low_score_denial_clauses(
        f"甲公司举行例行会议，{tail}。  ", _DENIAL_SCOPE_THEME_GROUPS
    ) == "甲公司举行例行会议，。  "
