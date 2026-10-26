import dataclasses
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ops import ai_filter_ops_check as monitor


def _healthy_metrics(**updates: float) -> bytes:
    values = {
        "ai_filter_refresh_stale": 0,
        "ai_filter_refresh_degraded": 0,
        "ai_filter_feed_empty_degraded_sources": 0,
        "ai_filter_worker_queue_alert": 0,
        "ai_filter_score_fallback_alert": 0,
        "ai_filter_score_safe_neutral_count_2h": 2,
        "ai_filter_score_safe_neutral_denominator_2h": 8,
        "ai_filter_score_safe_neutral_ratio_2h": 0.25,
        "ai_filter_score_safe_neutral_count_24h": 6,
        "ai_filter_score_safe_neutral_denominator_24h": 26,
        "ai_filter_score_safe_neutral_ratio_24h": 6 / 26,
        "ai_filter_cold_filter_limit": 4,
        "ai_filter_cold_filter_inflight": 0,
        "ai_filter_cold_filter_rejections_total": 0,
    }
    values.update(updates)
    return ("\n".join(f"{name} {value}" for name, value in values.items()) + "\n").encode()


def _settings(tmp_path: Path, **updates) -> monitor.Settings:
    base = monitor.Settings(
        root=tmp_path / "app",
        backup_dir=tmp_path / "backups",
        backup_max_age_seconds=108000,
        state_file=tmp_path / "state" / "state.json",
        cooldown_seconds=1800,
        metrics_url="http://127.0.0.1:5000/metrics",
        api_key_file=None,
        timer_unit="ai-filter-market-close.timer",
        release_registry=tmp_path / "app" / "data" / "releases",
        compose_project="ai-rss-filter",
        required_services=("ai-filter-api", "redis"),
        app_services=("ai-filter-api",),
        offsite_hook=None,
        offsite_state_file=tmp_path / "backups" / ".offsite-upload.state",
        require_offsite=False,
        telegram_token="",
        telegram_chat_id="",
        command_timeout_seconds=5,
    )
    return dataclasses.replace(base, **updates)


def _valid_backup(settings: monitor.Settings, now: int) -> tuple[Path, str]:
    settings.backup_dir.mkdir(parents=True)
    archive = settings.backup_dir / "system_backup_2026-07-19T033001.tar.gz"
    _write_backup_archive(archive, b"verified-backup")
    os.utime(archive, (now - 60, now - 60))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive) + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )
    return archive, digest


def _write_backup_archive(path: Path, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo(
            "2026-07-19/ai-rss-filter/sqlite/data/cache.db"
        )
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))


def _release_registry(
    tmp_path: Path, services: tuple[str, ...]
) -> tuple[Path, str, Path]:
    digest = "sha256:" + "a" * 64
    registry = tmp_path / "app" / "data" / "releases"
    manifests = registry / "manifests"
    manifests.mkdir(parents=True)
    (registry / "release.lock").touch()
    source = b"services: {}\n"
    manifest_hash = hashlib.sha256(source).hexdigest()
    manifest = manifests / f"{manifest_hash}.compose.yml"
    manifest.write_bytes(source)
    (registry / "current.state").write_text(
        f"my-ai-filter:2026.07.19.1\n{digest}\n{manifest_hash}\n"
        f"{' '.join(services)}\n",
        encoding="utf-8",
    )
    return registry, digest, manifest


def test_backup_check_requires_fresh_archive_and_matching_sha(tmp_path):
    now = 2_000_000_000
    settings = _settings(tmp_path)
    archive, _digest = _valid_backup(settings, now)

    result = monitor.check_backup(settings, now)
    assert result.ok is True
    assert archive.name in result.detail

    archive.write_bytes(b"tampered")
    os.utime(archive, (now - 60, now - 60))
    result = monitor.check_backup(settings, now)
    assert result.ok is False
    assert result.detail == "SHA256 mismatch"


def test_backup_check_rejects_stale_archive(tmp_path):
    now = 2_000_000_000
    settings = _settings(tmp_path, backup_max_age_seconds=100)
    archive, _digest = _valid_backup(settings, now)
    os.utime(archive, (now - 101, now - 101))

    result = monitor.check_backup(settings, now)

    assert result.ok is False
    assert result.detail.startswith("stale age_seconds=")


def test_backup_check_rejects_archive_without_canonical_database(tmp_path):
    now = 2_000_000_000
    settings = _settings(tmp_path)
    settings.backup_dir.mkdir(parents=True)
    archive = settings.backup_dir / "system_backup_2026-07-19T033001.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("2026-07-19/ai-rss-filter/source-config.tar.gz")
        member.size = 4
        bundle.addfile(member, io.BytesIO(b"data"))
    os.utime(archive, (now - 60, now - 60))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive) + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )

    result = monitor.check_backup(settings, now)

    assert result.ok is False
    assert "data/cache.db" in result.detail


def test_backup_hash_is_cached_privately_and_new_archive_is_reverified(
    monkeypatch, tmp_path
):
    now = 2_000_000_000
    verification_state = tmp_path / "state" / "backup-verification.json"
    settings = _settings(
        tmp_path,
        backup_verification_state_file=verification_state,
        persist_verification_state=True,
    )
    _valid_backup(settings, now)
    real_hash = monitor._hash_file
    calls = []

    def counted(path, **kwargs):
        calls.append(path.name)
        return real_hash(path, **kwargs)

    monkeypatch.setattr(monitor, "_hash_file", counted)
    first = monitor.check_backup(settings, now)
    second = monitor.check_backup(settings, now + 60)

    assert first.ok is True and "verification=full" in first.detail
    assert second.ok is True and "verification=cached" in second.detail
    assert calls == ["system_backup_2026-07-19T033001.tar.gz"]
    assert verification_state.stat().st_mode & 0o777 == 0o600

    archive = settings.backup_dir / "system_backup_2026-07-20T033001.tar.gz"
    _write_backup_archive(archive, b"next-verified-backup")
    os.utime(archive, (now + 120, now + 120))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive) + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii"
    )
    third = monitor.check_backup(settings, now + 180)

    assert third.ok is True and "verification=full" in third.detail
    assert calls[-1] == archive.name


def test_backup_dry_run_mode_never_writes_verification_state(tmp_path):
    now = 2_000_000_000
    verification_state = tmp_path / "state" / "backup-verification.json"
    settings = _settings(
        tmp_path,
        backup_verification_state_file=verification_state,
        persist_verification_state=False,
    )
    _valid_backup(settings, now)

    result = monitor.check_backup(settings, now)

    assert result.ok is True
    assert "verification=full" in result.detail
    assert not verification_state.exists()


def test_offsite_unconfigured_is_explicit_and_can_be_required(tmp_path):
    optional = monitor.check_offsite(_settings(tmp_path), 2_000_000_000)
    required = monitor.check_offsite(
        _settings(tmp_path, require_offsite=True), 2_000_000_000
    )

    assert optional.ok is True
    assert optional.warning is True
    assert "local-only" in optional.detail
    assert required.ok is False
    assert "unconfigured" in required.detail


def test_restore_drill_requires_private_fresh_success_state(tmp_path):
    now = 2_000_000_000
    state = tmp_path / "state" / "restore-drill.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ok",
                "checked_at": now - 60,
                "archive": "system_backup_2026-07-19T033001.tar.gz",
                "sha256": "a" * 64,
                "required_tables": [
                    "news_cache",
                    "app_meta",
                    "event_mapping_replay_ledger",
                    "event_mapping_replay_dirty_roots",
                    "event_similarity_replay_dirty_roots",
                    "feedback_example_snapshots",
                    "scoring_contract_snapshots",
                ],
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    settings = _settings(
        tmp_path,
        restore_drill_state_file=state,
        restore_drill_max_age_seconds=100,
    )

    assert monitor.check_restore_drill(settings, now).ok is True
    assert monitor.check_restore_drill(settings, now + 101).ok is False

    state.chmod(0o644)
    unsafe = monitor.check_restore_drill(settings, now)
    assert unsafe.ok is False
    assert "unsafe" in unsafe.detail


def test_restore_drill_running_state_warns_briefly_then_fails_closed(tmp_path):
    now = 2_000_000_000
    state = tmp_path / "state" / "restore-drill.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "running",
                "started_at": now - 60,
                "checked_at": now - 60,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    settings = _settings(
        tmp_path,
        restore_drill_state_file=state,
        restore_drill_running_grace_seconds=3000,
    )

    active = monitor.check_restore_drill(settings, now)
    timed_out = monitor.check_restore_drill(settings, now + 2941)

    assert active.ok is True and active.warning is True
    assert "running" in active.detail
    assert timed_out.ok is False
    assert "timeout" in timed_out.detail


def test_event_enrichment_check_rejects_failed_and_stale_state(tmp_path):
    now = 2_000_000_000
    state = tmp_path / "state" / "event-enrichment.json"
    state.parent.mkdir(mode=0o700)
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "degraded",
                "started_at": now - 60,
                "completed_at": now - 30,
                "exit_code": 1,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    settings = _settings(tmp_path, enrichment_state_file=state)

    degraded = monitor.check_event_enrichment(settings, now)
    assert degraded.ok is False
    assert "status=degraded" in degraded.detail

    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["status"] = "ok"
    payload["exit_code"] = 0
    payload["completed_at"] = now - settings.enrichment_max_age_seconds - 1
    payload["started_at"] = payload["completed_at"] - 30
    state.write_text(json.dumps(payload), encoding="utf-8")
    state.chmod(0o600)
    stale = monitor.check_event_enrichment(settings, now)
    assert stale.ok is False
    assert "stale" in stale.detail


def test_event_enrichment_check_accepts_explicit_successful_noop(tmp_path):
    now = 2_000_000_000
    state = tmp_path / "state" / "event-enrichment.json"
    state.parent.mkdir(mode=0o700)
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ok",
                "started_at": now - 10,
                "completed_at": now - 9,
                "exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)

    result = monitor.check_event_enrichment(
        _settings(tmp_path, enrichment_state_file=state), now
    )

    assert result.ok is True
    assert "completed" in result.detail


def _market_database(path: Path, trade_date: str, source_ts: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE future_prices (symbol TEXT, trade_date TEXT, ts INTEGER)"
        )
        connection.executemany(
            "INSERT INTO future_prices VALUES (?, ?, ?)",
            [
                (symbol, trade_date, source_ts)
                for symbol in sorted(monitor._MARKET_REQUIRED_SYMBOLS)
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_market_result(tmp_path: Path, kind: str, **fields) -> None:
    directory = tmp_path / "app" / "data" / "market_close"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "kind": kind,
        "run_id": "a" * 32,
        "trade_date": "2026-08-04",
        "status": "success",
        "started_at": 1,
        "completed_at": 2,
        "due_total": 0,
        "unresolved": 0,
        "truncated": False,
        "written": 4,
        "exit_code": 0,
    }
    payload.update(fields)
    path = directory / f"{kind}_last.json"
    path.write_text(json.dumps(payload) + "\n", encoding="ascii")
    path.chmod(0o600)


def test_market_timer_checks_service_result_and_expected_database_date(
    monkeypatch, tmp_path
):
    zone = ZoneInfo("Asia/Shanghai")
    now = int(datetime(2026, 8, 4, 16, 30, tzinfo=zone).timestamp())
    close_ts = int(datetime(2026, 8, 4, 15, 0, tzinfo=zone).timestamp())
    database_path = tmp_path / "cache.db"
    _market_database(database_path, "2026-08-04", close_ts)
    settings = _settings(tmp_path, database_path=database_path)

    def systemctl(command, timeout):
        del timeout
        if command[1] in {"is-enabled", "is-active"}:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "--value" in command:
            return subprocess.CompletedProcess(command, 0, "Wed 2026-08-05 15:10:00 CST\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            "ActiveState=inactive\nResult=success\nExecMainStatus=0\n"
            "ExecMainExitTimestamp=Tue 2026-08-04 15:11:00 CST\n",
            "",
        )

    monkeypatch.setattr(monitor, "_run", systemctl)
    missing_result = monitor.check_market_timer(settings, now)
    assert missing_result.ok is False
    assert "collector result missing" in missing_result.detail

    _write_market_result(tmp_path, "collector", status="skipped_maintenance", trade_date="")
    skipped = monitor.check_market_timer(settings, now)
    assert skipped.ok is False
    assert "skipped maintenance" in skipped.detail

    _write_market_result(tmp_path, "collector", status="success")
    healthy = monitor.check_market_timer(settings, now)
    assert healthy.ok is True
    assert "collector success" in healthy.detail
    assert "database_trade_date=2026-08-04" in healthy.detail

    _write_market_result(tmp_path, "collector", status="incomplete")
    incomplete = monitor.check_market_timer(settings, now)
    assert incomplete.ok is False
    assert "repair result missing" in incomplete.detail

    _write_market_result(tmp_path, "repair", status="complete", started_at=3, completed_at=4)
    repaired = monitor.check_market_timer(settings, now)
    assert repaired.ok is True
    assert repaired.warning is True
    assert "same-day repair complete" in repaired.detail

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("DELETE FROM future_prices WHERE symbol = 'sh000001'")
        connection.commit()
    finally:
        connection.close()
    missing_after_failure = monitor.check_market_timer(settings, now)
    assert missing_after_failure.ok is False
    assert "missing required benchmark" in missing_after_failure.detail

    monkeypatch.setattr(monitor, "_run", systemctl)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO future_prices VALUES (?, ?, ?)",
        ("sh000001", "2026-08-04", close_ts),
    )
    connection.commit()
    connection.close()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE future_prices SET ts = NULL WHERE symbol = 'sh000001'"
        )
        connection.commit()
    finally:
        connection.close()
    malformed = monitor.check_market_timer(settings, now)
    assert malformed.ok is False
    assert malformed.name == "market_timer"


def test_market_timer_rejects_coverage_replay_success(tmp_path):
    _write_market_result(
        tmp_path,
        "collector",
        status="success",
        written=0,
        due_total=0,
        detail=(
            "replay: maintenance skip cleared after verified coverage; "
            "benchmark_rows=4 pending=0"
        ),
    )
    replayed = monitor._market_timer_from_results(_settings(tmp_path), "2026-08-04", 4)
    assert replayed.ok is False
    assert "coverage replay" in replayed.detail
    assert "not natural close" in replayed.detail

    _write_market_result(tmp_path, "collector", status="success")
    natural = monitor._market_timer_from_results(_settings(tmp_path), "2026-08-04", 4)
    assert natural.ok is True
    assert "coverage replay" not in natural.detail


def test_market_history_reports_backlog_without_failing_same_day(tmp_path):
    now = int(datetime(2026, 8, 4, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    missing = monitor.check_market_history(_settings(tmp_path), now)
    assert missing.ok is True
    assert missing.warning is True
    assert "not yet reported" in missing.detail

    _write_market_result(
        tmp_path,
        "history",
        status="incomplete",
        trade_date="2026-08-03",
        remaining_total=12,
        truncated=True,
        unresolved=1,
        started_at=now - 1800,
        completed_at=now - 1740,
    )
    remaining = monitor.check_market_history(_settings(tmp_path), now)
    assert remaining.ok is True
    assert remaining.warning is True
    assert "historical pending remain" in remaining.detail
    assert "total=12" in remaining.detail

    _write_market_result(
        tmp_path,
        "history",
        status="error",
        detail="split database paths",
    )
    failed = monitor.check_market_history(_settings(tmp_path), now)
    assert failed.ok is False


def test_root_disk_warns_before_it_fails(monkeypatch, tmp_path):
    class _Usage:
        def __init__(self, used, total, free):
            self.used = used
            self.total = total
            self.free = free

    database = tmp_path / "cache.db"
    database.write_bytes(b"x" * 2048)
    settings = _settings(tmp_path, database_path=database)
    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda path: _Usage(47, 59, 9.2))
    warned = monitor.check_root_disk(settings, 1)
    assert warned.ok is True
    assert warned.warning is True
    assert "root_disk_used_percent=84" in warned.detail
    assert "cache_db_bytes=2048" in warned.detail

    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda path: _Usage(90, 100, 10))
    failed = monitor.check_root_disk(settings, 1)
    assert failed.ok is False
    assert "root_disk_used_percent=90" in failed.detail


@pytest.mark.parametrize(
    "started,completed,accepted",
    [(1, 2, False), (2, 3, False), (3, 4, True), (None, 4, False), (4, 3, False)],
)
def test_same_day_repair_must_follow_collector_completion(tmp_path, started, completed, accepted):
    _write_market_result(tmp_path, "collector", status="incomplete", completed_at=2)
    _write_market_result(tmp_path, "repair", status="complete",
                         started_at=started, completed_at=completed)
    result = monitor._market_timer_from_results(_settings(tmp_path), "2026-08-04", 4)
    assert result.ok is accepted


@pytest.mark.parametrize(
    "now_text,run_text,accepted",
    [
        ("2026-09-13T17:00:00", "2026-09-11T16:30:00", True),
        ("2026-09-14T16:35:29", "2026-09-11T16:30:00", True),
        ("2026-09-14T16:35:30", "2026-09-11T16:30:00", False),
        ("2026-09-25T17:00:00", "2026-09-24T16:30:00", True),
        ("2026-09-28T17:00:00", "2026-09-24T16:30:00", False),
        ("2026-09-13T17:00:00", "2026-09-14T16:30:00", False),
    ],
)
def test_history_completion_freshness_follows_trading_schedule(tmp_path, now_text, run_text, accepted):
    zone = ZoneInfo("Asia/Shanghai")
    now = int(datetime.fromisoformat(now_text).replace(tzinfo=zone).timestamp())
    started = int(datetime.fromisoformat(run_text).replace(tzinfo=zone).timestamp())
    _write_market_result(tmp_path, "history", status="complete",
                         trade_date="2026-08-03", started_at=started, completed_at=started + 60)
    result = monitor.check_market_history(_settings(tmp_path), now)
    assert result.ok is accepted
    if not accepted:
        assert "stale or future" in result.detail


@pytest.mark.parametrize("used,expected,ok,warning", [
    (790, 79, True, False), (791, 80, True, True), (795, 80, True, True),
    (890, 89, True, True), (891, 90, False, False), (895, 90, False, False),
])
def test_root_disk_uses_df_ceiling_at_thresholds(monkeypatch, tmp_path, used, expected, ok, warning):
    usage = type("Usage", (), {"used": used, "free": 1000 - used})()
    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda _path: usage)
    result = monitor.check_root_disk(_settings(tmp_path), 1)
    assert result.ok is ok
    assert result.warning is warning
    assert f"root_disk_used_percent={expected}" in result.detail


def test_alert_cooldown_deduplicates_and_recovery_is_once(tmp_path):
    del tmp_path
    failure = [monitor.CheckResult("backup", False, "stale")]
    changed_detail = [monitor.CheckResult("backup", False, "sha mismatch")]
    changed_check = [
        monitor.CheckResult("backup", False, "stale"),
        monitor.CheckResult("timer", False, "inactive"),
    ]
    healthy = [monitor.CheckResult("backup", True, "fresh")]
    now = 1000

    assert monitor.notification_decision({}, failure, now, 300) == "failure"
    previous = {
        "status": "failing",
        "fingerprint": monitor._fingerprint(failure),
        "last_notice_at": now,
    }
    assert monitor.notification_decision(previous, failure, now + 299, 300) == ""
    assert monitor.notification_decision(previous, failure, now + 300, 300) == "failure"
    assert monitor.notification_decision(previous, changed_detail, now + 1, 300) == "failure"
    assert monitor.notification_decision(previous, changed_check, now + 1, 300) == "failure"
    assert monitor.notification_decision(previous, healthy, now + 1, 300) == "recovery"
    assert monitor.notification_decision({"status": "healthy"}, healthy, now, 300) == ""


def test_metrics_probe_uses_key_without_exposing_it(monkeypatch, tmp_path):
    secret = "super-secret-api-key"
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", secret)
    captured = {}

    def successful_get(_url, **kwargs):
        captured.update(kwargs["headers"])
        return 200, _healthy_metrics()

    monkeypatch.setattr(monitor, "_http_get", successful_get)
    assert monitor.check_metrics(settings, 1).ok is True
    assert captured["X-API-Key"] == secret

    def failed_get(_url, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(monitor, "_http_get", failed_get)
    failed = monitor.check_metrics(settings, 1)
    assert failed.ok is False
    assert secret not in failed.detail


@pytest.mark.parametrize(
    ("metric", "detail"),
    [
        ("ai_filter_refresh_stale", "refresh_stale"),
        ("ai_filter_refresh_degraded", "refresh_degraded"),
        ("ai_filter_worker_queue_alert", "worker_queue_alert"),
        ("ai_filter_score_fallback_alert", "score_fallback_alert"),
    ],
)
def test_metrics_probe_fails_on_critical_red_gauge(monkeypatch, tmp_path, metric, detail):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", "safe-test-key")
    monkeypatch.setattr(
        monitor,
        "_http_get",
        lambda *_args, **_kwargs: (200, _healthy_metrics(**{metric: 0 if metric.endswith("_ok") or metric.endswith("_available") else 1})),
    )

    result = monitor.check_metrics(settings, 1)

    assert result.ok is False
    assert detail in result.detail


def test_metrics_probe_warns_on_cold_filter_capacity(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", "safe-test-key")
    monkeypatch.setattr(
        monitor,
        "_http_get",
        lambda *_args, **_kwargs: (
            200,
            _healthy_metrics(
                ai_filter_cold_filter_inflight=4,
                ai_filter_cold_filter_rejections_total=2,
            ),
        ),
    )

    result = monitor.check_metrics(settings, 1)

    assert result.ok is True
    assert result.warning is True
    assert "cold_filter_at_capacity" in result.detail
    assert "cold_filter_rejections_seen" in result.detail


def test_metrics_probe_reports_safe_neutral_as_info_only(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", "safe-test-key")
    monkeypatch.setattr(
        monitor,
        "_http_get",
        lambda *_args, **_kwargs: (200, _healthy_metrics()),
    )

    result = monitor.check_metrics(settings, 1)

    assert result.ok is True
    assert result.warning is False
    assert "safe_neutral_2h=2/8 ratio=0.250000" in result.detail
    assert "safe_neutral_24h=6/26 ratio=0.230769" in result.detail


def test_metrics_probe_keeps_safe_neutral_metrics_optional(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", "safe-test-key")
    payload = _healthy_metrics().decode()
    payload = "\n".join(
        line for line in payload.splitlines() if "score_safe_neutral" not in line
    ) + "\n"
    monkeypatch.setattr(
        monitor, "_http_get", lambda *_args, **_kwargs: (200, payload.encode())
    )

    result = monitor.check_metrics(settings, 1)

    assert result.ok is True
    assert result.warning is False
    assert "safe_neutral" not in result.detail


def test_metrics_probe_fails_closed_when_required_metric_is_missing(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setenv("AI_FILTER_API_KEY", "safe-test-key")
    payload = _healthy_metrics().decode().replace("ai_filter_worker_queue_alert 0\n", "")
    monkeypatch.setattr(
        monitor, "_http_get", lambda *_args, **_kwargs: (200, payload.encode())
    )

    result = monitor.check_metrics(settings, 1)

    assert result.ok is False
    assert "ai_filter_worker_queue_alert" in result.detail


def test_telegram_transport_failure_never_returns_token(monkeypatch, tmp_path):
    token = "telegram-secret-token"
    settings = _settings(
        tmp_path, telegram_token=token, telegram_chat_id="123456"
    )

    def fail(_request, timeout):
        del timeout
        raise RuntimeError(token)

    monkeypatch.setattr(monitor.urllib.request, "urlopen", fail)
    sent, detail = monitor.send_telegram(settings, "safe message")

    assert sent is False
    assert token not in detail
    assert detail == "transport failure"


def test_telegram_credentials_fall_back_to_private_application_env(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OPS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPS_TELEGRAM_CHAT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=existing-private-token\nTELEGRAM_CHAT_ID=9988\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    token, chat_id = monitor._load_telegram_credentials(tmp_path)

    assert token == "existing-private-token"
    assert chat_id == "9988"

    env_file.chmod(0o644)
    assert monitor._load_telegram_credentials(tmp_path) == ("", "")


def test_dedicated_telegram_credentials_take_precedence_without_mixing(
    monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=application-token\nTELEGRAM_CHAT_ID=1122\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("OPS_TELEGRAM_BOT_TOKEN", "dedicated-token")
    monkeypatch.delenv("OPS_TELEGRAM_CHAT_ID", raising=False)

    assert monitor._load_telegram_credentials(tmp_path) == ("dedicated-token", "")


def test_notification_transport_is_explicit_when_unconfigured(tmp_path):
    unconfigured = monitor.check_notification_transport(_settings(tmp_path), 1)
    incomplete = monitor.check_notification_transport(
        _settings(tmp_path, telegram_token="token-only"), 1
    )
    configured = monitor.check_notification_transport(
        _settings(tmp_path, telegram_token="token", telegram_chat_id="123"), 1
    )

    assert unconfigured.ok is True and unconfigured.warning is True
    assert incomplete.ok is False
    assert configured.ok is True and configured.warning is False


def test_failed_notification_remains_pending_for_next_run():
    failure = [monitor.CheckResult("metrics", False, "refresh_stale")]
    previous = {
        "status": "failing",
        "fingerprint": monitor._fingerprint(failure),
        "last_notice_at": 1000,
        "pending_notification": "failure",
    }

    assert monitor.notification_decision(previous, failure, 1001, 1800) == "failure"


def test_release_check_uses_current_five_service_state_and_ignores_stale_config(
    monkeypatch, tmp_path
):
    current_services = (
        "redis",
        "rsshub",
        "ai-filter-web",
        "ai-filter-api",
        "ai-filter-worker",
    )
    registry, digest, manifest = _release_registry(tmp_path, current_services)
    settings = _settings(
        tmp_path,
        release_registry=registry,
        required_services=current_services + ("ai-filter-sse", "retired-sidecar"),
        app_services=(
            "ai-filter-api",
            "ai-filter-worker",
            "ai-filter-sse",
            "retired-sidecar",
        ),
    )
    monkeypatch.setattr(
        monitor,
        "_read_manifest_runtime_services",
        lambda _settings, _manifest: {
            service: True for service in current_services
        },
    )
    found: list[str] = []

    def find_container(_settings, service):
        found.append(service)
        return f"cid-{service}"

    def healthy(command, timeout):
        del timeout
        service = command[-1].removeprefix("cid-")
        image = (
            digest
            if service in {"ai-filter-api", "ai-filter-worker"}
            else "sha256:" + "b" * 64
        )
        return subprocess.CompletedProcess(
            command, 0, f"running|healthy|{image}|0|false\n", ""
        )

    monkeypatch.setattr(monitor, "_service_container_id", find_container)
    monkeypatch.setattr(monitor, "_run", healthy)

    result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is True
    assert found == list(current_services)

    manifest.write_text("services:\n  drifted: {}\n", encoding="utf-8")
    found.clear()
    result = monitor.check_release_and_containers(settings, 1)
    assert result.ok is False
    assert result.detail == "archived manifest missing or drifted"
    assert found == []


def test_release_check_supports_legacy_six_service_rollback(monkeypatch, tmp_path):
    legacy_services = (
        "redis",
        "rsshub",
        "ai-filter-web",
        "ai-filter-api",
        "ai-filter-sse",
        "ai-filter-worker",
    )
    registry, digest, _manifest = _release_registry(tmp_path, legacy_services)
    settings = _settings(
        tmp_path,
        release_registry=registry,
        required_services=(
            "redis",
            "rsshub",
            "ai-filter-web",
            "ai-filter-api",
            "ai-filter-worker",
        ),
        app_services=("ai-filter-api", "ai-filter-worker"),
    )
    monkeypatch.setattr(
        monitor,
        "_read_manifest_runtime_services",
        lambda _settings, _manifest: {
            service: True for service in legacy_services
        },
    )
    found: list[str] = []

    def find_container(_settings, service):
        found.append(service)
        return f"cid-{service}"

    def healthy(command, timeout):
        del timeout
        service = command[-1].removeprefix("cid-")
        image = (
            digest
            if service in {"ai-filter-api", "ai-filter-worker", "ai-filter-sse"}
            else "sha256:" + "b" * 64
        )
        return subprocess.CompletedProcess(
            command, 0, f"running|healthy|{image}|0|false\n", ""
        )

    monkeypatch.setattr(monitor, "_service_container_id", find_container)
    monkeypatch.setattr(monitor, "_run", healthy)

    result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is True
    assert found == list(legacy_services)


@pytest.mark.parametrize(
    "drifted_service",
    ["ai-filter-api", "ai-filter-worker", "ai-filter-sse"],
)
def test_release_check_rejects_current_and_legacy_app_image_drift(
    monkeypatch, tmp_path, drifted_service
):
    legacy_services = (
        "redis",
        "rsshub",
        "ai-filter-web",
        "ai-filter-api",
        "ai-filter-sse",
        "ai-filter-worker",
    )
    registry, digest, _manifest = _release_registry(tmp_path, legacy_services)
    settings = _settings(
        tmp_path,
        release_registry=registry,
        app_services=("ai-filter-api", "ai-filter-worker"),
    )
    monkeypatch.setattr(
        monitor,
        "_read_manifest_runtime_services",
        lambda _settings, _manifest: {
            service: True for service in legacy_services
        },
    )
    monkeypatch.setattr(
        monitor, "_service_container_id", lambda _settings, service: f"cid-{service}"
    )

    def inspect_with_app_drift(command, timeout):
        del timeout
        service = command[-1].removeprefix("cid-")
        if service == drifted_service:
            image = "sha256:" + "c" * 64
        elif service in {"ai-filter-api", "ai-filter-worker", "ai-filter-sse"}:
            image = digest
        else:
            image = "sha256:" + "b" * 64
        return subprocess.CompletedProcess(
            command, 0, f"running|healthy|{image}|0|false\n", ""
        )

    monkeypatch.setattr(monitor, "_run", inspect_with_app_drift)

    result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is False
    assert result.detail == f"service {drifted_service} image drift"


@pytest.mark.parametrize(
    "drift_line",
    [
        "C /app/core/processor.py",
        "A /app/services/new_runtime.py",
        "D /app/core/scorer.py",
        "C /app/config.yaml",
    ],
)
def test_release_check_rejects_runtime_source_or_config_drift(
    monkeypatch, tmp_path, drift_line
):
    services = ("redis", "ai-filter-api", "ai-filter-worker")
    registry, digest, _manifest = _release_registry(tmp_path, services)
    settings = _settings(
        tmp_path,
        release_registry=registry,
        app_services=("ai-filter-api", "ai-filter-worker"),
    )
    monkeypatch.setattr(
        monitor,
        "_read_manifest_runtime_services",
        lambda _settings, _manifest: {service: True for service in services},
    )
    monkeypatch.setattr(
        monitor, "_service_container_id", lambda _settings, service: f"cid-{service}"
    )

    def inspect_and_diff(command, timeout):
        del timeout
        service = command[-1].removeprefix("cid-")
        if command[1] == "diff":
            output = f"{drift_line}\n" if service == "ai-filter-worker" else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        image = digest if service in settings.app_services else "sha256:" + "b" * 64
        return subprocess.CompletedProcess(
            command, 0, f"running|healthy|{image}|0|false\n", ""
        )

    monkeypatch.setattr(monitor, "_run", inspect_and_diff)

    result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is False
    assert result.detail == "service ai-filter-worker source/config drift"


@pytest.mark.parametrize(
    ("recorded_services", "manifest_services"),
    [
        (
            ("redis", "rsshub", "ai-filter-web", "ai-filter-api"),
            (
                "redis",
                "rsshub",
                "ai-filter-web",
                "ai-filter-api",
                "ai-filter-worker",
            ),
        ),
        (
            (
                "redis",
                "rsshub",
                "ai-filter-web",
                "ai-filter-api",
                "ai-filter-worker",
            ),
            (
                "redis",
                "rsshub",
                "ai-filter-web",
                "ai-filter-api",
                "ai-filter-sse",
                "ai-filter-worker",
            ),
        ),
        (
            (
                "redis",
                "rsshub",
                "ai-filter-web",
                "ai-filter-api",
                "ai-filter-worker",
                "retired-sidecar",
            ),
            (
                "redis",
                "rsshub",
                "ai-filter-web",
                "ai-filter-api",
                "ai-filter-worker",
            ),
        ),
    ],
    ids=["missing-current-worker", "missing-legacy-sse", "extra-recorded-service"],
)
def test_release_check_binds_recorded_services_to_manifest_default_topology(
    monkeypatch, tmp_path, recorded_services, manifest_services
):
    registry, _digest, _manifest = _release_registry(tmp_path, recorded_services)
    settings = _settings(tmp_path, release_registry=registry)
    monkeypatch.setattr(
        monitor,
        "_read_manifest_runtime_services",
        lambda _settings, _manifest: {
            service: True for service in manifest_services
        },
    )
    monkeypatch.setattr(
        monitor,
        "_service_container_id",
        lambda *_args: pytest.fail("topology mismatch must fail before inspection"),
    )

    result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is False
    assert result.detail == "release services do not match archived manifest"


def test_release_check_rejects_missing_health_state_when_manifest_requires_it(
    monkeypatch, tmp_path
):
    legacy_services = (
        "redis",
        "rsshub",
        "ai-filter-web",
        "ai-filter-api",
        "ai-filter-sse",
        "ai-filter-worker",
    )
    registry, digest, _manifest = _release_registry(tmp_path, legacy_services)
    settings = _settings(tmp_path, release_registry=registry)
    monkeypatch.setattr(
        monitor,
        "_read_manifest_runtime_services",
        lambda _settings, _manifest: {
            service: service == "ai-filter-sse" for service in legacy_services
        },
    )
    monkeypatch.setattr(
        monitor, "_service_container_id", lambda _settings, service: f"cid-{service}"
    )

    def inspect_without_sse_health(command, timeout):
        del timeout
        service = command[-1].removeprefix("cid-")
        health = "none" if service == "ai-filter-sse" else "healthy"
        image = (
            digest
            if service in {"ai-filter-api", "ai-filter-worker", "ai-filter-sse"}
            else "sha256:" + "b" * 64
        )
        return subprocess.CompletedProcess(
            command, 0, f"running|{health}|{image}|0|false\n", ""
        )

    monkeypatch.setattr(monitor, "_run", inspect_without_sse_health)

    result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is False
    assert result.detail == "service ai-filter-sse is not healthy"


def test_manifest_runtime_model_excludes_profiles_and_uses_sterile_environment(
    monkeypatch, tmp_path
):
    settings = _settings(tmp_path)
    manifest = tmp_path / "manifest.yml"
    manifest.write_text("services: {}\n", encoding="utf-8")
    captured = {}
    model = {
        "services": {
            "redis": {"healthcheck": {"test": ["CMD", "true"]}},
            "ai-filter-worker": {},
            "market-close-collector": {"profiles": ["cron"]},
        }
    }

    def render(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, json.dumps(model), "")

    monkeypatch.setenv("SHOULD_NOT_REACH_COMPOSE", "secret-value")
    monkeypatch.setattr(monitor.subprocess, "run", render)

    services = monitor._read_manifest_runtime_services(settings, manifest)

    assert services == {"redis": True, "ai-filter-worker": False}
    assert captured["kwargs"]["env"] == {
        "PATH": os.environ.get("PATH", os.defpath)
    }
    assert captured["command"][captured["command"].index("--env-file") + 1] == os.devnull
    assert "--no-interpolate" in captured["command"]
    assert "--no-env-resolution" in captured["command"]


@pytest.mark.parametrize(
    "services",
    [
        ("redis", "redis"),
        (".redis",),
        ("-redis",),
        ("redis/bad",),
    ],
)
def test_release_state_rejects_duplicate_or_unsafe_services(tmp_path, services):
    registry, _digest, _manifest = _release_registry(tmp_path, services)

    with pytest.raises(ValueError, match="release services malformed"):
        monitor._read_release_state(registry)


def test_release_check_defers_while_release_lock_is_exclusive(monkeypatch, tmp_path):
    registry = tmp_path / "app" / "data" / "releases"
    registry.mkdir(parents=True)
    lock_path = registry / "release.lock"
    lock_path.touch()
    settings = _settings(tmp_path, release_registry=registry)
    monkeypatch.setattr(
        monitor,
        "_service_container_id",
        lambda *_args: pytest.fail("container inspection must wait for release lock"),
    )

    with lock_path.open("r", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = monitor.check_release_and_containers(settings, 1)

    assert result.ok is True
    assert result.warning is True
    assert "deferred" in result.detail


def test_configuration_file_must_be_private(tmp_path):
    config = tmp_path / "ops.env"
    config.write_text("OPS_ALERT_COOLDOWN_SECONDS=10\n", encoding="utf-8")
    config.chmod(0o644)

    with pytest.raises(ValueError, match="ownership or mode is unsafe"):
        monitor._load_configuration_files((config,))


def test_dry_run_writes_no_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        monitor,
        "CHECKS",
        (lambda _settings, _now: monitor.CheckResult("test", True, "ready"),),
    )
    state = tmp_path / "state" / "state.json"
    rc = monitor.main(
        [
            "--config",
            str(tmp_path / "missing-monitor.env"),
            "--backup-config",
            str(tmp_path / "missing-backup.env"),
            "--root",
            str(tmp_path / "app"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--state-file",
            str(state),
            "--dry-run",
        ]
    )

    assert rc == 0
    assert not state.exists()
    assert "OK test: ready" in capsys.readouterr().out


def test_only_loopback_http_urls_are_allowed():
    assert monitor._is_loopback_url("http://127.0.0.1:5000/metrics")
    assert monitor._is_loopback_url("http://[::1]:5001/readiness")
    assert not monitor._is_loopback_url("https://127.0.0.1/metrics")
    assert not monitor._is_loopback_url("http://example.com/metrics")
    assert not monitor._is_loopback_url("http://user:pass@127.0.0.1/metrics")
