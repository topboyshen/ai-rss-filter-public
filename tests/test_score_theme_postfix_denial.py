"""Narrow diagnostic regressions, not human labels or external fact verification."""

import pytest

from core.score_evidence import strip_explicit_theme_denials
from core.scorer import _score_grounding_failure, _score_theme_is_source_grounded


@pytest.mark.parametrize("direction", ["⚖️", "中性"])
@pytest.mark.parametrize(
    "source,score,tag,logic,remaining_reason",
    [
        (
            "加拿大总理卡尼提议让加拿大成为欧盟‘联系成员国’。"
            "欧盟对吸纳加拿大成为‘联系成员国’持开放态度。",
            0,
            "海外无关消息",
            "加拿大提议成为欧盟联系成员国，属海外地缘政治消息，与A股宏观流动性无关。",
            "",
        ),
        (
            "市场消息：也门武装部队通报，也门塔伊兹境内胡塞武装据点遭到三次空袭。",
            1,
            "海外地缘冲突",
            "也门胡塞武装据点遭三次空袭，属海外地缘事件，与A股宏观流动性无关。",
            "ungrounded_score_facts",
        ),
    ],
)
def test_recorded_low_neutral_postfix_denial_is_not_positive_theme(
    source, score, tag, logic, remaining_reason, direction
):
    parsed = (score, tag, logic, direction)
    assert _score_theme_is_source_grounded(parsed, source, source)
    # This fix does not exempt the remainder from independent fact validation.
    assert _score_grounding_failure(parsed, source, source) == remaining_reason


@pytest.mark.parametrize(
    "logic",
    [
        "与A股宏观流动性无关。",
        "与流动性无关",
        "例行通知，与AI或算力无关。",
    ],
)
def test_complete_standalone_denial_clause_can_be_low_neutral(logic):
    assert not _score_grounding_failure((2, "其他", logic, "⚖️"), "海外例行会议", "")


@pytest.mark.parametrize(
    "logic",
    [
        "与A股宏观流动性无关，但流动性改善。",
        "与A股宏观流动性无关。央行降准释放资金。",
        "与流动性无关；宏观政策改善。",
        "与流动性无关但流动性改善。",
        "流动性改善，与A股宏观流动性无关。",
        "与AI无关，但算力需求增长。",
        "与A股宏观流动性。无关。",
        "与A股宏观流动性\n无关。",
        "与流动性改善有关，与其他事情无关。",
        "并非与A股宏观流动性无关。",
        "不是与流动性无关。",
        "与流动性无关紧要的变化有关。",
    ],
)
def test_denial_never_hides_positive_or_ambiguous_theme_claim(logic):
    assert (
        _score_grounding_failure((2, "其他", logic, "⚖️"), "海外例行会议", "")
        == "ungrounded_score_theme"
    )


@pytest.mark.parametrize(
    "score,tag,direction",
    [(5, "其他", "⚖️"), (2, "其他", "📈"), (2, "其他", "📉"), (2, "宏观放水", "⚖️")],
)
def test_postfix_denial_does_not_relax_score_direction_or_positive_tag(
    score, tag, direction
):
    assert (
        _score_grounding_failure(
            (score, tag, "与A股宏观流动性无关。", direction), "海外例行会议", ""
        )
        == "ungrounded_score_theme"
    )


def test_postfix_denied_source_cannot_ground_positive_theme():
    assert (
        _score_grounding_failure(
            (8, "宏观放水", "流动性改善", "📈"),
            "海外例行会议，与A股宏观流动性无关。",
            "",
        )
        == "ungrounded_score_theme"
    )


def test_denial_strip_preserves_following_clause_and_delimiters():
    assert (
        strip_explicit_theme_denials(
            "海外消息，与A股宏观流动性无关；但流动性改善。", ("流动性",)
        )
        == "海外消息，；但流动性改善。"
    )


def test_denial_does_not_remove_separate_positive_source_evidence():
    assert not _score_grounding_failure(
        (8, "宏观放水", "央行降准", "📈"), "海外例行会议，与AI无关；央行降准。", ""
    )
