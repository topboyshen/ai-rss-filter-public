"""Origin diagnostics add no interpretation to historical processing rows."""

import sqlite3
import time

import pytest

from db import database
from db import news_processing as storage


REFRESH = "0123456789abcdef0123456789abcdef"
_OLD_COLUMNS = (
    "processing_id", "article_key", "link", "feed_url", "strategy_version",
    "started_at", "finished_at", "outcome", "state", "error_type", "model_attempts",
    "result_returned", "reuse_kind", "signal_key", "score", "score_path",
    "aggregation_selected", "signal_association",
)


def _row(pid="origin-fixture", **changes):
    return {
        "processing_id": pid,
        "link": "https://article.invalid/continuation-origin",
        "feed_url": "https://feed.invalid/continuation-origin",
        "strategy_version": "origin-storage-fixture",
        "started_at": time.time() - 1,
        "finished_at": time.time(),
        "outcome": "score_deferred",
        "state": "deferred",
        "model_attempts": 2,
        "result_returned": False,
        **changes,
    }


def _key():
    row = _row()
    return database._backtest_signal_key(row["link"], row["strategy_version"])


@pytest.fixture
def initialized():
    database.init_db()
    return database


def _legacy_insert(conn, pid):
    values = (
        pid, "legacy-article", "https://article.invalid/legacy", "legacy-feed", "legacy-strategy",
        100.0, 101.0, "filtered", "completed", "", 1, 0, "", None, None, None, None, "none",
    )
    conn.execute(
        f"INSERT INTO news_processing_logs ({','.join(_OLD_COLUMNS)}) "
        f"VALUES ({','.join('?' for _ in _OLD_COLUMNS)})",
        values,
    )
    return values


def test_existing_processing_rows_keep_all_values_and_unknown_origin():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("""CREATE TABLE news_processing_logs (
            processing_id TEXT PRIMARY KEY NOT NULL, article_key TEXT NOT NULL,
            link TEXT NOT NULL, feed_url TEXT NOT NULL DEFAULT '',
            strategy_version TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,
            finished_at REAL NOT NULL, outcome TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL CHECK(state IN ('completed','deferred','error')),
            error_type TEXT NOT NULL DEFAULT '', model_attempts INTEGER NOT NULL DEFAULT 0,
            result_returned INTEGER NOT NULL DEFAULT 0, reuse_kind TEXT NOT NULL DEFAULT '',
            signal_key TEXT, score REAL, score_path TEXT, aggregation_selected INTEGER,
            signal_association TEXT NOT NULL DEFAULT 'none'
        )""")
        before = _legacy_insert(conn, "legacy-before")
        storage.init_news_processing_storage(conn.cursor())
        schema = conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        storage.init_news_processing_storage(conn.cursor())
        assert conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == schema
        migrated = conn.execute("SELECT * FROM news_processing_logs").fetchone()
        assert migrated[:len(before)] == before
        assert migrated[len(before):] == ("unknown", "", "", None)
        # An actual old-style INSERT still works after the additive migration.
        after = _legacy_insert(conn, "legacy-after")
        inserted = conn.execute(
            "SELECT * FROM news_processing_logs WHERE processing_id='legacy-after'",
        ).fetchone()
        assert inserted[:len(after)] == after
        assert inserted[len(after):] == ("unknown", "", "", None)


@pytest.mark.parametrize("source", ["unknown", "natural_feed", "natural_continuation", "webhook"])
@pytest.mark.parametrize("attempt", [None, 0, 1, 2, 3])
def test_new_processing_trace_returns_exact_origin_fields(initialized, source, attempt):
    row = _row(
        trigger_source=source, refresh_id=REFRESH,
        continuation_key=_key() if attempt is not None else "",
        continuation_attempt=attempt,
    )
    assert initialized.record_news_processing(row)
    result = initialized.get_news_processing_trace(row["processing_id"])["processing"]
    assert result["trigger_source"] == source
    assert result["refresh_id"] == REFRESH
    assert result["continuation_key"] == (_key() if attempt is not None else "")
    assert result["continuation_attempt"] == attempt
    assert result["model_attempts"] == 2
    assert result["article_key"] == initialized._backtest_signal_key(
        row["link"], row["strategy_version"],
    )


def test_duplicate_processing_id_never_relabels_existing_execution(initialized):
    row = _row(trigger_source="webhook", continuation_key=_key(), continuation_attempt=1)
    assert initialized.record_news_processing(row)
    before = initialized.get_news_processing_trace(row["processing_id"])["processing"]
    assert not initialized.record_news_processing({
        **row, "trigger_source": "natural_continuation", "refresh_id": REFRESH,
        "continuation_attempt": 2,
    })
    assert initialized.get_news_processing_trace(row["processing_id"])["processing"] == before


@pytest.mark.parametrize("field,value", [
    ("trigger_source", "cron"), ("trigger_source", "natural_continuation\ncredential"),
    ("trigger_source", None), ("trigger_source", {}),
    ("refresh_id", "f" * 33), ("refresh_id", "x" * 32), ("refresh_id", None),
    ("refresh_id", 123), ("refresh_id", " " + REFRESH),
    ("continuation_key", "f" * 63), ("continuation_key", "x" * 64),
    ("continuation_key", []), ("continuation_key", None),
    ("continuation_attempt", -1), ("continuation_attempt", 4),
    ("continuation_attempt", True), ("continuation_attempt", "2"),
    ("continuation_attempt", 2.0), ("continuation_attempt", float("inf")),
    ("continuation_attempt", float("nan")),
])
def test_invalid_origin_drops_only_diagnostic_row(initialized, caplog, field, value):
    row = _row(**{field: value})
    assert initialized.record_news_processing(row) is False
    assert initialized.get_news_processing_trace(row["processing_id"])["found"] is False
    assert "diagnostic insert skipped" in caplog.text
    assert "credential" not in caplog.text
    with initialized.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM news_cache").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM backtest_signals").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"continuation_key": "a" * 64, "continuation_attempt": 1},
    {"continuation_attempt": 0},
    {"continuation_attempt": 3},
])
def test_wrong_or_missing_journal_identity_does_not_claim_continuation(initialized, changes):
    assert not initialized.record_news_processing(_row(**changes))


def test_journal_key_without_attempt_is_not_partial_attribution(initialized):
    assert not initialized.record_news_processing(_row(continuation_key=_key()))


def test_origin_migration_does_not_modify_business_tables(initialized):
    with initialized.get_connection() as conn:
        tables = ("news_cache", "backtest_signals", "llm_call_logs")
        before = {
            name: (conn.execute(f"PRAGMA table_info({name})").fetchall(),
                   conn.execute(f"SELECT * FROM {name}").fetchall())
            for name in tables
        }
        storage.init_news_processing_storage(conn.cursor())
        storage.init_news_processing_storage(conn.cursor())
        after = {
            name: (conn.execute(f"PRAGMA table_info({name})").fetchall(),
                   conn.execute(f"SELECT * FROM {name}").fetchall())
            for name in tables
        }
        assert before == after
