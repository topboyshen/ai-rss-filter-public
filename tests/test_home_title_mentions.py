"""Title-bound company mentions stay independent of theme pools and rendering."""

from copy import deepcopy
import re

import pytest

import db.database as database
import sse_engine
from services.news_mentions import (
    TITLE_MENTIONS_READING_STYLE,
    attach_title_mentions,
)
from test_home_news_reading import NOW, _client, _entry


def _result(title, name="示例药业", *, status="ok", reason="matched"):
    mentions = [{
        "security_id": "SSE:688001", "name": name,
        "ticker": "688001", "exchange": "SSE",
    }] if name else []
    return {"raw_title": title, "status": status, "reason": reason, "mentions": mentions}


def _stub_lookup(monkeypatch, lookup):
    monkeypatch.setattr(database, "get_direct_equity_mentions_batch", lookup)


@pytest.mark.parametrize("reading", [False, True])
def test_direct_company_without_theme_is_shared_and_rendering_has_no_io(monkeypatch, reading):
    title = "示例药业：子公司收到监管通知"
    entries = [_entry(title)]
    before = deepcopy(entries)
    _stub_lookup(monkeypatch, lambda titles: [_result(title) for title in titles])
    prepared = attach_title_mentions(entries)

    def forbidden(_titles):
        pytest.fail("Rendering must not read the master")

    _stub_lookup(monkeypatch, forbidden)
    html = sse_engine._render_cards(prepared, reading=reading)
    assert entries == before
    assert "标题提及：示例药业（688001·SSE）" in html
    assert "仅说明提及，不代表推荐或受益" in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html
    assert "主题背景／规则池" not in html
    assert "confidence" not in html and "relevance" not in html
    if reading:
        assert "方向与入选依据" in html
        assert not re.search(r"<details[^>]*\bopen\b", html)


@pytest.mark.parametrize("reading", [False, True])
def test_direct_company_and_theme_remain_separate(monkeypatch, reading):
    title = "示例药业披露创新药研发进展"
    _stub_lookup(monkeypatch, lambda titles: [_result(title) for title in titles])
    html = sse_engine._render_cards(attach_title_mentions([_entry(title)]), reading=reading)
    direct_start = html.index('class="home-news-title-mentions"')
    pool_start = html.index("主题背景／规则池（非本文点名、不代表受益）")
    assert direct_start < pool_start
    assert "标题提及：示例药业（688001·SSE）" in html
    assert "标题匹配主题：创新药" in html
    assert "未提供个股示例" in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html
    assert not re.search(r"<details[^>]*\bopen\b", html)


@pytest.mark.parametrize("reading", [False, True])
def test_checked_no_match_keeps_one_theme_note(monkeypatch, reading):
    _stub_lookup(monkeypatch, lambda titles: [_result(t, "", reason="no_match") for t in titles])
    html = sse_engine._render_cards(
        attach_title_mentions([_entry("普通公司发布例行公告")]), reading=reading,
    )
    assert html.count(sse_engine._UNMAPPED_TITLE_NOTE) == 1
    assert "标题提及：" not in html
    assert "主题背景／规则池" not in html


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("reason", ["missing_title", "master_unavailable", "ambiguous"])
def test_missing_failure_and_ambiguity_do_not_claim_no_match(monkeypatch, reading, reason):
    title = "" if reason == "missing_title" else "示例药业收到通知"
    status = "ok" if reason == "ambiguous" else "unavailable"
    _stub_lookup(monkeypatch, lambda titles: [_result(t, "", status=status, reason=reason) for t in titles])
    entry = _entry("示例药业的展示标题", raw_title=title, _reading_title="示例药业展示用全文")
    html = sse_engine._render_cards(attach_title_mentions([entry]), reading=reading)
    assert "<article" in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html
    assert "688001" not in html
    expected = {"missing_title": "原始标题未记录", "master_unavailable": "识别暂不可用",
                "ambiguous": "证券身份有歧义"}[reason]
    assert expected in html


@pytest.mark.parametrize("reading", [False, True])
def test_stale_result_is_rejected_without_hiding_current_news(monkeypatch, reading):
    title = "示例药业收到通知"
    entry = _entry("另一公司收到通知", _title_equity_mentions=_result(title))
    html = sse_engine._render_cards([entry], reading=reading)
    assert "另一公司收到通知" in html and "识别暂不可用" in html
    assert "688001" not in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html


def test_batch_failure_keeps_all_cards_and_does_not_expose_exception(monkeypatch):
    def broken(_titles):
        raise RuntimeError("private-database-path-and-secret")

    _stub_lookup(monkeypatch, broken)
    entries = [_entry("新闻一"), _entry("创新药新闻二", link="https://news.example/two")]
    html = _client(monkeypatch, entries).get("/glance-sse").get_data(as_text=True)
    assert html.count("<article") == 2
    assert html.count("识别暂不可用") == 2
    assert "private-database-path-and-secret" not in html
    assert "标题匹配主题：创新药" in html
    assert sse_engine._UNMAPPED_TITLE_NOTE not in html


def test_only_final_selected_raw_titles_are_batched_after_dedupe(monkeypatch):
    prefix = "公司发布通知并详细说明相关事项的最新进展，" * 5
    older, newer = prefix + "甲公司收到通知", prefix + "乙公司收到通知"
    entries = [
        _entry(older, score=7.5, importance_score=7.5, time=NOW - 10,
               _title_equity_mentions=_result(older, "甲公司")),
        _entry(newer, score=8.8, importance_score=8.8, time=NOW - 60,
               source="另一来源", feed_url="https://feed.example/b",
               link="https://news.example/second"),
        _entry("未选入的旧新闻", time=NOW - 49 * 3600),
    ]
    batches = []

    def lookup(titles):
        batches.append(titles)
        return [_result(t, "乙公司") for t in titles]

    _stub_lookup(monkeypatch, lookup)
    html = _client(monkeypatch, entries).get("/glance-sse").get_data(as_text=True)
    assert batches == [[newer]]
    assert html.count("<article") == 1
    assert "标题提及：乙公司（688001·SSE）" in html
    assert "标题提及：甲公司" not in html
    assert "合并2条" in html


def test_source_only_input_and_batch_result_order_are_enforced(monkeypatch):
    entries = [_entry("包装标题：示例药业", raw_title="", logic="示例药业业绩增长"),
               _entry("展示标题", raw_title="原始标题", _reading_title="另一个公司展示全文")]
    captured = []

    def misordered(titles):
        captured.extend(titles)
        return [_result(t) for t in reversed(titles)]

    _stub_lookup(monkeypatch, misordered)
    prepared = attach_title_mentions(entries)
    assert captured == ["", "原始标题"]
    assert all(e["_title_equity_mentions"]["status"] == "unavailable" for e in prepared)
    assert all(not e["_title_equity_mentions"]["mentions"] for e in prepared)


@pytest.mark.parametrize("reading", [False, True])
def test_names_are_escaped_and_internal_scores_never_rendered(monkeypatch, reading):
    title = "测试公司收到通知"
    result = _result(title, '<svg/onload=alert(1)>')
    result["mentions"][0].update(confidence=0.96, relevance=10.0)
    _stub_lookup(monkeypatch, lambda _titles: [result])
    html = sse_engine._render_cards(attach_title_mentions([_entry(title)]), reading=reading)
    assert "<svg" not in html and "&lt;svg/onload=alert(1)&gt;" in html
    assert "0.96" not in html and "10.0" not in html


def test_lead_company_line_has_scoped_contrast_and_wrap_rule(monkeypatch):
    _stub_lookup(monkeypatch, lambda titles: [_result(t) for t in titles])
    html = _client(monkeypatch, [_entry("示例药业收到通知")]).get("/glance-sse").get_data(as_text=True)
    assert TITLE_MENTIONS_READING_STYLE in html
    assert 'class="home-news-title-mentions"' in html
    assert "overflow-wrap:anywhere;" in html


def test_empty_batch_does_not_read_master(monkeypatch):
    def forbidden(_titles):
        pytest.fail("An empty card batch must not query securities")

    _stub_lookup(monkeypatch, forbidden)
    assert attach_title_mentions([]) == []
