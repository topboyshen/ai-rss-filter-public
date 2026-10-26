"""Bounded daily reading access, independent of scoring or weekly data."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly
from services.event_radar_query import EventRadarQueryError
from utils.report_paging import MAX_REPORT_PAGE, REPORT_BLOCK_KEYS, parse_report_paging

DAY = "2026-10-05"
NOW = int(datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
OBSERVED = int(datetime(2026, 10, 5, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


def _seed(database, count, *, priority=6, prefix="公司披露项目进展", stance="supports"):
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    ids = []
    with database.get_connection() as conn:
        for index in range(count):
            title = f"{prefix}{index}，金额{index + 10}亿元"
            event_id = resolve_or_create_event(
                conn, {"daily-paging": title}, title=title, event_type="policy",
                direction="中性", priority=priority, occurred_at=OBSERVED, now=OBSERVED,
            )
            upsert_event_evidence(
                conn, event_id, source_uri=f"https://news.example/{event_id}",
                source_name="https://news.example/rss", claim_key=event_id,
                claim_text=title, stance=stance, announcement_at=OBSERVED,
                observed_at=OBSERVED, now=OBSERVED,
            )
            ids.append(event_id)
    return sorted(ids)


def _report(**kwargs):
    return daily.load_daily_report(DAY, now=NOW, **kwargs)


def _block(report, key="new"):
    return next(block for block in report["blocks"] if block["key"] == key)


def _ids(block):
    return [item["event_id"] for item in block["entries"]]


@pytest.mark.parametrize("count", [0, 1, 20, 21, 40, 41, 101])
def test_static_ranked_set_is_readable_in_pages_of_twenty_without_gaps(database, count):
    expected = _seed(database, count)
    overview = _report()
    first = _block(overview)
    assert _ids(first) == expected[:20]
    assert overview["pagination_block"] is None
    assert "固定快照" in overview["pagination_notice"]
    assert first["hidden_overflow"] == max(0, count - 20)
    pages = max(1, (count + 19) // 20)
    assert first["pagination"]["page_count"] == pages
    assert bool(first["pagination"]["next_url"]) is (count > 20)
    seen = []
    for page in range(1, pages + 1):
        report = _report(block="new", page=page)
        assert len(report["blocks"]) == 1
        block = _block(report)
        paging = block["pagination"]
        shown = _ids(block)
        assert shown == expected[(page - 1) * 20:page * 20]
        assert len(shown) <= 20
        assert paging["eligible_total"] == count
        assert paging["start_index"] == ((page - 1) * 20 + 1 if count else 0)
        assert paging["end_index"] == min(page * 20, count)
        assert bool(paging["previous_url"]) is (page > 1)
        assert bool(paging["next_url"]) is (page < pages)
        if page < pages:
            assert paging["next_url"] == (
                f"/dashboard/events/daily?date={DAY}&block=new&page={page + 1}"
            )
        seen.extend(shown)
    assert seen == expected and len(set(seen)) == count
    if count >= 21:
        assert expected[20] in _ids(_block(_report(block="new", page=2)))
    with pytest.raises(EventRadarQueryError, match="no longer exists"):
        _report(block="new", page=pages + 1)


def test_paging_keeps_low_score_gate_and_denial_exception(database):
    high = _seed(database, 21)
    low = _seed(database, 5, priority=5, prefix="低分公司项目")
    denials = _seed(database, 23, priority=1, prefix="公司否认订单传闻", stance="contradicts")
    _seed(database, 1, priority=1, prefix="交易所要求公司澄清订单")
    overview = _report()
    new_page = _block(_report(block="new", page=2))
    assert _ids(new_page) == high[20:] and not set(low) & set(_ids(new_page))
    assert new_page["total"] == 26 and new_page["hidden_low_priority"] == 5
    correction = _block(_report(block="correction", page=2), "correction")
    assert _ids(correction) == denials[20:]
    assert correction["hidden_low_priority"] == 1
    assert all(item["correction_kind"] == "denial" for item in correction["entries"])
    assert [block["key"] for block in overview["blocks"]] == list(daily.BLOCK_ORDER)


def test_only_twenty_selected_page_cards_load_associations_and_sources(database, monkeypatch):
    expected = _seed(database, 61)
    _seed(database, 25, priority=1, prefix="公司否认传闻", stance="contradicts")
    association_reads, source_views = [], []
    original_load, original_source = daily._load_associations, daily._source_views

    def load(conn, ids, cutoff):
        association_reads.append(set(ids))
        return original_load(conn, ids, cutoff)

    def source(items, history, **kwargs):
        source_views.append(items[0]["event_id"])
        return original_source(items, history, **kwargs)

    monkeypatch.setattr(daily, "_load_associations", load)
    monkeypatch.setattr(daily, "_source_views", source)
    report = _report(block="new", page=2)
    assert _ids(_block(report)) == expected[20:40]
    assert association_reads == [set(expected[20:40])]
    assert len(source_views) == 20 and set(source_views) == set(expected[20:40])


def test_rank_order_is_unchanged_and_weekly_does_not_gain_daily_urls():
    items = [
        {"block": "new", "event_id": f"id-{index:02}", "priority": 6 + index % 3,
         "latest_at": index % 5, "correction_kind": ""}
        for index in range(45)
    ]
    expected = sorted(items, key=lambda item: (-item["priority"], -item["latest_at"], item["event_id"]))
    original = daily._assemble_blocks(items, "ok", "")
    paged = daily._assemble_blocks(items, "ok", "", block_pages={"new": 2})
    assert _ids(original[0]) == [item["event_id"] for item in expected[:20]]
    assert _ids(paged[0]) == [item["event_id"] for item in expected[20:40]]
    assert all("pagination" not in block for block in original)
    assert set(daily.BLOCK_ORDER) == REPORT_BLOCK_KEYS
    assert MAX_REPORT_PAGE * daily.MAX_ITEMS_PER_BLOCK >= weekly.MAX_WEEK_EVIDENCE_ROWS


@pytest.mark.parametrize("block,page", [
    (None, "2"), ("new", None), ("all", "1"), ("new", "0"), ("new", "-1"),
    ("new", "01"), ("new", "1.0"), ("new", "1e2"), ("new", " 2"), ("new", "２"),
    ("new", "7001"), ("new", "999999999999999999"), ("new", True),
])
def test_bad_paging_is_rejected_before_opening_database(monkeypatch, block, page):
    monkeypatch.setattr(daily, "get_read_only_connection", lambda: pytest.fail("unexpected DB read"))
    with pytest.raises(EventRadarQueryError):
        _report(block=block, page=page)


def test_paging_requires_explicit_date_before_database_read(monkeypatch):
    monkeypatch.setattr(daily, "get_read_only_connection", lambda: pytest.fail("unexpected DB read"))
    with pytest.raises(EventRadarQueryError):
        daily.load_daily_report(block="new", page=2, now=NOW)
    assert parse_report_paging() == (None, 1)
    assert parse_report_paging("pending", "7000") == ("pending", 7000)


@pytest.mark.parametrize("date_value", ["", " ", "   ", " 2026-10-05", "2026-10-05 "])
def test_paging_rejects_blank_or_padded_date_before_database_read(monkeypatch, date_value):
    monkeypatch.setattr(daily, "get_read_only_connection", lambda: pytest.fail("unexpected DB read"))
    with pytest.raises(EventRadarQueryError):
        daily.load_daily_report(date_value, block="new", page=2, now=NOW)


def test_blank_date_without_paging_still_defaults_to_today():
    from datetime import date

    today = date(2026, 10, 12)
    assert daily.parse_report_date(None, now=NOW) == today
    assert daily.parse_report_date("", now=NOW) == today
    assert daily.parse_report_date("   ", now=NOW) == today


def test_daily_paging_never_claims_cross_request_snapshot_stability(database):
    expected = _seed(database, 21)
    first = _report(block="new", page=2)
    assert _ids(_block(first)) == expected[20:]
    _seed(database, 1, priority=9, prefix="新到的高分事件")
    second = _report(block="new", page=2)
    assert _ids(_block(second)) == expected[19:]
    assert second["pagination_notice"] == "每次翻页按当前已有记录重建，排序可能变化；这不是固定快照。"
    assert second["overview_url"] == f"/dashboard/events/daily?date={DAY}"
