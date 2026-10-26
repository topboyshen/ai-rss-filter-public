"""Presentation-only evidence boundaries for both news card layouts."""

import re
from copy import deepcopy

import pytest

from sse_engine import _render_cards
from services.news_reading import render_evidence_context


def _entry(**overrides):
    return {
        "title": "公司发布公告", "raw_title": "公司发布公告",
        "link": "https://example.org/news", "source": "发布方",
        "score": 8.0, "importance_score": 8.0, "direction": "看多",
        "score_path": "llm", "safe_neutral_reason": "",
        **overrides,
    }


@pytest.mark.parametrize("reading", [False, True])
def test_empty_source_title_does_not_assert_body_is_missing(reading):
    # A body may exist even if raw_title is empty; the cached card has no
    # persisted field describing how much body the scorer actually received.
    html = _render_cards([_entry(raw_title="", summary="已有正文")], reading=reading)
    assert "标题缺失" in html
    assert "评分时使用的正文范围未记录" in html
    assert "原文缺失" not in html
    assert "正文不存在" not in html


@pytest.mark.parametrize("path,reason,expected", [
    ("llm_safe_neutral_grounding", "facts", "事实表述未通过现有来源校验"),
    ("llm_safe_neutral_grounding", "theme", "主题关联未通过现有来源校验"),
    ("llm_safe_neutral_contract", "contract", "输出格式或字段未通过校验"),
    ("llm_safe_neutral_initial", "initial", "初次输出为保守中性，具体原因未记录"),
    ("llm_safe_neutral_initial", "", "具体原因未完整记录"),
])
def test_safe_neutral_distinguishes_recorded_reason(path, reason, expected):
    for reading in (False, True):
        html = _render_cards([_entry(score_path=path, safe_neutral_reason=reason)], reading=reading)
        assert "系统兜底：保守中性" in html
        assert expected in html
        assert "记录的处理路径：模型评分" not in html


@pytest.mark.parametrize("path,label", [
    ("fast_pass", "本地快速规则"), ("local_reject", "本地拒绝规则"),
    ("fallback_local", "本地兜底"), ("similarity", "相似结果复用"),
    ("unexpected", "未知"), ("", "未知"),
])
def test_non_model_paths_are_not_labeled_model(path, label):
    html = render_evidence_context(_entry(score_path=path))
    assert f"记录的处理路径：{label}" in html
    assert "记录的处理路径：模型评分" not in html
    assert "当前展示分数与方向的逐项生成来源未确认" in html


def test_legacy_entry_without_metadata_keeps_unknowns():
    html = render_evidence_context({"title": "旧快照标题"})
    assert "标题缺失" in html
    assert "正文范围未记录" in html
    assert "记录的处理路径：未知" in html


def test_both_layouts_keep_card_facts_and_html_link_protection():
    entries = [_entry(), _entry(title="第二条", raw_title="第二条", link="javascript:alert(1)",
                                score=7.1, importance_score=7.1, direction="看空")]
    before = deepcopy(entries)
    rendered = [_render_cards(entries, reading=reading) for reading in (False, True)]
    assert entries == before
    for html in rendered:
        assert html.count("<article") == 2
        assert html.count("证据与处理范围") == 2
        assert html.index("公司发布公告") < html.index("第二条")
        assert 'href="https://example.org/news"' in html
        assert 'href="#"' in html
        assert "javascript:alert" not in html
        assert "看多" in html and "看空" in html
        assert "标题点名仅说明提及，不支持投资结论" in html
    assert re.findall(r"重要度 ([\d.]+)/10", rendered[0]) == ["8.0", "7.1"]
    assert re.findall(r"阅读优先级 ([\d.]+)/10", rendered[1]) == ["8.0", "7.1"]


def test_untrusted_persisted_path_is_bounded_and_escaped():
    html = render_evidence_context(_entry(score_path='<script>alert(1)</script>', raw_title='<b>x</b>'))
    assert "<script>" not in html and "<b>x</b>" not in html
    assert "记录的处理路径：未知" in html
