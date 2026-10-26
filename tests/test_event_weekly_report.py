"""Weekly outcomes over temporary databases; no model or network calls."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly
from services.event_radar_query import EventRadarQueryError, EventRadarUnavailableError

TZ = ZoneInfo("Asia/Shanghai")


def _ts(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp())


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


def _event(database, title, at, *, key=None, priority=7.0):
    from db.event_core import resolve_or_create_event

    with database.get_connection() as conn:
        return resolve_or_create_event(
            conn, {"weekly_test": key or title}, title=title, event_type="policy",
            direction="中性", priority=priority, occurred_at=at, now=at,
        )


def _evidence(database, event_id, title, at, *, url=None, stance="supports"):
    from db.event_core import upsert_event_evidence

    url = url or f"https://news.example/{event_id}/{at}"
    with database.get_connection() as conn:
        return upsert_event_evidence(
            conn, event_id, source_uri=url, source_name="https://news.example/rss",
            claim_key=f"weekly:{url}:{title}", claim_text=title, stance=stance,
            announcement_at=at - 60, observed_at=at, now=at,
        )


def _article(database, title, when, *, key=None, priority=7.0):
    at = _ts(when)
    event = _event(database, title, at, key=key, priority=priority)
    _evidence(database, event, title, at)
    return event


def _report(week="2026-10-05", now="2026-10-12T01:00:00"):
    return weekly.load_weekly_report(week, now=_ts(now))


def _block(report, key):
    return next(block for block in report["blocks"] if block["key"] == key)


def _items(report):
    return [item for block in report["blocks"] for item in block["entries"]]


@pytest.mark.parametrize("bad", [
    "", " ", "2026-10-5", "2026-W41", "2026-10-05 ", " 2026-10-05", "2026-10-06",
    "2026-13-01", "2026-02-30", "2026-10-19", "2024-10-07", 20261005,
])
def test_week_parameter_is_strict_monday_and_bounded(bad):
    with pytest.raises(EventRadarQueryError):
        weekly.parse_report_week(bad, now=_ts("2026-10-12T01:00:00"))


def test_default_is_previous_complete_week_and_navigation_stops_at_bounds(database):
    now = _ts("2027-01-04T00:00:00")
    assert weekly.parse_report_week(now=now) == date(2026, 12, 28)
    report = weekly.load_weekly_report(now=now)
    assert report["week"] == "2026-12-28"
    assert report["start_date"] == "2026-12-28" and report["end_date"] == "2027-01-03"
    assert report["next_week"] == "2027-01-04"
    oldest = weekly.load_weekly_report(report["min_week"], now=now)
    assert oldest["prev_week"] == ""
    assert date.fromisoformat(oldest["min_week"]).weekday() == 0


def test_cross_year_window_includes_monday_and_sunday_but_not_next_monday(database):
    _article(database, "周前报道", "2026-12-27T23:59:59")
    _article(database, "周一起点报道", "2026-12-28T00:00:00")
    _article(database, "周日终点报道", "2027-01-03T23:59:59")
    _article(database, "次周报道", "2027-01-04T00:00:00")
    report = _report("2026-12-28", "2027-01-04T09:00:00")
    assert {item["title"] for item in _items(report)} == {"周一起点报道", "周日终点报道"}
    assert report["evidence_count"] == 2 and report["partial"] is False
    assert report["cutoff_label"] == "截止 2027-01-03 23:59:59（北京时间）"
    assert report["is_historical"] is True
    assert "事件标题与事件评分为当前值" in report["historical_note"]


def test_current_week_stops_at_now_and_does_not_mark_future_days_missing(database):
    _article(database, "周一入库", "2026-10-05T09:00:00")
    _article(database, "当前入库", "2026-10-07T09:00:00")
    _article(database, "未来入库", "2026-10-07T10:00:00")
    report = _report(now="2026-10-07T09:00:00")
    assert report["evidence_count"] == 2
    assert report["partial"] and report["is_current_week"]
    assert report["next_week"] == ""
    assert "本周未结束" in report["cutoff_label"]
    assert report["missing_dates"] == ["2026-10-06"]
    assert [day["date"] for day in report["coverage_days"]] == [
        "2026-10-05", "2026-10-06", "2026-10-07",
    ]
    assert report["coverage_days"][-1]["partial"]
    assert not report["coverage_complete"]


def test_weekly_denial_reposts_are_one_change_and_older_repost_cannot_supersede_newer_fact(database):
    title = "某公司项目已经落地"
    event = _article(database, title, "2026-10-05T09:00:00")
    first = "某公司否认项目投资金额为100亿元"
    latest = "更正：某公司项目投资金额为80亿元"
    _evidence(database, event, first, _ts("2026-10-06T18:00:00"))
    _evidence(database, event, latest, _ts("2026-10-07T18:00:00"))
    _evidence(database, event, first, _ts("2026-10-09T18:00:00"))
    report = _report()
    corrections = _block(report, "correction")
    assert corrections["total"] == 1
    item = corrections["entries"][0]
    assert f"本周出现否认/更正：{latest}" in item["change"]
    assert "本周出现否认/更正：" + first not in item["change"]
    assert "此前有 1 条支持记录" in item["change"]
    assert item["report_count"] == 4 and item["unique_change_count"] == 3
    assert report["repeat_evidence_count"] == 1
    assert report["repeat_event_count"] == 0
    assert len(item["sources"]) == 4


def test_cross_root_weekly_denial_reposts_fold_once_even_at_low_priority(database):
    for index, day in enumerate((5, 7, 9)):
        _article(database, "某公司否认获大额订单", f"2026-10-{day:02d}T10:00:00",
                 key=f"denial-{index}", priority=2.0)
    report = _report()
    correction = _block(report, "correction")
    assert correction["total"] == len(correction["entries"]) == 1
    assert correction["entries"][0]["unique_change_count"] == 1
    assert report["evidence_count"] == 3
    assert report["repeat_evidence_count"] == 2


def test_preweek_denial_repost_is_background_while_new_denial_is_visible(database):
    old = "甲公司否认获得项目订单"
    _article(database, old, "2026-10-04T18:00:00", key="old-denial")
    _article(database, old, "2026-10-05T09:00:00", key="repeat-denial")
    new = "乙公司否认获得项目订单"
    _article(database, new, "2026-10-07T09:00:00")
    report = _report()
    assert report["repeat_event_count"] == 1
    assert report["repeat_evidence_count"] == 1
    assert [item["title"] for item in _block(report, "correction")["entries"]] == [new]
    assert old not in {item["title"] for item in report["highlights"]}


def test_new_followup_keeps_preweek_denial_as_previous_background(database):
    event = _article(database, "某公司否认收购项目", "2026-10-04T18:00:00")
    _evidence(database, event, "某公司披露审计结果", _ts("2026-10-07T10:00:00"))
    item = _block(_report(), "advanced")["entries"][0]
    assert item["previous"] == {
        "claim_text": "某公司否认收购项目", "time_label": "2026-10-04 18:00",
    }
    assert any("此前已有否认/更正" in note for note in item["uncertainties"])


def test_comparators_are_distinct_in_week_and_against_preweek_history(database):
    _article(database, "某公司净利润≤5亿元", "2026-10-04T12:00:00")
    _article(database, "某公司净利润≥5亿元", "2026-10-05T12:00:00")
    _article(database, "某指数成交额<1万亿元", "2026-10-06T12:00:00")
    _article(database, "某指数成交额≥1万亿元", "2026-10-07T12:00:00")
    report = _report()
    assert report["repeat_event_count"] == 0
    assert report["duplicate_groups_folded"] == 0
    assert len(_items(report)) == 3


def test_weekly_aggregation_retains_support_beyond_daily_top_twenty(database):
    for index in range(20):
        _article(database, f"优先级较高的政策{index}", "2026-10-05T09:00:00", priority=8.0)
    original = "某企业获得项目订单"
    event = _article(database, original, "2026-10-05T10:00:00", priority=6.0)
    day = daily.load_daily_report("2026-10-05", now=_ts("2026-10-12T01:00:00"))
    assert original not in {item["title"] for item in _items(day)}
    _evidence(database, event, "某企业否认获得项目订单", _ts("2026-10-06T10:00:00"))
    report = _report()
    item = _block(report, "correction")["entries"][0]
    assert item["title"] == original
    assert "此前有 1 条支持记录（最早 2026-10-05 10:00）" in item["change"]
    assert any(source["claim_text"] == original for source in item["sources"])
    assert report["evidence_count"] == 22


def test_highlights_only_use_displayed_candidates_and_never_pad(database):
    _article(database, "评分不足的普通报道", "2026-10-05T09:00:00", priority=2.0)
    _article(database, "某公司否认重大传闻", "2026-10-06T09:00:00", priority=1.0)
    report = _report()
    assert len(report["highlights"]) == 1
    assert report["highlights"][0] in _items(report)
    assert report["highlights"][0]["correction_kind"] == "denial"
    for index in range(8):
        _article(database, f"新增政策条目{index}", "2026-10-07T10:00:00")
    report = _report()
    assert len(report["highlights"]) == 5
    assert all(item in _items(report) for item in report["highlights"])


def test_midweek_gap_is_not_hidden_by_fresh_sunday_record(database):
    _article(database, "更早覆盖起点", "2026-10-01T00:00:00")
    for day in (5, 6, 8, 9, 10, 11):
        _article(database, f"当日记录{day}", f"2026-10-{day:02d}T23:59:59")
    report = _report()
    assert report["freshness"]["stale"] is False
    assert report["data_state"] == "incomplete"
    assert report["missing_dates"] == ["2026-10-07"]
    assert "2026-10-07" in report["data_state_label"]
    assert not report["coverage_complete"]


def test_truncation_does_not_falsely_report_unread_tail_as_missing(database, monkeypatch):
    for day in range(5, 12):
        _article(database, f"本周记录{day}", f"2026-10-{day:02d}T23:59:59")
    monkeypatch.setattr(weekly, "MAX_WEEK_EVIDENCE_ROWS", 2)
    report = _report()
    assert report["evidence_count"] == 2
    assert report["data_state"] == "truncated"
    assert "本周证据超过读取上限 2 条" in report["data_state_label"]
    assert report["missing_dates"] == []
    assert all(day["has_evidence"] and day["evidence_count"] is None for day in report["coverage_days"])
    assert all("证据不足" in block["empty_label"] for block in report["blocks"] if not block["entries"])


@pytest.mark.parametrize("target", ["MAX_LOOKBACK_EVIDENCE_ROWS", "MAX_LINKED_HISTORY_ROWS"])
def test_preweek_history_truncation_marks_report_incomplete(database, monkeypatch, target):
    event = _article(database, "周前最早记录", "2026-10-01T09:00:00")
    _evidence(database, event, "周前第二条记录", _ts("2026-10-02T09:00:00"))
    _evidence(database, event, "本周新记录", _ts("2026-10-05T09:00:00"))
    monkeypatch.setattr(weekly, target, 1)
    report = _report()
    assert report["data_state"] == "truncated"
    assert "读取上限" in report["data_state_label"]
    assert not report["coverage_complete"]


def test_no_records_and_precoverage_do_not_mean_no_events(database):
    report = _report()
    assert report["data_state"] == "before_coverage"
    assert report["highlights"] == []
    _article(database, "周前已有覆盖", "2026-10-01T09:00:00")
    report = _report()
    assert report["data_state"] == "no_ingestion"
    assert len(report["missing_dates"]) == 7
    assert all("证据不足" in block["empty_label"] for block in report["blocks"])


def test_current_title_does_not_rewrite_historical_groups_or_baseline(database):
    _article(database, "周前历史标题", "2026-10-04T09:00:00")
    left = _article(database, "本周独立事件甲", "2026-10-05T09:00:00")
    right = _article(database, "本周独立事件乙", "2026-10-06T09:00:00")
    with database.get_connection() as conn:
        conn.execute("UPDATE events SET title=? WHERE event_id IN (?, ?)",
                     ("周前历史标题", left, right))
        conn.commit()
    report = _report()
    assert report["event_group_count"] == 2
    assert report["duplicate_groups_folded"] == 0
    assert _block(report, "new")["total"] == 2
    assert all(item["previous"] is None for item in _items(report))
    assert {item["title"] for item in _items(report)} == {"周前历史标题"}


def test_readonly_loader_does_not_write_and_does_not_call_model(database, monkeypatch):
    from contextlib import contextmanager

    from core.scorer import Scorer

    _article(database, "本周只读记录", "2026-10-05T09:00:00")
    statements = []
    original = weekly.get_read_only_connection

    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(weekly, "get_read_only_connection", traced)
    monkeypatch.setattr(Scorer, "__init__", lambda *_a, **_k: pytest.fail("report must not call a model"))
    report = _report()
    assert report["evidence_count"] == 1
    assert statements and any(sql == "BEGIN" for sql in statements)
    forbidden = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")
    assert not any(sql.lstrip().upper().startswith(forbidden) for sql in statements)


def test_merge_after_week_cutoff_does_not_rewrite_weekly_identity(database):
    from db.event_core import merge_events

    old = _article(database, "周前独立事件", "2026-10-04T09:00:00")
    new = _article(database, "本周独立事件", "2026-10-06T09:00:00")
    with database.get_connection() as conn:
        merge_events(conn, new, old, reason="test-after-week", now=_ts("2026-10-12T09:00:00"))
    report = _report(now="2026-10-13T09:00:00")
    assert _block(report, "new")["total"] == 1
    item = _block(report, "new")["entries"][0]
    assert item["event_id"] == new and item["previous"] is None


@pytest.mark.parametrize("before_week", [False, True])
@pytest.mark.parametrize(("original", "correction", "stance"), [
    ("某公司项目状态说明", "某公司项目状态说明", "contradicts"),
    ("某公司澄清 未参与该项目", "某公司澄清：未参与该项目", "supports"),
])
def test_changed_denial_semantics_survive_same_normalized_title(
    database, before_week, original, correction, stance,
):
    when = "2026-10-04T09:00:00" if before_week else "2026-10-05T09:00:00"
    event = _article(database, original, when)
    _evidence(database, event, correction, _ts("2026-10-06T10:00:00"), stance=stance)
    report = _report()
    entries = _block(report, "correction")["entries"]
    assert len(entries) == 1
    assert "本周出现否认/更正：" + correction in entries[0]["change"]
    assert entries[0]["correction_kind"] == "denial"
    assert report["repeat_event_count"] == 0
    assert report["repeat_evidence_count"] == 0
    day = daily.load_daily_report("2026-10-06", now=_ts("2026-10-12T01:00:00"))
    daily_entries = _block(day, "correction")["entries"]
    assert len(daily_entries) == 1
    assert daily_entries[0]["correction_kind"] == entries[0]["correction_kind"]
    assert "今日出现否认/更正：" + correction in daily_entries[0]["change"]
    assert day["repeat_event_count"] == 0


def test_primary_new_correction_source_is_not_hidden_by_many_old_reposts(database):
    event = _article(database, "某公司项目落地", "2026-10-05T09:00:00")
    first = "某公司否认投资金额为100亿元"
    latest = "更正：某公司项目投资金额为80亿元"
    _evidence(database, event, first, _ts("2026-10-06T10:00:00"))
    _evidence(database, event, latest, _ts("2026-10-07T10:00:00"), url="https://news.example/correction")
    for index in range(6):
        _evidence(database, event, first, _ts("2026-10-09T10:00:00") + index,
                  url=f"https://reprint.example/{index}")
    item = _block(_report(), "correction")["entries"][0]
    assert latest in item["change"]
    assert item["sources"][0]["claim_text"] == latest
    assert item["sources"][0]["url"] == "https://news.example/correction"
    assert len(item["sources"]) == 5 and item["sources_more"] == 4


def test_display_title_cannot_invent_an_original_company_mention(database):
    from db.event_core import upsert_event_security, upsert_security

    event = _article(database, "养殖行业支持政策已经发布", "2026-10-05T09:00:00")
    with database.get_connection() as conn:
        security = upsert_security(conn, exchange="SZSE", ticker="000876", name="新希望", now=_ts("2026-10-05T09:00:00"))
        upsert_event_security(
            conn, event, security, relation_type="direct", confidence=0.8,
            relevance=8.0, mapping_version="mapping-test-v1", now=_ts("2026-10-05T09:00:00"),
        )
        conn.execute("UPDATE events SET title=? WHERE event_id=?", ("新希望获政策支持", event))
        conn.commit()
    item = _items(_report())[0]
    assert item["title"] == "新希望获政策支持"
    assert item["companies"] == []
    assert item["rule_association_count"] == 1


def test_read_deadline_interrupts_sql_and_returns_unavailable(database, monkeypatch):
    ticks = iter([0.0])
    monkeypatch.setattr(weekly, "MAX_REPORT_SECONDS", 1.0)
    monkeypatch.setattr(weekly.time, "monotonic", lambda: next(ticks, 2.0))

    def long_read(conn, *_args):
        conn.execute(
            "WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<100000) "
            "SELECT SUM(n) FROM seq"
        ).fetchone()
        pytest.fail("SQL progress handler failed to interrupt expired report")

    monkeypatch.setattr(daily, "_load_window_evidence", long_read)
    with pytest.raises(EventRadarUnavailableError, match="weekly report is unavailable"):
        _report()


def test_cross_root_prior_denial_between_support_endpoints_is_not_counted_again(database):
    title = "某公司项目状态说明"
    _article(database, title, "2026-09-28T09:00:00", key="preweek-first")
    denied = _event(database, title, _ts("2026-09-30T09:00:00"), key="preweek-denial")
    _evidence(database, denied, title, _ts("2026-09-30T09:00:00"), stance="contradicts")
    _article(database, title, "2026-10-02T09:00:00", key="preweek-last")
    repeat = _event(database, title, _ts("2026-10-06T09:00:00"), key="thisweek-repeat")
    _evidence(database, repeat, title, _ts("2026-10-06T09:00:00"), stance="contradicts")
    report = _report()
    assert report["repeat_event_count"] == 1
    assert report["repeat_evidence_count"] == 1
    assert _block(report, "correction")["total"] == 0


def test_only_final_visible_groups_load_associations_and_source_views(database, monkeypatch):
    high = {
        _article(database, f"本周高分政策{index}", "2026-10-05T10:00:00", priority=8.0)
        for index in range(25)
    }
    low = {
        _article(database, f"本周低分政策{index}", "2026-10-06T10:00:00", priority=2.0)
        for index in range(40)
    }
    denials = {
        _article(database, f"某公司否认项目事项{index}", "2026-10-07T10:00:00", priority=1.0)
        for index in range(3)
    }
    association_reads, association_views, source_views = [], [], []
    original_load = daily._load_associations
    original_association = daily._association_view
    original_source = daily._source_views

    def load(conn, event_ids, cutoff):
        association_reads.append(set(event_ids))
        return original_load(conn, event_ids, cutoff)

    def association(group, names):
        association_views.append(group["root_ids"][0])
        return original_association(group, names)

    def source(items, history, **kwargs):
        source_views.append(items[0]["event_id"])
        return original_source(items, history, **kwargs)

    monkeypatch.setattr(daily, "_load_associations", load)
    monkeypatch.setattr(daily, "_association_view", association)
    monkeypatch.setattr(daily, "_source_views", source)
    report = _report()
    shown = {item["event_id"] for item in _items(report)}
    assert len(shown) == 23 and denials <= shown and not low & shown
    assert shown <= high | denials
    assert _block(report, "new")["total"] == 65
    assert _block(report, "new")["hidden_low_priority"] == 40
    assert _block(report, "new")["hidden_overflow"] == 5
    assert _block(report, "correction")["total"] == 3
    assert report["evidence_count"] == 68 and report["event_group_count"] == 68
    assert association_reads == [shown]
    assert set(association_views) == set(source_views) == shown
    assert len(association_views) == len(source_views) == 23


@pytest.mark.parametrize("complete_lookback", [False, True])
def test_optimized_linked_history_keeps_every_prior_support_and_denial(database, complete_lookback):
    event = _article(database, "某公司获得项目订单", "2026-09-20T09:00:00")
    _evidence(database, event, "某公司否认获得项目订单", _ts("2026-09-22T09:00:00"), stance="contradicts")
    _evidence(database, event, "某公司披露项目详情", _ts("2026-09-28T00:00:00"))
    _evidence(database, event, "某公司项目情况补充说明", _ts("2026-10-01T10:00:00"))
    other = _article(database, "另一公司项目正式落地", "2026-10-03T10:00:00")
    _article(database, "未关联的旧报道", "2026-09-21T09:00:00")
    members = {event, other}
    before = _ts("2026-10-05T00:00:00")
    with database.get_read_only_connection() as conn:
        expected = daily._load_prior_evidence(conn, members, before)
        lookback, cut = daily._load_window_evidence(
            conn, before - daily.REPEAT_LOOKBACK_SECONDS, before - 1,
            100 if complete_lookback else 1,
        )
        assert cut is not complete_lookback
        statements = []
        conn.set_trace_callback(statements.append)
        actual, truncated = weekly._linked_history(
            conn, members, before, lookback_rows=lookback if not cut else None,
        )
    expected_rows = {row["evidence_id"]: row for rows in expected.values() for row in rows}
    actual_rows = {row["evidence_id"]: row for rows in actual.values() for row in rows}
    assert not truncated and actual_rows == expected_rows
    assert len(actual_rows) == 5
    assert sum(row["stance"] == "supports" for row in actual_rows.values()) == 4
    assert sum(row["stance"] == "contradicts" for row in actual_rows.values()) == 1
    history_sql = [sql for sql in statements if "INDEXED BY idx_event_evidence_unbound_event_observed" in sql]
    assert len(history_sql) == 1
    assert "INDEXED BY idx_event_evidence_unbound_event_observed" in history_sql[0]
    cutoff = before - daily.REPEAT_LOOKBACK_SECONDS if complete_lookback else before
    assert f"ev.observed_at < {cutoff}" in history_sql[0]
    assert "ev.claim_text" not in history_sql[0]  # Only identities enter the SQL sorter.
    payload_sql = [sql for sql in statements if "WHERE ev.rowid IN" in sql]
    assert len(payload_sql) == 1 and "ORDER BY" not in payload_sql[0]


def test_linked_history_shares_one_cap_across_old_rows_and_reused_lookback(database, monkeypatch):
    event = _article(database, "项目最早支持记录", "2026-09-20T09:00:00")
    _evidence(database, event, "项目另一支持记录", _ts("2026-09-21T09:00:00"))
    _evidence(database, event, "项目近期支持记录", _ts("2026-10-01T09:00:00"))
    _evidence(database, event, "项目近期更正记录", _ts("2026-10-02T09:00:00"))
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", 3)
    before = _ts("2026-10-05T00:00:00")
    with database.get_read_only_connection() as conn:
        recent, _ = daily._load_window_evidence(
            conn, before - daily.REPEAT_LOOKBACK_SECONDS, before - 1, 100,
        )
        history, cut = weekly._linked_history(conn, {event}, before, lookback_rows=recent)
    assert cut and len(history[event]) == 3
    assert [row["claim_text"] for row in history[event]] == [
        "项目最早支持记录", "项目另一支持记录", "项目近期支持记录",
    ]


def test_linked_history_honors_connection_parameter_limit(database):
    import sqlite3

    members = {
        _article(database, f"旧项目记录{index}", "2026-09-20T09:00:00")
        for index in range(7)
    }
    with database.get_read_only_connection() as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, len(daily._PUBLIC_SCORE_PATH_VALUES) + 5)
        history, cut = weekly._linked_history(conn, members, _ts("2026-10-05T00:00:00"))
    assert not cut and set(history) == members
    assert sum(len(rows) for rows in history.values()) == 7
