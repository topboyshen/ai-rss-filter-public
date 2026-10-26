import hashlib
import json
import sqlite3
import uuid

import pytest

from db.event_core import (
    EVENT_MERGE_CONTRACT,
    EventCoreSchemaError,
    EventCoreConflictError,
    _DDL,
    _INDEX_DDL,
    _TRIGGER_DDL,
    _schema_is_current,
    canonical_event_group,
    canonical_event_id,
    init_event_core_schema,
    merge_events,
    reconcile_event_group_priorities,
    record_event_mapping_replay,
    resolve_or_create_event,
    upsert_event_evidence,
    upsert_event_outcome,
    upsert_event_security,
    upsert_event_similarity,
    upsert_security,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def _event(
    connection,
    key: str,
    *,
    title: str | None = None,
    now: int = 100,
    available_at: int | None = None,
) -> str:
    return resolve_or_create_event(
        connection,
        {"wire": key},
        title=title or f"event {key}",
        event_type="policy",
        direction="bullish",
        priority=8.0,
        occurred_at=90,
        available_at=available_at,
        strategy_version="strategy-v1",
        model_version="model-v1",
        now=now,
    )


def test_empty_schema_and_repeated_initialization_are_idempotent():
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    traced = []
    connection.set_trace_callback(traced.append)
    init_event_core_schema(connection)
    connection.set_trace_callback(None)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "events",
        "event_identities",
        "securities",
        "event_securities",
        "event_evidence",
        "event_mapping_replay_ledger",
        "event_mapping_replay_dirty_roots",
        "event_similarity_replay_dirty_roots",
        "event_outcomes",
        "event_similarities",
        "event_merge_history",
    }.issubset(tables)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    mutating = ("BEGIN IMMEDIATE", "CREATE ", "ALTER ", "DROP ", "UPDATE ", "INSERT ", "DELETE ")
    assert not [sql for sql in traced if sql.lstrip().upper().startswith(mutating)]
    connection.close()


def test_similarity_replay_upgrade_seeds_existing_edge_endpoints_once():
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    left = _event(connection, "similarity-upgrade-left")
    right = _event(connection, "similarity-upgrade-right")
    upsert_event_similarity(
        connection,
        left,
        right,
        similarity=0.8,
        method="event-graph-v1",
        model_version="deterministic-title-security-v1",
        rationale="升级前的当前事件图边。",
        now=200,
    )
    connection.execute("DROP TABLE event_similarity_replay_dirty_roots")
    connection.commit()

    init_event_core_schema(connection)

    assert connection.execute(
        "SELECT root_event_id, generation FROM "
        "event_similarity_replay_dirty_roots ORDER BY root_event_id"
    ).fetchall() == [(event_id, 1) for event_id in sorted((left, right))]
    connection.execute("DELETE FROM event_similarity_replay_dirty_roots")
    connection.commit()
    init_event_core_schema(connection)
    assert connection.execute(
        "SELECT COUNT(*) FROM event_similarity_replay_dirty_roots"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.parametrize(
    ("drop_sql", "replacement_sql", "object_type"),
    (
        (
            "DROP INDEX uq_event_securities_current",
            "CREATE INDEX uq_event_securities_current "
            "ON event_securities(event_id)",
            "index",
        ),
        (
            "DROP TRIGGER event_core_events_version_monotonic",
            "CREATE TRIGGER event_core_events_version_monotonic "
            "AFTER UPDATE OF version ON events BEGIN SELECT 1; END",
            "trigger",
        ),
    ),
)
def test_same_named_wrong_schema_object_cannot_pass_as_current(
    drop_sql, replacement_sql, object_type
):
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    connection.execute(drop_sql)
    connection.execute(replacement_sql)

    assert _schema_is_current(connection) is False
    with pytest.raises(EventCoreSchemaError, match=object_type):
        init_event_core_schema(connection)
    connection.close()


def test_split_foreign_keys_cannot_impersonate_required_composite_constraint():
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    connection.execute("DROP TABLE event_evidence")

    evidence_ddl = next(
        statement
        for statement in _DDL
        if "CREATE TABLE IF NOT EXISTS event_evidence" in statement
    )
    composite = """FOREIGN KEY (mapping_id, event_id, security_id, mapping_revision)
            REFERENCES event_securities(mapping_id, event_id, security_id, revision)
            ON DELETE RESTRICT"""
    split = """FOREIGN KEY (mapping_id) REFERENCES event_securities(mapping_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (event_id) REFERENCES event_securities(event_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (security_id) REFERENCES event_securities(security_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (mapping_revision) REFERENCES event_securities(revision)
            ON DELETE RESTRICT"""
    broken_ddl = evidence_ddl.replace(composite, split)
    assert broken_ddl != evidence_ddl
    connection.execute(broken_ddl)
    for statement in _INDEX_DDL:
        connection.execute(statement)
    for statement in _TRIGGER_DDL:
        connection.execute(statement)

    assert _schema_is_current(connection) is False
    with pytest.raises(EventCoreSchemaError, match="event_evidence foreign keys"):
        init_event_core_schema(connection)
    connection.close()


def test_observation_columns_migrate_additively_and_backfill_ingestion_time():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE TABLE events ("
        "event_id TEXT PRIMARY KEY, title TEXT NOT NULL, event_type TEXT NOT NULL, "
        "direction TEXT NOT NULL, priority REAL NOT NULL, occurred_at INTEGER NOT NULL, "
        "version INTEGER NOT NULL, merge_state TEXT NOT NULL, "
        "merged_into TEXT REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE RESTRICT, "
        "strategy_version TEXT NOT NULL, model_version TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    legacy_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO events VALUES (?, 'legacy', 'policy', 'neutral', 5, 50, 1, "
        "'active', NULL, 's1', 'm1', 100, 120)",
        (legacy_id,),
    )
    connection.commit()

    init_event_core_schema(connection)
    init_event_core_schema(connection)

    assert connection.execute(
        "SELECT occurred_at, first_seen_at, last_seen_at, available_at FROM events "
        "WHERE event_id = ?",
        (legacy_id,),
    ).fetchone() == (50, 100, 120, 100)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE events SET event_id = ? WHERE event_id = ?",
            (str(uuid.uuid4()), legacy_id),
        )
    connection.rollback()
    connection.close()


def test_external_identity_resolves_stable_uuid_and_metadata_version(conn):
    title = "央行公布政策"
    event_id = _event(conn, "source-42", title=title)
    assert str(uuid.UUID(event_id)) == event_id
    assert event_id != hashlib.sha1(title.encode()).hexdigest()[:16]
    assert conn.execute(
        "SELECT occurred_at, first_seen_at, last_seen_at, available_at FROM events "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone() == (90, 100, 100, 100)

    assert _event(conn, "source-42", title=title, now=101) == event_id
    assert conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT first_seen_at, last_seen_at, available_at FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone() == (100, 101, 100)

    assert _event(conn, "source-42", title="央行公布政策（更新）", now=102) == event_id
    row = conn.execute(
        "SELECT title, version FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert tuple(row) == ("央行公布政策（更新）", 2)

    # Adding a second durable identity changes the record once, then becomes a no-op.
    resolved = resolve_or_create_event(
        conn,
        {"wire": "source-42", "filing": "notice-7"},
        title="央行公布政策（更新）",
        event_type="policy",
        direction="bullish",
        priority=8,
        occurred_at=90,
        strategy_version="strategy-v1",
        model_version="model-v1",
        now=103,
    )
    assert resolved == event_id
    assert conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()[0] == 3

    # A replayed/backdated availability observation cannot rewrite history.
    assert _event(
        conn, "source-42", title="央行公布政策（更新）", now=105, available_at=99
    ) == event_id
    assert conn.execute(
        "SELECT version, first_seen_at, last_seen_at, available_at FROM events "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone() == (3, 100, 105, 100)
    assert _event(
        conn, "source-42", title="央行公布政策（更新）", now=106, available_at=105
    ) == event_id
    assert conn.execute(
        "SELECT version, first_seen_at, last_seen_at, available_at FROM events "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone() == (4, 100, 106, 105)
    assert resolve_or_create_event(
        conn,
        {"wire": "source-42", "filing": "notice-7"},
        title="央行公布政策（更新）",
        event_type="policy",
        direction="bullish",
        priority=8,
        occurred_at=90,
        strategy_version="strategy-v1",
        model_version="model-v1",
        now=104,
    ) == event_id
    assert conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()[0] == 4


def test_new_event_clamps_backdated_availability_but_honors_forward_delay(conn):
    event_id = _event(conn, "historical-availability", now=100, available_at=80)
    assert conn.execute(
        "SELECT first_seen_at, last_seen_at, available_at FROM events WHERE event_id = ?",
        (event_id,),
    ).fetchone() == (100, 100, 100)
    delayed = _event(conn, "delayed-availability", now=100, available_at=120)
    assert conn.execute(
        "SELECT first_seen_at, last_seen_at, available_at FROM events WHERE event_id = ?",
        (delayed,),
    ).fetchone() == (100, 120, 120)


def test_conflicting_identities_fail_closed_and_roll_back(conn):
    first = _event(conn, "a")
    second = _event(conn, "b")
    before = conn.execute("SELECT COUNT(*) FROM event_identities").fetchone()[0]

    with pytest.raises(EventCoreConflictError, match="different canonical"):
        resolve_or_create_event(
            conn,
            [("wire", "a"), ("wire", "b"), ("wire", "new")],
            title="must not overwrite",
            now=200,
        )

    assert conn.execute("SELECT COUNT(*) FROM event_identities").fetchone()[0] == before
    assert conn.execute(
        "SELECT title FROM events WHERE event_id = ?", (first,)
    ).fetchone()[0] == "event a"
    assert conn.execute(
        "SELECT title FROM events WHERE event_id = ?", (second,)
    ).fetchone()[0] == "event b"


def test_merge_chain_is_canonical_audited_versioned_and_idempotent(conn):
    first = _event(conn, "a")
    second = _event(conn, "b")
    third = _event(conn, "c")

    assert merge_events(conn, first, second, reason="duplicate", actor="reviewer", now=110) == second
    assert conn.execute(
        "SELECT merge_contract FROM events WHERE event_id = ?", (first,)
    ).fetchone()[0] == EVENT_MERGE_CONTRACT
    versions_after_first_merge = dict(
        conn.execute("SELECT event_id, version FROM events")
    )
    assert merge_events(conn, first, second, reason="duplicate", actor="reviewer", now=111) == second
    assert dict(conn.execute("SELECT event_id, version FROM events")) == versions_after_first_merge

    assert merge_events(conn, second, third, reason="larger cluster", now=120) == third
    assert canonical_event_id(conn, first) == third
    assert canonical_event_id(conn, second) == third
    assert canonical_event_id(conn, third) == third
    branch = _event(conn, "branch")
    merge_events(conn, branch, third, reason="same root", now=121)
    group = canonical_event_group(conn, first)
    assert group[0] == third
    assert set(group) == {first, second, third, branch}
    assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 3
    assert conn.execute(
        "SELECT merge_state, merged_into FROM events WHERE event_id = ?", (first,)
    ).fetchone() == ("merged", second)


def test_merge_contract_fences_old_writer_order_and_direct_forgery(conn):
    source = _event(conn, "merge-contract-source")
    target = _event(conn, "merge-contract-target")

    with pytest.raises(sqlite3.IntegrityError, match="merge contract transition"):
        # Previous images performed this UPDATE before inserting history and do
        # not know the additive merge_contract column.
        conn.execute(
            "UPDATE events SET merge_state = 'merged', merged_into = ?, "
            "version = version + 1 WHERE event_id = ?",
            (target, source),
        )
    assert conn.execute(
        "SELECT merge_state, merged_into, merge_contract FROM events "
        "WHERE event_id = ?",
        (source,),
    ).fetchone() == ("active", None, "")
    assert conn.execute("SELECT COUNT(*) FROM event_merge_history").fetchone()[0] == 0

    with pytest.raises(sqlite3.IntegrityError, match="merge contract transition"):
        conn.execute(
            "UPDATE events SET merge_contract = ? WHERE event_id = ?",
            (EVENT_MERGE_CONTRACT, source),
        )
    with pytest.raises(sqlite3.IntegrityError, match="merge contract transition"):
        conn.execute(
            "UPDATE events SET merge_state = 'merged', merged_into = ?, "
            "merge_contract = ?, version = version + 1 WHERE event_id = ?",
            (target, EVENT_MERGE_CONTRACT, source),
        )
    merge_events(conn, source, target, reason="qualified", actor="test", now=200)
    assert conn.execute(
        "SELECT merge_state, merged_into, merge_contract FROM events "
        "WHERE event_id = ?",
        (source,),
    ).fetchone() == ("merged", target, EVENT_MERGE_CONTRACT)
    with pytest.raises(sqlite3.IntegrityError, match="merge contract transition"):
        conn.execute(
            "UPDATE events SET merge_contract = '' WHERE event_id = ?",
            (source,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="merge history is immutable"):
        conn.execute(
            "UPDATE event_merge_history SET reason = 'forged' "
            "WHERE source_event_id = ?",
            (source,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="merge history is immutable"):
        conn.execute(
            "DELETE FROM event_merge_history WHERE source_event_id = ?",
            (source,),
        )


@pytest.mark.parametrize(
    "weak_contract",
    (
        "merge_contract TEXT DEFAULT ''",
        "merge_contract TEXT NOT NULL DEFAULT NULL",
    ),
)
def test_schema_rejects_weakened_merge_contract_column(weak_contract):
    connection = sqlite3.connect(":memory:")
    weakened_events = _DDL[0].replace(
        "merge_contract TEXT NOT NULL DEFAULT ''",
        weak_contract,
        1,
    )
    assert weakened_events != _DDL[0]
    connection.execute(weakened_events)

    with pytest.raises(
        EventCoreSchemaError,
        match="incompatible events merge_contract constraint",
    ):
        init_event_core_schema(connection)
    connection.close()


def test_merge_carries_forward_source_observation_times(conn):
    target = _event(conn, "old-target", now=100, available_at=100)
    source = _event(conn, "recent-source", now=200, available_at=190)

    merge_events(conn, source, target, now=210)

    assert conn.execute(
        "SELECT first_seen_at, last_seen_at, available_at FROM events WHERE event_id = ?",
        (target,),
    ).fetchone() == (100, 200, 200)


def test_merge_priority_uses_group_max_and_merged_replay_cannot_lower_it(conn):
    target = resolve_or_create_event(
        conn,
        {"wire": "low-target"},
        title="较早低分事件",
        priority=5.0,
        now=100,
    )
    source = resolve_or_create_event(
        conn,
        {"wire": "high-source"},
        title="较晚高分事件",
        priority=9.0,
        now=101,
    )

    merge_events(conn, source, target, now=110)
    assert conn.execute(
        "SELECT priority FROM events WHERE event_id = ?", (target,)
    ).fetchone()[0] == 9.0

    # Replaying the merged source resolves to the root, but its new lower
    # score cannot erase the audited 9.0 member score.
    assert resolve_or_create_event(
        conn,
        {"wire": "high-source"},
        title="较晚事件重新评分",
        priority=4.0,
        now=120,
    ) == target
    assert conn.execute(
        "SELECT priority FROM events WHERE event_id = ?", (target,)
    ).fetchone()[0] == 9.0

    # The floor applies only to merged groups; an ordinary event can still be
    # corrected downward.
    solo = resolve_or_create_event(
        conn, {"wire": "solo"}, title="独立事件", priority=8.0, now=130
    )
    resolve_or_create_event(
        conn, {"wire": "solo"}, title="独立事件", priority=3.0, now=140
    )
    assert conn.execute(
        "SELECT priority FROM events WHERE event_id = ?", (solo,)
    ).fetchone()[0] == 3.0


def test_priority_reconciliation_repairs_legacy_root_once(conn):
    target = resolve_or_create_event(
        conn, {"wire": "legacy-target"}, title="旧根", priority=5.0, now=200
    )
    source = resolve_or_create_event(
        conn, {"wire": "legacy-source"}, title="旧成员", priority=8.5, now=201
    )
    merge_events(conn, source, target, now=210)
    conn.execute(
        "UPDATE events SET priority = 5.0 WHERE event_id = ?", (target,)
    )
    before_version = conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (target,)
    ).fetchone()[0]

    assert reconcile_event_group_priorities(conn, now=220) == 1
    repaired = conn.execute(
        "SELECT priority, version, updated_at FROM events WHERE event_id = ?",
        (target,),
    ).fetchone()
    assert repaired == (8.5, before_version + 1, 220)
    assert reconcile_event_group_priorities(conn, now=230) == 0
    assert conn.execute(
        "SELECT version FROM events WHERE event_id = ?", (target,)
    ).fetchone()[0] == before_version + 1


def test_schema_foreign_keys_and_numeric_checks_fail_closed(conn):
    event_id = _event(conn, "constraints")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600000", name="浦发银行", now=100
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE events SET priority = 10.01 WHERE event_id = ?", (event_id,)
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE events SET event_id = ? WHERE event_id = ?",
            (str(uuid.uuid4()), event_id),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event_securities "
            "(mapping_id, event_id, security_id, relation_type, direction, confidence, "
            "relevance, rationale, mapping_version, revision, is_current, created_at) "
            "VALUES (?, ?, ?, 'affected', 'neutral', 1.1, 5, '', '', 1, 1, 1)",
            (str(uuid.uuid4()), event_id, security_id),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event_evidence "
            "(evidence_id, event_id, source_uri, claim_key, claim_text, observed_at, "
            "created_at, updated_at) VALUES (?, ?, 'https://example.com', 'c1', 'claim', "
            "1, 1, 1)",
            (str(uuid.uuid4()), str(uuid.uuid4())),
        )
    conn.rollback()

    with pytest.raises(ValueError, match="between 0 and 1"):
        upsert_event_similarity(conn, event_id, _event(conn, "other"), similarity=1.1)


def test_all_relation_upserts_are_idempotent_and_update_once(conn):
    event_id = _event(conn, "main")
    similar_id = _event(conn, "history")
    security_id = upsert_security(
        conn,
        exchange="sse",
        ticker="600000",
        name="浦发银行",
        asset_type="equity",
        currency="cny",
        aliases=["浦发", "SPDB"],
        industry="银行",
        concepts=["沪深300"],
        now=100,
    )
    assert upsert_security(
        conn,
        exchange="SSE",
        ticker="600000",
        name="浦发银行",
        aliases=["SPDB", "浦发"],
        industry="银行",
        concepts=["沪深300"],
        now=101,
    ) == security_id
    assert conn.execute(
        "SELECT version FROM securities WHERE security_id = ?", (security_id,)
    ).fetchone()[0] == 1
    security_metadata = conn.execute(
        "SELECT aliases, industry, concepts FROM securities WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    assert json.loads(security_metadata[0]) == ["SPDB", "浦发"]
    assert security_metadata[1] == "银行"
    assert json.loads(security_metadata[2]) == ["沪深300"]

    mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        direction="bullish",
        confidence=0.8,
        relevance=9,
        rationale="直接受益",
        mapping_version="map-v1",
        now=110,
    )
    assert upsert_event_security(
        conn,
        event_id,
        security_id,
        direction="bullish",
        confidence=0.8,
        relevance=9,
        rationale="直接受益",
        mapping_version="map-v1",
        now=111,
    ) == mapping_id

    evidence_id = upsert_event_evidence(
        conn,
        event_id,
        source_uri="https://example.com/notice",
        claim_key="policy-published",
        claim_text="政策已经发布",
        security_id=security_id,
        mapping_id=mapping_id,
        excerpt="官方公告",
        snapshot_text="公告正文快照",
        stance="supports",
        reliability=0.95,
        announcement_at=80,
        observed_at=90,
        now=110,
    )
    assert upsert_event_evidence(
        conn,
        event_id,
        source_uri="https://example.com/notice",
        claim_key="policy-published",
        claim_text="政策已经发布",
        security_id=security_id,
        mapping_id=mapping_id,
        excerpt="官方公告",
        snapshot_text="公告正文快照",
        stance="supports",
        reliability=0.95,
        announcement_at=80,
        observed_at=90,
        now=111,
    ) == evidence_id

    outcome_id = upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=100,
        measured_at=200,
        baseline_price=10.0,
        gross_return=0.03,
        benchmark_return=0.02,
        excess_return=0.01,
        mfe=0.05,
        mae=-0.01,
        direction_hit=True,
        method_version="close-v1",
        data_version="prices-2026-07-20",
        data_quality=1,
        now=210,
    )
    assert upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=100,
        measured_at=200,
        baseline_price=10.0,
        gross_return=0.03,
        benchmark_return=0.02,
        excess_return=0.01,
        mfe=0.05,
        mae=-0.01,
        direction_hit=True,
        method_version="close-v1",
        data_version="prices-2026-07-20",
        data_quality=1,
        now=211,
    ) == outcome_id

    similarity_key = upsert_event_similarity(
        conn,
        event_id,
        similar_id,
        similarity=0.87,
        method="features",
        model_version="sim-v1",
        rationale="同类政策冲击",
        common_securities=[security_id],
        shared_paths=[{"path": ["政策", "银行"], "weight": 0.8}],
        computed_at=220,
        now=220,
    )
    assert upsert_event_similarity(
        conn,
        similar_id,
        event_id,
        similarity=0.87,
        method="features",
        model_version="sim-v1",
        rationale="同类政策冲击",
        common_securities=[security_id],
        shared_paths=[{"weight": 0.8, "path": ["政策", "银行"]}],
        computed_at=220,
        now=221,
    ) == similarity_key

    for table in ("event_securities", "event_evidence", "event_outcomes", "event_similarities"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert conn.execute("SELECT version FROM event_evidence").fetchone()[0] == 1
    assert conn.execute("SELECT version FROM event_similarities").fetchone()[0] == 1
    assert conn.execute("SELECT revision FROM event_securities").fetchone()[0] == 1
    assert conn.execute("SELECT revision FROM event_outcomes").fetchone()[0] == 1

    upsert_event_security(
        conn,
        event_id,
        security_id,
        direction="bullish",
        confidence=0.9,
        relevance=9,
        rationale="直接受益",
        mapping_version="map-v1",
        now=230,
    )
    assert conn.execute(
        "SELECT revision, is_current FROM event_securities ORDER BY revision"
    ).fetchall() == [(1, 0), (2, 1)]

    upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=100,
        measured_at=200,
        baseline_price=10.0,
        gross_return=0.04,
        benchmark_return=0.02,
        excess_return=0.02,
        mfe=0.06,
        mae=-0.01,
        direction_hit=True,
        method_version="close-v1",
        data_version="prices-2026-07-20",
        now=231,
    )
    assert conn.execute(
        "SELECT revision, is_current, gross_return FROM event_outcomes ORDER BY revision"
    ).fetchall() == [(1, 0, 0.03), (2, 1, 0.04)]


def test_retired_event_security_reappears_as_next_immutable_revision(conn):
    event_id = _event(conn, "mapping-reappears")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600519", name="贵州茅台", now=100
    )
    first_mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        relation_type="direct",
        direction="bullish",
        confidence=0.8,
        relevance=9,
        rationale="原文直接提及",
        mapping_version="asset-map:v1",
        now=101,
    )

    conn.execute(
        "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ?",
        (first_mapping_id,),
    )
    replacement_mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        relation_type="direct",
        direction="bullish",
        confidence=0.8,
        relevance=9,
        rationale="原文直接提及",
        mapping_version="asset-map:v1",
        now=102,
    )

    assert replacement_mapping_id != first_mapping_id
    assert conn.execute(
        "SELECT mapping_id, revision, is_current, created_at "
        "FROM event_securities ORDER BY revision"
    ).fetchall() == [
        (first_mapping_id, 1, 0, 101),
        (replacement_mapping_id, 2, 1, 102),
    ]
    assert upsert_event_security(
        conn,
        event_id,
        security_id,
        relation_type="direct",
        direction="bullish",
        confidence=0.8,
        relevance=9,
        rationale="原文直接提及",
        mapping_version="asset-map:v1",
        now=103,
    ) == replacement_mapping_id

    revised_mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        relation_type="direct",
        direction="bearish",
        confidence=0.9,
        relevance=8,
        rationale="后续事实修订",
        mapping_version="asset-map:v2",
        now=104,
    )
    assert revised_mapping_id not in {first_mapping_id, replacement_mapping_id}
    assert conn.execute(
        "SELECT mapping_id, revision, is_current, direction "
        "FROM event_securities ORDER BY revision"
    ).fetchall() == [
        (first_mapping_id, 1, 0, "bullish"),
        (replacement_mapping_id, 2, 0, "bullish"),
        (revised_mapping_id, 3, 1, "bearish"),
    ]


def test_mapping_and_outcome_revision_payloads_reject_direct_sql_updates(conn):
    event_id = _event(conn, "direct-sql-immutability")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600036", name="招商银行", now=100
    )
    mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        direction="bullish",
        confidence=0.8,
        relevance=8,
        mapping_version="map-v1",
        now=101,
    )
    outcome_id = upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=100,
        measured_at=200,
        gross_return=0.03,
        method_version="close-v1",
        data_version="prices-v1",
        now=201,
    )

    with pytest.raises(sqlite3.IntegrityError, match="event-security revisions are immutable"):
        conn.execute(
            "UPDATE event_securities SET confidence = 0.1 WHERE mapping_id = ?",
            (mapping_id,),
        )
    conn.rollback()
    assert conn.execute(
        "SELECT confidence FROM event_securities WHERE mapping_id = ?", (mapping_id,)
    ).fetchone()[0] == 0.8

    with pytest.raises(sqlite3.IntegrityError, match="event outcome revisions are immutable"):
        conn.execute(
            "UPDATE event_outcomes SET gross_return = -0.5 WHERE outcome_id = ?",
            (outcome_id,),
        )
    conn.rollback()
    assert conn.execute(
        "SELECT gross_return FROM event_outcomes WHERE outcome_id = ?", (outcome_id,)
    ).fetchone()[0] == 0.03

    with pytest.raises(sqlite3.IntegrityError, match="event outcome revisions are immutable"):
        conn.execute(
            "DELETE FROM event_outcomes WHERE outcome_id = ?", (outcome_id,)
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="event-security revisions are immutable"):
        conn.execute(
            "DELETE FROM event_securities WHERE mapping_id = ?", (mapping_id,)
        )
    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) FROM event_outcomes WHERE outcome_id = ?", (outcome_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM event_securities WHERE mapping_id = ?", (mapping_id,)
    ).fetchone()[0] == 1


def test_security_identifier_conflicts_do_not_rebind(conn):
    security_id = upsert_security(
        conn,
        exchange="SZSE",
        ticker="000001",
        name="平安银行",
        security_id="cn-stock-000001",
        now=100,
    )
    with pytest.raises(EventCoreConflictError, match="another exchange/ticker"):
        upsert_security(
            conn,
            exchange="SSE",
            ticker="600000",
            name="浦发银行",
            security_id=security_id,
            now=101,
        )
    assert conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 1


def test_missing_outcome_metrics_stay_null_and_frozen_revisions_are_restricted(conn):
    event_id = _event(conn, "null-outcome")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600519", name="贵州茅台", now=100
    )
    mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        confidence=0.7,
        mapping_version="map-v1",
        now=101,
    )
    outcome_id = upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+2",
        baseline_at=100,
        measured_at=200,
        method_version="close-v1",
        data_version="missing-calendar-day",
        now=201,
    )
    row = conn.execute(
        "SELECT outcome_id, mapping_id, mapping_revision, baseline_price, gross_return, "
        "benchmark_return, excess_return, mfe, mae FROM event_outcomes"
    ).fetchone()
    assert row[:3] == (outcome_id, mapping_id, 1)
    assert row[3:] == (None, None, None, None, None, None)

    # Historical mappings and their outcome audit trail cannot be deleted.
    upsert_event_security(
        conn,
        event_id,
        security_id,
        confidence=0.8,
        mapping_version="map-v2",
        now=202,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM event_securities WHERE mapping_id = ?", (mapping_id,))
    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) FROM event_outcomes WHERE outcome_id = ?", (outcome_id,)
    ).fetchone()[0] == 1


def test_evidence_source_url_is_storage_only_and_rejects_credentials(conn):
    event_id = _event(conn, "unsafe-source")
    with pytest.raises(ValueError, match="credential-free"):
        upsert_event_evidence(
            conn,
            event_id,
            source_uri="https://user:secret@example.com/private",
            claim_key="claim",
            claim_text="结论",
        )
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        upsert_event_evidence(
            conn,
            event_id,
            source_uri="file:///etc/passwd",
            claim_key="claim",
            claim_text="结论",
        )


def test_evidence_snapshot_is_immutable_and_observation_time_is_monotonic(conn):
    event_id = _event(conn, "immutable-evidence")
    evidence_id = upsert_event_evidence(
        conn,
        event_id,
        source_uri="https://example.com/notice",
        claim_key="claim-v1",
        claim_text="原始结论",
        snapshot_text="原始快照",
        observed_at=100,
        now=100,
    )

    with pytest.raises(EventCoreConflictError, match="cannot move backward"):
        upsert_event_evidence(
            conn,
            event_id,
            source_uri="https://example.com/notice",
            claim_key="claim-v1",
            claim_text="原始结论",
            snapshot_text="原始快照",
            observed_at=99,
            now=101,
        )
    with pytest.raises(EventCoreConflictError, match="snapshot is immutable"):
        upsert_event_evidence(
            conn,
            event_id,
            source_uri="https://example.com/notice",
            claim_key="claim-v1",
            claim_text="修改后的结论",
            snapshot_text="修改后的快照",
            observed_at=101,
            now=101,
        )

    assert conn.execute(
        "SELECT evidence_id, claim_text, snapshot_text, observed_at, version "
        "FROM event_evidence WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone() == (evidence_id, "原始结论", "原始快照", 100, 1)


def test_mapping_replay_ledger_is_versioned_retryable_and_terminal(conn):
    event_id = _event(conn, "mapping-replay-ledger")
    evidence_id = upsert_event_evidence(
        conn,
        event_id,
        source_uri="https://example.test/mapping-replay-ledger",
        claim_key="article:mapping-replay-ledger",
        claim_text="寒武纪发布新一代算力芯片",
        snapshot_text="寒武纪发布新一代算力芯片",
        observed_at=100,
        now=100,
    )
    root_version = int(
        conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
    )
    fingerprint = hashlib.sha256(b"mapping-replay-input-v1").hexdigest()
    output_fingerprint = hashlib.sha256(b"mapping-replay-output-v1").hexdigest()
    replay_contract = {
        "evidence_version": 1,
        "root_event_id": event_id,
        "root_event_version": root_version,
        "input_fingerprint": fingerprint,
        "output_fingerprint": output_fingerprint,
    }

    assert record_event_mapping_replay(
        conn,
        evidence_id,
        "asset-map:test-v1",
        status="failed",
        failure_code="ValueError",
        now=110,
        **replay_contract,
    ) == 1
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        conn.execute(
            "UPDATE event_mapping_replay_ledger SET root_event_version = 2 "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (evidence_id, "asset-map:test-v1", fingerprint),
        )
    with pytest.raises(sqlite3.IntegrityError, match="attempts must advance once"):
        conn.execute(
            "UPDATE event_mapping_replay_ledger SET attempts = attempts "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (evidence_id, "asset-map:test-v1", fingerprint),
        )
    assert record_event_mapping_replay(
        conn,
        evidence_id,
        "asset-map:test-v1",
        status="failed",
        failure_code="ValueError",
        now=120,
        **replay_contract,
    ) == 2
    assert record_event_mapping_replay(
        conn,
        evidence_id,
        "asset-map:test-v1",
        status="mapped",
        mapped_count=2,
        now=130,
        **replay_contract,
    ) == 3
    assert record_event_mapping_replay(
        conn,
        evidence_id,
        "asset-map:test-v1",
        status="mapped",
        mapped_count=2,
        now=140,
        **replay_contract,
    ) == 3

    assert conn.execute(
        "SELECT status, mapped_count, attempts, failure_code, processed_at, updated_at "
        "FROM event_mapping_replay_ledger WHERE evidence_id = ? AND mapping_version = ? "
        "AND input_fingerprint = ?",
        (evidence_id, "asset-map:test-v1", fingerprint),
    ).fetchone() == ("mapped", 2, 3, "", 130, 130)
    with pytest.raises(EventCoreConflictError, match="terminal"):
        record_event_mapping_replay(
            conn,
            evidence_id,
            "asset-map:test-v1",
            status="no_match",
            now=150,
            **replay_contract,
        )
    with pytest.raises(sqlite3.IntegrityError, match="terminal"):
        conn.execute(
            "UPDATE event_mapping_replay_ledger SET mapped_count = 3 "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (evidence_id, "asset-map:test-v1", fingerprint),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "DELETE FROM event_mapping_replay_ledger "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (evidence_id, "asset-map:test-v1", fingerprint),
        )
    with pytest.raises(sqlite3.IntegrityError, match="identity already exists"):
        conn.execute(
            "INSERT OR REPLACE INTO event_mapping_replay_ledger "
            "SELECT * FROM event_mapping_replay_ledger "
            "WHERE evidence_id = ? AND mapping_version = ? AND input_fingerprint = ?",
            (evidence_id, "asset-map:test-v1", fingerprint),
        )


def test_mapping_replay_ledger_migrates_additively(conn):
    event_id = _event(conn, "mapping-replay-migration")
    evidence_id = upsert_event_evidence(
        conn,
        event_id,
        source_uri="https://example.test/mapping-replay-migration",
        claim_key="article:mapping-replay-migration",
        claim_text="映射迁移保留证据",
        observed_at=100,
        now=100,
    )
    conn.commit()
    for trigger in (
        "event_core_mapping_replay_dirty_group_merge",
        "event_core_mapping_replay_dirty_mapping_insert",
        "event_core_mapping_replay_dirty_mapping_retire",
        "event_core_mapping_replay_dirty_evidence_insert",
        "event_core_mapping_replay_dirty_evidence_update",
        "event_core_mapping_replay_dirty_evidence_delete",
    ):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP TABLE event_mapping_replay_dirty_roots")
    conn.execute("DROP TRIGGER event_core_mapping_replay_terminal_immutable")
    conn.execute("DROP TRIGGER event_core_mapping_replay_delete_immutable")
    conn.execute("DROP INDEX idx_event_mapping_replay_lookup")
    conn.execute("DROP INDEX idx_event_evidence_unbound_observed")
    conn.execute("DROP TABLE event_mapping_replay_ledger")

    assert _schema_is_current(conn) is False
    init_event_core_schema(conn)

    assert _schema_is_current(conn) is True
    assert conn.execute(
        "SELECT claim_text FROM event_evidence WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()[0] == "映射迁移保留证据"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_mapping_replay_queries_use_contract_and_dirty_covering_indexes(conn):
    terminal_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT evidence_id, status, output_fingerprint "
            "FROM event_mapping_replay_ledger WHERE mapping_version = ? "
            "AND input_fingerprint = ? AND status IN ('mapped', 'no_match')",
            ("asset-map:test", "a" * 64),
        )
    )
    dirty_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT root_event_id "
            "FROM event_mapping_replay_dirty_roots "
            "ORDER BY dirty_at, root_event_id LIMIT 10"
        )
    )

    assert "idx_event_mapping_replay_contract_input" in terminal_plan
    assert "idx_event_mapping_replay_dirty_order" in dirty_plan


def test_similarity_incident_indexes_migrate_additively_without_replacing_triggers(
    conn,
):
    trigger_sql_before = tuple(
        conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    )
    conn.execute("DROP INDEX idx_event_similarities_contract_left")
    conn.execute("DROP INDEX idx_event_similarities_contract_right")
    conn.commit()

    assert _schema_is_current(conn) is False
    init_event_core_schema(conn)

    assert _schema_is_current(conn) is True
    index_names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert {
        "idx_event_similarities_contract_left",
        "idx_event_similarities_contract_right",
        "idx_event_similarities_reverse",
    }.issubset(index_names)
    assert tuple(
        conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    ) == trigger_sql_before


@pytest.mark.parametrize(
    ("endpoint", "index_name"),
    (
        ("event_id", "idx_event_similarities_contract_left"),
        ("similar_event_id", "idx_event_similarities_contract_right"),
    ),
)
def test_similarity_incident_queries_use_contract_indexes_without_scan(
    conn,
    endpoint,
    index_name,
):
    plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT event_id, similar_event_id, similarity, "
            "rationale, common_securities, shared_paths FROM event_similarities "
            f"INDEXED BY {index_name} "
            "WHERE method = ? AND model_version = ? "
            f"AND {endpoint} IN (?) ORDER BY event_id, similar_event_id",
            ("event-graph-v1", "deterministic-title-security-v1", str(uuid.uuid4())),
        )
    )

    assert index_name in plan
    assert "SCAN EVENT_SIMILARITIES" not in plan.upper()


def test_mapping_replay_dirty_table_rejects_weakened_ddl():
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    try:
        for trigger in (
            "event_core_mapping_replay_dirty_group_merge",
            "event_core_mapping_replay_dirty_mapping_insert",
            "event_core_mapping_replay_dirty_mapping_retire",
            "event_core_mapping_replay_dirty_evidence_insert",
            "event_core_mapping_replay_dirty_evidence_update",
            "event_core_mapping_replay_dirty_evidence_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP INDEX idx_event_mapping_replay_dirty_order")
        connection.execute("DROP TABLE event_mapping_replay_dirty_roots")
        connection.execute(
            "CREATE TABLE event_mapping_replay_dirty_roots ("
            "root_event_id TEXT, reason TEXT, dirty_at INTEGER)"
        )

        with pytest.raises(
            EventCoreSchemaError,
            match=r"incompatible (?:table definition|event_mapping_replay_dirty_roots foreign keys)",
        ):
            init_event_core_schema(connection)
    finally:
        connection.close()


def test_outcome_data_version_appends_one_stable_series_revision(conn):
    event_id = _event(conn, "outcome-data-revision")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600000", name="浦发银行", now=100
    )
    mapping_id = upsert_event_security(conn, event_id, security_id, now=101)
    outcome_id = upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=100,
        measured_at=200,
        baseline_price=10,
        gross_return=0.01,
        method_version="close-v1",
        data_version="prices-v1",
        now=200,
    )
    assert upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=100,
        measured_at=200,
        baseline_price=10,
        gross_return=0.02,
        method_version="close-v1",
        data_version="prices-v2",
        now=201,
    ) == outcome_id

    assert conn.execute(
        "SELECT outcome_id, revision, is_current, data_version, gross_return "
        "FROM event_outcomes ORDER BY revision"
    ).fetchall() == [
        (outcome_id, 1, 0, "prices-v1", 0.01),
        (outcome_id, 2, 1, "prices-v2", 0.02),
    ]


def test_legacy_data_version_current_index_is_replaced_once(conn):
    conn.execute("DROP INDEX uq_event_outcomes_series_current")
    conn.execute(
        "CREATE UNIQUE INDEX uq_event_outcomes_current ON event_outcomes("
        "event_id, security_id, mapping_id, horizon, method_version, data_version) "
        "WHERE is_current = 1"
    )
    conn.commit()

    init_event_core_schema(conn)

    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'event_outcomes'"
        )
    }
    assert "uq_event_outcomes_series_current" in indexes
    assert "uq_event_outcomes_current" not in indexes


def test_schema_repair_does_not_collapse_independent_signal_series(conn):
    event_id = _event(conn, "signal-series-schema-repair")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600000", name="浦发银行", now=100
    )
    mapping_id = upsert_event_security(conn, event_id, security_id, now=101)
    rows = []
    for signal_ledger_id in (1, 2):
        outcome_revision_id = str(uuid.uuid4())
        outcome_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO event_outcomes ("
            "outcome_revision_id, outcome_id, event_id, security_id, mapping_id, "
            "mapping_revision, signal_ledger_id, signal_available_at, "
            "observation_state, horizon, baseline_at, measured_at, method_version, "
            "data_version, data_quality, revision, is_current, created_at"
            ") VALUES (?, ?, ?, ?, ?, 1, ?, 110, 'complete', 'T+1', 120, "
            "220, 'close-v5', ?, 1, 1, 1, 221)",
            (
                outcome_revision_id,
                outcome_id,
                event_id,
                security_id,
                mapping_id,
                signal_ledger_id,
                f"prices-v{signal_ledger_id}",
            ),
        )
        rows.append(outcome_revision_id)
    conn.commit()

    # Force schema initialization through an unrelated repair path.  The
    # legacy event-level outcome migration must not run against current data.
    conn.execute("DROP INDEX idx_event_evidence_event_time")
    conn.commit()
    init_event_core_schema(conn)

    assert conn.execute(
        "SELECT outcome_revision_id, signal_ledger_id, is_current "
        "FROM event_outcomes ORDER BY signal_ledger_id"
    ).fetchall() == [(rows[0], 1, 1), (rows[1], 2, 1)]


def test_wrong_legacy_index_definition_fails_before_retiring_outcomes(conn):
    event_id = _event(conn, "wrong-legacy-outcome-index")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600000", name="浦发银行", now=100
    )
    mapping_id = upsert_event_security(conn, event_id, security_id, now=101)
    revision_ids = []
    for signal_ledger_id in (1, 2):
        outcome_revision_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO event_outcomes ("
            "outcome_revision_id, outcome_id, event_id, security_id, mapping_id, "
            "mapping_revision, signal_ledger_id, signal_available_at, "
            "observation_state, horizon, baseline_at, measured_at, method_version, "
            "data_version, data_quality, revision, is_current, created_at"
            ") VALUES (?, ?, ?, ?, ?, 1, ?, 110, 'complete', 'T+1', 120, "
            "220, 'close-v5', ?, 1, 1, 1, 221)",
            (
                outcome_revision_id,
                str(uuid.uuid4()),
                event_id,
                security_id,
                mapping_id,
                signal_ledger_id,
                f"prices-v{signal_ledger_id}",
            ),
        )
        revision_ids.append(outcome_revision_id)
    conn.execute(
        "CREATE INDEX uq_event_outcomes_current ON event_outcomes(created_at)"
    )
    conn.commit()

    with pytest.raises(
        EventCoreSchemaError,
        match="incompatible index definition: uq_event_outcomes_current",
    ):
        init_event_core_schema(conn)

    assert conn.execute(
        "SELECT outcome_revision_id, signal_ledger_id, is_current "
        "FROM event_outcomes ORDER BY signal_ledger_id"
    ).fetchall() == [(revision_ids[0], 1, 1), (revision_ids[1], 2, 1)]
    assert conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'uq_event_outcomes_current'"
    ).fetchone()[0] == (
        "CREATE INDEX uq_event_outcomes_current ON event_outcomes(created_at)"
    )


def test_legacy_outcome_revision_identity_is_rebuilt_losslessly(conn):
    event_id = _event(conn, "legacy-outcome-series-identity")
    security_id = upsert_security(
        conn, exchange="SSE", ticker="600000", name="浦发银行", now=100
    )
    mapping_id = upsert_event_security(
        conn,
        event_id,
        security_id,
        direction="bullish",
        confidence=0.8,
        relevance=8,
        mapping_version="map-v1",
        now=101,
    )
    upsert_event_outcome(
        conn,
        event_id,
        security_id,
        mapping_id=mapping_id,
        horizon="T+1",
        baseline_at=120,
        measured_at=220,
        gross_return=0.03,
        method_version="close-v1",
        data_version="prices-v1",
        now=221,
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(event_outcomes)")]
    quoted = ", ".join(f'"{column}"' for column in columns)
    before = conn.execute(
        f"SELECT {quoted} FROM event_outcomes ORDER BY outcome_revision_id"
    ).fetchall()
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'event_outcomes'"
    ).fetchone()[0]
    legacy_sql = table_sql.replace(
        "CREATE TABLE event_outcomes",
        "CREATE TABLE event_outcomes_legacy_contract",
        1,
    ).replace(
        "UNIQUE (outcome_id, revision),",
        "UNIQUE (outcome_id, revision), "
        "UNIQUE (event_id, security_id, mapping_id, horizon, method_version, revision),",
        1,
    )
    assert legacy_sql != table_sql
    for trigger in (
        "event_core_outcome_ids_immutable",
        "event_core_outcome_revision_immutable",
        "event_core_outcome_revision_immutable_delete",
    ):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute(legacy_sql)
    conn.execute(
        f"INSERT INTO event_outcomes_legacy_contract ({quoted}) "
        f"SELECT {quoted} FROM event_outcomes"
    )
    conn.execute("DROP TABLE event_outcomes")
    conn.execute(
        "ALTER TABLE event_outcomes_legacy_contract RENAME TO event_outcomes"
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_event_outcomes_series_current ON event_outcomes("
        "event_id, security_id, mapping_id, horizon, method_version) "
        "WHERE is_current = 1"
    )
    conn.commit()

    assert _schema_is_current(conn) is False
    init_event_core_schema(conn)

    assert _schema_is_current(conn) is True
    after = conn.execute(
        f"SELECT {quoted} FROM event_outcomes ORDER BY outcome_revision_id"
    ).fetchall()
    assert after == before
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'event_outcomes'"
        )
    }
    assert {
        "uq_event_outcomes_series_current",
        "uq_event_outcomes_legacy_series_current",
        "uq_event_outcomes_series_revision",
        "uq_event_outcomes_legacy_series_revision",
        "idx_event_outcomes_signal_contract",
    }.issubset(indexes)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
