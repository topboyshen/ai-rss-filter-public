import sqlite3

import pytest

from db.event_core import init_event_core_schema
from workers import security_master_collector as collector


def _database(tmp_path):
    path = tmp_path / "cache.db"
    conn = sqlite3.connect(path)
    init_event_core_schema(conn)
    conn.close()
    return path


def test_worker_defaults_to_a_query_only_dry_run(tmp_path, monkeypatch):
    path = _database(tmp_path)
    observed = {}

    def fake_sync(conn, *, dry_run):
        observed["dry_run"] = dry_run
        observed["query_only"] = int(
            conn.execute("PRAGMA query_only").fetchone()[0]
        )
        return {"inserted": 12}

    monkeypatch.setattr(collector, "run_security_master_sync", fake_sync)
    assert collector.run(str(path)) == {"inserted": 12}
    assert observed == {"dry_run": True, "query_only": 1}


def test_worker_apply_is_explicit_and_uses_a_writable_connection(
    tmp_path, monkeypatch
):
    path = _database(tmp_path)
    observed = {}

    def fake_sync(conn, *, dry_run):
        observed["dry_run"] = dry_run
        observed["query_only"] = int(
            conn.execute("PRAGMA query_only").fetchone()[0]
        )
        return {"inserted": 12}

    monkeypatch.setattr(collector, "run_security_master_sync", fake_sync)
    assert collector.run(str(path), apply=True) == {"inserted": 12}
    assert observed == {"dry_run": False, "query_only": 0}


def test_worker_rejects_incomplete_or_symlink_database(tmp_path, monkeypatch):
    incomplete = tmp_path / "incomplete.db"
    sqlite3.connect(incomplete).close()
    monkeypatch.setattr(
        collector,
        "run_security_master_sync",
        lambda *_args, **_kwargs: pytest.fail("invalid database reached sync"),
    )
    with pytest.raises(RuntimeError, match="schema"):
        collector.run(str(incomplete))

    real = _database(tmp_path)
    link = tmp_path / "linked.db"
    link.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        collector.run(str(link))


def test_worker_requires_the_security_natural_key_unique_constraint(
    tmp_path, monkeypatch
):
    path = tmp_path / "unsafe.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE securities ("
        "security_id TEXT, exchange TEXT, ticker TEXT, name TEXT, "
        "asset_type TEXT, currency TEXT, aliases TEXT, industry TEXT, "
        "concepts TEXT, is_active INTEGER, version INTEGER, "
        "created_at INTEGER, updated_at INTEGER)"
    )
    conn.execute("CREATE TABLE event_securities (mapping_id TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        collector,
        "run_security_master_sync",
        lambda *_args, **_kwargs: pytest.fail("unsafe schema reached sync"),
    )

    with pytest.raises(RuntimeError, match=r"UNIQUE\(exchange,ticker\)"):
        collector.run(str(path))
