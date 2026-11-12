"""P2-7: realtime SQLite loads are read-only and failures stay observable."""

import hashlib
import logging
import sqlite3
import time

import pytest

import sse_engine
from db.database import SQLITE_BUSY_TIMEOUT_SECONDS

FEED = "http://rss/test"


def _cfg():
    return {
        "threshold": {"pass": 7},
        "feeds": [{"name": "测试源", "url": FEED, "aggregation_threshold": 7}],
    }


def _create_cache_db(path, *, wal=False):
    with sqlite3.connect(path) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE news_cache ("
            "title TEXT, link TEXT, score REAL, tag TEXT, logic TEXT, "
            "created_at INTEGER, feed_url TEXT)"
        )
        conn.execute(
            "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("只读条目", "https://example.com/a", 8.5, "宏观", "影响流动性",
             int(time.time()), FEED),
        )
    path.chmod(0o444)
    return path


def test_missing_database_is_not_created_and_warns(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "missing.db"
    monkeypatch.setattr("db.database.DB_PATH", str(missing))
    with caplog.at_level(logging.WARNING, logger=sse_engine.__name__):
        assert sse_engine._load_from_db(cfg=_cfg()) == []
    assert not missing.exists()
    assert "Realtime DB load failed: OperationalError" in caplog.text


def test_missing_database_strict_raises_without_creating_file(tmp_path, monkeypatch):
    missing = tmp_path / "missing.db"
    monkeypatch.setattr("db.database.DB_PATH", str(missing))
    with pytest.raises(sqlite3.OperationalError):
        sse_engine._load_from_db(cfg=_cfg(), strict=True)
    assert not missing.exists()


def test_read_only_database_file_is_readable_and_unchanged(tmp_path, monkeypatch):
    path = _create_cache_db(tmp_path / "news.db")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr("db.database.DB_PATH", str(path))
    entries = sse_engine._load_from_db(cfg=_cfg(), strict=True)
    assert [entry["title"] for entry in entries] == ["只读条目"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_read_only_wal_database_is_readable(tmp_path, monkeypatch):
    path = _create_cache_db(tmp_path / "wal.db", wal=True)
    monkeypatch.setattr("db.database.DB_PATH", str(path))
    entries = sse_engine._load_from_db(cfg=_cfg(), strict=True)
    assert [entry["title"] for entry in entries] == ["只读条目"]


def test_loader_opens_sqlite_with_read_only_uri_and_busy_timeout(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    path.touch()
    monkeypatch.setattr("db.database.DB_PATH", str(path))
    real_connect = sqlite3.connect
    calls = []

    def observed_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    assert sse_engine._load_from_db(cfg=_cfg()) == []
    assert len(calls) == 1
    assert calls[0][0][0] == path.resolve().as_uri() + "?mode=ro"
    assert calls[0][1] == {"uri": True, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
