import json

import pytest


@pytest.fixture(autouse=True)
def reset_webhook_replay_cache():
    import routes.webhook_routes as webhook_routes

    with webhook_routes._webhook_replay_lock:
        webhook_routes._webhook_replay_seen.clear()
    yield
    with webhook_routes._webhook_replay_lock:
        webhook_routes._webhook_replay_seen.clear()


@pytest.fixture
def app_client():
    import app as app_module

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def test_briefing_llm_rejects_before_database_query(app_client, monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(dashboard_routes, "check_api_key", lambda: False)
    monkeypatch.setattr(
        dashboard_routes,
        "_get_briefing_articles",
        lambda **_kwargs: pytest.fail("database query must not run before authentication"),
    )

    assert app_client.get("/briefing-card?llm=true").status_code == 401
    assert app_client.get("/briefing?llm=true").status_code == 401


def test_digest_push_requires_auth_and_post(app_client, monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(dashboard_routes, "check_api_key", lambda: False)
    assert app_client.get("/briefing?push=true").status_code == 401

    monkeypatch.setattr(dashboard_routes, "check_api_key", lambda: True)
    response = app_client.get("/briefing?push=true")
    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"


def test_public_local_briefing_remains_available(app_client, monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(dashboard_routes, "check_api_key", lambda: False)
    monkeypatch.setattr(dashboard_routes, "_get_briefing_articles", lambda **_kwargs: [])

    assert app_client.get("/briefing-card").status_code == 200
    assert app_client.get("/briefing").status_code == 200


def test_public_briefing_routes_share_default_window_and_allow_override(
    app_client, monkeypatch
):
    import routes.dashboard_routes as dashboard_routes

    calls = []
    cfg = {
        "briefing": {"default_hours": 24},
        "briefing_themes": [],
        "briefing_assembly": {"enabled": False},
    }
    monkeypatch.setattr(dashboard_routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(
        dashboard_routes,
        "_get_briefing_articles",
        lambda **kwargs: calls.append(kwargs) or [],
    )
    monkeypatch.setattr(
        dashboard_routes,
        "attach_quant_market_context",
        lambda result: result,
    )

    assert app_client.get("/briefing-card").status_code == 200
    assert app_client.get("/briefing").status_code == 200
    assert app_client.get("/briefing-card?hours=48").status_code == 200
    assert app_client.get("/briefing?hours=48").status_code == 200

    assert [call["hours"] for call in calls] == [24, 24, 48, 48]


def test_briefing_card_parameters_are_bounded(app_client, monkeypatch):
    import routes.dashboard_routes as dashboard_routes

    captured = {}

    def fake_query(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(dashboard_routes, "_get_briefing_articles", fake_query)
    response = app_client.get(
        "/briefing-card?hours=999999&limit=999999&min_score=99&feed_url=" + "x" * 3000
    )

    assert response.status_code == 200
    assert captured["hours"] == 168
    assert captured["limit"] == 50
    assert captured["min_score"] == 10.0
    assert len(captured["feed_url"]) == 2048


def test_feedback_requires_auth_and_bounds_fields(app_client, monkeypatch):
    import routes.dashboard_routes as dashboard_routes
    from routes.browser_auth import DashboardAuth

    monkeypatch.setattr(dashboard_routes, "get_dashboard_auth", lambda: None)
    monkeypatch.setattr(
        dashboard_routes.DB,
        "add_article_feedback",
        lambda *_args: pytest.fail("feedback must not be written before authentication"),
    )
    assert app_client.post("/feedback", data={"link": "https://example.com"}).status_code == 401

    captured = {}
    monkeypatch.setattr(
        dashboard_routes,
        "get_dashboard_auth",
        lambda: DashboardAuth(kind="header"),
    )
    monkeypatch.setattr(
        dashboard_routes.DB,
        "add_article_feedback",
        lambda link, label, score, note: captured.update(
            link=link, label=label, score=score, note=note
        ),
    )
    response = app_client.post(
        "/feedback",
        data={
            "link": "https://example.com/" + "a" * 3000,
            "label": "good",
            "target_score": "999",
            "note": "n" * 3000,
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith("/dashboard")
    assert len(captured["link"]) == 2048
    assert captured["score"] == 10.0
    assert len(captured["note"]) == 1000


def _webhook_request(payload):
    import app as app_module
    from routes.webhook_routes import webhook_receiver

    return app_module.app.test_request_context(
        "/webhook",
        method="POST",
        data=json.dumps(payload),
        content_type="application/json",
    ), webhook_receiver


def test_webhook_rejects_oversized_batch(monkeypatch):
    import routes.webhook_routes as webhook_routes

    monkeypatch.setattr(webhook_routes, "_webhook_max_entries", 2)
    context, receiver = _webhook_request({"entries": [{}, {}, {}]})
    with context:
        response = receiver()
    assert response.status_code == 413


def test_webhook_applies_backpressure(monkeypatch):
    import routes.webhook_routes as webhook_routes

    monkeypatch.setattr(webhook_routes, "_submit_webhook_delivery", lambda *_a: ("full", None))
    context, receiver = _webhook_request({"entries": []})
    with context:
        response = receiver()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


def test_webhook_normalizes_fields_before_bounded_submit(monkeypatch):
    import routes.webhook_routes as webhook_routes

    captured = {}
    monkeypatch.setattr(
        webhook_routes,
        "_submit_webhook_delivery",
        lambda task, _body: captured.update(task=task) or ("stored", {"state": "accepted"}),
    )
    context, receiver = _webhook_request({
        "feed": {"feed_url": "f" * 3000},
        "entries": [{
            "title": "t" * 1000,
            "url": "https://example.com/" + "u" * 3000,
            "content": "<p>safe</p>" + "界" * 30000,
        }],
    })
    with context:
        response = receiver()

    entry = captured["task"]["entries"][0]
    assert response.status_code == 202
    assert len(captured["task"]["feed_url"]) == 2048
    assert len(entry["title"]) == 512
    assert len(entry["url"]) == 2048
    assert len(entry["content"].encode("utf-8")) <= 20 * 1024
    assert "<p>" not in entry["content"]
