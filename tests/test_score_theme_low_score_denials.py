"""Low neutral denial clauses (synthetic fixtures; no production text)."""

import json

import pytest

from core.score_evidence import strip_low_score_denial_clauses
from core.scorer import (
    _DENIAL_SCOPE_THEME_GROUPS,
    Scorer,
    _score_grounding_failure,
    _score_grounding_verdict,
)

SOURCE = "某国外长举行例行通话，双方交换意见。"


@pytest.mark.parametrize(
    "logic",
    [
        "例行外交通话，属海外事件，与A股宏观流动性及政策无关。",
        "例行外交通话，与A股宏观流动性及资本市场制度无关",
        "例行外交通话，非国家级政策或流动性事件",
        "例行外交通话，属行业动态，非宏观流动性或国家级政策",
        "例行外交通话，而非货币政策事件",
        "例行外交通话，不涉及A股流动性或资本市场政策",
        "例行外交通话，未涉货币政策或大盘极端情绪",
        "例行外交通话，无央行政策、社融异常、北向巨幅异动或国家级政策。",
        "例行外交通话，与AI芯片算力等科技产业无关",
        "例行外交通话，与AI、芯片、半导体、算力等科技政策无关。",
        "例行外交通话，与A股宏观流动性无直接关联",
        "例行外交通话，对A股宏观流动性无直接影响",
        "外长通话与A股宏观流动性无关，属海外消息。",
    ],
)
@pytest.mark.parametrize("score,direction", [(0, "⚖️"), (2, "中性"), (4, "⚖️")])
def test_low_neutral_denial_clause_is_not_a_theme_claim(logic, score, direction):
    assert not _score_grounding_failure((score, "外交动态", logic, direction), SOURCE, "")


@pytest.mark.parametrize(
    "logic",
    [
        # A positive claim next to a denial is still checked.
        "流动性改善，与A股宏观流动性无关。",
        "与A股宏观流动性无关，但流动性改善。",
        "不涉及AI，但算力需求大增",
        "非国家级政策，央行降准释放资金",
        # Contrast or positive predicates inside the clause block stripping.
        "与流动性改善无关",
        "非流动性宽松事件",
        "无降准但流动性充裕的信号",
        # Double negation / not a closed denial.
        "并非与A股宏观流动性无关。",
        "不是与流动性无关。",
        "与流动性无关紧要的变化有关。",
        "与A股宏观流动性。无关。",
        # Words starting with 非/无 that are not negations.
        "非农数据超预期，流动性预期变化",
        "非银机构流动性事件",
        "无人机产业链受AI驱动",
        "无疑影响流动性政策",
        # The subject of "X与…无关" keeps its own theme marker.
        "AI算力需求与A股流动性无关",
    ],
)
def test_affirmative_or_ambiguous_mentions_are_still_checked(logic):
    assert (
        _score_grounding_failure((2, "外交动态", logic, "⚖️"), SOURCE, "")
        == "ungrounded_score_theme"
    )


@pytest.mark.parametrize(
    "score,direction",
    [(4.5, "⚖️"), (5, "⚖️"), (8, "⚖️"), (2, "📈"), (2, "📉"), (3, "看多")],
)
def test_denial_stripping_needs_low_score_and_neutral_direction(score, direction):
    logic = "例行外交通话，非国家级政策或流动性事件"
    assert (
        _score_grounding_failure((score, "外交动态", logic, direction), SOURCE, "")
        == "ungrounded_score_theme"
    )


@pytest.mark.parametrize("tag", ["流动性", "宏观放水", "AI算力"])
def test_affirmative_tag_is_never_exempted(tag):
    logic = "例行外交通话，与A股宏观流动性及政策无关"
    assert (
        _score_grounding_failure((1, tag, logic, "⚖️"), SOURCE, "")
        == "ungrounded_score_theme"
    )


def test_remaining_logic_still_gets_numeric_fact_checks():
    logic = "外长通话三次，与A股宏观流动性及政策无关"
    assert (
        _score_grounding_failure((1, "外交动态", logic, "⚖️"), SOURCE, "")
        == "ungrounded_score_facts"
    )


def test_strip_keeps_delimiters_and_other_clauses_verbatim():
    assert (
        strip_low_score_denial_clauses(
            "海外消息，与a股宏观流动性及政策无关；但流动性改善。", _DENIAL_SCOPE_THEME_GROUPS
        )
        == "海外消息，；但流动性改善。"
    )
    assert (
        strip_low_score_denial_clauses(
            "外长通话与a股宏观流动性无关，属海外消息。", _DENIAL_SCOPE_THEME_GROUPS
        )
        == "外长通话，属海外消息。"
    )


def _validator_verdict(logic: str, score: int = 2, sentiment: str = "中性") -> str:
    """Final verdict through the production entry: raw JSON -> parse -> grounding."""
    raw = json.dumps(
        {"score": score, "tag": "外交动态", "logic": logic, "sentiment": sentiment},
        ensure_ascii=False,
    )
    parsed = Scorer._parse_score_result(raw)
    assert parsed[0] == score
    return _score_grounding_verdict(parsed, SOURCE, "")[0] or "accept"


# Coordinated objects (以及/及/和/、/或 + noun phrase) and closed forms
# (与X无关 / 对X无影响 / 和X没有关系) are denials, whatever X names.
DENIALS_ACCEPTED = [
    "不涉及AI以及半导体",
    "不涉及AI、半导体或算力",
    "不涉及AI及芯片",
    "与芯片出口无关",
    "对芯片出口无直接影响",
    "与人工智能产业政策无关",
    "和芯片没有关系",
    "与AI政策无关",
    "与AI芯片出口及央行政策无关",
    "对A股流动性及芯片出口无影响",
    "外长通话与芯片出口无关",
    "与外资情绪及半导体出口无关",
    "不涉及AI。",
    "与半导体无关，属外交事件",
    "例行通话，不涉及AI",
]
# New statements after connectors, predicates after a list separator, and
# open forms whose scope runs two themes/events together are still checked.
CLAIMS_REJECTED = [
    "不涉及AI并且央行降准",
    "不涉及AI同时央行降准",
    "不涉及AI，同时央行降准",
    "与AI无关而且央行降准",
    "与AI无关，央行宣布降息",
    "与AI无关但芯片出口回暖",
    "非政策事件且利好半导体",
    "不涉及AI另外央行降准",
    "不涉及AI此外央行降准",
    "非宏观事件并央行降准",
    "不涉及AI以及央行宣布降准",
    "不涉及AI以及央行降准",
    "不涉及AI和央行降息",
    "不涉及AI及央行宣布降准",
    "与芯片无关央行已降准",
    "并非与流动性无关的事件",
    "不涉及AI央行降准",
    "不涉及AI芯片出口回暖",
    "不涉及AI央行宣布降准",
    "不涉及AI芯片订单中标",
    "非AI降息事件",
    "无AI央行政策影响",
    "与芯片出口回暖无关",
]


@pytest.mark.parametrize("logic", DENIALS_ACCEPTED)
@pytest.mark.parametrize("score,sentiment", [(0, "中性"), (4, "中性")])
def test_denial_forms_accept_through_validator(logic, score, sentiment):
    assert _validator_verdict(logic, score, sentiment) == "accept"


@pytest.mark.parametrize("logic", CLAIMS_REJECTED)
@pytest.mark.parametrize("score,sentiment", [(0, "中性"), (4, "中性")])
def test_claims_beside_denials_reject_through_validator(logic, score, sentiment):
    assert _validator_verdict(logic, score, sentiment) == "ungrounded_score_theme"


@pytest.mark.parametrize("logic", DENIALS_ACCEPTED[:4])
def test_denials_are_not_exempt_above_four(logic):
    assert _validator_verdict(logic, 6, "中性") == "ungrounded_score_theme"


@pytest.mark.parametrize(
    "logic,kept",
    [
        ("不涉及ai并且央行降准", "并且央行降准"),
        ("与ai无关但芯片出口回暖", "但芯片出口回暖"),
        ("非政策事件且利好半导体", "且利好半导体"),
        ("不涉及ai以及半导体", ""),
        ("与芯片出口无关", ""),
        ("对芯片出口无直接影响", ""),
        ("与人工智能产业政策无关", ""),
        ("外长通话与芯片出口无关", "外长通话"),
        ("不涉及ai以及央行降准", "不涉及ai以及央行降准"),
        ("不涉及ai及央行宣布降准", "不涉及ai及央行宣布降准"),
        ("不涉及ai央行降准", "不涉及ai央行降准"),
        ("不涉及ai芯片出口回暖", "不涉及ai芯片出口回暖"),
        ("不涉及ai且与宏观流动性无关", ""),
        ("与并购重组无关", ""),
        ("不涉及ai。", "。"),
        ("与半导体无关，属外交事件", "，属外交事件"),
        ("不涉及某某某某某某某某某某某某某", "不涉及某某某某某某某某某某某某某"),
    ],
)
def test_exact_post_strip_text(logic, kept):
    assert strip_low_score_denial_clauses(logic, _DENIAL_SCOPE_THEME_GROUPS) == kept
