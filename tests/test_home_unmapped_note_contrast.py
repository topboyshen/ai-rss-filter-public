"""Keep the unmapped note readable on the institutional lead card only."""

from copy import deepcopy
import re

import pytest

import sse_engine
from services.news_mentions import TITLE_MENTIONS_READING_STYLE
from test_home_news_reading import _client, _entry


@pytest.mark.parametrize("reading", [False, True])
def test_unmapped_note_preserves_copy_default_style_and_card_state(reading):
    entry = _entry("普通公司发布例行公告")
    before = deepcopy(entry)
    html = sse_engine._render_one_card(entry, reading=reading)

    assert entry == before
    assert html.count('class="home-news-unmapped-note"') == 1
    assert html.count(sse_engine._UNMAPPED_TITLE_NOTE) == 1
    assert 'color:var(--color-text-subdue,inherit);font-size:11px;line-height:1.5;' in html
    assert "主题背景／规则池" not in html
    assert "<style>" not in html
    assert "#d0dbe1" not in html
    if reading:
        assert html.index('class="home-news-context"') < html.index('class="home-news-unmapped-note"')
        assert not re.search(r"<details[^>]*\bopen\b", html)


def test_reading_widget_emits_one_strictly_scoped_lead_note_rule(monkeypatch):
    import db.database as database

    monkeypatch.setattr(database, "get_direct_equity_mentions_batch", lambda titles: [
        {"raw_title": title, "status": "ok", "mentions": []} for title in titles
    ])
    entries = [_entry("普通公告一"), _entry("普通公告二", link="https://news.example/two")]
    html = _client(monkeypatch, entries).get("/glance-sse").get_data(as_text=True)
    rules = re.findall(r"<style>(.*?)</style>", html, re.S)

    assert rules == [
        "body:has(.ai-home-institutional-masthead) #sse-container "
        "> .home-news-card:nth-of-type(1) > .home-news-unmapped-note "
        "{color:#d0dbe1 !important;}" + TITLE_MENTIONS_READING_STYLE
    ]
    assert html.count('class="home-news-unmapped-note"') == 2
    # The style sits after the cards, not in the grid or before the first header.
    assert re.search(r'</article>\s*</div>\s*<style>', html)
    assert re.search(r'<div class="home-news-reading"[^>]*>\s*<div', html)


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("stocks", ["", "测试公司"])
def test_mapped_cards_keep_closed_rule_pool_without_unmapped_note(reading, stocks):
    html = sse_engine._render_one_card(_entry(
        "测试主题新闻", mapped_theme="创新药", mapping_confidence=0.8,
        assets="医药", sectors="创新药", stocks=stocks,
    ), reading=reading)

    assert 'class="home-news-unmapped-note"' not in html
    assert "主题背景／规则池（非本文点名、不代表受益）" in html
    assert (stocks or "未提供个股示例") in html
    assert not re.search(r"<details[^>]*\bopen\b", html)
