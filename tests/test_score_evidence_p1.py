"""Source-grounding regressions, including the production 6000 -> 6001 error."""

import json

import pytest

from core.scorer import (
    LLMBackend,
    Scorer,
    _score_grounding_failure,
    _score_grounding_verdict,
)
from scoring_provenance import SAFE_NEUTRAL_CONTRACT_VALUES


@pytest.mark.parametrize(
    "source,logic,valid",
    [
        (
            "今年以来，中长期资金净买入A股超过6000亿元",
            "今年以来，中长期资金净买入A股超过6001亿元",
            False,
        ),
        (
            "今年以来，中长期资金净买入A股超过6000亿元",
            "今年以来，中长期资金净买入A股超过6000亿元",
            True,
        ),
        (
            "证监会李超：今年以来中长期资金净买入A股超过6000亿元",
            "今年以来中长期资金净买入A股超过6000亿元",
            True,
        ),
        (
            "证监会李超：今年以来中长期资金净买入A股超过6000亿元",
            "今年以来中长期资金净买入A股超过6001亿元",
            False,
        ),
        (
            "证监会李超否认：中长期资金净买入A股超过6000亿元",
            "中长期资金净买入A股超过6000亿元",
            False,
        ),
        ("甲公司：订单3亿元；乙公司：订单5亿元", "甲公司订单5亿元", False),
        ("甲公司：订单3亿元；乙公司：订单5亿元", "甲公司订单3亿元", True),
        (
            "证监会李超：资金净买入A股超过6000亿元",
            "资金净买入A股超过6000亿元；证监会李超：资金净买入A股超过6000亿元",
            False,
        ),
        ("甲公司订单3亿元；乙公司订单5亿元", "甲公司订单5亿元", False),
        ("甲公司订单3亿元；乙公司订单5亿元", "甲公司订单3亿元", True),
        ("甲公司订单3亿元", "甲公司订单30000万元", True),
        ("甲公司订单3亿元", "甲公司订单3万元", False),
        (
            "甲公司订单100000000000000000000000000001元",
            "甲公司订单100000000000000000000000000002元",
            False,
        ),
        (
            "甲公司订单3.00000000000000000000000000001亿元",
            "甲公司订单3.00000000000000000000000000002亿元",
            False,
        ),
        ("甲公司利润同比增长20%", "甲公司利润环比增长20%", False),
        ("甲公司利润同比下降20%", "甲公司利润同比增长20%", False),
        ("甲公司拟投资3亿元", "甲公司已投资3亿元", False),
        ("甲公司订单不超过3亿元", "甲公司订单超过3亿元", False),
        ("甲公司未获得3亿元订单", "甲公司获得3亿元订单", False),
        ("甲公司订单3亿元", "甲公司订单３亿元", True),
    ],
)
def test_numeric_claim_keeps_entity_unit_qualifier_and_polarity(source, logic, valid):
    reason = _score_grounding_failure((8, "公司订单", logic, "📈"), source, "")
    assert (not reason) is valid
    if not valid:
        assert reason == "ungrounded_score_facts"


@pytest.mark.parametrize(
    "source,logic,valid",
    [
        ("甲公司未签订采购合同", "甲公司已签订采购合同", False),
        ("甲公司否认：甲公司签订采购合同", "甲公司签订采购合同", False),
        ("甲公司：已签订采购合同", "乙公司签订采购合同", False),
        ("甲公司：已签订采购合同", "甲公司签订采购合同", True),
        ("甲公司否认签订采购合同的传闻", "甲公司签订采购合同", False),
        ("甲公司拟收购乙公司", "甲公司已收购乙公司", False),
        ("甲公司获得项目订单", "乙公司获得项目订单", False),
        ("甲公司未签订采购合同；乙公司已签订采购合同", "甲公司已签订采购合同", False),
        ("甲公司未签订采购合同", "甲公司未签订采购合同", True),
        ("甲公司拟收购乙公司", "甲公司拟收购乙公司", True),
        ("甲公司签订采购合同", "公司签订采购合同", True),
    ],
)
def test_literal_qualitative_events_keep_subject_and_denial(source, logic, valid):
    assert (
        not _score_grounding_failure((8, "公司公告", logic, "📈"), source, "")
    ) is valid


FROZEN_MEETING_TITLE = "海天味业发布年度股东大会通知"
FROZEN_MEETING_SUMMARY = "公司将于近期召开年度股东大会，审议董事会工作报告和利润分配预案。"
FROZEN_ORDER_LOGIC = "海天味业获得大额订单，业绩预期改善"


def test_elided_second_verb_and_synonym_rewrite_still_ground():
    """A later verb in one clause, and 签署/签订, must not look absent."""
    cases = [
        ("甲公司建设新工厂并获得项目订单", "", "甲公司获得项目订单"),
        ("甲公司签署采购合同", "", "甲公司签订采购合同"),
        ("甲公司取得项目订单", "", "甲公司获得项目订单"),
        ("甲公司并购乙公司", "", "甲公司收购乙公司"),
    ]
    for title, summary, logic in cases:
        reason, kind = _score_grounding_verdict(
            (8, "公司公告", logic, "看多"), title, summary
        )
        assert (reason, kind) == ("", ""), (title, logic, reason, kind)


def test_synonym_negation_or_plan_cannot_ground_an_assertion():
    """Synonym recognition must keep the existing denial and plan checks."""
    rejected = [
        ("甲公司未签署采购合同", "", "甲公司签订采购合同"),
        ("甲公司计划签署采购合同", "", "甲公司签订采购合同"),
        ("甲公司未取得项目订单", "", "甲公司获得项目订单"),
    ]
    for title, summary, logic in rejected:
        reason, kind = _score_grounding_verdict(
            (8, "公司公告", logic, "看多"), title, summary
        )
        assert (reason, kind) == ("ungrounded_score_facts", "event_fact_failure"), (
            title,
            logic,
            reason,
            kind,
        )


def test_conjoined_second_verb_is_seen_in_both_directions():
    """One clause, two event verbs: repeat a stated verb, never add an unstated one."""
    # The source states the second verb too; the reason repeats only that verb.
    grounded = (
        "甲公司签订合同并获得项目订单",
        "",
        "甲公司获得项目订单",
    )
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", grounded[2], "看多"), grounded[0], grounded[1]
    )
    assert (reason, kind) == ("", ""), (reason, kind)

    # The source never states the added second verb; it must not slip through
    # just because the first verb and its object do overlap.
    bypass = (
        "海天味业签订采购合同",
        "",
        "海天味业签订采购合同并获得大额订单",
    )
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", bypass[2], "看多"), bypass[0], bypass[1]
    )
    assert (reason, kind) == ("ungrounded_score_facts", "event_fact_failure"), (
        reason,
        kind,
    )

    # A modal directly in front of the second verb still governs that verb.
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", "甲公司签订合同并获得订单", "看多"),
        "甲公司签订合同但未获得订单",
        "",
    )
    assert (reason, kind) == ("ungrounded_score_facts", "event_fact_failure"), (
        reason,
        kind,
    )


@pytest.mark.parametrize(
    "title,summary,logic,expected",
    [
        # R1: a subject-less denial in the summary is not affirmative evidence.
        ("甲公司公告", "否认获得项目订单", "甲公司获得项目订单", False),
        ("甲公司公告", "获得项目订单的传闻已被否认", "甲公司获得项目订单", False),
        ("甲公司获得订单的消息被否认", "", "甲公司获得订单", False),
        # R2: approval to acquire is not a completed acquisition.
        ("甲公司获得收购乙公司的批准", "", "甲公司收购乙公司", False),
        ("甲公司获得收购乙公司的批准", "", "甲公司获得项目订单", False),
        ("甲公司获批收购乙公司", "", "甲公司收购乙公司", False),
        ("甲公司获得签订合同的授权", "", "甲公司签订合同", False),
        ("甲公司获得签订合同的授权", "", "甲公司获得项目订单", False),
        ("甲公司获得中标项目资格", "", "甲公司中标项目", False),
        # SIB1: a noun phrase announcing a plan is not an executed acquisition.
        ("甲公司并购计划获得批准", "", "甲公司收购乙公司", False),
        ("甲公司并购计划未获得监管批复", "", "甲公司收购乙公司", False),
        ("甲公司收购乙公司的计划获批", "", "甲公司收购乙公司", False),
        # H1: the second predicate's denial must remain visible.
        ("甲公司并购未获批", "", "甲公司并购获批", False),
        ("甲公司并购计划未获得监管批复", "", "甲公司并购获批", False),
        ("甲公司并购未获批", "", "甲公司并购未获批", True),
        # Explicit completed events and established aliases remain accepted.
        ("甲公司获得收购乙公司的批准", "", "甲公司获得收购乙公司的批准", True),
        ("甲公司获得签订合同的授权", "", "甲公司获得签订合同的授权", True),
        ("甲公司收购乙公司", "", "甲公司并购乙公司", True),
        ("甲公司获批并收购乙公司", "", "甲公司收购乙公司", True),
        ("甲公司获得授权并签订合同", "", "甲公司签订合同", True),
        ("甲公司中标项目", "", "甲公司中标项目", True),
        ("甲公司签订合同并获得项目订单", "", "甲公司获得项目订单", True),
        ("甲公司公告", "获得项目订单", "甲公司获得项目订单", True),
        ("甲公司签署采购合同", "", "甲公司签订合同", True),
        ("甲公司获得大额订单", "", "甲公司取得重大订单", True),
        ("乙公司获得补助", "", "甲公司获得订单", False),
    ],
)
def test_approval_plan_and_subjectless_denial_are_not_completed_events(
    title, summary, logic, expected
):
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", logic, "看多"), title, summary
    )
    assert (reason, kind) == (
        ("", "") if expected else ("ungrounded_score_facts", "event_fact_failure")
    ), (title, summary, logic, reason, kind)


@pytest.mark.parametrize(
    "source,logic,expected",
    [
        ("甲公司签订合同、乙公司获得订单", "乙公司获得订单", True),
        ("甲公司签订合同、乙公司获得订单", "甲公司获得订单", False),
        ("甲公司签订合同并乙公司获得订单", "乙公司获得订单", True),
        ("甲公司签订合同并乙公司获得订单", "甲公司获得订单", False),
    ],
)
def test_conjoined_clause_with_a_new_subject_keeps_event_owner(source, logic, expected):
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", logic, "看多"),
        source,
        "",
    )
    assert (reason, kind) == (
        ("", "") if expected else ("ungrounded_score_facts", "event_fact_failure")
    ), (logic, reason, kind)


@pytest.mark.parametrize(
    "source,logic,expected",
    [
        # 合并/兼并/吞并/归并 carry 并 inside one word. Splitting there invents
        # an event owner the source never names.
        ("甲公司合并乙公司获得订单", "乙公司获得订单", False),
        ("甲公司兼并乙公司获得订单", "乙公司获得订单", False),
        ("甲公司吞并乙公司获得订单", "乙公司获得订单", False),
        ("甲公司归并乙公司获得订单", "乙公司获得订单", False),
        ("甲公司合并乙公司获批", "乙公司获批", False),
        ("甲公司合并乙公司中标项目", "乙公司中标项目", False),
        ("甲公司合并乙公司获得大额订单", "乙公司获得订单", False),
        # 并购 is one verb; splitting it drops the acquisition the source states.
        ("甲公司并购乙公司获得订单", "甲公司并购乙公司", True),
        ("甲公司并购乙公司获得订单", "甲公司收购乙公司", True),
        ("甲公司并购乙公司获得订单", "甲公司获得订单", True),
        ("甲公司并购乙公司", "甲公司并购乙公司", True),
        # 并且 conjoins clauses but must leave the following subject intact.
        ("甲公司签订合同并且乙公司获得订单", "乙公司获得订单", True),
        ("甲公司签订合同并且乙公司获得订单", "甲公司获得订单", False),
        # A real conjunction with its own predicates still splits, both ways.
        ("甲公司签订合同并乙公司获得订单", "乙公司获得订单", True),
        ("甲公司签订合同并乙公司获得订单", "甲公司获得订单", False),
        ("甲公司签订合同、乙公司获得订单", "乙公司获得订单", True),
        ("甲公司签订合同、乙公司获得订单", "甲公司获得订单", False),
        # A 并 after a comma still conjoins within the same sentence...
        ("甲公司获得订单，并乙公司获得合同", "乙公司获得合同", True),
        ("甲公司获得订单，并乙公司获得合同", "甲公司获得合同", False),
        # ...but a predicate in the previous sentence must not justify a split.
        ("甲公司获得订单。合并乙公司获得合同", "乙公司获得合同", False),
    ],
)
def test_a_compound_word_with_并_never_ends_a_clause(source, logic, expected):
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", logic, "看多"),
        source,
        "",
    )
    assert (reason, kind) == (
        ("", "") if expected else ("ungrounded_score_facts", "event_fact_failure")
    ), (source, logic, reason, kind)


@pytest.mark.parametrize("compound", ["合并", "兼并", "吞并", "归并"])
def test_an_earlier_predicate_does_not_turn_a_compound_word_into_a_conjunction(compound):
    source = f"甲公司获得订单后{compound}乙公司获得合同"
    assert _score_grounding_verdict(
        (8, "公司公告", "乙公司获得合同", "看多"), source, ""
    ) == ("ungrounded_score_facts", "event_fact_failure")


@pytest.mark.parametrize(
    "logic,expected",
    [
        ("甲公司收购乙公司", True),
        ("甲公司并购乙公司", True),
        ("甲公司获得订单", True),
        ("乙公司获得订单", False),
    ],
)
def test_an_earlier_predicate_does_not_split_the_later_acquisition(logic, expected):
    source = "甲公司签订合同并购乙公司获得订单"
    assert _score_grounding_verdict(
        (8, "公司公告", logic, "看多"), source, ""
    ) == (("", "") if expected else ("ungrounded_score_facts", "event_fact_failure"))


@pytest.mark.parametrize("connector", ["并", "并且", "并同时"])
@pytest.mark.parametrize(
    "logic,expected",
    [("乙公司未获得订单", True), ("乙公司获得订单", False), ("甲公司未获得订单", False)],
)
def test_sentence_initial_connector_preserves_the_explicit_owner_and_denial(connector, logic, expected):
    source = f"甲公司签订合同。{connector}乙公司未获得订单"
    assert _score_grounding_verdict(
        (8, "公司公告", logic, "看多"), source, ""
    ) == (("", "") if expected else ("ungrounded_score_facts", "event_fact_failure"))


def test_denial_after_a_noun_phrase_plan_still_governs_the_later_verb():
    """"并购计划未获得" is a denial of 获得; the noun 计划 must not leak forward."""
    source = "甲公司并购计划未获得监管批复"
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", "甲公司获得监管批复", "看多"), source, ""
    )
    assert (reason, kind) == ("ungrounded_score_facts", "event_fact_failure")

    # The same source must still ground a denial or an asserted outcome.
    for logic in ("甲公司未获得监管批复",):
        assert _score_grounding_verdict(
            (8, "公司公告", logic, "看多"), source, ""
        ) == ("", "")
    assert _score_grounding_verdict(
        (8, "公司公告", "甲公司获得监管批复", "看多"),
        "甲公司并购计划已获得监管批复",
        "",
    ) == ("", "")


def test_meeting_source_does_not_ground_an_order_claim():
    """Frozen title, summary, and model logic: a meeting is not an order."""
    reason, kind = _score_grounding_verdict(
        (8, "公司订单", FROZEN_ORDER_LOGIC, "看多"),
        FROZEN_MEETING_TITLE,
        FROZEN_MEETING_SUMMARY,
    )
    assert (reason, kind) == ("ungrounded_score_facts", "event_fact_failure")


@pytest.mark.parametrize(
    "title,summary,logic,valid",
    [
        (
            "甲公司获得项目订单",
            "",
            "甲公司已获得项目订单",
            True,
        ),
        (
            FROZEN_MEETING_TITLE,
            "公司获得项目订单",
            "公司获得项目订单，利好业绩",
            True,
        ),
        (
            "甲公司计划获得项目订单",
            "",
            "甲公司获得项目订单",
            False,
        ),
        (
            "甲公司计划获得项目订单",
            "",
            "甲公司计划获得项目订单",
            True,
        ),
        (
            "甲公司未获得项目订单",
            "",
            "甲公司获得项目订单",
            False,
        ),
        (
            "甲公司未获得项目订单",
            "",
            "甲公司未获得项目订单",
            True,
        ),
    ],
)
def test_order_claim_controls_keep_rewrite_plan_and_negation(
    title, summary, logic, valid
):
    reason, kind = _score_grounding_verdict(
        (8, "公司公告", logic, "看多"), title, summary
    )
    if valid:
        assert (reason, kind) == ("", "")
    else:
        assert (reason, kind) == ("ungrounded_score_facts", "event_fact_failure")


def test_low_neutral_explicit_theme_denial_is_not_an_affirmative_tag():
    source = "海天味业发布年度股东大会通知"
    logic = "常规股东大会通知，不涉及AI或算力产业催化"
    assert not _score_grounding_failure((2, "公司公告", logic, "⚖️"), source, "")
    assert _score_grounding_failure((8, "AI算力", logic, "📈"), source, "")
    assert _score_grounding_failure((2, "AI算力", logic, "⚖️"), source, "")


def test_denial_does_not_hide_following_positive_claim_or_ground_a_source_claim():
    source = "海天味业发布年度股东大会通知"
    assert _score_grounding_failure(
        (2, "公司公告", "不涉及AI，但算力需求大增", "⚖️"), source, ""
    )
    assert _score_grounding_failure(
        (8, "AI算力", "算力业务发展", "📈"), "公司不涉及AI或算力", ""
    )


def test_wrong_numeric_score_resolves_locally_and_records_only_model_rejection(monkeypatch):
    from types import SimpleNamespace

    from core.scorer import capture_llm_calls
    from db import database
    from db.score_quality import read_score_quality

    database.init_db()
    scorer = Scorer()
    backend = LLMBackend(
        name="fixture",
        model="qwen-fixture",
        api_key="",
        base_url="https://invalid.test",
        rate_limit=0,
    )
    monkeypatch.setattr(scorer, "backends", [backend])
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "score": 8,
                "tag": "资金入市",
                "logic": "中长期资金净买入A股超过6001亿元",
                "sentiment": "看多",
            })))],
            usage=None,
            model="qwen-fixture",
        )

    backend.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=respond)
    ))
    with capture_llm_calls() as captured:
        result = scorer.score_stage1_with_identity(
            "中长期资金净买入A股超过6000亿元",
            "",
            "",
            {
                "scoring_strategy_version": "p1-test",
                "llm_semantic_repair": {"enabled": True},
            },
        )
    assert result.values == SAFE_NEUTRAL_CONTRACT_VALUES
    assert result.model_name == "local_safe_neutral"
    assert result.semantic_resolution == "safe_neutral_grounding"
    assert result.neutral_reason == "facts"
    assert len(requests) == captured.total == 1
    assert captured.by_purpose == {"stage1": 1}
    quality = read_score_quality(24, "p1-test")
    rows = quality["validation"]
    assert {r["reason"] for r in rows} == {"ungrounded_score_facts"}
    assert sum(r["observed"] for r in rows) == 1
    assert all(r["attempt"] == "initial" and r["accepted"] == 0 for r in rows)
    assert quality["transport"][0]["attempts"] == 1
    assert quality["transport"][0]["succeeded"] == 1


def test_fixed_safe_neutral_is_not_fact_checked_against_source_investment_words():
    source = "今年投资催化明显，机构继续加仓"
    assert not _score_grounding_failure(SAFE_NEUTRAL_CONTRACT_VALUES, source, "")
    invented = (4.0, "其他", "甲公司已投资3亿元", "⚖️")
    assert _score_grounding_failure(invented, source, "") == "ungrounded_score_facts"


def test_display_title_cannot_self_ground_wrong_numeric_logic():
    decorated = (
        "[⭐8.0 📈 行业景气] 证监会李超：今年以来中长期资金净买入A股超过6000亿元 "
        "💡今年以来中长期资金净买入A股超过6001亿元"
    )
    parsed = (8.0, "行业景气", "今年以来中长期资金净买入A股超过6001亿元", "📈")
    assert _score_grounding_failure(parsed, decorated, "") == "ungrounded_score_facts"
    assert not _score_grounding_failure(
        (8.0, "行业景气", "今年以来中长期资金净买入A股超过6000亿元", "📈"),
        decorated,
        "",
    )


@pytest.mark.parametrize(
    "source,logic",
    [
        ("甲公司拟投资3亿元", "甲公司已投资3亿元"),
        ("甲公司预计投资3亿元", "甲公司投资3亿元"),
        ("甲公司计划投资建设工厂", "甲公司投资建设工厂"),
    ],
)
def test_plan_or_forecast_cannot_ground_an_asserted_investment(source, logic):
    assert _score_grounding_failure((8.0, "公司公告", logic, "📈"), source, "") == (
        "ungrounded_score_facts"
    )


@pytest.mark.parametrize(
    "source,logic,valid",
    [
        ("甲公司预计将收购乙公司", "甲公司已收购乙公司", False),
        ("甲公司可能已经获得项目订单", "甲公司已经获得项目订单", False),
        ("甲公司预计将投产", "甲公司已投产", False),
        ("甲公司预计将收购乙公司", "甲公司预计将收购乙公司", True),
        ("甲公司可能已经获得项目订单", "甲公司可能已获得项目订单", True),
        ("甲公司预计将投产", "甲公司预计将投产", True),
        ("甲公司预计将收购乙公司", "甲公司预计已收购乙公司", False),
        ("甲公司可能尚未签订采购合同", "甲公司可能已签订采购合同", False),
        ("甲公司可能尚未签订采购合同", "甲公司尚未签订采购合同", False),
        ("甲公司可能尚未签订采购合同", "甲公司可能尚未签订采购合同", True),
        ("甲公司预计不会收购乙公司", "甲公司预计将收购乙公司", False),
        ("甲公司预计不会收购乙公司", "甲公司预计不会收购乙公司", True),
        ("甲公司否认已经签订采购合同", "甲公司已经签订采购合同", False),
        ("甲公司否认已经签订采购合同", "甲公司否认已签订采购合同", True),
        ("预计甲公司将投产", "甲公司将投产", False),
        ("预计甲公司将投产", "甲公司预计将投产", True),
        ("甲公司预计将收购乙公司", "丙公司预计将收购乙公司", False),
        ("甲公司预计将收购乙公司", "甲公司已收购乙公司并有望改善业绩", False),
        ("甲公司预计将投产", "甲公司已投产，利好业绩", False),
    ],
)
def test_compound_modals_preserve_uncertainty_denial_and_event_state(
    source, logic, valid
):
    reason = _score_grounding_failure((8.0, "公司公告", logic, "📈"), source, "")
    assert reason == ("" if valid else "ungrounded_score_facts")


def test_evidence_helper_is_in_strategy_fingerprint_but_monitoring_is_not(
    tmp_path, monkeypatch
):
    import importlib

    config = importlib.import_module("core.config")
    core = tmp_path / "core"
    core.mkdir()
    monkeypatch.setattr(config, "__file__", str(core / "config.py"))
    evidence = core / "score_evidence.py"
    evidence.write_text("policy = 1\n")
    original = config._strategy_code_fingerprint()
    evidence.write_text("policy = 2\n")
    changed = config._strategy_code_fingerprint()
    assert changed != original
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "score_quality.py").write_text("monitor = 1\n")
    assert config._strategy_code_fingerprint() == changed
