import json
import fcntl


def _configure_refresh(monkeypatch, outcomes):
    import services.refresh as refresh

    feeds = [
        {"url": f"https://example.com/{index}", "name": f"source-{index}"}
        for index in range(len(outcomes))
    ]
    cfg = {"feeds": feeds, "output": {"max_items": 0}}
    responses = iter(outcomes)
    recorded = []

    monkeypatch.setattr(refresh, "_cfg", lambda: cfg)
    monkeypatch.setattr(
        refresh, "resolve_prompt", lambda *_args, **_kwargs: ("default", "prompt")
    )
    monkeypatch.setattr(refresh, "process_feed", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(refresh, "flush_stats", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(refresh, "cleanup_old_entries", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        refresh.DB,
        "record_refresh_attempt",
        lambda result, successful_at=None: recorded.append((result, successful_at)),
    )
    return refresh, recorded


def _feed_result(error=None):
    return (
        [],
        {"total": 0, "passed": 0, "api_calls": 0},
        error,
        None,
    )


def test_partial_refresh_is_successful_but_explicitly_observable(monkeypatch):
    refresh, recorded = _configure_refresh(
        monkeypatch,
        [_feed_result(), _feed_result("upstream timeout")],
    )

    result = refresh._execute_feed_refresh()

    assert result["status"] == "partial"
    assert result["enabled_sources"] == 2
    assert result["succeeded_sources"] == 1
    assert result["failed_sources"] == 1
    assert result["failures"] == [
        {"source": "source-1", "error": "upstream timeout"}
    ]
    assert recorded[0][0] == result
    assert recorded[0][1] is None


def test_all_sources_success_advances_full_refresh_freshness(monkeypatch):
    refresh, recorded = _configure_refresh(
        monkeypatch,
        [_feed_result(), _feed_result()],
    )

    result = refresh._execute_feed_refresh()

    assert result["status"] == "success"
    assert recorded == [(result, result["finished_at"])]


def test_all_source_failure_does_not_advance_freshness(monkeypatch):
    refresh, recorded = _configure_refresh(
        monkeypatch,
        [_feed_result("503"), _feed_result("timeout")],
    )

    result = refresh._execute_feed_refresh()

    assert result["status"] == "failed"
    assert result["succeeded_sources"] == 0
    assert recorded == [(result, None)]


def test_only_partial_source_is_not_complete_and_does_not_cache_passed_news(monkeypatch):
    from types import SimpleNamespace

    stats = {"total": 3, "passed": 1, "api_calls": 0,
             "source_unrecognized": 1, "score_deferred": 2}
    refresh, recorded = _configure_refresh(monkeypatch, [
        ([SimpleNamespace(title="[⭐8.0] 有效消息")], stats, None, "source"),
    ])
    cached = []
    monkeypatch.setattr(refresh, "set_cached", lambda *args: cached.append(args))

    result = refresh._execute_feed_refresh()

    assert result["status"] == "partial"
    assert result["succeeded_sources"] == result["failed_sources"] == 0
    assert result["partial_sources"] == 1
    assert result["source_unrecognized_entries"] == 1
    assert result["score_deferred_entries"] == 2
    assert recorded == [(result, None)] and cached == []


def test_refresh_counts_complete_partial_and_failed_separately(monkeypatch):
    deferred = ([], {"total": 1, "passed": 0, "api_calls": 2, "score_deferred": 1}, None, None)
    refresh, recorded = _configure_refresh(monkeypatch, [
        _feed_result(), deferred, _feed_result("upstream unavailable"),
    ])

    result = refresh._execute_feed_refresh()

    assert result["status"] == "partial" and result["enabled_sources"] == 3
    assert [result[key] for key in ("succeeded_sources", "partial_sources", "failed_sources")] == [1, 1, 1]
    assert result["score_deferred_entries"] == 1 and result["source_unrecognized_entries"] == 0
    assert recorded == [(result, None)]


def test_partial_entry_failure_never_populates_complete_refresh_cache(monkeypatch):
    from types import SimpleNamespace

    stats = {"total": 2, "passed": 1, "api_calls": 1, "entry_errors": 1}
    refresh, recorded = _configure_refresh(monkeypatch, [
        ([SimpleNamespace(title="[⭐8.0] 有效消息")], stats, None, "source"),
    ])
    cached = []
    monkeypatch.setattr(refresh, "set_cached", lambda *args: cached.append(args))

    result = refresh._execute_feed_refresh()

    assert result["status"] == "partial" and result["partial_sources"] == 1
    assert result["succeeded_sources"] == result["failed_sources"] == 0
    assert recorded == [(result, None)] and cached == []


def test_partial_refresh_command_does_not_request_automatic_collection_retry(monkeypatch, tmp_path):
    import services.refresh as refresh

    monkeypatch.setattr(refresh, "try_acquire_refresh_lock", lambda: True)
    monkeypatch.setattr(refresh, "set_refresh_running", lambda _running: None)
    monkeypatch.setattr(refresh, "_execute_feed_refresh", lambda: {"status": "partial"})
    monkeypatch.setenv("AI_FILTER_REFRESH_LOCK", str(tmp_path / "refresh.lock"))

    assert refresh.run_refresh_command() is True


def test_no_enabled_sources_is_a_successful_noop(monkeypatch):
    import services.refresh as refresh

    recorded = []
    monkeypatch.setattr(
        refresh,
        "_cfg",
        lambda: {
            "feeds": [
                {"url": "https://example.com/disabled", "enabled": False}
            ]
        },
    )
    monkeypatch.setattr(refresh, "flush_stats", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        refresh.DB,
        "record_refresh_attempt",
        lambda result, successful_at=None: recorded.append((result, successful_at)),
    )

    result = refresh._execute_feed_refresh()

    assert result["status"] == "noop"
    assert result["enabled_sources"] == 0
    assert recorded == [(result, None)]


def test_refresh_command_returns_failure_for_all_source_failure(monkeypatch, tmp_path):
    import services.refresh as refresh

    monkeypatch.setattr(refresh, "try_acquire_refresh_lock", lambda: True)
    monkeypatch.setattr(refresh, "set_refresh_running", lambda _running: None)
    monkeypatch.setattr(
        refresh,
        "_execute_feed_refresh",
        lambda: {"status": "failed"},
    )
    monkeypatch.setenv("AI_FILTER_REFRESH_LOCK", str(tmp_path / "refresh.lock"))

    assert refresh.run_refresh_command() is False


def test_refresh_lock_contention_has_a_distinct_busy_result(monkeypatch):
    import services.refresh as refresh

    monkeypatch.setattr(refresh, "try_acquire_refresh_lock", lambda: False)

    assert refresh.run_refresh_command() == refresh.REFRESH_BUSY


def test_refresh_execution_lock_is_nonblocking_and_released(monkeypatch, tmp_path):
    import services.refresh as refresh

    lock_path = tmp_path / "refresh.lock"
    monkeypatch.setenv("AI_FILTER_REFRESH_LOCK", str(lock_path))
    with lock_path.open("a+", encoding="utf-8") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with refresh.refresh_execution_lock() as acquired:
            assert acquired is False

    with refresh.refresh_execution_lock() as acquired:
        assert acquired is True

    with lock_path.open("a+", encoding="utf-8") as next_owner:
        fcntl.flock(next_owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_missing_real_refresh_is_stale_and_last_attempt_is_exposed(monkeypatch):
    import routes.shared as shared

    last_attempt = {
        "status": "failed",
        "enabled_sources": 2,
        "succeeded_sources": 0,
    }
    monkeypatch.setattr(shared, "get_last_full_refresh_ts", lambda: 0)
    monkeypatch.setattr(
        shared.DB,
        "get_app_meta",
        lambda key: json.dumps(last_attempt) if key == "last_refresh_result" else None,
    )

    result = shared._staleness_info(
        {"feeds": [{"url": "https://example.com/feed"}], "cache_ttl": 60}
    )

    assert result["stale"] is True
    assert result["degraded"] is True
    assert result["last_full_refresh_at"] is None
    assert result["last_attempt"] == last_attempt


def test_refresh_attempt_and_freshness_are_persisted_together():
    from db import database

    database.init_db()
    database.set_last_full_refresh_ts(123)
    failed = {"status": "failed", "succeeded_sources": 0}
    database.record_refresh_attempt(failed)

    assert database.get_last_full_refresh_ts() == 123
    assert json.loads(database.get_app_meta("last_refresh_result")) == failed

    partial = {"status": "partial", "succeeded_sources": 1}
    database.record_refresh_attempt(partial, successful_at=456)

    assert database.get_last_full_refresh_ts() == 123
    assert json.loads(database.get_app_meta("last_refresh_result")) == partial

    success = {"status": "success", "succeeded_sources": 2}
    database.record_refresh_attempt(success, successful_at=456)

    assert database.get_last_full_refresh_ts() == 456
    assert json.loads(database.get_app_meta("last_refresh_result")) == success
