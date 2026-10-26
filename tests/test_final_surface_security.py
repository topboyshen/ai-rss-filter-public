import json

import pytest
from flask import Flask


XSS = '<a href="https://evil.example">fake</a>'


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _telegram_cfg():
    return {
        "telegram": {
            "enabled": True,
            "bot_token": "token",
            "chat_id": "chat",
        }
    }


def test_internal_jin10_feed_bypasses_same_process_http(monkeypatch):
    import core.processor as processor
    import services.jin10_feed as jin10_feed

    captured = {}
    monkeypatch.setattr(
        processor,
        "fetch_feed",
        lambda *_args, **_kwargs: pytest.fail("internal Jin10 must not use HTTP self-call"),
    )
    monkeypatch.setattr(
        jin10_feed,
        "build_jin10_rss",
        lambda request_url, cfg, limit: captured.update(
            request_url=request_url, limit=limit
        ) or "<rss/>",
    )

    raw = processor._load_feed_bytes(
        "http://ai-filter-web:5000/jin10/rss", {"max_entries": 15}, 20, 3
    )
    assert raw == b"<rss/>"
    assert captured == {
        "request_url": "http://ai-filter-web:5000/jin10/rss",
        "limit": 15,
    }
    assert processor._is_internal_jin10_feed("http://127.0.0.1:5000/jin10/rss")
    assert not processor._is_internal_jin10_feed("https://example.com/jin10/rss")


def test_article_telegram_escapes_html_and_rejects_script_url(monkeypatch):
    import core.notifier as notifier

    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(payload=json.loads(request.data), timeout=timeout)
        return _Response()

    monkeypatch.setattr(notifier, "urlopen", fake_urlopen)
    assert notifier.send_telegram_notification(
        XSS, "javascript:alert(1)", 9, XSS, XSS, _telegram_cfg(), 8
    )

    message = captured["payload"]["text"]
    assert XSS not in message
    assert "&lt;a href=&quot;" in message
    assert "javascript:" not in message


def test_digest_telegram_escapes_llm_fields_and_dashboard_url(monkeypatch):
    import services.digest as digest

    captured = {}
    monkeypatch.setenv("PUBLIC_BASE_URL", "javascript:alert(1)")

    def fake_urlopen(request, timeout):
        captured.update(payload=json.loads(request.data), timeout=timeout)
        return _Response()

    monkeypatch.setattr(digest, "urlopen", fake_urlopen)
    result = digest._push_digest_to_telegram({
        "period_hours": 24,
        "article_count": 1,
        "confidence": 0.8,
        "overall_assessment": XSS,
        "risk_alert": XSS,
        "themes": [{
            "direction": "看多",
            "theme": XSS,
            "insight": XSS,
            "action": XSS,
        }],
    }, _telegram_cfg())

    assert result is True
    message = captured["payload"]["text"]
    assert XSS not in message
    assert "javascript:" not in message
    assert "http://127.0.0.1:9998" in message


def test_dashboard_auth_happens_before_database_work(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(dashboard_routes, "get_dashboard_auth", lambda: None)
    monkeypatch.setattr(
        dashboard_routes.DB,
        "get_stats",
        lambda: pytest.fail("dashboard queried the database before authentication"),
    )
    with app_module.app.test_client() as client:
        response = client.get("/dashboard")
    assert response.status_code == 401


def test_backtest_parameters_cache_and_rate_surface_are_bounded(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    calls = []
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()

    def fake_backtest(days, min_score):
        calls.append((days, min_score))
        return {
            "days": days,
            "min_score": min_score,
            "strategy_version": "score:test",
            "mapping_version": "asset-map:test",
            "buckets": [],
        }

    monkeypatch.setattr(
        dashboard_routes,
        "_backtest_cache_contract",
        lambda: ("score:test", "asset-map:test"),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "get_mapping_version",
        lambda: "asset-map:test",
    )
    monkeypatch.setattr(dashboard_routes, "run_score_backtest", fake_backtest)
    with app_module.app.test_client() as client:
        first = client.get("/score-backtest?format=json&days=99999&min_score=99")
        second = client.get("/score-backtest?format=json&days=99999&min_score=99")
    assert first.status_code == second.status_code == 200
    assert calls == [(365, 10.0)]


def test_backtest_cache_contract_uses_effective_score_and_mapping_versions(monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    source_cfg = {"scoring_strategy_version": "score:base"}
    monkeypatch.setattr(dashboard_routes, "load_config", lambda: source_cfg)

    def fake_resolve(cfg, *, register_snapshot):
        assert cfg is source_cfg
        assert register_snapshot is False
        return {"scoring_strategy_version": "score:effective"}

    monkeypatch.setattr(dashboard_routes, "resolve_scoring_contract", fake_resolve)
    monkeypatch.setattr(
        dashboard_routes,
        "get_mapping_version",
        lambda: "asset-map:effective",
    )

    assert dashboard_routes._backtest_cache_contract() == (
        "score:effective",
        "asset-map:effective",
    )


def test_backtest_cache_is_isolated_by_score_and_mapping_contract(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    contract = ["score:v1", "asset-map:v1"]
    backtest_calls = []
    mapping_calls = []
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()

    monkeypatch.setattr(
        dashboard_routes,
        "_backtest_cache_contract",
        lambda: tuple(contract),
    )

    def fake_backtest(days, min_score):
        backtest_calls.append((days, min_score, tuple(contract)))
        return {
            "revision": len(backtest_calls),
            "strategy_version": contract[0],
            "mapping_version": contract[1],
            "buckets": [],
        }

    def fake_mapping(*, days, current_contract):
        mapping_calls.append((days, current_contract))
        return {"contract": list(current_contract)}

    monkeypatch.setattr(dashboard_routes, "run_score_backtest", fake_backtest)
    monkeypatch.setattr(dashboard_routes, "run_mapping_validation", fake_mapping)
    monkeypatch.setattr(
        dashboard_routes,
        "get_mapping_version",
        lambda: contract[1],
    )

    with app_module.app.test_client() as client:
        first = client.get("/score-backtest?format=json&days=90")
        same_contract = client.get("/score-backtest?format=json&days=90")
        contract[0] = "score:v2"
        new_score_contract = client.get("/score-backtest?format=json&days=90")
        contract[1] = "asset-map:v2"
        new_mapping_contract = client.get("/score-backtest?format=json&days=90")
        same_new_contract = client.get("/score-backtest?format=json&days=90")

    assert [response.status_code for response in (
        first,
        same_contract,
        new_score_contract,
        new_mapping_contract,
        same_new_contract,
    )] == [200, 200, 200, 200, 200]
    assert [response.get_json()["revision"] for response in (
        first,
        same_contract,
        new_score_contract,
        new_mapping_contract,
        same_new_contract,
    )] == [1, 1, 2, 3, 3]
    assert backtest_calls == [
        (90, None, ("score:v1", "asset-map:v1")),
        (90, None, ("score:v2", "asset-map:v1")),
        (90, None, ("score:v2", "asset-map:v2")),
    ]
    assert mapping_calls == [
        (90, ("score:v1", "asset-map:v1")),
        (90, ("score:v2", "asset-map:v1")),
        (90, ("score:v2", "asset-map:v2")),
    ]


def test_backtest_compute_contract_wins_when_feedback_changes_mid_request(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    live_contract = ["score:v1", "asset-map:v1"]
    backtest_calls = []
    mapping_calls = []
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()

    monkeypatch.setattr(
        dashboard_routes,
        "_backtest_cache_contract",
        lambda: tuple(live_contract),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "get_mapping_version",
        lambda: live_contract[1],
    )

    def fake_backtest(days, min_score):
        backtest_calls.append((days, min_score))
        live_contract[0] = "score:v2-live"
        live_contract[1] = "asset-map:v2-live"
        return {
            "strategy_version": "score:v2-computed",
            "mapping_version": "asset-map:v2-computed",
            "buckets": [],
        }

    def fake_mapping(*, days, current_contract):
        mapping_calls.append((days, current_contract))
        return {"contract": list(current_contract)}

    monkeypatch.setattr(dashboard_routes, "run_score_backtest", fake_backtest)
    monkeypatch.setattr(dashboard_routes, "run_mapping_validation", fake_mapping)

    with app_module.app.test_client() as client:
        first = client.get("/score-backtest?format=json&days=90")
        live_contract[:] = ["score:v2-computed", "asset-map:v2-computed"]
        second = client.get("/score-backtest?format=json&days=90")

    assert first.status_code == second.status_code == 200
    assert backtest_calls == [(90, None)]
    assert mapping_calls == [(90, ("score:v2-computed", "asset-map:v2-computed"))]
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        assert (90, None, "score:v1", "asset-map:v1") not in dashboard_routes._BACKTEST_CACHE
        assert (
            90,
            None,
            "score:v2-computed",
            "asset-map:v2-computed",
        ) in dashboard_routes._BACKTEST_CACHE


def test_backtest_cache_is_bypassed_when_contract_is_unavailable(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    calls = []
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()

    monkeypatch.setattr(dashboard_routes, "_backtest_cache_contract", lambda: None)
    monkeypatch.setattr(
        dashboard_routes,
        "run_score_backtest",
        lambda **_kwargs: calls.append(True) or {"revision": len(calls), "buckets": []},
    )
    monkeypatch.setattr(
        dashboard_routes,
        "run_mapping_validation",
        lambda **_kwargs: {},
    )

    with app_module.app.test_client() as client:
        first = client.get("/score-backtest?format=json&days=90")
        second = client.get("/score-backtest?format=json&days=90")

    assert first.get_json()["revision"] == 1
    assert second.get_json()["revision"] == 2
    assert calls == [True, True]
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        assert dashboard_routes._BACKTEST_CACHE == {}


def test_backtest_compact_and_full_views_share_production_selection_cache(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    calls = []
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()

    def fake_backtest(days, min_score):
        calls.append((days, min_score))
        return {
            "strategy_version": "score:test",
            "mapping_version": "asset-map:test",
            "requested_days": days,
            "actual_window_days": 0,
            "deduped_samples": 0,
            "unique_signal_days": 0,
            "history_status": "accumulating",
            "selection_policy": "stored_aggregation_threshold",
            "buckets": [],
        }

    monkeypatch.setattr(
        dashboard_routes,
        "_backtest_cache_contract",
        lambda: ("score:test", "asset-map:test"),
    )
    monkeypatch.setattr(
        dashboard_routes,
        "get_mapping_version",
        lambda: "asset-map:test",
    )
    monkeypatch.setattr(dashboard_routes, "run_score_backtest", fake_backtest)
    with app_module.app.test_client() as client:
        compact = client.get("/score-backtest?days=90&view=compact")
        full = client.get("/score-backtest?days=90")

    assert compact.status_code == full.status_code == 200
    assert "score-validity-compact" in compact.get_data(as_text=True)
    assert "个股映射后验" in compact.get_data(as_text=True)
    assert "score-validity-compact" not in full.get_data(as_text=True)
    assert "个股映射后验关联" in full.get_data(as_text=True)
    assert calls == [(90, None)]


def test_backtest_does_not_wait_when_another_compute_is_running(monkeypatch):
    import app as app_module
    import routes.dashboard_routes as dashboard_routes
    import routes.security as security

    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    assert dashboard_routes._BACKTEST_COMPUTE_LOCK.acquire(blocking=False)
    try:
        with app_module.app.test_client() as client:
            response = client.get("/score-backtest?days=12")
    finally:
        dashboard_routes._BACKTEST_COMPUTE_LOCK.release()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


def test_jin10_fetch_is_cached_and_outages_are_negative_cached(monkeypatch):
    import services.jin10_feed as jin10_feed

    calls = []
    with jin10_feed._JIN10_CACHE_LOCK:
        jin10_feed._JIN10_CACHE.update(source_url="", ts=0.0, items=[])
    monkeypatch.setattr(
        jin10_feed,
        "_fetch_jin10_uncached",
        lambda url: calls.append(url) or [{"title": "safe"}],
    )
    assert jin10_feed.fetch_jin10_latest("https://example.com/feed") == [{"title": "safe"}]
    assert jin10_feed.fetch_jin10_latest("https://example.com/feed") == [{"title": "safe"}]
    assert calls == ["https://example.com/feed"]

    with jin10_feed._JIN10_CACHE_LOCK:
        jin10_feed._JIN10_CACHE.update(source_url="", ts=0.0, items=[])
    failures = []

    def fail(url):
        failures.append(url)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(jin10_feed, "_fetch_jin10_uncached", fail)
    with pytest.raises(RuntimeError):
        jin10_feed.fetch_jin10_latest("https://example.com/down")
    assert jin10_feed.fetch_jin10_latest("https://example.com/down") == []
    assert failures == ["https://example.com/down"]


def test_jin10_does_not_wait_for_inflight_upstream_fetch(monkeypatch):
    import services.jin10_feed as jin10_feed

    with jin10_feed._JIN10_CACHE_LOCK:
        jin10_feed._JIN10_CACHE.update(source_url="", ts=0.0, items=[])
    monkeypatch.setattr(
        jin10_feed,
        "_fetch_jin10_uncached",
        lambda _url: pytest.fail("a waiter must not start another fetch"),
    )
    assert jin10_feed._JIN10_FETCH_LOCK.acquire(blocking=False)
    try:
        assert jin10_feed.fetch_jin10_latest("https://example.com/feed") == []
    finally:
        jin10_feed._JIN10_FETCH_LOCK.release()


def test_public_jin10_route_clamps_limit(monkeypatch):
    import app as app_module
    import routes.jin10_routes as jin10_routes
    import routes.security as security

    captured = {}
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    monkeypatch.setattr(
        jin10_routes,
        "build_jin10_rss",
        lambda request_url, cfg, limit: captured.update(limit=limit) or "<rss/>",
    )
    with app_module.app.test_client() as client:
        response = client.get("/jin10/rss?limit=99999")
    assert response.status_code == 200
    assert captured["limit"] == 50


def test_rate_limit_ignores_untrusted_forwarded_for(monkeypatch):
    import routes.security as security

    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app = Flask(__name__)

    @app.route("/limited")
    @security.rate_limit(max_requests=1, window_seconds=60)
    def limited():
        return "ok"

    with app.test_client() as client:
        assert client.get("/limited", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
        assert client.get("/limited", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_rate_limit_accepts_gateway_observed_ip_only_from_exact_proxy(monkeypatch):
    import socket
    import routes.security as security

    monkeypatch.setenv("AI_FILTER_TRUSTED_PROXY_HOST", "ai-filter-web")
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", 5000))
        ],
    )
    app = Flask(__name__)

    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "10.0.0.8"},
        headers={
            "X-AI-Filter-Client-IP": "203.0.113.7",
            "X-Forwarded-For": "198.51.100.99",
        },
    ):
        assert security._rate_limit_client_ip() == "203.0.113.7"

    # A direct caller cannot activate the dedicated header, even if it also
    # forges the conventional forwarding chain.
    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "198.51.100.10"},
        headers={
            "X-AI-Filter-Client-IP": "203.0.113.8",
            "X-Forwarded-For": "203.0.113.9",
        },
    ):
        assert security._rate_limit_client_ip() == "198.51.100.10"


def test_rate_limits_are_isolated_per_endpoint():
    import routes.security as security

    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app = Flask(__name__)

    @app.route("/one")
    @security.rate_limit(max_requests=1, window_seconds=60)
    def one():
        return "one"

    @app.route("/two")
    @security.rate_limit(max_requests=1, window_seconds=60)
    def two():
        return "two"

    with app.test_client() as client:
        assert client.get("/one").status_code == 200
        assert client.get("/two").status_code == 200


def _webhook_context(payload):
    import app as app_module
    from routes.webhook_routes import webhook_receiver

    return app_module.app.test_request_context(
        "/webhook",
        method="POST",
        data=json.dumps(payload, sort_keys=True),
        content_type="application/json",
    ), webhook_receiver


def test_webhook_exact_replay_is_not_resubmitted(monkeypatch, tmp_path):
    import routes.webhook_routes as webhook_routes

    monkeypatch.setattr(webhook_routes, "_webhook_spool_dir", tmp_path / "spool")
    with webhook_routes._webhook_replay_lock:
        webhook_routes._webhook_replay_seen.clear()
    payload = {"entries": [{"title": "a", "url": "https://example.com"}]}
    context, receiver = _webhook_context(payload)
    with context:
        first = receiver()
    context, receiver = _webhook_context(payload)
    with context:
        second = receiver()

    assert first.status_code == second.status_code == 202
    assert json.loads(second.get_data(as_text=True))["status"] == "duplicate"
    assert len(list((tmp_path / "spool").glob("*.json"))) == 1
    assert json.loads(second.get_data(as_text=True))["durable"] is True


def test_webhook_queue_rejection_releases_replay_reservation(monkeypatch):
    import routes.webhook_routes as webhook_routes

    outcomes = iter([("full", None), ("stored", {"state": "accepted"})])
    with webhook_routes._webhook_replay_lock:
        webhook_routes._webhook_replay_seen.clear()
    monkeypatch.setattr(
        webhook_routes, "_submit_webhook_delivery", lambda *_args: next(outcomes)
    )
    payload = {"entries": []}
    context, receiver = _webhook_context(payload)
    with context:
        assert receiver().status_code == 503
    context, receiver = _webhook_context(payload)
    with context:
        assert json.loads(receiver().get_data(as_text=True))["status"] == "received"
