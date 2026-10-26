"""Public event pages must not scan unrelated event evidence."""

from contextlib import contextmanager
import sqlite3
import time

import pytest

from db import database


@pytest.fixture
def event_database(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE news_cache (link TEXT PRIMARY KEY, score_path TEXT);"
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, title TEXT, priority REAL, "
        "event_type TEXT, direction TEXT, first_seen_at INTEGER, last_seen_at INTEGER, "
        "merge_state TEXT, merged_into TEXT);"
        "CREATE INDEX idx_events_merge_target ON events(merged_into) "
        "WHERE merged_into IS NOT NULL;"
        "CREATE TABLE event_evidence (evidence_id TEXT PRIMARY KEY, event_id TEXT, "
        "source_uri TEXT, source_name TEXT, excerpt TEXT, snapshot_text TEXT, "
        "reliability REAL, observed_at INTEGER, announcement_at INTEGER);"
        "CREATE INDEX idx_event_evidence_event_time "
        "ON event_evidence(event_id, announcement_at DESC);"
    )

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(database, "get_connection", connection)
    try:
        yield conn
    finally:
        conn.close()


def _measure_work(conn, read):
    """Count VM work, not wall time or SQLite-version-specific plan wording."""
    blocks = 0

    def progress():
        nonlocal blocks
        blocks += 1
        # Bound a regression's runtime too: abort after ten million VM steps.
        return int(blocks >= 100_000)

    conn.set_progress_handler(progress, 100)
    try:
        result = read()
    finally:
        conn.set_progress_handler(None, 0)
    return result, blocks * 100


def _add_unrelated_evidence(conn, count=20_000):
    conn.executemany(
        "INSERT INTO event_evidence VALUES (?, 'old', ?, 'old wire', 'old', '', 1, 1, 1)",
        [(f"old-{i}", f"https://wire.test/old/{i}") for i in range(count)],
    )


def test_public_event_evidence_work_is_bounded_by_the_selected_page(event_database):
    now = int(time.time()) - 60
    conn = event_database
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, ?, 'policy', '看多', ?, ?, ?, ?)",
        [
            (f"event-{i}", f"Event {i}", 10 - i, now, now, "active", None)
            for i in range(1, 6)
        ] + [
            ("child", "Merged event", 1, now, now, "merged", "event-1"),
            ("old", "Old event", 10, 1, 1, "active", None),
        ],
    )
    evidence_rows = []
    for i in range(1, 6):
        for kind, reliability in [("public", 0.8), ("quarantined", 0.99)]:
            evidence_rows.append((
                f"evidence-{i}-{kind}", f"event-{i}",
                f"https://wire.test/{i}/{kind}", kind, kind, "",
                reliability, now, now,
            ))
    evidence_rows.append((
        "merged-evidence", "child", "https://wire.test/child", "second wire",
        "merged public evidence", "", 0.9, now, now,
    ))
    conn.executemany("INSERT INTO event_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", evidence_rows)
    conn.executemany(
        "INSERT INTO news_cache VALUES (?, ?)",
        [
            (row[2], "quality_quarantine" if row[3] == "quarantined" else "llm")
            for row in evidence_rows
        ],
    )

    statements = []

    def read():
        return database.get_events(cfg={}, hours=24, limit=5)

    conn.set_trace_callback(statements.append)
    try:
        initial_rows, initial_steps = _measure_work(conn, read)
    finally:
        conn.set_trace_callback(None)
    _add_unrelated_evidence(conn)
    rows, grown_steps = _measure_work(conn, read)
    sql = next(statement for statement in statements if statement.startswith("WITH RECURSIVE"))
    previous_sql = sql.replace(
        "event_group CROSS JOIN event_evidence", "event_group JOIN event_evidence"
    )

    previous_rows, _ = _measure_work(conn, lambda: list(conn.execute(previous_sql)))
    assert rows == initial_rows == previous_rows
    assert len(rows) == 5
    assert rows[0][2:4] == ("https://wire.test/child", "merged public evidence")
    assert rows[0][6] == 2
    assert all("quarantined" not in row[2] for row in rows)
    # Adding irrelevant evidence must not add work proportional to its size.
    assert grown_steps <= initial_steps + 1_000, (initial_steps, grown_steps)


@pytest.mark.parametrize(
    ("query", "expected_limit"),
    [("", 30), ("?limit=50", 50), ("?limit=999", 50)],
    ids=["route-default-30", "maximum-50", "oversize-clamped-to-50"],
)
def test_public_event_route_page_boundaries_remain_bounded(
    event_database, monkeypatch, query, expected_limit
):
    from flask import Flask
    from routes import feed_routes, security

    conn = event_database
    now = int(time.time()) - 60
    # More eligible events than both page lengths makes truncation observable.
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, ?, 'policy', '看多', ?, ?, 'active', NULL)",
        [
            (f"event-{i:03}", f"Event {i}", 10 - i / 100, now, now)
            for i in range(1, 66)
        ] + [("old", "Old event", 10, 1, 1)],
    )
    conn.executemany(
        "INSERT INTO event_evidence VALUES (?, ?, ?, 'wire', ?, '', 0.8, ?, ?)",
        [
            (f"evidence-{i:03}", f"event-{i:03}", f"https://wire.test/{i}",
             f"Summary {i}", now, now)
            for i in range(1, 66)
        ],
    )
    monkeypatch.setattr(feed_routes, "_cfg", lambda: {})
    monkeypatch.setattr(security, "_rate_limit_store", {})
    monkeypatch.delenv("AI_FILTER_TRUSTED_PROXY_HOST", raising=False)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(feed_routes.feed_bp)
    with app.test_client() as client:
        def read():
            return client.get("/events/json" + query)

        initial, initial_steps = _measure_work(conn, read)
        _add_unrelated_evidence(conn)
        grown, grown_steps = _measure_work(conn, read)

    assert initial.status_code == grown.status_code == 200
    initial_events = initial.get_json()["events"]
    assert grown.get_json()["events"] == initial_events
    assert [event["event_id"] for event in initial_events] == [
        f"event-{i:03}" for i in range(1, expected_limit + 1)
    ]
    assert all(event["source_count"] == 1 for event in initial_events)
    assert grown_steps <= initial_steps + 1_000, (initial_steps, grown_steps)


def test_large_merged_event_preserves_all_public_descendant_evidence(event_database):
    conn = event_database
    now = int(time.time()) - 60
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, ?, 'policy', '看多', ?, ?, 'active', NULL)",
        [("root", "Merged root", 9, now, now), ("old", "Old event", 10, 1, 1)],
    )
    public_evidence = []

    def add_descendants(start, end):
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, 8, 'policy', '看多', ?, ?, 'merged', ?)",
            [
                (f"child-{i}", f"Child {i}", now, now,
                 "root" if i // 2 == 0 else f"child-{i // 2}")
                for i in range(start, end + 1)
            ],
        )
        evidence_rows = []
        cache_rows = []
        for i in range(start, end + 1):
            for slot in range(8):
                source_uri = f"https://wire.test/child/{i}/{slot}"
                source_name = "" if slot == 1 else f"shared-wire-{slot % 3}"
                excerpt = "" if slot == 1 else f"Excerpt {i}/{slot}"
                snapshot = f"Historical snapshot {i}/{slot}"
                reliability = 1.0 if slot == 2 else 0.8
                if i == 500 and slot == 1:
                    # The canonical source lies on a leaf, uses snapshot fallback,
                    # and must still obey the public 8,000-character summary cap.
                    reliability = 0.99
                    snapshot = "Leaf evidence " + "x" * 9_000
                evidence = (
                    f"evidence-{i:04}-{slot}", f"child-{i}", source_uri,
                    source_name, excerpt, snapshot, reliability, now + i, now + i,
                )
                evidence_rows.append(evidence)
                if slot != 2:
                    public_evidence.append(evidence)
                # Historical sources may have no news_cache row. Quarantined
                # evidence must never supply the summary or source count.
                if slot != 1:
                    cache_rows.append((
                        source_uri, "quality_quarantine" if slot == 2 else "llm"
                    ))
        conn.executemany(
            "INSERT INTO event_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            evidence_rows,
        )
        conn.executemany("INSERT INTO news_cache VALUES (?, ?)", cache_rows)

    def expected_root():
        canonical = min(public_evidence, key=lambda row: (-row[6], row[7], row[0]))
        source_count = len({(row[3] or row[2]).strip() for row in public_evidence})
        return (
            "root", "Merged root", canonical[2], (canonical[4] or canonical[5])[:8_000],
            9.0, "policy", source_count, now, now, "看多",
        )

    def read():
        return database.get_events(cfg={}, hours=24, limit=50)

    add_descendants(1, 250)
    small_rows, small_steps = _measure_work(conn, read)
    assert small_rows == [expected_root()]
    add_descendants(251, 500)
    large_rows, large_steps = _measure_work(conn, read)
    assert large_rows == [expected_root()]
    assert large_rows[0][2] == "https://wire.test/child/500/1"
    assert len(large_rows[0][3]) == 8_000
    assert large_rows[0][6] == 503
    # Doubling members/evidence may include sorting growth, but must not
    # introduce a per-descendant scan over the complete evidence collection.
    assert large_steps <= small_steps * 3 + 5_000, (small_steps, large_steps)
    _add_unrelated_evidence(conn)
    with_unrelated, unrelated_steps = _measure_work(conn, read)
    assert with_unrelated == large_rows
    assert unrelated_steps <= large_steps + 1_000, (large_steps, unrelated_steps)
