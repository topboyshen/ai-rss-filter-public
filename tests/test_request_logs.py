from __future__ import annotations

from db import database
from db.request_logs import get_request_logs, log_request


def _row(
    ts: int,
    url: str,
    *,
    total: int = 3,
    passed: int = 1,
    error: str | None = None,
):
    log_request(
        timestamp=ts,
        url=url,
        total=total,
        passed=passed,
        cache_hits=2,
        similarity_hits=0,
        blacklist_hits=1,
        fast_pass_hits=1,
        api_calls=4,
        duration_ms=12,
        error=error,
    )


def test_log_request_round_trip_newest_first(isolate_ai_filter_db):
    database.init_db()
    _row(100, "https://a.example/feed")
    _row(200, "https://b.example/feed", total=9, passed=2, error="timeout")

    rows = get_request_logs(limit=10)
    assert [row[0] for row in rows] == [200, 100]
    newest = rows[0]
    assert newest == (
        200,
        "https://b.example/feed",
        9,
        2,
        2,
        0,
        1,
        1,
        4,
        12,
        "timeout",
    )


def test_get_request_logs_honors_limit(isolate_ai_filter_db):
    database.init_db()
    for ts in (1, 2, 3, 4, 5):
        _row(ts, f"https://feed.example/{ts}")
    rows = get_request_logs(limit=2)
    assert [row[0] for row in rows] == [5, 4]


def test_database_facade_matches_module(isolate_ai_filter_db):
    database.init_db()
    database.log_request(
        timestamp=50,
        url="https://facade.example/feed",
        total=1,
        passed=1,
        cache_hits=0,
        similarity_hits=0,
        blacklist_hits=0,
        fast_pass_hits=0,
        api_calls=0,
        duration_ms=3,
    )
    via_module = get_request_logs(limit=1)
    via_db = database.DB.get_request_logs(limit=1)
    via_pkg = database.get_request_logs(limit=1)
    assert via_module == via_db == via_pkg
    assert via_module[0][1] == "https://facade.example/feed"
