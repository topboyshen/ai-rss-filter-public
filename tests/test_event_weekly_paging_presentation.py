"""Render weekly pagination without touching a database or starting a server."""

import re
from html import unescape
from pathlib import Path

import pytest
from flask import Flask, render_template


TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
OVERVIEW = "/dashboard/events/weekly?week=2026-09-28"
NOTICE = "每次翻页按当前已有记录重建，排序可能变化；这不是固定快照。"


@pytest.fixture
def render_report():
    app = Flask(__name__, template_folder=str(TEMPLATES))

    def render(report, template="event_radar_weekly.html"):
        with app.test_request_context():
            return render_template(template, report=report, auth=None, release_version=None)

    return render


def _entry(index, title=None):
    return {
        "event_id": f"event-{index}", "title": title or f"周报事件 {index}",
        "event_type": "政策", "priority_label": "8", "merged_group_count": 1,
        "report_count": 2, "change": "本周新增记录", "correction_kind": None,
        "previous": None, "earliest": None, "sources": [], "sources_more": 0,
        "uncertainties": ["等待后续披露"], "next_check": "查看正式公告",
        "companies": [], "rule_association_count": 0,
    }


def _block(page=1, eligible=45, *, key="new", label="新增事件"):
    start = (page - 1) * 20
    end = min(start + 20, eligible)
    page_count = max(1, (eligible + 19) // 20)
    return {
        "key": key, "label": label,
        "entries": [_entry(index) for index in range(start + 1, end + 1)],
        "total": eligible + 2, "hidden_low_priority": 2,
        "hidden_overflow": max(0, eligible - 20), "empty_label": "暂无可展示事件。",
        "pagination": {
            "page": page, "page_count": page_count, "eligible_total": eligible,
            "start_index": start + 1 if eligible else 0, "end_index": end,
            "previous_url": f"{OVERVIEW}&block={key}&page={page - 1}" if page > 1 else None,
            "next_url": f"{OVERVIEW}&block={key}&page={page + 1}" if page < page_count else None,
        },
    }


def _report(block, *, selected=False):
    return {
        "week": "2026-09-28", "start_date": "2026-09-28", "end_date": "2026-10-04",
        "min_week": "2025-10-06", "max_week": "2026-10-05",
        "prev_week": "2026-09-21", "next_week": "2026-10-05",
        "cutoff_label": "截止 2026-10-04 23:59:59", "generated_at_label": "重建时间",
        "freshness": {"latest_label": "2026-10-04 18:00", "age_label": ""},
        "coverage_label": "本周覆盖 7 天", "coverage_complete": True,
        "coverage_days": [{"date": "2026-10-04", "evidence_count": 90, "partial": False}],
        "is_current_week": False, "data_state": "ok", "missing_dates": [],
        "evidence_count": 90, "event_group_count": 47, "duplicate_groups_folded": 0,
        "repeat_event_count": 0, "display_min_priority": 7,
        "blocks": [block], "highlights": block["entries"][:5],
        "highlights_label": "本周已有记录", "notes": ["仅依据已有入库记录"],
        "pagination_block": block["key"] if selected else None,
        "overview_url": OVERVIEW, "pagination_notice": NOTICE,
    }


def _hrefs(body):
    return [unescape(href) for href in re.findall(r'<a\b[^>]*href="([^"]+)"', body)]


def test_overview_keeps_highlights_linked_and_exposes_remaining_entries(render_report):
    block = _block()
    block["entries"][0]["title"] = '<img src=x onerror="alert(1)"> & 周报'
    body = render_report(_report(block))

    assert body.count('class="event-card"') == 20
    assert "展示第 1–20 条 / 可展示 45 条 · 共 47" in body
    assert "低于门槛未展开 2" in body
    assert "超出条数上限" not in body
    assert f"{OVERVIEW}&block=new&page=2" in _hrefs(body)
    assert "查看更多新增事件 →" in body
    assert "上一页" not in body
    assert NOTICE in body
    assert "<img" not in body and "&lt;img" in body and "&amp; 周报" in body
    highlights = re.findall(r'data-highlight-event-id="([^"]+)"', body)
    assert len(highlights) == 5
    for event_id in highlights:
        assert f"#report-event-{event_id}" in _hrefs(body)
        assert f'id="report-event-{event_id}"' in body
    assert "<script" not in body


@pytest.mark.parametrize("page, count, span", [(1, 20, "1–20"), (2, 20, "21–40"), (3, 5, "41–45")])
def test_selected_block_page_has_bounds_return_link_and_no_orphan_highlights(render_report, page, count, span):
    report = _report(_block(page), selected=True)
    # Keep an unrelated overview highlight in the model to check the template guard.
    report["highlights"] = [_entry(999)]
    body = render_report(report)
    hrefs = _hrefs(body)

    assert body.count('class="event-card"') == count
    assert "事件周报 · 新增事件" in body
    assert f"当前仅展示新增事件 · 第 {page} / 3 页" in body
    assert f"展示第 {span} 条 / 可展示 45 条 · 共 47" in body
    assert OVERVIEW in hrefs and "返回本周概览" in body
    assert 'data-testid="weekly-highlights"' not in body
    assert "#report-event-" not in body and "event-999" not in body
    assert (f"{OVERVIEW}&block=new&page={page - 1}" in hrefs) == (page > 1)
    assert (f"{OVERVIEW}&block=new&page={page + 1}" in hrefs) == (page < 3)
    # Coverage remains week-wide, and changing the week drops block/page state.
    assert "本周入库原文 90 条" in body and "归并后事件 47 个" in body
    assert "/dashboard/events/weekly?week=2026-09-21" in hrefs
    assert "/dashboard/events/weekly?week=2026-10-05" in hrefs
    date_form = re.search(r'<form class="date-form".*?</form>', body, re.S).group(0)
    assert re.findall(r'name="([^"]+)"', date_form) == ["week"]
    assert NOTICE in body


@pytest.mark.parametrize("eligible", [0, 20])
def test_overview_without_overflow_has_no_paging_controls(render_report, eligible):
    body = render_report(_report(_block(eligible=eligible)))

    assert 'data-testid="pagination-new"' not in body
    assert not any("&page=" in href for href in _hrefs(body))
    if not eligible:
        assert "可展示 0 条 · 共 2" in body
        assert 'data-testid="empty-new"' in body
        assert "展示第 0" not in body


def test_daily_shared_blocks_retain_counts_and_wording_without_paging(render_report):
    block = _block()
    del block["pagination"]
    report = _report(block)
    report.update({
        "date": "2026-09-28", "prev_date": "2026-09-27", "next_date": "2026-09-29",
        "min_date": "2025-10-06", "max_date": "2026-10-05",
        "pagination_notice": "", "pagination_block": None,
    })
    body = render_report(report, "event_radar_daily.html")

    assert "展开 20 / 共 47 · 低于门槛未展开 2 · 超出条数上限 25" in body
    assert "当日 2 条报道已合并" in body
    assert "当日入库原文 90 条" in body
    assert 'data-testid="pagination-new"' not in body
    assert "返回本周概览" not in body and NOTICE not in body
    assert "展示第" not in body


DAILY_OVERVIEW = "/dashboard/events/daily?date=2026-09-28"


def _daily_urls(block):
    for key in ("previous_url", "next_url"):
        url = block["pagination"].get(key)
        if url:
            block["pagination"][key] = url.replace(OVERVIEW, DAILY_OVERVIEW)
    return block


def _daily_report(block, *, selected=False):
    report = _report(block, selected=selected)
    report.update({
        "date": "2026-09-28", "prev_date": "2026-09-27", "next_date": "2026-09-29",
        "min_date": "2025-10-06", "max_date": "2026-10-05",
        "is_historical": True, "historical_note": "",
        "overview_url": DAILY_OVERVIEW,
    })
    return report


def test_daily_overview_exposes_remaining_entries_without_weekly_highlights(render_report):
    body = render_report(_daily_report(_daily_urls(_block())), "event_radar_daily.html")

    assert body.count('class="event-card"') == 20
    assert "展示第 1–20 条 / 可展示 45 条 · 共 47" in body
    assert "超出条数上限" not in body
    assert f"{DAILY_OVERVIEW}&block=new&page=2" in _hrefs(body)
    assert "查看更多新增事件 →" in body
    assert NOTICE in body
    assert 'data-testid="weekly-highlights"' not in body
    date_form = re.search(r'<form class="date-form".*?</form>', body, re.S).group(0)
    assert re.findall(r'name="([^"]+)"', date_form) == ["date"]


@pytest.mark.parametrize("page, count, span", [(1, 20, "1–20"), (2, 20, "21–40"), (3, 5, "41–45")])
def test_daily_selected_block_page_has_bounds_and_return_link(render_report, page, count, span):
    body = render_report(
        _daily_report(_daily_urls(_block(page)), selected=True), "event_radar_daily.html",
    )
    hrefs = _hrefs(body)

    assert body.count('class="event-card"') == count
    assert "事件日报 · 新增事件 · 2026-09-28" in body
    assert f"当前仅展示新增事件 · 第 {page} / 3 页" in body
    assert f"展示第 {span} 条 / 可展示 45 条 · 共 47" in body
    assert DAILY_OVERVIEW in hrefs and "返回当日概览" in body
    assert (f"{DAILY_OVERVIEW}&block=new&page={page - 1}" in hrefs) == (page > 1)
    assert (f"{DAILY_OVERVIEW}&block=new&page={page + 1}" in hrefs) == (page < 3)
    assert "/dashboard/events/daily?date=2026-09-27" in hrefs
    assert "/dashboard/events/daily?date=2026-09-29" in hrefs
    date_form = re.search(r'<form class="date-form".*?</form>', body, re.S).group(0)
    assert re.findall(r'name="([^"]+)"', date_form) == ["date"]
    assert NOTICE in body
