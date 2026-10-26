import sqlite3
import time
from collections import Counter

from sse_engine import (
    _load_from_db,
    _render_cards,
    _select_balanced_cards,
    _source_bucket,
)


def _entry(source, suffix, ts, feed_url=None):
    return {
        "title": f"{source}情报{suffix}",
        "link": f"https://news.example/{source}/{suffix}",
        "score": 8.0,
        "tag": "宏观",
        "logic": "测试逻辑",
        "time": ts,
        "source": source,
        "feed_url": feed_url or f"https://rss.example/{source}",
    }


def test_balanced_cards_prioritize_fresh_and_share_source_cap():
    now = 2_000_000_000
    entries = [
        _entry("华尔街见闻·实时", "fresh-1", now - 100, "http://rss/wallstreetcn/live"),
        _entry("华尔街见闻全球", "fresh-2", now - 200, "http://rss/wallstreetcn/news/global"),
        _entry("华尔街见闻·A股实时", "old", now - 20 * 3600, "http://rss/wallstreetcn/live/a-stock"),
        _entry("格隆汇", "fresh", now - 300, "http://rss/gelonghui/home"),
        _entry("格隆汇", "old-1", now - 18 * 3600, "http://rss/gelonghui/home"),
        _entry("格隆汇", "old-2", now - 19 * 3600, "http://rss/gelonghui/home"),
        _entry("财联社电报", "old", now - 14 * 3600, "http://rss/cls/telegraph/watch"),
    ]

    selected, fresh_count, fallback_count = _select_balanced_cards(entries, now_ts=now)

    counts = Counter(_source_bucket(entry) for entry in selected)
    assert max(counts.values()) <= 2
    assert counts["华尔街见闻"] == 2
    assert counts["格隆汇"] == 2
    assert fresh_count == 3
    assert fallback_count == 2
    assert all(entry["time"] >= now - 12 * 3600 for entry in selected[:fresh_count])
    assert all(entry["time"] < now - 12 * 3600 for entry in selected[fresh_count:])
    for partition in (selected[:fresh_count], selected[fresh_count:]):
        assert all(
            _source_bucket(previous) != _source_bucket(current)
            for previous, current in zip(partition, partition[1:])
        )


def test_fallback_never_precedes_remaining_fresh_cards():
    now = 2_000_000_000
    entries = [
        _entry("格隆汇", "fresh-1", now - 1 * 3600),
        _entry("格隆汇", "fresh-2", now - 2 * 3600),
        _entry("36氧快讯", "old", now - 13 * 3600, "http://rss/36kr/newsflashes"),
    ]

    selected, fresh_count, fallback_count = _select_balanced_cards(entries, now_ts=now)

    assert fresh_count == 2
    assert fallback_count == 1
    assert [entry["source"] for entry in selected] == ["格隆汇", "格隆汇", "36氧快讯"]


def test_balanced_cards_do_not_relax_cap_when_list_is_short():
    now = 2_000_000_000
    entries = [_entry("格隆汇", index, now - index) for index in range(6)]

    selected, fresh_count, fallback_count = _select_balanced_cards(entries, now_ts=now)

    assert len(selected) == 2
    assert fresh_count == 2
    assert fallback_count == 0


def test_explicit_single_source_mode_can_disable_cap():
    now = 2_000_000_000
    entries = [_entry("格隆汇", index, now - index) for index in range(6)]

    selected, fresh_count, fallback_count = _select_balanced_cards(
        entries,
        now_ts=now,
        max_per_source=None,
    )

    assert len(selected) == 6
    assert fresh_count == 6
    assert fallback_count == 0


def test_source_bucket_does_not_let_article_link_override_feed_identity():
    assert _source_bucket({
        "source": "格隆汇",
        "feed_url": "http://rss/gelonghui/home",
        "link": "https://wallstreetcn.com/articles/linked-from-story",
    }) == "格隆汇"
    assert _source_bucket({
        "source": "",
        "feed_url": "http://rss/gelonghui/home",
        "link": "https://wallstreetcn.com/articles/linked-from-story",
    }) == "格隆汇"


def test_render_cards_shows_and_escapes_source():
    entry = _entry("安全来源", "x", int(time.time()))
    entry["source"] = "<script>alert(1)</script>"
    html = _render_cards([entry])

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_load_from_db_preserves_feed_url_and_configured_source(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE news_cache ("
        "title TEXT, link TEXT, score REAL, tag TEXT, logic TEXT, "
        "created_at INTEGER, feed_url TEXT)"
    )
    conn.execute(
        "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "央行重要消息",
            "https://example.com/a",
            8.5,
            "宏观",
            "影响流动性",
            int(time.time()),
            "http://rsshub:1200/wallstreetcn/live",
        ),
    )
    conn.execute(
        "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "未配置压测消息",
            "https://example.com/news/1",
            9.5,
            "测试",
            "不应进入实时组件",
            int(time.time()),
            "unknown",
        ),
    )
    conn.execute(
        "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "伪造同路径来源",
            "https://example.com/news/2",
            9.8,
            "测试",
            "外部主机不能冒充 Docker host rewrite",
            int(time.time()),
            "https://evil.example/wallstreetcn/live",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("db.database.DB_PATH", str(db_path))
    cfg = {
        "threshold": {"pass": 7},
        "feeds": [{
            "name": "华尔街见闻·实时",
            "url": "http://rsshub:1200/wallstreetcn/live",
            "aggregation_threshold": 7,
        }],
        "_runtime_feed_url_aliases": {
            "http://rsshub:1200/wallstreetcn/live": [
                "http://127.0.0.1:1200/wallstreetcn/live"
            ]
        },
    }

    entries = _load_from_db(limit=5, cfg=cfg)

    assert len(entries) == 1
    assert entries[0]["feed_url"] == "http://rsshub:1200/wallstreetcn/live"
    assert entries[0]["source"] == "华尔街见闻·实时"


def test_load_from_db_rejects_disabled_and_unknown_feeds(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE news_cache ("
        "title TEXT, link TEXT, score REAL, tag TEXT, logic TEXT, "
        "created_at INTEGER, feed_url TEXT)"
    )
    now = int(time.time())
    conn.executemany(
        "INSERT INTO news_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("禁用源", "https://example.com/disabled", 9.5, "测试", "", now, "http://rss/disabled"),
            ("未知源", "https://example.com/unknown", 9.5, "测试", "", now, "unknown"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("db.database.DB_PATH", str(db_path))
    cfg = {
        "threshold": {"pass": 7},
        "feeds": [{"name": "禁用源", "url": "http://rss/disabled", "enabled": False}],
    }

    assert _load_from_db(limit=5, cfg=cfg) == []
    assert _load_from_db(limit=5, cfg=cfg, feed_url="unknown") == []
