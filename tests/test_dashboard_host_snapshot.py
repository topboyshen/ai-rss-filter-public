"""Read-only homepage consumption of the existing hourly host snapshot."""

import json
from pathlib import Path

import pytest

from services import dashboard_ops as ops
from tests import test_dashboard_browser_auth as browser_tests
from tests import test_dashboard_ops as dashboard_tests


dashboard_browser = browser_tests.dashboard_browser
integration_client = dashboard_tests.integration_client
NOW = dashboard_tests.NOW
RELEASE = "my-ai-filter:2026.09.19.test"
CHECK_NAMES = (
    "release_containers",
    "metrics",
    "notification_transport",
    "event_enrichment",
    "market_timer",
    "market_history",
    "root_disk",
    "backup",
    "restore_drill",
    "offsite_upload",
)


def _payload(**overrides):
    payload = {
        "version": 1,
        "producer": "ai-filter-ops-check",
        "started_at": int(NOW - 120),
        "completed_at": int(NOW - 60),
        "release_ref": RELEASE,
        "checks": [{"name": name, "status": "PASS"} for name in CHECK_NAMES],
    }
    payload.update(overrides)
    return payload


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read(tmp_path, payload, *, expected_release=RELEASE):
    return ops.read_host_ops_snapshot(
        _write(tmp_path / "host_status.json", payload), expected_release, now=NOW
    )


def _change_check(payload, name, status):
    next(check for check in payload["checks"] if check["name"] == name)["status"] = (
        status
    )
    return payload


def _check(result, name):
    return next(check for check in result["checks"] if check["name"] == name)


def test_fresh_snapshot_preserves_public_checks_without_claiming_live_health(tmp_path):
    result = _read(tmp_path, _payload())

    assert result["status"] == "ok"
    assert result["label"] == "发布与容器：检查通过"
    assert "不是实时健康" in result["detail"]
    assert result["checked_at"] == "09-21 15:59:00 CST"
    assert [check["name"] for check in result["checks"]] == list(CHECK_NAMES)
    assert all(check["status"] == "ok" for check in result["checks"])


@pytest.mark.parametrize(
    "updates",
    [
        {"started_at": int(NOW - ops.HOST_SNAPSHOT_MAX_AGE - 1)},
        {"completed_at": int(NOW + 1)},
        {"started_at": int(NOW + 1), "completed_at": int(NOW + 2)},
        {"started_at": int(NOW - 30), "completed_at": int(NOW - 60)},
        {"started_at": None},
        {"completed_at": False},
        {"completed_at": "nan"},
        {"version": True},
        {"producer": "other-monitor"},
        {"release_ref": "my-ai-filter:older"},
    ],
)
def test_stale_future_misordered_or_unbound_snapshots_are_unknown(tmp_path, updates):
    result = _read(tmp_path, _payload(**updates))

    assert result["status"] == "unknown"
    assert result["checks"] == []


def test_freshness_uses_started_at_not_recent_completion(tmp_path):
    payload = _payload(
        started_at=int(NOW - ops.HOST_SNAPSHOT_MAX_AGE - 1), completed_at=int(NOW)
    )

    assert _read(tmp_path, payload)["status"] == "unknown"


def test_exact_freshness_limit_is_accepted(tmp_path):
    payload = _payload(started_at=int(NOW - ops.HOST_SNAPSHOT_MAX_AGE))

    assert _read(tmp_path, payload)["status"] == "ok"


@pytest.mark.parametrize("expected_release", ["", "development", "my-ai-filter:other"])
def test_unverifiable_running_version_cannot_accept_snapshot(
    tmp_path, expected_release
):
    assert (
        _read(tmp_path, _payload(), expected_release=expected_release)["status"]
        == "unknown"
    )


def test_missing_snapshot_remains_unknown(tmp_path):
    result = ops.read_host_ops_snapshot(tmp_path / "missing.json", RELEASE, now=NOW)

    assert result["status"] == "unknown"
    assert result["checked_at"] == "未记录"
    assert result["checks"] == []


@pytest.mark.parametrize("contents", ["{broken", "[]", "null", '"text"'])
def test_malformed_snapshot_is_unknown(tmp_path, contents):
    path = tmp_path / "host_status.json"
    path.write_text(contents, encoding="utf-8")

    assert ops.read_host_ops_snapshot(path, RELEASE, now=NOW)["status"] == "unknown"


@pytest.mark.parametrize("key", ["api_key", "detail", "hostname", "unexpected"])
def test_unknown_or_secret_top_level_fields_reject_entire_snapshot(tmp_path, key):
    result = _read(tmp_path, _payload(**{key: "secret-token-never-render"}))

    assert result["status"] == "unknown"
    assert result["checks"] == []
    assert "secret-token" not in json.dumps(result)


@pytest.mark.parametrize(
    "checks",
    [
        None,
        {},
        ["release_containers"],
        [{"name": "release_containers", "status": "UNKNOWN"}],
        [{"name": "unrecognized-secret-name", "status": "PASS"}],
        [{"name": "release_containers", "status": "PASS", "detail": "secret"}],
        [{"name": "release_containers", "status": "PASS", "api_key": "secret"}],
        [{"name": "release_containers", "status": True}],
        [{"name": ["release_containers"], "status": "PASS"}],
        [{"name": "metrics", "status": "PASS"}] * 2,
        [{"name": "metrics", "status": "PASS"}] * 11,
    ],
)
def test_invalid_or_ambiguous_check_rows_reject_entire_snapshot(tmp_path, checks):
    result = _read(tmp_path, _payload(checks=checks))

    assert result["status"] == "unknown"
    assert result["checks"] == []
    assert "secret" not in json.dumps(result)


def test_only_release_containers_determine_service_card(tmp_path):
    payload = _change_check(_payload(), "metrics", "FAIL")
    _change_check(payload, "root_disk", "WARN")

    result = _read(tmp_path, payload)

    assert result["status"] == "ok"
    assert _check(result, "metrics")["status"] == "warn"
    assert _check(result, "metrics")["label"] == "检查失败"
    assert _check(result, "root_disk")["status"] == "warn"
    assert _check(result, "root_disk")["label"] == "检查有警告"


@pytest.mark.parametrize("status", ["WARN", "FAIL"])
def test_container_warning_or_failure_controls_service_card(tmp_path, status):
    result = _read(tmp_path, _change_check(_payload(), "release_containers", status))

    assert result["status"] == "warn"


def test_missing_container_result_is_unknown_even_when_other_checks_pass(tmp_path):
    payload = _payload(checks=[{"name": "metrics", "status": "PASS"}])

    result = _read(tmp_path, payload)

    assert result["status"] == "unknown"
    assert _check(result, "release_containers")["status"] == "unknown"
    assert _check(result, "metrics")["status"] == "ok"
    assert _check(result, "root_disk")["status"] == "unknown"


@pytest.mark.parametrize("name", ["backup", "restore_drill", "offsite_upload"])
def test_policy_skip_is_information_not_pass_or_failure(tmp_path, name):
    result = _read(tmp_path, _change_check(_payload(), name, "SKIPPED_BY_POLICY"))

    assert result["status"] == "ok"
    assert _check(result, name)["status"] == "info"
    assert _check(result, name)["label"] == "按策略未执行"


@pytest.mark.parametrize("name", ["release_containers", "metrics", "root_disk"])
def test_policy_skip_cannot_mask_unrelated_checks(tmp_path, name):
    result = _read(tmp_path, _change_check(_payload(), name, "SKIPPED_BY_POLICY"))

    assert result["status"] == "unknown"
    assert result["checks"] == []


def _build(tmp_path, monkeypatch, payload):
    _write(tmp_path / "ops" / "host_status.json", payload)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(ops, "_spool_dir", lambda: spool)
    monkeypatch.setattr(ops, "release_version", lambda: RELEASE)
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", str(tmp_path / "market"))
    return ops.build_dashboard_ops(
        {},
        [],
        dashboard_tests._freshness(succeeded=20, failed=0),
        db_path=tmp_path / "cache.db",
        now=NOW,
    )


def test_non_container_warnings_enter_attention_without_changing_service(
    tmp_path, monkeypatch
):
    payload = _change_check(_payload(), "root_disk", "WARN")
    _change_check(payload, "metrics", "FAIL")
    _change_check(payload, "backup", "SKIPPED_BY_POLICY")

    result = _build(tmp_path, monkeypatch, payload)

    assert result["service"]["status"] == "ok"
    assert len(result["attention"]) == 2
    titles = [item["title"] for item in result["attention"]]
    assert any("根分区空间" in title for title in titles)
    assert any("应用指标" in title for title in titles)
    assert not any("备份" in title for title in titles)
    assert all(item["href"] == "#host-checks" for item in result["attention"])
    assert all(item["timing"] == "持续时间未记录" for item in result["attention"])


def test_expired_warning_does_not_become_a_current_fault(tmp_path, monkeypatch):
    payload = _change_check(_payload(started_at=int(NOW - 90000)), "metrics", "FAIL")

    result = _build(tmp_path, monkeypatch, payload)

    assert result["service"]["status"] == "unknown"
    assert result["attention"] == []


def test_actual_dashboard_authentication_precedes_snapshot_read(
    dashboard_browser, monkeypatch, tmp_path
):
    client, _auth, _security = dashboard_browser
    dashboard_tests._prepare_dashboard(monkeypatch, tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("host snapshot must not be read before authentication")

    monkeypatch.setattr(ops, "read_host_ops_snapshot", forbidden)
    assert (
        client.get("/dashboard", headers={"Accept": "application/json"}).status_code
        == 401
    )
    response = client.get(
        "/dashboard",
        base_url=browser_tests.BASE_URL,
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard/login?next=/dashboard"
    assert browser_tests._login(client).status_code == 303
    assert (
        client.get(
            "/dashboard",
            base_url=browser_tests.BASE_URL,
            headers={"X-API-Key": "incorrect"},
        ).status_code
        == 401
    )


def test_authenticated_dashboard_renders_real_snapshot_and_private_headers(
    integration_client, monkeypatch, tmp_path
):
    from db import database

    dashboard_tests._prepare_dashboard(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "release_version", lambda: RELEASE)
    monkeypatch.setattr(ops.time, "time", lambda: NOW)
    payload = _change_check(_payload(), "backup", "SKIPPED_BY_POLICY")
    _change_check(payload, "root_disk", "WARN")
    _write(Path(database.DB_PATH).parent / "ops" / "host_status.json", payload)

    response = integration_client.get("/dashboard")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "发布与容器：检查通过" in body
    assert "根分区空间：检查有警告" in body
    assert "按策略未执行" in body
    assert "09-21 15:59:00 CST" in body
    assert 'href="#host-checks"' in body
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
