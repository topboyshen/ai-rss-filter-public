from contextlib import contextmanager
import sqlite3
import time
import uuid

import pytest


@pytest.fixture
def database():
    import db.database as database_module

    database_module.init_db()
    return database_module


@pytest.fixture
def public_events_client(database, monkeypatch):
    import app as app_module
    import routes.feed_routes as feed_routes
    import routes.security as security

    monkeypatch.setattr(feed_routes, "_cfg", lambda: {})
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    try:
        with app_module.app.test_client() as client:
            yield client, feed_routes
    finally:
        with security._rate_limit_lock:
            security._rate_limit_store.clear()


def _event(database, key, *, priority=8.0, now=None):
    from db.event_core import resolve_or_create_event

    observed_at = int(time.time()) if now is None else int(now)
    with database.get_connection() as conn:
        return resolve_or_create_event(
            conn,
            {"test": str(key)},
            title=f"事件 {key}",
            event_type="policy",
            direction="看多",
            priority=priority,
            occurred_at=observed_at - 10,
            strategy_version="public-events-v1",
            model_version="model-test-v1",
            now=observed_at,
        )


def _evidence(
    database,
    event_id,
    url,
    *,
    source_name="",
    excerpt="",
    snapshot_text="",
    reliability=0.5,
    observed_at=None,
):
    from db.event_core import upsert_event_evidence

    observed = int(time.time()) if observed_at is None else int(observed_at)
    with database.get_connection() as conn:
        return upsert_event_evidence(
            conn,
            event_id,
            source_uri=url,
            source_name=source_name,
            claim_key=f"claim:{uuid.uuid4()}",
            claim_text=f"claim for {url}",
            excerpt=excerpt,
            snapshot_text=snapshot_text,
            stance="supports",
            reliability=reliability,
            observed_at=observed,
            now=observed,
        )


def _cache_source(database, url, *, score_path):
    now = int(time.time())
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, summary, created_at, "
            "feed_url, score_path, strategy_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                url,
                f"cache row for {url}",
                f"cache row for {url}",
                8.0,
                "policy",
                "cached logic",
                "cached summary",
                now,
                "feed-a",
                score_path,
                "public-events-v1",
            ),
        )
        conn.commit()


def test_get_events_batches_evidence_and_preserves_merged_group_contract(
    database, monkeypatch
):
    from db.event_core import merge_events

    now = int(time.time()) - 60
    root_id = _event(database, "root", priority=9.0, now=now)
    source_id = _event(database, "source", priority=7.0, now=now + 1)
    _evidence(
        database,
        root_id,
        "https://wire.example/root",
        source_name="Wire A",
        excerpt="lower reliability summary",
        reliability=0.8,
        observed_at=now + 5,
    )
    _evidence(
        database,
        source_id,
        "https://wire.example/canonical",
        source_name="Wire A",
        snapshot_text="canonical frozen summary",
        reliability=0.99,
        observed_at=now + 6,
    )
    _evidence(
        database,
        source_id,
        "https://second.example/story",
        source_name="",
        excerpt="second source",
        reliability=0.6,
        observed_at=now + 7,
    )
    with database.get_connection() as conn:
        merge_events(
            conn,
            source_id,
            root_id,
            reason="same event",
            actor="test",
            now=now + 10,
        )

    statements = []
    original_get_connection = database.get_connection

    @contextmanager
    def traced_connection():
        with original_get_connection() as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(database, "get_connection", traced_connection)
    monkeypatch.setattr(
        database,
        "canonical_event_group",
        lambda *_args, **_kwargs: pytest.fail("get_events regressed to per-event lookup"),
    )

    events = database.get_events(
        cfg={"scoring_strategy_version": "public-events-v1"},
        hours=24,
        limit=999,
    )

    assert events == [
        (
            root_id,
            "事件 root",
            "https://wire.example/canonical",
            "canonical frozen summary",
            9.0,
            "policy",
            2,
            now,
            now + 1,
            "看多",
        )
    ]
    read_statements = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(read_statements) == 1
    assert read_statements[0].lstrip().upper().startswith("WITH RECURSIVE")
    assert "LIMIT 50" in read_statements[0].upper()


def test_get_events_merged_group_uses_only_public_evidence_for_canonical_and_count(
    database,
):
    from db.event_core import merge_events

    now = int(time.time()) - 60
    root_id = _event(database, "mixed-root", priority=9.0, now=now)
    child_id = _event(database, "mixed-child", priority=7.0, now=now + 1)
    quarantine_url = "https://wire.example/quarantined"
    clean_url = "https://wire.example/clean"
    external_url = "https://archive.example/historical"
    _cache_source(database, quarantine_url, score_path="quality_quarantine")
    _cache_source(database, clean_url, score_path="llm")
    _evidence(
        database,
        root_id,
        quarantine_url,
        source_name="Quarantine Wire",
        excerpt="quarantined canonical candidate",
        reliability=0.99,
        observed_at=now + 5,
    )
    _evidence(
        database,
        child_id,
        clean_url,
        source_name="Clean Wire",
        excerpt="clean merged summary",
        reliability=0.9,
        observed_at=now + 6,
    )
    _evidence(
        database,
        child_id,
        external_url,
        source_name="Historical Archive",
        excerpt="external historical evidence",
        reliability=0.8,
        observed_at=now + 7,
    )
    with database.get_connection() as conn:
        merge_events(
            conn,
            child_id,
            root_id,
            reason="same event",
            actor="test",
            now=now + 10,
        )

    events = database.get_events(
        cfg={"scoring_strategy_version": "public-events-v1"},
        hours=24,
        limit=10,
    )

    assert events == [
        (
            root_id,
            "事件 mixed-root",
            clean_url,
            "clean merged summary",
            9.0,
            "policy",
            2,
            now,
            now + 1,
            "看多",
        )
    ]


def test_get_events_excludes_events_without_public_evidence(database):
    now = int(time.time()) - 60
    quarantine_id = _event(database, "quarantine-only", now=now)
    _event(database, "no-evidence", now=now + 1)
    quarantine_url = "https://wire.example/quarantine-only"
    _cache_source(database, quarantine_url, score_path="quality_quarantine")
    _evidence(
        database,
        quarantine_id,
        quarantine_url,
        source_name="Quarantine Wire",
        excerpt="must remain private",
        reliability=0.99,
        observed_at=now + 5,
    )

    assert database.get_events(
        cfg={"scoring_strategy_version": "public-events-v1"},
        hours=24,
        limit=10,
    ) == []


def test_get_events_supports_legacy_news_cache_without_score_path(monkeypatch):
    import db.database as database_module

    now = int(time.time()) - 60
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE news_cache (link TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE events ("
            "event_id TEXT PRIMARY KEY, title TEXT, priority REAL, event_type TEXT, "
            "direction TEXT, first_seen_at INTEGER, last_seen_at INTEGER, "
            "merge_state TEXT, merged_into TEXT)"
        )
        conn.execute(
            "CREATE TABLE event_evidence ("
            "evidence_id TEXT PRIMARY KEY, event_id TEXT, source_uri TEXT, "
            "source_name TEXT, excerpt TEXT, snapshot_text TEXT, reliability REAL, "
            "observed_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO news_cache (link) VALUES (?)",
            ("https://legacy.example/source",),
        )
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-event",
                "Legacy event",
                8.0,
                "policy",
                "看多",
                now,
                now,
                "active",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO event_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-evidence",
                "legacy-event",
                "https://legacy.example/source",
                "Legacy Wire",
                "legacy public summary",
                "",
                0.8,
                now + 1,
            ),
        )
        conn.commit()
        monkeypatch.setattr(database_module, "get_connection", lambda: conn)

        events = database_module.get_events(cfg={}, hours=24, limit=10)
    finally:
        conn.close()

    assert events == [
        (
            "legacy-event",
            "Legacy event",
            "https://legacy.example/source",
            "legacy public summary",
            8.0,
            "policy",
            1,
            now,
            now,
            "看多",
        )
    ]


def test_public_events_limit_is_clamped_to_50(public_events_client, monkeypatch):
    client, feed_routes = public_events_client
    calls = []

    def fake_get_events(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(feed_routes.DB, "get_events", staticmethod(fake_get_events))

    response = client.get("/events/json?limit=999999")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["events"] == []
    assert set(payload) == {"events"}
    assert calls == [{"cfg": {}, "hours": 24, "limit": 50}]


@pytest.mark.parametrize("format_type", ["json", "rss", "html"])
def test_public_event_formats_do_not_expose_internal_replay_state(
    public_events_client, monkeypatch, format_type
):
    client, feed_routes = public_events_client
    monkeypatch.setattr(feed_routes.DB, "get_events", lambda **_kwargs: [])

    response = client.get(f"/events/{format_type}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "X-Event-Radar-State" not in response.headers
    assert "映射队列" not in body
    assert "相似图队列" not in body
    assert "契约结清" not in body
    if format_type == "json":
        assert set(response.get_json()) == {"events"}


@pytest.mark.parametrize("format_type", ["json", "rss", "html"])
def test_event_feed_cache_failure_is_retryable_not_a_valid_empty_feed(
    public_events_client, monkeypatch, format_type
):
    client, feed_routes = public_events_client

    def fail_event_read(**_kwargs):
        raise sqlite3.OperationalError("database is unavailable")

    monkeypatch.setattr(feed_routes.DB, "get_events", fail_event_read)
    response = client.get(f"/events/{format_type}")
    body = response.get_data(as_text=True)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Event-Radar-State"] == "unknown"
    if format_type == "json":
        assert response.get_json() == {
            "error": "event_feed_temporarily_unavailable",
            "events": None,
        }
    else:
        assert "暂不可用" in body
        assert "事件缓存暂不可用，请稍后重试" in body


@pytest.mark.parametrize("path", ["/events/json", "/events/rss", "/events/html"])
def test_public_event_formats_exclude_quarantine_only_events_but_allow_external_evidence(
    public_events_client, database, path
):
    client, _feed_routes = public_events_client
    now = int(time.time()) - 60
    quarantine_id = _event(database, "private-quarantine", priority=9.5, now=now)
    clean_id = _event(database, "public-external", priority=8.5, now=now + 1)
    quarantine_url = "https://wire.example/private-quarantine"
    external_url = "https://archive.example/public-external"
    _cache_source(database, quarantine_url, score_path="quality_quarantine")
    _evidence(
        database,
        quarantine_id,
        quarantine_url,
        source_name="Quarantine Wire",
        excerpt="private quarantine evidence",
        reliability=0.99,
        observed_at=now + 5,
    )
    _evidence(
        database,
        clean_id,
        external_url,
        source_name="Historical Archive",
        excerpt="public external evidence",
        reliability=0.8,
        observed_at=now + 6,
    )

    response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "事件 public-external" in body
    assert "public external evidence" in body
    assert "事件 private-quarantine" not in body
    assert "private quarantine evidence" not in body
    if path.endswith("/json"):
        assert [event["event_id"] for event in response.get_json()["events"]] == [
            clean_id
        ]


@pytest.mark.parametrize("path", ["/events/json", "/events/rss", "/events/html"])
def test_public_events_exclude_historical_replacement_character_across_formats(
    public_events_client, monkeypatch, path
):
    client, feed_routes = public_events_client
    now = int(time.time())
    clean = (
        "clean-event-id",
        "Clean event",
        "https://example.test/clean",
        "Clean summary",
        8.5,
        "verified-type",
        1,
        now - 10,
        now,
        "看多",
    )
    historical_poc = (
        "replacement-event-id",
        "Replacement character PoC",
        "https://example.test/poc",
        "Immutable historical summary",
        9.5,
        "damaged\ufffdhistorical-type",
        1,
        now - 10,
        now,
        "看多",
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_events",
        staticmethod(lambda **_kwargs: [historical_poc, clean]),
    )

    response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Clean event" in body
    assert "\ufffd" not in body
    assert "Replacement character PoC" not in body
    assert "damaged" not in body
    assert "其他事件" not in body
    if path.endswith("/json"):
        assert [event["event_id"] for event in response.get_json()["events"]] == [
            "clean-event-id"
        ]


@pytest.mark.parametrize("path", ["/events/json", "/events/rss", "/events/html"])
def test_public_events_degrade_malformed_legacy_taxonomy_without_hiding_event(
    public_events_client, monkeypatch, path
):
    client, feed_routes = public_events_client
    now = int(time.time())
    malformed = (
        "malformed-taxonomy-event",
        "Malformed historical taxonomy remains visible",
        "https://example.test/malformed-taxonomy",
        "Audited historical event",
        8.5,
        "囸级政策、国家级政策",
        1,
        now - 10,
        now,
        "中性",
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_events",
        staticmethod(lambda **_kwargs: [malformed]),
    )

    response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Malformed historical taxonomy remains visible" in body
    assert "囸级政策、国家级政策" not in body
    assert "其他事件" in body
    if path.endswith("/json"):
        assert response.get_json()["events"][0]["tag"] == "其他事件"


def test_public_events_route_is_rate_limited(public_events_client, monkeypatch):
    client, feed_routes = public_events_client
    calls = []

    def fake_get_events(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(feed_routes.DB, "get_events", staticmethod(fake_get_events))

    for _ in range(60):
        assert client.get("/events/json").status_code == 200
    blocked = client.get("/events/json")

    assert blocked.status_code == 429
    assert blocked.get_json() == {"error": "Rate limit exceeded"}
    assert len(calls) == 60
