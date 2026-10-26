"""Frozen SQLite regressions for source/time coverage before card quotas."""

import socket
import sqlite3
import uuid

from flask import Flask
import pytest

import db.database as database
import sse_engine


NOW = 2_000_000_000
A, B = "https://feed.example/a", "https://feed.example/b"
CFG = {"scoring_strategy_version": "audit-v1", "threshold": {"pass": 7.0},
       "feeds": [{"name": "来源甲", "url": A}, {"name": "来源乙", "url": B}]}


@pytest.fixture
def news_db(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External network is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    monkeypatch.setattr(sse_engine.time, "time", lambda: NOW)
    monkeypatch.setattr(sse_engine, "load_config", lambda: CFG)
    monkeypatch.setattr(database, "get_direct_equity_mentions_batch", lambda titles: [
        {"raw_title": title, "status": "ok", "reason": "no_match", "mentions": []}
        for title in titles
    ])
    path = tmp_path / "candidate.sqlite"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE news_cache (title TEXT, raw_title TEXT, link TEXT, score REAL, "
                     "tag TEXT, logic TEXT, created_at INTEGER, feed_url TEXT, direction TEXT, "
                     "event_id TEXT, event_version INTEGER, score_path TEXT, strategy_version TEXT)")
        conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, version INTEGER, "
                     "priority REAL, direction TEXT, merged_into TEXT, strategy_version TEXT, merge_state TEXT)")
    return path


def _insert(path, key, *, age=60, score=8.6, feed=A, title=None, parent=None, version="audit-v1"):
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(key)))
    title = title or f"第{key}号产业进展公告"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO events VALUES (?, 1, ?, '中性', ?, ?, ?)",
                     (event_id, score, parent, version, "merged" if parent else "active"))
        conn.execute("INSERT INTO news_cache VALUES (?, ?, ?, ?, '宏观政策', '', ?, ?, "
                     "'中性', ?, 1, 'llm', ?)",
                     (title, title, f"https://news.example/{key}", score, NOW - age, feed, event_id, version))
    return event_id


@pytest.mark.parametrize("old_age", [60, 20 * 3600])
def test_one_source_cannot_hide_another_sources_fresh_card(news_db, old_age):
    for index in range(240):
        _insert(news_db, f"old-{index}", age=old_age + index, score=9.0)
    _insert(news_db, "fresh", feed=B, title="最新央行操作安排已公布")

    candidates = sse_engine._load_from_db(limit=30, cfg=CFG, strict=True)
    selected, fresh, fallback = sse_engine._select_balanced_cards(candidates, now_ts=NOW)
    assert len(candidates) == 31
    assert len(selected) == 3
    assert any(entry["title"] == "最新央行操作安排已公布" for entry in selected)
    assert (fresh, fallback) == ((3, 0) if old_age == 60 else (1, 2))
    app = Flask(__name__)
    app.add_url_rule("/glance-sse", view_func=sse_engine.glance_sse_widget)
    html = app.test_client().get("/glance-sse").get_data(as_text=True)
    assert "最新央行操作安排已公布" in html
    assert "近12h暂无过阈" not in html


def test_canonical_copies_do_not_consume_candidate_slots_or_issue_per_row_queries(news_db, monkeypatch):
    root = _insert(news_db, "root", score=9.0)
    for index in range(240):
        _insert(news_db, f"copy-{index}", score=9.0, parent=root)
    _insert(news_db, "independent", score=8.6)
    queries = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    candidates = sse_engine._load_from_db(limit=2, cfg=CFG, strict=True)
    assert len(candidates) == 2
    assert len({entry["event_id"] for entry in candidates}) == 2
    assert any("independent" in entry["link"] for entry in candidates)
    # Source discovery, two schema reads, and one bounded result query.
    assert len(queries) == 4


def test_filtering_precedes_candidate_rank_and_observations_keep_timeline_order(news_db):
    for index in range(60):
        _insert(news_db, f"noise-{index}", title=f"早报第{index}号", score=9.9)
        _insert(news_db, f"stale-{index}", score=9.8, version="old-contract")
        _insert(news_db, f"observe-{index}", age=3600 + index, score=8.4)
    _insert(news_db, "new-observation", age=10, score=7.5)
    candidates = sse_engine._load_from_db(limit=2, cfg=CFG, strict=True)
    assert len(candidates) == 2
    assert any("new-observation" in entry["link"] for entry in candidates)
    assert all(entry["score"] < 8.5 for entry in candidates)
    assert all(entry["strategy_version"] == "audit-v1" for entry in candidates)


def test_source_threshold_time_cutoff_and_empty_result_remain_enforced(news_db):
    _insert(news_db, "low", score=6.9)
    _insert(news_db, "expired", age=49 * 3600, score=9.9)
    _insert(news_db, "unknown-source", feed="https://unconfigured.example/rss", score=9.9)
    assert sse_engine._load_from_db(limit=30, cfg=CFG, strict=True) == []
    _insert(news_db, "below-override", score=8.9)
    assert sse_engine._load_from_db(limit=30, cfg=CFG, min_score=9.0, strict=True) == []
    assert sse_engine._load_from_db(limit=0, cfg=CFG, strict=True) == []


def test_fresh_copy_of_canonical_event_keeps_fresh_delivery_metadata(news_db):
    root = _insert(news_db, "fresh-root", feed=B, age=60, score=8.6)
    _insert(news_db, "old-merged-copy", age=20 * 3600, score=9.0, parent=root)
    candidates = sse_engine._load_from_db(limit=30, cfg=CFG, strict=True)
    selected, fresh, fallback = sse_engine._select_balanced_cards(candidates, now_ts=NOW)
    assert len(selected) == 1
    assert (fresh, fallback) == (1, 0)
    assert selected[0]["link"] == "https://news.example/fresh-root"
    assert selected[0]["score"] == 9.0
    assert selected[0]["raw_title"] == "第old-merged-copy号产业进展公告"


def test_shared_canonical_events_cannot_starve_a_sources_independent_card(news_db):
    for index in range(30):
        root = _insert(news_db, f"root-{index}", age=60, score=9.0)
        _insert(news_db, f"source-b-copy-{index}", feed=B, age=120, score=9.0, parent=root)
    _insert(news_db, "source-b-independent", feed=B, age=1, score=8.6)

    candidates = sse_engine._load_from_db(limit=30, cfg=CFG, strict=True)
    assert len(candidates) == 31  # Each canonical event consumes only its owner's quota.
    selected, fresh, fallback = sse_engine._select_balanced_cards(candidates, now_ts=NOW)
    assert (len(selected), fresh, fallback) == (3, 3, 0)
    assert any(entry["link"] == "https://news.example/source-b-independent" for entry in selected)


@pytest.mark.parametrize("score", [None, "bad-score", "NaN", float("inf"), -1.0, 10.1])
def test_invalid_legacy_score_does_not_abort_other_candidates(news_db, score):
    _insert(news_db, "valid")
    with sqlite3.connect(news_db) as conn:
        conn.execute("UPDATE news_cache SET raw_title = NULL")
        conn.execute("INSERT INTO news_cache VALUES (?, NULL, ?, ?, '宏观政策', NULL, ?, ?, "
                     "'中性', NULL, 0, 'llm', 'audit-v1')",
                     ("坏分数旧数据", "https://news.example/invalid", score, NOW, A))
        conn.execute("INSERT INTO news_cache VALUES (?, NULL, ?, 'bad-score', '宏观政策', NULL, ?, ?, "
                     "'中性', NULL, 0, 'llm', 'audit-v1')",
                     ("未知来源", "https://news.example/unknown", NOW, "https://unknown.example/rss"))
    candidates = sse_engine._load_from_db(limit=30, cfg=CFG, strict=True)
    assert [entry["link"] for entry in candidates] == ["https://news.example/valid"]
    assert candidates[0]["raw_title"] == ""
