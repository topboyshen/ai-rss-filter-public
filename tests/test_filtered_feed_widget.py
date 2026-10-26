import calendar
import time
from types import SimpleNamespace

import pytest


def test_rss_pipeline_treats_parsed_dates_as_utc():
    from core.processor import _entry_timestamp, build_rss_xml
    from routes.feed_routes import _entry_time_for_sort, _published_parsed_from_ts
    from utils.helpers import FeedEntry

    timestamp = calendar.timegm((2026, 7, 13, 12, 3, 53, 0, 0, 0))
    parsed = time.gmtime(timestamp)
    entry = FeedEntry("标题", "https://example.com/a", "摘要", parsed)

    assert _entry_timestamp(entry) == timestamp
    assert _entry_time_for_sort(entry) == timestamp
    assert _published_parsed_from_ts(timestamp) == parsed
    xml = build_rss_xml(
        [entry],
        "测试",
        "https://example.com/feed",
        "https://example.com/cached",
        {},
    )
    assert "<pubDate>Mon, 13 Jul 2026 12:03:53 GMT</pubDate>" in xml

    empty_summary = FeedEntry(
        "无摘要标题", "https://example.com/b", "", parsed
    )
    empty_xml = build_rss_xml(
        [empty_summary],
        "测试",
        "https://example.com/feed",
        "https://example.com/cached",
        {},
    )
    assert "<description></description>" in empty_xml


def test_jin10_timestamp_parser_normalizes_every_input_to_utc():
    from services.jin10_feed import _parse_timestamp

    timestamp = calendar.timegm((2026, 7, 13, 12, 3, 53, 0, 0, 0))
    expected = time.gmtime(timestamp)

    assert _parse_timestamp(timestamp) == expected
    assert _parse_timestamp(timestamp * 1000) == expected
    assert _parse_timestamp("2026-07-13T12:03:53Z") == expected
    assert _parse_timestamp("Mon, 13 Jul 2026 12:03:53 GMT") == expected


def test_cached_feed_uses_compact_title_bounded_limit_and_no_local_cache(monkeypatch):
    import app as app_module
    import routes.feed_routes as feeds

    timestamp = calendar.timegm((2026, 7, 13, 12, 3, 53, 0, 0, 0))
    row = (
        "https://example.com/article",
        "[⭐8.5 📈 国家政策] 原始标题 💡很长的模型点评",
        "原始标题" * 80,
        8.5,
        "国家政策",
        "政策逻辑说明",
        "",
        timestamp,
    )
    query = {}
    cfg = {"cache_ttl": 300, "scoring_strategy_version": "test-v1"}

    monkeypatch.setattr(feeds, "_cfg", lambda: cfg)
    monkeypatch.setattr(feeds, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(
        feeds.DB,
        "get_feed_config",
        lambda *_args: {"name": "测试情报源"},
    )
    monkeypatch.setattr(
        feeds,
        "get_cached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached_feed must bypass process-local XML cache")
        ),
    )
    monkeypatch.setattr(
        feeds,
        "set_cached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cached_feed must bypass process-local XML cache")
        ),
    )

    def load(_cfg, *, hours, limit, feed_url):
        query.update(hours=hours, limit=limit, feed_url=feed_url)
        return [row]

    monkeypatch.setattr(feeds.DB, "get_entries_for_cached_rss", load)

    with app_module.app.test_client() as client:
        response = client.get(
            "/cached_feed?url=https://feed.example/rss&hours=9999&limit=9999"
        )

    assert response.status_code == 200
    assert query == {
        "hours": 24 * 7,
        "limit": 50,
        "feed_url": "https://feed.example/rss",
    }
    body = response.get_data(as_text=True)
    assert "📈 8.5｜原始标题" in body
    assert "…" in body
    assert "很长的模型点评" not in body
    assert "政策逻辑说明" in body
    assert "Mon, 13 Jul 2026 12:03:53 GMT" in body
    assert "AI 智能过滤: 测试情报源" in body
    assert "AI 智能过滤: https://feed.example/rss" not in body
    assert "<link>https://example.com/</link>" in body
    assert "http://rsshub:1200" not in body
    assert "<category>" not in body


@pytest.mark.parametrize("mode", ["scored", "preview", "fallback_preview"])
def test_cached_widget_and_observations_drop_polluted_rows_without_backfill(
    monkeypatch, caplog, mode
):
    import app as app_module
    import routes.feed_routes as feeds

    now = int(time.time())
    polluted = (
        "https://example.test/polluted",
        "PoC clean-looking widget title",
        "PoC clean-looking widget title",
        9.5,
        "damaged\ufffdtag",
        "apparently clean logic",
        "apparently clean summary",
        now,
    )
    clean = (
        "https://example.test/clean",
        "Clean retained widget title",
        "Clean retained widget title",
        8.5,
        "macro",
        "clean logic",
        "clean summary",
        now - 1,
    )
    scored_calls = []
    preview_calls = []
    cfg = {"cache_ttl": 300, "scoring_strategy_version": "test-v1"}
    monkeypatch.setattr(feeds, "_cfg", lambda: cfg)
    monkeypatch.setattr(feeds, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(
        feeds.DB,
        "get_feed_config",
        lambda *_args: {"name": "测试情报源"},
    )

    def load_scored(_cfg, **kwargs):
        scored_calls.append(kwargs)
        return [] if mode == "fallback_preview" else [polluted, clean]

    def load_preview(_cfg, **kwargs):
        preview_calls.append(kwargs)
        if mode == "scored":
            pytest.fail("scored widget must not query observations")
        return [polluted, clean]

    monkeypatch.setattr(feeds.DB, "get_entries_for_cached_rss", load_scored)
    monkeypatch.setattr(feeds.DB, "get_recent_entries_for_feed_preview", load_preview)
    caplog.set_level("WARNING", logger=feeds.__name__)
    path = "/cached_feed?url=https://feed.example/rss&limit=2"
    if mode == "preview":
        path += "&preview=true"

    with app_module.app.test_client() as client:
        response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Clean retained widget title" in body
    assert "PoC clean-looking widget title" not in body
    assert "\ufffd" not in body
    if mode == "scored":
        assert len(scored_calls) == 1
        assert scored_calls[0]["limit"] == 2
        assert preview_calls == []
        expected_context = "/cached_feed"
    elif mode == "preview":
        assert scored_calls == []
        assert len(preview_calls) == 1
        assert preview_calls[0]["limit"] == 2
        expected_context = "/cached_feed preview"
        assert "[观察] Clean retained widget title" in body
    else:
        assert len(scored_calls) == 1
        assert scored_calls[0]["limit"] == 2
        assert len(preview_calls) == 1
        assert preview_calls[0]["limit"] == 2
        expected_context = "/cached_feed fallback preview"
        assert "[观察] Clean retained widget title" in body
    assert any(
        f"context={expected_context} count=1" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("mode", ["scored", "preview"])
def test_cached_widget_does_not_top_up_when_every_row_is_excluded(
    monkeypatch, mode
):
    import app as app_module
    import routes.feed_routes as feeds

    polluted = (
        "https://example.test/polluted-only",
        "PoC excluded-only widget title",
        "PoC excluded-only widget title",
        9.5,
        "damaged\ufffdtag",
        "apparently clean logic",
        "apparently clean summary",
        int(time.time()),
    )
    scored_calls = []
    preview_calls = []
    monkeypatch.setattr(
        feeds,
        "_cfg",
        lambda: {"cache_ttl": 300, "scoring_strategy_version": "test-v1"},
    )
    monkeypatch.setattr(feeds, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(feeds.DB, "get_feed_config", lambda *_args: {"name": "测试源"})

    def load_scored(_cfg, **kwargs):
        scored_calls.append(kwargs)
        if mode == "preview":
            pytest.fail("excluded preview rows must not fall through to scored refill")
        return [polluted]

    def load_preview(_cfg, **kwargs):
        preview_calls.append(kwargs)
        if mode == "scored":
            pytest.fail("excluded scored rows must not fall through to preview refill")
        return [polluted]

    monkeypatch.setattr(feeds.DB, "get_entries_for_cached_rss", load_scored)
    monkeypatch.setattr(feeds.DB, "get_recent_entries_for_feed_preview", load_preview)
    path = "/cached_feed?url=https://feed.example/rss&limit=1"
    if mode == "preview":
        path += "&preview=true"

    with app_module.app.test_client() as client:
        response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "<item>" not in body
    assert "PoC excluded-only widget title" not in body
    if mode == "scored":
        assert len(scored_calls) == 1
        assert preview_calls == []
    else:
        assert scored_calls == []
        assert len(preview_calls) == 1


def test_cached_widget_and_preview_exclude_clean_text_quality_quarantine(
    monkeypatch,
):
    import app as app_module
    import db.database as database
    import routes.feed_routes as feeds

    database.init_db()
    feed_url = "https://feed.example/rss"
    now = int(time.time())
    cfg = {
        "cache_ttl": 300,
        "scoring_strategy_version": "test-v1",
        "threshold": {"pass": 7.0},
        "feeds": [
            {
                "url": feed_url,
                "name": "测试情报源",
                "aggregation_threshold": 7.0,
            }
        ],
    }
    database.set_cached_entry(
        "https://example.test/clean",
        "Clean retained database row",
        "Clean retained database row",
        8.5,
        "宏观",
        "clean logic",
        "clean summary",
        feed_url=feed_url,
        score_path="llm",
        strategy_version="test-v1",
        created_at=now - 1,
    )
    database.set_cached_entry(
        "https://example.test/quarantine",
        "PoC clean-text quarantined widget row",
        "PoC clean-text quarantined widget row",
        9.5,
        "宏观",
        "apparently clean logic",
        "apparently clean summary",
        feed_url=feed_url,
        score_path="quality_quarantine",
        error_type="invalid_model_output",
        strategy_version="test-v1",
        created_at=now,
    )
    monkeypatch.setattr(feeds, "_cfg", lambda: cfg)
    monkeypatch.setattr(feeds, "validate_target_url", lambda *_args: (True, ""))

    with app_module.app.test_client() as client:
        scored = client.get(f"/cached_feed?url={feed_url}&limit=2")
        preview = client.get(f"/cached_feed?url={feed_url}&limit=2&preview=true")

    for response in (scored, preview):
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Clean retained database row" in body
        assert "PoC clean-text quarantined widget row" not in body


def test_public_cached_json_feed_clamps_query_and_is_rate_limited(monkeypatch):
    import app as app_module
    import routes.feed_routes as feeds
    import routes.security as security

    calls = []
    monkeypatch.setattr(feeds, "_cfg", lambda: {})
    monkeypatch.setattr(
        feeds.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [],
    )
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    try:
        with app_module.app.test_client() as client:
            first = client.get("/feed/json?hours=999999&limit=999999")
            assert first.status_code == 200
            for _ in range(59):
                assert client.get("/feed/json").status_code == 200
            blocked = client.get("/feed/json")
    finally:
        with security._rate_limit_lock:
            security._rate_limit_store.clear()

    assert calls[0] == {
        "hours": 168,
        "limit": 50,
        "feed_url": None,
        "include_event_identity": True,
    }
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"


def test_cached_feed_observation_title_is_compact():
    from routes.feed_routes import _cached_feed_entry

    row = (
        "https://example.com/article",
        "[⭐4.0 ⚖️ 观察] 原始标题 💡模型点评",
        "原始标题",
        4.0,
        "观察",
        "逻辑",
        "摘要",
        1,
    )

    entry = _cached_feed_entry(row, observation=True)

    assert entry.title == "[观察] 原始标题"
    assert entry.ai_summary == "摘要"


def test_cached_score_hit_repairs_publication_time_and_feed_url(monkeypatch):
    import core.processor as processor
    import db.database as database

    # process_entry re-reads this link's live cache row; give it a schema.
    database.init_db()

    timestamp = calendar.timegm((2026, 7, 13, 12, 3, 53, 0, 0, 0))
    entry = SimpleNamespace(
        title="原始标题",
        link="https://example.com/article",
        summary="正文",
        published_parsed=time.gmtime(timestamp),
        get=lambda key, default="": default,
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
        "_runtime_feed_url_aliases": {
            "http://rsshub:1200/example/rss": [
                "http://127.0.0.1:1200/example/rss"
            ]
        },
    }
    cached = {
        entry.link: (
            "[⭐8.5 📈 政策] 原始标题",
            "原始标题",
            8.5,
            "政策",
            "逻辑",
            timestamp - 8 * 3600,
            "摘要",
            "test-v1",
            "llm",
        )
    }

    result, reason = processor.process_entry(
        entry,
        [],
        "prompt",
        cfg,
        feed_url="http://127.0.0.1:1200/example/rss",
        link_cache=cached,
    )

    assert reason == "cache"
    assert result is entry
    assert repaired == [
        (
            entry.link,
                {
                    "created_at": timestamp,
                    "feed_url": "http://rsshub:1200/example/rss",
                    "backtest_selection": {
                        "strategy_version": "test-v1",
                        "aggregation_threshold": 7.0,
                    },
                },
        )
    ]


def test_metadata_repair_includes_cached_items_beyond_scoring_cap(monkeypatch):
    import core.processor as processor

    timestamp = calendar.timegm((2026, 7, 13, 12, 3, 53, 0, 0, 0))
    older_cached = SimpleNamespace(
        link="https://example.com/older",
        published_parsed=time.gmtime(timestamp),
        get=lambda key, default="": default,
    )
    uncached = SimpleNamespace(
        link="https://example.com/new",
        published_parsed=time.gmtime(timestamp + 60),
        get=lambda key, default="": default,
    )
    writes = []
    monkeypatch.setattr(
        processor.DB,
        "get_cached_entries_by_links",
        lambda links: {older_cached.link: ("cached",)},
    )
    monkeypatch.setattr(
        processor.DB,
        "update_cached_entry_origins",
        lambda rows: writes.extend(rows),
    )
    cfg = {
        "_runtime_feed_url_aliases": {
            "http://rsshub:1200/gelonghui/home": [
                "http://127.0.0.1:1200/gelonghui/home"
            ]
        }
    }

    cached = processor._repair_cached_feed_origins(
        [uncached, older_cached],
        "http://127.0.0.1:1200/gelonghui/home",
        cfg,
    )

    assert older_cached.link in cached
    assert writes == [
        (
            older_cached.link,
            timestamp,
            "http://rsshub:1200/gelonghui/home",
        )
    ]
