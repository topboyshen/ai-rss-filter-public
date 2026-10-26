"""A merged group's maximum priority has no independent score-contract owner."""

import hashlib
import sqlite3
import time

import pytest

import sse_engine
from db.event_core import init_event_core_schema, merge_events, resolve_or_create_event


CURRENT = "2026-05-19-v2:current-full-fingerprint"
HISTORICAL = "2026-05-19-v2:old-full-fingerprint"
TITLE = "创新药行业发布临床进展与授权出海交易情况"
FEED = "https://feed.example/news"


def _fixture_database(tmp_path, monkeypatch, *, merged, rescore=False, source_version=HISTORICAL):
    path = tmp_path / "merged-score.sqlite"
    now = int(time.time()) - 10
    with sqlite3.connect(path) as conn:
        init_event_core_schema(conn)
        target = resolve_or_create_event(
            conn, {"wire": "current-target"}, title=TITLE, direction="中性",
            priority=7.0, strategy_version=CURRENT, now=now,
        )
        if merged:
            source = resolve_or_create_event(
                conn, {"wire": "historical-source"}, title=TITLE, direction="看多",
                priority=9.0, strategy_version=source_version, now=now,
            )
            merge_events(conn, source, target, reason="same event", now=now + 1)
        if rescore:
            resolve_or_create_event(
                conn, {"wire": "current-target"}, title=TITLE, direction="中性",
                priority=7.0, strategy_version=CURRENT, now=now + 2,
            )
        conn.execute(
            "CREATE TABLE news_cache (title TEXT, raw_title TEXT, link TEXT, score REAL, "
            "tag TEXT, logic TEXT, created_at INTEGER, feed_url TEXT, direction TEXT, "
            "event_id TEXT, event_version INTEGER, score_path TEXT, strategy_version TEXT)"
        )
        conn.execute(
            "INSERT INTO news_cache VALUES (?, ?, ?, 7.0, '创新药', ?, ?, ?, "
            "'中性', ?, 1, 'llm', ?)",
            (TITLE, TITLE, "https://news.example/one", "当前缓存理由", now, FEED, target, CURRENT),
        )
        root = conn.execute(
            "SELECT priority, direction, strategy_version FROM events WHERE event_id = ?",
            (target,),
        ).fetchone()
        assert root == (9.0 if merged else 7.0, "中性", CURRENT)

    import db.database as database

    monkeypatch.setattr(database, "DB_PATH", str(path))
    connect = sqlite3.connect
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}

    def read_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_authorizer(lambda operation, *_: sqlite3.SQLITE_DENY
                                  if operation in writes else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(sqlite3, "connect", read_connection)
    return path


@pytest.mark.parametrize("reading", [False, True])
@pytest.mark.parametrize("rescore, source_version", [
    (False, HISTORICAL), (True, HISTORICAL), (False, CURRENT), (False, ""),
])
def test_merged_priority_does_not_borrow_root_contract(
    tmp_path, monkeypatch, reading, rescore, source_version,
):
    path = _fixture_database(tmp_path, monkeypatch, merged=True, rescore=rescore,
                             source_version=source_version)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg = {"scoring_strategy_version": CURRENT, "threshold": {"pass": 7},
           "feeds": [{"name": "测试发布方", "url": FEED}]}
    # The old descendant's maximum still passes the existing 8-point filter.
    entries = sse_engine._load_from_db(cfg=cfg, min_score=8.0, strict=True)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["score"] == entry["importance_score"] == 9.0
    assert entry["direction"] == "中性" and entry["logic"] == "当前缓存理由"
    assert entry["_score_sources"]["score"] == {"value": 9.0, "version": ""}
    assert entry["_score_sources"]["direction"]["version"] == CURRENT
    assert entry["_score_sources"]["logic"]["version"] == CURRENT
    assert entry["_score_contract_state"] == "unknown"
    html = sse_engine._render_cards(entries, reading=reading)
    assert "评分契约未记录，当前有效性未知" in html
    assert "评分来源：当前评分契约" not in html
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("reading", [False, True])
def test_unmerged_current_priority_remains_current(tmp_path, monkeypatch, reading):
    path = _fixture_database(tmp_path, monkeypatch, merged=False)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg = {"scoring_strategy_version": CURRENT, "threshold": {"pass": 7},
           "feeds": [{"name": "测试发布方", "url": FEED}]}
    entries = sse_engine._load_from_db(cfg=cfg, min_score=7.0, strict=True)
    assert len(entries) == 1 and entries[0]["score"] == 7.0
    assert entries[0]["_score_contract_state"] == "current"
    assert "评分来源：当前评分契约" in sse_engine._render_cards(entries, reading=reading)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
