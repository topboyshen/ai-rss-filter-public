"""Indexed linked-history reads keep the old report semantics without old scans."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services import event_daily_report as daily
from services import event_weekly_report as weekly

BEFORE = int(datetime(2026, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
NOW = BEFORE + 8 * 86400
NO_CACHE = object()


class _OldHistoryIndex:
    """Run the query through the event/publication index used by .122."""

    index_name = "idx_event_evidence_event_time"

    def __init__(self, connection):
        self.connection = connection

    def getlimit(self, category):
        return self.connection.getlimit(category)

    def execute(self, sql, parameters=()):
        sql = sql.replace(
            "INDEXED BY idx_event_evidence_unbound_event_observed",
            f"INDEXED BY {self.index_name}",
        )
        return self.connection.execute(sql, parameters)


class _TimeFirstHistoryIndex(_OldHistoryIndex):
    """Retain the unrelated-history scan comparator predating .122."""

    index_name = "idx_event_evidence_unbound_observed"


@pytest.fixture
def history_connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE event_evidence (
            evidence_id TEXT PRIMARY KEY, event_id TEXT, source_uri TEXT,
            source_name TEXT, claim_text TEXT, stance TEXT,
            announcement_at INTEGER, observed_at INTEGER, mapping_id TEXT
        );
        CREATE INDEX idx_event_evidence_event_time
            ON event_evidence(event_id, announcement_at DESC);
        CREATE INDEX idx_event_evidence_unbound_observed
            ON event_evidence(observed_at, evidence_id) WHERE mapping_id IS NULL;
        CREATE INDEX idx_event_evidence_unbound_event_observed
            ON event_evidence(event_id, observed_at, evidence_id) WHERE mapping_id IS NULL;
        CREATE TABLE news_cache (link TEXT, score_path TEXT);
        CREATE INDEX idx_test_cache_link ON news_cache(link);
    """)
    yield connection
    connection.close()


def _add(connection, evidence, event, observed, *, mapping=None, announcement=None,
         stance="supports", cache_path=NO_CACHE, title=None):
    uri = f"https://news.example/{evidence}"
    connection.execute(
        "INSERT INTO event_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence, event, uri, "https://news.example/rss", title or evidence, stance,
         announcement, observed, mapping),
    )
    if cache_path is not NO_CACHE:
        connection.execute("INSERT INTO news_cache VALUES (?, ?)", (uri, cache_path))


def _ids(history):
    return {event: [row["evidence_id"] for row in rows] for event, rows in history.items()}


def _assert_old_equal(connection, members, *, lookback=None):
    expected = weekly._linked_history(
        _OldHistoryIndex(connection), members, BEFORE, lookback_rows=lookback,
    )
    actual = weekly._linked_history(connection, members, BEFORE, lookback_rows=lookback)
    assert actual == expected
    return actual


@pytest.mark.parametrize("path", daily._PUBLIC_SCORE_PATH_VALUES)
def test_public_cached_paths_and_uncached_sources_remain_visible(history_connection, path):
    conn = history_connection
    _add(conn, "public", "selected", BEFORE - 10, cache_path=f"  {path.upper()}  ")
    _add(conn, "uncached", "selected", BEFORE - 9)
    _add(conn, "unrelated", "other", BEFORE - 8, cache_path=path)
    history, cut = _assert_old_equal(conn, {"selected"})
    assert _ids(history) == {"selected": ["public", "uncached"]}
    assert not cut


@pytest.mark.parametrize("path", [
    "llm_safe_neutral_grounding", "stage2_safe_neutral_initial", "quality_quarantine",
    "unknown", "", "   ", None,
])
def test_nonpublic_or_mapped_history_cannot_enter_report(history_connection, path):
    conn = history_connection
    _add(conn, "private", "selected", BEFORE - 30, cache_path=path)
    _add(conn, "mapped", "selected", BEFORE - 20, mapping="mapping-id", cache_path="llm")
    _add(conn, "visible", "selected", BEFORE - 10, cache_path="llm")
    history, cut = _assert_old_equal(conn, {"selected"})
    assert _ids(history) == {"selected": ["visible"]}
    assert not cut


def test_any_nonpublic_cache_match_excludes_source_even_with_public_match(history_connection):
    conn = history_connection
    _add(conn, "mixed", "selected", BEFORE - 20, cache_path="llm")
    conn.execute("INSERT INTO news_cache VALUES (?, ?)",
                 ("https://news.example/mixed", "quality_quarantine"))
    _add(conn, "visible", "selected", BEFORE - 10)
    history, cut = _assert_old_equal(conn, {"selected"})
    assert _ids(history) == {"selected": ["visible"]}
    assert not cut


@pytest.mark.parametrize("complete", [False, True])
def test_observed_boundaries_not_publication_time_determine_history(history_connection, complete):
    conn = history_connection
    boundary = BEFORE - daily.REPEAT_LOOKBACK_SECONDS
    _add(conn, "old", "selected", boundary - 1, announcement=BEFORE + 999)
    _add(conn, "boundary", "selected", boundary, announcement=None)
    _add(conn, "recent", "selected", BEFORE - 1, announcement=boundary - 999)
    _add(conn, "at-week-start", "selected", BEFORE, announcement=boundary - 999)
    _add(conn, "future", "selected", BEFORE + 1, announcement=boundary - 999)
    recent, cut = daily._load_window_evidence(conn, boundary, BEFORE - 1, 100)
    assert not cut
    history, cut = _assert_old_equal(conn, {"selected"}, lookback=recent if complete else None)
    assert _ids(history) == {"selected": ["old", "boundary", "recent"]}
    assert not cut


def test_complete_empty_lookback_is_not_treated_like_incomplete_lookback(history_connection):
    conn = history_connection
    _add(conn, "old", "selected", BEFORE - daily.REPEAT_LOOKBACK_SECONDS - 1)
    _add(conn, "recent", "selected", BEFORE - 1)
    empty, empty_cut = _assert_old_equal(conn, {"selected"}, lookback=[])
    unknown, unknown_cut = _assert_old_equal(conn, {"selected"}, lookback=None)
    assert _ids(empty) == {"selected": ["old"]}
    assert _ids(unknown) == {"selected": ["old", "recent"]}
    assert not empty_cut and not unknown_cut


@pytest.mark.parametrize("available", [1, 2, 20])
@pytest.mark.parametrize("cap", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("complete", [False, True])
def test_shared_cap_and_batch_priority_are_exactly_preserved(
    history_connection, monkeypatch, available, cap, complete,
):
    conn = history_connection
    boundary = BEFORE - daily.REPEAT_LOOKBACK_SECONDS
    _add(conn, "a-old", "a", boundary - 10)
    _add(conn, "b-old", "b", boundary - 30)
    _add(conn, "c-old", "c", boundary - 20)
    _add(conn, "a-recent", "a", BEFORE - 10)
    _add(conn, "b-recent", "b", BEFORE - 30)
    _add(conn, "c-recent", "c", BEFORE - 20)
    recent, cut = daily._load_window_evidence(conn, boundary, BEFORE - 1, 100)
    assert not cut
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", cap)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                  len(daily._PUBLIC_SCORE_PATH_VALUES) + 2 + available)
    history, cut = _assert_old_equal(conn, {"c", "a", "b"}, lookback=recent if complete else None)
    assert sum(map(len, history.values())) == cap
    assert cut is (cap < 6)
    if available == 1 and cap == 1:
        assert _ids(history) == {"a": ["a-old"]}  # Batch priority, not oldest member.


def test_equal_observed_time_orders_by_evidence_id_not_announcement(history_connection):
    conn = history_connection
    _add(conn, "z", "selected", BEFORE - 1, announcement=1)
    _add(conn, "a", "selected", BEFORE - 1, announcement=BEFORE + 1)
    _add(conn, "m", "selected", BEFORE - 1, announcement=None)
    history, cut = _assert_old_equal(conn, {"selected"})
    assert _ids(history) == {"selected": ["a", "m", "z"]}
    assert not cut


def test_wide_payloads_never_enter_temporary_sort(history_connection, monkeypatch):
    conn = history_connection
    for index in range(31):
        _add(conn, f"long-{index:02}", "selected" if index % 2 else "second", BEFORE - index - 1,
             title=f"公司{index}更正金额≤1.5亿元" + "原始来源文本" * 900,
             announcement=index)
    # The connection limit independently constrains both member selection and
    # the second payload fetch; the latter must not rely on SQLite's default.
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, len(daily._PUBLIC_SCORE_PATH_VALUES) + 4)
    expected = conn.execute(
        f"SELECT {daily._EVIDENCE_COLUMNS} FROM event_evidence AS ev "
        "INDEXED BY idx_event_evidence_event_time "
        "WHERE ev.event_id IN ('selected', 'second') AND ev.mapping_id IS NULL AND ev.observed_at < ? "
        f"AND {daily._public_cache_evidence_predicate('ev')} "
        "ORDER BY ev.observed_at, ev.evidence_id LIMIT ?",
        (BEFORE, *daily._PUBLIC_SCORE_PATH_VALUES, 21),
    ).fetchall()
    statements = []
    conn.set_trace_callback(statements.append)
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", 20)
    actual, cut = weekly._linked_history(conn, {"selected", "second"}, BEFORE)
    grouped = {}
    for row in expected[:20]:
        grouped.setdefault(row["event_id"], []).append(daily._evidence_dict(row))
    assert actual == grouped
    assert cut
    sorters = [sql for sql in statements if "ORDER BY" in sql]
    assert len(sorters) == 1
    projection = sorters[0].split("FROM", 1)[0]
    assert "ev.rowid" in projection and "ev.evidence_id" in projection
    assert "ev.claim_text" not in projection and "ev.source_uri" not in projection
    payloads = [sql for sql in statements if "WHERE ev.rowid IN" in sql]
    assert len(payloads) > 1 and all("ORDER BY" not in sql for sql in payloads)


@pytest.mark.parametrize("member", ["selected", "17"])
@pytest.mark.parametrize("cap", [1, 3, 5])
def test_single_member_uses_ordered_index_without_wide_sort_or_payload_refetch(
    history_connection, monkeypatch, member, cap,
):
    conn = history_connection
    for evidence in ("z", "a", "m"):
        _add(conn, evidence, member, BEFORE - 10, cache_path="llm",
             title=f"{evidence}公司更正金额≤1.5亿元" + "原始来源文本" * 900)
    _add(conn, "mapped", member, BEFORE - 30, mapping="mapping-id")
    _add(conn, "private", member, BEFORE - 20, cache_path="quality_quarantine")
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, len(daily._PUBLIC_SCORE_PATH_VALUES) + 3)
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", cap)
    expected = weekly._linked_history(_OldHistoryIndex(conn), {member}, BEFORE)
    boxed = []
    convert = weekly._history_evidence_dict

    def record(row):
        boxed.append(row["evidence_id"])
        return convert(row)

    monkeypatch.setattr(weekly, "_history_evidence_dict", record)
    statements = []
    conn.set_trace_callback(statements.append)
    actual = weekly._linked_history(conn, {member}, BEFORE)
    conn.set_trace_callback(None)
    assert actual == expected
    assert _ids(actual[0]) == {member: ["a", "m", "z"][:cap]}
    assert actual[1] is (cap < 3)
    # The overflow row is queried only as a sentinel, never cleaned or boxed.
    assert boxed == ["a", "m", "z"][:cap]
    ordered = [sql for sql in statements if "ORDER BY" in sql]
    assert len(ordered) == 1 and "ev.claim_text" in ordered[0].split("FROM", 1)[0]
    assert not any("WHERE ev.rowid IN" in sql for sql in statements)
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + ordered[0])]
    assert any("idx_event_evidence_unbound_event_observed" in detail
               and "event_id=? AND observed_at<?" in detail for detail in plan)
    assert any("SEARCH public_cache USING INDEX idx_test_cache_link (link=?)" in detail
               for detail in plan)
    assert not any("TEMP B-TREE" in detail for detail in plan)
    assert weekly.MAX_REPORT_SECONDS == 5.0


@pytest.mark.parametrize("last_has_row", [False, True])
def test_final_single_member_chunk_preserves_zero_remaining_cap(
    history_connection, monkeypatch, last_has_row,
):
    conn = history_connection
    for member in ("a", "b"):
        _add(conn, member, member, BEFORE - 10)
    if last_has_row:
        _add(conn, "c", "c", BEFORE - 20)
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", 2)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, len(daily._PUBLIC_SCORE_PATH_VALUES) + 4)
    convert, single = weekly._history_evidence_dict, weekly._single_member_history
    boxed, calls = [], []

    def record(row):
        boxed.append(row["evidence_id"])
        return convert(row)

    def track(connection, member, before, limit):
        calls.append((member, limit))
        return single(connection, member, before, limit)

    monkeypatch.setattr(weekly, "_history_evidence_dict", record)
    monkeypatch.setattr(weekly, "_single_member_history", track)
    history, cut = weekly._linked_history(conn, {"c", "a", "b"}, BEFORE)
    assert _ids(history) == {"a": ["a"], "b": ["b"]}
    assert cut is last_has_row
    assert boxed == ["a", "b"]
    assert calls == [("c", 0)]


def test_history_payload_plain_shortcut_preserves_fields_and_post_cleaning_limit(history_connection):
    conn = history_connection
    texts = [
        "\xa0\t公司\u2003披露  利润−５％\n", "  公司  " * 1200,
        "\x1c公司\x85利润\u2028增长\u2029５％\x1f", "甲" * 1000 + "\u0301末尾",
        "<em>Ａ公司</em>&nbsp;利润&minus;５％", "&amp;amp;amp;lt;b&amp;amp;amp;gt;公司",
        "公司<script>不应出现</script>披露项目", "公司披露<b 未闭合标签金额−5亿元",
    ]
    for index, text in enumerate(texts):
        _add(conn, f"payload-{index}", "selected", BEFORE - index - 1, title=text,
             announcement=None if index % 2 else BEFORE + index)
    expected = conn.execute(
        f"SELECT {daily._EVIDENCE_COLUMNS} FROM event_evidence AS ev "
        "ORDER BY ev.observed_at, ev.evidence_id",
    ).fetchall()
    identities = conn.execute(
        "SELECT evidence_id, observed_at, rowid FROM event_evidence ORDER BY rowid DESC",
    ).fetchall()
    actual = weekly._history_payloads(conn, identities)
    assert actual == [daily._evidence_dict(row) for row in expected]
    assert all(len(row["claim_text"]) <= 1000 for row in actual)
    for raw in expected:
        assert weekly._history_evidence_dict(raw) == daily._evidence_dict(raw)


def test_empty_members_do_not_execute_sql_and_tiny_variable_limit_still_fails(history_connection):
    conn = history_connection
    statements = []
    conn.set_trace_callback(statements.append)
    assert weekly._linked_history(conn, set(), BEFORE) == ({}, False)
    assert not statements
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, len(daily._PUBLIC_SCORE_PATH_VALUES) + 2)
    with pytest.raises(sqlite3.OperationalError, match="larger SQLite variable limit"):
        weekly._linked_history(conn, {"selected"}, BEFORE)
    assert not statements


def _vm_steps(connection, *, old=False, time_first=False, members=None):
    ticks = 0

    def progress():
        nonlocal ticks
        ticks += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        target = _OldHistoryIndex(connection) if old else connection
        if time_first:
            target = _TimeFirstHistoryIndex(connection)
        result = weekly._linked_history(target, members or {"selected"}, BEFORE)
    finally:
        connection.set_progress_handler(None, 0)
    return result, ticks


def test_unrelated_history_does_not_grow_query_work(history_connection):
    conn = history_connection
    _add(conn, "selected-old", "selected", BEFORE - 50)
    before, baseline = _vm_steps(conn)
    conn.executemany(
        "INSERT INTO event_evidence VALUES (?, ?, ?, '', ?, 'supports', 1, ?, NULL)",
        [(f"other-{i:05}", f"other-event-{i:05}", f"https://news.example/other/{i}",
          f"无关历史{i}", BEFORE - 100 - i) for i in range(6000)],
    )
    after, optimized = _vm_steps(conn)
    reference, old = _vm_steps(conn, time_first=True)
    assert before == after == reference
    assert optimized <= baseline * 2
    assert old > optimized * 20


@pytest.mark.parametrize("excluded", ["mapped", "future", "mixed"])
@pytest.mark.parametrize("members", [{"selected"}, {"selected", "second"}])
def test_mapped_and_future_rows_do_not_amplify_linked_history_work(
    history_connection, excluded, members,
):
    conn = history_connection
    for event in sorted(members):
        _add(conn, f"{event}-earlier", event, BEFORE - 20)
        _add(conn, f"{event}-later", event, BEFORE - 10, stance="contradicts")
        _add(conn, f"{event}-private", event, BEFORE - 5, cache_path="quality_quarantine")
    before, baseline = _vm_steps(conn, members=members)
    rows = []
    for index in range(6000):
        mapped = excluded == "mapped" or (excluded == "mixed" and index % 2 == 0)
        event = sorted(members)[index % len(members)]
        rows.append((f"excluded-{index}", event, f"https://news.example/excluded/{index}",
                     "excluded", BEFORE + index if not mapped else BEFORE - index - 1,
                     "mapping-id" if mapped else None))
    conn.executemany(
        "INSERT INTO event_evidence VALUES (?, ?, ?, '', ?, 'supports', NULL, ?, ?)", rows,
    )
    actual, optimized = _vm_steps(conn, members=members)
    reference, old = _vm_steps(conn, old=True, members=members)
    assert before == actual == reference
    assert not actual[1]
    assert optimized <= baseline * 2
    assert old > optimized * 20


def test_single_large_event_still_has_identical_bounded_oldest_first_result(
    history_connection, monkeypatch,
):
    conn = history_connection
    for index in range(1500):
        _add(conn, f"visible-{index:04}", "selected", BEFORE - 1500 + index,
             announcement=BEFORE + 1500 - index)
    monkeypatch.setattr(weekly, "MAX_LINKED_HISTORY_ROWS", 25)
    actual, optimized = _vm_steps(conn)
    reference, old = _vm_steps(conn, old=True)
    assert actual == reference
    assert _ids(actual[0]) == {"selected": [f"visible-{index:04}" for index in range(25)]}
    assert actual[1]
    assert old > optimized * 20


def test_history_index_is_time_bounded_and_cache_lookup_is_link_indexed(history_connection):
    conn = history_connection
    _add(conn, "public", "selected", BEFORE - 10, cache_path="llm")
    _add(conn, "mixed", "selected", BEFORE - 5, cache_path="llm")
    conn.execute("INSERT INTO news_cache VALUES (?, ?)",
                 ("https://news.example/mixed", "quality_quarantine"))
    baseline_result, baseline_work = _vm_steps(conn)
    conn.executemany("INSERT INTO news_cache VALUES (?, ?)", [
        (f"https://unrelated.example/{index}", "quality_quarantine") for index in range(6000)
    ])
    statements = []
    conn.set_trace_callback(statements.append)
    actual, work = _vm_steps(conn)
    conn.set_trace_callback(None)
    assert actual == baseline_result
    assert _ids(actual[0]) == {"selected": ["public"]}
    assert work <= baseline_work * 2
    sql = next(statement for statement in statements if "SELECT ev.evidence_id" in statement)
    plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("idx_event_evidence_unbound_event_observed" in detail
               and "event_id=? AND observed_at<?" in detail for detail in plan)
    assert any("SEARCH public_cache USING INDEX idx_test_cache_link (link=?)" in detail
               for detail in plan)
    assert not any("SCAN public_cache" in detail for detail in plan)


def test_history_requires_the_new_index_instead_of_silently_using_the_old_index(history_connection):
    conn = history_connection
    _add(conn, "old", "selected", BEFORE - 1)
    conn.execute("DROP INDEX idx_event_evidence_unbound_event_observed")
    with pytest.raises(sqlite3.OperationalError, match="idx_event_evidence_unbound_event_observed"):
        weekly._linked_history(conn, {"selected"}, BEFORE)


def _real_article(database, key, title, observed, *, priority=7):
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    with database.get_connection() as conn:
        event = resolve_or_create_event(
            conn, {"linked_query_test": key}, title=title, event_type="policy",
            direction="中性", priority=priority, occurred_at=observed, now=observed,
        )
        upsert_event_evidence(
            conn, event, source_uri=f"https://news.example/{key}/{observed}",
            source_name="https://news.example/rss", claim_key=f"query:{key}:{observed}",
            claim_text=title, stance="supports", observed_at=observed, now=observed,
        )
    return event


def _real_followup(database, event, title, observed, *, stance="supports"):
    from db.event_core import upsert_event_evidence

    with database.get_connection() as conn:
        return upsert_event_evidence(
            conn, event, source_uri=f"https://news.example/{event}/{observed}",
            source_name="https://news.example/rss", claim_key=f"query:{event}:{observed}",
            claim_text=title, stance=stance, observed_at=observed, now=observed,
        )


def _report_fixture(database):
    from db.event_core import merge_events

    database.init_db()
    old = BEFORE - daily.REPEAT_LOOKBACK_SECONDS - 100
    support = _real_article(database, "support", "甲公司获得项目订单", old)
    denial = _real_article(database, "denial", "甲公司否认获得项目订单", old + 1)
    with database.get_connection() as conn:
        merge_events(conn, support, denial, reason="query-regression", now=BEFORE - 1)
    _real_followup(database, denial, "甲公司披露补充细节", BEFORE + 10)
    repeat = _real_article(database, "repeat", "乙公司否认项目传闻", BEFORE - 86400)
    _real_followup(database, repeat, "乙公司否认项目传闻", BEFORE + 20)
    _real_article(database, "lookback-only", "仅作回看对照的独立报道", BEFORE - 86400 + 1)
    correction = _real_article(database, "correction", "丙公司项目正常推进", old + 2, priority=1)
    _real_followup(database, correction, "丙公司项目正常推进", BEFORE + 30, stance="contradicts")
    pending = _real_article(database, "pending", "丁公司计划建设工厂", BEFORE + 40)
    for index in range(23):
        _real_article(database, f"new-{index}", f"独立政策事项{index}", BEFORE + 50 + index)
    return {"advanced": denial, "correction": correction, "pending": pending}


@pytest.mark.parametrize("selection,page", [(None, None), ("new", 2), ("correction", 1)])
@pytest.mark.parametrize("lookback_limit", [1, weekly.MAX_LOOKBACK_EVIDENCE_ROWS])
def test_full_report_and_paging_match_old_query_with_root_and_correction_history(
    monkeypatch, selection, page, lookback_limit,
):
    import db.database as database

    expected_ids = _report_fixture(database)
    monkeypatch.setattr(weekly, "MAX_LOOKBACK_EVIDENCE_ROWS", lookback_limit)
    original = weekly._linked_history

    def reference(connection, *args, **kwargs):
        return original(_OldHistoryIndex(connection), *args, **kwargs)

    monkeypatch.setattr(weekly, "_linked_history", reference)
    expected = weekly.load_weekly_report("2026-10-05", now=NOW, block=selection, page=page)
    monkeypatch.setattr(weekly, "_linked_history", original)
    actual = weekly.load_weekly_report("2026-10-05", now=NOW, block=selection, page=page)
    assert actual == expected
    if selection is None:
        blocks = {block["key"]: block for block in actual["blocks"]}
        assert blocks["new"]["total"] == 23
        assert blocks["advanced"]["entries"][0]["event_id"] == expected_ids["advanced"]
        correction = blocks["correction"]["entries"][0]
        assert correction["event_id"] == expected_ids["correction"]
        assert correction["correction_kind"] == "denial"
        assert "此前有 1 条支持记录" in correction["change"]
        assert actual["repeat_event_count"] == 1
        assert blocks["pending"]["entries"][0]["event_id"] == expected_ids["pending"]
        assert actual["data_state"] == ("truncated" if lookback_limit == 1 else "incomplete")
    elif selection == "new":
        assert len(actual["blocks"][0]["entries"]) == 3


def test_query_read_is_still_readonly(history_connection):
    conn = history_connection
    _add(conn, "old", "selected", BEFORE - 1)
    statements = []
    conn.set_trace_callback(statements.append)
    conn.execute("PRAGMA query_only=ON")
    history, cut = weekly._linked_history(conn, {"selected"}, BEFORE)
    assert _ids(history) == {"selected": ["old"]} and not cut
    writes = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE")
    assert not any(sql.lstrip().upper().startswith(writes) for sql in statements)
