"""Committed inputs must eventually reach read-only homepage snapshots."""
import importlib
import os
import time
from html import escape
from types import SimpleNamespace

from flask import Flask
import pytest

from db import database
from services import dashboard_snapshot_updates as updates


@pytest.fixture
def publication(monkeypatch, tmp_path):
    import routes.webhook_routes as webhook
    import routes.security as security

    webhook.stop_webhook_dispatcher(timeout=2)
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "1")
    monkeypatch.setenv("AI_FILTER_ROLE", "worker")
    monkeypatch.setenv("AI_FILTER_VERSION", "test-publication")
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(webhook, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(webhook, "_webhook_spool_max_tasks", 32)
    clock = [1000]
    monkeypatch.setattr(updates, "time", SimpleNamespace(time=lambda: clock[0]))
    database.init_db()
    security._rate_limit_store.clear()
    yield webhook, clock
    webhook.stop_webhook_dispatcher(timeout=2)
    security._rate_limit_store.clear()


def _cache(number=1):
    title = f"浦发银行发布第{number}份年度报告"
    return database.set_cached_entry(
        f"https://example.test/{number}", title, title, 8, "银行", "报告发布",
        feed_url="https://example.test/rss", score_path="llm",
        strategy_version="test-contract",
    )


def _signal(number=1):
    return {
        "event_key": f"event:{number}", "independence_key": f"event:{number}",
        "link": f"https://example.test/signal/{number}", "feed_url": "feed-a",
        "available_at": 1000, "scored_at": 1000, "score": 8,
        "direction": "中性", "strategy_version": "test-contract",
        "mapping_version": "asset-map:test", "score_path": "llm",
        "selected": True, "aggregation_threshold": 7,
    }


def _pending_task(webhook):
    directory = webhook._ensure_spool_dir()
    paths = webhook._pending_paths_unlocked(directory)
    assert len(paths) == 1
    return webhook._load_spooled_task(paths[0])["task"]


def _success(monkeypatch):
    import services.dashboard_snapshots as snapshots

    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: {
        "ok": sorted(updates.REALTIME_CARDS), "failed": [], "deferred": [],
    })


def test_cache_write_and_dirty_marker_share_actual_transaction(publication):
    _cache()
    assert updates.realtime_revision_status() == {
        "input": 1, "published": 0, "published_at": 0,
    }
    with database.get_read_only_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 1


def test_failed_commit_rolls_back_cache_and_dirty_marker(publication, monkeypatch):
    original = updates.mark_realtime_inputs_dirty

    def fail_after_mark(conn):
        original(conn)
        raise RuntimeError("write fails before commit")

    monkeypatch.setattr(updates, "mark_realtime_inputs_dirty", fail_after_mark)
    with pytest.raises(RuntimeError, match="before commit"):
        _cache()
    assert updates.realtime_revision_status()["input"] == 0
    with database.get_read_only_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0


@pytest.mark.parametrize("invalid", ["invalid", "", "-1", "1.2"])
def test_malformed_metadata_fails_write_instead_of_losing_dirty(publication, invalid):
    database.set_app_meta(updates.INPUT_REVISION_KEY, invalid)
    with pytest.raises(ValueError, match="revision"):
        _cache()
    with database.get_read_only_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0


def test_signal_insert_only_advances_when_new_row_is_committed(publication):
    assert database.store_backtest_signal(_signal()) is True
    assert database.store_backtest_signal(_signal()) is False
    assert updates.realtime_revision_status()["input"] == 1


@pytest.mark.parametrize("bulk", [False, True])
def test_actual_origin_change_dirty_but_identical_or_missing_origin_is_not(
    publication, bulk,
):
    _cache()
    with database.get_read_only_connection() as conn:
        stamp = conn.execute("SELECT created_at FROM news_cache").fetchone()[0]

    def reconcile(link, created_at, feed):
        if bulk:
            database.update_cached_entry_origins([(link, created_at, feed)])
        else:
            database.update_cached_entry_origin(link, created_at=created_at, feed_url=feed)

    reconcile("https://example.test/1", stamp, "https://example.test/rss")
    reconcile("https://example.test/missing", stamp, "https://example.test/rss")
    assert updates.realtime_revision_status()["input"] == 1
    reconcile("https://example.test/1", stamp + 1, "https://example.test/rss")
    assert updates.realtime_revision_status()["input"] == 2
    reconcile("https://example.test/1", stamp + 1, "https://example.test/second-rss")
    assert updates.realtime_revision_status()["input"] == 3


def test_enabled_false_does_not_open_or_create_database(monkeypatch):
    monkeypatch.setenv("AI_FILTER_DASHBOARD_SNAPSHOTS", "0")
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("DB read"))
    assert updates.enqueue_pending_realtime_snapshot() == "disabled"
    assert updates.capture_realtime_revision() == 0
    updates.mark_realtime_inputs_dirty(None)


@pytest.mark.parametrize("bulk", [False, True])
def test_new_source_without_timestamp_dirty_once_with_return_contract_preserved(
    publication, bulk,
):
    _cache()

    def record(link, source):
        if bulk:
            return database.record_article_sources([(link, source)], observed_at=2000)
        return database.record_article_source(link, source, observed_at=2000)

    assert record("https://example.test/1", "https://example.test/rss") == 1
    assert updates.realtime_revision_status()["input"] == 1
    assert record("https://example.test/1", "https://example.test/new-rss") == 1
    assert updates.realtime_revision_status()["input"] == 2
    assert record("https://example.test/1", "https://example.test/new-rss") == 1
    assert record("https://example.test/missing", "https://example.test/new-rss") == 0
    assert updates.realtime_revision_status()["input"] == 2


def test_queue_full_then_worker_tick_recovers_without_new_ingestion(
    publication, monkeypatch,
):
    import worker_main

    webhook, _clock = publication
    _cache()
    monkeypatch.setattr(webhook, "_webhook_spool_max_tasks", 0)
    assert updates.enqueue_pending_realtime_snapshot() == "full"
    assert updates.realtime_revision_status()["published"] == 0
    monkeypatch.setattr(webhook, "_webhook_spool_max_tasks", 32)
    worker_main._retry_pending_dashboard_publication()
    worker_main._dashboard_retry_thread.join(timeout=2)
    assert not worker_main._dashboard_retry_thread.is_alive()
    assert _pending_task(webhook)["realtime_input_revision"] == 1


def test_worker_health_retry_never_waits_on_blocked_spool(publication, monkeypatch):
    import threading
    import worker_main

    entered, release = threading.Event(), threading.Event()

    def blocked_queue():
        entered.set()
        assert release.wait(timeout=3)
        return "full"

    monkeypatch.setattr(updates, "enqueue_pending_realtime_snapshot", blocked_queue)
    worker_main._retry_pending_dashboard_publication()
    assert entered.wait(timeout=1)
    first_thread = worker_main._dashboard_retry_thread
    # Health ticks cannot wait for the blocked I/O or create unlimited threads.
    for _ in range(20):
        worker_main._retry_pending_dashboard_publication()
    assert worker_main._dashboard_retry_thread is first_thread
    assert first_thread.is_alive()
    release.set()
    first_thread.join(timeout=1)
    assert not first_thread.is_alive()


def test_completed_bucket_receipt_does_not_hide_next_revision(publication, monkeypatch):
    webhook, clock = publication
    _cache()
    assert updates.enqueue_pending_realtime_snapshot() == "stored"
    path, envelope = webhook._claim_next_spooled_task()
    _success(monkeypatch)
    assert webhook._execute_spooled_task(envelope["task"]) is True
    assert webhook._complete_spooled_task(path, envelope["digest"], envelope["owner"])
    _cache(2)
    assert updates.enqueue_pending_realtime_snapshot() == "coalesced"
    clock[0] += updates.REALTIME_MERGE_SECONDS
    assert updates.enqueue_pending_realtime_snapshot() == "stored"
    task = _pending_task(webhook)
    assert task["request_window"] == 1030 // 30  # same original five-minute bucket
    assert 1000 // 300 == 1030 // 300
    assert task["realtime_input_revision"] == 2


def test_writes_during_publication_remain_pending(publication, monkeypatch):
    import services.dashboard_snapshots as snapshots

    webhook, clock = publication
    _cache()

    def publish_with_new_input():
        _cache(2)
        return {"ok": sorted(updates.REALTIME_CARDS), "failed": []}

    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", publish_with_new_input)
    task = {"task_type": "dashboard_realtime_refresh", "snapshot_version": 1}
    assert webhook._execute_spooled_task(task) is True
    assert updates.realtime_revision_status()["input"] == 2
    assert updates.realtime_revision_status()["published"] == 1
    clock[0] += 30
    assert updates.enqueue_pending_realtime_snapshot() == "stored"
    _success(monkeypatch)
    assert webhook._execute_spooled_task(_pending_task(webhook)) is True
    assert updates.realtime_revision_status()["published"] == 2
    updates.acknowledge_realtime_revision(1)
    assert updates.realtime_revision_status()["published"] == 2


@pytest.mark.parametrize("failed_card", sorted(updates.REALTIME_CARDS))
def test_any_light_card_failure_keeps_obligation(publication, monkeypatch, failed_card):
    import services.dashboard_snapshots as snapshots

    webhook, _clock = publication
    _cache()
    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: {
        "ok": list(updates.REALTIME_CARDS - {failed_card}), "failed": [failed_card],
    })
    assert webhook._execute_spooled_task({
        "task_type": "dashboard_realtime_refresh", "snapshot_version": 1,
    }) is False
    assert updates.realtime_revision_status()["published"] == 0


def test_dirty_is_recovered_after_module_restart(publication):
    webhook, _clock = publication
    _cache()
    restarted = importlib.reload(updates)
    assert restarted.realtime_revision_status()["input"] == 1
    assert restarted.enqueue_pending_realtime_snapshot() == "stored"
    assert _pending_task(webhook)["realtime_input_revision"] == 1


def test_dead_letter_receipt_cannot_disable_still_dirty_revision(publication):
    webhook, clock = publication
    _cache()
    assert updates.enqueue_pending_realtime_snapshot() == "stored"
    path, envelope = webhook._claim_next_spooled_task()
    with webhook._locked_spool() as directory:
        webhook._dead_letter_unlocked(directory, path, envelope, "injected publication error")
    assert updates.enqueue_pending_realtime_snapshot() == "duplicate"
    assert updates.realtime_revision_status()["published"] == 0
    clock[0] += 30
    assert updates.enqueue_pending_realtime_snapshot() == "stored"
    assert _pending_task(webhook)["realtime_input_revision"] == 1


def test_realtime_publisher_is_not_starved_by_newer_scoring_tasks(publication):
    webhook, _clock = publication
    score = {"entries": [], "feed_url": "https://example.test/rss"}
    live = {"task_type": "dashboard_realtime_refresh", "snapshot_version": 1}
    full = {"task_type": "dashboard_snapshot_refresh", "snapshot_version": 1}
    for task in [live, full, score]:
        assert webhook._persist_webhook_task(task) == "stored"
    pending = webhook._pending_paths_unlocked(webhook._ensure_spool_dir())
    assert [webhook._spool_task_type(path) for path in pending] == [
        "dashboard_realtime_refresh", "score", "dashboard_snapshot_refresh",
    ]


def test_pending_fifo_survives_identical_file_mtimes(publication):
    """Coarse mtimes must not let a newer score task jump a queued publication."""
    webhook, _clock = publication
    tasks = [
        {"entries": [], "feed_url": f"https://example.test/{number}"}
        for number in range(6)
    ]
    tasks.insert(3, {"task_type": "dashboard_realtime_refresh", "snapshot_version": 1})
    for task in tasks:
        assert webhook._persist_webhook_task(task) == "stored"
    directory = webhook._ensure_spool_dir()
    expected = [f"{webhook._task_digest(task)}.json" for task in tasks]
    tied_ns = 1_700_000_000_123_456_789
    for name in expected:
        os.utime(directory / name, ns=(tied_ns, tied_ns))
    pending = webhook._pending_paths_unlocked(directory)
    assert [path.name for path in pending] == expected


def test_per_type_successor_is_bounded_without_dropping_full_quality_request(publication):
    webhook, _clock = publication
    task = {"task_type": "dashboard_realtime_refresh", "snapshot_version": 1,
            "realtime_input_revision": 1}
    assert webhook._persist_webhook_task(task) == "stored"
    webhook._claim_next_spooled_task()
    assert webhook._persist_webhook_task({**task, "realtime_input_revision": 2}) == "stored"
    for revision in range(3, 100):
        assert webhook._persist_webhook_task({**task, "realtime_input_revision": revision}) == "duplicate"
    assert webhook._persist_webhook_task({
        "task_type": "dashboard_snapshot_refresh", "snapshot_version": 1,
    }) == "stored"
    directory = webhook._ensure_spool_dir()
    assert len(webhook._pending_paths_unlocked(directory)) == 2
    assert len(webhook._inflight_paths_unlocked(directory)) == 1


def test_partial_batch_committed_first_entry_still_queues_publication(publication, monkeypatch):
    webhook, _clock = publication
    monkeypatch.setattr(webhook, "Scorer", lambda: object())
    monkeypatch.setattr(webhook, "_cfg", lambda: {"feeds": []})
    monkeypatch.setattr(webhook, "resolve_scoring_contract", lambda cfg, **_: cfg)
    monkeypatch.setattr(webhook, "resolve_prompt", lambda *_: ("default", "prompt"))
    monkeypatch.setattr(webhook, "_find_notify_threshold", lambda *_: 7)
    monkeypatch.setattr(webhook.DB, "get_feed_config", lambda *_: {})

    def process(entry, **_kwargs):
        if entry.link.endswith("2"):
            raise RuntimeError("second entry failed")
        _cache()
        return entry, "ok"

    monkeypatch.setattr(webhook, "process_entry", process)
    assert webhook._score_in_background({
        "feed_url": "https://example.test/rss",
        "entries": [{"title": "first", "url": "https://example.test/1"},
                    {"title": "second", "url": "https://example.test/2"}],
    }) is False
    assert updates.realtime_revision_status()["input"] == 1
    assert _pending_task(webhook)["realtime_input_revision"] == 1


def test_real_cache_to_three_card_files_to_read_only_web(publication, monkeypatch):
    import routes.dashboard_routes as dashboard
    import services.dashboard_snapshots as snapshots
    import sse_engine

    webhook, _clock = publication
    _cache()
    live = Flask("live-publication")

    def render_cache():
        with database.get_read_only_connection() as conn:
            titles = conn.execute("SELECT raw_title FROM news_cache").fetchall()
        cards_html = "<article>" + "".join(escape(row[0]) for row in titles) + "</article>"
        return sse_engine._render_realtime_widget(
            cards_html, [{"source": "测试来源"} for _row in titles], len(titles), 0, None,
        )

    for card in updates.REALTIME_CARDS:
        live.add_url_rule(snapshots.CARD_PATHS[card].split("?", 1)[0], card, render_cache)
    original_build = snapshots.build_realtime_dashboard_snapshots
    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: original_build(live))
    assert webhook._execute_spooled_task({
        "task_type": "dashboard_realtime_refresh", "snapshot_version": 1,
    }) is True
    assert updates.realtime_revision_status()["published"] == 1
    assert snapshots.read_dashboard_snapshot("model_quality") is None
    for card in updates.REALTIME_CARDS:
        assert "浦发银行发布第1份年度报告" in snapshots.read_dashboard_snapshot(card)["html"]
    stored_html = snapshots.read_dashboard_snapshot("realtime")["html"]
    assert 'dashboard-snapshot-time' not in stored_html

    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    web = Flask("read-only-publication")
    web.add_url_rule("/glance-sse", "glance", lambda: dashboard._dashboard_snapshot_response(
        "realtime", title="Priority Intelligence", cache_seconds=30,
    ))
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web must not query DB"))
    monkeypatch.setattr(database, "get_connection", lambda: pytest.fail("Web must not write DB"))
    response = web.test_client().get("/glance-sse")
    assert response.status_code == 200
    assert response.headers["X-Card-Snapshot-State"] == "ready"
    assert response.headers["X-Card-Snapshot-Generated-At"]
    assert "浦发银行发布第1份年度报告" in response.get_data(as_text=True)
    assert "页面数据快照：" in response.get_data(as_text=True)
    assert "北京时间；非新闻发布时间" in response.get_data(as_text=True)
    from test_snapshot_time_layout import _without_clock, _root_signature

    rendered = response.get_data(as_text=True)
    assert _without_clock(rendered) == stored_html
    assert _root_signature(rendered) == _root_signature(stored_html)


def _collection_attempt(status="success"):
    stamp = int(time.time()) - 1
    failed = 0 if status == "success" else 20
    return {"version": 1, "status": status, "started_at": stamp - 300,
            "finished_at": stamp, "enabled_sources": 20,
            "succeeded_sources": 20 - failed, "failed_sources": failed,
            "failures": [] if not failed else [{"source": "异常发布方"}]}


def _coverage_publication_app():
    import services.dashboard_snapshots as snapshots
    import sse_engine

    live = Flask("coverage-publication")
    for card in updates.REALTIME_CARDS:
        live.add_url_rule(
            snapshots.CARD_PATHS[card].split("?", 1)[0], card,
            lambda: sse_engine._render_realtime_widget("<article>新闻保持可读</article>", [], 0, 0, None),
        )
    return live


def test_identical_collection_payload_does_not_republish_but_new_attempt_does(publication):
    result = _collection_attempt()
    database.record_refresh_attempt(result, successful_at=result["finished_at"])
    assert updates.realtime_revision_status()["input"] == 1
    database.record_refresh_attempt(dict(reversed(list(result.items()))), successful_at=result["finished_at"])
    assert updates.realtime_revision_status()["input"] == 1
    database.record_refresh_attempt({**result, "finished_at": result["finished_at"] + 1})
    assert updates.realtime_revision_status()["input"] == 2


def test_collection_metadata_and_obligation_roll_back_together(publication, monkeypatch):
    result = _collection_attempt()
    database.record_refresh_attempt(result, successful_at=result["finished_at"])
    original_payload = database.get_app_meta("last_refresh_result")
    original = updates.mark_realtime_inputs_dirty

    def fail_after_mark(conn):
        original(conn)
        raise RuntimeError("coverage commit interrupted")

    monkeypatch.setattr(updates, "mark_realtime_inputs_dirty", fail_after_mark)
    with pytest.raises(RuntimeError, match="coverage commit"):
        database.record_refresh_attempt(_collection_attempt("failed"))
    assert updates.realtime_revision_status()["input"] == 1
    assert database.get_app_meta("last_refresh_result") == original_payload
    assert database.get_last_full_refresh_ts() == result["finished_at"]


def test_all_failed_without_news_reaches_existing_worker_queue_and_file_only_web(publication, monkeypatch):
    import routes.dashboard_routes as dashboard
    import services.dashboard_snapshots as snapshots
    import worker_main

    webhook, clock = publication
    live = _coverage_publication_app()
    build = snapshots.build_realtime_dashboard_snapshots
    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: build(live))
    success = _collection_attempt()
    database.record_refresh_attempt(success, successful_at=success["finished_at"])
    assert webhook._execute_spooled_task({"task_type": "dashboard_realtime_refresh", "snapshot_version": 1})
    complete_html = snapshots.read_dashboard_snapshot("realtime")["html"]
    assert 'data-state="complete"' in complete_html and "20/20" in complete_html
    database.record_refresh_attempt(_collection_attempt("failed"))
    assert updates.realtime_revision_status()["input"] == 2
    assert updates.realtime_revision_status()["published"] == 1
    assert database.get_last_full_refresh_ts() == success["finished_at"]
    with database.get_read_only_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_cache").fetchone()[0] == 0
    clock[0] += 30
    worker_main._retry_pending_dashboard_publication()
    worker_main._dashboard_retry_thread.join(timeout=2)
    assert not worker_main._dashboard_retry_thread.is_alive()
    assert webhook._execute_spooled_task(_pending_task(webhook))
    assert updates.realtime_revision_status()["published"] == 2
    artifact = snapshots.read_dashboard_snapshot("realtime")["html"]
    assert 'data-state="incomplete"' in artifact and "0/20" in artifact and "20/20" not in artifact
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    web = Flask("coverage-file-only")
    web.add_url_rule("/glance-sse", "glance", lambda: dashboard._dashboard_snapshot_response(
        "realtime", title="News", cache_seconds=30,
    ))
    monkeypatch.setattr(database, "get_read_only_connection", lambda: pytest.fail("Web DB read"))
    monkeypatch.setattr(database, "get_connection", lambda: pytest.fail("Web DB write"))
    response = web.test_client().get("/glance-sse")
    assert response.status_code == 200 and response.headers["X-Card-Snapshot-State"] == "ready"
    assert response.headers["X-Card-Ingestion-State"] == "incomplete"
    assert "0/20" in response.get_data(as_text=True)
    assert "新闻保持可读" in response.get_data(as_text=True)


def test_failed_coverage_publication_keeps_latest_attempt_pending(publication, monkeypatch):
    import services.dashboard_snapshots as snapshots

    webhook, _clock = publication
    database.record_refresh_attempt(_collection_attempt("failed"))
    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: {
        "ok": [], "failed": ["realtime"], "deferred": [],
    })
    assert not webhook._execute_spooled_task({"task_type": "dashboard_realtime_refresh", "snapshot_version": 1})
    assert updates.realtime_revision_status() == {"input": 1, "published": 0, "published_at": 0}


def test_new_partial_during_publication_is_not_acknowledged_as_old_complete(publication, monkeypatch):
    import services.dashboard_snapshots as snapshots

    webhook, _clock = publication
    live = _coverage_publication_app()
    build = snapshots.build_realtime_dashboard_snapshots
    render = snapshots._render_card
    database.record_refresh_attempt(_collection_attempt())
    changed = []

    def partial_after_realtime(app, card):
        if card == "event_radar" and not changed:
            result = {**_collection_attempt("failed"), "status": "partial",
                      "succeeded_sources": 19, "failed_sources": 1}
            database.record_refresh_attempt(result)
            changed.append(True)
        return render(app, card)

    monkeypatch.setattr(snapshots, "_render_card", partial_after_realtime)
    monkeypatch.setattr(snapshots, "build_realtime_dashboard_snapshots", lambda: build(live))
    task = {"task_type": "dashboard_realtime_refresh", "snapshot_version": 1}
    assert webhook._execute_spooled_task(task)
    assert updates.realtime_revision_status()["input"] == 2
    assert updates.realtime_revision_status()["published"] == 1
    assert 'data-state="complete"' in snapshots.read_dashboard_snapshot("realtime")["html"]
    assert webhook._execute_spooled_task(task)
    assert updates.realtime_revision_status()["published"] == 2
    artifact = snapshots.read_dashboard_snapshot("realtime")["html"]
    assert 'data-state="incomplete"' in artifact and "19/20" in artifact and "20/20" not in artifact
