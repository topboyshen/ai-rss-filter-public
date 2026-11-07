"""Browser destinations require explicit configuration, never example/Host URLs."""

from pathlib import Path
from types import SimpleNamespace

from flask import Flask
import pytest

from services.navigation_origins import dashboard_origin, news_home_origin
from services.neutral_records import render_neutral_records_entry
from tests import test_dashboard_browser_auth as browser_auth_tests
from tests.test_dashboard_browser_auth import API_KEY


dashboard_browser = browser_auth_tests.dashboard_browser


@pytest.mark.parametrize("value,expected", [
    ("https://dashboard.example.test", "https://dashboard.example.test"),
    ("https://dashboard.example.test/", "https://dashboard.example.test"),
    ("https://DASHBOARD.example.test:8443/", "https://dashboard.example.test:8443"),
    ("http://127.0.0.1:5000", "http://127.0.0.1:5000"),
    ("http://[::1]:5000/", "http://[::1]:5000"),
])
def test_explicit_origin_only(monkeypatch, value, expected):
    monkeypatch.setenv("AI_FILTER_PUBLIC_DASHBOARD_ORIGIN", value)
    monkeypatch.setenv("AI_FILTER_NEWS_HOME_ORIGIN", value)
    assert dashboard_origin() == news_home_origin() == expected


@pytest.mark.parametrize("value", [
    "", "https://example.invalid", "https://ai.example.invalid/", "https://EXAMPLE.INVALID.",
    "https://invalid", "//dashboard.example.test", "javascript:alert(1)",
    "ftp://dashboard.example.test", "https://user@dashboard.example.test",
    "https://user:secret@dashboard.example.test", "https://dashboard.example.test/path",
    "https://dashboard.example.test//", "https://dashboard.example.test?x=1",
    "https://dashboard.example.test?", "https://dashboard.example.test#x",
    "https://dashboard.example.test#", "https://dashboard.example.test:0",
    "https://dashboard.example.test:65536", "https://dashboard.example.test:bad",
    "https://dashboard.example.test:", "https://dashboard.example.test:0443",
    "https://dashboard.example.test\n", " https://dashboard.example.test",
    "https://dash\tboard.example.test", "https://dashboard.example.test\x00",
    "https://dashboard.example.test\x7f", 'https://dashboard.example.test\"onclick=bad',
    "https://dashboard.example.test\\evil", "https://%64ashboard.example.test",
    "https://[::1]evil", "https://[not-ip]", "https://[::1%25eth0]",
    "https://-bad.example.test", "https://bad..example.test", "https://例子.test",
    "https://" + "a" * 2048,
])
def test_invalid_origin_never_becomes_a_link(monkeypatch, value):
    from services import navigation_origins
    # os.environ itself rejects NUL; inject only this module's environment view
    # so the renderer's rejection is exercised without changing process state.
    monkeypatch.setattr(navigation_origins, "os", SimpleNamespace(environ={
        "AI_FILTER_PUBLIC_DASHBOARD_ORIGIN": value, "AI_FILTER_NEWS_HOME_ORIGIN": value,
    }))
    assert dashboard_origin() is None and news_home_origin() is None
    rendered = render_neutral_records_entry()
    assert "后台入口未配置" in rendered and "href=" not in rendered


def test_no_host_cors_or_internal_service_fallback(monkeypatch):
    monkeypatch.delenv("AI_FILTER_PUBLIC_DASHBOARD_ORIGIN", raising=False)
    monkeypatch.delenv("AI_FILTER_NEWS_HOME_ORIGIN", raising=False)
    monkeypatch.setenv("AI_FILTER_WEB_BASE_URL", "http://ai-filter-web:5000")
    monkeypatch.setenv("AI_FILTER_CORS_ALLOWED_ORIGINS", "https://unrelated.example.test")
    app = Flask(__name__)
    with app.test_request_context(base_url="https://attacker.example.test"):
        assert dashboard_origin() is None and news_home_origin() is None
        assert "href=" not in render_neutral_records_entry()


@pytest.fixture
def card_client(monkeypatch):
    import routes.event_radar_routes as routes
    import routes.security as security
    import services.ingestion_status as ingestion
    import routes.dashboard_routes as dashboard

    security._rate_limit_store.clear()
    monkeypatch.setattr(dashboard, "_dashboard_snapshot_response", lambda *a, **k: None)
    monkeypatch.setattr(ingestion, "read_refresh_coverage", lambda: ("complete", "20/20"))
    monkeypatch.setattr(routes, "load_home_event_digest", lambda: {
        "date": "2026-11-06", "cutoff_label": "已存截止", "data_state": "ok",
        "freshness": {"stale": False, "latest_label": "已存时间"},
        "highlights": [{"event_id": "fixed-event", "title": "真实样例标题", "block": "new"}],
        "corrections": [], "daily_url": "/dashboard/events/daily?date=2026-11-06",
        "weekly_url": "/dashboard/events/weekly?week=2026-11-02",
    })
    monkeypatch.setattr(routes, "load_event_stream", lambda **k: {
        "events": [{"event_id": "fixed-event", "title": "真实样例标题", "priority": 8,
                    "stocks": [{"name": "样例公司", "ticker": "600001", "relation_type": "direct"}]}],
        "pagination": {},
    })
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
    app.config["TESTING"] = True
    app.register_blueprint(routes.event_radar_bp)
    yield app.test_client()
    security._rate_limit_store.clear()


@pytest.mark.parametrize("path", ["/event-radar-card?limit=8", "/event-radar-card?limit=1"])
@pytest.mark.parametrize("configured", [True, False])
def test_actual_card_routes_bind_backend_origin_without_changing_content(card_client, monkeypatch, path, configured):
    origin = "https://dashboard.example.test:8443"
    monkeypatch.setenv("AI_FILTER_PUBLIC_DASHBOARD_ORIGIN", origin if configured else "https://example.invalid")
    response = card_client.get(path, base_url="https://unrelated.example.test")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "真实样例标题" in body
    assert "example.invalid" not in body and "unrelated.example.test" not in body
    if configured:
        assert f'href="{origin}/dashboard/events?event_id=fixed-event' in body
        assert "后台入口未配置" not in body
        if "limit=8" in path:
            assert f'href="{origin}/dashboard/events/daily?date=2026-11-06"' in body
            assert f'href="{origin}/dashboard/events/weekly?week=2026-11-02"' in body
    else:
        assert "后台入口未配置" in body and "href=" not in body


def test_neutral_widget_uses_its_explicit_backend_origin(monkeypatch):
    monkeypatch.setenv("AI_FILTER_PUBLIC_DASHBOARD_ORIGIN", "https://dashboard.example.test")
    monkeypatch.setenv("AI_FILTER_NEWS_HOME_ORIGIN", "https://news.example.test")
    rendered = render_neutral_records_entry()
    assert 'href="https://dashboard.example.test/dashboard/neutral-records"' in rendered
    assert "news.example.test" not in rendered
    assert "需后台登录" in rendered


@pytest.mark.parametrize("configured", [True, False])
def test_actual_authenticated_navigation_has_no_example_destination(dashboard_browser, monkeypatch, configured):
    client, _, _ = dashboard_browser
    monkeypatch.setenv("AI_FILTER_NEWS_HOME_ORIGIN", "https://news.example.test" if configured else "https://example.invalid")
    for path in ("/dashboard", "/dashboard/neutral-records"):
        response = client.get(path, headers={"X-API-Key": API_KEY}, base_url="https://unrelated.example.test")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "https://example.invalid" not in body and "https://unrelated.example.test" not in body
        assert 'href="/dashboard' in body
        if configured:
            assert 'href="https://news.example.test/quality"' in body
            if path == "/dashboard":
                assert 'href="https://news.example.test/"' in body
        else:
            assert "模型质量入口未配置" in body
            if path == "/dashboard":
                assert "新闻首页入口未配置" in body
