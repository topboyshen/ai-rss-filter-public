"""Dashboard summaries must not turn missing evidence into green health."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests import test_integration as integration_tests


integration_client = integration_tests.client
NOW = datetime(2026, 9, 21, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def _text(value):
    return json.dumps(value, ensure_ascii=False)


def _feed(*, error="", empty=0, passed=0, checked=NOW - 60, last_nonempty=NOW - 60):
    # success/fail are lifetime counters; neither represents the latest refresh.
    return (
        "https://example.test/feed",
        "测试订阅源",
        checked,
        15,
        999,
        777,
        20,
        passed,
        4.0,
        error,
        empty,
        last_nonempty,
    )


def _freshness(*, status="success", stale=False, succeeded=19, failed=1):
    return {
        "last_full_refresh_at": "2026-09-21T15:50:00+08:00",
        "seconds_since_full_refresh": 600,
        "staleness_threshold_seconds": 7200,
        "stale": stale,
        "degraded": status in {"partial", "failed"},
        "last_attempt": {
            "version": 1,
            "status": status,
            "started_at": int(NOW - 120),
            "finished_at": int(NOW - 60),
            "enabled_sources": succeeded + failed,
            "succeeded_sources": succeeded,
            "failed_sources": failed,
            "failures": [],
        },
    }


def _receipt(directory, kind="collector", **overrides):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "kind": kind,
        "run_id": "a" * 32,
        "trade_date": "2026-09-21",
        "status": "success",
        "started_at": int(NOW - 120),
        "completed_at": int(NOW - 60),
        "due_total": 20,
        "resolved": 20,
        "unresolved": 0,
        "exit_code": 0,
        "detail": "",
        "scope": "due-coverage",
    }
    payload.update(overrides)
    path = directory / f"{kind}_last.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _task(directory, number=1, *, created_at=NOW - 60):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{number:064x}.json"
    path.write_text(json.dumps({"created_at": created_at}), encoding="utf-8")
    return path


def _collector(directory):
    from services.dashboard_ops import read_market_receipts

    return next(
        item
        for item in read_market_receipts(directory, now=NOW)
        if item["kind"] == "collector"
    )


def test_news_uses_real_last_attempt_not_feed_lifetime_counts():
    from services.dashboard_ops import news_status

    result = news_status(_freshness(status="partial"))

    assert result["status"] != "ok"
    assert "19" in _text(result) and "20" in _text(result)
    assert "15:59" in _text(result)
    assert "999" not in _text(result) and "777" not in _text(result)


def test_news_distinguishes_partial_completion_from_failed_fetch():
    from services.dashboard_ops import news_status

    freshness = _freshness(status="partial", succeeded=19, failed=0)
    freshness["last_attempt"].update({"enabled_sources": 20, "partial_sources": 1,
                                      "score_deferred_entries": 2})
    result = news_status(freshness)

    assert result["status"] == "warn"
    assert "完整完成 19" in result["attempt_result"]
    assert "部分完成 1" in result["attempt_result"] and "失败 0" in result["attempt_result"]


def test_missing_news_result_is_unknown_even_when_stale_flag_is_false():
    from services.dashboard_ops import news_status

    result = news_status({"stale": False, "degraded": False, "last_attempt": None})

    assert result["status"] == "unknown"


def test_refresh_in_progress_does_not_hide_previous_failed_attempt():
    from services.dashboard_ops import news_status

    result = news_status(_freshness(status="failed", succeeded=0, failed=20), True)

    assert result["status"] != "ok"
    assert "20" in _text(result)


def test_zero_passed_and_historical_failures_are_not_current_feed_faults():
    from services.dashboard_ops import feed_attention

    assert feed_attention([_feed(passed=0)], {}) == []


def test_last_nonempty_is_not_treated_as_fault_start():
    from services.dashboard_ops import feed_attention

    result = feed_attention([_feed(error="upstream timeout", last_nonempty=1)], {})

    assert len(result) == 1
    assert "持续时间未记录" in _text(result)
    assert "持续 56" not in _text(result)


def test_existing_empty_streak_threshold_is_preserved():
    from services.dashboard_ops import feed_attention

    cfg = {"feed_health": {"empty_degraded_after": 3}}

    assert feed_attention([_feed(empty=2)], cfg) == []
    assert len(feed_attention([_feed(empty=3)], cfg)) == 1


def test_market_success_is_only_receipt_success(tmp_path):
    _receipt(tmp_path)

    result = _collector(tmp_path)

    assert result["label"] == "收据记录成功"
    assert "2026-09-21" in _text(result)
    assert "15:59" in _text(result)
    assert "自然收盘成功" not in _text(result)


@pytest.mark.parametrize("detail", ["replay:coverage", "manual replay:coverage"])
def test_replay_success_never_becomes_natural_close_success(tmp_path, detail):
    _receipt(tmp_path, detail=detail)

    result = _collector(tmp_path)

    assert result["status"] != "ok"
    assert result["label"] != "收据记录成功"
    assert "回放" in _text(result)


@pytest.mark.parametrize("status", ["skipped_nontrading", "skipped_maintenance"])
def test_skipped_receipt_is_not_a_success_or_a_current_fault(tmp_path, status):
    _receipt(tmp_path, status=status)

    result = _collector(tmp_path)

    assert result["status"] == "info"
    assert result["label"] != "收据记录成功"


def test_previous_trade_date_receipt_cannot_prove_current_natural_close(tmp_path):
    _receipt(tmp_path, trade_date="2026-09-18", completed_at=int(NOW - 3 * 86400))

    result = _collector(tmp_path)

    assert result["status"] != "ok"
    assert "2026-09-18" in _text(result)
    assert result["label"] != "收据记录成功"


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"version": 999}'])
def test_malformed_market_file_is_unknown_without_breaking_other_cards(
    tmp_path, payload
):
    from services.dashboard_ops import read_market_receipts

    (tmp_path / "collector_last.json").write_text(payload, encoding="utf-8")
    _receipt(tmp_path, "repair")

    result = read_market_receipts(tmp_path, now=NOW)

    assert len(result) == 3
    assert (
        next(item for item in result if item["kind"] == "collector")["status"]
        == "unknown"
    )
    assert (
        next(item for item in result if item["kind"] == "repair")["status"] != "unknown"
    )


def test_missing_and_denied_market_receipts_remain_unknown(tmp_path, monkeypatch):
    import services.dashboard_ops as ops

    assert all(
        item["status"] == "unknown" for item in ops.read_market_receipts(tmp_path)
    )

    def denied(_path):
        raise PermissionError("private filesystem detail must not reach the page")

    monkeypatch.setattr(ops, "read_small_json", denied)
    result = ops.read_market_receipts(tmp_path)
    assert all(item["status"] == "unknown" for item in result)
    assert "private filesystem" not in _text(result)


def test_small_json_reader_refuses_oversized_and_non_object_files(tmp_path):
    from services.dashboard_ops import MAX_JSON_BYTES, read_small_json

    path = tmp_path / "result.json"
    path.write_text('{"detail":"' + "x" * MAX_JSON_BYTES + '"}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_small_json(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        read_small_json(path)
    path.write_text('{"status":"success"}', encoding="utf-8")
    assert read_small_json(path) == {"status": "success"}


def test_small_json_reader_rejects_symlinks_and_fifo_without_reading_them(tmp_path):
    import os

    from services.dashboard_ops import read_small_json

    original = tmp_path / "private.json"
    original.write_text('{"secret":"not-for-this-receipt"}', encoding="utf-8")
    link = tmp_path / "linked.json"
    link.symlink_to(original)
    with pytest.raises(OSError):
        read_small_json(link)
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        read_small_json(fifo)


def test_deep_json_receipt_is_unknown_instead_of_crashing_the_page(tmp_path):
    from services.dashboard_ops import read_small_json

    path = tmp_path / "collector_last.json"
    path.write_text('{"value":' + "[" * 2000 + "0" + "]" * 2000 + "}", encoding="utf-8")

    with pytest.raises(ValueError):
        read_small_json(path)
    assert _collector(tmp_path)["status"] == "unknown"


@pytest.mark.parametrize(
    "overrides",
    [
        {"completed_at": int(NOW + 60)},
        {"trade_date": "2026-09-22"},
        {"kind": "history"},
        {"trade_date": "not-a-date"},
        {"trade_date": ""},
        {"completed_at": "nan"},
    ],
)
def test_unusable_market_contract_is_unknown(tmp_path, overrides):
    path = _receipt(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _collector(tmp_path)["status"] == "unknown"


def test_queue_missing_directory_does_not_report_a_healthy_empty_queue(tmp_path):
    from services.dashboard_ops import read_queue_status

    assert read_queue_status(tmp_path / "absent", now=NOW)["status"] == "unknown"


def test_queue_only_reads_existing_files_and_never_probes_dependencies(
    tmp_path, monkeypatch
):
    from services import worker_health
    from services.dashboard_ops import read_queue_status

    def forbidden():
        pytest.fail("Dashboard must not run dependency probes")

    monkeypatch.setattr(worker_health, "probe_redis", forbidden)
    monkeypatch.setattr(worker_health, "probe_spool_writable", forbidden)
    task = _task(tmp_path)
    before = task.read_bytes(), task.stat().st_mtime_ns

    result = read_queue_status(tmp_path, now=NOW)

    assert result["status"] == "ok"
    assert (task.read_bytes(), task.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [task]


def test_bounded_queue_scan_does_not_label_partial_scan_healthy(tmp_path, monkeypatch):
    import services.dashboard_ops as ops

    for index in range(4):
        _task(tmp_path, index)
    monkeypatch.setattr(ops, "MAX_QUEUE_ENTRIES", 2)

    result = ops.read_queue_status(tmp_path, now=NOW)

    assert result["status"] == "unknown"


def test_queue_wall_clock_budget_expiry_is_unknown(tmp_path, monkeypatch):
    import services.dashboard_ops as ops

    _task(tmp_path)
    clock = [0.0]

    def monotonic():
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(ops.time, "monotonic", monotonic)
    monkeypatch.setattr(ops, "QUEUE_SCAN_SECONDS", 0.05)

    assert ops.read_queue_status(tmp_path, now=NOW)["status"] == "unknown"


def test_dead_letter_and_expired_task_are_actionable(tmp_path):
    from services.dashboard_ops import read_queue_status

    _task(tmp_path, created_at=NOW - 100000)
    dead = tmp_path / "dead-letter"
    dead.mkdir()
    (dead / f"{'b' * 64}.5.1.json").write_text("{}", encoding="utf-8")

    result = read_queue_status(tmp_path, now=NOW)

    assert result["status"] not in {"ok", "info"}


def test_complete_overview_cannot_turn_service_unknown_into_all_green(
    tmp_path, monkeypatch
):
    from services import dashboard_ops as ops

    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(ops, "_spool_dir", lambda: spool)
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", "relative-is-invalid")

    result = ops.build_dashboard_ops(
        {},
        [_feed(passed=0)],
        _freshness(succeeded=20, failed=0),
        db_path=tmp_path / "cache.db",
        now=NOW,
    )

    assert result["attention"] == []
    assert result["news"]["status"] == "ok"
    assert result["service"]["status"] == "unknown"
    assert all(item["status"] == "unknown" for item in result["market"])


def _prepare_dashboard(monkeypatch, tmp_path, *, rows=None):
    import routes.dashboard_routes as routes

    rows = rows or []
    details = {
        row[0]: {
            "rate_text": "0/20",
            "threshold": 7,
            "breakdown_text": "未达阈值 20",
            "other_contract_total": 0,
            "zero_output_reason": "未达阈值 20",
        }
        for row in rows
    }
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", str(tmp_path / "market"))
    monkeypatch.setenv("WEBHOOK_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(routes, "_staleness_info", lambda _cfg: _freshness())
    monkeypatch.setattr(routes, "_get_feed_health_snapshot", lambda: (rows, details))
    monkeypatch.setattr(routes, "_get_briefing_articles", lambda **_kwargs: [])
    monkeypatch.setattr(routes, "attach_quant_market_context", lambda value: value)
    monkeypatch.setattr(routes, "is_refresh_job_running", lambda: False)
    return routes


def test_authenticated_real_dashboard_renders_unknowns_and_escapes_errors(
    integration_client, monkeypatch, tmp_path
):
    error = '<script>alert("unsafe")</script>'
    _prepare_dashboard(monkeypatch, tmp_path, rows=[_feed(error=error)])

    response = integration_client.get("/dashboard")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "需要处理" in body
    assert "持续时间未记录" in body
    assert "未接入" in body and "未知" in body
    assert 'href="/ops-feeds"' in body
    assert 'href="/dashboard/neutral-records"' in body
    assert error not in body
    assert "&lt;script&gt;" in body
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_unauthenticated_request_does_not_read_operations_files(
    integration_client, monkeypatch, tmp_path
):
    from services import dashboard_ops as ops

    routes = _prepare_dashboard(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "get_dashboard_auth", lambda: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("operations evidence must not be read before authentication")

    monkeypatch.setattr(ops, "read_queue_status", forbidden)
    monkeypatch.setattr(ops, "read_market_receipts", forbidden)

    assert integration_client.get("/dashboard").status_code == 401
