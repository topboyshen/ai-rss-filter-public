"""A recorded ordinary word must not disable genuine numeric grounding guards."""

import json
from types import SimpleNamespace

import pytest

from core.scorer import (
    Scorer,
    _has_unsupported_numeric_expression,
    _numeric_claims_are_grounded,
    _score_grounding_verdict,
)


FROZEN_TITLE = "商务部新闻发言人就中欧混动汽车、电动汽车磋商共识答记者问"
# Source and the two rejected outputs from sealed sample S09. No transport is used.
FROZEN_SOURCE = (
    "中欧在贸易投资磋商机制下持续对话，达成多项成果共识，有利于维护稳定、平衡的中欧关键贸易伙伴关系。"
    "双方本着相互尊重、平等协商的原则，就混动汽车、电动汽车贸易进行了密集磋商，平衡处理经贸分歧和彼此关切。 "
    "关于混动汽车，双方强调并确认了三项基本原则。一是尊重事实。根据欧盟统计局数据，2024年欧盟自中国进口混动汽车10.7万辆，"
    "2025年为30.2万辆。双方在2025年基础上达成向上安排，确保中国企业的出口空间和稳定增长，照顾了双方产业基于现实的合理关切。"
    "二是非歧视。任何措施都不会专门针对中国产品，更不能歧视性对待中国企业。中方获得配额将不会低于欧方的其他任何进口来源地。"
    "三是合规。双方确认贸易安排应符合世贸组织规则。中方保留在世贸组织框架下诉诸争端解决、要求贸易补偿等一切权利。"
    "这三项基本原则贯穿始终，是保障中国企业合法权益的坚实基础。 关于电动汽车，中欧双方于今年1月实现反补贴案“软着陆”，"
    "欧方发布了价格承诺通用指导文件。就此，欧方将继续推进电动汽车案企业价格承诺相关程序，以及其它复审程序。 "
    "在对等互利基础上，上述共识有力维护了中国企业的实际利益，避免了中欧贸易摩擦局面升级，也为中欧双方提供了稳定、可预期、"
    "可持续的贸易投资环境，有助于双边经贸关系稳定、平衡和健康发展。"
)


@pytest.mark.parametrize(
    "stage,score,tag,logic",
    [
        (
            "stage1",
            8,
            "新能源汽车",
            "中欧就混动汽车达成向上安排确保出口空间增长，电动汽车反补贴案软着陆并推进价格承诺程序，避免贸易摩擦升级",
        ),
        (
            "stage2",
            7,
            "新能源汽车出口",
            "中欧就混动汽车达成配额向上安排，确认非歧视原则；电动汽车反补贴案软着陆后推进价格承诺程序，稳定出口预期。",
        ),
    ],
)
def test_recorded_soft_landing_attempt_passes_full_validator(
    monkeypatch, stage, score, tag, logic
):
    monkeypatch.setattr("core.scorer.record_score_validation", lambda **kwargs: None)
    monkeypatch.setattr("core.scorer.capture_validation_attempt", lambda **kwargs: None)
    scorer = object.__new__(Scorer)
    raw = json.dumps(
        dict(score=score, tag=tag, logic=logic, sentiment="看多"), ensure_ascii=False
    )
    parsed, reason, detail = scorer._validate_scoring_attempt(
        raw,
        FROZEN_TITLE,
        FROZEN_SOURCE,
        "initial",
        "",
        stage,
        SimpleNamespace(name="recorded-fixture", model="no-transport"),
        {},
    )
    assert parsed == (float(score), tag, logic, "📈")
    assert reason == detail == ""


@pytest.mark.parametrize(
    "word", ["软着陆", "“软着陆”", "「软着陆」", "软着陆后推进价格承诺程序"]
)
def test_ordinary_soft_landing_word_is_not_an_unsupported_number(word):
    text = f"电动汽车反补贴案{word}"
    assert not _has_unsupported_numeric_expression(text)
    assert _numeric_claims_are_grounded(text, text)


@pytest.mark.parametrize(
    "text",
    [
        "合同金额陆万元",
        "合同金额陆佰万元",
        "合同金额壹佰万元",
        "合同金额贰仟万元",
        "合同金额叁拾万元",
        "合同金额肆万元",
        "合同金额伍万元",
        "合同金额柒万元",
        "合同金额捌万元",
        "合同金额玖万元",
        "电动汽车反补贴案软着陆，合同金额陆万元",
        "合同金额陆佰万元，电动汽车反补贴案软着陆",
        "软着陆，合同金额陆万元，再次软着陆",
        "软着陆万元",
        "软着陆佰万元",
        "软着陆.5万元",
        "软着陆5万元",
        "软着陆 .5万元",
        "软着陆 5万元",
        "软着陆 万元",
        "软着陆　.5万元",
        # These constructed boundaries exercise suspect/ratio checks on raw text.
        "软着陆吨",
        "软着陆成",
        "软着陆折",
    ],
)
def test_financial_numerals_and_ambiguous_numeric_continuations_stay_guarded(text):
    assert _has_unsupported_numeric_expression(text)
    # Even verbatim source support cannot make an unsupported numeral supported.
    assert not _numeric_claims_are_grounded(text, text)


@pytest.mark.parametrize(
    "source,logic,reason,kind",
    [
        (
            "电动汽车反补贴案软着陆，订单3亿元",
            "电动汽车反补贴案软着陆，订单3亿元",
            "",
            "",
        ),
        (
            "电动汽车反补贴案软着陆，订单3亿元",
            "电动汽车反补贴案软着陆，订单5亿元",
            "ungrounded_score_facts",
            "numeric_fact_failure",
        ),
        (
            "电动汽车反补贴案软着陆，订单3亿元",
            "电动汽车反补贴案软着陆，订单3万元",
            "ungrounded_score_facts",
            "numeric_fact_failure",
        ),
        (
            "电动汽车反补贴案软着陆，利润下降20%",
            "电动汽车反补贴案软着陆，利润增长20%",
            "ungrounded_score_facts",
            "numeric_fact_failure",
        ),
        (
            "电动汽车反补贴案软着陆",
            "电动汽车反补贴案软着陆并获得订单",
            "ungrounded_score_facts",
            "event_fact_failure",
        ),
    ],
)
def test_soft_landing_never_exempts_other_numeric_or_event_claims(
    source, logic, reason, kind
):
    assert _score_grounding_verdict((8, "电动汽车", logic, "📈"), source, "") == (
        reason,
        kind,
    )


def test_soft_landing_never_exempts_an_unsupported_theme():
    assert _score_grounding_verdict(
        (8, "AI", "AI业务软着陆", "📈"),
        "电动汽车反补贴案软着陆",
        "",
    ) == ("ungrounded_score_theme", "theme_failure")
