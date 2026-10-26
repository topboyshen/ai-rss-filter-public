import sqlite3
import sys
import threading
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _row_for(database, url):
    return next(row for row in database.get_feed_health() if row[0] == url)


def test_feed_health_migrates_legacy_schema_and_backfills_last_nonempty(
    tmp_path, monkeypatch
):
    import db.database as database

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE feed_health ("
            "url TEXT PRIMARY KEY, name TEXT, last_check INTEGER, "
            "response_time_ms INTEGER, success INTEGER DEFAULT 0, "
            "fail INTEGER DEFAULT 0, articles_total INTEGER DEFAULT 0, "
            "articles_passed INTEGER DEFAULT 0, avg_score REAL, last_error TEXT)"
        )
        conn.execute(
            "INSERT INTO feed_health VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://example.com/legacy",
                "Legacy",
                1234,
                80,
                4,
                0,
                9,
                2,
                7.2,
                None,
            ),
        )

    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()

    row = _row_for(database, "https://example.com/legacy")
    assert len(row) == 12
    assert row[10] == 0
    assert row[11] == 1234


def test_feed_health_tracks_empty_streak_without_corrupting_transport_health(
    tmp_path, monkeypatch
):
    import db.database as database

    db_path = tmp_path / "health.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    timestamps = iter([100, 200, 300, 400, 500])
    monkeypatch.setattr(database.time, "time", lambda: next(timestamps))
    url = "https://example.com/feed"

    database.update_feed_health(url, "Feed", 50, True, 4, 1, 7.5)
    row = _row_for(database, url)
    assert row[4:6] == (1, 0)
    assert row[10] == 0
    assert row[11] == 100

    database.update_feed_health(url, "Feed", 40, True, 0, 0, None)
    database.update_feed_health(url, "Feed", 45, True, 0, 0, None)
    row = _row_for(database, url)
    assert row[4:6] == (3, 0)
    assert row[10] == 2
    assert row[11] == 100

    database.update_feed_health(url, "Feed", 500, False, 0, 0, None, "503")
    row = _row_for(database, url)
    assert row[4:6] == (3, 1)
    assert row[10] == 2
    assert row[11] == 100

    database.update_feed_health(url, "Feed", 35, True, 2, 1, 8.0)
    row = _row_for(database, url)
    assert row[4:6] == (4, 1)
    assert row[10] == 0
    assert row[11] == 500


def test_llm_call_log_atomically_updates_application_counter(tmp_path, monkeypatch):
    import db.database as database

    db_path = tmp_path / "llm.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    before = database.get_stats()[1]

    database.log_llm_call(
        purpose="summary",
        backend_name="test",
        model_name="model",
        success=False,
        error_type="TimeoutError",
        strategy_version="v1",
    )

    assert database.get_stats()[1] == before + 1
    assert database.get_llm_cost_stats(hours=1)["calls"] == 1


def test_feed_health_samples_timestamp_after_writer_lock(tmp_path, monkeypatch):
    import db.database as database

    db_path = tmp_path / "ordered.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()
    sampled = threading.Event()
    finished = threading.Event()
    real_time = time.time

    def sampled_time():
        sampled.set()
        return real_time()

    monkeypatch.setattr(database.time, "time", sampled_time)
    database.db_lock.acquire()
    try:
        thread = threading.Thread(
            target=lambda: (
                database.update_feed_health(
                    "https://example.com/ordered",
                    "Ordered",
                    10,
                    True,
                    1,
                    0,
                    None,
                ),
                finished.set(),
            )
        )
        thread.start()
        assert sampled.wait(0.05) is False
    finally:
        database.db_lock.release()

    thread.join(timeout=2)
    assert finished.is_set()
    assert sampled.is_set()
