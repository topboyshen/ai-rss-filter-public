import time
from types import SimpleNamespace

import pytest

from core.digest_engine import dedupe_articles
from db.event_core import merge_events, resolve_or_create_event


@pytest.fixture
def event_db():
    import db.database as database

    database.init_db()
    with database.get_connection() as conn:
        first = resolve_or_create_event(
            conn,
            {"wire": "first"},
            title="第一版事件",
            event_type="policy",
            direction="bullish",
            priority=8,
            occurred_at=100,
            now=100,
        )
        second = resolve_or_create_event(
            conn,
            {"wire": "second"},
            title="第二个事件",
            event_type="industry",
            direction="neutral",
            priority=6,
            occurred_at=101,
            now=101,
        )
    return database, first, second


def _insert_article(database, *, link, event_id, title="央行政策更新", score=8.5):
    with database.get_connection() as conn:
        version = conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, summary, created_at, "
            "feed_url, direction, event_id, event_version, score_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
            (
                link,
                title,
                title,
                score,
                "宏观",
                "政策传导",
                "摘要内容足够长，用于稳定事件 Briefing 测试。",
                int(time.time()),
                "https://rss.example/source",
                "看多",
                event_id,
                version,
            ),
        )
        conn.commit()


def test_briefing_query_returns_canonical_event_and_current_version(event_db):
    database, source_id, target_id = event_db
    with database.get_connection() as conn:
        assert merge_events(conn, source_id, target_id, reason="same event") == target_id
        current_version = conn.execute(
            "SELECT version FROM events WHERE event_id = ?", (target_id,)
        ).fetchone()[0]
    link = "https://example.com/briefing-1"
    _insert_article(database, link=link, event_id=source_id)

    from services.intelligence_query import query_briefing_tuples
    from routes.dashboard_routes import _briefing_article_from_row

    rows = query_briefing_tuples(
        {"threshold": {"pass": 7}}, min_score=7, hours=48, limit=5
    )
    assert len(rows) == 1
    assert rows[0][10:] == (target_id, current_version)
    article = _briefing_article_from_row(rows[0])
    assert article["event_id"] == target_id
    assert article["event_version"] == current_version


def test_dedupe_uses_event_identity_before_title(event_db):
    _database, first, second = event_db
    articles = [
        {
            "event_id": first,
            "event_version": 1,
            "title": "完全不同的标题 A",
            "link": "https://example.com/a",
            "score": 8,
            "created_at": 10,
        },
        {
            "event_id": first,
            "event_version": 1,
            "title": "完全不同的标题 B",
            "link": "https://example.com/b",
            "score": 9,
            "created_at": 11,
        },
        {
            "event_id": second,
            "event_version": 1,
            "title": "完全不同的标题 B",
            "link": "https://example.com/c",
            "score": 7,
            "created_at": 12,
        },
    ]

    result = dedupe_articles(articles)
    assert len(result) == 2
    by_id = {article["event_id"]: article for article in result}
    assert by_id[first]["duplicate_count"] == 2
    assert by_id[first]["title"] == "完全不同的标题 B"
    assert by_id[second]["duplicate_count"] == 1


def test_dedupe_does_not_treat_broken_event_reference_as_legacy(event_db):
    bogus_id = "3d31d562-4f53-4db7-ab55-c54ec1461cb4"
    articles = [
        {
            "event_id": bogus_id,
            "title": "相同标题不得掩盖损坏的事件引用",
            "link": "https://example.com/broken-a",
        },
        {
            "event_id": bogus_id,
            "title": "相同标题不得掩盖损坏的事件引用",
            "link": "https://example.com/broken-b",
        },
    ]

    result = dedupe_articles(articles)
    assert len(result) == 2
    assert all("event_id" not in article for article in result)


def test_webhook_success_uses_durable_scoring_result_without_broadcast(monkeypatch):
    import routes.webhook_routes as webhook_routes

    event_id = "e3f5489a-af8f-4c62-a58f-baa6dfd95e31"
    scored = SimpleNamespace(
        title="[⭐8.5 宏观] 央行信号",
        summary="流动性改善",
        event_id=event_id,
        event_version=4,
    )
    processed_titles = []
    monkeypatch.setattr(webhook_routes, "Scorer", lambda: object())
    monkeypatch.setattr(
        webhook_routes,
        "_cfg",
        lambda: {
            "feeds": [
                {"name": "测试源", "url": "https://rss.example/source"}
            ]
        },
    )
    monkeypatch.setattr(
        webhook_routes, "resolve_prompt", lambda *_args: ("default", "prompt")
    )
    monkeypatch.setattr(webhook_routes, "_find_notify_threshold", lambda *_args: 9)
    def _process_entry(entry, *_args, **_kwargs):
        processed_titles.append(entry.title)
        return scored, "scored"

    monkeypatch.setattr(webhook_routes, "process_entry", _process_entry)
    monkeypatch.setattr(webhook_routes.DB, "set_app_meta", lambda *_args: None)
    monkeypatch.setattr(webhook_routes.DB, "update_feed_health", lambda **_kwargs: None)
    assert webhook_routes._score_in_background(
        {
            "feed_url": "https://rss.example/source",
            "entries": [
                {
                    "title": (
                        '&lt;span class="section-news"&gt;央行信号&lt;/span&gt;'
                        '&lt;br /&gt;&lt;img src=x onerror="alert(1)"&gt;'
                    ),
                    "url": "https://example.com/webhook-event",
                    "content": "正文",
                }
            ],
        }
    )
    assert processed_titles == ["央行信号"]


def test_event_radar_read_model_cleans_frozen_legacy_evidence_for_display():
    from services.event_radar_query import _source_dict

    source = _source_dict({
        "evidence_id": "7ad379d4-8f01-4f49-83d1-3989d255058b",
        "source_name": "金十数据",
        "source_uri": "https://flash.jin10.com/detail/20260728115559661800",
        "claim_text": (
            '<span class="section-news">1. 国资委推进央企AI+行动</span><br />'
            '<img src=x onerror="alert(1)">'
        ),
        "excerpt": "&lt;b&gt;国家级产业政策&lt;/b&gt;",
        "stance": "supports",
        "reliability": 0.8,
        "announcement_at": None,
        "observed_at": 1,
        "version": 1,
        "observation_count": 1,
    })

    assert source["claim_text"] == "1. 国资委推进央企AI+行动"
    assert source["excerpt"] == "国家级产业政策"
    assert "span" not in source["claim_text"]
    assert "onerror" not in source["claim_text"]
