"""Fresh homepage snapshots must not hide a partial collection attempt."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json

from flask import Flask
import pytest

from services.ingestion_status import describe_refresh_coverage, read_refresh_coverage
from test_home_news_reading import _entry
from test_snapshot_time_layout import _reading_shell, _root_signature


def _attempt(status="partial", *, source="华尔街见闻全球"):
    now = int(datetime.now(timezone.utc).timestamp())
    failed = 0 if status == "success" else 1
    return {"version": 1, "status": status, "started_at": now - 300,
            "finished_at": now - 1, "enabled_sources": 20,
            "succeeded_sources": 20 - failed, "failed_sources": failed,
            "failures": [] if not failed else [{"source": source, "error": "private URL"}]}


@pytest.fixture(autouse=True)
def _reset_limiter():
    from routes import security

    security._rate_limit_store.clear()
    yield
    security._rate_limit_store.clear()


@pytest.mark.parametrize("status,state,count", [
    ("partial", "incomplete", "19/20"), ("success", "complete", "20/20"),
])
def test_valid_latest_attempt_has_timestamp_and_separate_completeness(status, state, count):
    actual_state, text = describe_refresh_coverage(_attempt(status))
    assert actual_state == state
    assert count in text and "北京时间；完成时刻" in text
    if status == "partial":
        assert "华尔街见闻全球" in text
        assert "页面快照新鲜不代表采集完整" in text
        assert "private URL" not in text


@pytest.mark.parametrize("changed", [
    {"succeeded_sources": True}, {"enabled_sources": "20"},
    {"succeeded_sources": 20}, {"failed_sources": -1},
    {"status": "success"}, {"status": "unknown"},
    {"finished_at": 0}, {"finished_at": 10**15},
    {"finished_at": "not-a-time"}, {"started_at": 10**15},
    {"started_at": 0}, {"version": 2}, {"status": []},
])
def test_invalid_or_inconsistent_metadata_never_claims_complete(changed):
    state, text = describe_refresh_coverage({**_attempt(), **changed})
    assert state == "unknown"
    assert "状态未知" in text and "20/20" not in text


def test_zero_sources_is_not_successful_collection():
    result = {**_attempt(), "status": "noop", "enabled_sources": 0,
              "succeeded_sources": 0, "failed_sources": 0, "failures": []}
    state, text = describe_refresh_coverage(result)
    assert state == "noop" and "不代表采集完成" in text


def test_failed_collection_without_names_keeps_counts_but_marks_missing_details():
    result = {**_attempt(), "status": "failed", "succeeded_sources": 0,
              "failed_sources": 20, "failures": None}
    state, text = describe_refresh_coverage(result)
    assert state == "incomplete" and "0/20" in text
    assert "异常源：未记录，详情暂不可用" in text


def test_partial_source_distinguishes_missing_rows_and_pending_scoring():
    result = {**_attempt(), "partial_sources": 1, "failed_sources": 0,
              "source_unrecognized_entries": 2, "score_deferred_entries": 3}

    state, text = describe_refresh_coverage(result)

    assert state == "incomplete" and "19/20" in text
    assert "部分完成 1 源" in text and "未识别内容 2 条" in text and "待评分 3 条" in text
    assert "抓取／处理失败 0 源" in text and "华尔街见闻全球" in text


def test_only_partial_sources_remain_usable_but_cannot_claim_complete():
    result = {**_attempt(), "succeeded_sources": 0, "partial_sources": 20,
              "failed_sources": 0, "score_deferred_entries": 20}
    state, text = describe_refresh_coverage(result)
    assert state == "incomplete" and "0/20" in text and "待评分 20 条" in text


@pytest.mark.parametrize("changes", [
    {"partial_sources": True}, {"partial_sources": -1}, {"partial_sources": 21},
    {"source_unrecognized_entries": "1"}, {"score_deferred_entries": -1},
    {"status": "success", "succeeded_sources": 20, "failed_sources": 0,
     "score_deferred_entries": 1},
])
def test_invalid_partial_counters_are_unknown(changes):
    state, text = describe_refresh_coverage({**_attempt(), **changes})
    assert state == "unknown" and "状态未知" in text


def _publish_coverage(tmp_path, monkeypatch, original, *, stale=False, legacy=False):
    from services import dashboard_snapshots as snapshots
    from routes.dashboard_routes import _prepend_realtime_ingestion_coverage

    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    html = original if legacy else _prepend_realtime_ingestion_coverage(original)[0]
    generated = (datetime.now(timezone.utc) - timedelta(hours=8 if stale else 0, minutes=1)).isoformat()
    snapshots.write_dashboard_snapshot(
        "realtime", html, generated_at=generated, strategy_version="score:test",
        mapping_version="mapping:test", data_version="1", snapshot_dir=tmp_path / "snapshots",
        db_path=tmp_path / "cache.db",
    )
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    return (tmp_path / "snapshots" / "realtime.json").read_bytes()


def _client_with_snapshot(tmp_path, monkeypatch, *, stale=False, cards=1, legacy=False):
    from db import database
    import sse_engine

    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("AI_FILTER_VERSION", "my-ai-filter:coverage-test")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    database.init_db()
    original = _reading_shell([
        _entry(f"普通公司{i}发布例行公告", link=f"https://example.test/{i}")
        for i in range(cards)
    ])
    stored = _publish_coverage(tmp_path, monkeypatch, original, stale=stale, legacy=legacy)
    app = Flask(__name__)
    app.add_url_rule("/glance-sse", view_func=sse_engine.glance_sse_widget)
    return app.test_client(), original, stored


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("cards", [0, 1, 3, 30])
def test_fresh_or_stale_snapshot_displays_latest_partial_without_live_work(tmp_path, monkeypatch, stale, cards):
    from db import database
    import sse_engine

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch, stale=stale, cards=cards)
    database.set_app_meta("last_full_refresh", str(int(datetime.now(timezone.utc).timestamp())))
    database.set_app_meta("last_refresh_result", json.dumps(_attempt()))
    calls = []
    original_connection = database.get_read_only_connection

    @contextmanager
    def counted_read():
        calls.append("connect")
        with original_connection() as conn:
            conn.set_trace_callback(calls.append)
            yield conn

    def forbidden(*_args, **_kwargs):
        pytest.fail("homepage coverage must not load cards, trigger refresh, or write metadata")

    monkeypatch.setattr(database, "get_read_only_connection", counted_read)
    stored = _publish_coverage(tmp_path, monkeypatch, original, stale=stale)
    assert calls.count("connect") == 1
    selects = [statement for statement in calls if statement.startswith("SELECT")]
    assert len(selects) == 1 and "last_refresh_result" in selects[0]
    monkeypatch.setattr(database, "get_read_only_connection", forbidden)
    monkeypatch.setattr(database, "get_connection", forbidden)
    monkeypatch.setattr(sse_engine, "_load_from_db", forbidden)
    response = client.get("/glance-sse")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == ("stale" if stale else "ready")
    assert response.headers["X-Card-Ingestion-State"] == "incomplete"
    assert "最近采集：19/20" in body and "异常源：华尔街见闻全球" in body
    assert "20/20" not in body  # Older full success must never replace latest partial.
    assert body.count('class="dashboard-ingestion-coverage"') == 1
    assert _root_signature(body) == _root_signature(original)
    assert body.count('class="dashboard-snapshot-warning"') == int(stale)
    assert original[original.index('<div id="sse-container"'):] in body
    assert "随页面快照发布" in body
    assert (tmp_path / "snapshots" / "realtime.json").read_bytes() == stored


def test_new_partial_replaces_complete_when_next_still_fresh_snapshot_is_published(tmp_path, monkeypatch):
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch)
    database.set_app_meta("last_refresh_result", json.dumps(_attempt("success")))
    complete = _publish_coverage(tmp_path, monkeypatch, original)
    first = client.get("/glance-sse")
    assert first.headers["X-Card-Ingestion-State"] == "complete"
    assert "最近采集：20/20" in first.get_data(as_text=True)
    database.set_app_meta("last_refresh_result", json.dumps(_attempt()))
    # File-only Web does not pretend to observe metadata not yet published.
    assert client.get("/glance-sse").headers["X-Card-Ingestion-State"] == "complete"
    partial = _publish_coverage(tmp_path, monkeypatch, original)
    second = client.get("/glance-sse")
    assert second.headers["X-Card-Snapshot-State"] == "ready"
    assert second.headers["X-Card-Ingestion-State"] == "incomplete"
    assert "最近采集：19/20" in second.get_data(as_text=True)
    assert partial != complete
    assert (tmp_path / "snapshots" / "realtime.json").read_bytes() == partial


@pytest.mark.parametrize("raw", [None, "broken-json", "[]", "{}", "x" * 64_001])
def test_missing_or_corrupt_attempt_keeps_news_with_unknown_coverage(tmp_path, monkeypatch, raw):
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch)
    if raw is not None:
        database.set_app_meta("last_refresh_result", raw)
    _publish_coverage(tmp_path, monkeypatch, original)
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web DB read"))
    response = client.get("/glance-sse")
    assert response.status_code == 200 and response.headers["X-Card-Snapshot-State"] == "ready"
    assert response.headers["X-Card-Ingestion-State"] == "unknown"
    body = response.get_data(as_text=True)
    assert "最近采集：状态未知" in body and "普通公司0发布例行公告" in body


def test_read_failure_keeps_news_and_source_names_are_escaped(tmp_path, monkeypatch):
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch)
    database.set_app_meta("last_refresh_result", json.dumps(_attempt(source='<img src=x onerror="alert(1)">')))
    _publish_coverage(tmp_path, monkeypatch, original)
    body = client.get("/glance-sse").get_data(as_text=True)
    assert "&lt;img" in body and '<img src=x' not in body

    def failed_read():
        raise OSError("database unavailable")

    monkeypatch.setattr(database, "get_read_only_connection", failed_read)
    _publish_coverage(tmp_path, monkeypatch, original)
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web DB read"))
    response = client.get("/glance-sse")
    assert response.status_code == 200
    assert response.headers["X-Card-Ingestion-State"] == "unknown"
    assert "普通公司0发布例行公告" in response.get_data(as_text=True)


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    from db import database

    path = tmp_path / "absent.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    assert read_refresh_coverage()[0] == "unknown"
    assert not path.exists()


@pytest.mark.parametrize("marker", [
    "", '<span class="dashboard-ingestion-coverage" data-state="invalid">20/20</span>',
    '<span class="dashboard-ingestion-coverage" data-state="invalid" data-state="complete">20/20</span>',
    '<span class="dashboard-ingestion-coverage" data-state="complete" data-state="unknown">20/20</span>',
    '<span class="dashboard-ingestion-coverage" data-state="complete">20/20</span>' * 2,
])
def test_legacy_or_invalid_published_marker_is_unknown_without_web_database(tmp_path, monkeypatch, marker):
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch, legacy=True)
    html = original
    if marker:
        from routes.dashboard_routes import _READING_WINDOW_START

        end = _READING_WINDOW_START.match(html).end()
        html = html[:end] + marker + html[end:]
    stored = _publish_coverage(tmp_path, monkeypatch, html, legacy=True)
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web DB read"))
    monkeypatch.setattr(database, "get_connection", lambda: pytest.fail("Web DB write"))
    response = client.get("/glance-sse")
    body = response.get_data(as_text=True)
    assert response.headers["X-Card-Ingestion-State"] == "unknown"
    assert "最近采集：状态未知" in body and "20/20" not in body
    assert body.count('class="dashboard-ingestion-coverage"') == 1
    assert _root_signature(body) == _root_signature(original)
    assert (tmp_path / "snapshots" / "realtime.json").read_bytes() == stored


@pytest.mark.parametrize("source", [
    "http://internal.invalid/feed?api_key=synthetic-secret",
    "https://synthetic-user:synthetic-secret@internal.invalid/feed",
    "//internal.invalid/feed?token=synthetic-secret",
    "redis://internal.invalid:6379/0",
    "API_KEY=synthetic-secret",
    "api key=synthetic-secret",
    "Authorization: Bearer synthetic-secret",
    "Cookie: session=synthetic-secret",
    "密钥：synthetic-secret",
    "https%3A%2F%2Finternal.invalid%2Ffeed%3Fapi_key%3Dsynthetic-secret",
    "internal.invalid/feed?access_token=synthetic-secret",
    "华尔街见闻全球 " + "名" * 210 + " api_key=synthetic-secret",
])
def test_private_source_labels_never_reach_public_response(tmp_path, monkeypatch, source):
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch)
    database.set_app_meta("last_refresh_result", json.dumps(_attempt(source=source)))
    _publish_coverage(tmp_path, monkeypatch, original)
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web DB read"))
    response = client.get("/glance-sse")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers["X-Card-Ingestion-State"] == "incomplete"
    assert "19/20" in body and "来源名不可公开，详情暂不可用" in body
    assert "synthetic-secret" not in body and "internal.invalid" not in body
    assert "普通公司0发布例行公告" in body


def test_private_fallback_does_not_hide_safe_publisher_names(tmp_path, monkeypatch):
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch)
    attempt = {**_attempt(), "succeeded_sources": 18, "failed_sources": 2,
               "failures": [{"source": "华尔街见闻全球"},
                            {"source": "http://internal.invalid?api_key=synthetic-secret"}]}
    database.set_app_meta("last_refresh_result", json.dumps(attempt))
    _publish_coverage(tmp_path, monkeypatch, original)
    body = client.get("/glance-sse").get_data(as_text=True)
    assert "18/20" in body and "华尔街见闻全球（另有来源名不可公开）" in body
    assert "synthetic-secret" not in body and "internal.invalid" not in body


def test_real_refresh_writer_url_fallback_is_not_a_public_publisher_name(tmp_path, monkeypatch):
    from services import refresh
    from db import database

    client, original, _stored = _client_with_snapshot(tmp_path, monkeypatch)
    private_url = "http://internal.invalid/feed?api_key=synthetic-secret"
    cfg = {"feeds": [{"url": private_url}, {"url": "https://example.test/news", "name": "正常发布方"}]}
    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    monkeypatch.setattr(refresh, "resolve_scoring_contract", lambda value, **_kwargs: value)
    monkeypatch.setattr(
        refresh, "resolve_prompt", lambda *_args, **_kwargs: ("default", "prompt")
    )
    monkeypatch.setattr(refresh, "process_feed", lambda url, *_args, **_kwargs: (
        [], {"total": 0, "passed": 0, "api_calls": 0},
        "upstream unavailable" if url == private_url else None, None,
    ))
    monkeypatch.setattr(refresh, "flush_stats", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *_args, **_kwargs: None)
    attempt = refresh._execute_feed_refresh()
    assert attempt["status"] == "partial"
    assert attempt["failures"][0]["source"] == private_url
    assert json.loads(database.get_app_meta("last_refresh_result"))["failures"][0]["source"] == private_url
    _publish_coverage(tmp_path, monkeypatch, original)
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web DB read"))
    response = client.get("/glance-sse")
    assert response.headers["X-Card-Ingestion-State"] == "incomplete"
    body = response.get_data(as_text=True)
    assert "最近采集：1/2" in body and "来源名不可公开" in body
    assert "synthetic-secret" not in body and "internal.invalid" not in body
    assert "普通公司0发布例行公告" in body
