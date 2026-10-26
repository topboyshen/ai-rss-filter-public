"""Offline harness guards, not a substitute for its actual PostgreSQL run."""
import importlib.util
import copy
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "ops/miniflux/held-access/verify_legacy.py"
SPEC = importlib.util.spec_from_file_location("legacy_access_verify", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_default_is_dry_run_and_no_docker(monkeypatch, capsys):
    monkeypatch.setattr(module.base.subprocess, "run", lambda *a, **k: pytest.fail("dry run invoked Docker"))
    monkeypatch.setattr("sys.argv", ["verify", "--source", "/unused", "--container", "synthetic", "--output", "/unused"])
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert result == {"run": False, "production": False, "http": 0, "models": 0, "forwarding_allowed": False}


def test_wrong_frozen_migration_rejects_before_sql(tmp_path, monkeypatch):
    runner = module.Runner("synthetic-owned", tmp_path, tmp_path / "output")
    (tmp_path / "internal/database").mkdir(parents=True)
    (tmp_path / "internal/database/webhook_outbox.go").write_bytes(b"wrong source")
    monkeypatch.setattr(runner, "sql", lambda *a, **k: pytest.fail("unbound migration executed"))
    with pytest.raises(ValueError, match="fixed migration"):
        runner.create_source(runner.names["SOURCE"])
    with pytest.raises(ValueError, match="owned isolated"):
        runner.create_source("production")


def test_new_evidence_is_private_and_never_overwritten(tmp_path):
    runner = module.Runner("synthetic-owned", tmp_path, tmp_path)
    runner.save("CHECK.json", {"original": True})
    assert (tmp_path / "CHECK.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        runner.save("CHECK.json", {"original": False})
    assert json.loads((tmp_path / "CHECK.json").read_text()) == {"original": True}


def test_real_pg_commands_keep_partial_timeout_evidence(tmp_path, monkeypatch):
    runner = module.Runner("synthetic-owned", tmp_path, tmp_path)
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 3, output=b"synthetic partial", stderr=b"SQL timeout")
    monkeypatch.setattr(module.base.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="timed out"):
        runner.sql("SELECT synthetic")
    record = json.loads((tmp_path / "COMMAND-001.json").read_text())
    assert record["timed_out"] and record["exit_code"] is None
    assert record["stdout"] == "synthetic partial"


def test_installer_has_no_legacy_execution_or_mutation_path():
    sql = (PATH.parent / "legacy_audit.sql").read_text()
    for forbidden in ("GRANT UPDATE", "GRANT DELETE", "GRANT INSERT", "GRANT TRUNCATE", "execution_claims", "retry_once"):
        assert forbidden not in sql
    assert "session_user" in sql
    assert "current_database()<>'synthetic_outbox'" in sql
    assert "SET search_path=pg_catalog,__SCHEMA__,pg_temp" in sql
    assert "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA __SCHEMA__ FROM PUBLIC" in sql
    assert "'forwarding_allowed',false" in sql and "'automatic_compensation',false" in sql
    assert "n>5000 OR bytes>268435456" in sql


def test_actual_verifier_reads_both_locked_migrations_and_negative_boundaries():
    source = PATH.read_text()
    assert '"legacy_handoff.go", LEGACY_MIGRATION_SHA' in source
    assert "observer.PG_IDENTITY_SQL" in source and "observer.PG_SAMPLE_SQL" in source
    assert "inventory.sender_blockers(counts)" in source
    assert 'ThreadPoolExecutor(max_workers=6)' in source
    assert 'self.boundary_cases("_rows", 4999, 32)' in source
    assert 'self.boundary_cases("_bytes", 254, 1048576)' in source
    assert "db_api_transport_tested" in source
    assert "large_byte_fixture_raw_not_exported" in source
    assert "audit['SCHEMA']}.shared_inventory()" in source
    assert '"shared hard quota exceeded" not in command["stderr"]' in source


def valid_cgroup():
    return {"memory.events": {"available": True, "raw": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"},
            "memory.peak": {"available": True, "raw": "120791040\n"}}


def test_cgroup_requires_actual_available_complete_counters():
    result = module.require_cgroup(valid_cgroup())
    assert result["memory_peak_bytes"] == 120791040
    assert result["memory_events"]["oom"] == result["memory_events"]["oom_kill"] == 0


@pytest.mark.parametrize("metric,field,value", [
    ("memory.events", "available", False), ("memory.events", "available", 1),
    ("memory.peak", "available", False), ("memory.events", "raw", None),
    ("memory.events", "raw", ""), ("memory.events", "raw", "oom 0\n"),
    ("memory.events", "raw", "oom -1\noom_kill 0"),
    ("memory.events", "raw", "oom true\noom_kill 0"),
    ("memory.events", "raw", "oom 0\noom 0\noom_kill 0"),
    ("memory.events", "raw", "oom 1\noom_kill 0"),
    ("memory.peak", "raw", ""), ("memory.peak", "raw", True),
    ("memory.peak", "raw", "-1"), ("memory.peak", "raw", "1.2"),
])
def test_missing_invalid_or_oom_cgroup_never_passes(metric, field, value):
    records = copy.deepcopy(valid_cgroup())
    records[metric][field] = value
    with pytest.raises(AssertionError):
        module.require_cgroup(records)


def test_missing_cgroup_measurement_never_passes():
    with pytest.raises(AssertionError, match="unavailable"):
        module.require_cgroup({})


@pytest.mark.parametrize("record", [
    {"exit_code": 2, "timed_out": False, "stderr": "FATAL: no pg_hba.conf entry"},
    {"exit_code": 3, "timed_out": False, "stderr": "ERROR:  relation does not exist\n"},
    {"exit_code": 1, "timed_out": False, "stderr": "ERROR:  permission denied for schema fixture\n"},
    {"exit_code": 3, "timed_out": True, "stderr": "ERROR:  permission denied for schema fixture\n"},
    {"exit_code": 3, "timed_out": False, "stderr": "ERROR:  permission denied for schema fixture\nERROR:  another error\n"},
])
def test_sql_rejection_cannot_be_connection_hba_timeout_or_unrelated_failure(record):
    with pytest.raises(AssertionError, match="exact expected"):
        module.require_sql_rejection(record, "permission denied for schema fixture")


def test_sql_rejection_requires_exact_server_reason():
    module.require_sql_rejection({"exit_code": 3, "timed_out": False,
                                  "stderr": "ERROR:  permission denied for schema fixture\nLINE 1: SELECT fixture.t\n"},
                                 "permission denied for schema fixture")


def test_login_must_connect_and_match_both_exact_identities(tmp_path, monkeypatch):
    runner = module.Runner("synthetic-owned", tmp_path, tmp_path)
    login = runner.logins["execute"]
    monkeypatch.setattr(runner, "sql", lambda *a: json.dumps({"session_user": login, "current_user": login}))
    assert runner.check_login("execute")["session_user"] == login
    monkeypatch.setattr(runner, "sql", lambda *a: json.dumps({"session_user": "postgres", "current_user": login}))
    with pytest.raises(AssertionError, match="login identity"):
        runner.check_login("execute")
    def refused(*args):
        raise AssertionError("unexpected real PostgreSQL permission/result")
    monkeypatch.setattr(runner, "sql", refused)
    with pytest.raises(AssertionError, match="PostgreSQL"):
        runner.check_login("none")


def container_identity():
    return {"id": "owned-id", "image": "sha256:locked", "configured_image": "locked@sha256:manifest",
            "memory": 402653184, "memory_swap": 402653184, "pids": 64, "cpu": 1000000000,
            "mounts": [{"Name": "owned-volume"}], "state": {"StartedAt": "2026-10-23T04:50:35Z"}}


def test_same_container_identity_positive():
    module.require_same_container(container_identity(), container_identity())


@pytest.mark.parametrize("key,value", [
    ("id", "replacement"), ("image", "sha256:other"), ("configured_image", "other"),
    ("memory", 1), ("memory_swap", 1), ("pids", 1), ("cpu", 1), ("mounts", []),
    ("state", {"StartedAt": "2026-10-23T04:51:35Z"}), ("state", {}),
])
def test_restarted_replaced_or_reconfigured_container_never_passes(key, value):
    changed = copy.deepcopy(container_identity())
    changed[key] = value
    with pytest.raises(AssertionError):
        module.require_same_container(container_identity(), changed)


def test_equal_but_missing_start_identity_does_not_pass():
    missing = container_identity()
    missing["state"] = {}
    with pytest.raises(AssertionError, match="start identity"):
        module.require_same_container(missing, missing)
