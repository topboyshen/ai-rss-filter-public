"""Daily-report outcomes for clarification cues adjacent to party names."""

import pytest
import test_event_daily_report as daily_helpers

from test_event_daily_report import (
    _block,
    _page,
    _report,
    _report_event,
    _section,
    _ts,
)

# Reuse the real database and authenticated Flask fixtures without rebuilding
# their setup or weakening the final-page assertions.
client = daily_helpers.client
database = daily_helpers.database


@pytest.mark.parametrize("title", [
    "新希望发布澄清公告：未与任何方接触重组",
    "若羽臣发布澄清公告：未参与该项目",
    "新希望：发布澄清声明（公司无上述合作事项）",
    "若羽臣：对市场传闻予以澄清",
    "新希望：澄清称未参与该项目",
])
def test_named_party_clarifications_survive_the_normal_score_gate(
    database, client, monkeypatch, title,
):
    # Corrections remain visible even below the ordinary priority threshold.
    _report_event(database, "actual", title, "https://n.example/actual",
                  at=_ts(5, 10), priority=5.0)

    report = _report(5, now=_ts(6, 1))
    item = _block(report, "correction")["entries"][0]
    assert item["title"] == title
    assert item["correction_kind"] == "denial"
    assert item["change"] == f"今日出现否认/更正：{title}"
    assert _block(report, "new")["total"] == 0

    section = _section(_page(client, monkeypatch, 5, _ts(6, 1)), "correction")
    assert title in section
    assert f"今日出现否认/更正：{title}" in section


@pytest.mark.parametrize("title", [
    "如果新希望发布澄清公告将有助于了解情况",
    "如果若羽臣发布澄清公告将有助于了解情况",
    "希望若羽臣予以澄清",
    "投资者：希望新希望发布澄清公告",
    "分析人士表示希望若羽臣发布澄清公告",
    "分析人士呼吁新希望发布澄清公告",
    "若能发布澄清公告将有助于了解情况",
    "意大利财政部长：如果欧洲央行行长拉加德能就其职务（去留）问题作出澄清，将是合适的。",
])
def test_clarification_appeals_remain_ordinary_news(
    database, client, monkeypatch, title,
):
    _report_event(database, "appeal", title, "https://n.example/appeal",
                  at=_ts(5, 10), priority=6.5)

    report = _report(5, now=_ts(6, 1))
    assert _block(report, "correction")["total"] == 0
    item = _block(report, "new")["entries"][0]
    assert item["title"] == title and item["correction_kind"] == ""

    html = _page(client, monkeypatch, 5, _ts(6, 1))
    assert title in _section(html, "new")
    assert "今日出现否认/更正" not in html
