"""Exercise the public entry and authenticated HTML against real cache reads."""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests import test_dashboard_browser_auth as browser_auth_tests
from tests.test_dashboard_browser_auth import (
    API_KEY,
    BASE_URL,
    _login,
)


dashboard_browser = browser_auth_tests.dashboard_browser
PAGE_PATH = "/dashboard/neutral-records"
PAGE_URL = BASE_URL + PAGE_PATH
STRATEGY = "score:neutral-route-test"
FEED_URL = "https://example.test/feed"
FACTS_TEXT = "保守处理说明：事实表述未通过现有来源校验"
UNKNOWN_TEXT = "保守处理说明：具体原因未完整记录"


@pytest.fixture
def neutral_browser(dashboard_browser, monkeypatch):
    import routes.neutral_records_routes as routes
    import routes.security as security

    cfg = {"feeds": [{"url": FEED_URL, "name": "测试订阅源"}]}
    contract_calls = []

    def resolve(config, *, register_snapshot):
        assert config is cfg
        assert register_snapshot is False
        contract_calls.append(config)
        return {"scoring_strategy_version": STRATEGY}

    monkeypatch.setattr(routes, "_cfg", lambda: cfg)
    monkeypatch.setattr(routes, "resolve_scoring_contract", resolve)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    with security._rate_limit_lock:
        security._rate_limit_store.clear()
    client, _auth, _security = dashboard_browser
    return client, cfg, contract_calls


def _cache_row(title, *, reason="", path="llm_safe_neutral_grounding",
               strategy=STRATEGY, link=None, created_at=None):
    """Direct SQL fixtures model historical/invalid rows without repairing them."""
    from db import database

    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO news_cache "
            "(link, title, raw_title, feed_url, created_at, score_path, "
            "strategy_version, safe_neutral_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (link or f"https://example.test/{title}", title, title, FEED_URL,
             created_at or int(time.time()) - 60, path, strategy, reason),
        )
        conn.commit()


def _persist_facts_record():
    """Use the production writer with a matching non-selected signal contract."""
    from db import database

    now = int(time.time()) - 120
    link = "https://example.test/persisted-facts"
    signal = {
        "link": link, "raw_title": "真实写入事实记录", "score": 4.0,
        "score_path": "llm_safe_neutral_grounding", "direction": "中性",
        "score_confidence": 0.0, "strategy_version": STRATEGY,
        "mapping_version": "asset-map:route-test", "available_at": now,
        "scored_at": now, "aggregation_threshold": 6, "tag": "其他",
        "logic": "来源未显示明确投资催化", "selected": 0,
        "stage1_score": 4.0, "stage1_model": "qwen",
    }
    database.set_cached_entry(
        link, "真实写入事实记录", "真实写入事实记录", 4.0,
        "其他", "来源未显示明确投资催化", feed_url=FEED_URL,
        score_path="llm_safe_neutral_grounding", strategy_version=STRATEGY,
        direction="中性", score_confidence=0.0, model_name="qwen",
        stage1_score=4.0, stage1_model="qwen", safe_neutral_reason="facts",
        created_at=now, backtest_signal=signal,
    )
    return now


def _article_for(body, title):
    return next(article for article in re.findall(
        r"<article>.*?</article>", body, flags=re.S
    ) if title in article)


def _assert_private_headers(response):
    assert response.mimetype == "text/html"
    assert response.headers["Cache-Control"].startswith("no-store")
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_anonymous_browser_login_returns_to_real_html_page(neutral_browser):
    client, _cfg, calls = neutral_browser
    response = client.get(
        PAGE_PATH, base_url=BASE_URL, headers={"Accept": "text/html"}
    )
    assert response.status_code == 302
    assert response.headers["Location"] == f"/dashboard/login?next={PAGE_PATH}"
    assert calls == []

    login = _login(client, next_path=PAGE_PATH)
    assert login.status_code == 303
    assert login.headers["Location"] == PAGE_PATH
    response = client.get(login.headers["Location"], base_url=BASE_URL)
    assert response.status_code == 200
    assert "近24小时现存缓存中暂无当前契约" in response.get_data(as_text=True)
    assert "<html" in response.get_data(as_text=True)
    assert len(calls) == 1
    _assert_private_headers(response)


def test_query_key_never_authorizes_and_bad_header_never_uses_cookie(neutral_browser):
    client, _cfg, calls = neutral_browser
    for accept, expected in [("text/html", 302), ("application/json", 401)]:
        response = client.get(
            f"{PAGE_PATH}?key={API_KEY}", base_url=BASE_URL,
            headers={"Accept": accept},
        )
        assert response.status_code == expected
    assert calls == []

    assert _login(client, next_path=PAGE_PATH).status_code == 303
    response = client.get(
        PAGE_PATH, base_url=BASE_URL,
        headers={"Accept": "text/html", "X-API-Key": "incorrect"},
    )
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}
    assert calls == []


def test_real_cache_contract_reason_and_time_reach_authenticated_html(neutral_browser):
    client, _cfg, calls = neutral_browser
    from db import database

    stamp = _persist_facts_record()
    _cache_row("主题记录", reason="theme")
    _cache_row("格式记录", reason="contract", path="stage2_safe_neutral_contract")
    _cache_row("初次记录", reason="initial", path="llm_safe_neutral_initial")
    _cache_row("空原因记录", path="llm_safe_neutral_contract")
    _cache_row("未知原因记录", reason="unrecorded-diagnostic")
    _cache_row("旧契约隐藏", reason="facts", strategy="score:old")
    _cache_row("普通路径隐藏", reason="facts", path="llm")
    _cache_row("过期记录隐藏", reason="facts", created_at=int(time.time()) - 90000)

    assert _login(client, next_path=PAGE_PATH).status_code == 303
    response = client.get(PAGE_PATH, base_url=BASE_URL)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert len(calls) == 1
    assert "本次显示 6 条" in body and STRATEGY in body
    assert FACTS_TEXT in _article_for(body, "真实写入事实记录")
    assert "主题关联未通过现有来源校验" in _article_for(body, "主题记录")
    assert "输出格式或字段未通过校验" in _article_for(body, "格式记录")
    assert "初次输出为保守中性，具体原因未记录" in _article_for(body, "初次记录")
    assert UNKNOWN_TEXT in _article_for(body, "空原因记录")
    assert UNKNOWN_TEXT in _article_for(body, "未知原因记录")
    assert all(title not in body for title in ("旧契约隐藏", "普通路径隐藏", "过期记录隐藏"))
    assert "unrecorded-diagnostic" not in body
    time_label = datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai"))
    assert f"收录 {time_label:%Y-%m-%d %H:%M}（北京时间）" in body
    assert "订阅来源：测试订阅源" in body
    assert "收录时间不是发布时间或评分时间" in body
    with database.get_read_only_connection() as conn:
        reasons = dict(conn.execute(
            "SELECT raw_title, safe_neutral_reason FROM news_cache"
        ).fetchall())
    assert reasons["真实写入事实记录"] == "facts"
    assert reasons["空原因记录"] == ""
    assert reasons["未知原因记录"] == "unrecorded-diagnostic"
    _assert_private_headers(response)


def test_record_output_escapes_text_and_omits_dangerous_links(neutral_browser):
    client, cfg, _calls = neutral_browser
    cfg["feeds"][0]["name"] = '<img src=x onerror="alert(1)">来源'
    _cache_row('营收 <5% & "引号" <x-neutral>', reason="facts",
               link="javascript:alert(1)")
    _cache_row("安全链接", reason="theme", link="https://example.test/a?x=1&y=2")
    response = client.get(
        PAGE_PATH, base_url=BASE_URL, headers={"X-API-Key": API_KEY}
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "营收 &lt;5% &amp;" in body and "&lt;x-neutral&gt;" in body
    assert "&lt;img" in body and "<img" not in body
    assert "javascript:" not in body
    assert "https://example.test/a?x=1&amp;y=2" in body
    assert "<script" not in body and "<iframe" not in body
    _assert_private_headers(response)


@pytest.mark.parametrize("query", ["hours=48", "limit=1", "strategy_version=old", "format=json"])
def test_authenticated_view_rejects_query_scope_changes(neutral_browser, query):
    client, _cfg, calls = neutral_browser
    response = client.get(
        f"{PAGE_PATH}?{query}", base_url=BASE_URL, headers={"X-API-Key": API_KEY}
    )
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("failure", ["database", "contract"])
def test_unavailable_is_not_reported_as_empty(neutral_browser, monkeypatch, tmp_path, failure):
    from db import database
    import routes.neutral_records_routes as routes

    client, _cfg, _calls = neutral_browser
    if failure == "database":
        missing = tmp_path / "missing-neutral.db"
        monkeypatch.setattr(database, "DB_PATH", str(missing))
    else:
        def unavailable(*_args, **_kwargs):
            raise RuntimeError("private contract diagnostic must not leak")
        monkeypatch.setattr(routes, "resolve_scoring_contract", unavailable)
    response = client.get(
        PAGE_PATH, base_url=BASE_URL, headers={"X-API-Key": API_KEY}
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "记录暂不可读取，无法判断是否存在保守处理记录" in body
    assert "暂无当前契约" not in body
    assert "private contract diagnostic" not in body
    if failure == "database":
        assert not missing.exists()
        assert str(missing) not in body
    _assert_private_headers(response)


def _backtest_result():
    return {
        "strategy_version": STRATEGY, "mapping_version": "asset-map:route-test",
        "requested_days": 90, "actual_window_days": 0, "deduped_samples": 0,
        "unique_signal_days": 0, "history_status": "accumulating",
        "selection_policy": "stored_aggregation_threshold", "buckets": [],
    }


def _assert_static_entry(body):
    match = re.search(r'<details\b([^>]*)>\s*<summary[^>]*>保守处理记录</summary>', body)
    assert match and "open" not in match.group(1).split()
    assert f'href="{PAGE_URL}"' in body
    assert "查看记录（需后台登录）" in body
    assert "<script" not in body and "<iframe" not in body
    assert "?key=" not in body and "?token=" not in body
    assert "公共卡片不应读取这条私有记录" not in body


@pytest.mark.parametrize("query", ["days=90", "days=90&view=compact"])
def test_score_backtest_http_contains_only_static_entry(neutral_browser, monkeypatch, query):
    from db import database
    import routes.dashboard_routes as dashboard_routes

    client, _cfg, calls = neutral_browser
    _cache_row("公共卡片不应读取这条私有记录", reason="facts")
    result = _backtest_result()
    with dashboard_routes._BACKTEST_CACHE_LOCK:
        dashboard_routes._BACKTEST_CACHE.clear()
    monkeypatch.setattr(dashboard_routes, "_backtest_cache_contract",
                        lambda: (STRATEGY, "asset-map:route-test"))
    monkeypatch.setattr(dashboard_routes, "run_score_backtest", lambda **_kwargs: result)
    monkeypatch.setattr(dashboard_routes, "run_mapping_validation", lambda **_kwargs: None)
    monkeypatch.setattr(dashboard_routes, "bridge_preview_data_version", lambda *_args: "none")

    def forbidden(*_args, **_kwargs):
        pytest.fail("public widget must not query private cache records")

    monkeypatch.setattr(database.sqlite3, "connect", forbidden)
    response = client.get(f"/score-backtest?{query}", base_url=BASE_URL)
    assert response.status_code == 200
    _assert_static_entry(response.get_data(as_text=True))
    assert calls == []


def test_model_quality_snapshot_http_remains_file_only(neutral_browser, monkeypatch, tmp_path):
    from db import database
    import routes.dashboard_routes as dashboard_routes
    import services.dashboard_snapshots as snapshots
    from services.backtest import render_backtest_compact_html

    client, _cfg, calls = neutral_browser
    _cache_row("公共卡片不应读取这条私有记录", reason="facts")
    directory = tmp_path / "dashboard-snapshots"
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(directory))
    monkeypatch.setenv("AI_FILTER_VERSION", "neutral-route-test")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    snapshots.write_dashboard_snapshot(
        "model_quality", render_backtest_compact_html(_backtest_result()),
        generated_at=_fresh_snapshot_generated_at(), strategy_version=STRATEGY,
        mapping_version="asset-map:route-test", data_version="1",
        snapshot_dir=directory,
    )
    monkeypatch.setenv("AI_FILTER_ROLE", "web")

    def forbidden(*_args, **_kwargs):
        pytest.fail("snapshot serving must not open SQLite or compute live")

    monkeypatch.setattr(database.sqlite3, "connect", forbidden)
    monkeypatch.setattr(dashboard_routes, "_backtest_cache_contract", forbidden)
    monkeypatch.setattr(dashboard_routes, "run_score_backtest", forbidden)
    monkeypatch.setattr(dashboard_routes, "run_mapping_validation", forbidden)
    response = client.get("/score-backtest?days=90&view=compact", base_url=BASE_URL)
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    _assert_static_entry(response.get_data(as_text=True))
    assert calls == []
def _fresh_snapshot_generated_at() -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
