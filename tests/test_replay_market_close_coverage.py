import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from scripts import replay_market_close_coverage as replay
from db import database
from db.event_core import upsert_security
from services import event_outcomes


def test_replay_date_rejects_same_day_leak(monkeypatch):
    def requirements(trade_date, *, start_date=None, limit=32):
        pairs = [
            {"symbol": "sh600405", "trade_date": "2026-09-10"},
            {"symbol": "sh600406", "trade_date": "2026-09-11"},
        ]
        selected = pairs if start_date is None else [p for p in pairs if p["trade_date"] >= start_date]
        return {
            "pairs": selected[:limit],
            "total": len(selected),
            "truncated": len(selected) > limit,
        }

    monkeypatch.setattr(replay, "get_pending_quote_requirements", requirements)
    row = replay.replay_date("2026-09-11")
    assert row["errors"] == []
    assert row["same_total"] == 1
    assert row["all_total"] == 2


def test_replay_date_flags_cap_mismatch(monkeypatch):
    def requirements(trade_date, *, start_date=None, limit=32):
        pairs = [{"symbol": f"sh{600000 + i}", "trade_date": trade_date} for i in range(40)]
        return {"pairs": pairs[:10], "total": 40, "truncated": True}

    monkeypatch.setattr(replay, "get_pending_quote_requirements", requirements)
    row = replay.replay_date("2026-09-11")
    assert "truncated same-day did not return the pair cap" in row["errors"]


def test_readonly_uri_requires_file(tmp_path):
    with pytest.raises(SystemExit):
        replay._readonly_uri(tmp_path / "missing.db")
    db = tmp_path / "cache.db"
    db.write_bytes(b"")
    assert replay._readonly_uri(db).startswith("file:")
    assert "mode=ro" in replay._readonly_uri(db)


def _seed_replay_signal(monkeypatch):
    database.init_db()
    available_at = int(datetime.fromisoformat("2026-09-11T10:00:00+08:00").timestamp())
    with database.get_connection() as conn:
        upsert_security(
            conn, exchange="SSE", ticker="688256", name="寒武纪",
            security_id="SSE:688256", asset_type="equity", now=available_at - 60,
        )
        conn.execute("UPDATE app_meta SET v='1' WHERE k='mapping_validation_started_at'")
        conn.commit()
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    assert database.store_backtest_signal({
        "link": "https://example.com/replay-snapshot", "event_key": "replay-snapshot",
        "available_at": available_at, "scored_at": available_at,
        "published_at": available_at - 60, "score": 9.0, "direction": "看多",
        "score_confidence": 0.8, "strategy_version": "strategy-v1",
        "mapping_version": "asset-map:v1", "mapped_theme": "AI算力",
        "backtest_symbol": "sz399006", "score_path": "stage2",
        "aggregation_threshold": 7.0, "selected": True,
        "security_candidates": [{
            "security_id": "SSE:688256", "relation_type": "direct", "direction": "看多",
            "confidence": 0.96, "relevance": 10.0, "rationale": "原文直接点名寒武纪",
        }],
    })


def test_main_reads_selected_snapshot_not_default_database(tmp_path, monkeypatch, capsys):
    _seed_replay_signal(monkeypatch)
    selected = tmp_path / "locked?copy#.db"
    with database.get_read_only_connection() as source, sqlite3.connect(selected) as copy:
        source.backup(copy)
    close_at = int(datetime.fromisoformat("2026-09-11T15:00:00+08:00").timestamp())
    database.store_future_prices([
        {"symbol": symbol, "name": symbol, "trade_date": "2026-09-11",
         "close_price": 100.0, "change_amt": 0.0, "change_pct": 0.0, "ts": close_at}
        for symbol in ("sh688256", "sh000300")
    ])
    assert event_outcomes.get_pending_quote_requirements("2026-09-11")["total"] == 0
    default_path = database.DB_PATH
    before = selected.read_bytes()
    reader = event_outcomes._read_frozen_mapping_snapshot
    reads = []

    def require_selected_read_only(conn, **kwargs):
        reads.append(conn.execute("PRAGMA database_list").fetchone()[2])
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            conn.execute("CREATE TABLE replay_must_not_write (value TEXT)")
        return reader(conn, **kwargs)

    monkeypatch.setattr(event_outcomes, "_read_frozen_mapping_snapshot", require_selected_read_only)
    assert replay.main(["--db", str(selected), "--dates", "2026-09-11"]) == 0
    assert "OK 2026-09-11 all=2 trunc=False same=2" in capsys.readouterr().out
    assert len(reads) == 2
    assert all(Path(path).resolve() == selected.resolve() for path in reads)
    assert database.DB_PATH == default_path
    assert selected.read_bytes() == before
    assert not (tmp_path / "locked").exists()
