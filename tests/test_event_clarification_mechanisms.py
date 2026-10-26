"""Building a clarification mechanism is not a denial of a specific event."""

import pytest
import test_event_daily_report as daily_helpers

from services.event_daily_report import _clause_correction_kind, _headline_correction_kind

database = daily_helpers.database


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
