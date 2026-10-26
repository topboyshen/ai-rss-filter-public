import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def clear_ops_widget_cache():
    import routes.dashboard_routes as dashboard_routes

    with dashboard_routes._OPS_WIDGET_CACHE_LOCK:
        dashboard_routes._OPS_WIDGET_CACHE.clear()
    yield
    with dashboard_routes._OPS_WIDGET_CACHE_LOCK:
        dashboard_routes._OPS_WIDGET_CACHE.clear()


def _setup_ops_db(tmp_path, monkeypatch):
    import db.database as database
    import routes.dashboard_routes as dashboard_routes

    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(dashboard_routes, "get_connection", database.get_connection)
    monkeypatch.setattr(dashboard_routes, "db_lock", database.db_lock)
    database.init_db()
    return database, dashboard_routes


def _patch_ops_cfg(monkeypatch, dashboard_routes, cfg):
    from core.config import config as core_config

    monkeypatch.setattr(dashboard_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(core_config, "_cfg", cfg)


def test_never_checked_feed_data_state_is_unknown():
    from utils.helpers import feed_data_health

    row = ("url", "name", 0, 0, 0, 0, 0, 0, None, None, 0, 0)
    state = feed_data_health(
        row,
        {"feed_health": {"empty_degraded_after": "3"}},
    )

    assert state["data_state"] == "unknown"
    assert state["data_state_label"] == "未检查"
    assert state["data_degraded"] is False

    malformed_cfg_state = feed_data_health(row, {"feed_health": 3})
    assert malformed_cfg_state["data_state"] == "unknown"


def test_ops_health_filters_disabled_and_old_urls(tmp_path, monkeypatch):
    database, dashboard_routes = _setup_ops_db(tmp_path, monkeypatch)
    now = int(time.time())
    new_url = "http://127.0.0.1:1200/cls/telegraph/watch"
    old_url = "http://127.0.0.1:1200/cls/telegraph"
    disabled_url = "http://127.0.0.1:1200/cls/depth"

    cfg = {
        "threshold": {"pass": 5},
        "feeds": [
            {"name": "财联社深度", "url": disabled_url, "enabled": False},
            {"name": "财联社电报", "url": new_url, "enabled": True},
        ],
    }

    _patch_ops_cfg(monkeypatch, dashboard_routes, cfg)

    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO feed_health (url, name, last_check, response_time_ms, success, fail, articles_total, articles_passed, avg_score, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (disabled_url, "财联社深度", now, 100, 0, 3, 0, 0, None, "503 depth failed"),
        )
        conn.execute(
            "INSERT INTO feed_health (url, name, last_check, response_time_ms, success, fail, articles_total, articles_passed, avg_score, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (old_url, "财联社电报", now, 100, 0, 4, 0, 0, None, "503 old route failed"),
        )
        conn.execute(
            "UPDATE feed_health SET name = ?, last_check = ?, response_time_ms = ?, success = ?, fail = ?, "
            "articles_total = ?, articles_passed = ?, avg_score = ?, last_error = ? WHERE url = ?",
            ("财联社电报", now, 80, 1, 0, 2, 1, 7.5, None, new_url),
        )
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("https://example.com/new", "新路由文章", "新路由文章", 8.2, "宏观", "", now, "", new_url, "strategy"),
        )
        conn.execute(
            "INSERT INTO news_cache (link, title, raw_title, score, tag, logic, created_at, summary, feed_url, strategy_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("https://example.com/old", "旧路由文章", "旧路由文章", 8.2, "宏观", "", now, "", old_url, "strategy"),
        )
        conn.commit()

    rows = dashboard_routes._get_health_from_news_cache()
    urls = [row[0] for row in rows]

    assert urls == [new_url]
    assert rows[0][1] == "财联社电报"
    assert rows[0][6] == 1
    assert rows[0][9] is None


def test_ops_feeds_widget_handles_empty_and_malformed_health(tmp_path, monkeypatch):
    database, dashboard_routes = _setup_ops_db(tmp_path, monkeypatch)
    url = "http://127.0.0.1:1200/cls/telegraph/watch"
    cfg = {
        "threshold": {"pass": 5},
        "feeds": [{"name": "财联社电报", "url": url, "enabled": True}],
    }

    _patch_ops_cfg(monkeypatch, dashboard_routes, cfg)

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE feed_health SET name = ?, last_check = ?, response_time_ms = ?, success = ?, fail = ?, "
            "articles_total = ?, articles_passed = ?, avg_score = ?, last_error = ? WHERE url = ?",
            ("财联社电报", "bad-ts", "bad-ms", "bad-success", None, "bad-total", "bad-passed", "bad-score", None, url),
        )
        conn.commit()

    from flask import Flask

    test_app = Flask(__name__)
    with test_app.test_request_context("/ops-feeds"):
        response = dashboard_routes.ops_feeds_widget()

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Feed 源健康明细" in body
    assert "财联社电报" in body
    assert "无数据" in body


def test_ops_feeds_widget_uses_recent_feed_health_without_cache_rows(tmp_path, monkeypatch):
    database, dashboard_routes = _setup_ops_db(tmp_path, monkeypatch)
    now = int(time.time())
    url = "http://127.0.0.1:1200/gov/csrc/news"
    cfg = {
        "threshold": {"pass": 5},
        "feeds": [{"name": "证监会新闻", "url": url, "enabled": True}],
    }

    _patch_ops_cfg(monkeypatch, dashboard_routes, cfg)

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE feed_health SET name = ?, last_check = ?, response_time_ms = ?, success = ?, fail = ?, "
            "articles_total = ?, articles_passed = ?, avg_score = ?, last_error = ? WHERE url = ?",
            ("证监会新闻", now, 100, 1, 0, 5, 0, None, None, url),
        )
        conn.commit()

    from flask import Flask

    test_app = Flask(__name__)
    with test_app.test_request_context("/ops-feeds"):
        response = dashboard_routes.ops_feeds_widget()

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "证监会新闻" in body
    assert "正常" in body
    assert "无数据" not in body


def test_consecutive_empty_feed_is_visible_even_with_recent_cache_rows(
    tmp_path, monkeypatch
):
    database, dashboard_routes = _setup_ops_db(tmp_path, monkeypatch)
    import routes.admin_routes as admin_routes

    now = int(time.time())
    url = "http://127.0.0.1:1200/gov/csrc/news"
    cfg = {
        "threshold": {"pass": 5},
        "feed_health": {"empty_degraded_after": 3},
        "feeds": [{"name": "证监会新闻", "url": url, "enabled": True}],
    }
    _patch_ops_cfg(monkeypatch, dashboard_routes, cfg)
    monkeypatch.setattr(admin_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(admin_routes, "check_api_key", lambda: True)

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE feed_health SET name=?, last_check=?, response_time_ms=?, "
            "success=?, fail=?, articles_total=?, articles_passed=?, avg_score=?, "
            "last_error=?, consecutive_empty_checks=?, last_nonempty_at=? WHERE url=?",
            (
                "证监会新闻",
                now,
                90,
                8,
                0,
                20,
                3,
                7.1,
                None,
                3,
                now - 3600,
                url,
            ),
        )
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, score, tag, logic, created_at, summary, "
            "feed_url, strategy_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://example.com/cached",
                "缓存文章",
                "缓存文章",
                8.0,
                "宏观",
                "",
                now,
                "",
                url,
                "strategy",
            ),
        )
        conn.commit()

    rows = dashboard_routes._get_health_from_news_cache()
    assert len(rows[0]) == 12
    assert rows[0][10:] == (3, now - 3600)

    from flask import Flask

    test_app = Flask(__name__)
    with test_app.test_request_context("/ops-feeds"):
        widget_response = dashboard_routes.ops_feeds_widget()
    with test_app.test_request_context("/health/feeds"):
        api_response = admin_routes.feed_health()

    widget_body = widget_response.get_data(as_text=True)
    api_body = json.loads(api_response.get_data(as_text=True))
    assert "连续空源" in widget_body
    assert "空源连击 3" in widget_body
    assert api_body[0]["data_state"] == "stale_empty"
    assert api_body[0]["data_degraded"] is True
    assert api_body[0]["consecutive_empty_checks"] == 3



def test_status_and_health_feeds_match_enabled_sources(tmp_path, monkeypatch):
    database, dashboard_routes = _setup_ops_db(tmp_path, monkeypatch)
    from app import app

    enabled_url = "http://127.0.0.1:1200/cls/telegraph/watch"
    disabled_url = "http://127.0.0.1:1200/cls/depth"
    cfg = {
        "threshold": {"pass": 5, "notify": 8, "similarity": 0.86},
        "generate_summary": True,
        "feeds": [
            {"name": "财联社深度", "url": disabled_url, "enabled": False},
            {"name": "财联社电报", "url": enabled_url, "enabled": True},
        ],
        "refresh": {},
    }
    import routes.admin_routes as admin_routes

    _patch_ops_cfg(monkeypatch, dashboard_routes, cfg)
    monkeypatch.setattr(admin_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(admin_routes, "check_api_key", lambda: True)
    # Patch DB.get_feed_health to return empty (isolated test db has no feed_health rows)
    monkeypatch.setattr(database.DB, "get_feed_health", lambda: [])

    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO feed_health (url, name, last_check, response_time_ms, success, fail, articles_total, articles_passed, avg_score, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (disabled_url, "财联社深度", int(time.time()), 100, 0, 9, 0, 0, None, "503"),
        )
        conn.commit()

    with app.test_client() as client:
        status_response = client.get("/status")
        health_response = client.get("/health/feeds")

    assert status_response.status_code == 200
    assert health_response.status_code == 200
    status_body = json.loads(status_response.get_data(as_text=True))
    health_body = json.loads(health_response.get_data(as_text=True))

    assert status_body["config"]["sources_count"] == 2
    assert len(status_body["feeds"]) == 1
    assert len(health_body) == 0  # DB.get_feed_health mocked to empty
    assert status_body["feeds"][0]["name"] == "财联社电报"


@pytest.mark.parametrize("path", ["/status", "/metrics"])
def test_sensitive_ops_endpoints_authenticate_before_database(path, monkeypatch):
    import routes.admin_routes as admin_routes
    from app import app

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: False)
    monkeypatch.setattr(
        admin_routes.DB,
        "get_stats",
        lambda: pytest.fail("database read ran before authentication"),
    )
    with app.test_client() as client:
        response = client.get(path)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_status_and_metrics_are_read_only_but_health_stays_public(monkeypatch):
    import routes.admin_routes as admin_routes
    import routes.dashboard_routes as dashboard_routes
    from app import app

    cfg = {
        "threshold": {"pass": 7, "notify": 9, "similarity": 0.86},
        "feeds": [],
        "generate_summary": False,
        "refresh": {},
    }
    staleness = {
        "last_full_refresh_at": None,
        "seconds_since_full_refresh": None,
        "staleness_threshold_seconds": 14400,
        "stale": False,
        "degraded": False,
        "last_attempt": {},
        "last_ingestion_at": None,
    }
    queue = {
        "pending": 0,
        "inflight": 0,
        "dead_letter": 0,
        "invalid_files": 0,
        "oldest_task_age_seconds": 0.0,
        "task_max_age_seconds": 1800,
        "scan_ok": True,
        "stale": False,
        "alert": False,
    }

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(
        admin_routes,
        "flush_stats",
        lambda _callback: pytest.fail("GET endpoint attempted a stats flush"),
    )
    monkeypatch.setattr(admin_routes.DB, "get_stats", lambda: None)
    monkeypatch.setattr(admin_routes.DB, "get_feed_health", lambda: [])
    monkeypatch.setattr(admin_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(admin_routes, "_staleness_info", lambda _cfg: staleness)
    monkeypatch.setattr(admin_routes, "is_refresh_job_running", lambda: False)
    monkeypatch.setattr(admin_routes, "_worker_queue_metrics", lambda: queue)
    monkeypatch.setattr(
        dashboard_routes, "_get_health_from_news_cache", lambda: []
    )

    with app.test_client() as client:
        status_response = client.get("/status")
        metrics_response = client.get("/metrics")

        monkeypatch.setattr(admin_routes, "check_api_key", lambda: False)
        health_response = client.get("/health")
        readiness_response = client.get("/readiness")

    assert status_response.status_code == 200
    assert metrics_response.status_code == 200
    assert status_response.headers["Cache-Control"] == "no-store"
    assert metrics_response.headers["Cache-Control"] == "no-store"
    metrics_body = metrics_response.get_data(as_text=True)
    assert "ai_filter_event_replay" not in metrics_body
    assert "ai_filter_event_mapping_current_contract" not in metrics_body
    assert health_response.status_code == 200
    assert readiness_response.status_code == 200


def test_empty_degraded_metric_counts_only_enabled_configured_sources(monkeypatch):
    import routes.admin_routes as admin_routes
    from flask import Flask

    now = int(time.time())
    active = "https://example.com/active"
    disabled = "https://example.com/disabled"
    removed = "https://example.com/removed"
    cfg = {
        "feed_health": {"empty_degraded_after": "3"},
        "feeds": [
            {"url": active, "name": "Active", "enabled": True},
            {"url": disabled, "name": "Disabled", "enabled": False},
        ],
    }
    rows = [
        (url, url, now, 10, 4, 0, 0, 0, None, None, 3, now - 60)
        for url in (active, disabled, removed)
    ]

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(admin_routes.DB, "get_stats", lambda: None)
    monkeypatch.setattr(admin_routes.DB, "get_feed_health", lambda: rows)
    monkeypatch.setattr(admin_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(
        admin_routes,
        "_staleness_info",
        lambda _cfg: {"stale": False, "seconds_since_full_refresh": 0},
    )
    monkeypatch.setattr(admin_routes, "is_refresh_job_running", lambda: False)
    monkeypatch.setattr(
        admin_routes,
        "_worker_queue_metrics",
        lambda: {
            "pending": 0,
            "inflight": 0,
            "dead_letter": 0,
            "invalid_files": 0,
            "oldest_task_age_seconds": 0,
            "task_max_age_seconds": 1800,
            "scan_ok": True,
            "stale": False,
            "alert": False,
        },
    )

    app = Flask(__name__)
    with app.test_request_context("/metrics"):
        response = admin_routes.metrics()

    assert "ai_filter_feed_empty_degraded_sources 1" in response.get_data(
        as_text=True
    )


def test_ops_feeds_is_short_cached_and_redacts_internal_details(monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    from flask import Flask

    internal_url = "http://rsshub:1200/private/source"
    internal_error = "upstream secret-token refused connection"
    calls = []
    monkeypatch.setattr(
        dashboard_routes,
        "_cfg",
        lambda: {
            "threshold": {"pass": 7},
            "feeds": [
                {"name": "内部源", "url": internal_url, "enabled": True}
            ],
        },
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_get_feed_health_snapshot",
        lambda: calls.append(1)
        or (
            [(internal_url, "内部源", int(time.time()), 20, 1, 0, 3, 2, 8.1, internal_error)],
            {internal_url: {"rate_text": "66.7%", "threshold": 7, "breakdown_text": "正式达标 2"}},
        ),
    )

    test_app = Flask(__name__)
    with test_app.test_request_context("/ops-feeds"):
        first = dashboard_routes.ops_feeds_widget()
    with test_app.test_request_context("/ops-feeds"):
        second = dashboard_routes.ops_feeds_widget()

    body = second.get_data(as_text=True)
    assert first.status_code == second.status_code == 200
    assert calls == [1]
    assert "max-age=60" in second.headers["Cache-Control"]
    assert "内部源" in body
    assert internal_url not in body
    assert internal_error not in body


def test_feed_widget_and_dashboard_share_no_sample_and_zero_rate_labels(
    tmp_path, monkeypatch
):
    from datetime import datetime
    from flask import Flask, render_template_string

    database, routes = _setup_ops_db(tmp_path, monkeypatch)
    feeds = [
        {"name": "无样本源", "url": "https://example.com/empty"},
        {"name": "<script>低分源</script>", "url": "https://example.com/low"},
    ]
    cfg = {"threshold": {"pass": 6}, "feeds": feeds}
    _patch_ops_cfg(monkeypatch, routes, cfg)
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache (link,title,score,score_path,feed_url,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("https://example.com/item", "普通新闻", 4, "llm", feeds[1]["url"], int(time.time())),
        )
        conn.commit()
    rows, details = routes._get_feed_health_snapshot()
    template = Path(routes.DASHBOARD_TEMPLATE).read_text(encoding="utf-8")
    section = template.split('<h2>📡 源健康状态</h2>', 1)[1].split(
        '<h2>📝 最近20条请求日志</h2>', 1
    )[0]
    app = Flask(__name__)
    with app.test_request_context("/ops-feeds"):
        widget = routes.ops_feeds_widget().get_data(as_text=True)
        dashboard = render_template_string(
            section, health_rows=rows, feed_metric_details=details, cfg=cfg,
            datetime=datetime, feed_data_health=routes.feed_data_health,
        )
    for body in (widget, dashboard):
        assert "新增有效信号率" in body
        assert "不是抓取成功率" in body
        assert "暂无24h样本" in body
        assert "0.0%" in body
        assert "低于阈值 1" in body
        assert "格式/字段中性 0" in body
        assert "来源依据中性 0" in body
        assert "&lt;script&gt;低分源&lt;/script&gt;" in body
        assert "<script>低分源</script>" not in body


def test_ops_summary_uses_short_application_cache(monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    from flask import Flask

    calls = []
    monkeypatch.setattr(dashboard_routes, "_cfg", lambda: {})
    monkeypatch.setattr(
        dashboard_routes,
        "_staleness_info",
        lambda _cfg: {
            "stale": False,
            "degraded": False,
            "last_full_refresh_at": int(time.time()),
        },
    )
    monkeypatch.setattr(dashboard_routes, "is_refresh_job_running", lambda: False)
    monkeypatch.setattr(
        dashboard_routes.DB,
        "get_stats",
        lambda: calls.append(1) or (10, 2, 3, 0, 0, 0, 4, 6, int(time.time())),
    )

    test_app = Flask(__name__)
    with test_app.test_request_context("/ops-summary"):
        first = dashboard_routes.ops_summary_widget()
    with test_app.test_request_context("/ops-summary"):
        second = dashboard_routes.ops_summary_widget()

    assert first.status_code == second.status_code == 200
    assert calls == [1]
    assert "max-age=30" in second.headers["Cache-Control"]
