"""Real Flask request hooks and disk fence; synthetic requests, no model calls."""
import hashlib
import hmac
import io
import json
import time
from pathlib import Path

import pytest
from flask import Flask, Response

from services import admission_fence as fence_module
from services import admission_http
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401
from tests.test_webhook_durable_delivery import delivery, raw_body, signed_post  # noqa: F401


@pytest.fixture
def fence(tmp_path, monkeypatch):
    root = tmp_path / "fence"
    now = int(time.time())
    binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64,
               "strategy": "synthetic-http-contract", "source_sha256": fence_module.source_sha256(),
               "deployment_nonce": "c" * 32}
    fence_module.provision(root, binding, {"not_before": now - 1, "expires_at": now + 600,
                                          "max_admissions": 100, "max_bytes": 1024 * 1024})
    monkeypatch.setenv("AI_FILTER_ADMISSION_MODE", "enforced")
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(root))
    monkeypatch.setenv("AI_FILTER_ADMISSION_BINDING", json.dumps(binding))
    return fence_module.Fence(root, binding)


def open_fence(fence):
    return fence.open_trial("1" * 64, 0, "synthetic HTTP test")


def draining_fence(fence):
    open_fence(fence)
    return fence.stop_new("2" * 64, 1, "synthetic HTTP stop")


@pytest.fixture
def application(monkeypatch):
    import app as app_module

    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
    monkeypatch.setenv("AI_FILTER_ROLE", "all")
    monkeypatch.setenv("MINIFLUX_WEBHOOK_SECRET", "synthetic-admission-signing-secret")
    return app_module


def replace_view(application, monkeypatch, endpoint, result="synthetic", status=200):
    calls = []

    def view(**_kwargs):
        calls.append(fence_module.current_kind(default="intake"))
        return result, status

    monkeypatch.setitem(application.app.view_functions, endpoint, view)
    return calls


@pytest.mark.parametrize("path,endpoint", [
    ("/health", "admin.health"),
    ("/dashboard/events", "event_radar.event_radar_page"),
    ("/dashboard/events/daily?date=2026-10-22", "event_radar.event_daily_report_page"),
    ("/dashboard/events/weekly?week=2026-10-19", "event_radar.event_weekly_report_page"),
])
def test_exact_read_only_routes_remain_available_while_frozen(fence, application, monkeypatch, path, endpoint):
    calls = replace_view(application, monkeypatch, endpoint)
    with application.app.test_client() as client:
        assert client.get(path).status_code == 200
        assert client.head(path).status_code == 200
    assert len(calls) == 2
    assert fence.snapshot()["intake_count"] == 0


@pytest.mark.parametrize("method,path,endpoint", [
    ("get", "/filter?url=https://example.invalid/feed", "feed.filter_rss"),
    ("get", "/filter/multi?urls=https://example.invalid/feed", "feed.filter_multi"),
    ("post", "/feedback", "dash.feedback"), ("post", "/refresh", "admin.refresh"),
    ("post", "/stats/reset", "admin.reset_stats_endpoint"), ("post", "/config/reload", "admin.reload_config"),
    ("get", "/briefing?llm=true", "dash.briefing"),
    ("get", "/briefing-card?llm=true", "dash.briefing_card_widget"),
    ("get", "/briefing?llm=false&llm=true", "dash.briefing"),
    ("get", "/briefing?llm=false", "dash.briefing"),
    ("post", "/briefing?push=true", "dash.briefing"),
    ("get", "/briefing?push=true", "dash.briefing"),
    ("post", "/dashboard/login", "browser_auth.dashboard_login_submit"),
    ("post", "/dashboard/logout", "browser_auth.dashboard_logout"),
])
def test_producer_routes_refuse_before_business_while_draining(fence, application, monkeypatch, method, path, endpoint):
    draining_fence(fence)
    calls = replace_view(application, monkeypatch, endpoint)
    with application.app.test_client() as client:
        response = getattr(client, method)(path)
    assert response.status_code == 503
    assert response.get_json()["durable"] is False
    assert calls == []
    assert fence.snapshot()["intake_count"] == 0


@pytest.mark.parametrize("method,path", [("get", "/future-producer"), ("post", "/future-producer"),
                                         ("options", "/future-producer"), ("post", "/health")])
def test_unknown_methods_and_paths_are_not_read_exemptions(fence, application, method, path):
    with application.app.test_client() as client:
        assert getattr(client, method)(path).status_code == 503


def test_known_path_with_unrecognised_endpoint_is_not_exempt(fence):
    isolated = Flask("synthetic-unknown-health")
    isolated.add_url_rule("/health", "future_write", lambda: "must not run")
    admission_http.install_http_admission(isolated)
    assert isolated.test_client().get("/health").status_code == 503


def test_unsigned_webhook_keeps_original_401_before_any_fence_access(fence, application, monkeypatch):
    monkeypatch.setattr(admission_http.admission_fence, "reserve_lease", lambda *_a, **_k: pytest.fail("before auth"))
    with application.app.test_client() as client:
        for path in ("/webhook", "/webhook/receipt"):
            assert client.post(path, data=b"synthetic", headers={"X-Miniflux-Signature": "0" * 64}).status_code == 401


def signed_headers(body):
    return {"Content-Type": "application/json", "X-Miniflux-Signature": hmac.new(
        b"synthetic-admission-signing-secret", body, hashlib.sha256).hexdigest()}


def test_receipt_recovery_is_drain_not_read_only_or_new_intake(fence, application, monkeypatch):
    draining_fence(fence)
    calls = replace_view(application, monkeypatch, "webhook.webhook_receipt")
    body = b'{"synthetic":true}'
    with application.app.test_client() as client:
        assert client.post("/webhook/receipt", data=body, headers=signed_headers(body)).status_code == 200
        assert client.post("/webhook", data=body, headers=signed_headers(body)).status_code == 503
    assert calls == ["drain"]
    assert fence.snapshot()["intake_count"] == 0
    assert fence.snapshot()["active"] == 0
    fence.freeze("3" * 64, 2, "synthetic complete")
    with application.app.test_client() as client:
        assert client.post("/webhook/receipt", data=body, headers=signed_headers(body)).status_code == 503


def test_request_holds_lease_through_sync_handler_and_reuses_nested_guard(fence, application, monkeypatch):
    open_fence(fence)
    seen = []

    def view():
        with fence_module.admission("nested_handler", kind="inherit"):
            seen.append(fence.snapshot())
        return "stored", 202

    monkeypatch.setitem(application.app.view_functions, "admin.refresh", view)
    with application.app.test_client() as client:
        assert client.post("/refresh", data=b"abc").status_code == 202
    assert seen[0]["active"] == 1
    assert seen[0]["intake_count"] == 1
    assert seen[0]["charged_bytes"] == 3
    assert fence.snapshot()["active"] == 0
    assert fence.snapshot()["uncertain"] == 0


def test_unauthenticated_business_request_may_consume_admission_not_news_quota(fence, application, monkeypatch):
    import routes.admin_routes as admin

    open_fence(fence)
    monkeypatch.setattr(admin, "check_api_key", lambda: False)
    with application.app.test_client() as client:
        assert client.post("/refresh").status_code == 401
    assert fence.snapshot()["intake_count"] == 1
    assert fence.snapshot()["active"] == 0


@pytest.mark.parametrize("mode", ["raise", "500", "stream"])
def test_uncertain_response_preserves_activity_evidence(fence, application, monkeypatch, mode):
    open_fence(fence)
    produced = []

    def view():
        if mode == "raise":
            raise RuntimeError("synthetic handler failure")
        if mode == "500":
            return "synthetic failure", 500

        def body():
            produced.append("must not execute after teardown")
            yield b"late work"

        return Response(body())

    monkeypatch.setitem(application.app.view_functions, "admin.refresh", view)
    with application.app.test_client() as client:
        assert client.post("/refresh").status_code in {500, 503}
    assert produced == []
    assert fence.snapshot()["uncertain"] == 1
    assert len(list((fence.root / "leases").glob("*.json"))) == 1


def test_finish_fsync_error_cannot_return_original_202(fence, application, monkeypatch):
    open_fence(fence)
    replace_view(application, monkeypatch, "admin.refresh", status=202)
    original = fence_module._write

    def fail_finish(path, value):
        if Path(path).parent.name == "finishes":
            raise OSError("synthetic finish fsync failure")
        return original(path, value)

    monkeypatch.setattr(fence_module, "_write", fail_finish)
    with application.app.test_client() as client:
        assert client.post("/refresh").status_code == 503
    assert fence.snapshot()["active"] == 1
    assert list((fence.root / "leases").glob("*.json"))


def test_startup_initialization_has_no_frozen_bypass(fence, application, monkeypatch):
    calls = []
    monkeypatch.setattr(application, "_init_db", lambda: calls.append("init"))
    for entrypoint in (application.initialize_app, application.init_db):
        with pytest.raises(fence_module.AdmissionClosed):
            entrypoint()
    assert calls == []
    open_fence(fence)
    application.initialize_app()
    assert calls == ["init"]
    assert fence.snapshot()["intake_count"] == 1


def test_default_disabled_does_not_create_fence_or_change_streaming(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_FILTER_ADMISSION_MODE", raising=False)
    monkeypatch.setenv("AI_FILTER_ADMISSION_ROOT", str(tmp_path / "never-created"))
    app = Flask("synthetic-disabled")
    app.add_url_rule("/unknown", "stream", lambda: Response(iter([b"unchanged"])), methods=["POST"])
    admission_http.install_http_admission(app)
    assert app.test_client().post("/unknown").data == b"unchanged"
    assert not (tmp_path / "never-created").exists()


def test_read_exemptions_do_not_bypass_real_page_auth(fence, application, monkeypatch):
    import routes.event_radar_routes as radar

    monkeypatch.setattr(radar, "get_dashboard_auth", lambda: None)
    monkeypatch.setattr(radar, "load_weekly_report", lambda **_kw: pytest.fail("auth bypass"))
    with application.app.test_client() as client:
        response = client.get("/dashboard/events/weekly", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert fence.snapshot()["intake_count"] == 0


@pytest.mark.parametrize("environment", [
    {"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
    {"CONTENT_LENGTH": "", "HTTP_TRANSFER_ENCODING": "chunked"},
    {"CONTENT_LENGTH": "4", "HTTP_TRANSFER_ENCODING": "chunked"},
    {"CONTENT_LENGTH": "not-a-length"},
])
def test_unknown_or_ambiguous_body_length_is_not_zero_budget(fence, application, monkeypatch, environment):
    open_fence(fence)
    calls = replace_view(application, monkeypatch, "admin.refresh")

    class Unreadable(io.BytesIO):
        def read(self, *_args):
            pytest.fail("admission must not consume unknown body")

        def readinto(self, *_args):
            pytest.fail("admission must not consume unknown body")

    with application.app.test_client() as client:
        response = client.post("/refresh", environ_overrides={"wsgi.input": Unreadable(b"synthetic"), **environment})
    assert response.status_code == 503
    assert calls == []
    assert fence.snapshot()["intake_count"] == 0


def test_declared_body_budget_rejects_without_reading_body(fence, application, monkeypatch):
    open_fence(fence)
    calls = replace_view(application, monkeypatch, "admin.refresh")
    with application.app.test_client() as client:
        response = client.post("/refresh", environ_overrides={"CONTENT_LENGTH": str(1024 * 1024 + 1)})
    assert response.status_code == 503
    assert calls == []
    assert fence.snapshot()["intake_count"] == 0


def test_real_signed_receiver_receipt_and_drain_use_same_body_without_model(fence, delivery):  # noqa: F811
    open_fence(fence)
    body = raw_body(243)
    accepted = signed_post(delivery, body)
    assert accepted.status_code == 202
    assert accepted.get_json()["durable"] is True
    assert accepted.get_json()["delivery_id"] == hashlib.sha256(body).hexdigest()
    assert len(list(delivery.webhook._webhook_spool_dir.glob("*.json"))) == 1
    assert fence.snapshot()["intake_count"] == 1
    assert fence.snapshot()["charged_bytes"] == len(body)
    fence.stop_new("2" * 64, 1, "real receiver synthetic drain")
    assert signed_post(delivery, body).status_code == 503
    queried = signed_post(delivery, body, "/webhook/receipt")
    assert queried.status_code == 200
    assert queried.get_json()["durable"] is True
    assert queried.get_json()["delivery_id"] == accepted.get_json()["delivery_id"]
    assert len(list(delivery.webhook._webhook_spool_dir.glob("*.json"))) == 1
    assert delivery.calls == []
    assert fence.snapshot()["active"] == 0
    assert fence.snapshot()["uncertain"] == 0


@pytest.mark.parametrize("path,module_name", [("/stats", "routes.admin_routes"),
                                             ("/dashboard", "routes.dashboard_routes")])
def test_get_stats_flushing_routes_do_not_write_while_frozen(fence, application, monkeypatch, path, module_name):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "check_api_key", lambda: True)
    if hasattr(module, "get_dashboard_auth"):
        monkeypatch.setattr(module, "get_dashboard_auth", lambda: object())
    writes = []

    def forbidden_flush(_callback):
        writes.append("would flush stats to SQLite")
        raise RuntimeError("synthetic unexpected business write")

    monkeypatch.setattr(module, "flush_stats", forbidden_flush)
    with application.app.test_client() as client:
        assert client.get(path).status_code == 503
    assert writes == []
    assert fence.snapshot()["intake_count"] == 0


@pytest.mark.parametrize("path", ["/status", "/readiness", "/metrics", "/logs", "/health/feeds",
                                  "/", "/glance", "/glance-sse", "/briefing", "/briefing-card",
                                  "/event-radar-card", "/ops-summary", "/ops-feeds", "/score-backtest",
                                  "/feed/json", "/events/rss", "/cached_feed",
                                  "/dashboard/stocks/SSE/600000", "/integration/v1/signals",
                                  "/score-quality", "/dashboard/neutral-records"])
def test_potential_write_connections_are_not_exempt(fence, application, monkeypatch, path):
    import sqlite3

    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_kw: pytest.fail("SQLite opened before admission"))
    with application.app.test_client() as client:
        response = client.get(path, headers={"X-API-Key": "synthetic"})
    assert response.status_code == 503
    assert response.get_json()["error"] == "admission_unavailable"
    assert fence.snapshot()["intake_count"] == 0


@pytest.mark.parametrize("path", ["/dashboard/events", "/dashboard/events/daily?date=2026-10-22",
                                  "/dashboard/events/weekly?week=2026-10-19"])
def test_actual_exempt_report_handlers_use_existing_read_only_database(fence, application, monkeypatch, path):
    import sqlite3
    import db.database as database
    import routes.event_radar_routes as radar

    monkeypatch.setattr(radar, "get_dashboard_auth", lambda: object())
    original_connect = sqlite3.connect
    connections = []

    def connect(target, *args, **kwargs):
        connections.append((target, kwargs.get("uri")))
        return original_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    missing = Path(database.DB_PATH)
    assert not missing.exists()
    with application.app.test_client() as client:
        response = client.get(path)
    # The real reader reports missing evidence; admission itself did not reject
    # or create a fresh SQLite database. No model/view function was substituted.
    assert response.status_code == 503
    assert connections
    assert all(str(target).startswith("file:") and "mode=ro" in str(target) and uri is True
               for target, uri in connections)
    assert not missing.exists()
    assert fence.snapshot()["intake_count"] == 0


def test_real_health_never_initializes_or_connects_database(fence, application, monkeypatch):
    import sqlite3

    monkeypatch.setattr(application, "_init_db", lambda: pytest.fail("lazy startup"))
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_kw: pytest.fail("health DB write"))
    with application.app.test_client() as client:
        assert client.get("/health").status_code == 200
    assert fence.snapshot()["intake_count"] == 0
