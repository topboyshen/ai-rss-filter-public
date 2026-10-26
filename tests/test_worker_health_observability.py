import json
from pathlib import Path

import pytest
from flask import Flask


@pytest.fixture
def isolated_worker_health(monkeypatch, tmp_path):
    import services.worker_health as worker_health

    worker_health._reset_health_state_for_tests()
    spool = tmp_path / "spool"
    monkeypatch.setattr(worker_health, "HEALTH_PATH", tmp_path / "worker-health.json")
    monkeypatch.setattr(worker_health, "_spool_dir", lambda: spool)
    yield worker_health, spool
    worker_health._reset_health_state_for_tests()


def _write_task(path: Path, digest_char: str, created_at: float) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    task_path = path / f"{digest_char * 64}.json"
    task_path.write_text(
        json.dumps({"created_at": created_at, "task": {"task_type": "score"}}),
        encoding="utf-8",
    )
    return task_path


def test_dependency_probes_are_cached(isolated_worker_health, monkeypatch):
    worker_health, _spool = isolated_worker_health
    calls = {"spool": 0, "redis": 0}

    def spool_probe():
        calls["spool"] += 1
        return True

    def redis_probe():
        calls["redis"] += 1
        return True

    monkeypatch.setattr(worker_health, "probe_spool_writable", spool_probe)
    monkeypatch.setattr(worker_health, "probe_redis", redis_probe)

    first = worker_health._dependency_status(force=True, now_monotonic=100.0)
    cached = worker_health._dependency_status(
        now_monotonic=100.0
        + worker_health.DEPENDENCY_PROBE_INTERVAL_SECONDS
        - 0.1
    )
    refreshed = worker_health._dependency_status(
        now_monotonic=100.0
        + worker_health.DEPENDENCY_PROBE_INTERVAL_SECONDS
    )

    assert first["redis_ok"] is True
    assert cached["checked_at"] == 100.0
    assert refreshed["checked_at"] > cached["checked_at"]
    assert calls == {"spool": 2, "redis": 2}


def test_redis_probe_reuses_client_until_failure(
    isolated_worker_health, monkeypatch
):
    worker_health, _spool = isolated_worker_health
    import services.shared_cache as shared_cache

    class Client:
        def __init__(self):
            self.pings = 0
            self.closed = 0

        def ping(self):
            self.pings += 1
            return True

        def close(self):
            self.closed += 1

    client = Client()
    factories = []
    monkeypatch.setattr(shared_cache, "redis_enabled", lambda: True)
    monkeypatch.setattr(
        shared_cache, "redis_client", lambda: factories.append("created") or client
    )

    assert worker_health.probe_redis() is True
    assert worker_health.probe_redis() is True
    assert factories == ["created"]
    assert client.pings == 2

    worker_health.close_worker_health_resources()
    assert client.closed == 1


def test_spool_metrics_expose_pending_inflight_dlq_and_oldest_age(
    isolated_worker_health,
):
    worker_health, spool = isolated_worker_health
    _write_task(spool, "a", 100.0)
    _write_task(spool / "inflight", "b", 80.0)
    dlq = spool / "dead-letter"
    dlq.mkdir(mode=0o700)
    (dlq / f"{'c' * 64}.5.1.json").write_text("{}", encoding="utf-8")
    (dlq / "unrelated.json").write_text("{}", encoding="utf-8")

    metrics = worker_health.collect_spool_metrics(now=200.0)

    assert metrics["pending"] == 1
    assert metrics["inflight"] == 1
    assert metrics["dead_letter"] == 1
    assert metrics["oldest_task_age_seconds"] == 120.0
    assert metrics["scan_ok"] is True


def test_malformed_active_task_is_alerted_without_crashing_health(
    isolated_worker_health, monkeypatch
):
    worker_health, spool = isolated_worker_health
    spool.mkdir(mode=0o700, parents=True)
    malformed = spool / f"{'f' * 64}.json"
    malformed.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)
    monkeypatch.setattr(worker_health, "_WORKER_STARTED_AT", 1.0)

    payload = worker_health.write_worker_health(
        dispatcher_alive=True,
        now=200.0,
        force_dependency_probe=True,
    )
    healthy, _checked = worker_health.check_worker_health(now=201.0)

    assert payload["queue"]["invalid_files"] == 1
    assert payload["queue"]["healthy"] is True
    assert healthy is True

    import routes.admin_routes as admin_routes

    queue_metrics = admin_routes._worker_queue_metrics()
    assert queue_metrics["invalid_files"] == 1
    assert queue_metrics["alert"] is True


def test_stale_tasks_alert_without_failing_worker_liveness(
    isolated_worker_health, monkeypatch
):
    worker_health, spool = isolated_worker_health
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)
    monkeypatch.setattr(worker_health, "_WORKER_STARTED_AT", 1.0)
    monkeypatch.setattr(worker_health, "QUEUE_MAX_AGE_SECONDS", 60)
    _write_task(spool, "a", 100.0)

    payload = worker_health.write_worker_health(
        dispatcher_alive=True,
        now=200.0,
        force_dependency_probe=True,
    )
    healthy, checked = worker_health.check_worker_health(now=201.0)

    assert payload["queue"]["stale"] is True
    assert payload["queue"]["dead_letter"] == 0
    assert payload["queue"]["startup_grace"] is False
    assert checked["queue"]["healthy"] is True
    assert healthy is True

    import routes.admin_routes as admin_routes

    queue_metrics = admin_routes._worker_queue_metrics()
    assert queue_metrics["stale"] is True
    assert queue_metrics["alert"] is True


def test_dlq_only_stays_healthy_but_emits_alert(
    isolated_worker_health, monkeypatch
):
    worker_health, spool = isolated_worker_health
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)
    monkeypatch.setattr(worker_health, "_WORKER_STARTED_AT", 1.0)
    dlq = spool / "dead-letter"
    dlq.mkdir(mode=0o700, parents=True)
    (dlq / f"{'d' * 64}.5.1.json").write_text("{}", encoding="utf-8")

    payload = worker_health.write_worker_health(
        dispatcher_alive=True,
        now=200.0,
        force_dependency_probe=True,
    )
    healthy, _checked = worker_health.check_worker_health(now=201.0)

    assert payload["queue"]["dead_letter"] == 1
    assert payload["queue"]["startup_grace"] is False
    assert payload["queue"]["healthy"] is True
    assert healthy is True

    import routes.admin_routes as admin_routes

    queue_metrics = admin_routes._worker_queue_metrics()
    assert queue_metrics["dead_letter"] == 1
    assert queue_metrics["stale"] is False
    assert queue_metrics["alert"] is True


def test_startup_grace_suppresses_stale_task_liveness(
    isolated_worker_health, monkeypatch
):
    worker_health, spool = isolated_worker_health
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)
    monkeypatch.setattr(worker_health, "_WORKER_STARTED_AT", 190.0)
    monkeypatch.setattr(worker_health, "QUEUE_MAX_AGE_SECONDS", 60)
    _write_task(spool, "e", 100.0)

    payload = worker_health.write_worker_health(
        dispatcher_alive=True,
        now=200.0,
        force_dependency_probe=True,
    )
    healthy, _checked = worker_health.check_worker_health(now=201.0)

    assert payload["queue"]["stale"] is True
    assert payload["queue"]["startup_grace"] is True
    assert payload["queue"]["healthy"] is True
    assert healthy is True


def test_healthcheck_only_reads_snapshot_and_does_not_probe_dependencies(
    isolated_worker_health, monkeypatch
):
    worker_health, _spool = isolated_worker_health
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)
    payload = worker_health.write_worker_health(
        dispatcher_alive=True, force_dependency_probe=True
    )
    monkeypatch.setattr(
        worker_health,
        "probe_spool_writable",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected disk probe")),
    )
    monkeypatch.setattr(
        worker_health,
        "probe_redis",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected Redis probe")),
    )

    healthy, _checked = worker_health.check_worker_health(
        now=payload["updated_at"] + 1
    )

    assert healthy is True


def test_dead_background_subsystem_fails_worker_snapshot(
    isolated_worker_health, monkeypatch
):
    worker_health, _spool = isolated_worker_health
    monkeypatch.setattr(worker_health, "probe_spool_writable", lambda: True)
    monkeypatch.setattr(worker_health, "probe_redis", lambda: True)

    payload = worker_health.write_worker_health(
        dispatcher_alive=True,
        subsystems={"digest": False, "quotes": True},
        force_dependency_probe=True,
    )
    healthy, checked = worker_health.check_worker_health(
        now=payload["updated_at"] + 1
    )

    assert checked["subsystems"] == {"digest": False, "quotes": True}
    assert healthy is False


def test_prometheus_metrics_include_worker_queue_alerts(monkeypatch):
    import routes.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes, "check_api_key", lambda: True)
    monkeypatch.setattr(admin_routes.DB, "get_stats", lambda: None)
    monkeypatch.setattr(admin_routes.DB, "get_feed_health", lambda: [])
    monkeypatch.setattr(admin_routes, "_cfg", lambda: {})
    monkeypatch.setattr(
        admin_routes,
        "_staleness_info",
        lambda _cfg: {"stale": False, "seconds_since_full_refresh": 0},
    )
    monkeypatch.setattr(admin_routes, "is_refresh_job_running", lambda: False)
    monkeypatch.setattr(
        admin_routes,
        "_worker_queue_metrics",
        lambda: {
            "pending": 2,
            "inflight": 1,
            "dead_letter": 1,
            "oldest_task_age_seconds": 1900.0,
            "task_max_age_seconds": 1800,
            "scan_ok": True,
            "stale": True,
            "alert": True,
        },
    )
    monkeypatch.setattr(
        admin_routes,
        "_cold_filter_metrics",
        lambda: {"limit": 2, "inflight": 1, "rejections_total": 6},
    )

    app = Flask(__name__)
    with app.test_request_context("/metrics"):
        response = admin_routes.metrics()
    body = response.get_data(as_text=True)

    assert "ai_filter_worker_queue_pending 2" in body
    assert "ai_filter_worker_queue_inflight 1" in body
    assert "ai_filter_worker_queue_dead_letter 1" in body
    assert "ai_filter_worker_queue_oldest_age_seconds 1900.0" in body
    assert "ai_filter_worker_queue_stale 1" in body
    assert "ai_filter_worker_queue_alert 1" in body
    assert "ai_filter_cold_filter_limit 2" in body
    assert "ai_filter_cold_filter_inflight 1" in body
    assert "ai_filter_cold_filter_rejections_total 6" in body
