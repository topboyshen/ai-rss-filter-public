"""Normal same-subject compression is not a factual reversal or new event."""

import json
from unittest.mock import Mock

import pytest

from core.scorer import LLMBackend, Scorer, _parse_summary_result, _summary_event_text


@pytest.mark.parametrize(
    ("source", "summary"),
    [
        ("宁德时代表示今日已获得项目订单", "宁德时代获得项目订单"),
        ("宁德时代今日获得项目订单", "宁德时代获得项目订单"),
        ("宁德时代表示未获得项目订单", "宁德时代未获得项目订单"),
        ("宁德时代表示拟获得项目订单", "宁德时代拟获得项目订单"),
        ("宁德时代计划今日获得项目订单", "宁德时代计划获得项目订单"),
        ("宁德时代今日称计划获得项目订单", "宁德时代计划获得项目订单"),
        ("宁德时代表示已取得项目订单", "宁德时代获得项目订单"),
        ("宁德时代表示今日已签署项目合同", "宁德时代签订项目合同"),
        ("会议进一步扩大有效投资", "会议扩大有效投资"),
        ("会议拟进一步扩大有效投资", "会议拟扩大有效投资"),
        ("会议未进一步扩大有效投资", "会议未扩大有效投资"),
        ("会议部署进一步扩大有效投资", "会议部署扩大有效投资"),
    ],
)
def test_plain_expression_compression_is_accepted(source, summary):
    assert _parse_summary_result(
        json.dumps({"summary": summary}, ensure_ascii=False), source, ""
    ) == (summary, "")


@pytest.mark.parametrize(
    ("source", "summary"),
    [
        ("宁德时代表示今日已获得项目订单", "比亚迪获得项目订单"),
        ("宁德时代表示未获得项目订单", "宁德时代获得项目订单"),
        ("宁德时代表示拟获得项目订单", "宁德时代获得项目订单"),
        ("宁德时代未表示获得项目订单", "宁德时代未获得项目订单"),
        ("宁德时代今日未表示获得项目订单", "宁德时代未获得项目订单"),
        ("宁德时代没有表示获得项目订单", "宁德时代未获得项目订单"),
        ("宁德时代表示已获得政府补助", "宁德时代获得项目订单"),
        ("宁德时代表示。比亚迪获得项目订单", "宁德时代获得项目订单"),
        ("宁德时代表示\n比亚迪获得项目订单", "宁德时代获得项目订单"),
        ("宁德时代表示今日", "宁德时代获得项目订单"),
        ("会议拟进一步扩大有效投资", "会议扩大有效投资"),
        ("会议未进一步扩大有效投资", "会议扩大有效投资"),
        ("会议进一步扩大有效投资", "会议拟扩大有效投资"),
        ("会议进一步扩大有效投资", "央行扩大有效投资"),
        ("会议部署未进一步扩大有效投资", "会议部署扩大有效投资"),
        ("会议部署拟进一步扩大有效投资", "会议部署扩大有效投资"),
        ("会议部署进一步扩大有效投资", "会议部署扩大无效投资"),
        ("会议部署进一步。扩大有效投资", "会议部署扩大有效投资"),
    ],
)
def test_normalization_does_not_erase_fact_boundaries(source, summary):
    assert _parse_summary_result(
        json.dumps({"summary": summary}, ensure_ascii=False), source, ""
    ) == ("", "ungrounded_event_context")


@pytest.mark.parametrize(
    ("source", "view"),
    [
        ("宁德时代表示今日已获得项目订单", "宁德时代已获得项目订单"),
        ("宁德时代表示未获得项目订单", "宁德时代未获得项目订单"),
        ("宁德时代未表示获得项目订单", "宁德时代未表示获得项目订单"),
        ("宁德时代计划今日获得项目订单", "宁德时代计划获得项目订单"),
        ("宁德时代表示，获得项目订单", "宁德时代表示,获得项目订单"),
        ("会议进一步扩大有效投资", "会议扩大有效投资"),
        ("会议未进一步扩大有效投资", "会议未扩大有效投资"),
        ("会议进一步说明扩大有效投资", "会议进一步说明扩大有效投资"),
        ("会议今日\n进一步扩大有效投资", "会议今日\n进一步扩大有效投资"),
    ],
)
def test_comparison_view_is_bounded_and_preserves_original_claim(source, view):
    assert _summary_event_text(source) == view


@pytest.mark.parametrize(
    ("title", "body", "summary"),
    [
        (
            "宁德时代发布经营进展公告",
            "宁德时代表示今日已获得项目订单。公告提示，合同后续履行情况以及收益确认仍存在不确定性，相关信息以正式公告为准。",
            "宁德时代获得项目订单",
        ),
        (
            "国务院常务会议部署有关工作",
            "会议部署进一步扩大有效投资。会议要求统筹把握工作力度和节奏，坚持实事求是，完善配套政策，推动各项工作落实。",
            "会议部署扩大有效投资",
        ),
    ],
)
def test_original_full_source_reproductions_are_accepted(title, body, summary):
    assert len(body) > 50
    assert _parse_summary_result(
        json.dumps({"summary": summary}, ensure_ascii=False), title, body
    ) == (summary, "")


@pytest.mark.parametrize(
    ("title", "body", "summary"),
    [
        (
            "宁德时代发布经营进展公告",
            "宁德时代表示今日已获得项目订单。公告提示，合同后续履行情况以及收益确认仍存在不确定性，相关信息以正式公告为准。",
            "宁德时代获得项目订单",
        ),
        (
            "国务院常务会议部署有关工作",
            "会议部署进一步扩大有效投资。会议要求统筹把握工作力度和节奏，坚持实事求是，完善配套政策，推动各项工作落实。",
            "会议部署扩大有效投资",
        ),
    ],
)
def test_original_full_sources_pass_summary_pipeline_without_repair(
    monkeypatch, title, body, summary,
):
    backend = LLMBackend(
        name="plain-expression", api_key="", base_url="", model="fixture", rate_limit=0,
    )
    backend.client = object()
    scorer = Scorer()
    monkeypatch.setattr(scorer, "backends", [backend])
    monkeypatch.setattr(scorer, "_log_llm_call", lambda *_args, **_kwargs: None)
    call = Mock(return_value=json.dumps({"summary": summary}, ensure_ascii=False))
    monkeypatch.setattr(scorer, "_call_llm", call)
    assert len(body) > 50
    assert scorer.summarize(
        title, body,
        {"generate_summary": True, "llm_semantic_repair": {"enabled": True},
         "llm_routing": {"summary": ["plain-expression"]}},
    ) == summary
    assert call.call_count == 1
