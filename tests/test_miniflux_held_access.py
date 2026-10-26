"""Offline guards for the real PostgreSQL verifier; not permission proof."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "ops/miniflux/held-access/verify.py"
SPEC = importlib.util.spec_from_file_location("held_access_verify", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def names():
    return {"OWNER": "owner_test", "REVIEWER": "reviewer_test", "EXECUTOR": "executor_test",
            "AUDITOR": "auditor_test", "SCHEMA": "held_access_isolation_test", "SOURCE": "held_source_isolation_test"}


@pytest.mark.parametrize("bad", ["x;DROP ROLE postgres", 'a"b', "a-b", "x" * 64, "1a", "a b"])
def test_identifier_render_rejects_sql_injection(bad):
    values = names()
    values["OWNER"] = bad
    with pytest.raises(ValueError):
        module.render("__OWNER__", values)


def test_render_requires_isolated_schema_and_complete_binding():
    values = names()
    values["SCHEMA"] = "production"
    with pytest.raises(ValueError):
        module.render("__SCHEMA__", values)
    with pytest.raises(ValueError):
        module.render("__OTHER__", names())
    assert module.literal("a'b") == "'a''b'"
    assert module.render("__OWNER__.__SCHEMA__", names()) == "owner_test.held_access_isolation_test"


def test_dry_run_never_executes_docker(monkeypatch, capsys):
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("dry run attempted Docker"))
    monkeypatch.setattr("sys.argv", ["verify", "--container", "synthetic", "--source", "/unused", "--output", "/unused"])
    module.main()
    assert '"run": false' in capsys.readouterr().out


def test_timeout_retains_true_unknown_status_and_partial(tmp_path, monkeypatch):
    output = tmp_path / "evidence"
    output.mkdir()
    runner = module.Runner("owned-synthetic", tmp_path, output)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 3, output=b"partial synthetic", stderr=b"bounded timeout")

    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        runner.command(["not-executed"], "synthetic SQL", timeout=3)
    record = module.json.loads((output / "COMMAND-001.json").read_text())
    assert record["exit_code"] is None
    assert record["timed_out"] is True
    assert record["stdout"] == "partial synthetic"
    assert record["stderr"] == "bounded timeout"
    assert record["stdin_sha256"] == module.sha(b"synthetic SQL")


def test_real_permission_checks_cannot_be_replaced_by_text_only():
    source = PATH.read_text()
    assert '"psql", "-X", "-qAt"' in source
    assert "SET SESSION AUTHORIZATION" not in source
    assert 'login = self.logins[actor] if actor else "postgres"' in source
    assert "ThreadPoolExecutor(max_workers=6)" in source
    assert "values.count(True) != 1" in source
    assert "actual fixed sender migration SHA mismatch" in source
    assert "forwarding" in source
    assert "production_adapter" in source
    assert "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" in source
    assert "observer.PG_OUTBOX_SAMPLE_SQL" in source
    assert "observer._outbox_counts(rows)" in source
    assert "outbox_only_0001_not_a_drain_proof" in source


def test_policy_has_no_sender_mutation_capability():
    sql = (PATH.parent / "install.sql").read_text()
    assert "GRANT SELECT ON __SOURCE__.webhook_outbox TO __OWNER__" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "SET search_path=pg_catalog,__SCHEMA__,pg_temp" in sql
    assert "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA __SCHEMA__ FROM PUBLIC" in sql
    assert "session_user" in sql
    assert "pg_try_advisory_xact_lock" in sql
    assert "count(*)*8192+8192>16777216" in sql
    assert "IS DISTINCT FROM plan_hash" in sql
