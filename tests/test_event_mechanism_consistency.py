"""Mechanism wording keeps the same evidence semantics on three real read paths."""

import pytest
import test_event_daily_report as daily_helpers

from services import event_daily_report as daily
from services import event_home_digest as home
from services import event_weekly_report as weekly
from test_event_daily_report import _evidence, _report_event, _ts

database = daily_helpers.database


def _views(event_id, *, now):
    reports = (
        daily.load_daily_report("2026-10-05", now=now),
        weekly.load_weekly_report("2026-10-05", now=now),
    )
    items = [
        [item for block in report["blocks"] for item in block["entries"]
         if item["event_id"] == event_id]
        for report in reports
    ]
    digest = home.load_home_event_digest(now=now)
    items.append([
        item for item in digest["highlights"] + digest["corrections"]
        if item["event_id"] == event_id
    ])
    assert all(len(matches) == 1 for matches in items)
    return [matches[0] for matches in items]


@pytest.mark.parametrize(("title", "block", "kind"), [
    ("中证协拟健全不实信息快速澄清机制", "pending", ""),
    ("协会计划建立针对失实信息的快速辟谣机制", "pending", ""),
    ("协会建立澄清机制", "new", ""),
    ("协会建立不实信息快速澄清机制", "new", ""),
    ("协会发布澄清机制建设方案", "new", ""),
    ("协会发布辟谣制度完善方案", "new", ""),
    ("协会拟发布澄清机制建设方案", "pending", ""),
    ("甲公司针对并购传闻发布澄清公告", "correction", "denial"),
    ("甲公司对并购传闻予以澄清", "correction", "denial"),
    ("甲公司澄清称并购传闻不实", "correction", "denial"),
    ("甲公司发布声明予以澄清机制调整传闻", "correction", "denial"),
    ("甲公司按计划发布澄清机制调整传闻", "correction", "denial"),
    ("甲公司拟建立不实信息快速澄清机制并否认并购传闻", "correction", "denial"),
    ("甲公司否认并购传闻并拟建立不实信息快速澄清机制", "correction", "denial"),
    ("甲公司建立澄清机制后澄清称订单传闻不实", "correction", "denial"),
    ("甲公司关于建立澄清机制的传闻不实", "correction", "denial"),
    ("甲公司发布澄清机制建设方案并否认收购传闻", "correction", "denial"),
    ("甲公司否认收购传闻并发布澄清机制建设方案", "correction", "denial"),
    ("甲公司发布澄清机制建设方案，澄清称收购传闻不实", "correction", "denial"),
    ("甲公司发布澄清称机制建设方案被叫停的传闻不实", "correction", "denial"),
    ("甲公司对发布澄清机制建设方案被叫停的传闻予以澄清", "correction", "denial"),
    ("协会建立不实信息澄清机制并要求甲公司澄清收购传闻", "correction", "request"),
    ("协会拟建立澄清机制后要求甲公司澄清相关传闻", "correction", "request"),
    ("交易所要求甲公司澄清建立澄清机制的传闻", "correction", "request"),
    ("甲公司发布澄清机制建设方案后交易所要求其澄清传闻", "correction", "request"),
    ("甲公司发布澄清公告否认传闻后交易所要求其澄清机制建设情况", "correction", "denial"),
    ("甲公司予以澄清机制建设方案并未取消", "correction", "denial"),
    ("甲公司拟予以澄清机制建设方案", "pending", ""),
])
def test_same_evidence_has_same_classification_on_daily_weekly_home(database, title, block, kind):
    url = "https://news.example/mechanism"
    event_id = _report_event(database, "mechanism", title, url, at=_ts(5, 10), priority=7.5)
    for item in _views(event_id, now=_ts(5, 12)):
        assert (item["block"], item["correction_kind"]) == (block, kind)
        assert item["title"] == title
        assert item["sources"][0]["url"] == url
        assert item["sources"][0]["claim_text"] == title
        if kind == "request":
            assert "不是当事方的否认" in item["change"]
        elif kind == "denial":
            assert "出现否认/更正" in item["change"]


def test_recorded_contradiction_overrides_mechanism_on_all_read_paths(database):
    title = "中证协拟健全不实信息快速澄清机制"
    event_id = _report_event(
        database, "recorded", title, "https://news.example/recorded",
        at=_ts(5, 10), priority=1, stance="contradicts",
    )
    for item in _views(event_id, now=_ts(5, 12)):
        assert (item["block"], item["correction_kind"]) == ("correction", "denial")
        assert item["priority"] == 1


def test_same_headline_changed_stance_is_not_hidden_as_repost(database):
    title = "中证协拟健全不实信息快速澄清机制"
    event_id = _report_event(
        database, "changed", title, "https://news.example/support", at=_ts(5, 9),
    )
    _evidence(
        database, event_id, "https://news.example/denial", title,
        at=_ts(5, 11), stance="contradicts",
    )
    for item in _views(event_id, now=_ts(5, 12)):
        assert (item["block"], item["correction_kind"]) == ("correction", "denial")
        assert item["sources"][0]["url"] == "https://news.example/denial"
        assert "此前有 1 条支持记录" in item["change"]


@pytest.mark.parametrize("title", [
    "协会发布澄清机制建设方案",
    "中证协拟健全不实信息快速澄清机制",
])
def test_mechanism_mask_does_not_bypass_display_score_gate(database, title):
    event_id = _report_event(
        database, "low", title, "https://news.example/low", at=_ts(5, 10), priority=1,
    )
    now = _ts(5, 12)
    for report in (daily.load_daily_report("2026-10-05", now=now),
                   weekly.load_weekly_report("2026-10-05", now=now)):
        assert all(item["event_id"] != event_id
                   for block in report["blocks"] for item in block["entries"])
        assert next(block for block in report["blocks"] if block["key"] == "correction")["total"] == 0
    digest = home.load_home_event_digest(now=now)
    assert digest["highlights"] == digest["corrections"] == []


@pytest.mark.parametrize(("tail", "kind"), [
    ("但甲公司已否认收购", "denial"),
    ("然而甲公司已经明确否认收购", "denial"),
    ("随后甲公司已公开辟谣", "denial"),
    ("此后该公司已予以澄清", "denial"),
    ("但甲公司已发布澄清公告", "denial"),
    ("但甲公司已否认‘收购’传闻", "denial"),
    ("但甲公司已否认收购乙公司的说法", "denial"),
    ("但甲公司已否认收购乙公司的报道", "denial"),
    ("但甲公司已否认收购乙公司的说法是否属实", "request"),
    ("但甲公司拟否认收购", "request"),
    ("但甲公司尚未否认收购", "request"),
    ("但甲公司是否已否认收购", "request"),
    ("但是否甲公司已否认收购", "request"),
    ("但如果甲公司已否认收购则应提供依据", "request"),
    ("但并非甲公司已否认收购", "request"),
    ("但记者质疑甲公司已否认收购", "request"),
    ("但不代表甲公司已否认收购", "request"),
    ("但无法确认甲公司已否认收购", "request"),
    ("但有人猜测甲公司已否认收购", "request"),
    ("中的‘但甲公司已否认收购’是否属实", "request"),
    ("中的“但甲公司已否认收购”是否属实", "request"),
    ("中的「但甲公司已否认收购」是否属实", "request"),
    ("中的\"但甲公司已否认收购\"是否属实", "request"),
    ("中的“但甲公司已否认收购", "request"),
    ("但甲公司已否认收购的说法是否属实", "request"),
    ("但甲公司已否认收购的报道真实性", "request"),
    ("但甲公司已要求否认收购", "request"),
])
def test_post_request_response_must_be_actual_not_quoted_or_planned(database, tail, kind):
    title = "协会拟建立不实信息澄清机制并要求甲公司澄清相关传闻" + tail
    event_id = _report_event(
        database, "post-request", title, "https://news.example/post-request", at=_ts(5, 10),
    )
    for item in _views(event_id, now=_ts(5, 12)):
        assert (item["block"], item["correction_kind"]) == ("correction", kind)
