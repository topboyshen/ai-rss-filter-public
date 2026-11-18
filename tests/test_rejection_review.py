"""Rejected-record review: bounded listing, per-feed verdicts, durable ledger."""

import re
import sqlite3
import time

import pytest

from tests.test_dashboard_browser_auth import (
    API_KEY,
    BASE_URL,
    _login,
    dashboard_browser as _dashboard_browser_fixture,
)


dashboard_browser = _dashboard_browser_fixture
PAGE_PATH = "/dashboard/rejections"
DECISION_PATH = PAGE_PATH + "/decision"
FEED_URL = "https://example.test/feed"
FEED_URL_STRICT = "https://example.test/strict-feed"


@pytest.fixture
def review_browser(dashboard_browser, monkeypatch):
    import routes.rejection_review_routes as routes

    cfg = {
        "threshold": {"pass": 7, "notify": 9},
        "feeds": [
            {"url": FEED_URL, "name": "测试源", "pass_threshold": 6.5},
            {"url": FEED_URL_STRICT, "name": "严格源", "pass_threshold": 8.0},
        ],
    }
    monkeypatch.setattr(routes, "_cfg", lambda: cfg)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    client, _auth, _security = dashboard_browser
    return client, cfg


def _insert(
    link,
    *,
    score,
    title="待复核文章",
    feed_url=FEED_URL,
    score_path="llm",
    review_reason="",
    logic="",
    created_at=None,
    strategy="score:rejection-review-test",
):
    from db import database

    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, feed_url, created_at, score, score_path, "
            "review_reason, logic, strategy_version, tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link,
                title,
                title,
                feed_url,
                created_at if created_at is not None else int(time.time()) - 60,
                score,
                score_path,
                review_reason,
                logic,
                strategy,
                "其他",
            ),
        )
        conn.commit()


def _open_page(client, path=PAGE_PATH):
    probe = client.get(
        PAGE_PATH, base_url=BASE_URL, headers={"Accept": "text/html"}
    )
    if probe.status_code != 200:
        assert _login(client, next_path=PAGE_PATH).status_code == 303
    page = client.get(path, base_url=BASE_URL, headers={"Accept": "text/html"})
    assert page.status_code == 200
    return page


def _csrf(page):
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        page.get_data(as_text=True),
    )
    assert match, "review page did not contain a CSRF token"
    return match.group(1)


def _decision_rows(link):
    from db import database

    with database.get_connection() as conn:
        return conn.execute(
            "SELECT * FROM article_review_decisions WHERE link = ? "
            "ORDER BY id",
            (link,),
        ).fetchall()


def test_page_requires_authentication(review_browser):
    client, _cfg = review_browser

    html = client.get(
        PAGE_PATH, base_url=BASE_URL, headers={"Accept": "text/html"}
    )
    assert html.status_code == 302
    assert "/dashboard/login" in html.headers["Location"]

    api = client.get(PAGE_PATH, base_url=BASE_URL)
    assert api.status_code == 401


def test_lists_rejects_with_rule_reason_and_model_text(review_browser):
    client, _cfg = review_browser
    now = int(time.time())
    _insert(
        "https://example.test/low",
        score=4.0,
        title="规则命中低分文章",
        review_reason="fast_pass_hint,boundary_score",
        logic="仅为行情复述，无新增订单或政策细节",
        created_at=now - 120,
    )
    _insert(
        "https://example.test/norule",
        score=5.0,
        title="只有模型自述的文章",
        logic="缺少点名的公司或政策",
        created_at=now - 180,
    )
    _insert(
        "https://example.test/passing",
        score=8.5,
        title="已达标文章",
        logic="新增订单",
        created_at=now - 240,
    )

    body = _open_page(client).get_data(as_text=True)

    assert "规则命中低分文章" in body
    assert "只有模型自述的文章" in body
    assert "已达标文章" not in body
    assert "规则提示可快速放行" in body
    assert "分数位于阈值边界" in body
    assert "模型自述" in body
    assert "仅为行情复述，无新增订单或政策细节" in body
    assert "低于来源达标阈值 2 条" in body
    assert "有规则理由" in body


def test_rejection_uses_each_feeds_own_threshold(review_browser):
    client, _cfg = review_browser
    now = int(time.time())
    _insert(
        "https://example.test/strict-edge",
        score=7.2,
        title="严格源边界文章",
        feed_url=FEED_URL_STRICT,
        created_at=now - 120,
    )
    _insert(
        "https://example.test/normal-edge",
        score=7.2,
        title="常规源边界文章",
        feed_url=FEED_URL,
        created_at=now - 180,
    )

    body = _open_page(client).get_data(as_text=True)

    assert "严格源边界文章" in body
    assert "常规源边界文章" not in body


def test_window_excludes_records_older_than_72_hours(review_browser):
    client, _cfg = review_browser
    now = int(time.time())
    _insert(
        "https://example.test/old",
        score=1.0,
        title="窗口外旧文章",
        created_at=now - 73 * 3600,
    )
    _insert(
        "https://example.test/fresh",
        score=1.0,
        title="窗口内新文章",
        created_at=now - 3600,
    )

    body = _open_page(client).get_data(as_text=True)

    assert "窗口内新文章" in body
    assert "窗口外旧文章" not in body


def test_filters_and_invalid_query_parameters(review_browser):
    client, _cfg = review_browser
    _insert(
        "https://example.test/rule",
        score=4.0,
        title="规则标记样本",
        review_reason="high_value_source",
    )
    _insert(
        "https://example.test/none",
        score=4.0,
        title="模型自述样本",
    )

    rule_body = _open_page(
        client, PAGE_PATH + "?reason=rule"
    ).get_data(as_text=True)
    assert "规则标记样本" in rule_body
    assert "模型自述样本" not in rule_body

    none_body = _open_page(
        client, PAGE_PATH + "?reason=none"
    ).get_data(as_text=True)
    assert "模型自述样本" in none_body
    assert "规则标记样本" not in none_body

    path_body = _open_page(
        client, PAGE_PATH + "?path=llm"
    ).get_data(as_text=True)
    assert "规则标记样本" in path_body

    for bad in (
        "?unknown=1",
        "?page=0",
        "?page=999",
        "?path=bad-path",
        "?decision=maybe",
    ):
        response = client.get(
            PAGE_PATH + bad,
            base_url=BASE_URL,
            headers={"Accept": "text/html"},
        )
        assert response.status_code == 400, bad


def test_restore_writes_snapshot_and_never_rewrites_score(review_browser):
    client, _cfg = review_browser
    link = "https://example.test/restore"
    _insert(
        link,
        score=4.0,
        title="应被恢复的文章",
        score_path="stage2",
        review_reason="fast_pass_hint",
        logic="有新增订单，但模型判为低分",
    )
    page = _open_page(client)
    token = _csrf(page)

    response = client.post(
        DECISION_PATH,
        base_url=BASE_URL,
        data={
            "csrf_token": token,
            "link": link,
            "action": "restore",
            "note": "有新增订单",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith("?decided=restore")

    rows = _decision_rows(link)
    assert len(rows) == 1
    assert rows[0]["decision"] == "restored"
    assert rows[0]["note"] == "有新增订单"
    assert rows[0]["score"] == 4.0
    assert rows[0]["pass_threshold"] == 6.5
    assert rows[0]["score_path"] == "stage2"
    assert rows[0]["review_reason"] == "fast_pass_hint"
    assert rows[0]["raw_title"] == "应被恢复的文章"

    from db import database

    with database.get_connection() as conn:
        cache = conn.execute(
            "SELECT score, score_path FROM news_cache WHERE link = ?", (link,)
        ).fetchone()
    assert cache["score"] == 4.0
    assert cache["score_path"] == "stage2"

    body = client.get(
        response.headers["Location"],
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    ).get_data(as_text=True)
    assert "已恢复（人工放行）" in body
    assert "已记录：恢复（人工放行）" in body

    restored_body = client.get(
        PAGE_PATH + "?decision=restored",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    ).get_data(as_text=True)
    assert "应被恢复的文章" in restored_body
    confirmed_body = client.get(
        PAGE_PATH + "?decision=confirmed",
        base_url=BASE_URL,
        headers={"Accept": "text/html"},
    ).get_data(as_text=True)
    assert "应被恢复的文章" not in confirmed_body


def test_session_decision_requires_csrf(review_browser):
    client, _cfg = review_browser
    link = "https://example.test/csrf"
    _insert(link, score=4.0, title="需要 CSRF 的文章")
    _open_page(client)

    response = client.post(
        DECISION_PATH,
        base_url=BASE_URL,
        data={"link": link, "action": "restore"},
    )
    assert response.status_code == 403
    assert _decision_rows(link) == []


def test_header_auth_can_decide_without_csrf(review_browser):
    client, _cfg = review_browser
    link = "https://example.test/header-auth"
    _insert(link, score=4.0, title="头部认证文章")

    response = client.post(
        DECISION_PATH,
        base_url=BASE_URL,
        headers={"X-API-Key": API_KEY},
        data={"link": link, "action": "confirm"},
    )
    assert response.status_code == 303
    rows = _decision_rows(link)
    assert len(rows) == 1
    assert rows[0]["decision"] == "confirmed"


def test_decision_rejects_missing_passing_and_stale_records(review_browser):
    client, _cfg = review_browser
    _insert("https://example.test/pass", score=8.5, title="达标文章")
    _insert(
        "https://example.test/stale",
        score=1.0,
        title="过期文章",
        created_at=int(time.time()) - 73 * 3600,
    )
    _open_page(client)

    for link in (
        "https://example.test/missing",
        "https://example.test/pass",
        "https://example.test/stale",
    ):
        response = client.post(
            DECISION_PATH,
            base_url=BASE_URL,
            headers={"X-API-Key": API_KEY},
            data={"link": link, "action": "restore"},
        )
        assert response.status_code == 400, link
        assert _decision_rows(link) == []


def test_switching_and_revoking_keep_append_only_history(review_browser):
    client, _cfg = review_browser
    link = "https://example.test/switch"
    _insert(link, score=4.0, title="改判与撤销")
    _open_page(client)
    headers = {"X-API-Key": API_KEY}

    assert (
        client.post(
            DECISION_PATH,
            base_url=BASE_URL,
            headers=headers,
            data={"link": link, "action": "restore"},
        ).status_code
        == 303
    )
    assert (
        client.post(
            DECISION_PATH,
            base_url=BASE_URL,
            headers=headers,
            data={"link": link, "action": "confirm"},
        ).status_code
        == 303
    )

    rows = _decision_rows(link)
    assert [row["decision"] for row in rows] == ["restored", "confirmed"]
    assert rows[0]["revoked_at"] is not None
    assert rows[1]["revoked_at"] is None

    assert (
        client.post(
            DECISION_PATH,
            base_url=BASE_URL,
            headers=headers,
            data={"link": link, "action": "revoke"},
        ).status_code
        == 303
    )
    rows = _decision_rows(link)
    assert len(rows) == 2
    assert all(row["revoked_at"] is not None for row in rows)

    with pytest.raises(sqlite3.IntegrityError):
        from db import database

        with database.get_connection() as conn:
            conn.execute(
                "UPDATE article_review_decisions SET note = 'rewrite' "
                "WHERE link = ?",
                (link,),
            )
    with pytest.raises(sqlite3.IntegrityError):
        from db import database

        with database.get_connection() as conn:
            conn.execute(
                "DELETE FROM article_review_decisions WHERE link = ?", (link,)
            )


def test_page_read_never_opens_a_write_connection(review_browser, monkeypatch):
    client, _cfg = review_browser
    _insert("https://example.test/readonly", score=4.0, title="只读页面")
    _open_page(client)

    from db import database

    def forbidden(*_args, **_kwargs):
        pytest.fail("a browser read invoked a write-capable connection")

    monkeypatch.setattr(database, "get_connection", forbidden)
    response = client.get(
        PAGE_PATH, base_url=BASE_URL, headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
