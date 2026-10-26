from flask import Flask
import pytest



@pytest.fixture(autouse=True)
def _reset_shared_rate_limiter():
    """Widget requests share one process-wide limiter across test modules."""
    import routes.security as security

    security._rate_limit_store.clear()
    yield
    security._rate_limit_store.clear()

def _snapshot_client(monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    import routes.event_radar_routes as radar_routes
    import services.dashboard_snapshots as snapshots
    import sse_engine

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(
        snapshots,
        "read_dashboard_snapshot",
        lambda card, expected_contract=None: {
            "card": card,
            "html": f"<div>snapshot:{card}</div>",
            "generated_at": _fresh_snapshot_generated_at(),
        },
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    app.register_blueprint(radar_routes.event_radar_bp)
    app.add_url_rule(
        "/glance-sse",
        "snapshot_test_glance_sse",
        sse_engine.glance_sse_widget,
        methods=["GET"],
    )
    return app.test_client(), dashboard_routes, radar_routes, sse_engine


def test_homepage_snapshot_routes_never_run_live_builders(monkeypatch):
    client, dashboard_routes, radar_routes, sse_engine = _snapshot_client(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("production Web homepage must not compute live")

    monkeypatch.setattr(dashboard_routes, "_get_briefing_articles", forbidden)
    monkeypatch.setattr(dashboard_routes, "_backtest_cache_contract", forbidden)
    monkeypatch.setattr(dashboard_routes, "run_score_backtest", forbidden)
    monkeypatch.setattr(dashboard_routes, "run_mapping_validation", forbidden)
    monkeypatch.setattr(radar_routes, "load_event_stream", forbidden)
    monkeypatch.setattr(sse_engine, "_load_from_db", forbidden)

    expected = {
        "/briefing-card?min_score=7&limit=12": "briefing",
        "/glance-sse": "realtime",
        "/event-radar-card?limit=8": "event_radar",
        "/score-backtest?days=90&view=compact": "model_quality",
    }
    for path, card in expected.items():
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["X-Card-Snapshot-State"] == "ready"
        assert f"snapshot:{card}" in response.get_data(as_text=True)


def test_missing_homepage_snapshot_is_fast_200_placeholder(monkeypatch):
    client, _dashboard_routes, _radar_routes, _sse_engine = _snapshot_client(
        monkeypatch
    )
    import services.dashboard_snapshots as snapshots

    monkeypatch.setattr(
        snapshots,
        "read_dashboard_snapshot",
        lambda *_args, **_kwargs: None,
    )

    response = client.get("/score-backtest?days=90&view=compact")
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "unavailable"
    assert "后台快照正在生成" in response.get_data(as_text=True)


def test_non_homepage_briefing_profile_keeps_existing_behavior(monkeypatch):
    client, dashboard_routes, _radar_routes, _sse_engine = _snapshot_client(
        monkeypatch
    )
    calls = []
    monkeypatch.setattr(
        dashboard_routes,
        "_get_briefing_articles",
        lambda **kwargs: calls.append(kwargs) or [],
    )

    response = client.get("/briefing-card?hours=48")
    assert response.status_code == 200
    assert calls and calls[0]["hours"] == 48
    assert "X-Card-Snapshot-State" not in response.headers


def test_invalid_radar_query_is_rejected_before_snapshot(monkeypatch):
    client, _dashboard_routes, _radar_routes, _sse_engine = _snapshot_client(
        monkeypatch
    )
    response = client.get("/event-radar-card?limit=8&unexpected=1")
    assert response.status_code == 400


def _feedback_snapshot_client(tmp_path, monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    import services.dashboard_snapshots as snapshots
    from routes.browser_auth import DashboardAuth

    directory = tmp_path / "snapshots"
    database = tmp_path / "cache.db"
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(directory))
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>quality v1</section>",
        generated_at=_fresh_snapshot_generated_at(),
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=database,
    )
    original = (directory / "model_quality.json").read_bytes()
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(
        dashboard_routes,
        "get_dashboard_auth",
        lambda: DashboardAuth(kind="header"),
    )
    monkeypatch.setattr(dashboard_routes, "verify_session_csrf", lambda _auth: True)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    return app.test_client(), dashboard_routes, directory, original


def test_feedback_contract_change_invalidates_stale_snapshot_and_enqueues(
    tmp_path, monkeypatch
):
    client, dashboard_routes, directory, original = _feedback_snapshot_client(
        tmp_path, monkeypatch
    )
    versions = iter(("score:v1", "score:v2"))
    writes = []
    enqueued = []
    monkeypatch.setattr(
        dashboard_routes, "_feedback_strategy_version", lambda: next(versions)
    )
    monkeypatch.setattr(
        dashboard_routes.DB,
        "add_article_feedback",
        lambda *args: writes.append(args),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_enqueue_dashboard_snapshot_refresh",
        lambda token: enqueued.append(token) or "stored",
    )

    response = client.post(
        "/feedback", data={"link": "https://example.com/a", "label": "good"}
    )
    assert response.status_code == 303
    assert writes and len(enqueued) == 1
    assert (directory / "model_quality.json").read_bytes() == original
    card = client.get("/score-backtest?days=90&view=compact")
    assert card.status_code == 200
    assert card.headers["X-Card-Snapshot-State"] == "unavailable"


def test_failed_feedback_restores_old_ready_snapshot(tmp_path, monkeypatch):
    client, dashboard_routes, directory, original = _feedback_snapshot_client(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        dashboard_routes, "_feedback_strategy_version", lambda: "score:v1"
    )

    def fail_write(*_args):
        raise RuntimeError("feedback write failed")

    monkeypatch.setattr(dashboard_routes.DB, "add_article_feedback", fail_write)
    monkeypatch.setattr(
        dashboard_routes,
        "_enqueue_dashboard_snapshot_refresh",
        lambda _token: pytest.fail("failed feedback must not enqueue"),
    )

    with pytest.raises(RuntimeError, match="feedback write failed"):
        client.post(
            "/feedback",
            data={"link": "https://example.com/a", "label": "good"},
        )
    assert (directory / "model_quality.json").read_bytes() == original
    card = client.get("/score-backtest?days=90&view=compact")
    assert card.status_code == 200
    assert card.headers["X-Card-Snapshot-State"] == "ready"
    assert "quality v1" in card.get_data(as_text=True)
def _fresh_snapshot_generated_at() -> str:
    """A timestamp young enough to read as ready without env overrides."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )

def test_snapshot_older_than_max_age_is_marked_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import routes.dashboard_routes as dashboard_routes
    import services.dashboard_snapshots as snapshots

    stale_generated_at = (
        datetime.now(timezone.utc) - timedelta(days=37)
    ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(
        snapshots,
        "read_dashboard_snapshot",
        lambda card, expected_contract=None: {
            "card": card,
            "html": "<div>snapshot:model_quality</div>",
            "generated_at": stale_generated_at,
        },
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    client = app.test_client()
    response = client.get("/score-backtest?days=90&view=compact")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "数据已过期" in body
    assert "当前数据状态未知" in body
    assert "snapshot:model_quality" in body
    assert response.headers["X-Card-Snapshot-State"] == "stale"
def _write_real_model_quality_snapshot(tmp_path, generated_at: str) -> None:
    import services.dashboard_snapshots as snapshots

    directory = tmp_path / "snapshots"
    snapshots.write_dashboard_snapshot(
        "model_quality",
        "<section>stale body</section>",
        generated_at=generated_at,
        strategy_version="score:v1",
        mapping_version="asset-map:v1",
        data_version="1",
        snapshot_dir=directory,
        db_path=tmp_path / "cache.db",
    )


def test_real_stale_snapshot_marks_full_and_compact_view_stale(tmp_path, monkeypatch):
    """Real reader path: the /quality full view and the compact card share it."""
    from datetime import datetime, timedelta, timezone

    import routes.dashboard_routes as dashboard_routes

    stale_generated_at = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv(
        "AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots")
    )
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    _write_real_model_quality_snapshot(tmp_path, stale_generated_at)
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    client = app.test_client()
    for path in ("/score-backtest?days=90", "/score-backtest?days=90&view=compact"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "数据已过期" in body
        assert "已超过预期刷新间隔" in body
        assert "当前数据状态未知" in body
        assert "stale body" in body
        assert response.headers["X-Card-Snapshot-State"] == "stale"


def test_snapshot_max_age_rejects_non_finite_and_non_positive(monkeypatch):
    import math

    import routes.dashboard_routes as dashboard_routes

    monkeypatch.delenv("AI_FILTER_DASHBOARD_SNAPSHOT_MAX_AGE_SECONDS", raising=False)
    baseline = dashboard_routes._snapshot_max_age_seconds()
    assert math.isfinite(baseline) and baseline >= 60.0
    for bad in ("inf", "nan", "-5", "0", "abc", "1e400"):
        monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_MAX_AGE_SECONDS", bad)
        assert dashboard_routes._snapshot_max_age_seconds() == baseline


def test_snapshot_max_age_env_override_respected(monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_MAX_AGE_SECONDS", "3600")
    assert dashboard_routes._snapshot_max_age_seconds() == 3600.0
def _craft_future_snapshot_file(tmp_path: str, monkeypatch) -> None:
    """Write an envelope whose timestamps sit in the future with a valid digest.

    The writer refuses future stamps and the reader rejects
    generated > published, so a future stamped artifact can only appear by
    writing the file directly; the response layer must still not read it as
    ready.
    """
    import hashlib
    import json as json_module
    from datetime import datetime, timedelta, timezone

    import services.dashboard_snapshots as snapshots

    html = "<section>future-stamped body</section>"
    now = datetime.now(timezone.utc)
    envelope = {
        "schema": snapshots.SNAPSHOT_SCHEMA,
        "card": "model_quality",
        "generated_at": (now + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        ),
        "published_at": (now + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        ),
        "content_type": snapshots.SNAPSHOT_CONTENT_TYPE,
        "release_version": "my-ai-filter:test",
        "html": html,
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
    }
    directory = tmp_path / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    snapshot_file = directory / "model_quality.json"
    snapshot_file.write_text(json_module.dumps(envelope), encoding="utf-8")
    snapshot_file.chmod(0o600)


def test_real_future_stamped_snapshot_never_reads_ready(tmp_path, monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    _craft_future_snapshot_file(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv(
        "AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots")
    )
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:test")
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    client = app.test_client()
    response = client.get("/score-backtest?days=90&view=compact")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "快照时间异常" in body
    assert "当前数据状态未知" in body
    assert "future-stamped body" in body
    assert response.headers["X-Card-Snapshot-State"] == "stale"
_ENTRY_MARKED = (
    '<span class="quality-card-entry">'
    '<a href="/quality" style="color:inherit;">完整统计 →</a></span>'
)
_FULL_DETAILS = (
    '<details class="quality-full-statistics">完整市场指标</details>'
)


def _entry_snapshot_client(monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    import services.dashboard_snapshots as snapshots

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(
        snapshots,
        "read_dashboard_snapshot",
        lambda card, expected_contract=None: {
            "card": card,
            "html": f"<section>card body</section>{_ENTRY_MARKED}{_FULL_DETAILS}",
            "generated_at": _fresh_snapshot_generated_at(),
        },
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    return app.test_client()


def test_full_view_uses_inline_entry_and_drops_the_self_link(monkeypatch):
    """The /quality full page must not offer a link back to itself."""
    client = _entry_snapshot_client(monkeypatch)
    response = client.get("/score-backtest?days=90")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "完整统计 →" not in body
    assert 'href="/quality"' not in body
    assert "完整市场指标" in body
    assert "card body" in body
    assert response.headers["X-Card-Snapshot-State"] == "ready"


def test_compact_view_keeps_the_quality_link(monkeypatch):
    client = _entry_snapshot_client(monkeypatch)
    response = client.get("/score-backtest?days=90&view=compact")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "完整统计 →" in body
    assert 'href="/quality"' in body
    assert response.headers["X-Card-Snapshot-State"] == "ready"


def test_compact_renderer_marks_the_card_entry():
    from services.backtest import render_backtest_compact_html

    html = render_backtest_compact_html({})
    assert '<span class="quality-card-entry">' in html
    assert "完整统计 →" in html


def test_non_default_request_still_bypasses_the_snapshot(monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    import services.dashboard_snapshots as snapshots

    calls = {"snapshot": 0}

    def counting_read(*_args, **_kwargs):
        calls["snapshot"] += 1
        return None

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(snapshots, "read_dashboard_snapshot", counting_read)
    monkeypatch.setattr(dashboard_routes, "_backtest_cache_contract", lambda: None)
    monkeypatch.setattr(
        dashboard_routes, "run_score_backtest", lambda **kwargs: {"days": kwargs.get("days")}
    )
    monkeypatch.setattr(
        dashboard_routes, "run_mapping_validation", lambda **kwargs: None
    )
    monkeypatch.setattr(
        dashboard_routes,
        "_computed_backtest_contract",
        lambda result: ("score:v1", "map:v1"),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "render_backtest_html",
        lambda result, mapping: "<section>live full</section>",
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(dashboard_routes.dash_bp)
    response = app.test_client().get("/score-backtest?days=30")
    assert response.status_code == 200
    assert "live full" in response.get_data(as_text=True)
    assert calls["snapshot"] == 0
