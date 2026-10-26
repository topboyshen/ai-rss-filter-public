"""Snapshot timestamps must not become positional Glance grid items."""
from html.parser import HTMLParser
import re

import pytest

from routes.dashboard_routes import _prepend_realtime_snapshot_time, _prepend_stale_banner
import sse_engine
from test_home_news_reading import _entry


def _without_clock(html):
    return re.sub(r'<span class="dashboard-snapshot-time"[^>]*>[^<]*</span>', "", html)


class _Structure(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.roots = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        node = {"tag": tag, "class": attrs.get("class", ""),
                "id": attrs.get("id", ""), "children": []}
        if self.stack:
            self.stack[-1]["children"].append(node)
        else:
            self.roots.append(node)
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break


def _identity(node):
    return node["tag"], node["class"], node["id"]


def _root_signature(html):
    roots = _Structure(html).roots
    return [(_identity(root), [_identity(child) for child in root["children"]]) for root in roots]


def _reading_shell(entries):
    return sse_engine._render_realtime_widget(
        sse_engine._render_cards(entries, reading=True), entries, len(entries), 0, None,
    )


@pytest.mark.parametrize("state", ["unmapped", "mapped", "empty"])
def test_clock_remains_inside_existing_window_and_preserves_positional_layout(state):
    if state == "empty":
        entries = []
    elif state == "mapped":
        entries = [_entry("创新药公司公布新药临床进展")]
    else:
        entries = [_entry("普通公司发布例行公告")]
    original = _reading_shell(entries)
    rendered = _prepend_realtime_snapshot_time(original, "2026-09-28T11:08:28Z")

    assert rendered.count('class="dashboard-snapshot-time"') == 1
    assert "09-28 19:08（北京时间；非新闻发布时间）" in rendered
    assert _without_clock(rendered) == original
    assert _root_signature(rendered) == _root_signature(original)
    roots = _Structure(rendered).roots
    assert len(roots) == 1 and roots[0]["class"] == "home-news-reading"
    assert [child["tag"] for child in roots[0]["children"]] == ["div", "div", "style"]
    assert roots[0]["children"][1]["id"] == "sse-container"
    header = roots[0]["children"][0]
    left = header["children"][0]
    assert len(left["children"]) == 2  # nth-child(2) remains original disclaimer
    assert left["children"][0]["class"] == "home-news-window"
    assert left["children"][0]["children"][0]["class"] == "dashboard-snapshot-time"
    assert not re.search(r'<details[^>]*\bopen\b', rendered)
    if state == "empty":
        assert "暂无达到当前来源门槛的重点情报" in rendered
        assert '<article' not in rendered
    elif state == "unmapped":
        assert sse_engine._UNMAPPED_TITLE_NOTE in rendered
        assert "主题背景／规则池" not in rendered
        assert "方向与入选依据" in rendered
    else:
        assert "主题背景／规则池（非本文点名、不代表受益）" in rendered
        assert "未提供个股示例" in rendered
        assert "方向与入选依据" in rendered


@pytest.mark.parametrize("html", [
    '<article>旧新闻<div>标题提及：识别暂不可用</div></article>',
    '<section>未知阅读样式</section>',
    '<div>previous news</div>',
    '<div class="home-news-reading"><article>旧版壳</article></div>',
    '<div>别的根</div><div class="home-news-window">不可向外注入</div>',
])
def test_legacy_or_unknown_markup_is_left_byte_for_byte_unchanged(html):
    assert _prepend_realtime_snapshot_time(html, "2026-09-28T11:08:28Z") == html


@pytest.mark.parametrize("stamp", ["", "not-a-time", "<script>alert(1)</script>",
                                   "2026-09-28T11:08:28"])
def test_unknown_clock_stays_in_existing_header_and_cannot_inject_html(stamp):
    original = _reading_shell([])
    rendered = _prepend_realtime_snapshot_time(original, stamp)
    assert "页面数据快照：时间未知" in rendered
    assert "<script>" not in rendered
    assert _without_clock(rendered) == original
    assert _root_signature(rendered) == _root_signature(original)


@pytest.mark.parametrize("reason", ["expired", "future"])
@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize("clock", [False, True])
def test_snapshot_warning_preserves_reading_roots_and_grid_children(reason, count, clock):
    original = _reading_shell([
        _entry(f"新闻 {i}：普通公司发布例行公告", link=f"https://news.example/{i}")
        for i in range(count)
    ])
    if clock:
        original = _prepend_realtime_snapshot_time(original, "2026-09-28T11:08:28Z")
    rendered = _prepend_stale_banner(
        original, "2026-09-28T11:08:28Z", -60 if reason == "future" else 50_000,
        reason=reason,
    )
    assert _root_signature(rendered) == _root_signature(original)
    warning = re.search(
        r'<div class="dashboard-snapshot-warning"[^>]*>[^<]*</div>', rendered,
    ).group()
    assert rendered.replace(warning, "") == original
    assert ("快照时间异常" if reason == "future" else "数据已过期") in warning
    assert "当前数据状态未知" in warning and 'role="status"' in warning
    roots = _Structure(rendered).roots
    left = roots[0]["children"][0]["children"][0]
    assert len(left["children"]) == 2  # Preserve the original nth-child disclaimer.
    assert left["children"][0]["children"][0]["class"] == "dashboard-snapshot-warning"
    assert rendered.count('class="dashboard-snapshot-time"') == int(clock)


@pytest.mark.parametrize("html", ["<section>统计卡</section>", "<div>旧版新闻壳</div>"])
def test_other_or_legacy_snapshots_keep_visible_warning_and_body(html):
    rendered = _prepend_stale_banner(html, "bad-time", 50_000)
    assert rendered.endswith(html)
    assert "数据已过期：快照生成于 时间未知" in rendered


@pytest.mark.parametrize("reason", ["fresh", "expired", "future"])
def test_real_realtime_snapshot_response_preserves_home_shell(tmp_path, monkeypatch, reason):
    import hashlib
    import json
    from datetime import datetime, timedelta, timezone
    from flask import Flask
    from services import dashboard_snapshots as snapshots
    from routes import security

    directory = tmp_path / "snapshots"
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(directory))
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:layout-test")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_MAX_AGE_SECONDS", "3600")
    now = datetime.now(timezone.utc)
    original = _reading_shell([_entry("普通公司发布例行公告")])
    generated = (now - timedelta(days=2 if reason == "expired" else 0, minutes=5)).isoformat()
    snapshots.write_dashboard_snapshot(
        "realtime", original, generated_at=generated,
        strategy_version="score:layout", mapping_version="mapping:layout",
        data_version="1", snapshot_dir=directory, db_path=tmp_path / "cache.db",
    )
    path = directory / "realtime.json"
    if reason == "future":
        # Genuine reader path with a valid body digest but untrustworthy times.
        envelope = json.loads(path.read_text())
        envelope["generated_at"] = (now + timedelta(hours=2)).isoformat()
        envelope["published_at"] = (now + timedelta(hours=3)).isoformat()
        assert envelope["html_sha256"] == hashlib.sha256(original.encode()).hexdigest()
        path.write_text(json.dumps(envelope))
    stored = path.read_bytes()
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    app = Flask(__name__)
    app.add_url_rule("/glance-sse", view_func=sse_engine.glance_sse_widget)
    security._rate_limit_store.clear()
    try:
        response = app.test_client().get("/glance-sse")
    finally:
        security._rate_limit_store.clear()
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == ("ready" if reason == "fresh" else "stale")
    assert _root_signature(body) == _root_signature(original)
    assert body.count('class="dashboard-snapshot-warning"') == int(reason != "fresh")
    assert body.count('class="dashboard-snapshot-time"') == 1
    assert original[original.index('<div id="sse-container"'):] in body
    assert path.read_bytes() == stored  # Read-only even for an anomalous artifact.
