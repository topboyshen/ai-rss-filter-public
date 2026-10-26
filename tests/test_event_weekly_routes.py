"""Weekly report HTTP, browser-session and shared presentation regressions."""

import re
from contextlib import contextmanager
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


BASE = "https://ai.example.invalid"
PATH = "/dashboard/events/weekly"
KEY = "weekly-route-test-key"
SELECTED = PATH + "?week=2026-09-28"
PAGED = SELECTED + "&block=new&page=2"
NOW = int(datetime(2026, 10, 6, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
INVALID_PAGING_QUERIES = (
    "block=new&page=1", "week=2026-09-28&block=new", "week=2026-09-28&page=1",
    "week=2026-09-28&block=&page=1", "week=2026-09-28&block=all&page=1",
    "week=2026-09-28&block=NEW&page=1", "week=2026-09-28&block=new&page=",
    "week=2026-09-28&block=new&page=0", "week=2026-09-28&block=new&page=-1",
    "week=2026-09-28&block=new&page=01", "week=2026-09-28&block=new&page=1.0",
    "week=2026-09-28&block=new&page=%2B1", "week=2026-09-28&block=new&page=%201",
    "week=2026-09-28&block=new&page=1%20", "week=2026-09-28&block=new&page=%EF%BC%91",
    "week=2026-09-28&block=new&page=7001", "week=2026-09-28&block=new&page=" + "9" * 100,
    "week=2026-09-28&block=new&page=1&page=2",
    "week=2026-09-28&block=new&page=1&%70age=2",
    "week=2026-09-28&block=new&%62lock=correction&page=1",
    "week=2026-09-28&%77eek=2026-09-28&block=new&page=1",
    "week=2026-09-28&block=new&page=1&api_key=secret",
    "week=2026-09-28&block=new&page=1&limit=100",
    "week=2026-09-28&block=new&page=1%23event-detail",
    "week=2026-09-28&block=new&page=1%0a",
    "week=2026-09-28&block=new&page=%ff",
)


@pytest.fixture
def browser(monkeypatch):
    import app as app_module
    import db.database as database
    import routes.browser_auth as auth
    import routes.event_radar_routes as routes
    import routes.security as security
    import routes.shared as shared
    import services.event_daily_report as daily
    import services.event_weekly_report as weekly

    database.init_db()
    settings = {"api_key": KEY, "allow_query_api_key": True}
    monkeypatch.setenv("AI_FILTER_SESSION_SECRET", "weekly-route-session-secret-0123456789")
    monkeypatch.delenv("AI_FILTER_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setattr(auth, "_security_cfg", lambda: settings)
    monkeypatch.setattr(shared, "_security_cfg", lambda: settings)
    monkeypatch.setattr(auth, "_consume_login_attempt", lambda _secret: (True, 1))
    monkeypatch.setattr(auth, "_clear_login_attempts", lambda _secret: None)
    sessions = set()
    monkeypatch.setattr(auth, "_store_session", lambda _secret, sid: sessions.add(sid))
    monkeypatch.setattr(auth, "_session_record_valid", lambda _secret, sid: sid in sessions)
    monkeypatch.setattr(routes, "_cfg", lambda: {"feeds": []})
    monkeypatch.setattr(routes, "load_weekly_report", lambda week, **kwargs: weekly.load_weekly_report(week, now=NOW, **kwargs))
    monkeypatch.setattr(
        routes, "load_daily_report",
        lambda day, **kwargs: daily.load_daily_report(day, now=NOW, **kwargs),
    )
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client, auth, database


def _get(client, url=SELECTED):
    return client.get(url, base_url=BASE, headers={"X-API-Key": KEY})


def _field(response, name):
    match = re.search(r'name="' + name + r'"\s+value="([^"]+)"', response.get_data(as_text=True))
    assert match
    return unescape(match.group(1))


def _login(client, next_path=SELECTED):
    page = client.get("/dashboard/login", base_url=BASE)
    return client.post("/dashboard/login", base_url=BASE, data={
        "api_key": KEY, "csrf_token": _field(page, "csrf_token"), "next": next_path,
    })


def _event(database, title="本周入库检查事件", *, day=30, month=9, priority=8.0):
    from db.event_core import resolve_or_create_event, upsert_event_evidence

    at = int(datetime(2026, month, day, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    with database.get_connection() as conn:
        event_id = resolve_or_create_event(
            conn, {"weekly-route": title}, title=title, event_type="policy",
            direction="中性", priority=priority, occurred_at=at, now=at,
        )
        upsert_event_evidence(
            conn, event_id, source_uri=f"https://news.example/{event_id}",
            source_name="https://feed.example/rss", claim_key=event_id,
            claim_text=title, stance="supports", announcement_at=at - 60,
            observed_at=at, now=at,
        )
    return event_id


@pytest.mark.parametrize("target", [
    PATH, SELECTED, PATH + "?week=2026-10-05",
])
def test_weekly_safe_next_accepts_only_the_page_and_monday(target):
    from routes.browser_auth import _safe_next

    assert _safe_next(target) == target


@pytest.mark.parametrize("block", ["new", "advanced", "correction", "pending"])
@pytest.mark.parametrize("page", [1, 2, 7000])
def test_weekly_safe_next_preserves_bounded_paging(block, page):
    from routes.browser_auth import _safe_next

    target = SELECTED + f"&block={block}&page={page}"
    assert _safe_next(target) == target
    reordered = PATH + f"?page={page}&block={block}&week=2026-09-28"
    assert _safe_next(reordered) == target


@pytest.mark.parametrize("query", INVALID_PAGING_QUERIES)
def test_weekly_safe_next_rejects_invalid_paging(query):
    from routes.browser_auth import _safe_next

    assert _safe_next(PATH + "?" + query) == "/dashboard"


@pytest.mark.parametrize("target", [
    PAGED + "#", PAGED + "#block-new", "https://evil.example" + PAGED,
    "//evil.example" + PAGED, PAGED + "\\",
])
def test_weekly_safe_next_rejects_unsafe_paging_targets(target):
    from routes.browser_auth import _safe_next

    assert _safe_next(target) == "/dashboard"


@pytest.mark.parametrize("target", [
    PATH + "?week=2026-10-06", PATH + "?week=2026-02-30",
    PATH + "?week=2026-9-28", PATH + "?week=20260928", PATH + "?week=",
    PATH + "?week=%202026-09-28", PATH + "?week=2026-09-28%20",
    SELECTED + "&week=2026-10-05", SELECTED + "&%77eek=2026-09-28",
    SELECTED + "&tab=overview", SELECTED + "&api_key=secret",
    SELECTED + "#event-detail", SELECTED + "#", PATH + "#",
    PATH + "?week=//evil.example", "https://evil.example" + SELECTED,
    "//evil.example" + SELECTED, PATH + "/../weekly?week=2026-09-28",
    SELECTED + "%0a", SELECTED + "\\", PATH + "?week=%ff",
    PATH + "?date=2026-09-28",
])
def test_weekly_safe_next_rejects_ambiguous_or_unsafe_targets(target):
    from routes.browser_auth import _safe_next

    assert _safe_next(target) == "/dashboard"


@pytest.mark.parametrize("query", [
    "week=", "week=%20", "week=%202026-09-28", "week=2026-09-28%20",
    "week=2026-10-06", "week=2026-02-30", "week=2026-9-28",
    "week=2099-01-05", "week=2020-01-06", "week=2026-10-12",
    "week=2026-09-28&week=2026-09-28", "week=2026-09-28&date=2026-09-28",
    "week=2026-09-28&api_key=secret", "week=%00", "week=%ff",
])
def test_weekly_route_rejects_invalid_parameters(browser, query):
    client, _auth, _database = browser
    response = _get(client, PATH + "?" + query)
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid event weekly report query"}
    assert response.headers["Cache-Control"].startswith("no-store")


@pytest.mark.parametrize("query", INVALID_PAGING_QUERIES)
def test_weekly_route_rejects_invalid_paging_before_loading_report(browser, monkeypatch, query):
    client, _auth, _database = browser
    import routes.event_radar_routes as routes

    monkeypatch.setattr(routes, "load_weekly_report", lambda *_a, **_k: pytest.fail("invalid paging read"))
    response = _get(client, PATH + "?" + query)
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid event weekly report query"}
    assert response.headers["Cache-Control"].startswith("no-store")


@pytest.mark.parametrize("week", ["", "2026-10-06", "2026-02-30", "2099-01-05"])
def test_weekly_paging_rejects_invalid_week_before_database_read(browser, monkeypatch, week):
    client, _auth, _database = browser
    import services.event_weekly_report as weekly

    monkeypatch.setattr(weekly, "get_read_only_connection", lambda: pytest.fail("invalid week read"))
    response = _get(client, PATH + f"?week={week}&block=new&page=2")
    assert response.status_code == 400


def test_weekly_auth_precedes_all_data_reads_and_query_keys_never_authorize(browser, monkeypatch):
    client, _auth, _database = browser
    import routes.event_radar_routes as routes

    monkeypatch.setattr(routes, "load_weekly_report", lambda *_a, **_k: pytest.fail("unauthenticated read"))
    for suffix in ("", "?week=2026-09-28", "?week=2026-09-28&block=new&page=2", "?api_key=" + KEY):
        denied = client.get(PATH + suffix, base_url=BASE)
        assert denied.status_code == 401
    redirected = client.get(SELECTED, base_url=BASE, headers={"Accept": "text/html"})
    assert redirected.status_code == 302
    assert redirected.headers["Location"] == "/dashboard/login?next=/dashboard/events/weekly%3Fweek%3D2026-09-28"
    for suffix in ("?week=2026-10-06", "?week=2026-09-28&week=2026-10-05", "?week=2026-09-28&tab=x"):
        invalid = client.get(PATH + suffix, base_url=BASE, headers={"Accept": "text/html"})
        assert invalid.headers["Location"] == "/dashboard/login?next=/dashboard/events/weekly"

    reordered = client.get(PATH + "?page=2&block=new&week=2026-09-28", base_url=BASE,
                           headers={"Accept": "text/html"})
    assert reordered.headers["Location"] == (
        "/dashboard/login?next=/dashboard/events/weekly%3Fweek%3D2026-09-28%26block%3Dnew%26page%3D2"
    )
    invalid = client.get(PAGED + "&api_key=" + KEY, base_url=BASE, headers={"Accept": "text/html"})
    assert invalid.headers["Location"] == "/dashboard/login?next=/dashboard/events/weekly"


def test_weekly_login_csrf_round_trip_preserves_monday(browser):
    client, _auth, database = browser
    _event(database)
    start = client.get(SELECTED, base_url=BASE, headers={"Accept": "text/html"})
    page = client.get(start.headers["Location"], base_url=BASE)
    assert _field(page, "next") == SELECTED
    bad_csrf = client.post("/dashboard/login", base_url=BASE, data={
        "api_key": KEY, "csrf_token": "wrong", "next": SELECTED,
    })
    assert bad_csrf.status_code == 403
    assert _field(bad_csrf, "next") == SELECTED
    bad_key = client.post("/dashboard/login", base_url=BASE, data={
        "api_key": "wrong", "csrf_token": _field(bad_csrf, "csrf_token"), "next": SELECTED,
    })
    assert bad_key.status_code == 401
    assert _field(bad_key, "next") == SELECTED
    logged_in = client.post("/dashboard/login", base_url=BASE, data={
        "api_key": KEY, "csrf_token": _field(bad_key, "csrf_token"), "next": _field(bad_key, "next"),
    })
    assert logged_in.status_code == 303
    assert logged_in.headers["Location"] == SELECTED
    report = client.get(logged_in.headers["Location"], base_url=BASE)
    assert report.status_code == 200
    body = report.get_data(as_text=True)
    assert "本周入库检查事件" in body and "事件周报 2026-09-28 — 2026-10-04" in body
    assert 'action="/dashboard/logout"' in body
    assert 'name="csrf_token"' in body
    assert body.count('data-highlight-event-id="') == 1
    assert "2026-09-30 09:59" in body and "入库 2026-09-30 10:00" in body
    assert "script-src 'none'" in report.headers["Content-Security-Policy"]
    assert report.headers["Referrer-Policy"] == "no-referrer"
    assert report.headers["Cache-Control"].startswith("no-store")


def test_weekly_paging_login_csrf_round_trip_preserves_week_block_and_page(browser):
    client, _auth, database = browser
    event_ids = {_event(database, f"分页登录检查 {index}") for index in range(21)}
    start = client.get(PAGED, base_url=BASE, headers={"Accept": "text/html"})
    page = client.get(start.headers["Location"], base_url=BASE)
    assert _field(page, "next") == PAGED
    bad_csrf = client.post("/dashboard/login", base_url=BASE, data={
        "api_key": KEY, "csrf_token": "wrong", "next": PAGED,
    })
    assert bad_csrf.status_code == 403 and _field(bad_csrf, "next") == PAGED
    bad_key = client.post("/dashboard/login", base_url=BASE, data={
        "api_key": "wrong", "csrf_token": _field(bad_csrf, "csrf_token"), "next": PAGED,
    })
    assert bad_key.status_code == 401 and _field(bad_key, "next") == PAGED
    logged_in = client.post("/dashboard/login", base_url=BASE, data={
        "api_key": KEY, "csrf_token": _field(bad_key, "csrf_token"), "next": _field(bad_key, "next"),
    })
    assert logged_in.status_code == 303 and logged_in.headers["Location"] == PAGED
    report = client.get(logged_in.headers["Location"], base_url=BASE)
    assert report.status_code == 200
    body = report.get_data(as_text=True)
    assert re.findall(r'data-event-id="([^"]+)"', body) == sorted(event_ids)[20:]
    assert re.findall(r'data-block="([^"]+)"', body) == ["new"]
    assert 'data-testid="weekly-highlights"' not in body
    assert f'href="{SELECTED}"' in body
    assert 'action="/dashboard/logout"' in body and 'name="csrf_token"' in body
    assert "script-src 'none'" in report.headers["Content-Security-Policy"]
    assert report.headers["Referrer-Policy"] == "no-referrer"
    assert report.headers["Cache-Control"].startswith("no-store")


@pytest.mark.parametrize("target", [SELECTED, PAGED])
def test_weekly_bad_header_cannot_fall_back_to_session(browser, monkeypatch, target):
    client, _auth, _database = browser
    assert _login(client).status_code == 303
    import routes.event_radar_routes as routes

    monkeypatch.setattr(routes, "load_weekly_report", lambda *_a, **_k: pytest.fail("bad header fell back to cookie"))
    response = client.get(target, base_url=BASE, headers={"Accept": "text/html", "X-API-Key": "wrong"})
    assert response.status_code == 401


def test_weekly_read_failure_is_a_private_503(browser, monkeypatch):
    client, _auth, _database = browser
    import services.event_weekly_report as weekly
    import sqlite3

    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError("secret database location /private/production.db")
        yield

    monkeypatch.setattr(weekly, "get_read_only_connection", unavailable)
    response = _get(client)
    assert response.status_code == 503
    assert response.get_json() == {"error": "Event weekly report unavailable"}
    assert "production.db" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"].startswith("no-store")


@pytest.mark.parametrize("target", [SELECTED, SELECTED + "&block=new&page=1"])
def test_weekly_request_only_reads_existing_rows(browser, monkeypatch, target):
    client, _auth, database = browser
    _event(database)
    import services.event_weekly_report as weekly

    statements = []
    original = weekly.get_read_only_connection

    @contextmanager
    def tracked_read():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    with database.get_connection() as conn:
        before = list(conn.iterdump())
    monkeypatch.setattr(weekly, "get_read_only_connection", tracked_read)
    response = _get(client, target)
    assert response.status_code == 200
    assert statements and any("SELECT" in statement.upper() for statement in statements)
    assert not any(re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", statement, re.I) for statement in statements)
    with database.get_connection() as conn:
        assert list(conn.iterdump()) == before


def test_weekly_overflow_links_to_single_block_pages_with_exact_selection(browser):
    client, _auth, database = browser
    event_ids = {_event(database, f"新增事件 {index} <img src=x onerror=alert(1)>") for index in range(41)}
    _event(database, "某公司否认项目传闻", priority=2.0)
    overview = _get(client).get_data(as_text=True)
    assert f'href="{PAGED.replace("&", "&amp;")}"' in overview
    for page_number, expected in ((1, sorted(event_ids)[:20]), (2, sorted(event_ids)[20:40]), (3, sorted(event_ids)[40:])):
        target = SELECTED + f"&block=new&page={page_number}"
        response = _get(client, target)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert re.findall(r'data-event-id="([^"]+)"', body) == expected
        assert re.findall(r'data-block="([^"]+)"', body) == ["new"]
        assert 'data-testid="weekly-highlights"' not in body
        assert f'href="{SELECTED}"' in body
        assert "<img src=x" not in body and "<script" not in body
        assert 'target="_blank" rel="noopener noreferrer"' in body
    missing_page = _get(client, SELECTED + "&block=new&page=4")
    assert missing_page.status_code == 400
    assert missing_page.get_json() == {"error": "Invalid event weekly report query"}


def test_weekly_default_current_week_navigation_and_coverage(browser):
    client, _auth, database = browser
    _event(database)
    previous = _get(client, PATH)
    body = previous.get_data(as_text=True)
    assert previous.status_code == 200
    assert 'value="2026-09-28"' in body
    assert 'data-testid="partial-week"' not in body
    assert 'data-testid="missing-dates"' in body and "2026-09-29" in body
    assert 'data-testid="coverage-incomplete"' not in body
    scope = re.search(r'data-testid="report-scope".*?</section>', body, re.S).group(0)
    warnings = re.findall(r'<span class="state-warning"[^>]*>(.*?)</span>', scope, re.S)
    assert len(warnings) == 1
    assert "2026-09-29" in warnings[0]
    assert body.count('data-testid="missing-dates"') == 1
    assert "非当周封存周报" in body and "事件标题与事件评分为当前值" in body
    assert "本周入库原文" in body and "本周发生" not in body
    current = _get(client, PATH + "?week=2026-10-05").get_data(as_text=True)
    assert 'data-testid="partial-week"' in current
    assert "后一周 →" not in current
    assert 'max="2026-10-05" step="7" required' in current


def test_weekly_summary_uses_visible_events_with_bounded_escaped_text(browser):
    client, _auth, database = browser
    for index in range(6):
        _event(database, f"事件 {index} <img src=x onerror=alert(1)>", priority=9 - index / 10)
    body = _get(client).get_data(as_text=True)
    highlight_ids = re.findall(r'data-highlight-event-id="([^"]+)"', body)
    assert len(highlight_ids) == 5
    assert len(set(highlight_ids)) == 5
    assert all(f'id="report-event-{event_id}"' in body for event_id in highlight_ids)
    assert "<img src=x" not in body and "<script" not in body
    assert 'target="_blank" rel="noopener noreferrer"' in body
    assert "尚未确认之处" in body and "下一验证点" in body


def test_weekly_truncation_is_visible_even_with_nonempty_event_blocks(browser, monkeypatch):
    client, _auth, database = browser
    import services.event_weekly_report as weekly

    _event(database, "第一条已入库记录")
    _event(database, "第二条已入库记录", day=1, month=10)
    monkeypatch.setattr(weekly, "MAX_WEEK_EVIDENCE_ROWS", 1)
    body = _get(client).get_data(as_text=True)
    assert 'class="event-card"' in body
    scope = re.search(r'data-testid="report-scope".*?</section>', body, re.S).group(0)
    assert 'data-testid="data-state"' in scope
    assert "超过读取上限 1 条" in scope and "周报不完整" in scope
    assert 'data-testid="coverage-incomplete"' not in scope
    assert "已入库，计数不完整" in scope and "None 条" not in scope
    missing = re.search(r'data-testid="missing-dates"[^>]*>([^<]+)', scope).group(1)
    assert "2026-10-01" not in missing


def test_weekly_notes_fold_without_hiding_current_week_and_freshness_limits(browser):
    client, _auth, database = browser
    _event(database, "周前已有覆盖记录", day=27)
    _event(database, "本周一入库记录", day=5, month=10)
    _event(database, "本周二最新入库记录", day=6, month=10)
    body = _get(client, PATH + "?week=2026-10-05").get_data(as_text=True)
    scope = re.search(r'data-testid="report-scope".*?</section>', body, re.S).group(0)
    assert 'data-testid="partial-week"' in scope
    assert 'data-testid="coverage-incomplete"' in scope
    assert 'data-testid="data-state"' not in scope
    assert "截止 2026-10-06 12:00:00" in scope
    assert 'data-testid="freshness"' in scope
    assert "事件标题与事件评分为当前值" in scope
    notes = re.search(r'<details\b([^>]*)>(.*?)</details>', body, re.S)
    assert notes and "open" not in notes.group(1)
    assert "<summary>口径说明</summary>" in notes.group(2)
    assert 'class="notes"' in notes.group(2)
    assert "仅按规范化标题完全一致关联" in notes.group(2)


def test_weekly_earliest_week_does_not_link_outside_history_and_post_is_rejected(browser):
    client, _auth, _database = browser
    body = _get(client, PATH + "?week=2025-10-06").get_data(as_text=True)
    assert 'value="2025-10-06"' in body
    assert "← 前一周" not in body
    assert client.post(PATH, base_url=BASE, headers={"X-API-Key": KEY}).status_code == 405


def test_report_navigation_and_daily_shared_blocks_preserve_daily_wording(browser):
    client, _auth, database = browser
    _event(database, "日报保留检查", month=10, day=5)
    daily = _get(client, "/dashboard/events/daily?date=2026-10-05").get_data(as_text=True)
    weekly = _get(client, PATH + "?week=2026-10-05").get_data(as_text=True)
    radar = _get(client, "/dashboard/events").get_data(as_text=True)
    assert "事件日报 · 2026-10-05" in daily
    assert "当日入库原文" in daily and "日报保留检查" in daily
    assert 'href="/dashboard/events/weekly"' in daily
    assert 'href="/dashboard/events/daily"' in weekly
    assert 'href="/dashboard/events/weekly"' in radar


def test_report_mobile_layout_keeps_headings_and_evidence_readable():
    """The scoped narrow layout must stack labels and counts at 320/360 px."""
    templates = Path(__file__).resolve().parents[1] / "templates"
    styles = (templates / "event_radar_report_styles.html").read_text()
    assert ".event-report .block-head h2 { flex: 0 0 auto; }" in styles
    assert "@media (max-width: 600px)" in styles
    narrow = styles.split("@media (max-width: 600px)", 1)[1]
    assert ".event-report .block-count { flex-basis: 100%; }" in narrow
    assert ".event-report dl.chain { grid-template-columns: minmax(0, 1fr);" in narrow
    for name in ("daily", "weekly"):
        page = (templates / f"event_radar_{name}.html").read_text()
        assert f'<body class="event-report {name}-report">' in page
        assert "include 'event_radar_report_blocks.html'" in page
        assert "include 'event_radar_report_styles.html'" in page
