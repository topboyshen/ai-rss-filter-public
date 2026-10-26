"""Explicit negative-information denials in low neutral reasons.

Fixtures are frozen live Kimi responses captured on 2026-10-04 in an isolated
verification run (no secrets; the titles/summaries are fixed verification
items, not production records).  All assertions go through the production
validation path: raw JSON -> Scorer._parse_score_result -> _score_grounding_verdict.
"""

import json

import pytest

from core.score_evidence import strip_explicit_theme_denials, strip_low_score_denial_clauses
from core.scorer import _DENIAL_SCOPE_THEME_GROUPS, Scorer, _score_grounding_verdict

SOURCE = "某国外长举行例行通话，双方交换意见。"


def _verdict(logic, score=2, tag="外交动态", sentiment="中性", title=SOURCE, summary=""):
    raw = json.dumps(
        {"score": score, "tag": tag, "logic": logic, "sentiment": sentiment},
        ensure_ascii=False,
    )
    return _raw_verdict(raw, title, summary)


def _raw_verdict(raw, title, summary):
    parsed = Scorer._parse_score_result(raw)
    assert parsed[0] >= 0
    return parsed, _score_grounding_verdict(parsed, title, summary)[0] or "accept"


LIVE_KIMI_FIXTURES = [
    pytest.param(
        '中报窗口期机构密集调研，创新药步入业绩与国际化兑现期',
        '机构调研集中在创新药企业，关注中报业绩和国际化进展。',
        '{"score":2,"tag":"机构调研","logic":"机构调研创新药企业属行业常规动态，无宏观政策或流动性增量信息","sentiment":"中性"}',
        'accept',
        id='candidate_image_0',
    ),  # live Kimi 2026-10-04, candidate image: 无…增量信息 now stripped
    pytest.param(
        '百利天恒：T-Bren联合帕妥珠单抗III期临床试验完成首例受试者入组',
        '公司公告，该III期临床试验已完成首例受试者入组。',
        '{"score":1,"tag":"个股公告","logic":"公司公告其III期临床试验完成首例受试者入组，属个股研发进展，无宏观或流动性增量信息。","sentiment":"中性"}',
        'ungrounded_score_facts',
        id='candidate_image_1',
    ),  # live Kimi 2026-10-04, candidate image: theme now grounded; still fails the unchanged numeric check (III期)
    pytest.param(
        '外交部：中法外长通电话，就双边关系交换意见',
        '双方就双边关系及共同关心的问题交换了意见。',
        '{"score":0,"tag":"外交动态","logic":"外长通话属常规外交活动，无宏观流动性或资本市场增量信息","sentiment":"中性"}',
        'accept',
        id='candidate_image_2',
    ),  # live Kimi 2026-10-04, candidate image: 无…增量信息 now stripped
    pytest.param(
        '中报窗口期机构密集调研，创新药步入业绩与国际化兑现期',
        '机构调研集中在创新药企业，关注中报业绩和国际化进展。',
        '{"score":2,"tag":"行业调研","logic":"机构调研创新药企业，关注中报业绩与国际化，属行业常规动态，无宏观政策增量","sentiment":"中性"}',
        'accept',
        id='.95_image_3',
    ),  # live Kimi 2026-10-04, .95 image: the finite independent tail gap is now fixed
]


@pytest.mark.parametrize("title,summary,raw,expected", LIVE_KIMI_FIXTURES)
def test_live_kimi_negative_information_fixtures(title, summary, raw, expected):
    parsed, verdict = _raw_verdict(raw, title, summary)
    assert verdict == expected
    # The model's own low score is kept, never rewritten.
    assert parsed[0] == json.loads(raw)["score"] <= 4


@pytest.mark.parametrize(
    "logic",
    [
        "无AI增量信息",
        "无新增AI信息",
        "没有AI增量信息",
        "未见AI相关信息",
        "未提供AI增量信息",
        "缺乏AI相关信息",
        "外长通话属常规外交活动，无宏观流动性或资本市场增量信息",
        "无宏观政策或流动性增量信息",
        "无AI、芯片或算力增量信息",
    ],
)
@pytest.mark.parametrize("score", [0, 2, 4])
def test_explicit_negative_information_accepts(logic, score):
    parsed, verdict = _verdict(logic, score=score)
    assert verdict == "accept"
    assert parsed[0] == score


@pytest.mark.parametrize(
    "logic",
    [
        "有AI增量信息",
        "并非没有AI增量信息",
        "不是没有AI增量信息",
        "并非无AI增量信息",
        "不是无AI相关信息",
        "并非没有AI",
        "不是没有AI影响",
        "无AI增量信息，但央行降准",
        "无AI增量信息并且央行降准",
        "无AI增量信息，同时央行宣布降息",
        "无AI增量信息宣布降准",
        "无AI央行降准信息",
        "无AI芯片出口回暖信息",
        "AI增量信息较多",
    ],
)
def test_affirmative_double_negation_or_following_claims_reject(logic):
    assert _verdict(logic)[1] == "ungrounded_score_theme"


@pytest.mark.parametrize("logic", ["无AI增量信息", "无新增AI信息", "没有AI增量信息"])
@pytest.mark.parametrize("score,sentiment", [(6, "中性"), (8, "看多"), (3, "看多"), (2, "看空")])
def test_high_or_directional_scores_are_not_relaxed(logic, score, sentiment):
    raw = json.dumps(
        {"score": score, "tag": "外交动态", "logic": logic, "sentiment": sentiment},
        ensure_ascii=False,
    )
    parsed = Scorer._parse_score_result(raw)
    assert parsed[0] == score
    assert _score_grounding_verdict(parsed, SOURCE, "")[0] == "ungrounded_score_theme"


@pytest.mark.parametrize("tag", ["AI算力", "流动性"])
def test_affirmative_tag_is_never_exempted(tag):
    assert _verdict("无AI增量信息", tag=tag)[1] == "ungrounded_score_theme"


def test_number_and_event_fact_checks_are_unchanged():
    assert _score_grounding_verdict(
        Scorer._parse_score_result(
            json.dumps(
                {"score": 2, "tag": "外交动态", "logic": "无AI增量信息，涉及3项协议", "sentiment": "中性"},
                ensure_ascii=False,
            )
        ),
        SOURCE,
        "",
    ) == ("ungrounded_score_facts", "numeric_fact_failure")
    assert _score_grounding_verdict(
        (2, "公司公告", "甲公司获得订单，无AI增量信息", "中性"),
        "甲公司建设工厂并计划获得订单",
        "",
    ) == ("ungrounded_score_facts", "event_fact_failure")


_GAPS = ["", " ", "  ", "\t", "\u3000", " \u3000\t"]


@pytest.mark.parametrize("gap", _GAPS)
@pytest.mark.parametrize("negator", ["并非", "不是"])
@pytest.mark.parametrize(
    "tail", ["没有AI增量信息", "没有AI", "无AI增量信息", "不涉及AI", "与AI无关"]
)
def test_reason_double_negation_rejects_with_any_whitespace(gap, negator, tail):
    logic = f"{negator}{gap}{tail}"
    raw = json.dumps(
        {"score": 2, "tag": "外交动态", "logic": logic, "sentiment": "中性"},
        ensure_ascii=False,
    )
    parsed = Scorer._parse_score_result(raw)
    if "\t" in gap:
        # A tab in logic already violates the score contract: never accepted.
        assert parsed[0] < 0
    else:
        assert _score_grounding_verdict(parsed, SOURCE, "")[0] == "ungrounded_score_theme"
    # Both stripping layers agree for every gap, tabs included.
    assert "AI" in strip_low_score_denial_clauses(logic, _DENIAL_SCOPE_THEME_GROUPS)
    assert "AI" in strip_explicit_theme_denials(logic, ("AI",))


def _source_verdict(logic, source, score=6, tag="AI应用", sentiment="看多"):
    raw = json.dumps(
        {"score": score, "tag": tag, "logic": logic, "sentiment": sentiment},
        ensure_ascii=False,
    )
    return _raw_verdict(raw, source, "")[1]


@pytest.mark.parametrize("gap", _GAPS)
@pytest.mark.parametrize("negator", ["并非", "不是"])
def test_source_double_negation_keeps_only_the_theme_match(gap, negator):
    source = f"甲公司{negator}{gap}没有AI业务"
    assert _source_verdict("甲公司布局AI业务", source) == "accept"
    # Only the AI anchor survives; it does not ground other themes or events.
    assert _source_verdict("甲公司布局芯片业务", source) == "ungrounded_score_theme"
    assert _source_verdict("甲公司AI业务获得订单", source) == "ungrounded_score_facts"


def test_plain_source_denial_still_removes_the_theme():
    assert _source_verdict("甲公司布局AI业务", "甲公司没有AI业务") == "ungrounded_score_theme"


@pytest.mark.parametrize("score,sentiment", [(6, "看多"), (2, "中性")])
@pytest.mark.parametrize(
    "source,logic,tag",
    [
        ("甲公司尚未获得订单", "甲公司获得订单", "公司公告"),
        ("甲公司并非没有AI业务，尚未获得订单", "甲公司获得订单", "公司公告"),
        ("甲公司并非 没有AI业务，尚未获得订单", "甲公司AI业务获得订单", "AI应用"),
        ("甲公司不是\u3000没有AI业务，尚未获得订单", "甲公司AI业务获得订单", "AI应用"),
    ],
)
def test_following_event_claims_are_still_fact_checked(source, logic, tag, score, sentiment):
    assert _source_verdict(logic, source, score, tag, sentiment) == "ungrounded_score_facts"
