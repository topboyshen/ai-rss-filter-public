"""Read-only display provenance for current, historical and unknown scores."""

from copy import deepcopy
import hashlib
import sqlite3
import time

import pytest
from flask import Flask

import sse_engine
from services import news_reading


CURRENT = "2026-05-19-v2:current-full-fingerprint"
HISTORICAL = "2026-05-19-v2:old-full-fingerprint"
HISTORICAL_LABEL = "历史评分，未按当前契约重算"
UNKNOWN_LABEL = "评分契约未记录，当前有效性未知"
FEED = "https://feed.example/news"
TITLE = "创新药行业发布临床进展与授权出海交易情况"


def _cfg():
    return {"scoring_strategy_version": CURRENT, "threshold": {"pass": 7},
            "feeds": [{"name": "测试发布方", "url": FEED}]}


def _database(tmp_path, monkeypatch, *, cache_version=CURRENT, event_version=CURRENT,
              event_priority=9.0, event_direction="看多", cache_direction="中性",
              with_event_strategy=True):
    path = tmp_path / "score-contract.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE news_cache (title TEXT, raw_title TEXT, link TEXT, "
                     "score REAL, tag TEXT, logic TEXT, created_at INTEGER, feed_url TEXT, "
                     "direction TEXT, event_id TEXT, event_version INTEGER, "
                     "score_path TEXT, strategy_version TEXT)")
        strategy = ", strategy_version TEXT" if with_event_strategy else ""
        conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, version INTEGER, "
                     f"direction TEXT, priority REAL{strategy})")
        conn.execute("INSERT INTO news_cache VALUES (?, ?, ?, 8.0, '创新药', ?, ?, ?, "
                     "?, 'event-one', 1, 'llm', ?)",
                     (TITLE, TITLE, "https://news.example/one", "缓存评分理由保持原值",
                      int(time.time()), FEED, cache_direction, cache_version))
        values = ["event-one", 1, event_direction, event_priority]
        if with_event_strategy:
            values.append(event_version)
        conn.execute(f"INSERT INTO events VALUES ({','.join('?' for _ in values)})", values)
    import db.database as database

    monkeypatch.setattr(database, "DB_PATH", str(path))
    # Enforce read-only access on the actual connection opened by the loader.
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
@pytest.mark.parametrize("case, expected_state, expected_label", [
    ({}, "current", "评分来源：当前评分契约"),
    ({"event_version": HISTORICAL}, "historical", HISTORICAL_LABEL),
    ({"event_version": HISTORICAL, "event_priority": None}, "historical", HISTORICAL_LABEL),
    ({"cache_version": HISTORICAL, "event_direction": None}, "historical", HISTORICAL_LABEL),
    ({"cache_version": HISTORICAL, "event_direction": "看多"}, "historical", HISTORICAL_LABEL),
    ({"event_version": ""}, "unknown", UNKNOWN_LABEL),
    ({"with_event_strategy": False}, "unknown", UNKNOWN_LABEL),
])
def test_real_sqlite_mixed_score_sources_keep_values_and_disclose_contract(
    tmp_path, monkeypatch, reading, case, expected_state, expected_label,
):
    path = _database(tmp_path, monkeypatch, **case)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    entries = sse_engine._load_from_db(cfg=_cfg(), strict=True)

    assert len(entries) == 1  # The historical prefix-compatible policy is unchanged.
    entry = entries[0]
    assert entry["score"] == (case.get("event_priority", 9.0) or 8.0)
    assert entry["direction"] == (case.get("event_direction", "看多") or "中性")
    assert entry["logic"] == "缓存评分理由保持原值"
    assert entry["strategy_version"] == case.get("cache_version", CURRENT)
    assert entry["_score_contract_state"] == expected_state
    unchanged = deepcopy(entries)
    html = sse_engine._render_cards(entries, reading=reading)

    assert entries == unchanged
    assert expected_label in html
    assert "标题匹配主题：创新药（规则标签，不是本文点名）" in html
    score_area, theme_area = html.split("主题背景／规则池", 1)
    assert expected_label in score_area
    assert HISTORICAL_LABEL not in theme_area and UNKNOWN_LABEL not in theme_area
    if reading:
        assert expected_label not in html.split("<details", 1)[0]
        assert expected_label in html.split("方向与入选依据", 1)[1]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("reading", [False, True])
def test_missing_render_sidecar_is_unknown_not_current_or_title_mapping(reading):
    entry = {"title": TITLE, "raw_title": TITLE, "score": 8.0, "direction": "中性",
             "link": "https://news.example/one", "strategy_version": CURRENT,
             "_title_equity_mentions": {"status": "ok", "raw_title": TITLE,
                                        "mentions": [{"name": "示例药业", "ticker": "688001",
                                                      "exchange": "SSE"}]}}
    html = sse_engine._render_cards([entry], reading=reading)
    assert UNKNOWN_LABEL in html
    assert "评分来源：当前评分契约" not in html
    assert "标题提及：示例药业（688001·SSE）" in html
    assert UNKNOWN_LABEL not in html.split('class="home-news-title-mentions"', 1)[1]


def _source_entry(*, score, direction, version, timestamp):
    entry = {"title": TITLE, "raw_title": TITLE, "score": score,
             "importance_score": score, "direction": direction, "logic": "保持理由",
             "time": timestamp, "link": f"https://news.example/{timestamp}", "feed_url": FEED,
             "_score_sources": {key: {"version": version, "value": value}
                                for key, value in (("score", score), ("direction", direction),
                                                   ("logic", "保持理由"))}}
    return entry


def test_dedupe_score_identity_stays_with_displayed_importance_not_freshest_copy():
    now = int(time.time())
    old = _source_entry(score=8.0, direction="中性", version=HISTORICAL, timestamp=now - 10)
    new = _source_entry(score=9.0, direction="看多", version=CURRENT, timestamp=now)
    candidates = [old, new]
    before = deepcopy(candidates)
    selected, _, _ = sse_engine._select_balanced_cards(candidates, now_ts=now)
    restored = news_reading.restore_reading_titles(selected, candidates)
    prepared = news_reading.attach_score_contract_labels(restored, _cfg())

    assert candidates == before
    assert prepared[0]["importance_score"] == 8.0  # Keep existing independent display value.
    assert prepared[0]["score"] == 9.0 and prepared[0]["direction"] == "看多"
    assert prepared[0]["_score_sources"]["score"]["version"] == HISTORICAL
    assert prepared[0]["_score_sources"]["direction"]["version"] == CURRENT
    assert prepared[0]["_score_contract_state"] == "historical"
    for reading in (False, True):
        assert HISTORICAL_LABEL in sse_engine._render_cards(prepared, reading=reading)


def test_source_binding_and_current_configuration_missing_fail_unknown():
    entry = _source_entry(score=8.0, direction="中性", version=CURRENT, timestamp=1)
    assert news_reading.attach_score_contract_labels([entry], {})[0]["_score_contract_state"] == "unknown"
    changed = {**entry, "importance_score": 9.0}
    assert news_reading.attach_score_contract_labels([changed], _cfg())[0]["_score_contract_state"] == "unknown"


def test_conflicting_semantic_owners_are_unknown_after_dedupe_not_latest_identity():
    first = _source_entry(score=8.0, direction="中性", version=CURRENT, timestamp=1)
    second = _source_entry(score=8.0, direction="中性", version=HISTORICAL, timestamp=2)
    selected = deepcopy(first)
    restored = news_reading.restore_reading_titles([selected], [first, second])
    prepared = news_reading.attach_score_contract_labels(restored, _cfg())
    assert prepared[0]["_score_contract_state"] == "unknown"


@pytest.mark.parametrize("reading", [False, True])
def test_rendering_supplied_contract_metadata_never_opens_database(monkeypatch, reading):
    entry = _source_entry(score=8.0, direction="中性", version=HISTORICAL, timestamp=1)
    prepared = news_reading.attach_score_contract_labels([entry], _cfg())

    def forbidden(*_args, **_kwargs):
        pytest.fail("The renderer must not load a database or current configuration")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(sse_engine, "load_config", forbidden)
    assert HISTORICAL_LABEL in sse_engine._render_cards(prepared, reading=reading)


def test_live_route_uses_actual_event_score_contract_without_writes(tmp_path, monkeypatch):
    path = _database(tmp_path, monkeypatch, event_version=HISTORICAL)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    monkeypatch.setattr(sse_engine, "load_config", _cfg)
    import db.database as database

    monkeypatch.setattr(database, "get_direct_equity_mentions_batch", lambda titles: [
        {"raw_title": title, "status": "ok", "mentions": []} for title in titles
    ])
    app = Flask(__name__)
    app.add_url_rule("/glance-sse", view_func=sse_engine.glance_sse_widget)
    response = app.test_client().get("/glance-sse?min_score=7")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "阅读优先级 9.0/10" in html
    assert HISTORICAL_LABEL in html and "评分来源：当前评分契约" not in html
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("reading", [False, True])
def test_cached_current_flag_alone_cannot_establish_current_score(reading):
    entry = _source_entry(score=8.0, direction="中性", version=CURRENT, timestamp=1)
    entry.pop("_score_sources")
    entry.update({"_score_contract_state": "current", "_score_contract_current_version": CURRENT})
    assert UNKNOWN_LABEL in sse_engine._render_cards([entry], reading=reading)
