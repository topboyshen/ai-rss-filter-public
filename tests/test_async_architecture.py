import multiprocessing as mp
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest


def _dispatch_in_process(start, log_path):
    import routes.webhook_routes as webhook_routes

    def execute(_task):
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        time.sleep(0.15)
        return True

    webhook_routes._execute_spooled_task = execute
    start.wait(5)
    webhook_routes._dispatch_spooled_tasks_once()
    webhook_routes.wait_for_webhook_idle(5)
    webhook_routes.stop_webhook_dispatcher(timeout=5)


@pytest.fixture
def async_spool(monkeypatch, tmp_path):
    import routes.webhook_routes as webhook_routes

    webhook_routes.stop_webhook_dispatcher(timeout=2)
    monkeypatch.setattr(webhook_routes, "_webhook_spool_dir", tmp_path / "spool")
    monkeypatch.setattr(webhook_routes, "_webhook_spool_max_tasks", 32)
    monkeypatch.setattr(webhook_routes, "_webhook_max_attempts", 3)
    monkeypatch.setattr(webhook_routes, "_webhook_lease_seconds", 30)
    with webhook_routes._webhook_inflight_lock:
        webhook_routes._webhook_inflight.clear()
        webhook_routes._webhook_claims.clear()
        webhook_routes._webhook_retry_state.clear()
    yield webhook_routes
    webhook_routes.stop_webhook_dispatcher(timeout=2)


def _task(number=1):
    return {
        "feed_url": "https://example.com/feed",
        "entries": [{
            "title": f"entry-{number}",
            "url": f"https://example.com/{number}",
            "content": "content",
        }],
    }


def test_cross_process_atomic_claim_executes_digest_once(async_spool, tmp_path):
    webhook_routes = async_spool
    assert webhook_routes._persist_webhook_task(_task()) == "stored"
    log_path = str(tmp_path / "executions.log")
    ctx = mp.get_context("fork")
    start = ctx.Event()
    children = [
        ctx.Process(target=_dispatch_in_process, args=(start, log_path))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(8)
    assert [child.exitcode for child in children] == [0, 0]
    assert len(Path(log_path).read_text(encoding="utf-8").splitlines()) == 1


def test_attempts_are_persistent_and_exhaustion_moves_to_dlq(
    async_spool, monkeypatch
):
    webhook_routes = async_spool
    monkeypatch.setattr(webhook_routes, "_webhook_max_attempts", 2)
    assert webhook_routes._persist_webhook_task(_task()) == "stored"
    first_path, first = webhook_routes._claim_next_spooled_task()
    assert first["attempts"] == 1
    assert webhook_routes._webhook_slots.acquire(blocking=False)
    failed = Future()
    failed.set_result(False)
    webhook_routes._finish_spooled_task(
        failed, first_path, first["digest"], first["owner"]
    )
    second_path, second = webhook_routes._claim_next_spooled_task(
        now=time.time() + 120
    )
    assert second["attempts"] == 2
    assert webhook_routes._webhook_slots.acquire(blocking=False)
    failed_again = Future()
    failed_again.set_result(False)
    webhook_routes._finish_spooled_task(
        failed_again, second_path, second["digest"], second["owner"]
    )
    assert len(list((Path(webhook_routes._webhook_spool_dir) / "dead-letter").glob("*.json"))) == 1


def test_graceful_shutdown_waits_for_inflight(async_spool, monkeypatch):
    webhook_routes = async_spool
    started = threading.Event()
    release = threading.Event()

    def execute(_task):
        started.set()
        release.wait(2)
        return True

    monkeypatch.setattr(webhook_routes, "_execute_spooled_task", execute)
    assert webhook_routes._persist_webhook_task(_task()) == "stored"
    webhook_routes.start_webhook_dispatcher()
    assert started.wait(2)
    threading.Timer(0.2, release.set).start()
    before = time.monotonic()
    assert webhook_routes.stop_webhook_dispatcher(timeout=3) is True
    assert time.monotonic() - before >= 0.15
    assert webhook_routes.webhook_inflight_count() == 0


def test_redis_client_disables_unsupported_maintenance_notifications(monkeypatch):
    import redis
    import services.shared_cache as shared_cache

    captured = {}
    sentinel = object()

    def from_url(url, **kwargs):
        captured.update(url=url, **kwargs)
        return sentinel

    monkeypatch.setattr(shared_cache, "_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    assert shared_cache.redis_client() is sentinel
    assert captured["maint_notifications_config"].enabled is False


def test_shared_cache_round_trip(monkeypatch):
    import services.shared_cache as shared_cache

    class Redis:
        values = {}

        def setex(self, key, ttl, value):
            assert ttl == 30
            self.values[key] = value

        def get(self, key):
            return self.values.get(key)

        def close(self):
            pass

    monkeypatch.setattr(shared_cache, "redis_client", Redis)
    assert shared_cache.set_shared_json("quotes", {"ok": True}, 30)
    assert shared_cache.get_shared_json("quotes") == {"ok": True}


def test_worker_health_is_independent_from_http_port(monkeypatch, tmp_path):
    import services.worker_health as worker_health

    monkeypatch.setattr(worker_health, "HEALTH_PATH", tmp_path / "worker-health.json")
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)
    payload = worker_health.write_worker_health(dispatcher_alive=True)
    healthy, _ = worker_health.check_worker_health(now=payload["updated_at"] + 1)
    assert healthy is True


def test_quotes_web_route_only_reads_shared_cache(monkeypatch):
    from flask import Flask
    import routes.dashboard_routes as dashboard_routes
    import services.shared_cache as shared_cache

    monkeypatch.setattr(shared_cache, "get_shared_json", lambda _name: {"html": "shared"})
    monkeypatch.setattr(
        dashboard_routes,
        "_ensure_quotes_refresher",
        lambda: (_ for _ in ()).throw(AssertionError("web must not start refresher")),
    )
    app = Flask(__name__)
    with app.test_request_context("/quotes"):
        response = dashboard_routes.quotes()
    assert "shared" in response.get_data(as_text=True)


def test_web_reads_worker_refresh_state_from_shared_cache(monkeypatch):
    import routes.shared as shared
    import services.shared_cache as shared_cache

    monkeypatch.setattr(shared, "_refresh_running", False)
    monkeypatch.setattr(
        shared_cache,
        "get_shared_json",
        lambda name: {"running": True} if name == "refresh-status" else None,
    )
    assert shared.is_refresh_job_running() is True


def test_compose_keeps_aof_shared_cache_and_worker_healthcheck():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "--appendonly" in compose and '"yes"' in compose
    assert "redis-data:/data" in compose
    assert "  ai-filter-sse:" not in compose
    assert "location = /stream" not in compose
    worker = compose.split("  ai-filter-worker:", 1)[1].split(
        "  market-close-collector:", 1
    )[0]
    assert 'python", "-m", "services.worker_health"' in worker
    gateway = compose.split("  ai-filter-web:", 1)[1].split(
        "  ai-filter-worker:", 1
    )[0]
    assert "nginx:stable-alpine@sha256:" in gateway
    assert "ai-filter-api:" in gateway
