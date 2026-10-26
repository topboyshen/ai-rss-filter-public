"""Real Feed response contract; browser widths are checked by the companion QA."""

from html.parser import HTMLParser
from unittest.mock import patch

from flask import Flask

from routes.ops_feed_layout import OPS_FEED_LAYOUT_STYLE


class _Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.classes = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.classes.append((tag, dict(attrs).get("class", "")))


def render_ops_feed_fixture(empty=False):
    """Render production markup with deterministic data, without DB/network access."""
    from routes import dashboard_routes as routes

    url = "https://example.test/feed"
    rows = [] if empty else [
        (url, "较长的来源名称：财联社重要公告与新闻动态", 1_790_942_400,
         120, 1234, 0, 12345, 789, 7.4, None, 0, 1_790_942_400),
    ]
    details = {url: {
        "rate_text": "123 / 12345", "threshold": 7,
        "breakdown_text": "正式达标 123 · 保守中性 456 · 重复稿 789",
        "zero_output_reason": "",
    }}
    with (
        patch.object(routes, "_cfg", return_value={}),
        patch.object(routes, "_ops_cached_value", return_value=(rows, details)),
        patch.object(routes.time, "time", return_value=1_790_942_440),
        Flask(__name__).test_request_context("/ops-feeds"),
    ):
        response = routes.ops_feeds_widget()
        assert response.status_code == 200
        return response.get_data(as_text=True)


def test_feed_response_carries_its_component_layout_without_a_glance_upgrade():
    body = render_ops_feed_fixture()
    elements = _Elements(body)
    assert body.count(OPS_FEED_LAYOUT_STYLE) == 1
    assert ("div", "ops-feeds") in elements.classes
    assert elements.classes.count(("div", "ops-feed-row")) == 1
    assert "较长的来源名称" in body
    assert "123 / 12345" in body
    assert "正式达标 123 · 保守中性 456 · 重复稿 789" in body


def test_feed_reflow_tracks_component_width_without_clipping_or_hiding_data():
    assert "container: ops-feeds / inline-size" in OPS_FEED_LAYOUT_STYLE
    assert "@container ops-feeds (max-width: 560px)" in OPS_FEED_LAYOUT_STYLE
    assert "@media" not in OPS_FEED_LAYOUT_STYLE
    assert "repeat(3, minmax(0, 1fr))" in OPS_FEED_LAYOUT_STYLE
    assert "overflow-wrap: anywhere" in OPS_FEED_LAYOUT_STYLE
    assert "white-space: normal !important" in OPS_FEED_LAYOUT_STYLE
    for unsafe in ("overflow: hidden", "display: none", "order:", "overflow-x:"):
        assert unsafe not in OPS_FEED_LAYOUT_STYLE


def test_empty_feed_still_has_one_component_and_the_existing_empty_message():
    body = render_ops_feed_fixture(empty=True)
    assert body.count('class="ops-feeds"') == 1
    assert 'class="ops-feed-row"' not in body
    assert "暂无 Feed 统计数据" in body
