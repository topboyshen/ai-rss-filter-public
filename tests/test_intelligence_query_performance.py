"""Bound event-identity lookups without changing public feed selection."""

from contextlib import contextmanager
import sqlite3
import time
import uuid

import pytest

from services import intelligence_query


def _event_id(number):
    return str(uuid.UUID(int=number))


def _article(number, *, event_id=None, score=8.0):
    return (
        f"https://example.test/{number}",
        f"News {number}",
        f"News {number}",
        score,
        "policy",
        "logic",
        "summary",
        int(time.time()) - number,
        "https://example.test/feed",
        "neutral",
        _event_id(number) if event_id is None else event_id,
        1,
    )


@pytest.fixture
def event_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, version INTEGER, "
        "merge_state TEXT, merged_into TEXT)"
    )
    try:
        yield conn
    finally:
        conn.close()


def test_briefing_batches_event_resolution_before_returning_twelve_rows(
    event_connection, monkeypatch
):
    conn = event_connection
    conn.execute(
        "CREATE TABLE news_cache (link TEXT, title TEXT, raw_title TEXT, "
        "score REAL, tag TEXT, logic TEXT, summary TEXT, created_at INTEGER, "
        "feed_url TEXT, direction TEXT, event_id TEXT, event_version INTEGER, "
        "score_path TEXT)"
    )
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, 'active', NULL)",
        [(_event_id(number), 7) for number in range(1, 961)],
    )
    conn.executemany(
        "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
        [_article(number) for number in range(1, 961)],
    )

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(intelligence_query, "get_connection", connection)
    statements = []
    conn.set_trace_callback(statements.append)
    rows = intelligence_query.query_briefing_tuples(
        {"threshold": {"pass": 7}}, min_score=7, limit=12
    )

    assert [row[0] for row in rows] == [
        f"https://example.test/{number}" for number in range(1, 13)
    ]
    assert all(row[11] == 7 for row in rows)
    event_queries = [sql for sql in statements if "FROM events " in sql]
    assert len(event_queries) <= 2


def test_batched_resolution_preserves_merges_corrupt_references_and_legacy_rows(
    event_connection,
):
    conn = event_connection
    active_id, merged_id, broken_id, cycle_id = map(_event_id, range(1, 5))
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, ?, ?)",
        [
            (active_id, 9, "active", None),
            (merged_id, 2, "merged", active_id),
            (broken_id, 3, "active", active_id),
            (cycle_id, 4, "merged", cycle_id),
        ],
    )
    rows = [
        _article(1, event_id=active_id),
        _article(2, event_id=merged_id),
        _article(3, event_id=broken_id),
        _article(4, event_id=cycle_id),
        _article(5, event_id=_event_id(999)),
        _article(6, event_id=""),
    ]

    result = intelligence_query._rows_to_items(rows, conn)

    assert [(item.link, item.event_id, item.event_version) for item in result] == [
        ("https://example.test/1", active_id, 9),
        ("https://example.test/2", active_id, 9),
        ("https://example.test/6", "", 1),
    ]
    conn.execute("UPDATE events SET version = 10 WHERE event_id = ?", (active_id,))
    # A second request must see fresh versions; the cache is request-local.
    refreshed = intelligence_query._rows_to_items(rows, conn)
    assert [item.event_version for item in refreshed[:2]] == [10, 10]


def test_missing_event_schema_keeps_legacy_rows_and_hides_broken_references():
    with sqlite3.connect(":memory:") as conn:
        result = intelligence_query._rows_to_items(
            [_article(1), _article(2, event_id="")], conn
        )
    assert [item.link for item in result] == ["https://example.test/2"]
