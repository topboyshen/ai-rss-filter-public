import time


def test_persistent_score_stats_report_safe_neutral_without_changing_fallback(
    tmp_path, monkeypatch
):
    import db.database as database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cache.db"))
    database.init_db()
    now = int(time.time())
    rows = [
        ("fallback-1", "fallback_local", now),
        ("fallback-2", "fallback_local", now),
        ("llm-1", "llm", now),
        ("stage2-1", "stage2", now),
        ("neutral-stage1", "llm_safe_neutral_contract", now),
        ("neutral-stage2", "stage2_safe_neutral_contract", now),
        ("grounding-stage1", "llm_safe_neutral_grounding", now),
        ("grounding-stage2", "stage2_safe_neutral_grounding", now),
        ("similarity-1", "similarity", now),
        ("fast-pass-1", "fast_pass", now),
        ("local-reject-1", "local_reject", now),
        ("old-fallback", "fallback_local", now - 25 * 3600),
    ]
    with database.get_connection() as conn:
        for link, score_path, created_at in rows:
            conn.execute(
                "INSERT INTO news_cache "
                "(link, title, raw_title, score, tag, logic, created_at, summary, "
                "feed_url, strategy_version, score_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    link,
                    link,
                    link,
                    7.0,
                    "test",
                    "test",
                    created_at,
                    "",
                    "feed",
                    "strategy",
                    score_path,
                ),
            )
        conn.commit()

    stats = database.get_score_fallback_stats(hours=24)

    assert stats == {
        "window_hours": 24,
        "fallback_local": 2,
        "model_scored": 2,
        "eligible": 4,
        "fallback_ratio": 0.5,
        "safe_neutral": 4,
        "safe_neutral_denominator": 8,
        "safe_neutral_ratio": 0.5,
    }


def test_score_fallback_alert_has_minimum_sample_guard(monkeypatch):
    import routes.admin_routes as admin_routes

    monkeypatch.setattr(
        admin_routes.DB,
        "get_score_fallback_stats",
        lambda hours=24: {
            "window_hours": hours,
            "fallback_local": 12,
            "model_scored": 8,
            "eligible": 20,
            "fallback_ratio": 0.6,
        },
    )
    assert admin_routes._score_fallback_metrics()["alert"] is True

    monkeypatch.setattr(
        admin_routes.DB,
        "get_score_fallback_stats",
        lambda hours=24: {
            "window_hours": hours,
            "fallback_local": 9,
            "model_scored": 1,
            "eligible": 10,
            "fallback_ratio": 0.9,
        },
    )
    assert admin_routes._score_fallback_metrics()["alert"] is False


def test_authenticated_metrics_export_fallback_health(monkeypatch):
    import routes.admin_routes as admin_routes
    from flask import Flask

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(admin_routes.DB, "get_stats", lambda: None)
    monkeypatch.setattr(admin_routes.DB, "get_feed_health", lambda: [])
    monkeypatch.setattr(admin_routes, "_cfg", lambda: {})
    monkeypatch.setattr(
        admin_routes,
        "_staleness_info",
        lambda _cfg: {"stale": False, "degraded": False, "last_attempt": {}},
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
            "stale": False,
            "alert": False,
        },
    )
    monkeypatch.setattr(
        admin_routes,
        "_cold_filter_metrics",
        lambda: {"limit": 2, "inflight": 0, "rejections_total": 0},
    )
    monkeypatch.setattr(
        admin_routes,
        "_score_fallback_metrics",
        lambda: {
            "fallback_local": 12,
            "model_scored": 8,
            "eligible": 20,
            "fallback_ratio": 0.6,
            "safe_neutral": 6,
            "safe_neutral_denominator": 26,
            "safe_neutral_ratio": 6 / 26,
            "safe_neutral_2h": 2,
            "safe_neutral_denominator_2h": 8,
            "safe_neutral_ratio_2h": 0.25,
            "alert": True,
        },
    )

    app = Flask(__name__)
    with app.test_request_context("/metrics"):
        response = admin_routes.metrics()
    body = response.get_data(as_text=True)

    assert "ai_filter_score_fallback_local_24h 12" in body
    assert "ai_filter_score_model_scored_24h 8" in body
    assert "ai_filter_score_fallback_ratio_24h 0.600000" in body
    assert "ai_filter_score_safe_neutral_count_2h 2" in body
    assert "ai_filter_score_safe_neutral_denominator_2h 8" in body
    assert "ai_filter_score_safe_neutral_ratio_2h 0.250000" in body
    assert "ai_filter_score_safe_neutral_count_24h 6" in body
    assert "ai_filter_score_safe_neutral_denominator_24h 26" in body
    assert "ai_filter_score_safe_neutral_ratio_24h 0.230769" in body
    assert "ai_filter_score_fallback_alert 1" in body


def test_unauthenticated_process_local_metrics_route_is_removed(monkeypatch):
    import routes.admin_routes as admin_routes
    from app import app

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: False)
    with app.test_client() as client:
        removed = client.get("/prometheus-metrics")
        protected = client.get("/metrics")

    assert removed.status_code == 404
    assert protected.status_code == 401
