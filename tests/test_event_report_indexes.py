"""Additive history index migration must preserve strict event-core contracts."""
import sqlite3
import uuid

import pytest

from db.event_core import (
    EventCoreSchemaError,
    _INDEX_DDL,
    _schema_is_current,
    init_event_core_schema,
    resolve_or_create_event,
    upsert_event_evidence,
    verify_event_core_schema,
)
from db.event_report_indexes import HISTORY_INDEX_NAME, init_event_report_indexes


@pytest.fixture
def event_conn():
    conn = sqlite3.connect(":memory:")
    init_event_core_schema(conn)
    yield conn
    conn.close()


def _schema(conn, *, exclude_history_index=False):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
        if not exclude_history_index or str(row[1]) != HISTORY_INDEX_NAME
    ]


def _rows(conn):
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )]
    return {
        name: sorted(
            (tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')), key=repr,
        )
        for name in tables
    }


def _seed(conn):
    event_id = resolve_or_create_event(
        conn, {"test": "history-index"}, title="合成政策事件",
        event_type="policy", direction="neutral", priority=7, occurred_at=90, now=100,
    )
    evidence_id = upsert_event_evidence(
        conn, event_id, source_uri="https://example.test/report", claim_key="original",
        claim_text="合成政策原文", observed_at=100, now=100,
    )
    return event_id, evidence_id


def _assert_index_contract(conn):
    row = next(row for row in conn.execute("PRAGMA index_list(event_evidence)")
               if row[1] == HISTORY_INDEX_NAME)
    assert row[2] == 0  # Not unique: no new identity/data constraint.
    assert row[4] == 1  # Only unbound evidence enters the index.
    columns = conn.execute(f'PRAGMA index_xinfo("{HISTORY_INDEX_NAME}")').fetchall()
    keys = [(row[2], row[3], row[4]) for row in columns if row[5]]
    assert keys == [
        ("event_id", 0, "BINARY"),
        ("observed_at", 0, "BINARY"),
        ("evidence_id", 0, "BINARY"),
    ]
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (HISTORY_INDEX_NAME,),
    ).fetchone()[0]
    assert sql.split(" WHERE ", 1)[1] == "mapping_id IS NULL"


def test_fresh_database_startup_installs_optional_index():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _assert_index_contract(conn)
        verify_event_core_schema(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_core_upgrades_by_one_index_without_modifying_rows_or_constraints(event_conn):
    conn = event_conn
    _seed(conn)
    before_schema, before_rows = _schema(conn), _rows(conn)
    before_changes = conn.total_changes

    init_event_report_indexes(conn)

    assert _schema(conn, exclude_history_index=True) == before_schema
    assert _rows(conn) == before_rows
    assert conn.total_changes == before_changes
    _assert_index_contract(conn)
    verify_event_core_schema(conn)


def test_full_existing_database_startup_upgrades_missing_index_idempotently():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _seed(conn)
        conn.execute(f"DROP INDEX {HISTORY_INDEX_NAME}")
        conn.commit()
        before_schema, before_rows = _schema(conn), _rows(conn)

    database.init_db()
    database.init_db()

    with database.get_connection() as conn:
        assert _schema(conn, exclude_history_index=True) == before_schema
        assert _rows(conn) == before_rows
        _assert_index_contract(conn)


def test_unchanged_old_core_verifier_accepts_extra_index_and_does_not_require_it(event_conn):
    conn = event_conn
    assert not any(HISTORY_INDEX_NAME in ddl for ddl in _INDEX_DDL)
    assert _schema_is_current(conn)
    before = _schema(conn)
    init_event_report_indexes(conn)
    verify_event_core_schema(conn)
    assert _schema_is_current(conn)
    conn.execute(f"DROP INDEX {HISTORY_INDEX_NAME}")
    assert _schema(conn) == before
    verify_event_core_schema(conn)
    assert _schema_is_current(conn)


def test_repeated_installer_is_read_only_when_index_already_matches(event_conn):
    init_event_report_indexes(event_conn)
    _seed(event_conn)
    before_schema, before_rows = _schema(event_conn), _rows(event_conn)
    traced = []
    event_conn.set_trace_callback(traced.append)
    init_event_report_indexes(event_conn)
    event_conn.set_trace_callback(None)
    assert _schema(event_conn) == before_schema
    assert _rows(event_conn) == before_rows
    assert not any(sql.lstrip().upper().startswith((
        "CREATE", "DROP", "ALTER", "INSERT", "UPDATE", "DELETE", "BEGIN",
    )) for sql in traced)


def test_matching_case_whitespace_and_optional_if_not_exists_are_accepted(event_conn):
    event_conn.execute(
        f"CREATE INDEX IF NOT EXISTS {HISTORY_INDEX_NAME.upper()}\n"
        "ON event_evidence ( event_id , observed_at , evidence_id )\n"
        "WHERE mapping_id IS NULL;"
    )
    init_event_report_indexes(event_conn)
    verify_event_core_schema(event_conn)


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE {name} (untouched TEXT)",
    "CREATE VIEW {name} AS SELECT event_id FROM events",
    "CREATE TRIGGER {name} AFTER INSERT ON events BEGIN SELECT 1; END",
    "CREATE INDEX {name} ON events(event_id)",
    "CREATE INDEX {name} ON event_evidence(event_id, evidence_id, observed_at) WHERE mapping_id IS NULL",
    "CREATE INDEX {name} ON event_evidence(observed_at, event_id, evidence_id) WHERE mapping_id IS NULL",
    "CREATE INDEX {name} ON event_evidence(event_id, observed_at, evidence_id)",
    "CREATE INDEX {name} ON event_evidence(event_id, observed_at, evidence_id) WHERE mapping_id IS NOT NULL",
    "CREATE INDEX {name} ON event_evidence(event_id, observed_at, evidence_id) WHERE mapping_id IS NULL OR observed_at > 0",
    "CREATE UNIQUE INDEX {name} ON event_evidence(event_id, observed_at, evidence_id) WHERE mapping_id IS NULL",
    "CREATE INDEX {name} ON event_evidence(event_id COLLATE NOCASE, observed_at, evidence_id) WHERE mapping_id IS NULL",
    "CREATE INDEX {name} ON event_evidence(event_id, observed_at DESC, evidence_id) WHERE mapping_id IS NULL",
])
def test_same_named_wrong_object_fails_without_mutation(event_conn, ddl):
    conn = event_conn
    _seed(conn)
    conn.execute(ddl.format(name=HISTORY_INDEX_NAME))
    before_schema, before_rows = _schema(conn), _rows(conn)
    before_changes = conn.total_changes
    traced = []
    conn.set_trace_callback(traced.append)
    with pytest.raises(EventCoreSchemaError, match="incompatible optional event-report index"):
        init_event_report_indexes(conn)
    conn.set_trace_callback(None)
    assert _schema(conn) == before_schema
    assert _rows(conn) == before_rows
    assert conn.total_changes == before_changes
    assert not any(sql.lstrip().upper().startswith((
        "CREATE", "DROP", "ALTER", "INSERT", "UPDATE", "DELETE",
    )) for sql in traced)


def test_same_named_trigger_cannot_hide_beside_a_matching_index(event_conn):
    init_event_report_indexes(event_conn)
    event_conn.execute(
        f"CREATE TRIGGER {HISTORY_INDEX_NAME} AFTER INSERT ON events BEGIN SELECT 1; END"
    )
    before = _schema(event_conn)
    with pytest.raises(EventCoreSchemaError, match="incompatible optional event-report index"):
        init_event_report_indexes(event_conn)
    assert _schema(event_conn) == before


def test_startup_rejects_wrong_optional_index_without_rewriting_existing_database():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _seed(conn)
        conn.execute(f"DROP INDEX {HISTORY_INDEX_NAME}")
        conn.execute(f"CREATE INDEX {HISTORY_INDEX_NAME} ON event_evidence(observed_at)")
        conn.commit()
        before_schema, before_rows = _schema(conn), _rows(conn)
    with pytest.raises(EventCoreSchemaError, match="incompatible optional event-report index"):
        database.init_db()
    with database.get_connection() as conn:
        assert _schema(conn) == before_schema
        assert _rows(conn) == before_rows


def test_concurrent_creator_cannot_bypass_post_create_contract_verification():
    class ConcurrentConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith(f"CREATE INDEX IF NOT EXISTS {HISTORY_INDEX_NAME} "):
                super().execute(
                    f"CREATE INDEX {HISTORY_INDEX_NAME} ON event_evidence(observed_at)"
                )
            return super().execute(sql, parameters)

    conn = sqlite3.connect(":memory:", factory=ConcurrentConnection)
    try:
        init_event_core_schema(conn)
        with pytest.raises(EventCoreSchemaError, match="incompatible optional event-report index"):
            init_event_report_indexes(conn)
        assert [row[2] for row in conn.execute(
            f'PRAGMA index_info("{HISTORY_INDEX_NAME}")'
        )] == ["observed_at"]  # Do not drop/repair the competing object's definition.
    finally:
        conn.close()


def test_caller_transaction_can_roll_back_index_install(event_conn):
    event_conn.execute("BEGIN")
    init_event_report_indexes(event_conn)
    event_conn.rollback()
    assert event_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (HISTORY_INDEX_NAME,),
    ).fetchone() is None
    verify_event_core_schema(event_conn)


def test_missing_required_schema_object_still_fails_before_optional_installer(monkeypatch):
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        conn.execute("DROP TRIGGER event_core_events_version_monotonic")
        conn.commit()
        before_schema, before_rows = _schema(conn), _rows(conn)
    monkeypatch.setattr(database, "init_event_report_indexes", lambda conn: pytest.fail(
        "optional installer must not run after failed strict preflight"
    ))
    with pytest.raises(EventCoreSchemaError):
        database.init_db()
    with database.get_connection() as conn:
        assert _schema(conn) == before_schema
        assert _rows(conn) == before_rows


@pytest.mark.parametrize("overrides", [
    {"evidence_id": "invalid-uuid"},
    {"event_id": "00000000-0000-0000-0000-000000000099"},
    {"stance": "invented"},
    {"reliability": 2},
    {"observed_at": -1},
    {"mapping_id": "00000000-0000-0000-0000-000000000088"},
])
def test_additive_index_does_not_weaken_evidence_constraints(event_conn, overrides):
    event_id, _ = _seed(event_conn)
    init_event_report_indexes(event_conn)
    values = dict(
        evidence_id=str(uuid.uuid4()), event_id=event_id,
        source_uri="https://example.test/other", claim_key="other", claim_text="合成证据",
        observed_at=110, created_at=110, updated_at=110,
    ) | overrides
    columns = ", ".join(values)
    with pytest.raises(sqlite3.IntegrityError):
        event_conn.execute(
            f"INSERT INTO event_evidence ({columns}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


def test_history_lookup_has_event_and_time_index_access(event_conn):
    event_id, _ = _seed(event_conn)
    init_event_report_indexes(event_conn)
    plan = event_conn.execute(
        "EXPLAIN QUERY PLAN SELECT evidence_id FROM event_evidence "
        "WHERE event_id = ? AND mapping_id IS NULL AND observed_at < ? "
        "ORDER BY observed_at, evidence_id", (event_id, 200),
    ).fetchall()
    descriptions = " ".join(str(row[3]) for row in plan)
    assert HISTORY_INDEX_NAME in descriptions
    assert "event_id=? AND observed_at<?" in descriptions
    assert "USE TEMP B-TREE" not in descriptions
