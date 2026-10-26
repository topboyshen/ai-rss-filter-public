"""Only the existing host run may publish an allowlisted dashboard snapshot."""

import dataclasses
import json
import stat

import pytest

from ops import ai_filter_ops_check as monitor
from tests.test_ops_monitor import _release_registry, _settings


NOW = 2_000_000_000


@pytest.fixture
def host_run(monkeypatch, tmp_path):
    registry, _, _ = _release_registry(tmp_path, ("ai-filter-api", "redis"))
    settings = _settings(tmp_path, release_registry=registry)
    monkeypatch.setattr(monitor, "_load_configuration_files", lambda _paths: None)
    monkeypatch.setattr(monitor, "settings_from_env", lambda _args: settings)
    monkeypatch.setattr(monitor.time, "time", lambda: NOW + 7)
    monkeypatch.setattr(
        monitor, "evaluate",
        lambda _settings, _now: [
            monitor.CheckResult("release_containers", True, "private detail"),
            monitor.CheckResult("root_disk", True, "private path", warning=True),
            monitor.CheckResult("backup", True, "NO_BACKUP", skipped=True),
        ],
    )
    return settings


def _snapshot(settings):
    database = settings.database_path or settings.root / "data" / "cache.db"
    return database.parent / "ops" / "host_status.json"


def test_normal_run_exports_typed_snapshot_without_private_fields(host_run):
    assert monitor.main(["--now", str(NOW), "--quiet"]) == 0

    path = _snapshot(host_run)
    payload = json.loads(path.read_text())
    assert payload == {
        "version": 1,
        "producer": "ai-filter-ops-check",
        "started_at": NOW,
        "completed_at": NOW + 7,
        "release_ref": "my-ai-filter:2026.07.19.1",
        "checks": [
            {"name": "release_containers", "status": "PASS"},
            {"name": "root_disk", "status": "WARN"},
            {"name": "backup", "status": "SKIPPED_BY_POLICY"},
        ],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    private_state = json.loads(host_run.state_file.read_text())
    assert private_state["status"] == "healthy"
    assert private_state["warnings"] == ["root_disk"]
    assert "checks" not in private_state


def test_dry_run_does_not_create_snapshot_or_alert_state(host_run):
    assert monitor.main(["--now", str(NOW), "--dry-run", "--json"]) == 0

    assert not _snapshot(host_run).exists()
    assert not host_run.state_file.exists()


def test_existing_dry_run_snapshot_is_not_replaced(host_run):
    path = _snapshot(host_run)
    path.parent.mkdir()
    path.write_bytes(b"older snapshot")

    monitor.main(["--now", str(NOW), "--dry-run"])

    assert path.read_bytes() == b"older snapshot"


def test_check_allowlist_drops_secrets_and_normalizes_budget_failures():
    results = [
        monitor.CheckResult("check_release_and_containers", False, "token=secret"),
        monitor.CheckResult("check_metrics", False, "/private/path"),
        monitor.CheckResult("check_offsite", True, "private", skipped=True),
        monitor.CheckResult("unrecognized-secret-name", False, "token=secret"),
    ]

    assert monitor._dashboard_check_rows(results) == [
        {"name": "release_containers", "status": "FAIL"},
        {"name": "metrics", "status": "FAIL"},
        {"name": "offsite_upload", "status": "SKIPPED_BY_POLICY"},
    ]


def test_duplicate_check_names_do_not_produce_healthy_snapshot():
    results = [
        monitor.CheckResult("metrics", True, "first"),
        monitor.CheckResult("check_metrics", False, "second"),
    ]

    assert monitor._dashboard_check_rows(results) == []


def test_failed_policy_check_cannot_be_reported_as_skipped():
    result = monitor.CheckResult("backup", False, "invalid policy", skipped=True)

    assert monitor._dashboard_check_rows([result]) == [
        {"name": "backup", "status": "FAIL"}
    ]


@pytest.mark.parametrize("change", ["ref", "digest", "missing", "invalid"])
def test_release_change_during_check_clears_binding(host_run, monkeypatch, change):
    def changed(_settings, _now):
        path = host_run.release_registry / "current.state"
        lines = path.read_text().splitlines()
        if change == "missing":
            path.unlink()
        elif change == "invalid":
            path.write_text("malformed")
        else:
            if change == "ref":
                lines[0] = "my-ai-filter:2026.07.19.2"
            else:
                lines[1] = "sha256:" + "b" * 64
            path.write_text("\n".join(lines) + "\n")
        return [monitor.CheckResult("release_containers", True, "ready")]

    monkeypatch.setattr(monitor, "evaluate", changed)

    assert monitor.main(["--now", str(NOW), "--quiet"]) == 0
    assert json.loads(_snapshot(host_run).read_text())["release_ref"] == ""


def test_missing_initial_release_cannot_bind_to_later_state(host_run, monkeypatch):
    path = host_run.release_registry / "current.state"
    original = path.read_text()
    path.unlink()

    def created(_settings, _now):
        path.write_text(original)
        return [monitor.CheckResult("release_containers", True, "ready")]

    monkeypatch.setattr(monitor, "evaluate", created)

    assert monitor.main(["--now", str(NOW), "--quiet"]) == 0
    assert json.loads(_snapshot(host_run).read_text())["release_ref"] == ""


def test_arbitrary_release_ref_is_not_exported(host_run):
    path = host_run.release_registry / "current.state"
    lines = path.read_text().splitlines()
    lines[0] = "https://secret-user:secret-token@example.test/image"
    path.write_text("\n".join(lines) + "\n")

    assert monitor.main(["--now", str(NOW), "--quiet"]) == 0
    assert json.loads(_snapshot(host_run).read_text())["release_ref"] == ""


def test_database_override_controls_export_path(host_run):
    settings = dataclasses.replace(
        host_run, database_path=host_run.root / "separate" / "db.sqlite"
    )
    monitor._export_dashboard_snapshot(
        settings, [monitor.CheckResult("metrics", True, "private")],
        NOW, NOW + 1, monitor._dashboard_release_identity(settings),
    )

    assert _snapshot(settings).exists()
    assert not _snapshot(host_run).exists()


def test_atomic_export_failure_preserves_old_snapshot(host_run, monkeypatch, capsys):
    path = _snapshot(host_run)
    path.parent.mkdir()
    path.write_bytes(b"original snapshot")
    data_mode = stat.S_IMODE(path.parent.parent.stat().st_mode)

    def failed_replace(*_args, **_kwargs):
        raise PermissionError("private path secret-token")

    monkeypatch.setattr(monitor.os, "replace", failed_replace)
    monitor._export_dashboard_snapshot(host_run, [], NOW, NOW + 1, None)

    assert path.read_bytes() == b"original snapshot"
    assert list(path.parent.iterdir()) == [path]
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == data_mode
    assert capsys.readouterr().err == "WARN dashboard snapshot: export failed\n"


def test_linked_output_directory_is_not_followed(host_run, tmp_path, capsys):
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    target_mode = stat.S_IMODE(target.stat().st_mode)
    _snapshot(host_run).parent.symlink_to(target, target_is_directory=True)

    monitor._export_dashboard_snapshot(host_run, [], NOW, NOW + 1, None)

    assert not list(target.iterdir())
    assert stat.S_IMODE(target.stat().st_mode) == target_mode
    assert capsys.readouterr().err == "WARN dashboard snapshot: export failed\n"


def test_export_failure_does_not_change_health_or_notification_policy(
    host_run, monkeypatch, capsys
):
    def failed_export(*_args, **_kwargs):
        raise OSError("secret-token")

    sent = []
    monkeypatch.setattr(monitor, "_write_dashboard_snapshot", failed_export)
    monkeypatch.setattr(
        monitor, "evaluate",
        lambda _settings, _now: [monitor.CheckResult("metrics", False, "offline")],
    )
    monkeypatch.setattr(
        monitor, "send_telegram",
        lambda _settings, message: (sent.append(message) is None, "sent"),
    )

    assert monitor.main(["--now", str(NOW), "--quiet"]) == 1

    assert len(sent) == 1
    state = json.loads(host_run.state_file.read_text())
    assert state["status"] == "failing"
    assert state["notification"] == "sent"
    assert "secret-token" not in capsys.readouterr().err


def test_successful_health_exit_stays_successful_if_export_fails(host_run, monkeypatch):
    def failed_export(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(monitor, "_write_dashboard_snapshot", failed_export)

    assert monitor.main(["--now", str(NOW), "--quiet"]) == 0


def test_existing_snapshot_file_link_does_not_overwrite_target(host_run, tmp_path):
    path = _snapshot(host_run)
    path.parent.mkdir()
    target = tmp_path / "private"
    target.write_text("private material")
    path.symlink_to(target)

    monitor._export_dashboard_snapshot(host_run, [], NOW, NOW + 1, None)

    assert target.read_text() == "private material"
    assert not path.is_symlink()
    assert json.loads(path.read_text())["checks"] == []
