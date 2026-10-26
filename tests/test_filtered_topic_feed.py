import sqlite3
import time
from types import SimpleNamespace

import pytest


PRODUCTION_MARKUP_TITLE = (
    "国内新闻： 1. 央行公告，8月5日将开展5000亿元买断式逆回购操作，"
    '期限为3个月。 <span class="section-news...'
)
PRODUCTION_PLAIN_TITLE = (
    "国内新闻： 1. 央行公告，8月5日将开展5000亿元买断式逆回购操作，"
    "期限为3个月。"
)


def _topic_cfg(*, aggregation_threshold=6.0):
    return {
        "threshold": {"pass": 7.0, "notify": 9.0},
        "scoring_strategy_version": "topic-v1",
        "feeds": [
            {
                "url": "feed-a",
                "name": "测试源",
                "aggregation_threshold": aggregation_threshold,
            }
        ],
        "briefing_themes": [
            {
                "name": "AI 算力链",
                "keywords": ["AI", "算力", "GPU", "NVIDIA"],
            },
            {
                "name": "芯片国产化",
                "keywords": ["国产芯片", "半导体", "芯片"],
            },
        ],
    }


def _insert_cache_rows(database, rows):
    with database.get_connection() as conn:
        conn.executemany(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            rows,
        )
        conn.commit()


def test_configured_theme_resolver_uses_whitelist_aliases_and_merged_terms():
    from core.theme_rules import (
        InvalidThemeError,
        canonical_theme_name,
        resolve_configured_theme,
    )

    cfg = {
        "briefing_themes": [
            {"name": "AI 算力链", "keywords": ["GPU", "NVIDIA"]},
            {"name": "算力基建", "keywords": ["服务器", "gpu"]},
        ]
    }

    resolved = resolve_configured_theme(cfg, "ai算力")

    assert resolved is not None
    assert resolved.name == "AI 算力链"
    assert {term.casefold() for term in resolved.terms} >= {
        "ai 算力链",
        "gpu",
        "nvidia",
        "服务器",
    }
    assert canonical_theme_name("数字中国") == "AI 算力链"
    assert resolve_configured_theme(cfg, "  ") is None
    with pytest.raises(KeyError):
        resolve_configured_theme(cfg, "不存在主题")
    with pytest.raises(InvalidThemeError):
        resolve_configured_theme(cfg, "AI 算力链" + "伪" * 70)


def test_topic_and_score_filters_run_before_sql_limit(monkeypatch):
    import db.database as database

    database.init_db()
    now = int(time.time())
    rows = [
        (
            f"noise-{index}",
            f"高分但不相关 {index}",
            f"高分但不相关 {index}",
            9.8,
            "宏观",
            "无关逻辑",
            now - index,
            "摘要",
            "feed-a",
            "topic-v1",
        )
        for index in range(100)
    ]
    rows.extend(
        [
            (
                "matching-low",
                "国产芯片出现进展",
                "国产芯片出现进展",
                8.9,
                "科技",
                "逻辑",
                now - 200,
                "摘要",
                "feed-a",
                "topic-v1",
            ),
            (
                "matching-high",
                "半导体设备取得突破",
                "半导体设备取得突破",
                9.2,
                "科技",
                "逻辑",
                now - 300,
                "摘要",
                "feed-a",
                "topic-v1",
            ),
        ]
    )
    _insert_cache_rows(database, rows)

    result = database.get_entries_for_cached_rss(
        _topic_cfg(),
        hours=24,
        limit=1,
        min_score=9.0,
        theme_terms=("芯片国产化", "国产芯片", "半导体", "芯片"),
    )

    assert [row[0] for row in result] == ["matching-high"]
    assert float(result[0][3]) >= 9.0


def test_explicit_min_score_cannot_bypass_source_aggregation_threshold():
    import db.database as database

    database.init_db()
    now = int(time.time())
    rows = [
        (
            f"below-source-floor-{index}",
            f"AI 服务器普通更新 {index}",
            f"AI 服务器普通更新 {index}",
            7.9,
            "AI 算力链",
            "逻辑",
            now - index,
            "摘要",
            "feed-a",
            "topic-v1",
        )
        for index in range(100)
    ]
    rows.append(
        (
            "at-source-floor",
            "GPU 服务器订单",
            "GPU 服务器订单",
            8.0,
            "AI 算力链",
            "逻辑",
            now - 200,
            "摘要",
            "feed-a",
            "topic-v1",
        )
    )
    _insert_cache_rows(database, rows)

    result = database.get_entries_for_cached_rss(
        _topic_cfg(aggregation_threshold=8.0),
        feed_url="feed-a",
        limit=1,
        min_score=0.0,
        theme_terms=("AI 算力链", "GPU"),
    )

    assert [row[0] for row in result] == ["at-source-floor"]


def test_global_feed_applies_each_source_floor_before_limit():
    import db.database as database

    database.init_db()
    now = int(time.time())
    cfg = _topic_cfg(aggregation_threshold=8.0)
    cfg["feeds"].append(
        {
            "url": "feed-b",
            "name": "低门槛测试源",
            "aggregation_threshold": 6.0,
        }
    )
    rows = [
        (
            f"high-source-below-floor-{index}",
            f"高门槛源未达标 {index}",
            f"高门槛源未达标 {index}",
            7.9,
            "科技",
            "逻辑",
            now - index,
            "摘要",
            "feed-a",
            "topic-v1",
        )
        for index in range(100)
    ]
    rows.append(
        (
            "low-source-eligible",
            "低门槛源合格消息",
            "低门槛源合格消息",
            7.0,
            "科技",
            "逻辑",
            now - 200,
            "摘要",
            "feed-b",
            "topic-v1",
        )
    )
    _insert_cache_rows(database, rows)

    result = database.get_entries_for_cached_rss(
        cfg,
        limit=1,
        min_score=0.0,
    )

    assert [row[0] for row in result] == ["low-source-eligible"]


def test_global_feed_resolves_exact_threshold_inside_runtime_alias_family():
    import db.database as database

    database.init_db()
    now = int(time.time())
    cfg = _topic_cfg(aggregation_threshold=8.0)
    cfg["feeds"].append(
        {
            "url": "feed-b",
            "name": "别名家族低门槛源",
            "aggregation_threshold": 6.0,
        }
    )
    cfg["_runtime_feed_url_aliases"] = {"feed-a": ["feed-b"]}
    rows = [
        (
            f"canonical-below-floor-{index}",
            f"高门槛主源未达标 {index}",
            f"高门槛主源未达标 {index}",
            7.9,
            "科技",
            "逻辑",
            now - index,
            "摘要",
            "feed-a",
            "topic-v1",
        )
        for index in range(100)
    ]
    rows.append(
        (
            "exact-alias-eligible",
            "精确别名源合格消息",
            "精确别名源合格消息",
            7.0,
            "科技",
            "逻辑",
            now - 200,
            "摘要",
            "feed-b",
            "topic-v1",
        )
    )
    _insert_cache_rows(database, rows)

    result = database.get_entries_for_cached_rss(cfg, limit=1, min_score=0.0)

    assert [row[0] for row in result] == ["exact-alias-eligible"]


def test_public_high_score_and_preview_queries_exclude_quality_quarantine():
    import db.database as database

    database.init_db()
    now = int(time.time())
    _insert_cache_rows(
        database,
        [
            (
                "clean-public-row",
                "Clean public row",
                "Clean public row",
                8.5,
                "宏观",
                "clean logic",
                now - 1,
                "clean summary",
                "feed-a",
                "topic-v1",
            ),
            (
                "clean-text-quarantine",
                "PoC clean-text quarantined row",
                "PoC clean-text quarantined row",
                9.5,
                "宏观",
                "apparently clean logic",
                now,
                "apparently clean summary",
                "feed-a",
                "topic-v1",
            ),
            (
                "dirty-safe-neutral",
                "Safe neutral must remain private",
                "Safe neutral must remain private",
                9.8,
                "宏观",
                "apparently clean logic",
                now + 1,
                "apparently clean summary",
                "feed-a",
                "topic-v1",
            ),
        ],
    )
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE news_cache SET score_path = 'quality_quarantine', "
            "error_type = 'invalid_model_output' WHERE link = ?",
            ("clean-text-quarantine",),
        )
        conn.execute(
            "UPDATE news_cache SET score_path = "
            "' LLM_SAFE_NEUTRAL_CONTRACT ' WHERE link = ?",
            ("dirty-safe-neutral",),
        )
        conn.commit()

    high_score = database.get_entries_for_cached_rss(
        _topic_cfg(),
        feed_url="feed-a",
        limit=2,
        min_score=0.0,
    )
    preview = database.get_recent_entries_for_feed_preview(
        _topic_cfg(),
        feed_url="feed-a",
        limit=2,
        order_by="recent",
    )
    summary = database.get_high_score_articles_with_summary(
        cfg=_topic_cfg(),
        feed_url="feed-a",
        limit=3,
        min_score=0.0,
    )

    assert [row[0] for row in high_score] == ["clean-public-row"]
    assert [row[0] for row in preview] == ["clean-public-row"]
    assert [row[0] for row in summary] == ["clean-public-row"]


def test_public_query_supports_legacy_schema_without_score_path(monkeypatch):
    import services.intelligence_query as intelligence_query

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE news_cache ("
            "link TEXT PRIMARY KEY, title TEXT, raw_title TEXT, score REAL, "
            "tag TEXT, logic TEXT, summary TEXT, created_at INTEGER, "
            "feed_url TEXT, direction TEXT)"
        )
        conn.execute(
            "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-row",
                "Legacy public row",
                "Legacy public row",
                8.0,
                "宏观",
                "legacy logic",
                "legacy summary",
                int(time.time()),
                "feed-a",
                "看多",
            ),
        )
        monkeypatch.setattr(intelligence_query, "get_connection", lambda: conn)

        items = intelligence_query.query_intelligence_items(
            {},
            view="feed_preview",
            limit=2,
        )
    finally:
        conn.close()

    assert [item.link for item in items] == ["legacy-row"]


@pytest.fixture
def topic_feed_client(monkeypatch):
    import app as app_module
    import routes.feed_routes as feed_routes
    import routes.security as security

    monkeypatch.setattr(feed_routes, "_cfg", lambda: _topic_cfg())
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    try:
        with app_module.app.test_client() as client:
            yield client, feed_routes
    finally:
        with security._rate_limit_lock:
            security._rate_limit_store.clear()


def test_feed_rss_exposes_read_only_topic_slice(topic_feed_client, monkeypatch):
    client, feed_routes = topic_feed_client
    captured = {}
    row = (
        "https://example.com/chip",
        "[⭐9.1 📈 科技] 国产芯片订单增长 💡产业链验证",
        "国产芯片订单增长",
        9.1,
        "科技",
        "产业链验证",
        "摘要",
        int(time.time()),
    )

    def load(_cfg, **kwargs):
        captured.update(kwargs)
        return [row]

    monkeypatch.setattr(feed_routes.DB, "get_entries_for_cached_rss", load)
    monkeypatch.setattr(
        feed_routes.DB,
        "get_recent_entries_for_feed_preview",
        lambda *_args, **_kwargs: pytest.fail("filtered RSS must not use preview"),
    )
    monkeypatch.setattr(
        feed_routes,
        "process_feed",
        lambda *_args, **_kwargs: pytest.fail("filtered RSS must not score"),
    )

    response = client.get(
        "/feed/rss?min_score=8.5&theme=芯片国产化&hours=48&limit=30"
    )

    assert response.status_code == 200
    assert response.mimetype == "application/rss+xml"
    assert captured == {
        "hours": 48,
        "limit": 30,
        "feed_url": None,
        "min_score": 8.5,
        "theme_terms": ("芯片国产化", "国产芯片", "半导体", "芯片"),
    }
    body = response.get_data(as_text=True)
    assert "AI-Filter · 芯片国产化 · ≥8.5" in body
    assert "只读数据库缓存切片；主题=芯片国产化；最低分=8.5；不触发抓取或评分" in body
    assert "国产芯片订单增长" in body


@pytest.mark.parametrize("format_type", ["json", "rss", "html"])
def test_public_feed_formats_strip_provider_markup_at_db_output_boundary(
    topic_feed_client, monkeypatch, format_type
):
    client, feed_routes = topic_feed_client
    row = (
        "https://flash.jin10.com/detail/20260804235338178800",
        f"[⭐8.8 📈 宏观] {PRODUCTION_MARKUP_TITLE} 💡政策工具",
        PRODUCTION_MARKUP_TITLE,
        8.8,
        "宏观",
        "政策工具",
        "正常摘要",
        int(time.time()),
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda *_args, **_kwargs: [row],
    )

    response = client.get(f"/feed/{format_type}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert PRODUCTION_PLAIN_TITLE in body
    assert "正常摘要" in body
    assert "span" not in body.casefold()
    assert "section-news" not in body.casefold()
    if format_type == "json":
        assert response.get_json()["items"][0]["title"] == PRODUCTION_PLAIN_TITLE


@pytest.mark.parametrize("format_type", ["json", "rss", "html"])
def test_public_feed_formats_drop_polluted_cache_rows_without_backfill(
    topic_feed_client, monkeypatch, caplog, format_type
):
    client, feed_routes = topic_feed_client
    now = int(time.time())
    polluted = (
        "https://example.test/polluted",
        "PoC clean-looking cached title",
        "PoC clean-looking cached title",
        9.5,
        "damaged\ufffdtag",
        "apparently clean logic",
        "apparently clean summary",
        now,
    )
    clean = (
        "https://example.test/clean",
        "Clean retained cached title",
        "Clean retained cached title",
        8.5,
        "macro",
        "clean logic",
        "clean summary",
        now - 1,
    )
    calls = []
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [polluted, clean],
    )
    caplog.set_level("WARNING", logger=feed_routes.__name__)

    response = client.get(f"/feed/{format_type}?limit=2")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["limit"] == 2
    assert "Clean retained cached title" in body
    assert "PoC clean-looking cached title" not in body
    assert "\ufffd" not in body
    assert any(
        f"context=/feed/{format_type} count=1" in record.getMessage()
        for record in caplog.records
    )
    if format_type == "json":
        assert len(response.get_json()["items"]) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/filter?url=https://example.com/source",
        "/filter/multi?urls=https://example.com/source",
    ],
)
def test_filter_db_fallbacks_strip_provider_markup(
    topic_feed_client, monkeypatch, path
):
    client, feed_routes = topic_feed_client
    cfg = {
        **_topic_cfg(),
        "cache_ttl": 60,
        "output": {"max_items": 30},
    }
    row = (
        "https://flash.jin10.com/detail/20260804235338178800",
        f"[⭐8.8 📈 宏观] {PRODUCTION_MARKUP_TITLE} 💡政策工具",
        PRODUCTION_MARKUP_TITLE,
        8.8,
        "宏观",
        "政策工具",
        "正常摘要",
        int(time.time()),
    )
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(
        feed_routes,
        "resolve_scoring_contract",
        lambda *_args, **_kwargs: cfg,
    )
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(feed_routes, "set_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda *_args, **_kwargs: [row],
    )
    monkeypatch.setattr(
        feed_routes,
        "process_feed",
        lambda *_args, **_kwargs: pytest.fail("DB fallback must not fetch"),
    )

    response = client.get(path)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert PRODUCTION_PLAIN_TITLE in body
    assert "正常摘要" in body
    assert "span" not in body.casefold()
    assert "section-news" not in body.casefold()


@pytest.mark.parametrize(
    ("path", "expected_context"),
    [
        ("/filter?url=https://example.com/source", "/filter DB fallback"),
        (
            "/filter/multi?urls=https://example.com/source",
            "/filter/multi DB fallback",
        ),
    ],
)
def test_filter_db_fallbacks_drop_polluted_rows_without_expanding_query(
    topic_feed_client, monkeypatch, caplog, path, expected_context
):
    client, feed_routes = topic_feed_client
    cfg = {
        **_topic_cfg(),
        "cache_ttl": 60,
        "output": {"max_items": 2},
    }
    now = int(time.time())
    polluted = (
        "https://example.test/polluted",
        "PoC clean-looking fallback title",
        "PoC clean-looking fallback title",
        9.5,
        "damaged\ufffdtag",
        "apparently clean logic",
        "apparently clean summary",
        now,
    )
    clean = (
        "https://example.test/clean",
        "Clean retained fallback title",
        "Clean retained fallback title",
        8.5,
        "macro",
        "clean logic",
        "clean summary",
        now - 1,
    )
    calls = []
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(
        feed_routes,
        "resolve_scoring_contract",
        lambda *_args, **_kwargs: cfg,
    )
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(feed_routes, "set_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [polluted, clean],
    )
    monkeypatch.setattr(
        feed_routes,
        "process_feed",
        lambda *_args, **_kwargs: pytest.fail("clean DB survivor must avoid fetch"),
    )
    caplog.set_level("WARNING", logger=feed_routes.__name__)

    response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["limit"] == 2
    assert "Clean retained fallback title" in body
    assert "PoC clean-looking fallback title" not in body
    assert "\ufffd" not in body
    assert any(
        f"context={expected_context} count=1" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "path",
    [
        "/filter?url=https://example.com/source",
        "/filter/multi?urls=https://example.com/source",
    ],
)
def test_filter_db_fallbacks_do_not_refill_when_every_row_is_excluded(
    topic_feed_client, monkeypatch, path
):
    client, feed_routes = topic_feed_client
    cfg = {
        **_topic_cfg(),
        "cache_ttl": 60,
        "output": {"max_items": 1},
    }
    polluted = (
        "https://example.test/polluted-only",
        "PoC excluded-only fallback title",
        "PoC excluded-only fallback title",
        9.5,
        "damaged\ufffdtag",
        "apparently clean logic",
        "apparently clean summary",
        int(time.time()),
    )
    calls = []
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(
        feed_routes,
        "resolve_scoring_contract",
        lambda *_args, **_kwargs: cfg,
    )
    monkeypatch.setattr(feed_routes, "get_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(feed_routes, "set_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [polluted],
    )
    monkeypatch.setattr(
        feed_routes,
        "process_feed",
        lambda *_args, **_kwargs: pytest.fail("excluded rows must not trigger refill"),
    )

    response = client.get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["limit"] == 1
    assert "<item>" not in body
    assert "PoC excluded-only fallback title" not in body


def test_filter_multi_sanitizes_titles_loaded_from_legacy_xml_cache(
    topic_feed_client, monkeypatch
):
    client, feed_routes = topic_feed_client
    cfg = {
        **_topic_cfg(),
        "cache_ttl": 60,
        "output": {"max_items": 30},
    }
    cached_results = iter([None, "legacy XML"])
    legacy_title = f"[⭐8.8 📈 宏观] {PRODUCTION_MARKUP_TITLE}"
    monkeypatch.setattr(feed_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(feed_routes, "validate_target_url", lambda *_args: (True, ""))
    monkeypatch.setattr(
        feed_routes,
        "resolve_scoring_contract",
        lambda *_args, **_kwargs: cfg,
    )
    monkeypatch.setattr(
        feed_routes,
        "get_cached",
        lambda *_args, **_kwargs: next(cached_results),
    )
    monkeypatch.setattr(feed_routes, "set_cached", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        feed_routes.feedparser,
        "parse",
        lambda _xml: SimpleNamespace(
            version="rss20",
            bozo=False,
            entries=[
                {
                    "title": legacy_title,
                    "link": "https://example.com/cached",
                    "summary": "缓存摘要",
                    "published_parsed": time.gmtime(1),
                },
                {
                    "title": '<span class="section-news...',
                    "link": "https://example.com/untitled",
                    "summary": "空标题摘要",
                    "published_parsed": time.gmtime(2),
                },
            ]
        ),
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda *_args, **_kwargs: pytest.fail("legacy XML must avoid DB fallback"),
    )

    response = client.get("/filter/multi?urls=https://example.com/source")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"[⭐8.8 📈 宏观] {PRODUCTION_PLAIN_TITLE}" in body
    assert "缓存摘要" in body
    assert "未命名情报" in body
    assert "span" not in body.casefold()
    assert "section-news" not in body.casefold()


def test_feed_rss_defaults_to_pass_threshold(topic_feed_client, monkeypatch):
    client, feed_routes = topic_feed_client
    calls = []
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [],
    )

    response = client.get("/feed/rss")

    assert response.status_code == 200
    assert calls == [
        {"hours": 48, "limit": 30, "feed_url": None, "min_score": 7.0}
    ]
    assert "AI-Filter · 全部主题 · ≥7.0" in response.get_data(as_text=True)


def test_existing_feed_json_contract_is_unchanged_without_slice_params(
    topic_feed_client, monkeypatch
):
    client, feed_routes = topic_feed_client
    calls = []
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [],
    )

    response = client.get("/feed/json")

    assert response.status_code == 200
    assert calls == [
        {
            "hours": 24,
            "limit": 30,
            "feed_url": None,
            "include_event_identity": True,
        }
    ]
    assert response.get_json() == {
        "title": "AI 智能过滤: 综合情报",
        "link": "all",
        "strategy_version": "topic-v1",
        "items": [],
    }


def test_feed_json_uses_structured_score_and_opted_in_event_identity(
    topic_feed_client, monkeypatch
):
    client, feed_routes = topic_feed_client
    event_id = "81506d30-5263-4765-a24d-55dbfdb9dcf4"
    calls = []
    row = (
        "https://example.com/structured-metadata",
        "Plain cached title without a score prefix",
        "Plain cached title without a score prefix",
        8.5,
        "macro",
        "policy signal",
        "cached summary",
        int(time.time()),
        event_id,
        3,
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [row],
    )

    response = client.get("/feed/json")

    assert response.status_code == 200
    assert calls == [
        {
            "hours": 24,
            "limit": 30,
            "feed_url": None,
            "include_event_identity": True,
        }
    ]
    assert response.get_json()["items"][0] == {
        "title": "Plain cached title without a score prefix",
        "link": "https://example.com/structured-metadata",
        "summary": "cached summary",
        "score": 8.5,
        "event_id": event_id,
        "event_version": 3,
    }


def test_feed_tuple_event_identity_is_explicit_opt_in(monkeypatch):
    import services.intelligence_query as intelligence_query

    event_id = "81506d30-5263-4765-a24d-55dbfdb9dcf4"
    item = intelligence_query.IntelligenceItem(
        link="https://example.com/item",
        title="title",
        raw_title="raw title",
        score=8.5,
        tag="macro",
        logic="logic",
        summary="summary",
        created_at=123,
        feed_url="feed-a",
        direction="neutral",
        event_id=event_id,
        event_version=3,
    )
    monkeypatch.setattr(
        intelligence_query,
        "query_intelligence_items",
        lambda *_args, **_kwargs: [item],
    )

    legacy = intelligence_query.query_feed_tuples({}, feed_url=None)
    extended = intelligence_query.query_feed_tuples(
        {}, feed_url=None, include_event_identity=True
    )

    assert len(legacy[0]) == 8
    assert legacy[0] == item.as_feed_tuple()
    assert extended[0] == (*legacy[0], event_id, 3)


def test_build_json_feed_keeps_legacy_title_score_fallback():
    import json

    from core.processor import build_json_feed
    from utils.helpers import FeedEntry

    entry = FeedEntry(
        "\u2b507.4 legacy scored title",
        "https://example.com/legacy-score",
        "summary",
    )

    payload = json.loads(build_json_feed([entry], "title", "all", {}))

    assert payload["items"][0]["score"] == 7.4


@pytest.mark.parametrize("format_type", ["rss", "html"])
def test_non_json_feeds_keep_legacy_query_contract(
    topic_feed_client, monkeypatch, format_type
):
    client, feed_routes = topic_feed_client
    calls = []
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda _cfg, **kwargs: calls.append(kwargs) or [],
    )

    response = client.get(f"/feed/{format_type}")

    assert response.status_code == 200
    assert calls == [
        {
            "hours": 48 if format_type == "rss" else 24,
            "limit": 30,
            "feed_url": None,
            **({"min_score": 7.0} if format_type == "rss" else {}),
        }
    ]


def test_unknown_theme_is_a_valid_empty_feed_without_db_or_fallback(
    topic_feed_client, monkeypatch
):
    client, feed_routes = topic_feed_client
    monkeypatch.setattr(
        feed_routes.DB,
        "get_entries_for_cached_rss",
        lambda *_args, **_kwargs: pytest.fail("unknown theme must not query entries"),
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_recent_entries_for_feed_preview",
        lambda *_args, **_kwargs: pytest.fail("unknown theme must not preview"),
    )

    response = client.get("/feed/rss?theme=不存在主题")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "<rss" in body
    assert "<item>" not in body
    assert "不存在主题" in body
    assert "≥7.0" in body


def test_invalid_theme_control_character_returns_400(topic_feed_client):
    client, _feed_routes = topic_feed_client

    response = client.get("/feed/rss?theme=%01芯片")

    assert response.status_code == 400
    assert response.mimetype == "text/plain"


@pytest.mark.parametrize("value", ["99", "-1", "abc", "NaN", "Inf"])
def test_feed_rss_rejects_invalid_min_score(topic_feed_client, value):
    client, _feed_routes = topic_feed_client

    response = client.get(f"/feed/rss?min_score={value}")

    assert response.status_code == 400


def test_events_filter_direction_and_priority_before_limit():
    import db.database as database
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    database.init_db()
    now = int(time.time())
    with database.get_connection() as conn:
        for index in range(50):
            resolve_or_create_event(
                conn,
                {"test": f"neutral-{index}"},
                title=f"中性事件 {index}",
                event_type="test",
                direction="neutral",
                priority=10.0,
                occurred_at=now - 10,
                strategy_version="topic-v1",
                model_version="test",
                now=now,
            )
        bullish_id = resolve_or_create_event(
            conn,
            {"test": "bullish"},
            title="看多事件",
            event_type="test",
            direction="bullish",
            priority=8.0,
            occurred_at=now - 10,
            strategy_version="topic-v1",
            model_version="test",
            now=now,
        )
        upsert_event_evidence(
            conn,
            bullish_id,
            source_uri="https://example.test/bullish",
            source_name="Public Wire",
            claim_key="public-bullish",
            claim_text="public bullish evidence",
            excerpt="public bullish evidence",
            stance="supports",
            reliability=0.8,
            observed_at=now,
            now=now,
        )
        conn.commit()

    events = database.get_events(
        cfg={"scoring_strategy_version": "topic-v1"},
        hours=24,
        limit=1,
        min_score=7.0,
        direction="看多",
    )

    assert len(events) == 1
    assert events[0][0] == bullish_id
    assert events[0][4] == 8.0
    assert events[0][9] == "bullish"


def test_events_route_validates_and_passes_filters(topic_feed_client, monkeypatch):
    client, feed_routes = topic_feed_client
    calls = []
    monkeypatch.setattr(
        feed_routes.DB,
        "get_events",
        lambda **kwargs: calls.append(kwargs) or [],
    )

    response = client.get("/events/rss?min_score=7.5&direction=看多&limit=1")

    assert response.status_code == 200
    assert calls == [
        {
            "cfg": _topic_cfg(),
            "hours": 24,
            "limit": 1,
            "min_score": 7.5,
            "direction": "看多",
        }
    ]
    body = response.get_data(as_text=True)
    assert "A 股事件雷达 · 看多 · 重要度≥7.5" in body
    assert "最低事件重要度=7.5" in body
    assert client.get("/events/rss?direction=上涨").status_code == 400
    assert client.get("/events/rss?min_score=99").status_code == 400


@pytest.mark.parametrize("format_type", ["json", "rss", "html"])
def test_event_feed_formats_sanitize_canonical_title_without_losing_metadata(
    topic_feed_client, monkeypatch, format_type
):
    client, feed_routes = topic_feed_client
    event = (
        "event-1",
        PRODUCTION_MARKUP_TITLE,
        "https://flash.jin10.com/detail/20260804235338178800",
        "事件摘要",
        8.8,
        "宏观政策",
        2,
        int(time.time()) - 60,
        int(time.time()),
        "bullish",
    )
    monkeypatch.setattr(
        feed_routes.DB,
        "get_events",
        lambda **_kwargs: [event],
    )

    response = client.get(f"/events/{format_type}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert PRODUCTION_PLAIN_TITLE in body
    assert "span" not in body.casefold()
    assert "section-news" not in body.casefold()
    if format_type == "json":
        record = response.get_json()["events"][0]
        assert record == {
            "event_id": "event-1",
            "canonical_title": PRODUCTION_PLAIN_TITLE,
            "canonical_link": "https://flash.jin10.com/detail/20260804235338178800",
            "summary": "事件摘要",
            "score": 8.8,
            "tag": "宏观政策",
            "source_count": 2,
            "first_seen_at": event[7],
            "last_seen_at": event[8],
            "direction": "看多",
            "importance_score": 8.8,
        }
    else:
        assert (
            f"[事件 2源｜重要度 8.8/10｜看多｜宏观政策] {PRODUCTION_PLAIN_TITLE}"
            in body
        )


def test_feed_boundaries_fail_closed_when_markup_contains_no_title():
    from routes.feed_routes import _cached_feed_entry, _feed_entry_from_cached_row

    row = (
        "https://example.com/untitled",
        '<span class="section-news...',
        '<span class="section-news...',
        8.8,
        "宏观",
        "逻辑",
        "摘要",
        1,
    )

    assert _feed_entry_from_cached_row(row).title == "未命名情报"
    assert _cached_feed_entry(row, observation=True).title == "[观察] 未命名情报"


def test_event_json_fails_closed_when_markup_contains_no_title(
    topic_feed_client, monkeypatch
):
    client, feed_routes = topic_feed_client
    monkeypatch.setattr(
        feed_routes.DB,
        "get_events",
        lambda **_kwargs: [
            (
                "event-untitled",
                '<span class="section-news...',
                "https://example.com/event",
                "摘要",
                7.5,
                "宏观政策",
                1,
                1,
                2,
                "neutral",
            )
        ],
    )

    response = client.get("/events/json")

    assert response.status_code == 200
    assert response.get_json()["events"][0]["canonical_title"] == "未命名事件"


def test_channel_description_is_escaped_without_changing_legacy_default():
    from core.processor import build_html_feed, build_json_feed, build_rss_xml

    filtered = build_rss_xml(
        [],
        "主题",
        "https://example.com",
        "https://example.com/feed?theme=x",
        {},
        channel_description="主题=<芯片>&只读",
    )
    legacy = build_rss_xml(
        [], "主题", "https://example.com", "https://example.com/feed", {}
    )
    payload = build_json_feed(
        [],
        "主题",
        "https://example.com",
        {},
        channel_description="只读切片",
    )
    html = build_html_feed(
        [],
        "主题",
        "https://example.com",
        {},
        channel_description="主题=<芯片>&只读",
    )

    assert "主题=&lt;芯片&gt;&amp;只读" in filtered
    assert "主题=&lt;芯片&gt;&amp;只读" in html
    assert "AI Filtered RSS Feed for A-Share Macro Intelligence" in legacy
    assert '"description": "只读切片"' in payload
