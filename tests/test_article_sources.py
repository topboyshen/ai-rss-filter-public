import sqlite3
import time
from types import SimpleNamespace

import pytest

from db.event_core import EventCoreSchemaError


def _use_temp_database(tmp_path, monkeypatch):
    import db.database as database

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    return database, db_path


def _cache_article(database, link: str, feed_url: str, *, created_at: int = 1_700_000_000):
    database.set_cached_entry(
        link,
        "scored title",
        "raw title",
        8.0,
        "tag",
        "logic",
        "summary",
        feed_url=feed_url,
        score_path="llm",
        strategy_version="test-v1",
        created_at=created_at,
    )


def test_init_rejects_unmigrated_legacy_source_without_partial_write(
    tmp_path, monkeypatch
):
    database, db_path = _use_temp_database(tmp_path, monkeypatch)
    legacy_created_at = 1_700_000_123
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE news_cache ("
            "link TEXT PRIMARY KEY, title TEXT NOT NULL, created_at INTEGER, feed_url TEXT)"
        )
        conn.execute(
            "INSERT INTO news_cache (link, title, created_at, feed_url) VALUES (?, ?, ?, ?)",
            ("https://example.com/legacy", "legacy", legacy_created_at, "feed-a"),
        )

    with pytest.raises(
        EventCoreSchemaError,
        match="backfill marker is missing from a non-pristine database",
    ):
        database.init_db()

    # Startup must fail before additive source migration or any other schema
    # mutation.  The maintenance CLI owns the explicit event/source backfill.
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'article_sources'"
        ).fetchone() is None
        assert [
            row[1] for row in conn.execute("PRAGMA table_info(news_cache)")
        ] == ["link", "title", "created_at", "feed_url"]
        assert conn.execute(
            "SELECT feed_url FROM news_cache WHERE link = ?",
            ("https://example.com/legacy",),
        ).fetchone()[0] == "feed-a"


def test_set_cached_entry_preserves_all_sources_for_same_link(tmp_path, monkeypatch):
    database, _db_path = _use_temp_database(tmp_path, monkeypatch)
    database.init_db()
    link = "https://example.com/shared"
    now = int(time.time())

    _cache_article(database, link, "feed-a", created_at=now - 2)
    _cache_article(database, link, "feed-b", created_at=now - 1)
    _cache_article(database, link, "feed-b", created_at=now)

    sources = database.get_article_sources(link)
    assert {row["feed_url"] for row in sources} == {"feed-a", "feed-b"}
    assert len(sources) == 2
    # The compatibility field stays stable so whichever feed runs last cannot
    # relabel SSE/Briefing output. All later sightings live in article_sources.
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT feed_url, created_at FROM news_cache WHERE link = ?", (link,)
        ).fetchone()
    assert tuple(row) == ("feed-a", now)

    cfg = {
        "threshold": {"pass": 7.0, "notify": 9.0},
        "similarity_window_hours": 72,
        "scoring_strategy_version": "test-v1",
        "feeds": [
            {"url": "feed-a", "aggregation_threshold": 7.0},
            {"url": "feed-b", "aggregation_threshold": 7.0},
        ],
    }
    assert [
        row[0]
        for row in database.get_entries_for_cached_rss(cfg, feed_url="feed-b")
    ] == [link]
    assert [
        row[0] for row in database.get_recent_entries(cfg=cfg, feed_url="feed-b")
    ] == [link]


def test_origin_repairs_dual_write_and_keep_legacy_return_count(tmp_path, monkeypatch):
    database, _db_path = _use_temp_database(tmp_path, monkeypatch)
    database.init_db()
    link = "https://example.com/repair"
    _cache_article(database, link, "feed-a")

    database.update_cached_entry_origin(link, feed_url="feed-b")
    updated = database.update_cached_entry_origins(
        [(link, 1_700_000_300, "feed-c")]
    )

    assert updated == 1
    assert {row["feed_url"] for row in database.get_article_sources(link)} == {
        "feed-a",
        "feed-b",
        "feed-c",
    }
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT feed_url FROM news_cache WHERE link = ?", (link,)
        ).fetchone()[0] == "feed-a"
    assert database.record_article_source("https://example.com/missing", "feed-x") is False
    assert database.get_article_sources("https://example.com/missing") == []


def test_cached_entry_without_publication_time_still_records_source(monkeypatch):
    import core.processor as processor
    import db.database as database

    # process_entry re-reads this link's live cache row; give it a schema.
    database.init_db()

    entry = SimpleNamespace(
        title="cached title",
        link="https://example.com/webhook-cache-hit",
        summary="",
        get=lambda _key, default="": default,
    )
    repaired = []
    monkeypatch.setattr(
        processor.DB,
        "update_cached_entry_origin",
        lambda link, **kwargs: repaired.append((link, kwargs)),
    )
    cfg = {
        "threshold": {"pass": 7.0, "similarity": 0.8, "notify": 9.0},
        "scoring_strategy_version": "test-v1",
    }
    cached = {
        entry.link: (
            "[8.0] cached title",
            "cached title",
            8.0,
            "tag",
            "logic",
            int(time.time()),
            "",
            "test-v1",
            "llm",
        )
    }

    result, reason = processor.process_entry(
        entry,
        [],
        "prompt",
        cfg,
        feed_url="feed-b",
        link_cache=cached,
    )

    assert result is entry
    assert reason == "cache"
    assert repaired == [
        (
                entry.link,
                {
                    "created_at": None,
                    "feed_url": "feed-b",
                    "backtest_selection": {
                        "strategy_version": "test-v1",
                        "aggregation_threshold": 7.0,
                    },
                },
            )
        ]


def test_metadata_repair_records_timestamp_less_cached_items(monkeypatch):
    import core.processor as processor

    link = "https://example.com/no-date"
    entry = SimpleNamespace(
        link=link,
        get=lambda _key, default="": default,
    )
    sightings = []
    monkeypatch.setattr(
        processor.DB,
        "get_cached_entries_by_links",
        lambda _links: {link: ("cached",)},
    )
    monkeypatch.setattr(
        processor.DB,
        "record_article_sources",
        lambda rows: sightings.extend(rows),
    )

    cached = processor._repair_cached_feed_origins([entry], "feed-c", {})

    assert link in cached
    assert sightings == [(link, "feed-c")]
