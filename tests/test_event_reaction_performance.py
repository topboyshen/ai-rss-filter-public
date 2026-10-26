"""Reaction reads must not queue behind writers or scan unrelated signals."""

import sqlite3
import threading
import uuid

from db import database
from services import event_radar_query as radar


def test_radar_reads_without_waiting_for_the_process_writer_lock(monkeypatch):
    """A held application lock must not stop an existing SQLite WAL reader."""
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE reader_probe (value INTEGER)")
        conn.execute("INSERT INTO reader_probe VALUES (1)")

    opened = {}
    connect = sqlite3.connect

    def recording_connect(path, *args, **kwargs):
        conn = connect(path, *args, **kwargs)
        opened[id(conn)] = (str(path), kwargs.get("uri", False))
        return conn

    def page(conn, *args):
        path, uri = opened[id(conn)]
        assert uri and path.endswith("?mode=ro")
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM reader_probe").fetchone()[0] == 1
        return [], None, False

    monkeypatch.setattr(database.sqlite3, "connect", recording_connect)
    monkeypatch.setattr(radar, "_load_event_page", page)
    writer_lock = threading.Lock()
    monkeypatch.setattr(radar, "db_lock", writer_lock)
    completed = threading.Event()
    errors = []
    results = []

    def read():
        try:
            results.append(radar.load_event_radar(tab="reaction"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    reader = threading.Thread(target=read, daemon=True)
    with writer_lock:
        reader.start()
        completed_while_locked = completed.wait(timeout=3)
    reader.join(timeout=3)
    assert not reader.is_alive()
    assert completed_while_locked, "read-only Radar waited for the process writer lock"
    assert errors == []
    assert results[0]["events"] == []


def test_radar_list_and_detail_keep_one_snapshot_across_a_writer_commit(monkeypatch):
    """A committed concurrent revision appears only in the next request."""
    event_id = str(uuid.uuid4())
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE snapshot_probe (revision INTEGER)")
        conn.execute("INSERT INTO snapshot_probe VALUES (1)")
    errors = []
    committed = threading.Event()

    def write():
        try:
            with sqlite3.connect(database.DB_PATH, timeout=2) as conn:
                conn.execute("UPDATE snapshot_probe SET revision = 2")
            committed.set()
        except BaseException as exc:
            errors.append(exc)

    def page(conn, *args):
        revision = conn.execute("SELECT revision FROM snapshot_probe").fetchone()[0]
        if not committed.is_set():
            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            writer.join(timeout=3)
            assert not writer.is_alive(), (
                "WAL writer could not commit beside the reader"
            )
            assert errors == []
            assert committed.is_set()
        return [{"event_id": event_id, "revision": revision}], None, False

    def detail(conn, requested_event_id, tab, read_at):
        revision = conn.execute("SELECT revision FROM snapshot_probe").fetchone()[0]
        return requested_event_id, {"revision": revision}

    monkeypatch.setattr(radar, "_load_event_page", page)
    monkeypatch.setattr(radar, "_load_detail", detail)
    first = radar.load_event_radar(tab="reaction")
    second = radar.load_event_radar(tab="reaction")
    assert first["events"][0]["revision"] == first["detail"]["revision"] == 1
    assert second["events"][0]["revision"] == second["detail"]["revision"] == 2


def _measure_work(conn, read):
    blocks = 0

    def progress():
        nonlocal blocks
        blocks += 1
        return int(blocks >= 100_000)

    conn.set_progress_handler(progress, 100)
    try:
        result = read()
    finally:
        conn.set_progress_handler(None, 0)
    return result, blocks * 100


class _QueryPlanConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if "CROSS JOIN backtest_signal_securities AS frozen " in sql:
            self.generated_query = (sql, parameters)
        return super().execute(sql, parameters)

    def generated_plan(self):
        sql, parameters = self.generated_query
        return [
            row[3]
            for row in super().execute("EXPLAIN QUERY PLAN " + sql, parameters)
        ]


def test_generated_reaction_work_does_not_grow_with_unrelated_frozen_signals(
    monkeypatch,
):
    """The selected event drives both the ledger and frozen-mapping reads."""
    conn = sqlite3.connect(":memory:", factory=_QueryPlanConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, merged_into TEXT, "
            "available_at INTEGER);"
            "CREATE INDEX idx_events_merge_target ON events(merged_into) "
            "WHERE merged_into IS NOT NULL;"
            "CREATE TABLE backtest_signals (id INTEGER PRIMARY KEY, event_id TEXT, "
            "link TEXT, strategy_version TEXT, mapping_version TEXT, available_at INTEGER, "
            "recorded_at INTEGER, selected INTEGER, is_similarity_copy INTEGER, "
            "event_version INTEGER, score_path TEXT);"
            "CREATE INDEX idx_backtest_signals_event_available "
            "ON backtest_signals(event_id, available_at DESC);"
            "CREATE INDEX idx_backtest_signals_available ON backtest_signals(available_at);"
            "CREATE TABLE backtest_signal_securities (ledger_signal_id INTEGER, "
            "mapping_id TEXT, event_id TEXT, security_id TEXT, mapping_revision INTEGER, "
            "signal_available_at INTEGER, captured_at INTEGER, "
            "PRIMARY KEY(ledger_signal_id, mapping_id));"
            "CREATE INDEX idx_backtest_signal_securities_mapping "
            "ON backtest_signal_securities(mapping_id, mapping_revision);"
            "CREATE INDEX idx_backtest_signal_securities_event "
            "ON backtest_signal_securities(event_id, security_id, ledger_signal_id);"
            "CREATE TABLE event_securities (mapping_id TEXT PRIMARY KEY, event_id TEXT, "
            "security_id TEXT, revision INTEGER, relation_type TEXT, direction TEXT, "
            "rationale TEXT, mapping_version TEXT, created_at INTEGER, is_current INTEGER);"
            "CREATE UNIQUE INDEX uq_event_securities_current "
            "ON event_securities(event_id, security_id, relation_type) WHERE is_current = 1;"
            "CREATE TABLE securities (security_id TEXT PRIMARY KEY, exchange TEXT, "
            "ticker TEXT, name TEXT, asset_type TEXT);"
            "CREATE TABLE future_price_revisions (revision_id TEXT PRIMARY KEY, "
            "symbol TEXT, trade_date TEXT, recorded_at INTEGER, open_price REAL, close_price REAL, source_ts INTEGER, "
            "suspension_state TEXT, limit_state TEXT, tradability_state TEXT);"
            "CREATE INDEX idx_future_price_revisions_symbol_date "
            "ON future_price_revisions(symbol, trade_date, recorded_at DESC);"
        )
        conn.executemany(
            "INSERT INTO events VALUES (?, NULL, 1)", [("target",), ("other",)]
        )
        conn.execute(
            "INSERT INTO securities VALUES ('SSE:600000', 'SSE', '600000', "
            "'Test equity', 'equity')"
        )
        conn.execute(
            "INSERT INTO event_securities VALUES (?, ?, 'SSE:600000', 1, "
            "'direct', 'bullish', '', 'mapping-v1', 1, 1)",
            ("target-mapping", "target"),
        )
        conn.execute(
            "INSERT INTO backtest_signals VALUES (1, 'target', 'target-link', "
            "'strategy-v1', 'mapping-v1', 100, 100, 1, 0, 1, 'stage2')"
        )
        conn.execute(
            "INSERT INTO backtest_signal_securities VALUES "
            "(1, 'target-mapping', 'target', 'SSE:600000', 1, 100, 100)"
        )
        monkeypatch.setattr(radar, "baseline_trade_date", lambda _: "2026-01-05")
        monkeypatch.setattr(
            radar, "horizon_trade_date", lambda _, offset: f"2026-01-{5 + offset:02d}"
        )
        monkeypatch.setattr(radar, "_market_close_timestamp", lambda _: 200)
        monkeypatch.setattr(
            radar, "tradable_baseline_trade_date", lambda _: "2026-01-05"
        )

        def read():
            return radar._load_generated_observation_states(
                conn, "target", read_at=1_000, excluded_series=set()
            )

        initial, initial_steps = _measure_work(conn, read)
        conn.executemany(
            "INSERT INTO securities VALUES (?, 'SSE', ?, 'Other equity', 'equity')",
            [(f"SSE:{number:06d}", f"{number:06d}") for number in range(2, 20_002)],
        )
        conn.executemany(
            "INSERT INTO event_securities VALUES (?, 'other', ?, 1, "
            "'direct', 'bullish', '', 'mapping-v1', 1, 1)",
            [
                (f"other-mapping-{number}", f"SSE:{number:06d}")
                for number in range(2, 20_002)
            ],
        )
        conn.executemany(
            "INSERT INTO backtest_signals VALUES (?, 'other', ?, "
            "'strategy-v1', 'mapping-v1', 100, 100, 1, 0, 1, 'stage2')",
            [(number, f"other-{number}") for number in range(2, 20_002)],
        )
        conn.executemany(
            "INSERT INTO backtest_signal_securities VALUES "
            "(?, ?, 'other', ?, 1, 100, 100)",
            [
                (number, f"other-mapping-{number}", f"SSE:{number:06d}")
                for number in range(2, 20_002)
            ],
        )
        grown, grown_steps = _measure_work(conn, read)

        assert grown == initial
        assert len(grown) == len(radar.OUTCOME_HORIZON_DAYS)
        assert {item["signal_ledger_id"] for item in grown} == {1}
        assert {item["data_status"] for item in grown} == {"missing_asset_close"}
        plan = conn.generated_plan()
        diagnostic = (sqlite3.sqlite_version, initial_steps, grown_steps, plan)
        assert any(
            "SEARCH frozen USING INDEX sqlite_autoindex_backtest_signal_securities_1"
            in step for step in plan
        ), diagnostic
        assert not any("frozen" in step and "AUTOMATIC" in step for step in plan), diagnostic
        assert grown_steps <= initial_steps + 1_000, diagnostic
    finally:
        conn.close()
