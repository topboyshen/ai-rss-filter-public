"""Clinical-stage source wording grounds the innovative-drug theme (synthetic fixtures)."""

import pytest

from core.scorer import _score_grounding_failure, _score_theme_is_source_grounded


@pytest.mark.parametrize(
    "source",
    [
        "某药业：XY-01联合疗法Ⅲ期临床试验完成首例受试者入组",
        "某药业：XY-01联合疗法III期临床完成首例入组",
        "某生物：XY-02 Ib期临床数据积极",
        "某医药：XY-03启动二期临床",
        "某制药开展XY-04注射液临床试验",
        "某制药XY-05临床研究达到主要终点",
        "某生物XY-06的IND申请获国家药监局受理",
        "某生物XY-06 IND获批",
        "某药企向监管机构递交XY-07的NDA申请",
        "某药企XY-08 BLA申请获受理",
    ],
)
@pytest.mark.parametrize(
    "tag,logic",
    [("创新药", "研发进展可跟踪"), ("医药研发", "新药临床推进，属可跟踪研发催化")],
)
def test_clinical_stage_wording_grounds_drug_theme(source, tag, logic):
    parsed = (6.0, tag, logic, "📈")
    assert _score_theme_is_source_grounded(parsed, source, "")
    assert _score_grounding_failure(parsed, source, "") == ""


@pytest.mark.parametrize(
    "source",
    [
        "某银行发布三季度经营数据",
        "某医院门诊量增长，临床医生短缺",
        "某公司与客户签署NDA保密协议",
        "某公司IPO上市申请获受理",
        "某器械公司产品获批上市",
    ],
)
@pytest.mark.parametrize("tag,logic", [("创新药", "研发进展可跟踪"), ("医药", "新药获批推进")])
def test_drug_theme_on_non_clinical_text_is_still_rejected(source, tag, logic):
    assert (
        _score_grounding_failure((6.0, tag, logic, "📈"), source, "")
        == "ungrounded_score_theme"
    )


def test_new_evidence_words_add_no_marker_requirement():
    # Mentioning "临床试验" in a reason is checked by the existing 临床 marker only;
    # the new words are source evidence, not new markers.
    parsed = (5.0, "医疗器械", "IND申请进展可跟踪", "⚖️")
    assert _score_theme_is_source_grounded(parsed, "某器械公司注册进展", "")


def test_denied_clinical_wording_does_not_ground():
    assert (
        _score_grounding_failure(
            (6.0, "创新药", "研发进展可跟踪", "📈"), "公司不涉及临床试验业务", ""
        )
        == "ungrounded_score_theme"
    )


@pytest.mark.parametrize(
    "source",
    [
        "假期临床医生短缺，医院调整排班",
        "长期临床实践表明规范用药的重要性",
        "早期临床症状不典型，需警惕",
        "患者需定期临床随访",
        "近期临床医生招聘需求上升",
        "周期临床检验质控通报",
        "某医院临床医生获表彰",
        "某公司医疗器械临床试验完成入组",
        "某公司心脏支架临床试验达到主要终点",
        "某公司诊断试剂临床研究启动",
        "某疾控中心开展一般流行病学临床研究",
        "某大学开展临床研究项目申报",
        "国内相关临床研究仍待开展。部分药物存在心脏毒性",
        "某期刊第三期临床专刊出版",
        "第十三期临床技能培训班开班",
        "某医院收治III期肺癌患者增多",
        "2025年3期临床检验通讯",
    ],
)
@pytest.mark.parametrize("score", [9.0, 6.0])
def test_non_phase_clinical_wording_does_not_ground_drug_theme(source, score):
    parsed = (score, "创新药", "研发进展可跟踪", "📈")
    assert _score_grounding_failure(parsed, source, "") == "ungrounded_score_theme"


@pytest.mark.parametrize(
    "source",
    [
        "某公司XY-01 III期临床达到主要终点",
        "某公司XY-01 Ⅱ期临床试验完成入组",
        "某公司XY-02 Ib期数据积极",
        "某公司XY-03 IIa期临床启动",
        "某公司XY-04 Ⅰ/Ⅱ期临床获批",
        "某公司XY-05三期临床完成首例入组",
        "某公司XY-06 3期临床达到终点",
        "某公司XY-07 ADC药物临床试验启动",
        "某公司XY-08 IND获批",
    ],
)
def test_explicit_phase_or_drug_trial_grounds_drug_theme(source):
    parsed = (9.0, "创新药", "研发进展可跟踪", "📈")
    assert _score_grounding_failure(parsed, source, "") == ""


def test_device_context_blocks_generic_trial_but_not_explicit_phase():
    # Generic wording with a device fails closed even beside drug words...
    assert (
        _score_grounding_failure(
            (6.0, "创新药", "研发进展可跟踪", "📈"),
            "某药业旗下器械子公司开展临床试验",
            "",
        )
        == "ungrounded_score_theme"
    )
    # ...while an explicit drug trial phase is still evidence.
    assert _score_grounding_failure(
        (6.0, "创新药", "研发进展可跟踪", "📈"), "某药业XY-09 Ⅲ期临床达终点", ""
    ) == ""


def test_denied_phase_wording_does_not_ground():
    assert (
        _score_grounding_failure(
            (6.0, "创新药", "研发进展可跟踪", "📈"), "某药企称公司不涉及临床试验", ""
        )
        == "ungrounded_score_theme"
    )
