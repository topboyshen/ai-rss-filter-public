import ast
import json
import time
from pathlib import Path

import pytest

from scripts import activation_window_diagnostics as window
from scripts import production_activation_host_native as native
from tests.test_production_activation_host_native import _gateway_switch_dir

ROOT = Path(__file__).resolve().parents[1]
OWNER = "a" * 32
PASSWORD = "pw" + "9" * 30


class ActivationHarness:  # same class name as the CI harness; guard also checks owner/network
    def __init__(self, owner=OWNER, network=None):
        self.owner = owner
        self.network = network or "miniflux-pause-" + owner[:12] + "-network"


class Cursor:
    def __init__(self, log, fail=None):
        self.log, self.fail, self.last = log, fail, None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.log.append(sql)
        if self.fail and self.fail in sql:
            raise RuntimeError("boom password=" + PASSWORD)
        self.last = sql

    def fetchall(self):
        if "handoff_state" in self.last:
            return [("manifest-x", 0, "closed", "b" * 64)]
        if "handoff_leases" in self.last:
            return [("active", "outbox_pass", 1), ("uncertain", "token=leak7 " + "s" * 80, 2)] * 8
        if "count(*) FROM handoff_operations" in self.last:
            return [("confirmed", 3)]
        if "pg_locks" in self.last:
            return [(20261024, 2, "ShareLock", True, 1, 1), (20261024, 2, "ExclusiveLock", False, 1, 1)]
        if "pg_stat_activity" in self.last:
            return [("idle", None, None, 3), ("active", "Lock", "advisory", 1)]
        return [("initialize_closed", "confirmed", -1, 0, "c" * 64)] * 6


class Connection:
    def __init__(self, log, fail=None):
        self.log, self.fail, self.rolled_back = log, fail, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return Cursor(self.log, self.fail)

    def rollback(self):
        self.rolled_back = True


def _p(tmp_path, dsn=None):
    (tmp_path / "postgres.json").write_text(json.dumps({"dsn": dsn or f"postgres://synthetic:{PASSWORD}@172.18.0.5:5432/synthetic_pause?sslmode=disable"}))
    return {"directory": tmp_path}


def _connect(log, seen, fail=None):
    def connect(dsn, **kwargs):
        seen.append(kwargs)
        conn = Connection(log, fail)
        seen.append(conn)
        return conn
    return connect


def test_snapshot_is_read_only_bounded_and_redacted(tmp_path):
    log, seen = [], []
    value = window.pg_snapshot(ActivationHarness(), _p(tmp_path), connect=_connect(log, seen))
    assert log[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert "statement_timeout='3000ms'" in log[1] and seen[0]["connect_timeout"] == 3 and seen[0]["autocommit"] is False
    assert seen[1].rolled_back is True
    queries = [q for q in log if q.startswith("SELECT")]
    assert queries and all("LIMIT" in q for q in queries)
    assert not any(word in " ".join(log).upper() for word in ("INSERT", "UPDATE ", "DELETE", "ALTER", "LOCK TABLE", "FOR UPDATE", "PG_ADVISORY", "PG_TRY"))
    assert value["state"]["phase"] == "closed" and len(value["state"]["current_operation"]) == 12
    raw = json.dumps(value)
    assert "b" * 64 not in raw and "c" * 64 not in raw and "leak7" not in raw and PASSWORD not in raw
    assert len(raw) < 4096


@pytest.mark.parametrize("dsn", [
    "postgres://miniflux:x@172.18.0.5:5432/synthetic_pause",
    "postgres://synthetic:x@172.18.0.5:5432/miniflux",
    "postgres://synthetic:x@8.8.8.8:5432/synthetic_pause",
    "postgres://synthetic:x@127.0.0.1:5432/synthetic_pause",
    "postgres://synthetic:x@172.18.0.5:6432/synthetic_pause",
    "postgresql://synthetic:x@db.example:5432/synthetic_pause",
])
def test_refuses_non_synthetic_targets(tmp_path, dsn):
    log, seen = [], []
    value = window.pg_snapshot(ActivationHarness(), _p(tmp_path, dsn), connect=_connect(log, seen))
    assert value["error_type"] in ("AdmissionError", "ValueError") and not seen and not log


@pytest.mark.parametrize("harness", [object(), ActivationHarness(owner="z" * 32), ActivationHarness(network="prod-net")])
def test_refuses_non_ci_harness(tmp_path, harness):
    seen = []
    value = window.pg_snapshot(harness, _p(tmp_path), connect=_connect([], seen))
    assert "error_type" in value and not seen


def test_snapshot_failure_never_raises_and_is_redacted(tmp_path):
    value = window.pg_snapshot(ActivationHarness(), _p(tmp_path), connect=_connect([], [], fail="handoff_leases"))
    assert value["error_type"] == "RuntimeError" and PASSWORD not in json.dumps(value)


def test_snapshot_in_record_is_exported_bounded(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    switch = _gateway_switch_dir(root)
    snap = window.pg_snapshot(ActivationHarness(), _p(tmp_path), connect=_connect([], []))
    window.record(switch, "prepare", time.monotonic(), RuntimeError("x"),
                  postgres={"baseline_after_closed_initialization": snap, "at_failure": snap})
    assert len((switch / "watchdog-window.stderr").read_bytes()) <= window.RECORD_LIMIT
    monkeypatch.setattr(native.time, "monotonic", lambda: 100)
    rows = native.safe_inventory(root, 600)["gateway_command_stderr"]
    assert '"leases_not_finished"' in rows[0]["text"] and PASSWORD not in rows[0]["text"]
    assert json.loads(rows[0]["text"])["postgres"]["at_failure"]["state"]["phase"] == "closed"


def test_only_ci_native_verifier_invokes_snapshot():
    callers = set()
    for path in list((ROOT / "scripts").glob("*.py")) + list((ROOT / "services").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("pg_snapshot", "synthetic_dsn"):
                callers.add(path.name)
    assert callers == {"verify_production_activation_native.py"}


def test_snapshot_reports_advisory_holders_and_wait_events(tmp_path):
    log = []
    value = window.pg_snapshot(ActivationHarness(), _p(tmp_path), connect=_connect(log, []))
    lock_sql = next(q for q in log if "pg_locks" in q)
    assert "20261023::oid" in lock_sql and "20261024::oid" in lock_sql and "locktype='advisory'" in lock_sql
    assert value["advisory_locks"][0] == {"classid": 20261024, "objsubid": 2, "mode": "ShareLock", "granted": True,
                                          "locks": 1, "pids": 1}
    assert value["activity"][1] == {"state": "active", "wait": "Lock:advisory", "count": 1}
    assert all("query" not in q.split("FROM")[0] and "usename" not in q for q in log if "pg_stat_activity" in q)


def test_snapshot_contention_queries_on_real_postgres_shape():
    names = [name for name, _ in window.PG_QUERIES]
    assert names[-2:] == ["advisory_locks", "activity"]
    assert all(sql.lstrip().startswith("SELECT") and "LIMIT 16" in sql for _, sql in window.PG_QUERIES[-2:])
