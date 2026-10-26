from copy import deepcopy

from services import briefing_reading


def _theme(**overrides):
    theme = {
        "theme": "非洲电动",
        "theme_display_name": "非洲电动",
        "direction": "中性",
        "importance": 8.0,
        "source_confirmation": "头部事件覆盖 2 个入选发布方；转载谱系未核验；主题整体覆盖 3 个入选发布方",
        "articles": [{
            "title": "代表情报标题一",
            "link": "https://example.com/report",
            "source_name": "既有来源",
            "time": 1789812,
        }],
    }
    theme.update(overrides)
    return theme


def _result(themes):
    return {
        "overall_assessment": "既有总体观察",
        "market_impact": "既有市场影响",
        "data_as_of": "2026-09-19T15:00:00+08:00",
        "generated_at": "2026-09-19T15:05:00+08:00",
        "source": "llm",
        "briefing_assembly": {"enabled": True, "input_count": 12, "selected_count": 8},
        "themes": themes,
    }


def _one(theme):
    return briefing_reading.render_briefing_reading_html(_result([theme]), 24)


def test_representative_news_is_the_main_information():
    html = _one(_theme())
    assert "briefing-reading-news-title" in html
    assert "代表情报标题一" in html
    assert 'href="https://example.com/report"' in html
    assert "briefing-reading-theme-tag" in html
    assert "方向：中性 · 主题重要度 8.0/10" in html
    assert "收录 " in html and "收录时间未记录" not in html


def test_missing_article_shows_placeholder_without_link():
    html = _one(_theme(articles=[]))
    assert "暂无代表情报" in html
    assert '<a class="briefing-reading-news-title"' not in html


def test_unsafe_or_missing_link_stays_plain_text():
    unsafe = _one(_theme(articles=[{"title": "标题", "link": "javascript:alert(1)"}]))
    assert "标题" in unsafe
    assert '<a class="briefing-reading-news-title"' not in unsafe
    missing = _one(_theme(articles=[{"title": "标题二"}]))
    assert "标题二" in missing
    assert "收录时间未记录" in missing


def test_counter_is_dynamic_and_full_entry_is_native_details():
    html = briefing_reading.render_briefing_reading_html(
        _result([deepcopy(_theme()) for _ in range(5)]), 24)
    assert "展示前 3 个，共 5 个主题" in html
    assert "查看全部 5 个主题" in html
    assert html.count("<details") >= 4
    two = briefing_reading.render_briefing_reading_html(
        _result([deepcopy(_theme()) for _ in range(2)]), 24)
    assert "共 2 个主题" in two


def test_coverage_keeps_two_scopes_and_unknown():
    html = _one(_theme())
    assert "转载关系未核验" in html
    assert "头部事件覆盖 2 个入选发布方" in html and "主题整体覆盖 3 个入选发布方" in html
    assert "来源覆盖信息未记录" in _one(_theme(source_confirmation=""))


def test_style_classes_resist_old_important_rule():
    html = _one(_theme())
    assert ".briefing-reading-news-title{font-size:17px !important" in html
    assert ".briefing-reading-theme-tag{font-size:12px !important" in html


def test_real_time_fields_prefer_created_at_and_keep_display_strings():
    from core.digest_engine import _briefing_source_label

    epoch = {"title": "标题", "link": "https://example.com/a", "created_at": 1789812000}
    html = _one(_theme(articles=[epoch]))
    assert "收录时间未记录" not in html and "收录 " in html
    label = {"title": "标题", "link": "https://example.com/a", "time": "09-21 07:30"}
    assert "收录 09-21 07:30" in _one(_theme(articles=[label]))
    invalid = {"title": "标题", "link": "https://example.com/a", "time": "not-a-time"}
    assert "收录时间未记录" in _one(_theme(articles=[invalid]))

    feed = {"title": "标题", "link": "https://example.com/b",
            "feed_url": "http://rsshub:1200/yicai/latest?limit=8"}
    expected = _briefing_source_label(feed)
    assert expected and expected in _one(_theme(articles=[feed]))


def test_incomplete_or_unsafe_links_never_render_an_anchor():
    for bad in ("https://", "http://", "javascript:alert(1)", "", "ftp://x/y"):
        html = _one(_theme(articles=[{"title": "标题", "link": bad}]))
        assert '<a class="briefing-reading-news-title"' not in html, bad


def test_font_sizes_are_inline_important_to_beat_old_selectors():
    html = _one(_theme())
    assert 'style="font-size:17px !important' in html
    assert 'style="font-size:12px !important' in html
