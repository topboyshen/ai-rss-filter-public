import copy

import pytest

from core.briefing_grounding import (
    evaluate_commentary_grounding,
    extract_strong_claim_tokens,
    ground_theme_commentary,
)


def test_extracts_and_normalizes_each_strong_anchor_type():
    tokens = extract_strong_claim_tokens(
        "2026年7月28日，NVIDIA称600519上涨3.2%，涉及500亿美元《算力行动方案》"
    )

    assert "date:2026-07-28" in tokens
    assert "entity:nvidia" in tokens
    assert "code:600519" in tokens
    assert "percent:3.2%" in tokens
    assert "money:500亿美元" in tokens
    assert "policy:算力行动方案" in tokens


def test_one_evidence_can_ground_strong_claim_and_meaningful_text():
    result = evaluate_commentary_grounding(
        "NVIDIA签署500亿美元数据中心租赁协议",
        ["NVIDIA签署500亿美元的数据中心租赁协议"],
    )

    assert result["grounded"] is True
    assert result["evidence_index"] == 0
    assert "entity:nvidia" in result["distinct_hits"]
    assert "money:500亿美元" in result["distinct_hits"]


def test_hits_from_different_evidence_items_are_never_combined():
    result = evaluate_commentary_grounding(
        "NVIDIA投入500亿美元执行《算力基础设施行动计划》",
        [
            "NVIDIA将投入500亿美元建设数据中心",
            "有关部门发布《算力基础设施行动计划》",
        ],
    )

    assert result["grounded"] is False
    assert result["reason"] == "strong_anchor_mismatch"


def test_any_unmatched_strong_claim_token_fails_despite_other_hits():
    result = evaluate_commentary_grounding(
        "NVIDIA签署500亿美元数据中心租赁协议",
        ["NVIDIA签署400亿美元数据中心租赁协议"],
    )

    assert result["grounded"] is False
    assert result["reason"] == "strong_anchor_mismatch"
    assert "entity:nvidia" in result["matched_strong_tokens"]
    assert "money:500亿美元" not in result["matched_strong_tokens"]


def test_negated_evidence_cannot_ground_positive_claim():
    result = evaluate_commentary_grounding(
        "NVIDIA签署500亿美元数据中心租赁协议",
        ["NVIDIA否认签署500亿美元数据中心租赁协议"],
    )

    assert result["grounded"] is False
    assert result["reason"] == "source_qualification_conflict"
    assert result["source_qualification"] == "debunk"


@pytest.mark.parametrize(
    "evidence",
    [
        "据悉NVIDIA考虑签署500亿美元数据中心租赁协议",
        "NVIDIA计划签署500亿美元数据中心租赁协议",
        "NVIDIA或将签署500亿美元数据中心租赁协议，尚待监管批准",
    ],
)
def test_hedged_source_cannot_be_upgraded_to_completed_fact(evidence):
    result = evaluate_commentary_grounding(
        "NVIDIA签署500亿美元数据中心租赁协议",
        [evidence],
    )

    assert result["grounded"] is False
    assert result["reason"] == "modality_conflict"


def test_leading_alternative_marker_cannot_be_dropped():
    result = evaluate_commentary_grounding(
        "NVIDIA签署500亿美元数据中心租赁协议",
        ["或NVIDIA签署500亿美元数据中心租赁协议"],
    )

    assert result["grounded"] is False
    assert result["reason"] == "modality_conflict"


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("甲公司已增持乙公司5%股份", "甲公司将增持乙公司5%股份"),
        ("项目已投产，产能增加20%", "项目将投产，产能增加20%"),
        ("项目建成，投资100亿元", "项目将建成，投资100亿元"),
    ],
)
def test_future_tense_cannot_prove_completed_fact(claim, evidence):
    result = evaluate_commentary_grounding(claim, [evidence])

    assert result["grounded"] is False
    assert result["reason"] == "modality_conflict"


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("甲公司将增持乙公司5%股份", "甲公司或将增持乙公司5%股份"),
        ("项目已投产，产能增加20%", "项目正投产，产能增加20%"),
        ("项目正投产，产能增加20%", "项目仍在投产，产能增加20%"),
    ],
)
def test_fact_canonicalization_preserves_modality_and_tense(claim, evidence):
    result = evaluate_commentary_grounding(claim, [evidence])

    assert result["grounded"] is False
    assert result["reason"] == "source_quote_required"


@pytest.mark.parametrize(
    "evidence",
    [
        "如果监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "若监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "假如监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "假设监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "一旦监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "除非监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "前提是监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
    ],
)
def test_conditional_source_cannot_ground_unqualified_claim(evidence):
    result = evaluate_commentary_grounding(
        "NVIDIA将签署500亿美元数据中心租赁协议",
        [evidence],
    )

    assert result["grounded"] is False
    assert result["reason"] == "source_qualification_conflict"
    assert result["source_qualification"] == "conditional"


@pytest.mark.parametrize(
    "evidence",
    [
        "If approved, NVIDIA will sign a $50 billion data center lease",
        "Unless approved, NVIDIA will sign a $50 billion data center lease",
    ],
)
def test_english_conditional_source_cannot_ground_unqualified_claim(evidence):
    result = evaluate_commentary_grounding(
        "NVIDIA will sign a $50 billion data center lease",
        [evidence],
    )

    assert result["grounded"] is False
    assert result["reason"] == "source_qualification_conflict"
    assert result["source_qualification"] == "conditional"


@pytest.mark.parametrize(
    "evidence",
    [
        "NVIDIA将签署500亿美元数据中心租赁协议?",
        "NVIDIA将签署500亿美元数据中心租赁协议？",
        "NVIDIA是否将签署500亿美元数据中心租赁协议",
        "NVIDIA将签署500亿美元数据中心租赁协议，真相如何",
    ],
)
def test_question_source_cannot_ground_affirmative_claim(evidence):
    result = evaluate_commentary_grounding(
        "NVIDIA将签署500亿美元数据中心租赁协议",
        [evidence],
    )

    assert result["grounded"] is False
    assert result["reason"] == "source_qualification_conflict"
    assert result["source_qualification"] == "question"


@pytest.mark.parametrize(
    ("evidence", "qualification"),
    [
        ("网传NVIDIA签署500亿美元数据中心租赁协议", "rumor"),
        ("据知情人士透露，NVIDIA签署500亿美元数据中心租赁协议", "rumor"),
        ("消息人士称：NVIDIA签署500亿美元数据中心租赁协议", "rumor"),
        ("未经官方确认，NVIDIA签署500亿美元数据中心租赁协议", "rumor"),
        ("传出消息：NVIDIA签署500亿美元数据中心租赁协议", "rumor"),
        ("据报道，NVIDIA签署500亿美元数据中心租赁协议", "rumor"),
        ("官方辟谣：NVIDIA签署500亿美元数据中心租赁协议", "debunk"),
        ("NVIDIA签署500亿美元数据中心租赁协议的消息不实", "debunk"),
        ("NVIDIA签署500亿美元数据中心租赁协议不属实", "debunk"),
        ("NVIDIA签署500亿美元数据中心租赁协议系误读", "debunk"),
        ("NVIDIA签署500亿美元数据中心租赁协议是假消息", "debunk"),
        ("尚无证据表明NVIDIA签署500亿美元数据中心租赁协议", "debunk"),
        ("无法证实NVIDIA签署500亿美元数据中心租赁协议", "debunk"),
        ("NVIDIA签署500亿美元数据中心租赁协议的说法不准确", "debunk"),
        ("NVIDIA签署500亿美元数据中心租赁协议的消息存疑", "debunk"),
        ("公司否认：NVIDIA未签署500亿美元数据中心租赁协议", "debunk"),
        ("公司驳斥：NVIDIA取消500亿美元数据中心租赁协议", "debunk"),
    ],
)
def test_rumor_or_debunk_source_cannot_ground_affirmative_claim(
    evidence,
    qualification,
):
    result = evaluate_commentary_grounding(
        "NVIDIA签署500亿美元数据中心租赁协议",
        [evidence],
    )

    assert result["grounded"] is False
    assert result["reason"] == "source_qualification_conflict"
    assert result["source_qualification"] == qualification


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        (
            "NVIDIA未签署500亿美元数据中心租赁协议",
            "公司否认：NVIDIA未签署500亿美元数据中心租赁协议",
        ),
        (
            "NVIDIA取消500亿美元数据中心租赁协议",
            "公司驳斥：NVIDIA取消500亿美元数据中心租赁协议",
        ),
    ],
)
def test_debunk_prefix_cannot_be_dropped_when_claim_is_also_negative(claim, evidence):
    result = evaluate_commentary_grounding(claim, [evidence])

    assert result["grounded"] is False
    assert result["reason"] == "source_qualification_conflict"
    assert result["source_qualification"] == "debunk"


@pytest.mark.parametrize(
    "qualified_fact",
    [
        "如果监管批准，NVIDIA将签署500亿美元数据中心租赁协议",
        "网传NVIDIA签署500亿美元数据中心租赁协议",
        "NVIDIA是否将签署500亿美元数据中心租赁协议？",
        "NVIDIA签署500亿美元数据中心租赁协议的消息不实",
    ],
)
def test_matching_source_qualification_is_not_erased(qualified_fact):
    result = evaluate_commentary_grounding(qualified_fact, [qualified_fact])

    assert result["grounded"] is True
    assert result["source_qualification"] == ""


def test_shared_entities_and_amount_do_not_ground_new_trade_advice():
    result = evaluate_commentary_grounding(
        "立即买入NVIDIA等待500亿美元租赁履约",
        ["NVIDIA签署500亿美元数据中心租赁协议"],
    )

    assert result["grounded"] is False
    assert result["reason"] == "source_quote_required"


@pytest.mark.parametrize(
    ("claim", "evidence"),
    [
        ("利润增长5%", "营收增长5%，利润下降5%"),
        ("甲公司增持乙公司5%股份", "甲公司减持乙公司5%股份，丙公司增持股份"),
    ],
)
def test_facts_cannot_be_reassembled_within_one_evidence(claim, evidence):
    result = evaluate_commentary_grounding(claim, [evidence])

    assert result["grounded"] is False
    assert result["reason"] in {"source_quote_required", "strong_anchor_mismatch"}


def test_exact_source_owned_chinese_fact_can_ground_without_latin_anchor():
    result = evaluate_commentary_grounding(
        "贵州茅台业绩增长",
        ["财联社消息：贵州茅台业绩增长"],
    )

    assert result["grounded"] is True
    assert "source_quote" in result["distinct_hits"]


def test_generic_financial_words_do_not_count_as_distinct_hits():
    result = evaluate_commentary_grounding(
        "市场政策资金影响值得关注",
        ["市场政策及资金可能产生影响，值得关注"],
        min_distinct_hits=2,
        require_strong_hit=False,
    )

    assert result["grounded"] is False
    assert result["distinct_hits"] == ()
    assert result["reason"] == "modality_conflict"


def test_generic_english_terms_are_not_treated_as_named_entities():
    result = evaluate_commentary_grounding(
        "AI market policy risk",
        ["AI market policy risk"],
        min_distinct_hits=1,
        require_strong_hit=True,
    )

    assert result["grounded"] is False
    assert result["strong_claim_tokens"] == ()
    assert result["reason"] == "fact_relation_required"


def test_repeating_one_lexical_span_does_not_inflate_distinct_hits():
    result = evaluate_commentary_grounding(
        "半导体订单增加",
        ["半导体订单增加；半导体订单增加"],
        min_distinct_hits=2,
        require_strong_hit=False,
    )

    assert result["grounded"] is False
    assert result["distinct_hits"] == ("text:半导体订单增加",)


def test_exact_source_quote_is_a_strong_anchor_for_chinese_facts():
    commentary = "半导体订单增加，算力中心扩建"
    evidence = ["半导体订单增加；算力中心扩建"]

    strict = evaluate_commentary_grounding(
        commentary,
        evidence,
        min_distinct_hits=2,
        require_strong_hit=True,
    )
    relaxed = evaluate_commentary_grounding(
        commentary,
        evidence,
        min_distinct_hits=2,
        require_strong_hit=False,
    )

    assert strict["grounded"] is True
    assert "source_quote" in strict["distinct_hits"]
    assert relaxed["grounded"] is True


def test_date_code_and_percentage_must_cooccur_in_one_evidence():
    result = evaluate_commentary_grounding(
        "2026年7月28日，600519上涨3.2%",
        ["2026-07-28，600519上涨3.2%"],
        min_distinct_hits=3,
    )

    assert result["grounded"] is True
    assert set(result["strong_claim_tokens"]) == {
        "date:2026-07-28",
        "code:600519",
        "percent:3.2%",
    }


def test_english_entity_matching_is_case_insensitive():
    result = evaluate_commentary_grounding(
        "NVIDIA投入500亿美元扩建算力中心",
        ["nvidia投入500亿美元扩建算力中心"],
    )

    assert result["grounded"] is True


def test_ground_theme_commentary_applies_fallback_per_field_without_mutation():
    theme = {
        "theme": "AI算力",
        "direction": "看多",
        "insight": "NVIDIA签署500亿美元数据中心租赁协议",
        "why_it_matters": "这将改善市场影响",
        "market_impact": "半导体板块资金受益",
        "action": "关注NVIDIA后续500亿美元租赁履约",
    }
    original = copy.deepcopy(theme)
    fallback = {
        "why_it_matters": "确定性原因说明",
        "market_impact": "确定性市场说明",
    }

    result = ground_theme_commentary(
        theme,
        ["NVIDIA签署500亿美元数据中心租赁协议"],
        {"min_distinct_hits": 2, "require_strong_hit": True},
        fallback,
    )

    assert theme == original
    assert result["theme"] == "AI算力"
    assert result["direction"] == "看多"
    assert result["insight"] == original["insight"]
    assert result["action"] == ""
    assert result["why_it_matters"] == "确定性原因说明"
    assert result["market_impact"] == "确定性市场说明"
    assert result["grounding"]["status"] == "partial"
    assert result["grounding"]["fields"]["insight"]["status"] == "grounded"
    assert result["grounding"]["fields"]["why_it_matters"]["status"] == "fallback"
    assert result["grounding"]["fields"]["action"]["status"] == "hidden"


def test_missing_fallback_value_blanks_only_the_failed_field():
    result = ground_theme_commentary(
        {"insight": "没有强锚点的推断", "action": "未经支撑的操作"},
        ["另一条事实"],
        {"min_distinct_hits": 2, "require_strong_hit": True},
        {},
    )

    assert result["insight"] == ""
    assert result["action"] == ""
    assert result["why_it_matters"] == ""
    assert result["market_impact"] == ""
    assert result["grounding"]["status"] == "fallback"


def test_disabled_policy_returns_an_equal_copy_without_metadata():
    theme = {"theme": "芯片", "insight": "原生产文案", "nested": {"value": 1}}

    result = ground_theme_commentary(
        theme,
        ["无关证据"],
        {"enabled": False},
        {"insight": "不应使用"},
    )

    assert result == theme
    assert result is not theme
    assert result["nested"] is not theme["nested"]
    assert "grounding" not in result


def test_theme_grounding_materializes_generator_evidence_once():
    evidence = (text for text in ["NVIDIA签署500亿美元数据中心租赁协议"])
    claim = "NVIDIA签署500亿美元数据中心租赁协议"

    result = ground_theme_commentary(
        {
            "insight": claim,
            "why_it_matters": claim,
            "market_impact": claim,
            "action": claim,
        },
        evidence,
        {"min_distinct_hits": 2, "require_strong_hit": True},
        {},
    )

    assert result["grounding"]["status"] == "grounded_with_local_action"
    assert result["action"] == ""


@pytest.mark.parametrize(
    "policy",
    [
        {"min_distinct_hits": True},
        {"min_distinct_hits": 0},
        {"min_distinct_hits": 1.5},
        {"require_strong_hit": "yes"},
        {"enabled": 1},
    ],
)
def test_invalid_policy_is_rejected(policy):
    with pytest.raises(ValueError):
        ground_theme_commentary({}, [], policy, {})
