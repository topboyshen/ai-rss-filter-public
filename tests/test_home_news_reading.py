"""Synthetic coverage for the homepage's presentation-only news reader."""

from copy import deepcopy
import re
import sqlite3

from flask import Flask
import pytest

import sse_engine
from sse_engine import _UNMAPPED_TITLE_NOTE
from services.news_reading import restore_reading_titles
from utils.helpers import escape_html


NOW = 2_000_000_000
LONG_TITLE = "央行公布结构性工具的实施安排，" + "明确申请流程和执行范围，" * 10 + "并说明下一阶段安排。"


def _entry(title=LONG_TITLE, **fields):
    return {
        "title": title, "raw_title": title, "score": 8.8,
        "importance_score": 8.8, "direction": "看多",
        "link": "https://news.example/article", "source": "测试来源",
        "feed_url": "https://feed.example/a", "time": NOW - 60,
        "_selection_threshold": 7.2, "_realtime_section": "fresh_strong",
        **fields,
    }


def _client(monkeypatch, entries):
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    monkeypatch.setattr(sse_engine.time, "time", lambda: NOW)
    monkeypatch.setattr(sse_engine, "load_config", lambda: {
        "threshold": {"pass": 7},
        "feeds": [
            {"name": name, "url": f"https://feed.example/{suffix}", "enabled": True}
            for suffix, name in (("a", "测试来源"), ("b", "另一来源"), ("c", "回看来源"))
        ],
    })
    monkeypatch.setattr(sse_engine, "_load_from_db", lambda **_kwargs: deepcopy(entries))
    app = Flask(__name__)
    app.add_url_rule("/glance-sse", view_func=sse_engine.glance_sse_widget)
    return app.test_client()


def test_reading_cards_keep_selected_identity_order_scores_and_default_renderer():
    entries = [_entry(), _entry("行业观察新闻", score=7.3, importance_score=7.3,
                                link="https://news.example/second", source="另一来源")]
    before = deepcopy(entries)
    compact = sse_engine._render_cards(entries)
    reading = sse_engine._render_cards(entries, reading=True)

    assert entries == before
    assert re.findall(r'href="([^"]+)"', reading) == re.findall(r'href="([^"]+)"', compact)
    assert re.findall(r"阅读优先级 ([\d.]+)/10", reading) == re.findall(r"重要度 ([\d.]+)/10", compact)
    assert reading.count("<article") == compact.count("<article") == 2
    assert LONG_TITLE in reading and LONG_TITLE not in compact
    assert "-webkit-line-clamp" not in reading
    assert "-webkit-line-clamp:2" in compact


def test_reading_metadata_is_primary_and_analysis_is_closed(monkeypatch):
    monkeypatch.setattr(sse_engine.time, "time", lambda: NOW)
    html = sse_engine._render_cards([_entry()], reading=True)
    visible, details = html.split("<details", 1)

    assert "测试来源" in visible
    assert sse_engine._card_ingest_time_label(NOW - 60) in visible
    assert "阅读优先级 8.8/10" in visible
    assert "看多" not in visible and "统一强信号" not in visible
    assert "看多" in details and "统一强信号 ≥8.5" in details
    assert "主题背景／规则池（非本文点名、不代表受益）" in details
    assert all("open" not in attrs for attrs in re.findall(r"<details([^>]*)>", html))
    assert "规则池示例：" not in visible
    assert "发布 " not in html


@pytest.mark.parametrize("reading", [False, True])
def test_reading_and_compact_share_unmapped_rule_pool_and_direction_details(reading):
    html = sse_engine._render_cards([_entry("普通公司发布例行公告")], reading=reading)

    assert _UNMAPPED_TITLE_NOTE in html
    assert "主题背景／规则池" not in html
    assert "等待更多来源证据" not in html
    assert "观察成交放大和价格趋势是否延续" not in html
    if reading:
        assert "方向与入选依据" in html
        assert "看多" in html.split("<details", 1)[1]
    else:
        assert "方向与入选依据" not in html


@pytest.mark.parametrize("link", ["javascript:alert(1)", "data:text/html,test", "//evil.example/x"])
def test_full_titles_and_sources_are_escaped_and_script_urls_rejected(link):
    title = '<script>attack()</script><em>完整新闻</em> 研发投入<5% & "计划" ' + LONG_TITLE
    html = sse_engine._render_cards([_entry(title, link=link, source='<svg/onload=attack()>')], reading=True)

    assert '<script>' not in html and '<svg' not in html and '<em>' not in html
    assert 'attack()</script>' not in html
    assert '研发投入&lt;5% &amp; &quot;计划&quot;' in html
    assert '&lt;svg/onload=attack()&gt;' in html
    assert LONG_TITLE in html
    assert 'href="#"' in html
    assert link not in html
    assert 'rel="noopener noreferrer"' in html


def test_home_route_preserves_dedupe_counts_time_windows_and_full_titles(monkeypatch):
    entries = [
        _entry(),
        _entry(score=8.0, importance_score=8.0, time=NOW - 10,
               link="https://news.example/fresh-copy", source="另一来源", feed_url="https://feed.example/b"),
        _entry("回看新闻", score=7.2, importance_score=7.2, time=NOW - 13 * 3600,
               link="https://news.example/fallback", source="回看来源", feed_url="https://feed.example/c"),
        _entry("超过时间窗", time=NOW - 48 * 3600 - 1),
    ]
    before = deepcopy(entries)
    client = _client(monkeypatch, entries)
    html = client.get("/glance-sse").get_data(as_text=True)

    assert entries == before
    assert re.findall(r'<a href="([^"]+)"', html) == [
        "https://news.example/fresh-copy", "https://news.example/fallback",
    ]
    assert re.findall(r"阅读优先级 ([\d.]+)/10", html) == ["8.8", "7.2"]
    assert LONG_TITLE in html
    assert html.count("<article") == 2
    assert "近12h 1条 · 48h回看 1条 · 2个订阅源" in html
    assert "合并2条" in html and "超过时间窗" not in html
    assert "max-height:620px" not in html and "overflow-y:auto" not in html
    assert "scrollbar" not in html
    assert "阅读优先级，不代表买入强度" in html
    assert "视图缓存30s" in html and "30s刷新" not in html


def test_empty_home_route_keeps_existing_empty_condition_without_inner_scroll(monkeypatch):
    html = _client(monkeypatch, []).get("/glance-sse").get_data(as_text=True)
    assert "近48小时暂无达到当前来源门槛的重点情报" in html
    assert "<article" not in html
    assert "overflow-y" not in html and "max-height" not in html


def test_legacy_database_full_title_sidecar_does_not_change_selection_title(tmp_path, monkeypatch):
    path = tmp_path / "legacy-news.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE news_cache (link TEXT, title TEXT, score REAL, tag TEXT, "
                     "logic TEXT, created_at INTEGER, feed_url TEXT, direction TEXT)")
        conn.execute("INSERT INTO news_cache VALUES (?, ?, 8.8, '', '', ?, ?, '中性')",
                     ("https://news.example/legacy", LONG_TITLE, NOW, "https://feed.example/a"))
    import db.database as database

    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.setattr(sse_engine.time, "time", lambda: NOW)
    cfg = {"threshold": {"pass": 7}, "feeds": [{"name": "测试来源", "url": "https://feed.example/a"}]}
    entries = sse_engine._load_from_db(cfg=cfg)

    assert len(entries) == 1
    assert entries[0]["title"] == sse_engine._preferred_card_title("", LONG_TITLE)
    assert entries[0]["raw_title"] == ""
    assert escape_html(LONG_TITLE) in sse_engine._render_cards(entries, reading=True)


def test_legacy_cross_source_title_follows_scoring_owner_not_freshest_link(monkeypatch):
    prefix = "企业发布投资计划并详细说明项目的建设和实施范围，" * 8
    low_title, high_title = prefix + "原先方案。", prefix + "更新方案。"
    entries = [
        _entry(low_title, raw_title="", score=7.5, importance_score=7.5, event_id="",
               time=NOW - 10, link="https://news.example/fresh-copy"),
        _entry(high_title, raw_title="", score=8.8, importance_score=8.8, event_id="",
               time=NOW - 60, source="另一来源", feed_url="https://feed.example/b"),
    ]
    html = _client(monkeypatch, entries).get("/glance-sse").get_data(as_text=True)

    assert html.count("<article") == 1
    assert high_title in html and low_title not in html
    assert 'href="https://news.example/fresh-copy"' in html
    assert "阅读优先级 7.5/10" in html  # Existing independent importance_score behavior.


@pytest.mark.parametrize("raw_title", ["", "明确的原始标题"])
def test_ambiguous_full_title_owner_falls_back_to_selected_source_text(raw_title):
    selected = _entry("已有短标题…", raw_title=raw_title, event_id="", time=NOW)
    candidates = [
        {**selected, "_reading_title": "已有短标题的甲版延续", "link": "https://news.example/a"},
        {**selected, "_reading_title": "已有短标题的乙版延续", "link": "https://news.example/b"},
    ]
    before = deepcopy((selected, candidates))
    restored = restore_reading_titles([selected], candidates)

    assert (selected, candidates) == before
    assert restored[0]["_reading_title"] == (raw_title or "已有短标题…")
    assert {key: value for key, value in restored[0].items() if key != "_reading_title"} == selected


@pytest.mark.parametrize("timestamp", ["broken", float("inf"), None])
def test_bad_owner_timestamps_do_not_break_presentation(timestamp):
    selected = _entry("既有标题", raw_title="原始标题", created_at=timestamp)
    candidates = [{**selected, "_reading_title": "原始标题"}]
    assert restore_reading_titles([selected], candidates)[0]["_reading_title"] == "原始标题"


def test_unresolved_event_ids_do_not_borrow_full_titles_from_other_links():
    selected = _entry("既有标题", raw_title="")
    candidates = [{**selected, "event_id": "missing-event", "link": "https://news.example/other",
                   "_reading_title": "另一条新闻的完整标题"}]
    assert restore_reading_titles([selected], candidates)[0]["_reading_title"] == "既有标题"


def test_reading_keeps_trusted_mapping_and_hides_polluted_model_fields():
    title = "创新药行业发布临床进展与授权出海交易情况"
    entry = _entry(title, tag="AI算力", logic="服务器需求扩张", stocks="中际旭创",
                   assets="成长风格、纳指映射", score_reason="GPU需求增长")
    before = deepcopy(entry)
    html = sse_engine._render_cards([entry], reading=True)

    assert entry == before
    assert title in html and "标题匹配主题：创新药" in html
    assert "AI算力" not in html and "中际旭创" not in html and "GPU需求增长" not in html
    assert "服务器需求扩张" not in html
