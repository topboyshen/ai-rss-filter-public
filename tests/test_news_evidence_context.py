"""Presentation-only evidence boundaries for both news card layouts."""

import re
from copy import deepcopy

import pytest

import db.database as database
import sse_engine
from services import news_reading
from services.news_reading import render_evidence_context
from sse_engine import _render_cards
from test_title_mentions_snapshot import (
    _fresh_snapshot_generated_at,
    mention_snapshot_env as mention_snapshot_env,
)


def _entry(**overrides):
    return {
        "title": "公司发布公告", "raw_title": "公司发布公告",
        "link": "https://example.org/news", "source": "发布方",
        "score": 8.0, "importance_score": 8.0, "direction": "看多",
        "score_path": "llm", "safe_neutral_reason": "",
        **overrides,
    }


def _contexts(html):
    return re.findall(r'<details class="home-news-evidence"[^>]*>.*?</details>', html)


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("raw_title,provided", [
    (None, False), ("", False), (" \t\n ", False),
    ("<p><br>&nbsp;</p>", False), ("&lt;em&gt;&lt;/em&gt;", False),
    ("<script>not-source-text()</script>", False),
    ("公司发布公告", True), ("<em>公司</em>发布公告", True), ("研发投入<5%", True),
])
def test_card_title_state_never_claims_source_storage_or_body_availability(reading, raw_title, provided):
    entry = _entry(raw_title=raw_title, summary="不应展示的历史正文", content="不应展示的正文内容")
    html = _render_cards([entry], reading=reading)
    context, = _contexts(html)
    expected = "本卡片提供原始标题" if provided else "本卡片未提供原始标题"
    assert expected in context
    assert "无法从本卡片确认评分时使用的正文范围" in context
    assert "不应展示" not in html
    for unsupported_claim in ("缺原文无法判断", "原文缺失", "正文不存在", "正文为空", "原始标题已记录"):
        assert unsupported_claim not in context


@pytest.mark.parametrize("value", [None, "", " \t", [], {}, 0, 7, False, True])
def test_unavailable_or_malformed_raw_title_does_not_become_source_evidence(value):
    html = render_evidence_context(_entry(raw_title=value))
    assert "本卡片未提供原始标题" in html
    assert "本卡片提供原始标题" not in html


@pytest.mark.parametrize("reading", [False, True])
def test_missing_legacy_title_metadata_does_not_infer_from_display_title(reading):
    entry = _entry()
    del entry["raw_title"]
    html = _render_cards([entry], reading=reading)
    context, = _contexts(html)
    assert "本卡片未提供原始标题" in context
    assert "本卡片提供原始标题" not in context


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("path", [
    "llm_safe_neutral_grounding", "stage2_safe_neutral_grounding",
    "llm_safe_neutral_contract", "stage2_safe_neutral_contract",
    "llm_safe_neutral_initial", "stage2_safe_neutral_initial",
])
@pytest.mark.parametrize("reason,expected", [
    ("facts", "事实表述未通过现有来源校验"),
    ("theme", "主题关联未通过现有来源校验"),
    ("contract", "输出格式或字段未通过校验"),
    ("initial", "初次输出为保守中性，具体原因未记录"),
    ("", "具体原因未完整记录"),
    ("<script>private-rejection-detail</script>", "具体原因未完整记录"),
])
def test_all_safe_neutral_paths_keep_recorded_reason_separate(reading, path, reason, expected):
    # Direct rendering covers diagnostic/legacy entries too. This does not
    # assert that the unchanged public selection admits safe-neutral entries.
    html = _render_cards([_entry(score_path=path, safe_neutral_reason=reason)], reading=reading)
    context, = _contexts(html)
    assert "缓存处理记录：系统兜底（保守中性）" in context
    assert "缓存处理记录：模型评分" not in context
    assert "保守中性是处理结果，不表示新闻不重要" in context
    assert expected in html and expected not in context
    assert "private-rejection-detail" not in html


@pytest.mark.parametrize("path,label", [
    ("llm", "模型评分"), ("stage2", "模型二阶段评分"),
    ("fast_pass", "本地快速规则"), ("local_reject", "本地拒绝规则"),
    ("fallback_local", "本地兜底"), ("similarity", "相似结果复用"),
    ("radar", "雷达路径"), ("parse_error", "解析失败"),
    ("quality_quarantine", "质量隔离"),
    ("unexpected-private-path", "未知"), ("", "未知"), (None, "未知"),
])
def test_cached_path_has_bounded_labels_not_per_field_model_attribution(path, label):
    html = render_evidence_context(_entry(score_path=path))
    assert f"缓存处理记录：{label}" in html
    assert "unexpected-private-path" not in html
    assert "当前展示分数与方向的逐项生成来源未确认" in html
    assert "分数仅为阅读优先级" in html


def test_legacy_entry_without_metadata_keeps_unknowns():
    html = render_evidence_context({"title": "旧快照标题"})
    assert "本卡片未提供原始标题" in html
    assert "无法从本卡片确认评分时使用的正文范围" in html
    assert "缓存处理记录：未知" in html


def test_both_layouts_share_context_without_changing_card_facts_or_inputs():
    entries = [_entry(), _entry(title="第二条", raw_title="第二条", link="javascript:alert(1)",
                                score=7.1, importance_score=7.1, direction="看空")]
    before = deepcopy(entries)
    rendered = [_render_cards(entries, reading=reading) for reading in (False, True)]
    assert entries == before
    assert _contexts(rendered[0]) == _contexts(rendered[1])
    for html in rendered:
        assert html.count("<article") == len(_contexts(html)) == 2
        assert html.index("公司发布公告") < html.index("第二条")
        assert 'href="https://example.org/news"' in html
        assert 'href="#"' in html
        assert "javascript:alert" not in html
        assert "看多" in html and "看空" in html
        assert "标题点名仅说明提及，不支持投资结论" in html
        assert not re.search(r"<details[^>]*\bopen(?:\s|=|>)", html)
    assert re.findall(r"重要度 ([\d.]+)/10", rendered[0]) == ["8.0", "7.1"]
    assert re.findall(r"阅读优先级 ([\d.]+)/10", rendered[1]) == ["8.0", "7.1"]


@pytest.mark.parametrize("reading", [False, True])
def test_title_company_and_theme_are_separate_from_untrusted_model_fields(reading):
    title = "示例药业披露创新药研发进展"
    entry = _entry(
        title=title, raw_title=title, tag="伪造公司股份", logic="伪造公司股份受益",
        summary="private-source-body", content="private-source-content",
        _title_equity_mentions={
            "raw_title": title, "status": "ok", "reason": "matched",
            "mentions": [{"security_id": "SSE:688001", "name": "示例药业",
                          "ticker": "688001", "exchange": "SSE"}],
        },
    )
    html = _render_cards([entry], reading=reading)
    context, = _contexts(html)
    direct = re.search(r'<div class="home-news-title-mentions"[^>]*>(.*?)</div>', html).group(1)
    assert "标题提及：示例药业（688001·SSE）" in direct
    assert "创新药" not in direct
    assert "主题背景／规则池（非本文点名、不代表受益）" in html
    assert "标题匹配主题：创新药" in html
    assert "示例药业" not in context
    for value in ("伪造公司股份", "private-source-body", "private-source-content"):
        assert value not in html


@pytest.mark.parametrize("reading", [False, True])
def test_theme_without_title_company_does_not_invent_direct_mention(reading):
    title = "创新药行业研发进展"
    entry = _entry(title=title, raw_title=title, _title_equity_mentions={
        "raw_title": title, "status": "ok", "reason": "no_match", "mentions": [],
    })
    html = _render_cards([entry], reading=reading)
    assert "标题匹配主题：创新药" in html
    assert 'class="home-news-title-mentions"' not in html
    assert "标题提及：" not in html
    assert len(_contexts(html)) == 1


@pytest.mark.parametrize("reading", [False, True])
def test_prepared_card_rendering_adds_no_io(monkeypatch, reading):
    # Import dependencies before placing the I/O tripwires. No lookup, source
    # fetch, model request, or configuration read belongs to card formatting.
    import builtins
    import sqlite3
    import socket

    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Pure card rendering must not perform I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(database, "get_direct_equity_mentions_batch", forbidden)
    monkeypatch.setattr(sse_engine, "load_config", forbidden)
    html = _render_cards([_entry()], reading=reading)
    assert len(_contexts(html)) == 1
    assert calls == []


def test_old_published_snapshot_is_not_reinterpreted_or_augmented(mention_snapshot_env, monkeypatch):
    from services import dashboard_snapshots as snapshots

    app, directory, database_path = mention_snapshot_env
    old_html = '<article>旧版新闻卡<div>标题提及：识别暂不可用</div></article>'
    snapshots.write_dashboard_snapshot(
        "realtime", old_html, generated_at=_fresh_snapshot_generated_at(),
        snapshot_dir=directory, db_path=database_path,
    )
    monkeypatch.setenv("AI_FILTER_ROLE", "web")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Published snapshots must pass through without rendering current metadata")

    monkeypatch.setattr(sse_engine, "_render_cards", forbidden)
    monkeypatch.setattr(sse_engine, "_load_from_db", forbidden)
    monkeypatch.setattr(sse_engine, "render_evidence_context", forbidden)
    monkeypatch.setattr(news_reading, "render_evidence_context", forbidden)
    response = app.test_client().get("/glance-sse")
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    assert response.get_data(as_text=True) == old_html
    assert snapshots.read_dashboard_snapshot(
        "realtime", snapshot_dir=directory, db_path=database_path,
    )["html"] == old_html


def test_untrusted_persisted_path_is_bounded_and_escaped():
    html = render_evidence_context(_entry(score_path='<script>alert(1)</script>', raw_title='<b>x</b>'))
    assert "<script>" not in html and "<b>x</b>" not in html
    assert "缓存处理记录：未知" in html


@pytest.mark.parametrize("reading", [False, True])
def test_real_event_score_does_not_turn_cached_path_into_its_field_provenance(tmp_path, monkeypatch, reading):
    from test_home_score_contract_labels import _cfg, _database

    _database(tmp_path, monkeypatch, event_priority=9.0, event_direction="看空", cache_direction="中性")
    entries = sse_engine._load_from_db(cfg=_cfg(), strict=True)
    assert len(entries) == 1
    assert entries[0]["score"] == 9.0 and entries[0]["direction"] == "看空"
    assert entries[0]["score_path"] == "llm"
    html = _render_cards(entries, reading=reading)
    context, = _contexts(html)
    assert "缓存处理记录：模型评分" in context
    assert "当前展示分数与方向的逐项生成来源未确认" in context
    assert "9.0/10" in html and "看空" in html
    assert "当前分数来自模型" not in context and "当前方向来自模型" not in context


def test_fresh_source_link_and_scoring_owner_are_not_claimed_to_be_the_same(monkeypatch):
    from test_home_news_reading import NOW, _client, _entry as source_entry

    prefix = "企业发布投资计划并详细说明项目的建设和实施范围，" * 8
    low_title, high_title = prefix + "原先方案。", prefix + "更新方案。"
    entries = [
        source_entry(low_title, raw_title="", score=7.5, importance_score=7.5, event_id="",
                     score_path="llm", time=NOW - 10, link="https://news.example/fresh-copy"),
        source_entry(high_title, raw_title="", score=8.8, importance_score=8.8, event_id="",
                     score_path="llm", time=NOW - 60, source="另一来源", feed_url="https://feed.example/b"),
    ]
    html = _client(monkeypatch, entries).get("/glance-sse").get_data(as_text=True)
    context, = _contexts(html)
    assert high_title in html and low_title not in html
    assert 'href="https://news.example/fresh-copy"' in html
    assert "阅读优先级 7.5/10" in html
    assert "本卡片未提供原始标题" in context
    assert "无法从本卡片确认评分时使用的正文范围" in context
    assert "当前展示分数与方向的逐项生成来源未确认" in context
