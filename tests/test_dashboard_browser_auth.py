import re
from html import unescape
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest


API_KEY = "test-dashboard-api-key"
SESSION_SECRET = "dashboard-session-secret-for-tests-only-0123456789"
BASE_URL = "https://ai.example.invalid"
EVENT_ID = "e22bec92-ca3a-4ba4-87cf-9ab425893e9e"
EVENT_NEXT = f"/dashboard/events?event_id={EVENT_ID}&has_stocks=1&tab=overview"


def _csrf_from_login(response):
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.get_data(as_text=True),
    )
    assert match, "login form did not contain a CSRF token"
    return match.group(1)


def _cookie_value(response, name):
    for raw_cookie in response.headers.getlist("Set-Cookie"):
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        if name in parsed:
            return parsed[name].value
    raise AssertionError(f"response did not set {name}")


def _login(client, api_key=API_KEY, next_path="/dashboard"):
    page = client.get(
        "/dashboard/login",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    csrf_token = _csrf_from_login(page)
    response = client.post(
        "/dashboard/login",
        base_url=BASE_URL,
        data={"csrf_token": csrf_token, "api_key": api_key, "next": next_path},
    )
    return response


def _assert_dashboard_navigation(body):
    assert "<title>AI-Filter 后台管理</title>" in body
    assert "<h1>AI-Filter 后台管理</h1>" in body
    navigation = re.search(
        r'<nav\b[^>]*aria-label="后台导航"[^>]*>(.*?)</nav>', body, re.DOTALL
    )
    assert navigation is not None
    links = re.findall(
        r'<a\b([^>]*)>\s*([^<]+?)\s*</a>', navigation.group(1)
    )
    assert len(links) == 3
    expected = [
        ("https://example.invalid/", "返回新闻首页"),
        ("/dashboard/events", "事件雷达"),
        ("/briefing?format=html", "今日情报"),
    ]
    for (attributes, label), (href, expected_label) in zip(links, expected):
        assert dict(re.findall(r'([\w-]+)="([^"]*)"', attributes))["href"] == href
        assert label == expected_label
    home_attributes = dict(re.findall(r'([\w-]+)="([^"]*)"', links[0][0]))
    assert "noreferrer" in home_attributes.get("rel", "").split()
    assert home_attributes.get("target", "_self") == "_self"
    assert API_KEY not in navigation.group(1)
    return navigation.group(1)


@pytest.fixture
def dashboard_browser(monkeypatch):
    import app as app_module
    import routes.browser_auth as browser_auth
    import routes.shared as shared

    security = {
        "api_key": API_KEY,
        # Even if legacy query authentication is enabled elsewhere, it must not
        # authorize the browser Dashboard.
        "allow_query_api_key": True,
    }

    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SESSION_TTL_SECONDS", "28800")
    monkeypatch.setattr(browser_auth, "_security_cfg", lambda: security)
    monkeypatch.setattr(shared, "_security_cfg", lambda: security)
    monkeypatch.setattr(
        browser_auth, "_consume_login_attempt", lambda _secret: (True, 1)
    )
    monkeypatch.setattr(browser_auth, "_clear_login_attempts", lambda _secret: None)
    active_sessions = set()
    monkeypatch.setattr(
        browser_auth,
        "_store_session",
        lambda _secret, session_id: active_sessions.add(session_id),
    )
    monkeypatch.setattr(
        browser_auth,
        "_session_record_valid",
        lambda _secret, session_id: session_id in active_sessions,
    )
    monkeypatch.setattr(
        browser_auth,
        "_revoke_session",
        lambda _secret, session_id: active_sessions.discard(session_id),
    )
    app_module.app.config["TESTING"] = True
    app_module.init_db()

    with app_module.app.test_client() as client:
        yield client, browser_auth, security


def test_login_page_sets_strict_host_cookie_and_security_headers(dashboard_browser):
    client, browser_auth, _security = dashboard_browser

    response = client.get(
        "/dashboard/login",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert _csrf_from_login(response)
    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert browser_auth._LOGIN_CSRF_COOKIE in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    assert response.headers["Cache-Control"].startswith("no-store")
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_browser_login_refuses_plain_http_without_setting_cookies(dashboard_browser):
    client, browser_auth, _security = dashboard_browser

    response = client.get("/dashboard/login", base_url="http://ai.example.invalid")

    assert response.status_code == 400
    assert response.get_json() == {"error": "HTTPS required"}
    assert browser_auth._LOGIN_CSRF_COOKIE not in "\n".join(
        response.headers.getlist("Set-Cookie")
    )


def test_login_requires_matching_csrf_before_checking_credentials(
    dashboard_browser, monkeypatch
):
    client, browser_auth, _security = dashboard_browser
    page = client.get("/dashboard/login", base_url=BASE_URL)
    assert page.status_code == 200
    monkeypatch.setattr(
        browser_auth,
        "_consume_login_attempt",
        lambda _secret: pytest.fail("rate limiter must run only after CSRF validation"),
    )

    response = client.post(
        "/dashboard/login",
        base_url=BASE_URL,
        data={"csrf_token": "wrong", "api_key": API_KEY, "next": "/dashboard"},
    )

    assert response.status_code == 403
    assert "登录页面已过期" in response.get_data(as_text=True)
    assert browser_auth._SESSION_COOKIE not in "\n".join(
        response.headers.getlist("Set-Cookie")
    )


@pytest.mark.parametrize(
    ("limiter_result", "limiter_error", "expected_status", "retry_after"),
    [
        ((False, 6), None, 429, "900"),
        (None, RuntimeError("redis unavailable"), 503, None),
    ],
)
def test_login_rate_limiter_is_bounded_and_fails_closed(
    dashboard_browser,
    monkeypatch,
    limiter_result,
    limiter_error,
    expected_status,
    retry_after,
):
    client, browser_auth, _security = dashboard_browser
    page = client.get("/dashboard/login", base_url=BASE_URL)
    csrf_token = _csrf_from_login(page)

    def limiter(_secret):
        if limiter_error:
            raise limiter_error
        return limiter_result

    monkeypatch.setattr(browser_auth, "_consume_login_attempt", limiter)
    response = client.post(
        "/dashboard/login",
        base_url=BASE_URL,
        data={"csrf_token": csrf_token, "api_key": API_KEY},
    )

    assert response.status_code == expected_status
    assert response.headers.get("Retry-After") == retry_after
    assert browser_auth._SESSION_COOKIE not in "\n".join(
        response.headers.getlist("Set-Cookie")
    )


def test_successful_login_uses_signed_session_and_rejects_open_redirect(
    dashboard_browser,
):
    client, browser_auth, _security = dashboard_browser

    response = _login(client, next_path="https://evil.example/steal")

    assert response.status_code == 303
    assert response.headers["Location"] == "/dashboard"
    set_cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert browser_auth._SESSION_COOKIE in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Max-Age=28800" in set_cookie
    session_token = _cookie_value(response, browser_auth._SESSION_COOKIE)
    payload = browser_auth._serializer(
        SESSION_SECRET, browser_auth._SESSION_SALT
    ).loads(session_token)
    assert payload["purpose"] == "dashboard"
    assert payload["csrf"]
    assert payload["sid"]
    assert API_KEY not in session_token
    assert API_KEY not in repr(payload)

    # A valid ambient session is recognized without querying Dashboard data.
    already_logged_in = client.get("/dashboard/login", base_url=BASE_URL)
    assert already_logged_in.status_code == 303
    assert already_logged_in.headers["Location"] == "/dashboard"


def test_event_radar_is_an_exact_allowed_login_destination(dashboard_browser):
    client, browser_auth, _security = dashboard_browser

    login_page = client.get(
        "/dashboard/login?next=/dashboard/events",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    )
    assert login_page.status_code == 200
    assert 'name="next" value="/dashboard/events"' in login_page.get_data(
        as_text=True
    )

    response = client.post(
        "/dashboard/login",
        base_url=BASE_URL,
        data={
            "csrf_token": _csrf_from_login(login_page),
            "api_key": API_KEY,
            "next": "/dashboard/events",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"] == "/dashboard/events"
    assert browser_auth._safe_next("/dashboard/events?event_id=evil") == "/dashboard"


def test_event_selection_survives_login_round_trip(dashboard_browser, monkeypatch):
    from flask import Response
    import routes.event_radar_routes as radar_routes

    client, _browser_auth, _security = dashboard_browser
    queries = []

    def load_selected_event(**values):
        queries.append(values)
        return {"selected_event_id": values["event_id"]}

    monkeypatch.setattr(radar_routes, "load_event_radar", load_selected_event)
    monkeypatch.setattr(
        radar_routes, "_render_model",
        lambda model, _auth: Response(model["selected_event_id"]),
    )
    response = client.get(
        EVENT_NEXT + "&key=do-not-copy&cursor=do-not-copy",
        base_url=BASE_URL, headers={"Accept": "text/html"},
    )
    assert response.status_code == 302
    assert queries == []  # No protected event read before authentication.
    login_location = response.headers["Location"]
    assert parse_qs(urlsplit(login_location).query) == {"next": [EVENT_NEXT]}
    assert "do-not-copy" not in login_location

    page = client.get(login_location, base_url=BASE_URL)
    next_match = re.search(r'name="next"\s+value="([^"]+)"', page.get_data(as_text=True))
    assert next_match
    form_next = unescape(next_match.group(1))
    assert form_next == EVENT_NEXT
    login = client.post(
        "/dashboard/login", base_url=BASE_URL,
        data={"csrf_token": _csrf_from_login(page), "api_key": API_KEY, "next": form_next},
    )
    assert login.status_code == 303
    assert login.headers["Location"] == EVENT_NEXT
    selected = client.get(login.headers["Location"], base_url=BASE_URL)
    assert selected.status_code == 200
    assert selected.get_data(as_text=True) == EVENT_ID
    assert queries == [{
        "event_id": EVENT_ID, "cursor": "", "limit": "",
        "tab": "overview", "has_stocks": "1",
    }]


@pytest.mark.parametrize("tab", ["overview", "detail", "market", "outcomes", "reaction", "similar"])
def test_event_next_canonicalizes_allowed_selectors(tab):
    from routes.browser_auth import _safe_next

    target = f"/dashboard/events?tab={tab}&has_stocks=0&%65vent_id={EVENT_ID}"
    assert _safe_next(target) == f"/dashboard/events?event_id={EVENT_ID}&has_stocks=0&tab={tab}"


@pytest.mark.parametrize("target", [
    "https://evil.example/dashboard/events",
    f"https://ai.example.invalid{EVENT_NEXT}",
    f"//evil.example{EVENT_NEXT}",
    f"//{EVENT_NEXT}",
    f" /{EVENT_NEXT}",
    f"\n{EVENT_NEXT}",
    f"{EVENT_NEXT}\r\nX-Injected: true",
    f"{EVENT_NEXT}\x00",
    f"{EVENT_NEXT}\x7f",
    f"{EVENT_NEXT}#https://evil.example",
    f"{EVENT_NEXT}&next=https://evil.example",
    f"{EVENT_NEXT}&event_id={EVENT_ID}",
    f"/dashboard/events?event_id={EVENT_ID}&%65vent_id={EVENT_ID}",
    "/dashboard/events?event_id=%252f%252fevil.example",
    "/dashboard/events?event_id=%zz",
    "/dashboard/events?event_id=%ff",
    "/dashboard/events?event_id=",
    "/dashboard/events?event_id=not-a-uuid",
    f"/dashboard/events?event_id={EVENT_ID}%26tab%3Dsimilar",
    f"/dashboard/events?event_id={EVENT_ID}%0d%0aLocation%3Aevil",
    f"/dashboard/events?event_id={EVENT_ID}\\",
    f"/dashboard/events?event_id={EVENT_ID};tab=similar",
    f"/dashboard/events?event_id={EVENT_ID}&tab=unknown",
    f"/dashboard/events?event_id={EVENT_ID}&has_stocks=true",
    f"/dashboard/events?event_id={EVENT_ID}&tab",
    "/dashboard/events?tab=" + "a" * 513,
    "/dashboard?tab=overview",
    "/dashboard/neutral-records?tab=overview",
    f"/dashboard/events/../events?event_id={EVENT_ID}",
    f"/dashboard/%65vents?event_id={EVENT_ID}",
    f"/dashboard/events?event_id={EVENT_ID}&api_key=secret",
])
def test_event_next_rejects_ambiguous_or_unsafe_targets(target):
    from routes.browser_auth import _safe_next

    assert _safe_next(target) == "/dashboard"


def test_duplicate_event_selector_never_becomes_login_destination(dashboard_browser):
    client, _browser_auth, _security = dashboard_browser
    response = client.get(
        EVENT_NEXT + f"&event_id={EVENT_ID}", base_url=BASE_URL,
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["Location"]).query) == {"next": ["/dashboard"]}


def test_failed_login_and_existing_session_preserve_safe_event_target(dashboard_browser):
    client, _browser_auth, _security = dashboard_browser
    page = client.get("/dashboard/login", query_string={"next": EVENT_NEXT}, base_url=BASE_URL)
    failed = client.post(
        "/dashboard/login", base_url=BASE_URL,
        data={"csrf_token": _csrf_from_login(page), "api_key": "wrong", "next": EVENT_NEXT},
    )
    assert failed.status_code == 401
    match = re.search(r'name="next"\s+value="([^"]+)"', failed.get_data(as_text=True))
    assert match and unescape(match.group(1)) == EVENT_NEXT
    accepted = client.post(
        "/dashboard/login", base_url=BASE_URL,
        data={"csrf_token": _csrf_from_login(failed), "api_key": API_KEY, "next": EVENT_NEXT},
    )
    assert accepted.status_code == 303
    assert accepted.headers["Location"] == EVENT_NEXT
    already_logged_in = client.get(
        "/dashboard/login", query_string={"next": EVENT_NEXT}, base_url=BASE_URL,
    )
    assert already_logged_in.status_code == 303
    assert already_logged_in.headers["Location"] == EVENT_NEXT


def test_wrong_api_key_is_rejected_without_echo_or_session(dashboard_browser):
    client, browser_auth, _security = dashboard_browser
    supplied = "wrong-key-that-must-not-be-reflected"

    response = _login(client, api_key=supplied)

    assert response.status_code == 401
    body = response.get_data(as_text=True)
    assert "凭据无效" in body
    assert supplied not in body
    assert browser_auth._SESSION_COOKIE not in "\n".join(
        response.headers.getlist("Set-Cookie")
    )


def test_login_fails_closed_when_session_store_is_unavailable(
    dashboard_browser, monkeypatch
):
    client, browser_auth, _security = dashboard_browser

    def fail_store(_secret, _session_id):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(browser_auth, "_store_session", fail_store)
    response = _login(client)

    assert response.status_code == 503
    assert "会话保护服务暂不可用" in response.get_data(as_text=True)
    assert browser_auth._SESSION_COOKIE not in "\n".join(
        response.headers.getlist("Set-Cookie")
    )


def test_unicode_login_inputs_fail_cleanly_without_server_error(dashboard_browser):
    client, browser_auth, _security = dashboard_browser
    page = client.get("/dashboard/login", base_url=BASE_URL)
    assert page.status_code == 200

    bad_csrf = client.post(
        "/dashboard/login",
        base_url=BASE_URL,
        data={"csrf_token": "无效令牌", "api_key": API_KEY},
    )
    refreshed_csrf = _csrf_from_login(bad_csrf)
    unicode_key = client.post(
        "/dashboard/login",
        base_url=BASE_URL,
        data={"csrf_token": refreshed_csrf, "api_key": "错误密钥🔒"},
    )

    assert bad_csrf.status_code == 403
    assert unicode_key.status_code == 401
    assert browser_auth._SESSION_COOKIE not in "\n".join(
        unicode_key.headers.getlist("Set-Cookie")
    )


def test_session_renders_full_dashboard_with_logout_csrf(dashboard_browser):
    client, browser_auth, _security = dashboard_browser
    login = _login(client)
    session_token = _cookie_value(login, browser_auth._SESSION_COOKIE)
    payload = browser_auth._serializer(
        SESSION_SECRET, browser_auth._SESSION_SALT
    ).loads(session_token)

    response = client.get(
        "/dashboard",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    navigation = _assert_dashboard_navigation(body)
    assert 'action="/dashboard/logout"' in navigation
    assert f'value="{payload["csrf"]}"' in navigation
    assert 'action="/dashboard/logout"' in body
    assert f'value="{payload["csrf"]}"' in body
    quality_link = re.search(
        r'<a\b[^>]*href="([^"]+)"[^>]*>\s*模型质量\s*</a>', body
    )
    assert quality_link is not None
    assert quality_link.group(1) == "https://example.invalid/quality"
    assert 'href="/score-quality"' not in body
    assert 'href="/dashboard/neutral-records"' in body
    records = client.get("/dashboard/neutral-records", base_url=BASE_URL)
    assert records.status_code == 200
    assert (
        f'href="{quality_link.group(1)}">返回模型质量</a>'
        in records.get_data(as_text=True)
    )
    assert response.headers["Cache-Control"].startswith("no-store")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_header_authenticated_dashboard_has_same_navigation_without_logout(
    dashboard_browser,
):
    client, _browser_auth, _security = dashboard_browser

    response = client.get(
        "/dashboard",
        base_url=BASE_URL,
        headers={"Accept": "text/html", "X-API-Key": API_KEY},
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    _assert_dashboard_navigation(body)
    assert 'action="/dashboard/logout"' not in body
    assert 'name="csrf_token"' not in body
    assert API_KEY not in body
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_api_key_rotation_immediately_invalidates_existing_session(dashboard_browser):
    client, _browser_auth, security = dashboard_browser
    assert _login(client).status_code == 303
    security["api_key"] = "rotated-dashboard-key"

    response = client.get("/dashboard/login", base_url=BASE_URL)

    assert response.status_code == 200
    assert "管理 Dashboard" in response.get_data(as_text=True)


def test_dashboard_query_key_never_authorizes_and_auth_precedes_database(
    dashboard_browser, monkeypatch
):
    client, _browser_auth, _security = dashboard_browser
    import routes.dashboard_routes as dashboard_routes

    monkeypatch.setattr(
        dashboard_routes.DB,
        "get_stats",
        lambda: pytest.fail("Dashboard database work ran before authentication"),
    )

    json_response = client.get(
        f"/dashboard?key={API_KEY}",
        base_url=BASE_URL,
        headers={"Accept": "application/json"},
    )
    html_response = client.get(
        f"/dashboard?key={API_KEY}",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    )

    assert json_response.status_code == 401
    assert html_response.status_code == 302
    assert html_response.headers["Location"] == "/dashboard/login?next=/dashboard"


def test_explicit_bad_header_never_falls_back_to_valid_session(
    dashboard_browser, monkeypatch
):
    client, _browser_auth, _security = dashboard_browser
    import routes.dashboard_routes as dashboard_routes

    assert _login(client).status_code == 303
    monkeypatch.setattr(
        dashboard_routes.DB,
        "get_stats",
        lambda: pytest.fail("invalid header fell back to the session cookie"),
    )

    response = client.get(
        "/dashboard",
        base_url=BASE_URL,
        headers={"Accept": "text/html", "X-API-Key": "definitely-wrong"},
    )

    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_session_cookie_cannot_be_replayed_over_http_or_spoofed_proto(
    dashboard_browser, monkeypatch
):
    client, browser_auth, _security = dashboard_browser
    import app as app_module

    login = _login(client)
    session_token = _cookie_value(login, browser_auth._SESSION_COOKIE)
    cookie = f"{browser_auth._SESSION_COOKIE}={session_token}"
    monkeypatch.setattr(browser_auth, "request_from_trusted_proxy", lambda: False)

    with app_module.app.test_request_context(
        "/dashboard",
        base_url="http://ai.example.invalid",
        headers={"Cookie": cookie, "X-Forwarded-Proto": "https"},
    ):
        assert browser_auth.get_dashboard_auth() is None


@pytest.mark.parametrize("path", ["/stats", "/score-quality"])
def test_dashboard_session_does_not_authorize_non_dashboard_api(dashboard_browser, path):
    client, _browser_auth, _security = dashboard_browser
    assert _login(client).status_code == 303

    response = client.get(path, base_url=BASE_URL)

    assert response.status_code == 401


def test_session_feedback_requires_csrf_but_header_auth_does_not(
    dashboard_browser, monkeypatch
):
    client, browser_auth, _security = dashboard_browser
    import routes.dashboard_routes as dashboard_routes

    login = _login(client)
    assert login.status_code == 303
    session_token = _cookie_value(login, browser_auth._SESSION_COOKIE)
    payload = browser_auth._serializer(
        SESSION_SECRET, browser_auth._SESSION_SALT
    ).loads(session_token)
    writes = []
    monkeypatch.setattr(
        dashboard_routes.DB,
        "add_article_feedback",
        lambda *args: writes.append(args),
    )

    missing = client.post(
        "/feedback",
        base_url=BASE_URL,
        data={"link": "https://example.com/one", "label": "good"},
    )
    wrong = client.post(
        "/feedback",
        base_url=BASE_URL,
        data={
            "link": "https://example.com/two",
            "label": "good",
            "csrf_token": "wrong",
        },
    )
    valid = client.post(
        "/feedback",
        base_url=BASE_URL,
        data={
            "link": "https://example.com/three",
            "label": "good",
            "csrf_token": payload["csrf"],
        },
    )
    header = client.post(
        "/feedback",
        base_url=BASE_URL,
        headers={"X-API-Key": API_KEY},
        data={"link": "https://example.com/four", "label": "good"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert valid.status_code == 303
    assert header.status_code == 303
    assert [row[0] for row in writes] == [
        "https://example.com/three",
        "https://example.com/four",
    ]


def test_logout_is_post_only_requires_csrf_and_revokes_server_session(
    dashboard_browser, monkeypatch
):
    client, browser_auth, _security = dashboard_browser
    import app as app_module

    login = _login(client)
    session_token = _cookie_value(login, browser_auth._SESSION_COOKIE)
    payload = browser_auth._serializer(
        SESSION_SECRET, browser_auth._SESSION_SALT
    ).loads(session_token)
    revoked = []
    original_revoke = browser_auth._revoke_session

    def revoke_and_record(secret, session_id):
        revoked.append(session_id)
        original_revoke(secret, session_id)

    monkeypatch.setattr(
        browser_auth,
        "_revoke_session",
        revoke_and_record,
    )

    assert client.get("/dashboard/logout", base_url=BASE_URL).status_code == 405
    assert client.post(
        "/dashboard/logout",
        base_url=BASE_URL,
        data={"csrf_token": "wrong"},
    ).status_code == 403

    response = client.post(
        "/dashboard/logout",
        base_url=BASE_URL,
        data={"csrf_token": payload["csrf"]},
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/dashboard/login"
    cleared = "\n".join(response.headers.getlist("Set-Cookie"))
    assert browser_auth._SESSION_COOKIE in cleared
    assert "Max-Age=0" in cleared
    assert revoked == [payload["sid"]]
    with app_module.app.test_request_context(
        "/dashboard",
        base_url=BASE_URL,
        headers={"Cookie": f"{browser_auth._SESSION_COOKIE}={session_token}"},
    ):
        assert browser_auth._session_auth() is None
    login_page = client.get("/dashboard/login", base_url=BASE_URL)
    assert login_page.status_code == 200


@pytest.mark.parametrize(
    "secret",
    ["too-short", API_KEY],
)
def test_browser_login_fails_closed_for_unsafe_session_secret(
    dashboard_browser, monkeypatch, secret
):
    client, _browser_auth, _security = dashboard_browser
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", secret)

    response = client.get("/dashboard/login", base_url=BASE_URL)

    assert response.status_code == 503
    assert "浏览器登录尚未完成安全配置" in response.get_data(as_text=True)
    assert "csrf_token" not in response.get_data(as_text=True)


def test_session_ttl_is_bounded(monkeypatch):
    import routes.browser_auth as browser_auth

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SESSION_TTL_SECONDS", "1")
    assert browser_auth.dashboard_session_ttl_seconds() == 300
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SESSION_TTL_SECONDS", "999999")
    assert browser_auth.dashboard_session_ttl_seconds() == 12 * 3600


def test_file_secret_is_supported_and_conflicting_sources_fail_closed(
    dashboard_browser, monkeypatch, tmp_path
):
    _client, browser_auth, _security = dashboard_browser
    secret_file = tmp_path / "dashboard-session-secret"
    secret_file.write_text(SESSION_SECRET + "\n", encoding="utf-8")
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET_FILE", str(secret_file))
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET", raising=False)

    assert browser_auth.dashboard_session_secret() == SESSION_SECRET

    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", "x" * 64)
    assert browser_auth.dashboard_session_secret() == ""


def test_redis_session_record_uses_ttl_and_can_be_revoked(monkeypatch):
    import routes.browser_auth as browser_auth

    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SESSION_TTL_SECONDS", "28800")
    monkeypatch.setattr(browser_auth, "_security_cfg", lambda: {"api_key": API_KEY})

    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.last_ex = None

        def set(self, key, value, ex):
            self.values[key] = value
            self.last_ex = ex
            return True

        def get(self, key):
            return self.values.get(key)

        def delete(self, key):
            self.values.pop(key, None)

        def close(self):
            return None

    fake = FakeRedis()
    monkeypatch.setattr(browser_auth, "_redis_client", lambda: fake)
    session_id = "server-side-session-id"

    browser_auth._store_session(SESSION_SECRET, session_id)
    stored_key = next(iter(fake.values))
    assert session_id not in stored_key
    assert fake.values[stored_key] != API_KEY
    assert fake.last_ex == 28800
    assert browser_auth._session_record_valid(SESSION_SECRET, session_id) is True

    browser_auth._revoke_session(SESSION_SECRET, session_id)
    assert browser_auth._session_record_valid(SESSION_SECRET, session_id) is False


def test_dashboard_never_grants_cors_and_rejects_global_options_handler(
    dashboard_browser,
):
    client, _browser_auth, _security = dashboard_browser
    headers = {"Accept": "text/html", "Origin": "https://untrusted.example"}

    page = client.get("/dashboard/login", base_url=BASE_URL, headers=headers)
    options = client.options(
        "/dashboard/events", base_url=BASE_URL, headers=headers
    )

    assert page.status_code == 200
    assert page.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert options.status_code == 405
    assert options.headers["Allow"] == "GET, POST"
    for response in (page, options):
        assert not any(
            name.lower().startswith("access-control-")
            for name in response.headers.keys()
        )
