from collections import Counter

from sse_engine import _render_cards, _select_balanced_cards, _source_bucket


def _entry(source, title, score, ts):
    return {
        "title": title,
        "link": f"https://news.example/{source}/{title}",
        "score": score,
        "importance_score": score,
        "tag": "测试",
        "logic": "分层展示测试",
        "time": ts,
        "source": source,
        "feed_url": f"https://rss.example/{source}",
        "_selection_threshold": 6.2,
    }


def test_priority_tiers_keep_freshness_boundary_and_rank_strong_by_score():
    now = 2_000_000_000
    entries = [
        _entry("来源甲", "最新观察", 8.4, now - 10),
        _entry("来源乙", "较新强信号", 9.0, now - 100),
        _entry("来源丙", "更高分强信号", 9.5, now - 1_000),
        _entry("来源丁", "回看最高分", 9.8, now - 13 * 3600),
        _entry("来源戊", "回看观察", 8.0, now - 14 * 3600),
    ]

    selected, fresh_count, fallback_count = _select_balanced_cards(entries, now_ts=now)

    assert [entry["title"] for entry in selected] == [
        "更高分强信号",
        "较新强信号",
        "最新观察",
        "回看最高分",
        "回看观察",
    ]
    assert [entry["_realtime_section"] for entry in selected] == [
        "fresh_strong",
        "fresh_strong",
        "fresh_observe",
        "fallback_strong",
        "fallback_observe",
    ]
    assert (fresh_count, fallback_count) == (3, 2)


def test_strong_tier_still_uses_shared_publisher_cap():
    now = 2_000_000_000
    entries = [
        _entry("同一来源", "强信号一", 9.1, now - 10),
        _entry("同一来源", "强信号二", 9.5, now - 20),
        _entry("同一来源", "强信号三", 9.3, now - 30),
        _entry("其他来源", "观察项", 8.0, now - 5),
    ]

    selected, fresh_count, fallback_count = _select_balanced_cards(entries, now_ts=now)

    counts = Counter(_source_bucket(entry) for entry in selected)
    assert counts["同一来源"] == 2
    assert [entry["title"] for entry in selected[:2]] == ["强信号二", "强信号三"]
    assert (fresh_count, fallback_count) == (3, 0)


def test_time_boundaries_keep_exact_12_and_48_hour_entries():
    now = 2_000_000_000
    entries = [
        _entry("来源甲", "恰好十二小时", 8.5, now - 12 * 3600),
        _entry("来源乙", "超过十二小时一秒", 8.4, now - 12 * 3600 - 1),
        _entry("来源丙", "恰好四十八小时", 8.0, now - 48 * 3600),
        _entry("来源丁", "超过四十八小时一秒", 9.9, now - 48 * 3600 - 1),
    ]

    selected, fresh_count, fallback_count = _select_balanced_cards(
        entries,
        now_ts=now,
        max_per_source=None,
    )

    assert {entry["title"] for entry in selected} == {
        "恰好十二小时",
        "超过十二小时一秒",
        "恰好四十八小时",
    }
    assert next(
        entry for entry in selected if entry["title"] == "恰好十二小时"
    )["_realtime_section"] == "fresh_strong"
    assert all(
        entry["_realtime_section"].startswith("fallback_")
        for entry in selected
        if entry["title"] != "恰好十二小时"
    )
    assert (fresh_count, fallback_count) == (1, 2)


def test_publisher_cap_is_shared_across_all_priority_groups():
    now = 2_000_000_000
    entries = [
        _entry("同一来源", "新鲜强信号", 9.2, now - 10),
        _entry("同一来源", "新鲜观察", 8.0, now - 20),
        _entry("同一来源", "回看强信号", 9.8, now - 13 * 3600),
        _entry("同一来源", "回看观察", 7.0, now - 14 * 3600),
        _entry("其他来源", "其他回看观察", 7.0, now - 15 * 3600),
    ]

    selected, fresh_count, fallback_count = _select_balanced_cards(entries, now_ts=now)

    assert [
        entry["title"] for entry in selected if entry["source"] == "同一来源"
    ] == ["新鲜强信号", "新鲜观察"]
    assert "其他回看观察" in {entry["title"] for entry in selected}
    assert (fresh_count, fallback_count) == (2, 1)


def test_rendered_priority_groups_explain_why_each_card_was_selected():
    now = 2_000_000_000
    selected, _, _ = _select_balanced_cards(
        [
            _entry("来源甲", "政策强信号", 8.5, now - 60),
            _entry("来源乙", "行业观察项", 7.2, now - 30),
        ],
        now_ts=now,
    )

    html = _render_cards(selected)

    assert "近12小时 · 强信号" in html
    assert "近12小时 · 重点观察" in html
    assert "统一强信号 ≥8.5" in html
    assert "源门槛 ≥6.2" in html


def test_rendering_empty_cleaned_title_does_not_emit_a_group_header():
    html = _render_cards(
        [{
            **_entry("来源甲", "占位", 8.5, 2_000_000_000),
            "title": "&lt;span&gt;&lt;/span&gt;",
            "_realtime_section": "fresh_strong",
        }]
    )

    assert html == ""
