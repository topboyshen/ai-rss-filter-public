"""Request-local text memoization and complete-read coverage, without timing gates."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier, Lock, get_ident
from zoneinfo import ZoneInfo

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly

WEEK = "2026-10-05"
NOW = int(datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _call(kind, title="某公司获得订单", stance="supports"):
    if kind == "normalized":
        return daily._normalized_key(title)
    return daily._correction_kind({"claim_text": title, "stance": stance})


def _count_computations(monkeypatch, kind):
    name = "_normalize_headline" if kind == "normalized" else "_headline_correction_kind"
    original = getattr(daily, name)
    calls = Counter()

    def counted(*args):
        calls[args] += 1
        return original(*args)

    monkeypatch.setattr(daily, name, counted)
    return calls


@pytest.mark.parametrize("kind", ["normalized", "correction"])
def test_repeated_text_computes_once_only_inside_one_request(monkeypatch, kind):
    calls = _count_computations(monkeypatch, kind)
    expected = _call(kind)
    assert _call(kind) == expected
    assert sum(calls.values()) == 2  # No unbounded process-global cache.
    with daily._report_text_cache():
        assert [_call(kind) for _ in range(10)] == [expected] * 10
    assert sum(calls.values()) == 3
    with daily._report_text_cache():
        assert _call(kind) == expected
    assert sum(calls.values()) == 4
    assert _call(kind) == expected
    assert sum(calls.values()) == 5


def test_identical_title_different_stance_keeps_distinct_correction_results(monkeypatch):
    calls = _count_computations(monkeypatch, "correction")
    title = "某公司披露项目进展"
    with daily._report_text_cache():
        for _ in range(4):
            assert _call("correction", title, "supports") == ""
            assert _call("correction", title, "contradicts") == "denial"
    assert calls == {(title, "supports"): 1, (title, "contradicts"): 1}


@pytest.mark.parametrize("left,right", [
    ("净利润1.5亿元", "净利润15亿元"),
    ("同比-5%", "同比+5%"),
    ("金额≤5亿元", "金额≥5亿元"),
    ("成交额＜1万亿元", "成交额≥1万亿元"),
    ("成交额5亿元", "成交额5万元"),
    ("利率下降5bp", "利率下降5%"),
])
def test_cached_normalization_preserves_numeric_and_comparator_identity(left, right):
    expected = daily._normalized_key(left), daily._normalized_key(right)
    assert expected[0] != expected[1]
    with daily._report_text_cache():
        for _ in range(3):
            assert (daily._normalized_key(left), daily._normalized_key(right)) == expected


@pytest.mark.parametrize("kind", ["normalized", "correction"])
def test_nested_context_restores_parent_and_exception_does_not_leak(monkeypatch, kind):
    calls = _count_computations(monkeypatch, kind)
    with daily._report_text_cache():
        expected = _call(kind)
        with pytest.raises(RuntimeError, match="inner failure"):
            with daily._report_text_cache():
                assert _call(kind) == _call(kind) == expected
                raise RuntimeError("inner failure")
        assert sum(calls.values()) == 2
        assert _call(kind) == expected
        assert sum(calls.values()) == 2  # Outer cache was restored, not cleared.
    with pytest.raises(RuntimeError, match="outer failure"):
        with daily._report_text_cache():
            assert _call(kind) == expected
            raise RuntimeError("outer failure")
    assert _call(kind) == expected
    assert sum(calls.values()) == 4


@pytest.mark.parametrize("kind", ["normalized", "correction"])
def test_parallel_requests_never_share_or_clear_each_others_cache(monkeypatch, kind):
    name = "_normalize_headline" if kind == "normalized" else "_headline_correction_kind"
    original = getattr(daily, name)
    calls, lock, barrier = Counter(), Lock(), Barrier(2)

    def counted(*args):
        with lock:
            calls[get_ident()] += 1
        return original(*args)

    def request():
        with daily._report_text_cache():
            first = _call(kind)
            barrier.wait(timeout=5)
            assert _call(kind) == first
        barrier.wait(timeout=5)
        assert _call(kind) == first
        return get_ident(), first

    monkeypatch.setattr(daily, name, counted)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert len({identity for identity, _ in results}) == 2
    assert results[0][1] == results[1][1]
    assert sorted(calls.values()) == [2, 2]


@pytest.mark.parametrize("kind", ["normalized", "correction"])
def test_cache_is_bounded_and_eviction_does_not_change_result(monkeypatch, kind):
    first, latest = "公司记录0，金额-1.5亿元", "公司记录8192，金额-1.5亿元"
    expected_first, expected_latest = _call(kind, first), _call(kind, latest)
    calls = _count_computations(monkeypatch, kind)
    with daily._report_text_cache():
        for index in range(8193):
            _call(kind, f"公司记录{index}，金额-1.5亿元")
        assert sum(calls.values()) == 8193
        assert _call(kind, latest) == expected_latest
        assert sum(calls.values()) == 8193  # Most recent entry survives.
        assert _call(kind, first) == expected_first
        assert sum(calls.values()) == 8194  # Oldest of 8193 was evicted.


class _CoverageConnection:
    def __init__(self, exists=()):
        self.answers = iter(exists)
        self.queries = []

    def execute(self, query, args):
        self.queries.append((query, args))
        self.answer = next(self.answers)
        return self

    def fetchone(self):
        return (self.answer,)


def test_complete_week_coverage_uses_read_rows_without_seven_extra_queries():
    window = weekly.report_window(date.fromisoformat(WEEK), NOW)
    start = window["start"]
    rows = [{"observed_at": start}, {"observed_at": start + 1}, {"observed_at": start + 2 * 86400}]
    conn = _CoverageConnection()
    result = weekly._day_coverage(conn, window, rows, False)
    assert conn.queries == []
    assert [day["evidence_count"] for day in result] == [2, 0, 1, 0, 0, 0, 0]
    assert [day["has_evidence"] for day in result] == [True, False, True, False, False, False, False]
    assert all(not day["partial"] for day in result)


def test_truncated_week_still_queries_unread_days_without_inventing_counts():
    window = weekly.report_window(date.fromisoformat(WEEK), NOW)
    rows = [{"observed_at": window["start"]}]
    conn = _CoverageConnection([1, 1, 0, 1, 0, 0, 1])
    result = weekly._day_coverage(conn, window, rows, True)
    assert len(conn.queries) == 7
    assert all("SELECT EXISTS" in sql for sql, _ in conn.queries)
    assert [day["has_evidence"] for day in result] == [True, True, False, True, False, False, True]
    assert all(day["evidence_count"] is None for day in result)
    for index, (_, args) in enumerate(conn.queries):
        lower = window["start"] + index * 86400
        assert args[:2] == (lower, lower + 86399)


def test_partial_day_coverage_keeps_cutoff_and_empty_day_semantics():
    monday = weekly.report_window(date.fromisoformat(WEEK), NOW)["start"]
    now = monday + 2 * 86400 + 3600
    window = weekly.report_window(date.fromisoformat(WEEK), now)
    rows = [{"observed_at": monday}, {"observed_at": now}]
    result = weekly._day_coverage(_CoverageConnection(), window, rows, False)
    assert [day["date"] for day in result] == ["2026-10-05", "2026-10-06", "2026-10-07"]
    assert [day["evidence_count"] for day in result] == [1, 0, 1]
    assert [day["partial"] for day in result] == [False, False, True]


class _ProgressConnection:
    def __init__(self):
        self.handlers = []

    def set_progress_handler(self, callback, steps):
        self.handlers.append((callback, steps))


@pytest.mark.parametrize("fails", [False, True])
def test_weekly_builder_scopes_text_cache_and_cleans_up_on_failure(monkeypatch, fails):
    calls = _count_computations(monkeypatch, "normalized")
    conn = _ProgressConnection()

    def build(*args, **kwargs):
        assert _call("normalized") == _call("normalized")
        if fails:
            raise RuntimeError("report failure")
        return {"ready": True}

    monkeypatch.setattr(weekly, "_build_weekly_report", build)
    if fails:
        with pytest.raises(RuntimeError, match="report failure"):
            weekly.build_weekly_report(conn, date.fromisoformat(WEEK), NOW)
    else:
        assert weekly.build_weekly_report(conn, date.fromisoformat(WEEK), NOW) == {"ready": True}
    assert sum(calls.values()) == 1
    _call("normalized")
    assert sum(calls.values()) == 2
    assert weekly.MAX_REPORT_SECONDS == 5.0
    assert callable(conn.handlers[0][0]) and conn.handlers[0][1] == 1000
    assert conn.handlers[-1] == (None, 0)


def _add_article(database, title, index):
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    observed = NOW - 86400 + index
    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn, {"performance_test": index}, title=title, event_type="policy",
            direction="中性", priority=7.0, occurred_at=observed, now=observed,
        )
        upsert_event_evidence(
            conn, event_id, source_uri=f"https://news.example/performance/{index}",
            source_name="https://news.example/rss", claim_key=f"performance:{index}",
            claim_text=title, stance="supports", announcement_at=observed,
            observed_at=observed, now=observed,
        )


def test_next_request_rebuilds_report_and_sees_new_database_rows():
    import db.database as database

    database.init_db()
    _add_article(database, "甲公司获得项目订单", 1)
    before = weekly.load_weekly_report(WEEK, now=NOW)
    _add_article(database, "乙公司获得项目订单", 2)
    after = weekly.load_weekly_report(WEEK, now=NOW)
    def titles(report):
        return {item["title"] for block in report["blocks"] for item in block["entries"]}

    assert before["evidence_count"] == 1 and after["evidence_count"] == 2
    assert titles(before) == {"甲公司获得项目订单"}
    assert titles(after) == {"甲公司获得项目订单", "乙公司获得项目订单"}
