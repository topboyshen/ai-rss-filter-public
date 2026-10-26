"""Equivalent money spellings must not fail before numeric grounding runs."""

from decimal import localcontext

import pytest

from core.score_evidence import _event_money_comparison_text, _event_objects_related
from core.scorer import _score_grounding_verdict


@pytest.mark.parametrize(
    "source,logic",
    [
        ("甲公司中标1.4亿元光伏项目", "甲公司中标14000万元光伏项目"),
        ("甲公司中标14000万元光伏项目", "甲公司中标1.4亿元光伏项目"),
        ("甲公司中标1.40亿元光伏项目", "甲公司中标1.4亿元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "甲公司中标1.40亿元光伏项目"),
        ("甲公司中标140000000元光伏项目", "甲公司中标1.4亿元光伏项目"),
        ("甲公司中标0.014万亿元光伏项目", "甲公司中标140亿元光伏项目"),
        ("甲公司中标0.0001万元光伏项目", "甲公司中标1元光伏项目"),
        ("甲公司中标1.23456789亿元光伏项目", "甲公司中标123456789元光伏项目"),
        ("甲公司中标１.４０亿元光伏项目", "甲公司中标14000万元光伏项目"),
        ("甲公司计划中标1.4亿元光伏项目", "甲公司计划中标14000万元光伏项目"),
        ("甲公司未中标1.4亿元光伏项目", "甲公司未中标14000万元光伏项目"),
    ],
)
def test_equivalent_event_amount_survives_scoring_grounding(source, logic):
    assert _score_grounding_verdict((8, "公司公告", logic, "看多"), source, "") == ("", "")


@pytest.mark.parametrize(
    "source,logic",
    [
        ("甲公司中标1.4亿元光伏项目", "甲公司中标24000万元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "甲公司中标114000万元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "甲公司中标1.4万元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "甲公司中标1.4亿美元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "甲公司中标1.4亿港元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "乙公司中标14000万元光伏项目"),
        ("甲公司未中标1.4亿元光伏项目", "甲公司已中标14000万元光伏项目"),
        ("甲公司拟中标1.4亿元光伏项目", "甲公司已中标14000万元光伏项目"),
        ("甲公司预计中标1.4亿元光伏项目", "甲公司中标14000万元光伏项目"),
        ("甲公司中标不超过1.4亿元光伏项目", "甲公司中标超过14000万元光伏项目"),
        ("甲公司中标1.4亿元光伏项目", "甲公司中标14000万元水电项目"),
        ("甲公司中标光伏电站项目", "甲公司中标水电项目"),
        ("甲公司中标1.00000000000000000000000000001亿元光伏项目",
         "甲公司中标1.00000000000000000000000000002亿元光伏项目"),
        ("甲公司中标100000000000000000000000000001元光伏项目",
         "甲公司中标100000000000000000000000000002元光伏项目"),
    ],
)
def test_event_money_normalization_does_not_relax_fact_safety(source, logic):
    reason, _detail = _score_grounding_verdict((8, "公司公告", logic, "看多"), source, "")
    assert reason == "ungrounded_score_facts"


def test_event_object_amount_comparison_uses_exact_decimal_not_global_precision():
    with localcontext() as context:
        context.prec = 3
        assert _event_objects_related("1.23456789亿元光伏项目", "123456789元光伏项目")
        assert not _event_objects_related("1.23456789亿元光伏项目", "123456790元光伏项目")


@pytest.mark.parametrize(
    "source,logic",
    [
        ("1.4亿元光伏项目", "1.4亿美元光伏项目"),
        ("1.4亿元光伏项目", "1.4亿港元光伏项目"),
        ("1.4亿元光伏项目", "14000万元水电项目"),
        ("光伏电站项目", "水电项目"),
    ],
)
def test_event_object_comparison_keeps_currency_and_object(source, logic):
    assert not _event_objects_related(source, logic)


@pytest.mark.parametrize("text", ["000062股权", "2026年项目", "1.40亿美元光伏项目", "1.40亿港元项目"])
def test_event_money_comparison_preserves_nonmoney_identity_and_currency(text):
    assert _event_money_comparison_text(text) == text
