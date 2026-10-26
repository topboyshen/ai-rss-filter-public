"""Semantic-only frozen comparisons; these are not five-second timing claims.

The linked-history oracle keeps the .122 full-payload query independent of
candidate identity/payload shortcuts. The large whole-report comparison uses
an unbounded internal deadline only in this test, never the public builder.
"""
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly
from test_weekly_large_event_equivalence import old_report as old_report

BEFORE = int(datetime(2026, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
NOW = BEFORE + 8 * 86400
_COLUMNS = (
    "ev.evidence_id, ev.event_id, ev.source_uri, ev.source_name, ev.claim_text, "
    "ev.stance, ev.announcement_at, ev.observed_at"
)


def _old_evidence(row):
    return {
        "evidence_id": str(row["evidence_id"]), "event_id": str(row["event_id"]),
        "source_uri": str(row["source_uri"]), "source_name": str(row["source_name"] or ""),
        "claim_text": daily.plain_text_from_markup(row["claim_text"])[:1000],
        "stance": str(row["stance"]),
        "announcement_at": None if row["announcement_at"] is None else int(row["announcement_at"]),
        "observed_at": int(row["observed_at"]),
    }


def _old_linked_history(conn, members, before, *, cap, lookback_rows=None):
    """Frozen full-payload SQL and chunk-priority semantics, no candidate calls."""
    result = {}
    if not members:
        return result, False
    paths = daily._PUBLIC_SCORE_PATH_VALUES
    available = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - len(paths) - 2
    if available < 1:
        raise sqlite3.OperationalError("weekly report requires a larger SQLite variable limit")
    chunk_size = min(20000, available)
    upper = before - daily.REPEAT_LOOKBACK_SECONDS if lookback_rows is not None else before
    remaining = cap
    ordered_members = sorted(members)
    for offset in range(0, len(ordered_members), chunk_size):
        chunk = ordered_members[offset:offset + chunk_size]
        parameters = (*chunk, upper, *paths, remaining + 1)
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM event_evidence AS ev "
            "INDEXED BY idx_event_evidence_event_time "
            f"WHERE ev.event_id IN ({placeholders}) AND ev.mapping_id IS NULL "
            "AND ev.observed_at < ? AND NOT EXISTS ("
            "SELECT 1 FROM news_cache AS public_cache WHERE public_cache.link=ev.source_uri "
            "AND LOWER(TRIM(COALESCE(public_cache.score_path, ''))) NOT IN ("
            + ",".join("?" for _ in paths) + ")) "
            "ORDER BY ev.observed_at, ev.evidence_id LIMIT ?", parameters,
        ).fetchall()
        for raw in rows[:remaining]:
            item = _old_evidence(raw)
            result.setdefault(item["event_id"], []).append(item)
        if len(rows) > remaining:
            return result, True
        remaining -= len(rows)
    recent = sorted(
        (row for row in lookback_rows or [] if row["event_id"] in members),
        key=lambda row: (row["observed_at"], row["evidence_id"]),
    )
    for row in recent[:remaining]:
        result.setdefault(row["event_id"], []).append(row)
    return result, len(recent) > remaining


@pytest.fixture
def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE event_evidence (
            evidence_id TEXT PRIMARY KEY, event_id TEXT, source_uri TEXT, source_name TEXT,
            claim_text TEXT, stance TEXT, announcement_at INTEGER, observed_at INTEGER, mapping_id TEXT
        );
        CREATE INDEX idx_event_evidence_event_time ON event_evidence(event_id, announcement_at DESC);
        CREATE INDEX idx_event_evidence_unbound_observed ON event_evidence(observed_at, evidence_id)
            WHERE mapping_id IS NULL;
        CREATE INDEX idx_event_evidence_unbound_event_observed
            ON event_evidence(event_id, observed_at, evidence_id) WHERE mapping_id IS NULL;
        CREATE TABLE news_cache (link TEXT, score_path TEXT, raw_title TEXT, title TEXT);
        CREATE INDEX idx_test_cache_link ON news_cache(link);
    """)
    yield conn
    conn.close()


def _insert(conn, identity, member, observed, *, claim="历史原文", name="媒体", stance="supports",
            mapping=None, announcement=None, uri=None):
    conn.execute("INSERT INTO event_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        identity, member, uri or f"https://news.test/{identity}", name, claim,
        stance, announcement, observed, mapping,
    ))


def _compare(conn, members, monkeypatch, *, cap=60000, lookback_rows=None):
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", cap)
    expected = _old_linked_history(conn, members, BEFORE, cap=cap, lookback_rows=lookback_rows)
    actual = weekly._linked_history(conn, members, BEFORE, lookback_rows=lookback_rows)
    assert actual == expected
    assert list(actual[0]) == list(expected[0])  # Dictionary insertion order is also stable.
    return actual


@pytest.mark.parametrize("member_chunk", [1, 2, 3, 20])
@pytest.mark.parametrize("cap", [1, 3, 4, 7, 9])
@pytest.mark.parametrize("complete", [False, True])
def test_multi_member_ties_and_older_before_recent_cap_are_frozen(
    connection, monkeypatch, member_chunk, cap, complete,
):
    conn = connection
    boundary = BEFORE - daily.REPEAT_LOOKBACK_SECONDS
    for number, member in reversed(list(enumerate(("a", "b", "c")))):
        for suffix, when in (("old-z", boundary - 1), ("old-a", boundary - 1), ("recent", BEFORE - 1)):
            _insert(conn, f"{suffix}-{2-number}", member, when,
                    announcement=BEFORE + number if suffix == "old-a" else None)
    _insert(conn, "unrelated-earliest", "other", boundary - 9999)
    recent = [_old_evidence(row) for row in conn.execute(
        f"SELECT {_COLUMNS} FROM event_evidence AS ev WHERE observed_at >= ? "
        "AND observed_at < ? ORDER BY observed_at DESC, evidence_id DESC", (boundary, BEFORE),
    )]
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                  len(daily._PUBLIC_SCORE_PATH_VALUES) + 2 + member_chunk)
    history, cut = _compare(conn, {"c", "a", "b"}, monkeypatch, cap=cap,
                            lookback_rows=recent if complete else None)
    assert sum(map(len, history.values())) == min(cap, 9)
    assert cut is (cap < 9)
    if member_chunk == 1 and cap == 1:
        assert list(history) == ["a"]  # Preserve chunk priority, not global oldest member.


@pytest.mark.parametrize("claim,name", [
    (None, None), ("", ""), ("\t\u3000\xa0", "  "),
    ("\x1c甲公司\x85利润\u2028≤５亿元\u2029", None),
    ("<b>甲公司</b>&nbsp;增长&minus;5％", "媒体"),
    ("&amp;amp;lt;i&amp;amp;gt;公司披露", "http://rsshub:1200/wallstreetcn/live"),
    ("甲" * 998 + "\u0301" * 5 + "末尾", "https://example.test/feed"),
    ("甲公司项目明细" * 400, "https://example.test/feed"),
])
def test_payload_null_empty_markup_unicode_and_long_text_are_exact(connection, monkeypatch, claim, name):
    _insert(connection, "z", "a", BEFORE - 1, claim=claim, name=name)
    _insert(connection, "a", "b", BEFORE - 1, claim="公司金额≥5亿元", stance="contradicts")
    result, cut = _compare(connection, {"a", "b"}, monkeypatch)
    assert not cut
    assert all(len(item["claim_text"]) <= 1000 for rows in result.values() for item in rows)


@pytest.mark.parametrize("mode", ["mapped", "null-path", "empty-path", "dual-cache", "future", "raw-title"])
def test_exclusions_never_borrow_cached_raw_title_or_other_root(connection, monkeypatch, mode):
    _insert(connection, "good", "a", BEFORE - 2, claim="甲公司原文支持项目")
    _insert(connection, "other", "unrelated", BEFORE - 9, claim="甲公司原文支持项目")
    _insert(connection, "candidate", "b", BEFORE if mode == "future" else BEFORE - 1,
            mapping="mapping-id" if mode == "mapped" else None, claim="乙公司原文否认项目")
    if mode in {"null-path", "empty-path", "dual-cache", "raw-title"}:
        paths = {"null-path": [None], "empty-path": [""],
                 "dual-cache": ["llm", "quality_quarantine"], "raw-title": ["llm"]}[mode]
        connection.executemany("INSERT INTO news_cache VALUES (?, ?, ?, ?)", [
            ("https://news.test/candidate", path, "甲公司原文支持项目", "模型生成标题") for path in paths
        ])
    history, cut = _compare(connection, {"a", "b"}, monkeypatch)
    ids = {item["evidence_id"] for rows in history.values() for item in rows}
    assert ids == ({"good", "candidate"} if mode == "raw-title" else {"good"})
    if mode == "raw-title":
        assert history["b"][0]["claim_text"] == "乙公司原文否认项目"
    assert not cut


@pytest.mark.parametrize("count", [59999, 60000, 60001])
def test_real_sixty_thousand_boundary_is_not_replaced_by_small_cap(connection, monkeypatch, count):
    connection.executemany("INSERT INTO event_evidence VALUES (?, 'a', ?, '', ?, 'supports', NULL, ?, NULL)", (
        (f"{i:05}", f"https://news.test/{i}", f"合成历史{i}金额≤5亿元", BEFORE - 1)
        for i in reversed(range(count))
    ))
    history, cut = _compare(connection, {"a"}, monkeypatch)
    assert len(history["a"]) == min(count, 60000)
    assert history["a"][0]["evidence_id"] == "00000"
    assert history["a"][-1]["evidence_id"] == f"{min(count, 60000)-1:05}"
    assert cut is (count > 60000)


def _semantic_report(module, conn):
    """No time claim: test internal full semantics without public deadline."""
    with module.daily._report_text_cache():
        return module._build_weekly_report(conn, date(2026, 10, 5), NOW, float("inf"))


def test_public_builder_still_arms_and_clears_exact_five_second_sql_budget(monkeypatch):
    class ProgressConnection:
        def __init__(self):
            self.handlers = []

        def set_progress_handler(self, handler, steps):
            self.handlers.append((handler, steps))

    clock = [100.0]
    conn = ProgressConnection()
    monkeypatch.setattr(weekly.time, "monotonic", lambda: clock[0])

    def build(_conn, _week, _now, deadline, **kwargs):
        assert deadline == 105.0
        handler, steps = conn.handlers[-1]
        assert steps == 1000
        clock[0] = 104.999
        assert handler() == 0
        clock[0] = 105.0
        assert handler() == 1
        clock[0] = 100.0
        return {"bounded": True}

    monkeypatch.setattr(weekly, "_build_weekly_report", build)
    assert weekly.build_weekly_report(conn, date(2026, 10, 5), NOW) == {"bounded": True}
    assert conn.handlers[-1] == (None, 0)


def test_sixty_thousand_long_history_full_report_equals_frozen_oracle(old_report):
    import db.database as database
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    database.init_db()
    old = BEFORE - daily.REPEAT_LOOKBACK_SECONDS - 60001
    with database.get_connection() as conn:
        owner = resolve_or_create_event(
            conn, {"semantic-sixty-thousand": "root"}, title="甲公司项目最新进度", event_type="policy",
            priority=9, occurred_at=old, now=old,
        )
        conn.executemany(
            "INSERT INTO event_evidence (evidence_id,event_id,source_uri,source_name,claim_key,claim_text,"
            "stance,observed_at,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, 'supports', ?, ?, ?)",
            ((f"{i:08x}-0000-4000-8000-000000000000", owner, f"https://history.test/{i}",
              "https://history.test/feed", f"history:{i}", f"甲公司历史明细{i}，" + "项目描述" * 100,
              old + i, old + i, old + i) for i in reversed(range(60000))),
        )
        upsert_event_evidence(
            conn, owner, source_uri="https://news.test/current", claim_key="current",
            claim_text="甲公司澄清公告：未参与境外收购", stance="contradicts", observed_at=BEFORE + 1,
            now=BEFORE + 1,
        )
        conn.commit()
    with database.get_read_only_connection() as conn:
        before = conn.total_changes
        expected = _semantic_report(old_report, conn)
        actual = _semantic_report(weekly, conn)
        assert actual == expected  # Every nested source, note, count, company and page field.
        assert conn.total_changes == before == 0
    assert weekly.MAX_REPORT_SECONDS == old_report.MAX_REPORT_SECONDS == 5.0
    corrections = next(block for block in actual["blocks"] if block["key"] == "correction")
    assert corrections["total"] == 1
    item, = corrections["entries"]
    assert item["correction_kind"] == "denial"
    assert "60000" in item["change"]
