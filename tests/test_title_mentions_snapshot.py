"""Title-mention failures are independent of worker news snapshot availability."""

import sqlite3
import time

from flask import Flask
import pytest

import db.database as database
from services import dashboard_snapshots as snapshots
from services import news_mentions
import sse_engine


@pytest.fixture
def mention_snapshot_env(tmp_path, monkeypatch):
    directory = tmp_path / "dashboard_snapshots"
    database_path = tmp_path / "cache.db"
    with sqlite3.connect(database_path):
        pass
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(directory))
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:title-mentions-test")
    monkeypatch.setattr(database, "DB_PATH", str(database_path))
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.add_url_rule("/glance-sse", view_func=sse_engine.glance_sse_widget)
    return app, directory, database_path


def test_master_read_failure_still_publishes_news_and_unavailable_state(
    mention_snapshot_env, monkeypatch,
):
    app, directory, database_path = mention_snapshot_env
    title = "圣诺生物：子公司收到检查通知"
    feed_url = "https://feed.example/a"
    monkeypatch.setattr(sse_engine, "load_config", lambda: {
        "threshold": {"pass": 7},
        "feeds": [{"name": "测试来源", "url": feed_url, "enabled": True}],
    })
    loader_calls, master_calls = [], []

    def load_news(**kwargs):
        loader_calls.append(kwargs)
        return [{
            "title": title, "raw_title": title, "score": 8.8,
            "importance_score": 8.8, "direction": "中性",
            "link": "https://news.example/article", "source": "测试来源",
            "feed_url": feed_url, "time": int(time.time()) - 60,
        }]

    def fail_master_read(*_args, **_kwargs):
        master_calls.append(True)
        raise sqlite3.OperationalError("private-path: security master unavailable")

    monkeypatch.setattr(sse_engine, "_load_from_db", load_news)
    monkeypatch.setattr(database, "_equity_mention_index", fail_master_read)
    snapshots.write_dashboard_snapshot(
        "realtime", "<div>previous news</div>",
        generated_at=_fresh_snapshot_generated_at(),
        snapshot_dir=directory, db_path=database_path,
    )

    published = snapshots._build_one_dashboard_snapshot(
        "realtime", app, snapshot_dir=directory, db_path=database_path,
    )

    assert loader_calls == [
        {"limit": 30, "min_score": None, "feed_url": None,
         "cfg": sse_engine.load_config(), "strict": True},
    ]
    assert master_calls == [True]
    assert title in published["html"]
    assert "标题提及：识别暂不可用" in published["html"]
    assert "暂不展示资产关联" not in published["html"]
    assert "private-path" not in published["html"]
    assert "近48小时暂无" not in published["html"]
    stored = snapshots.read_dashboard_snapshot(
        "realtime", snapshot_dir=directory, db_path=database_path,
    )
    assert stored["html"] == published["html"]


def test_web_reads_published_mentions_without_loading_security_master(
    mention_snapshot_env, monkeypatch,
):
    app, directory, database_path = mention_snapshot_env
    html = '<article>测试新闻<div>标题提及：识别暂不可用</div></article>'
    snapshots.write_dashboard_snapshot(
        "realtime", html, generated_at=_fresh_snapshot_generated_at(),
        snapshot_dir=directory, db_path=database_path,
    )
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("Web snapshot reads must not resolve company mentions")

    monkeypatch.setattr(sse_engine, "_load_from_db", forbidden)
    monkeypatch.setattr(news_mentions, "attach_title_mentions", forbidden)
    monkeypatch.setattr(database, "get_direct_equity_mentions_batch", forbidden)

    response = app.test_client().get("/glance-sse")

    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    rendered = response.get_data(as_text=True)
    assert rendered == html
    assert "页面数据快照：" not in rendered
    assert response.headers["X-Card-Snapshot-Generated-At"]
    assert snapshots.read_dashboard_snapshot(
        "realtime", snapshot_dir=directory, db_path=database_path,
    )["html"] == html
    assert calls == []
def _fresh_snapshot_generated_at() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
