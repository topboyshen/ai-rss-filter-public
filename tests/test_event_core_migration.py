import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from db.event_core import (
    EventCoreSchemaError,
    count_unresolved_event_core_outbox_references,
    init_event_core_schema,
    resolve_or_create_event,
    upsert_event_outcome,
    upsert_event_security,
    upsert_security,
    verify_event_core_backfill_state,
)
from db.event_migration import (
    BACKFILL_META_KEY,
    backfill_stable_event_core,
    validate_stable_event_core,
)


def _legacy_news(conn, link: str, *, title: str = "旧政策新闻") -> None:
    # init_db now records a completed marker for a genuinely empty new image.
    # This helper deliberately turns that image into an uncompleted legacy
    # fixture for exercising the explicit maintenance migration.
    conn.execute("DELETE FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,))
    conn.execute(
        "INSERT INTO news_cache "
        "(link, title, raw_title, score, tag, logic, created_at, summary, feed_url, "
        "strategy_version, model_name, direction, event_id, event_version) "
        "VALUES (?, ?, ?, 8.2, '政策', '传导', 100, '证据摘要', ?, 'v1', 'qwen', "
        "'看多', NULL, 0)",
        (link, title, title, "https://feed.example/source"),
    )


def _legacy_signal(conn, link: str) -> None:
    conn.execute("DELETE FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,))
    conn.execute(
        "INSERT INTO backtest_signals "
        "(signal_key, event_key, link, available_at, scored_at, recorded_at, score, "
        "direction, strategy_version, aggregation_threshold, selected, event_id, "
        "event_version) VALUES (?, 'legacy-title-key', ?, 200, 200, 201, 8.2, "
        "'看多', 'v1', 7, 1, NULL, 0)",
        (f"signal:{link}", link),
    )


def _insert_valid_news_event(conn, link: str = "https://example.com/current") -> str:
    event_id = resolve_or_create_event(
        conn,
        [("test", link)],
        title="Current event",
        event_type="news",
        direction="neutral",
        priority=5,
        occurred_at=100,
        now=100,
    )
    version = int(
        conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO news_cache (link, title, event_id, event_version) "
        "VALUES (?, 'Current event', ?, ?)",
        (link, event_id, version),
    )
    conn.execute(
        "INSERT INTO backtest_signals ("
        "signal_key, event_key, link, available_at, scored_at, recorded_at, "
        "score, direction, strategy_version, aggregation_threshold, selected, "
        "event_id, event_version) VALUES "
        "(?, ?, ?, 100, 100, 100, 8, 'neutral', 'test-v1', 7, 1, ?, ?)",
        (f"signal:{link}", f"event:{link}", link, event_id, version),
    )
    return event_id


def test_fresh_empty_database_records_positive_backfill_marker_and_verifies_read_only():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        markers = dict(
            conn.execute(
                "SELECT k, v FROM app_meta WHERE k IN (?, ?)",
                ("event_core_schema_version", BACKFILL_META_KEY),
            ).fetchall()
        )
        assert markers["event_core_schema_version"] == "2"
        assert int(markers[BACKFILL_META_KEY]) > 0
        before = conn.total_changes
        verify_event_core_backfill_state(conn)
        assert conn.total_changes == before


@pytest.mark.parametrize("table", ["news_cache", "backtest_signals"])
def test_startup_rejects_unmarked_nonempty_database_before_schema_changes(table):
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(f'CREATE TABLE "{table}" (legacy_value TEXT)')
        conn.execute(f'INSERT INTO "{table}" VALUES (\'legacy\')')
        original_journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
    original_bytes = Path(database.DB_PATH).read_bytes()

    with pytest.raises(
        EventCoreSchemaError,
        match="backfill marker is missing from a non-pristine database",
    ):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
        ).fetchone() is None
        assert [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')] == [
            "legacy_value"
        ]
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            original_journal_mode
        )
    assert Path(database.DB_PATH).read_bytes() == original_bytes


def test_startup_rejects_missing_marker_on_otherwise_valid_nonempty_database():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _insert_valid_news_event(conn)
        conn.execute("DELETE FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,))
        conn.commit()

    with pytest.raises(EventCoreSchemaError, match="backfill marker is missing"):
        database.init_db()

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None


def test_markerless_existing_schema_is_not_misclassified_as_fresh():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("CREATE TABLE app_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO app_meta (k, v) VALUES ('event_core_schema_version', '1')"
        )
        original_journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()

    with pytest.raises(EventCoreSchemaError, match="non-pristine database"):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = 'event_core_schema_version'"
        ).fetchone()[0] == "1"
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            original_journal_mode
        )


def test_markerless_empty_unrelated_table_allows_normal_initialization():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("CREATE TABLE unrelated_state (id INTEGER PRIMARY KEY)")
    database.init_db()

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM unrelated_state"
        ).fetchone()[0] == 0
        verify_event_core_backfill_state(conn)


def test_markerless_partial_empty_event_core_schema_fails_before_storage_changes():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
        original_journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
    original_bytes = Path(database.DB_PATH).read_bytes()

    with pytest.raises(EventCoreSchemaError, match="existing empty event-core schema"):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            original_journal_mode
        )
        assert [
            row[1] for row in conn.execute("PRAGMA table_info(events)")
        ] == ["event_id"]
    assert Path(database.DB_PATH).read_bytes() == original_bytes


def test_markerless_complete_empty_event_core_schema_allows_initialization():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        init_event_core_schema(conn)

    database.init_db()

    with database.get_connection() as conn:
        verify_event_core_backfill_state(conn)


def test_markerless_nonempty_outbox_fails_before_storage_changes():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE integration_signal_outbox ("
            "seq INTEGER PRIMARY KEY, payload TEXT)"
        )
        conn.execute(
            "INSERT INTO integration_signal_outbox (seq, payload) VALUES (1, 'legacy')"
        )
        original_journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
    original_bytes = Path(database.DB_PATH).read_bytes()

    with pytest.raises(EventCoreSchemaError, match="integration_signal_outbox"):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            original_journal_mode
        )
        assert conn.execute(
            "SELECT payload FROM integration_signal_outbox WHERE seq = 1"
        ).fetchone()[0] == "legacy"
        assert [
            row[1]
            for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
        ] == ["seq", "payload"]
    assert Path(database.DB_PATH).read_bytes() == original_bytes


def test_markerless_empty_weak_outbox_fails_before_storage_changes():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE integration_signal_outbox ("
            "seq INTEGER PRIMARY KEY, payload TEXT)"
        )
        original_journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
    original_bytes = Path(database.DB_PATH).read_bytes()

    with pytest.raises(EventCoreSchemaError, match="integration_signal_outbox"):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            original_journal_mode
        )
        assert [
            row[1]
            for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
        ] == ["seq", "payload"]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone() is None
    assert Path(database.DB_PATH).read_bytes() == original_bytes


def test_completed_marker_with_empty_weak_outbox_fails_before_storage_changes():
    import db.database as database

    database.init_db()
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("DROP TABLE integration_signal_outbox")
        conn.execute(
            "CREATE TABLE integration_signal_outbox ("
            "seq INTEGER PRIMARY KEY, payload TEXT)"
        )
        conn.commit()
        original_journal_mode = str(
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).lower()
    original_bytes = Path(database.DB_PATH).read_bytes()

    with pytest.raises(EventCoreSchemaError, match="integration_signal_outbox"):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            original_journal_mode
        )
        assert [
            row[1]
            for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
        ] == ["seq", "payload"]
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is not None
    assert Path(database.DB_PATH).read_bytes() == original_bytes


def test_sqlite_internal_tables_do_not_disqualify_an_otherwise_pristine_database():
    import db.database as database

    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE temporary_sequence_owner ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        conn.execute("DROP TABLE temporary_sequence_owner")
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        } == {"sqlite_sequence"}

    database.init_db()
    with database.get_connection() as conn:
        verify_event_core_backfill_state(conn)


def test_marker_present_weakened_schema_fails_before_storage_changes():
    import db.database as database

    database.init_db()
    conn = sqlite3.connect(database.DB_PATH)
    try:
        conn.execute("DROP TABLE event_mapping_replay_ledger")
        conn.execute(
            "CREATE TABLE event_mapping_replay_ledger (id INTEGER PRIMARY KEY)"
        )
        conn.commit()
        assert str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower() == (
            "delete"
        )
    finally:
        conn.close()
    original_bytes = Path(database.DB_PATH).read_bytes()

    with pytest.raises(EventCoreSchemaError):
        database.init_db()

    with sqlite3.connect(database.DB_PATH) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == (
            "delete"
        )
        assert [
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(event_mapping_replay_ledger)"
            ).fetchall()
        ] == ["id"]
    assert Path(database.DB_PATH).read_bytes() == original_bytes


def test_startup_and_verifier_reject_orphaned_event_reference():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        conn.execute("DROP TRIGGER trg_news_cache_event_insert")
        conn.execute("DROP TRIGGER trg_news_cache_event_version_insert")
        conn.execute(
            "INSERT INTO news_cache (link, title, event_id, event_version) "
            "VALUES ('https://example.com/orphaned', 'Orphaned', ?, 1)",
            (str(uuid.uuid4()),),
        )
        conn.commit()
        with pytest.raises(EventCoreSchemaError, match="invalid event reference"):
            verify_event_core_backfill_state(conn)

    with pytest.raises(EventCoreSchemaError, match="invalid event reference"):
        database.init_db()


@pytest.mark.parametrize("bad_version", [0, 2, 1.5])
def test_verifier_rejects_invalid_event_version(bad_version):
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        event_id = _insert_valid_news_event(conn)
        conn.execute("DROP TRIGGER trg_news_cache_event_version_update")
        conn.execute(
            "UPDATE news_cache SET event_version = ? WHERE event_id = ?",
            (bad_version, event_id),
        )
        conn.commit()
        with pytest.raises(EventCoreSchemaError, match="invalid event reference"):
            verify_event_core_backfill_state(conn)


def test_valid_existing_database_passes_startup_without_rotating_marker():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _insert_valid_news_event(conn)
        marker = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()[0]
        conn.commit()
        verify_event_core_backfill_state(conn)

    database.init_db()
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()[0] == marker
        verify_event_core_backfill_state(conn)


def _legacy_outbox_row(conn, ledger_signal_id: int) -> None:
    """Append one retained v2 row without advancing its cursor metadata."""
    conn.execute(
        "INSERT INTO integration_signal_outbox ("
        "ledger_signal_id, event_id, available_at, recorded_at, enqueued_at, "
        "score, direction, score_confidence, strategy_version, mapping_version, "
        "mapped_theme, backtest_symbol, score_path, revision_key, signal_stage, "
        "revision_seq, revision_state_key, independence_id, "
        "aggregation_threshold, selected, is_similarity_copy) "
        "VALUES (?, ?, 200, 201, 201, 8.2, '看多', 0.8, 'v1', '', '', NULL, "
        "'stage2', 'base', 'base', 0, '', ?, 7, 1, 0)",
        (ledger_signal_id, "a" * 64, "b" * 64),
    )


def test_verifier_and_startup_reject_unresolved_retained_outbox_reference():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _legacy_outbox_row(conn, 999)
        conn.commit()
        assert count_unresolved_event_core_outbox_references(conn) == 1
        with pytest.raises(EventCoreSchemaError, match="unresolved stable event"):
            verify_event_core_backfill_state(conn)

    with pytest.raises(EventCoreSchemaError, match="unresolved stable event"):
        database.init_db()


def test_valid_production_outbox_state_passes_startup_and_verification():
    import db.database as database

    database.init_db()
    now = int(time.time())
    assert database.store_backtest_signal(
        {
            "event_key": "valid-production-outbox",
            "independence_key": "valid-production-outbox",
            "link": "https://example.com/valid-production-outbox",
            "available_at": now,
            "scored_at": now,
            "score": 8.5,
            "direction": "neutral",
            "score_confidence": 0.8,
            "strategy_version": "test-v1",
            "mapping_version": "",
            "mapped_theme": "test",
            "score_path": "stage2",
            "aggregation_threshold": 7,
            "selected": True,
        }
    )
    with database.get_connection() as conn:
        marker = conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()[0]
        assert count_unresolved_event_core_outbox_references(conn) == 0
        verify_event_core_backfill_state(conn)

    database.init_db()
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()[0] == marker
        assert count_unresolved_event_core_outbox_references(conn) == 0
        verify_event_core_backfill_state(conn)


def test_explicit_backfill_assigns_one_uuid_to_news_ledger_and_legacy_audit_map():
    import db.database as database

    database.init_db()
    link = "https://example.com/policy?a=1#fragment"
    with database.get_connection() as conn:
        # Model a database created by the retired Radar event-cache release.
        # Fresh installations no longer create this second identity authority.
        database._init_event_tables(conn.cursor())
        _legacy_news(conn, link)
        _legacy_signal(conn, link)
        conn.execute(
            "INSERT INTO event_cache "
            "(event_id, canonical_title, canonical_link, score, tag, first_seen_at, "
            "last_seen_at, strategy_version) VALUES "
            "('deadbeefdeadbeef', '旧政策新闻', ?, 8.2, '政策', 100, 200, 'v1')",
            (link,),
        )
        conn.commit()

    with database.get_connection() as conn:
        result = backfill_stable_event_core(conn, now=300)
    assert result["already_complete"] is False

    with database.get_connection() as conn:
        news = conn.execute(
            "SELECT event_id, event_version FROM news_cache WHERE link = ?", (link,)
        ).fetchone()
        ledger = conn.execute(
            "SELECT event_id, event_version FROM backtest_signals WHERE link = ?", (link,)
        ).fetchone()
        mapped = conn.execute(
            "SELECT event_id FROM legacy_event_id_map WHERE legacy_event_id = 'deadbeefdeadbeef'"
        ).fetchone()
        assert str(uuid.UUID(news[0])) == news[0]
        assert tuple(ledger) == tuple(news)
        assert mapped[0] == news[0]
        assert news[0] != "deadbeefdeadbeef"
        assert conn.execute(
            "SELECT COUNT(*) FROM event_evidence WHERE event_id = ?", (news[0],)
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with database.get_connection() as conn:
        repeated = backfill_stable_event_core(conn, now=400)
    assert repeated["already_complete"] is True


def test_empty_legacy_radar_membership_table_allows_migration():
    import db.database as database

    database.init_db()
    link = "https://example.com/empty-legacy-radar-membership"
    with database.get_connection() as conn:
        database._init_event_tables(conn.cursor())
        _legacy_news(conn, link)
        assert conn.execute("SELECT COUNT(*) FROM event_articles").fetchone()[0] == 0
        conn.commit()

    with database.get_connection() as conn:
        result = backfill_stable_event_core(conn, now=300)

    assert result["already_complete"] is False
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT event_id FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0]
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone()[0] == "300"


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_cli_rejects_nonempty_legacy_radar_membership_without_partial_write(
    apply, tmp_path
):
    import db.database as database
    from scripts import migrate_event_core as migration_cli

    database.init_db()
    link = "https://example.com/nonempty-legacy-radar-membership"
    with database.get_connection() as conn:
        database._init_event_tables(conn.cursor())
        _legacy_news(conn, link)
        conn.execute(
            "INSERT INTO event_cache "
            "(event_id, canonical_title, canonical_link, source_count) "
            "VALUES ('legacy-radar-cluster', '旧多源事件', ?, 1)",
            (link,),
        )
        conn.execute(
            "INSERT INTO event_articles "
            "(event_id, link, feed_url, raw_title, score, created_at) "
            "VALUES ('legacy-radar-cluster', ?, 'https://feed.example/source', "
            "'旧多源事件', 8.2, 100)",
            (link,),
        )
        conn.commit()

    args = ["--database", database.DB_PATH]
    backup_path = tmp_path / "legacy-membership-pre-apply.bak"
    if apply:
        args.extend(["--backup", str(backup_path), "--apply"])

    with pytest.raises(
        RuntimeError,
        match=r"legacy Radar membership migration is unsupported: "
        r"event_articles contains 1 row\(s\)",
    ):
        migration_cli.main(args)

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert tuple(
            conn.execute(
                "SELECT event_id, event_version FROM news_cache WHERE link = ?", (link,)
            ).fetchone()
        ) == (None, 0)
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'legacy_event_id_map'"
        ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM event_articles").fetchone()[0] == 1

    # Apply takes its mandatory pre-migration backup under the same write lock,
    # but the unsupported source database itself remains completely untouched.
    if apply:
        assert backup_path.is_file()
        backup = sqlite3.connect(str(backup_path))
        try:
            assert backup.execute(
                "SELECT COUNT(*) FROM event_articles"
            ).fetchone()[0] == 1
            assert backup.execute(
                "SELECT event_id, event_version FROM news_cache WHERE link = ?", (link,)
            ).fetchone() == (None, 0)
        finally:
            backup.close()


def test_true_legacy_price_and_outbox_schema_is_upgraded_inside_migration():
    import db.database as database
    from db.integration_outbox import get_integration_outbox_state

    link = "https://example.com/true-legacy"
    with database.get_connection() as conn:
        cursor = conn.cursor()
        database._init_app_meta(cursor)
        database._init_news_cache(cursor)
        database._init_backtest_signals(cursor)
        cursor.execute(
            "CREATE TABLE future_prices ("
            "symbol TEXT NOT NULL, trade_date TEXT NOT NULL, name TEXT, "
            "close_price REAL NOT NULL, change_amt REAL, change_pct REAL, ts INTEGER, "
            "PRIMARY KEY (symbol, trade_date))"
        )
        cursor.execute(
            "INSERT INTO future_prices VALUES "
            "('sz399006', '2026-07-20', '创业板指', 100, 1, 1, 200)"
        )
        cursor.execute(
            "CREATE TABLE integration_signal_outbox ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ledger_signal_id INTEGER NOT NULL UNIQUE, "
            "event_id TEXT NOT NULL UNIQUE, "
            "available_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL, "
            "enqueued_at INTEGER NOT NULL, score REAL NOT NULL, "
            "direction TEXT NOT NULL, score_confidence REAL NOT NULL, "
            "strategy_version TEXT NOT NULL, mapping_version TEXT NOT NULL, "
            "mapped_theme TEXT NOT NULL, backtest_symbol TEXT, "
            "score_path TEXT NOT NULL, revision_key TEXT NOT NULL, "
            "signal_stage TEXT NOT NULL, revision_seq INTEGER NOT NULL, "
            "revision_state_key TEXT NOT NULL, independence_id TEXT NOT NULL, "
            "aggregation_threshold REAL NOT NULL, selected INTEGER NOT NULL, "
            "is_similarity_copy INTEGER NOT NULL)"
        )
        _legacy_news(conn, link)
        _legacy_signal(conn, link)
        ledger_id = int(
            conn.execute(
                "SELECT id FROM backtest_signals WHERE link = ?", (link,)
            ).fetchone()[0]
        )
        cursor.execute(
            "INSERT INTO integration_signal_outbox ("
            "ledger_signal_id, event_id, available_at, recorded_at, enqueued_at, "
            "score, direction, score_confidence, strategy_version, mapping_version, "
            "mapped_theme, backtest_symbol, score_path, revision_key, signal_stage, "
            "revision_seq, revision_state_key, independence_id, "
            "aggregation_threshold, selected, is_similarity_copy) "
            "VALUES (?, ?, 200, 201, 201, 8.2, '看多', 0.8, 'v1', '', '', NULL, "
            "'stage2', 'base', 'base', 0, '', ?, 7, 1, 0)",
            (ledger_id, "a" * 64, "b" * 64),
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO app_meta (k, v) VALUES (?, ?)",
            [
                ("integration_outbox_ledger_id", "c" * 32),
                ("integration_outbox_epoch", "d" * 32),
                ("integration_outbox_floor_seq", "1"),
                ("integration_outbox_last_seq", "1"),
                ("integration_outbox_backfill_v1", "1"),
            ],
        )
        conn.commit()

    with database.get_connection() as conn:
        before = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "news_cache",
                "backtest_signals",
                "future_prices",
                "integration_signal_outbox",
            )
        }
        assert "revision_id" not in {
            str(row[1]) for row in conn.execute("PRAGMA table_info(future_prices)")
        }
        assert "domain_event_id" not in {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(integration_signal_outbox)")
        }

    with database.get_connection() as conn:
        # The retained Outbox row is older than the normal 90-day cutoff.  An
        # explicit event migration must still preserve its replay window.
        result = backfill_stable_event_core(conn, now=10_000_000)

    assert result["already_complete"] is False
    assert result["validated_future_price_rows"] == 1
    assert result["validated_outbox_rows"] == 1
    with database.get_connection() as conn:
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in before
        }
        assert after == before
        price = conn.execute(
            "SELECT p.revision_id, r.symbol, r.trade_date, r.close_price "
            "FROM future_prices AS p JOIN future_price_revisions AS r "
            "ON r.revision_id = p.revision_id"
        ).fetchone()
        assert price[0]
        assert tuple(price[1:]) == ("sz399006", "2026-07-20", 100)
        ledger = conn.execute(
            "SELECT event_id, event_version FROM backtest_signals WHERE link = ?",
            (link,),
        ).fetchone()
        outbox = conn.execute(
            "SELECT domain_event_id, event_version, event_mapping_id "
            "FROM integration_signal_outbox"
        ).fetchone()
        assert tuple(outbox) == (ledger[0], ledger[1], None)
        assert get_integration_outbox_state(conn) == {
            "ledger_id": "c" * 32,
            "epoch": "d" * 32,
            "floor_seq": 1,
            "last_seq": 1,
        }
        validate_stable_event_core(conn)


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_cli_rolls_back_when_outbox_initializer_would_normalize_cursor_metadata(
    apply, tmp_path
):
    """A damaged retained cursor must never be silently rewritten by migration."""
    import db.database as database
    from scripts import migrate_event_core as migration_cli

    database.init_db()
    link = "https://example.com/outbox-metadata-drift"
    with database.get_connection() as conn:
        _legacy_news(conn, link)
        _legacy_signal(conn, link)
        ledger_id = int(
            conn.execute(
                "SELECT id FROM backtest_signals WHERE link = ?", (link,)
            ).fetchone()[0]
        )
        # init_db starts with floor=1,last=0.  Directly model a damaged legacy
        # producer that retained seq=1 but failed to advance last_seq.  The
        # Outbox initializer used to normalize this to last=1 and commit it.
        _legacy_outbox_row(conn, ledger_id)
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = 'integration_outbox_last_seq'"
        ).fetchone()[0] == "0"
        conn.commit()

    backup_path = tmp_path / "invalid-outbox-pre-apply.bak"
    args = ["--database", database.DB_PATH]
    if apply:
        args.extend(["--backup", str(backup_path), "--apply"])
    with pytest.raises(
        RuntimeError,
        match=(
            "changed integration outbox metadata "
            "integration_outbox_last_seq"
        ),
    ):
        migration_cli.main(args)

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert tuple(
            conn.execute(
                "SELECT event_id, event_version FROM news_cache WHERE link = ?",
                (link,),
            ).fetchone()
        ) == (None, 0)
        assert tuple(
            conn.execute(
                "SELECT domain_event_id, event_version, event_mapping_id "
                "FROM integration_signal_outbox"
            ).fetchone()
        ) == (None, None, None)
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = 'integration_outbox_last_seq'"
        ).fetchone()[0] == "0"
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None

    if apply:
        assert backup_path.is_file()
        backup = sqlite3.connect(str(backup_path))
        try:
            assert backup.execute(
                "SELECT v FROM app_meta "
                "WHERE k = 'integration_outbox_last_seq'"
            ).fetchone()[0] == "0"
            assert backup.execute(
                "SELECT COUNT(*) FROM integration_signal_outbox"
            ).fetchone()[0] == 1
        finally:
            backup.close()


def test_equal_count_legacy_business_rewrite_fails_and_rolls_back(monkeypatch):
    import db.database as database
    import db.event_migration as event_migration

    database.init_db()
    link = "https://example.com/equal-count-rewrite"
    with database.get_connection() as conn:
        _legacy_news(conn, link, title="不可改写的原标题")
        conn.commit()

    original_backfill = event_migration._backfill_news

    def corrupting_backfill(conn, migrated_at):
        updated = original_backfill(conn, migrated_at)
        conn.execute(
            "UPDATE news_cache SET title = '被等量改写' WHERE link = ?", (link,)
        )
        return updated

    monkeypatch.setattr(event_migration, "_backfill_news", corrupting_backfill)
    with database.get_connection() as conn:
        with pytest.raises(
            RuntimeError,
            match="changed news_cache legacy business data",
        ):
            backfill_stable_event_core(conn, now=300)

    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT title FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0] == "不可改写的原标题"
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None


def test_backfill_failure_rolls_back_rows_and_completion_marker():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        _legacy_news(conn, "")
        conn.commit()

    with database.get_connection() as conn:
        with pytest.raises(ValueError, match="article link"):
            backfill_stable_event_core(conn, now=300)

    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None
        row = conn.execute(
            "SELECT event_id, event_version FROM news_cache WHERE link = ''"
        ).fetchone()
        assert tuple(row) == (None, 0)


def test_migration_rejects_caller_owned_transaction():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE touched (id INTEGER)")
    conn.execute("INSERT INTO touched VALUES (1)")
    with pytest.raises(RuntimeError, match="clean connection"):
        backfill_stable_event_core(conn)
    conn.rollback()
    conn.close()


def test_application_connections_enforce_event_references():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(
            sqlite3.IntegrityError,
            match="unknown news_cache event_id|invalid news_cache event version",
        ):
            conn.execute(
                "INSERT INTO news_cache (link, title, event_id, event_version) "
                "VALUES ('https://example.com/orphan', 'orphan', ?, 1)",
                (str(uuid.uuid4()),),
            )
        conn.rollback()


def test_completed_force_is_read_only_and_preserves_frozen_mapping_revisions():
    import db.database as database

    database.init_db()
    link = "https://example.com/mapped-policy"
    with database.get_connection() as conn:
        _legacy_news(conn, link)
        _legacy_signal(conn, link)
        conn.execute(
            "UPDATE backtest_signals SET backtest_symbol = 'sh000001', "
            "score_confidence = 0.8, mapped_theme = '大盘', mapping_version = 'map-v1' "
            "WHERE link = ?",
            (link,),
        )
        conn.commit()

    with database.get_connection() as conn:
        backfill_stable_event_core(conn, now=300)
    with database.get_connection() as conn:
        frozen = conn.execute(
            "SELECT event_mapping_id FROM backtest_signals WHERE link = ?", (link,)
        ).fetchone()[0]
        mapping_count = conn.execute(
            "SELECT COUNT(*) FROM event_securities"
        ).fetchone()[0]
        before_changes = conn.total_changes
        traced: list[str] = []
        conn.set_trace_callback(traced.append)
        result = backfill_stable_event_core(conn, now=400, force=True)
        conn.set_trace_callback(None)

        assert result["already_complete"] is True
        assert conn.total_changes == before_changes
        assert conn.execute(
            "SELECT event_mapping_id FROM backtest_signals WHERE link = ?", (link,)
        ).fetchone()[0] == frozen
        assert conn.execute(
            "SELECT COUNT(*) FROM event_securities"
        ).fetchone()[0] == mapping_count
        mutating = ("BEGIN IMMEDIATE", "CREATE ", "ALTER ", "DROP ", "UPDATE ", "INSERT ", "DELETE ")
        assert not [sql for sql in traced if sql.lstrip().upper().startswith(mutating)]


def test_completed_migration_guards_versions_and_mapping_references():
    import db.database as database

    database.init_db()
    link = "https://example.com/reference-guards"
    with database.get_connection() as conn:
        _legacy_news(conn, link)
        _legacy_signal(conn, link)
        conn.execute(
            "UPDATE backtest_signals SET backtest_symbol = 'sh000001', "
            "score_confidence = 0.8 WHERE link = ?",
            (link,),
        )
        conn.commit()
    with database.get_connection() as conn:
        backfill_stable_event_core(conn, now=300)

    with database.get_connection() as conn:
        validate_stable_event_core(conn)
        with pytest.raises(sqlite3.IntegrityError, match="invalid news_cache event version"):
            conn.execute(
                "UPDATE news_cache SET event_version = 999 WHERE link = ?", (link,)
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="invalid backtest event mapping"):
            conn.execute(
                "UPDATE backtest_signals SET event_mapping_id = ? WHERE link = ?",
                (str(uuid.uuid4()), link),
            )
        conn.rollback()


def test_validation_rejects_outbox_sequence_metadata_gap():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        validate_stable_event_core(conn)
        conn.execute(
            "UPDATE app_meta SET v = '1' "
            "WHERE k = 'integration_outbox_last_seq'"
        )
        conn.commit()

    with database.get_connection() as conn:
        with pytest.raises(RuntimeError, match="retained gap"):
            validate_stable_event_core(conn)


def test_validation_rejects_mismatched_current_future_price_revision():
    import db.database as database

    database.init_db()
    database.store_future_prices(
        [
            {
                "symbol": "sz399006",
                "trade_date": "2026-07-20",
                "name": "创业板指",
                "close_price": 100,
                "change_amt": 1,
                "change_pct": 1,
                "ts": 100,
            },
            {
                "symbol": "sh000001",
                "trade_date": "2026-07-20",
                "name": "上证综指",
                "close_price": 200,
                "change_amt": 2,
                "change_pct": 1,
                "ts": 100,
            },
        ]
    )
    with database.get_connection() as conn:
        wrong_revision = conn.execute(
            "SELECT revision_id FROM future_prices WHERE symbol = 'sh000001'"
        ).fetchone()[0]
        # The current schema rejects this write at the boundary.  Drop the
        # guard solely to exercise the offline validator against a legacy or
        # externally imported corrupt database.
        conn.execute("DROP TRIGGER trg_future_prices_revision_update")
        conn.execute(
            "UPDATE future_prices SET revision_id = ? WHERE symbol = 'sz399006'",
            (wrong_revision,),
        )
        conn.commit()

    with database.get_connection() as conn:
        with pytest.raises(RuntimeError, match="mismatched immutable revision"):
            validate_stable_event_core(conn)


def test_future_price_revision_v2_includes_normalized_source_timestamp():
    import db.database as database

    database.init_db()
    price = {
        "symbol": "sz399006",
        "trade_date": "2026-07-20",
        "name": "创业板指",
        "close_price": 100,
        "change_amt": 1,
        "change_pct": 1,
        "ts": "100",
    }
    database.store_future_prices([price])
    with database.get_connection() as conn:
        first_revision = conn.execute(
            "SELECT revision_id FROM future_prices WHERE symbol = 'sz399006'"
        ).fetchone()[0]

    database.store_future_prices([{**price, "ts": 100}])
    database.store_future_prices([{**price, "ts": 101}])
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT revision_id, ts FROM future_prices WHERE symbol = 'sz399006'"
        ).fetchone()
        revisions = conn.execute(
            "SELECT revision_id, source_ts FROM future_price_revisions "
            "WHERE symbol = 'sz399006' ORDER BY source_ts"
        ).fetchall()
    assert current[0] != first_revision
    assert tuple(current) == (revisions[1][0], 101)
    assert [row[1] for row in revisions] == [100, 101]


def test_future_price_current_pointer_is_monotonic_and_same_second_deterministic(
    monkeypatch,
):
    import db.database as database

    database.init_db()
    monkeypatch.setattr(database.time, "time", lambda: 1_000)

    def price(trade_date, close, source_ts):
        return {
            "symbol": "sz399006",
            "trade_date": trade_date,
            "name": "创业板指",
            "close_price": close,
            "change_amt": 0,
            "change_pct": 0,
            "ts": source_ts,
        }

    # An older vendor snapshot arriving last remains in the ledger but cannot
    # move the compatibility/current pointer backward.
    database.store_future_prices([price("2026-07-20", 200, 200)])
    database.store_future_prices([price("2026-07-20", 100, 100)])
    with database.get_connection() as conn:
        current = conn.execute(
            "SELECT close_price, ts, revision_id FROM future_prices "
            "WHERE symbol = 'sz399006' AND trade_date = '2026-07-20'"
        ).fetchone()
        assert tuple(current[:2]) == (200, 200)
        assert conn.execute(
            "SELECT COUNT(*) FROM future_price_revisions "
            "WHERE symbol = 'sz399006' AND trade_date = '2026-07-20'"
        ).fetchone()[0] == 2

    # Distinct corrections with the same source and recorded second use the
    # immutable UUID as a final total-order tie-breaker.  Invocation order does
    # not decide which snapshot becomes current.
    for trade_date, closes in (
        ("2026-07-21", (101, 102)),
        ("2026-07-22", (102, 101)),
    ):
        for close in closes:
            database.store_future_prices([price(trade_date, close, 300)])
        with database.get_connection() as conn:
            revision_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT revision_id FROM future_price_revisions "
                    "WHERE symbol = 'sz399006' AND trade_date = ?",
                    (trade_date,),
                ).fetchall()
            ]
            current_revision = str(
                conn.execute(
                    "SELECT revision_id FROM future_prices "
                    "WHERE symbol = 'sz399006' AND trade_date = ?",
                    (trade_date,),
                ).fetchone()[0]
            )
        assert current_revision == max(revision_ids)


def test_future_price_pointer_update_requires_the_global_best_revision(monkeypatch):
    import db.database as database

    database.init_db()
    monkeypatch.setattr(database.time, "time", lambda: 1_000)

    def price(close, source_ts):
        return {
            "symbol": "SSE:GLOBAL-BEST",
            "trade_date": "2026-07-20",
            "name": "global-best-regression",
            "close_price": close,
            "change_amt": close / 10,
            "change_pct": close / 100,
            "ts": source_ts,
        }

    for close, source_ts in ((100, 100), (200, 200), (300, 300)):
        database.store_future_prices([price(close, source_ts)])

    revision_projection = (
        "name, close_price, change_amt, change_pct, open_price, high_price, "
        "low_price, prev_close, volume, amount, limit_up, limit_down, "
        "suspension_state, limit_state, tradability_state, source_ts, revision_id"
    )
    pointer_assignments = (
        "name = ?, close_price = ?, change_amt = ?, change_pct = ?, "
        "open_price = ?, high_price = ?, low_price = ?, prev_close = ?, "
        "volume = ?, amount = ?, limit_up = ?, limit_down = ?, "
        "suspension_state = ?, limit_state = ?, tradability_state = ?, "
        "ts = ?, revision_id = ?"
    )
    pointer_where = (
        " WHERE symbol = 'SSE:GLOBAL-BEST' AND trade_date = '2026-07-20'"
    )
    with database.get_connection() as conn:
        revisions = [
            tuple(row)
            for row in conn.execute(
                f"SELECT {revision_projection} FROM future_price_revisions "
                "WHERE symbol = 'SSE:GLOBAL-BEST' AND trade_date = '2026-07-20' "
                "ORDER BY source_ts"
            ).fetchall()
        ]
        assert len(revisions) == 3
        conn.execute("DROP TRIGGER trg_future_prices_revision_update")
        conn.execute(
            f"UPDATE future_prices SET {pointer_assignments}{pointer_where}",
            revisions[0],
        )
        conn.commit()

    # Initialization upgrades the guard.  Moving from A to B is monotonic but
    # still illegal while the globally newest C exists; moving directly to C
    # remains valid.
    database.init_db()
    with database.get_connection() as conn:
        with pytest.raises(
            sqlite3.IntegrityError,
            match=database._FUTURE_PRICE_POINTER_TRIGGER_MARKER,
        ):
            conn.execute(
                f"UPDATE future_prices SET {pointer_assignments}{pointer_where}",
                revisions[1],
            )
        assert conn.execute(
            "SELECT revision_id FROM future_prices WHERE symbol = 'SSE:GLOBAL-BEST'"
        ).fetchone()[0] == revisions[0][-1]
        conn.execute(
            f"UPDATE future_prices SET {pointer_assignments}{pointer_where}",
            revisions[2],
        )
        assert conn.execute(
            "SELECT revision_id FROM future_prices WHERE symbol = 'SSE:GLOBAL-BEST'"
        ).fetchone()[0] == revisions[2][-1]
        conn.commit()


def test_future_price_store_recovers_invalid_pointers_only_with_global_best(
    monkeypatch,
):
    import db.database as database

    database.init_db()
    monkeypatch.setattr(database.time, "time", lambda: 1_000)

    def price(symbol, trade_date, close, source_ts):
        return {
            "symbol": symbol,
            "trade_date": trade_date,
            "name": f"name-{symbol}",
            "close_price": close,
            "change_amt": close / 10,
            "change_pct": close / 100,
            "open_price": close - 1,
            "high_price": close + 2,
            "low_price": close - 2,
            "prev_close": close - 3,
            "volume": close * 100,
            "amount": close * 1000,
            "limit_up": close + 10,
            "limit_down": close - 10,
            "suspension_state": "trading",
            "limit_state": "not_at_limit",
            "tradability_state": "trading_ohlc",
            "ts": source_ts,
        }

    cases = {
        "dangling": ("SSE:DANGLING", "2026-07-20"),
        "empty": ("SSE:EMPTY", "2026-07-20"),
        "cross": ("SSE:CROSS", "2026-07-20"),
        "payload": ("SSE:PAYLOAD", "2026-07-20"),
    }
    snapshots = {}
    for symbol, trade_date in cases.values():
        old = price(symbol, trade_date, 100, 100)
        best = price(symbol, trade_date, 200, 200)
        database.store_future_prices([old, best])
        snapshots[symbol] = (old, best)

    with database.get_connection() as conn:
        donor_revision = conn.execute(
            "SELECT revision_id FROM future_price_revisions "
            "WHERE symbol = 'SSE:DANGLING' AND source_ts = 200"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_future_prices_revision_update")
        conn.execute(
            "UPDATE future_prices SET revision_id = ? WHERE symbol = 'SSE:DANGLING'",
            (str(uuid.uuid4()),),
        )
        conn.execute(
            "UPDATE future_prices SET revision_id = '' WHERE symbol = 'SSE:EMPTY'"
        )
        conn.execute(
            "UPDATE future_prices SET revision_id = ? WHERE symbol = 'SSE:CROSS'",
            (donor_revision,),
        )
        conn.execute(
            "UPDATE future_prices SET name = 'corrupt', close_price = -1, "
            "open_price = -2, volume = -3, suspension_state = 'corrupt', ts = -4 "
            "WHERE symbol = 'SSE:PAYLOAD'"
        )
        conn.commit()

    # Empty IDs with an existing ledger are corruption, not legacy rows to be
    # frozen.  Initialization installs the complete mirror trigger once.
    database.init_db()
    with database.get_connection() as conn:
        trigger_sql_before = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN ('trg_future_prices_revision_insert', "
                "'trg_future_prices_revision_update') ORDER BY name"
            ).fetchall()
        )
        assert len(trigger_sql_before) == 2
        assert all(
            database._FUTURE_PRICE_POINTER_TRIGGER_MARKER in str(row[1])
            for row in trigger_sql_before
        )
    database.init_db()
    with database.get_connection() as conn:
        trigger_sql_after = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN ('trg_future_prices_revision_insert', "
                "'trg_future_prices_revision_update') ORDER BY name"
            ).fetchall()
        )
    assert trigger_sql_after == trigger_sql_before

    invalid_before = {}
    with database.get_connection() as conn:
        for label in ("dangling", "empty", "cross"):
            symbol, _ = cases[label]
            invalid_before[symbol] = conn.execute(
                "SELECT revision_id FROM future_prices WHERE symbol = ?",
                (symbol,),
            ).fetchone()[0]

    # Replaying a non-best ledger revision cannot bless a damaged pointer.
    for label in ("dangling", "empty", "cross"):
        symbol, _ = cases[label]
        database.store_future_prices([snapshots[symbol][0]])
    with database.get_connection() as conn:
        assert {
            str(row[0]): row[1]
            for row in conn.execute(
                "SELECT symbol, revision_id FROM future_prices "
                "WHERE symbol IN ('SSE:DANGLING', 'SSE:EMPTY', 'SSE:CROSS')"
            ).fetchall()
        } == invalid_before

    # Replaying each global best heals dangling, empty, and cross-key IDs.  A
    # replay of the same best ID also rewrites a corrupted compatibility payload.
    for symbol in snapshots:
        database.store_future_prices([snapshots[symbol][1]])
    with database.get_connection() as conn:
        mirror = database._future_price_pointer_matches_revision_sql(
            "pointer",
            "revision",
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM future_prices AS pointer "
            "JOIN future_price_revisions AS revision "
            "ON revision.revision_id = pointer.revision_id "
            f"WHERE pointer.symbol LIKE 'SSE:%' AND NOT ({mirror})"
        ).fetchone()[0] == 0
        with pytest.raises(
            sqlite3.IntegrityError,
            match=database._FUTURE_PRICE_POINTER_TRIGGER_MARKER,
        ):
            conn.execute(
                "UPDATE future_prices SET close_price = -999 "
                "WHERE symbol = 'SSE:PAYLOAD'"
            )

    insert_old = price("SSE:INSERT", "2026-07-20", 100, 100)
    insert_best = price("SSE:INSERT", "2026-07-20", 200, 200)
    database.store_future_prices([insert_old, insert_best])
    with database.get_connection() as conn:
        ids = {
            int(row[1]): str(row[0])
            for row in conn.execute(
                "SELECT revision_id, source_ts FROM future_price_revisions "
                "WHERE symbol = 'SSE:INSERT'"
            ).fetchall()
        }
        conn.execute("DELETE FROM future_prices WHERE symbol = 'SSE:INSERT'")
        insert_columns = (
            "symbol, trade_date, name, close_price, change_amt, change_pct, "
            "open_price, high_price, low_price, prev_close, volume, amount, "
            "limit_up, limit_down, suspension_state, limit_state, "
            "tradability_state, ts, revision_id"
        )
        revision_projection = (
            "symbol, trade_date, name, close_price, change_amt, change_pct, "
            "open_price, high_price, low_price, prev_close, volume, amount, "
            "limit_up, limit_down, suspension_state, limit_state, "
            "tradability_state, source_ts, revision_id"
        )
        # A missing pointer can only be reconstructed from the total-order
        # best, and even that row must mirror every immutable field.
        with pytest.raises(
            sqlite3.IntegrityError,
            match=database._FUTURE_PRICE_POINTER_TRIGGER_MARKER,
        ):
            conn.execute(
                f"INSERT INTO future_prices ({insert_columns}) "
                f"SELECT {revision_projection} FROM future_price_revisions "
                "WHERE revision_id = ?",
                (ids[100],),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match=database._FUTURE_PRICE_POINTER_TRIGGER_MARKER,
        ):
            conn.execute(
                f"INSERT INTO future_prices ({insert_columns}) "
                "SELECT symbol, trade_date, 'corrupt', close_price, change_amt, "
                "change_pct, open_price, high_price, low_price, prev_close, volume, "
                "amount, limit_up, limit_down, suspension_state, limit_state, "
                "tradability_state, source_ts, revision_id "
                "FROM future_price_revisions WHERE revision_id = ?",
                (ids[200],),
            )
        assert conn.execute(
            "SELECT 1 FROM future_prices WHERE symbol = 'SSE:INSERT'"
        ).fetchone() is None
        conn.commit()

    # The storage path uses UPDATE-first so SQLite never invokes the strict
    # BEFORE INSERT trigger for a zero-row/non-best replay.  The later best
    # snapshot is the only revision allowed to recreate the missing pointer.
    database.store_future_prices([insert_old])
    assert database.get_future_price("SSE:INSERT", "2026-07-20") is None
    database.store_future_prices([insert_best])
    recreated = database.get_future_price("SSE:INSERT", "2026-07-20")
    assert recreated is not None
    assert recreated["revision_id"] == ids[200]
    assert recreated["close_price"] == 200


def test_future_price_v1_revision_is_reused_after_identity_upgrade():
    import db.database as database

    database.init_db()
    price = {
        "symbol": "sz399006",
        "trade_date": "2026-07-20",
        "name": "创业板指",
        "close_price": 100,
        "change_amt": 1,
        "change_pct": 1,
        "ts": 100,
    }
    legacy_id = database._legacy_future_price_revision_id(
        price["symbol"], price["trade_date"], price["name"],
        price["close_price"], price["change_amt"], price["change_pct"],
        suspension_state=None, limit_state=None, tradability_state=None,
    )
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO future_price_revisions "
            "(revision_id, symbol, trade_date, name, close_price, change_amt, "
            "change_pct, source_ts, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 100, 100)",
            (legacy_id, price["symbol"], price["trade_date"], price["name"], 100, 1, 1),
        )
        conn.execute(
            "INSERT INTO future_prices "
            "(symbol, trade_date, name, close_price, change_amt, change_pct, ts, revision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 100, ?)",
            (price["symbol"], price["trade_date"], price["name"], 100, 1, 1, legacy_id),
        )
        conn.commit()

    database.store_future_prices([price])
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT revision_id FROM future_prices WHERE symbol = 'sz399006'"
        ).fetchone()[0] == legacy_id
        assert conn.execute(
            "SELECT COUNT(*) FROM future_price_revisions WHERE symbol = 'sz399006'"
        ).fetchone()[0] == 1


def test_validation_rejects_current_future_price_source_timestamp_drift():
    import db.database as database

    database.init_db()
    database.store_future_prices([{
        "symbol": "sz399006", "trade_date": "2026-07-20", "name": "创业板指",
        "close_price": 100, "change_amt": 1, "change_pct": 1, "ts": 100,
    }])
    with database.get_connection() as conn:
        conn.execute("DROP TRIGGER trg_future_prices_revision_update")
        conn.execute("UPDATE future_prices SET ts = 101 WHERE symbol = 'sz399006'")
        conn.commit()
        with pytest.raises(RuntimeError, match="mismatched immutable revision"):
            validate_stable_event_core(conn)


def test_signal_mapping_snapshot_rejects_and_validates_cross_event_binding():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        signal_event = resolve_or_create_event(
            conn, {"test": "signal-event"}, title="信号事件", now=100
        )
        mapping_event = resolve_or_create_event(
            conn, {"test": "mapping-event"}, title="映射事件", now=100
        )
        security_id = upsert_security(
            conn, exchange="SZSE", ticker="399006", name="创业板指", now=100
        )
        mapping_id = upsert_event_security(
            conn, mapping_event, security_id, mapping_version="map-v1", now=100
        )
        event_version = conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (signal_event,)
        ).fetchone()[0]
        ledger_id = conn.execute(
            "INSERT INTO backtest_signals "
            "(signal_key, link, available_at, scored_at, recorded_at, score, direction, "
            "mapping_version, aggregation_threshold, event_id, event_version) "
            "VALUES ('cross-event', 'https://example.com/cross-event', 200, 200, 200, "
            "8, '看多', 'map-v1', 7, ?, ?)",
            (signal_event, event_version),
        ).lastrowid
        snapshot = (ledger_id, mapping_id, mapping_event, security_id, 1, 200, 200)
        insert_sql = (
            "INSERT INTO backtest_signal_securities "
            "(ledger_signal_id, mapping_id, event_id, security_id, mapping_revision, "
            "signal_available_at, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not match signal ledger"):
            conn.execute(insert_sql, snapshot)

        conn.execute("DROP TRIGGER trg_backtest_signal_securities_insert")
        conn.execute(insert_sql, snapshot)
        database._init_backtest_signal_securities(conn.cursor())
        with pytest.raises(RuntimeError, match="invalid snapshot"):
            validate_stable_event_core(conn)


@pytest.mark.parametrize(
    ("data_version", "message"),
    [
        ("future-prices-v2:not-four-revisions", "invalid price data_version"),
        (
            "future-prices-v2:" + ":".join(str(uuid.uuid4()) for _ in range(4)),
            "missing future price revision",
        ),
    ],
)
def test_validation_rejects_invalid_or_missing_outcome_price_revisions(
    data_version, message
):
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn,
            {"test": data_version},
            title="行情引用审计",
            now=100,
        )
        security_id = upsert_security(
            conn,
            exchange="SZSE",
            ticker="399006",
            name="创业板指",
            security_id="sz399006",
            asset_type="index",
            now=100,
        )
        benchmark_id = upsert_security(
            conn,
            exchange="SSE",
            ticker="000001",
            name="上证综指",
            security_id="sh000001",
            asset_type="index",
            now=100,
        )
        mapping_id = upsert_event_security(
            conn,
            event_id,
            security_id,
            mapping_version="test-v1",
            now=101,
        )
        upsert_event_outcome(
            conn,
            event_id,
            security_id,
            mapping_id=mapping_id,
            horizon="T+1",
            baseline_at=100,
            measured_at=200,
            benchmark_security_id=benchmark_id,
            method_version="a-share-event-day-close-v2",
            data_version=data_version,
            now=200,
        )

        with pytest.raises(RuntimeError, match=message):
            validate_stable_event_core(conn)


def test_script_main_without_apply_uses_only_in_memory_copy(capsys):
    import db.database as database
    from scripts import migrate_event_core as migration_cli

    database.init_db()
    link = "https://example.com/dry-run"
    with database.get_connection() as conn:
        _legacy_news(conn, link)
        conn.commit()

    assert migration_cli.main(["--database", database.DB_PATH]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None


def test_apply_holds_write_lock_across_backup_and_migration(monkeypatch, tmp_path):
    import db.database as database
    from scripts import migrate_event_core as migration_cli

    database.init_db()
    link = "https://example.com/locked-backup"
    with database.get_connection() as conn:
        _legacy_news(conn, link)
        conn.commit()
    backup_path = tmp_path / "pre-migration.bak"
    original_backup = migration_cli._backup_under_write_lock
    lock_observed = {"value": False}

    def guarded_backup(lock_conn, source, destination):
        assert lock_conn.in_transaction
        writer = sqlite3.connect(str(source), timeout=0.01)
        writer.execute("PRAGMA busy_timeout=1")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
            lock_observed["value"] = True
        finally:
            writer.close()
        return original_backup(lock_conn, source, destination)

    monkeypatch.setattr(migration_cli, "_backup_under_write_lock", guarded_backup)
    assert migration_cli.main(
        [
            "--database",
            database.DB_PATH,
            "--backup",
            str(backup_path),
            "--apply",
        ]
    ) == 0
    assert lock_observed["value"] is True
    assert backup_path.stat().st_mode & 0o777 == 0o600

    backup = sqlite3.connect(str(backup_path))
    try:
        assert backup.execute(
            "SELECT event_id, event_version FROM news_cache WHERE link = ?", (link,)
        ).fetchone() == (None, 0)
        assert backup.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is None
    finally:
        backup.close()
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT event_id, event_version FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0]
        assert conn.execute(
            "SELECT 1 FROM app_meta WHERE k = ?", (BACKFILL_META_KEY,)
        ).fetchone() is not None
