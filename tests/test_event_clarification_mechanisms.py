"""Building a clarification mechanism is not a denial of a specific event."""

import pytest
import test_event_daily_report as daily_helpers

from services import event_daily_report as daily
from services import event_home_digest as home
from services import event_weekly_report as weekly
from services.event_daily_report import _clause_correction_kind, _headline_correction_kind
from test_event_daily_report import _block, _report_event, _ts

database = daily_helpers.database
client = daily_helpers.client


def _view(kind):
    if kind == "daily":
        return daily.load_daily_report("2026-10-05", now=_ts(5, 12))
    if kind == "weekly":
        return weekly.load_weekly_report("2026-10-05", now=_ts(5, 12))
    return home.load_home_event_digest(now=_ts(5, 12))


def _shown(view, kind):
    if kind == "home":
        return view["highlights"] + view["corrections"]
    return [item for block in view["blocks"] for item in block["entries"]]


@pytest.mark.parametrize(("title", "expected"), [
    ("甲公司公布业绩报告", ""),
    ("甲公司称并购传闻不实", "denial"),
    ("网传甲公司收购乙公司不实", "denial"),
    ("交易所要求甲公司澄清有关不实信息", "request"),
    ("甲公司公布经营业绩营业收入保持稳定研发投入增加" * 50, ""),
])
def test_unrelated_titles_skip_mechanism_substitution(monkeypatch, title, expected):
    class ForbiddenSubstitution:
        def sub(self, *_args, **_kwargs):
            pytest.fail("ordinary headlines must not scan the mechanism regex")

    monkeypatch.setattr(daily, "_CLARIFICATION_MECHANISM_RE", ForbiddenSubstitution())
    monkeypatch.setattr(daily, "_PLANNED_MECHANISM_ACTION_RE", ForbiddenSubstitution())
    assert daily._clause_correction_kind(title) == expected


@pytest.mark.parametrize(("noun", "action"), [
    ("机制", "澄清"), ("制度", "辟谣"), ("流程", "更正"),
])
def test_each_mechanism_noun_runs_masking_without_erasing_actual_denials(
    monkeypatch, noun, action,
):
    original = daily._CLARIFICATION_MECHANISM_RE
    calls = []

    class CountedSubstitution:
        def sub(self, replacement, text):
            calls.append(text)
            return original.sub(replacement, text)

    monkeypatch.setattr(daily, "_CLARIFICATION_MECHANISM_RE", CountedSubstitution())
    title = f"协会拟建立不实信息{action}{noun}"
    denial = title + "并称收购传闻不实"
    assert daily._clause_correction_kind(title) == ""
    assert daily._clause_correction_kind(denial) == "denial"
    assert calls == [title, denial]


@pytest.mark.parametrize("kind", ["daily", "weekly", "home"])
@pytest.mark.parametrize("priority", [5.0, 6.0])
@pytest.mark.parametrize(("title", "expected_block"), [
    ("中证协拟健全不实信息快速澄清机制", "pending"),
    ("协会计划建立针对失实信息的快速辟谣机制", "pending"),
    ("协会拟完善不实信息澄清制度", "pending"),
    ("协会拟健全辟谣机制", "pending"),
    ("协会计划完善更正流程", "pending"),
    ("甲公司拟予以澄清机制调整传闻", "pending"),
    ("甲公司计划予以澄清制度改革传闻", "pending"),
    ("协会拟发布澄清机制建设方案", "pending"),
    ("协会计划发布辟谣制度建设方案", "pending"),
    ("甲公司计划发布澄清机制调整传闻的声明", "pending"),
    ("甲公司计划予以澄清流程调整传闻", "pending"),
    ("协会公布不实信息快速澄清机制建设方案", "new"),
    ("协会建立不实信息快速澄清机制", "new"),
])
def test_mechanism_news_keeps_ordinary_classification_and_score_gate(
    database, kind, priority, title, expected_block,
):
    event_id = _report_event(database, "mechanism", title, "https://news.example/mechanism",
                             at=_ts(5, 11), priority=priority)
    view = _view(kind)
    shown = _shown(view, kind)
    if kind == "home":
        assert view["corrections"] == []
    else:
        assert _block(view, "correction")["total"] == 0
        assert _block(view, expected_block)["total"] == 1
    if priority < daily.DISPLAY_MIN_PRIORITY:
        assert shown == []
        if kind != "home":
            assert _block(view, expected_block)["hidden_low_priority"] == 1
    else:
        assert [item["event_id"] for item in shown] == [event_id]
        assert shown[0]["title"] == title
        assert shown[0]["block"] == expected_block
        assert shown[0]["correction_kind"] == ""
        assert "出现否认/更正" not in shown[0]["change"]


@pytest.mark.parametrize("kind", ["daily", "weekly", "home"])
@pytest.mark.parametrize("title", [
    "甲公司称并购传闻不实",
    "网传甲公司收购乙公司不实",
    "甲公司发布澄清公告：未参与该项目",
    "甲公司拟建立不实信息快速澄清机制，并否认并购传闻",
    "甲公司拟建立不实信息快速澄清机制并否认并购传闻",
    "甲公司否认并购传闻并拟建立不实信息快速澄清机制",
    "甲公司：拟建立辟谣机制；近日网传签订订单消息不属实",
    "甲公司：并购传闻不实，拟完善信息澄清机制",
    "甲公司否认已建立不实信息澄清机制",
    "甲公司更正公告：不实信息快速澄清机制尚未建立",
    "甲公司：关于建立澄清机制的传闻不实",
    "甲公司拟建立澄清机制并称此前收购消息不实",
    "甲公司发布声明予以澄清机制调整传闻",
    "甲公司予以澄清制度改革传闻",
    "甲公司发布澄清机制调整传闻的公告",
    "甲公司辟谣机制改革传闻",
    "甲公司更正流程信息中的错误",
    "甲公司拟予以澄清机制调整传闻并否认并购传闻",
    "甲公司否认并购传闻并拟予以澄清机制调整传闻",
    "协会拟发布澄清机制建设方案并称并购消息不实",
    "甲公司已按计划发布澄清机制调整传闻的声明",
    "甲公司按计划予以澄清制度改革传闻",
    "甲公司已按照计划发布澄清机制调整传闻的声明",
    "甲公司依照计划予以澄清流程调整传闻",
    "甲公司按原计划发布澄清机制调整传闻的声明",
    "甲公司按照既定计划予以澄清制度改革传闻",
    "甲公司依照原计划发布澄清机制调整传闻的声明",
    "甲公司按计划发布澄清机制调整传闻的声明后收到交易所问询函",
    "甲公司已按照计划予以澄清制度改革传闻后收到交易所问询函",
    "甲公司依照计划发布澄清机制调整传闻的声明后收到交易所问询函",
])
def test_real_denial_survives_mechanism_context_and_low_score(database, kind, title):
    event_id = _report_event(database, "denial", title, "https://news.example/denial",
                             at=_ts(5, 11), priority=1.0)
    shown = _shown(_view(kind), kind)
    assert [item["event_id"] for item in shown] == [event_id]
    assert shown[0]["title"] == title
    assert shown[0]["block"] == "correction"
    assert shown[0]["correction_kind"] == "denial"
    assert "出现否认/更正" in shown[0]["change"]


@pytest.mark.parametrize("kind", ["daily", "weekly", "home"])
@pytest.mark.parametrize("title", [
    "协会拟建立澄清机制后要求甲公司澄清相关传闻",
    "协会要求甲公司澄清相关传闻并拟建立不实信息澄清机制",
    "交易所要求甲公司澄清有关不实信息",
    "交易所要求甲公司拟予以澄清机制调整传闻",
    "甲公司拟予以澄清机制调整传闻后收到交易所问询函",
    "协会拟发布澄清机制建设方案后要求甲公司澄清相关传闻",
    "甲公司计划发布澄清机制调整传闻的声明后收到交易所问询函",
    "交易所要求甲公司按计划予以澄清制度改革传闻",
])
def test_mechanism_phrase_does_not_turn_a_request_into_a_company_denial(
    database, kind, title,
):
    event_id = _report_event(database, "request", title, "https://news.example/request",
                             at=_ts(5, 11), priority=6.0)
    shown = _shown(_view(kind), kind)
    assert [item["event_id"] for item in shown] == [event_id]
    assert shown[0]["block"] == "correction"
    assert shown[0]["correction_kind"] == "request"
    assert "不是当事方的否认或更正" in shown[0]["change"]


@pytest.mark.parametrize("kind", ["daily", "weekly", "home"])
def test_recorded_contradiction_is_not_erased_by_lexical_mechanism_rule(database, kind):
    event_id = _report_event(
        database, "recorded", "中证协拟健全不实信息快速澄清机制",
        "https://news.example/recorded", at=_ts(5, 11), priority=1.0,
        stance="contradicts",
    )
    shown = _shown(_view(kind), kind)
    assert [item["event_id"] for item in shown] == [event_id]
    assert shown[0]["correction_kind"] == "denial"


def test_low_score_mechanism_is_absent_from_real_daily_weekly_and_home_pages(
    database, client, monkeypatch,
):
    title = "中证协拟健全不实信息快速澄清机制"
    _report_event(database, "mechanism-page", title, "https://news.example/mechanism-page",
                  at=_ts(5, 11), priority=5.0)
    monkeypatch.setattr(daily.time, "time", lambda: _ts(5, 12))
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    for url in (
        "/dashboard/events/daily?date=2026-10-05",
        "/dashboard/events/weekly?week=2026-10-05",
        "/event-radar-card?limit=8",
    ):
        response = client.get(url)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert title not in body
        assert "出现否认/更正：" not in body


@pytest.mark.parametrize("title", [
    "中证协拟健全不实信息快速澄清机制",
    "协会计划建立针对失实信息的快速辟谣机制",
    "协会拟完善不实信息澄清制度",
    "协会建立不实信息快速澄清机制",
    "协会拟发布澄清机制建设方案",
])
def test_mechanism_construction_is_not_a_denial(title):
    assert _headline_correction_kind(title, "supports") == ""
    assert _clause_correction_kind(title) == ""


@pytest.mark.parametrize("title", [
    "甲公司发布澄清公告：未参与该项目",
    "甲公司对并购传闻予以澄清",
    "甲公司澄清称并购传闻不实",
    "甲公司发布声明，针对订单传闻予以澄清",
])
def test_event_specific_clarification_remains_a_denial(title):
    assert _headline_correction_kind(title, "supports") == "denial"


@pytest.mark.parametrize("title", [
    "甲公司拟建立不实信息快速澄清机制，并否认并购传闻",
    "甲公司否认并购传闻并拟建立不实信息快速澄清机制",
    "甲公司：关于建立澄清机制的传闻不实",
    "甲公司发布声明予以澄清机制调整传闻",
])
def test_mechanism_wording_does_not_hide_an_actual_denial(title):
    assert _headline_correction_kind(title, "supports") == "denial"


def test_recorded_contradiction_survives_mechanism_wording():
    title = "中证协拟健全不实信息快速澄清机制"
    assert _headline_correction_kind(title, "contradicts") == "denial"


def test_mechanism_plan_does_not_turn_a_request_into_a_denial():
    title = "协会拟建立澄清机制后要求甲公司澄清相关传闻"
    assert _headline_correction_kind(title, "supports") == "request"


def test_known_mechanism_headline_is_not_a_correction_block(database):
    from test_event_daily_report import _block, _report_event, _ts
    from services.event_daily_report import load_daily_report

    title = "中证协拟健全不实信息快速澄清机制"
    _report_event(database, "mechanism", title, "https://news.example/mechanism",
                  at=_ts(5, 11), priority=6.5)
    report = load_daily_report("2026-10-05", now=_ts(5, 12))
    assert _block(report, "correction")["total"] == 0
    item = _block(report, "pending")["entries"][0]
    assert item["title"] == title and item["correction_kind"] == ""
