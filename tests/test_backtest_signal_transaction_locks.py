"""Lock the current backtest-signal and future-price transaction contracts.

These tests do not change production behavior. They pin the savepoint
rollback, the schema-commit-then-BEGIN-IMMEDIATE order, the one-commit
price batch, and the current input/output of the signal write implementation.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from db import database


class _LedgerStop(BaseException):
    """BaseException used to prove the savepoint handler is not Exception-only."""


def _signal(**overrides) -> dict:
    payload = {
        "event_key": "https://example.com/lock",
        "independence_key": "https://example.com/lock",
        "link": "https://example.com/lock",
        "feed_url": "feed-a",
        "available_at": 1_700_000_100,
        "scored_at": 1_700_000_100,
        "published_at": 1_700_000_040,
        "score": 9.1,
        "direction": "看多",
        "score_confidence": 0.7,
        "strategy_version": "strategy-v1",
        "mapping_version": "asset-map:test",
        "mapped_theme": "AI算力",
        "backtest_symbol": "sz399006",
        "score_path": "stage2",
        "revision_key": "base",
        "signal_stage": "base",
        "aggregation_threshold": 8.0,
        "selected": True,
        "is_similarity_copy": False,
        "expected_gap": 1.5,
    }
    payload.update(overrides)
    return payload


def _price(symbol="sz399006", trade_date="2026-07-20", close=100, ts=100):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "name": "创业板指",
        "close_price": close,
        "change_amt": 1,
        "change_pct": 1,
        "ts": ts,
    }


def _signal_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM backtest_signals").fetchone()[0]


class _TraceCursor:
    def __init__(self, cursor, note):
        self._cursor = cursor
        self._note = note

    def execute(self, sql, *args, **kwargs):
        self._note(sql)
        self._cursor.execute(sql, *args, **kwargs)
        return self

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _TraceConnection:
    def __init__(self, conn, note):
        self._conn = conn
        self._note = note

    def execute(self, sql, *args, **kwargs):
        self._note(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        self._note("COMMIT")
        return self._conn.commit()

    def cursor(self):
        return _TraceCursor(self._conn.cursor(), self._note)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _watch(monkeypatch, bucket: list[str], *, cursor: bool = False) -> None:
    original = database.get_connection

    def note(sql) -> None:
        if not isinstance(sql, str):
            return
        compact = " ".join(sql.split())
        head = compact.upper()
        if compact == "COMMIT" or head.startswith(
            ("BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK")
        ):
            bucket.append(compact)
        elif cursor and "future_price" in compact:
            bucket.append(compact)

    @contextmanager
    def tracing(**kwargs):
        with original(**kwargs) as conn:
            yield _TraceConnection(conn, note)

    monkeypatch.setattr(database, "get_connection", tracing)


def test_impl_rejects_non_mapping_without_writing():
    database.init_db()
    with database.get_connection() as conn:
        with pytest.raises(TypeError, match="mapping"):
            database._insert_backtest_signal_impl(conn, ["nope"])
        assert _signal_count(conn) == 0


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"link": "  "}, "link is required"),
        ({"score": float("nan")}, "finite"),
        ({"aggregation_threshold": 10.1}, "0..10"),
        ({"aggregation_threshold": -0.1}, "0..10"),
        ({"score_path": "not-a-path"}, "unsupported"),
        ({"scored_at": -1}, "timestamps"),
        ({"revision_seq": -1}, "non-negative"),
        ({"revision_seq": "nope"}, "integer"),
        ({"expected_gap": 100.1}, "-100..100"),
        ({"require_insert": True}, "auto revision"),
        ({"signal_stage": "quality_repair"}, "quality repair"),
        ({"summary": "bad\ufffd"}, "replacement"),
    ],
)
def test_impl_rejects_invalid_payload_without_writing(overrides, match):
    database.init_db()
    with database.get_connection() as conn:
        with pytest.raises(ValueError, match=match):
            database._insert_backtest_signal_impl(conn, _signal(**overrides))
        assert _signal_count(conn) == 0


def test_impl_persists_caller_fields_and_normalizes_direction(monkeypatch):
    database.init_db()
    monkeypatch.setattr(database.time, "time", lambda: 1_700_000_000)
    signal = _signal(direction="暴涨")
    with database.get_connection() as conn:
        assert database._insert_backtest_signal_impl(conn, signal) is True
        conn.commit()
        row = conn.execute("SELECT * FROM backtest_signals").fetchone()
        assert _signal_count(conn) == 1
        assert row["link"] == signal["link"]
        assert row["direction"] == "中性"
        assert row["score"] == signal["score"]
        assert row["expected_gap"] == signal["expected_gap"]
        assert row["aggregation_threshold"] == signal["aggregation_threshold"]
        assert row["score_path"] == "stage2"
        assert row["revision_key"] == "base"
        assert row["signal_stage"] == "base"
        assert row["selected"] == 1
        assert row["strategy_version"] == signal["strategy_version"]
        assert row["mapped_theme"] == signal["mapped_theme"]
        assert row["backtest_symbol"] == signal["backtest_symbol"]
        assert row["available_at"] == signal["available_at"]
        assert row["scored_at"] == signal["scored_at"]
        assert row["published_at"] == signal["published_at"]
        assert row["event_id"]
        changed = _signal(direction="暴涨", expected_gap=-3.0)
        assert database._insert_backtest_signal_impl(conn, changed) is False
        assert _signal_count(conn) == 1
        assert conn.execute(
            "SELECT expected_gap FROM backtest_signals"
        ).fetchone()[0] == 1.5


def test_duplicate_replay_does_not_flip_expected_gap():
    database.init_db()
    original = _signal()
    assert database.store_backtest_signal(original) is True
    with database.get_connection() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM event_securities WHERE security_id = 'sz399006'"
        ).fetchone()[0]
        current = conn.execute(
            "SELECT expected_gap FROM event_securities "
            "WHERE security_id = 'sz399006' AND is_current = 1"
        ).fetchone()[0]
    assert current == 1.5
    assert database.store_backtest_signal({**original, "expected_gap": -9.0}) is False
    with database.get_connection() as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM event_securities WHERE security_id = 'sz399006'"
        ).fetchone()[0]
        gap = conn.execute(
            "SELECT expected_gap FROM event_securities "
            "WHERE security_id = 'sz399006' AND is_current = 1"
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT expected_gap FROM backtest_signals"
        ).fetchone()[0]
    assert after == before
    assert gap == 1.5
    assert ledger == 1.5


def test_outbox_miss_rolls_back_savepoint_and_keeps_outer_write(monkeypatch):
    database.init_db()
    monkeypatch.setattr(database, "enqueue_signal_revision", lambda *args, **kwargs: False)
    events: list[str] = []
    _watch(monkeypatch, events)
    with database.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO app_meta (k, v) VALUES ('txn-lock-marker', 'kept')"
        )
        with pytest.raises(RuntimeError, match="Outbox row was not inserted"):
            database._insert_backtest_signal(conn, _signal())
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = 'txn-lock-marker'"
        ).fetchone()[0] == "kept"
        assert _signal_count(conn) == 0
        conn.commit()
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = 'txn-lock-marker'"
        ).fetchone()[0] == "kept"
        assert _signal_count(conn) == 0
    rollback = next(
        index for index, item in enumerate(events)
        if item.startswith("ROLLBACK TO SAVEPOINT backtest_signal_")
    )
    release = next(
        index for index, item in enumerate(events)
        if item.startswith("RELEASE SAVEPOINT backtest_signal_")
    )
    assert release > rollback


def test_baseexception_inside_impl_rolls_back_and_propagates(monkeypatch):
    database.init_db()

    def stop(*_args, **_kwargs):
        raise _LedgerStop("stop")

    monkeypatch.setattr(database, "write_ledger_score_explanation", stop)
    events: list[str] = []
    _watch(monkeypatch, events)
    with database.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO app_meta (k, v) VALUES ('txn-lock-marker', 'kept')"
        )
        with pytest.raises(_LedgerStop, match="stop"):
            database._insert_backtest_signal(conn, _signal())
        assert conn.execute(
            "SELECT v FROM app_meta WHERE k = 'txn-lock-marker'"
        ).fetchone()[0] == "kept"
        assert _signal_count(conn) == 0
    assert any(
        item.startswith("ROLLBACK TO SAVEPOINT backtest_signal_") for item in events
    )
    assert any(
        item.startswith("RELEASE SAVEPOINT backtest_signal_") for item in events
    )


def test_store_commits_schema_before_begin_immediate(monkeypatch):
    database.init_db()
    events: list[str] = []
    _watch(monkeypatch, events)
    assert database.store_backtest_signal(_signal()) is True
    begin = events.index("BEGIN IMMEDIATE")
    assert "COMMIT" in events[:begin]
    tail = events[begin + 1:]
    savepoint = next(
        index for index, item in enumerate(tail)
        if item.startswith("SAVEPOINT backtest_signal_")
    )
    release = next(
        index for index, item in enumerate(tail)
        if item.startswith("RELEASE SAVEPOINT backtest_signal_")
    )
    assert release > savepoint
    assert events[-1] == "COMMIT"
    assert "BEGIN" not in events


def test_dashboard_publish_failure_rolls_back_uncommitted_signal(monkeypatch):
    database.init_db()
    events: list[str] = []
    _watch(monkeypatch, events)

    def fail(_conn):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(
        "services.dashboard_snapshot_updates.mark_realtime_inputs_dirty",
        fail,
    )
    with pytest.raises(RuntimeError, match="publish failed"):
        database.store_backtest_signal(_signal())
    begin = events.index("BEGIN IMMEDIATE")
    assert "COMMIT" not in events[begin + 1:]
    with database.get_connection() as conn:
        assert _signal_count(conn) == 0


def test_future_price_batch_commits_once_and_not_immediate(monkeypatch):
    database.init_db()
    events: list[str] = []
    _watch(monkeypatch, events)
    database.store_future_prices([
        _price(trade_date="2026-07-20", ts=100),
        _price(trade_date="2026-07-21", ts=101),
    ])
    assert events.count("COMMIT") == 1
    assert "BEGIN IMMEDIATE" not in events
    with database.get_connection() as conn:
        dates = [
            row[0] for row in conn.execute(
                "SELECT trade_date FROM future_prices ORDER BY trade_date"
            )
        ]
    assert dates == ["2026-07-20", "2026-07-21"]


def test_future_price_batch_failure_hides_earlier_prices(monkeypatch):
    database.init_db()
    events: list[str] = []
    _watch(monkeypatch, events)
    broken = _price(trade_date="2026-07-21")
    del broken["close_price"]
    with pytest.raises(KeyError):
        database.store_future_prices([_price(), broken])
    assert "COMMIT" not in events
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM future_price_revisions"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM future_prices"
        ).fetchone()[0] == 0


def test_future_price_pointer_update_precedes_insert_and_is_not_upsert(monkeypatch):
    database.init_db()
    statements: list[str] = []
    _watch(monkeypatch, statements, cursor=True)
    database.store_future_prices([_price()])
    relevant = [
        sql for sql in statements
        if sql.startswith("INSERT OR IGNORE INTO future_price_revisions")
        or sql.startswith("UPDATE future_prices")
        or sql.startswith("INSERT INTO future_prices")
    ]
    assert relevant[-3].startswith("INSERT OR IGNORE INTO future_price_revisions")
    assert relevant[-2].startswith("UPDATE future_prices")
    assert relevant[-1].startswith("INSERT INTO future_prices")
    assert all("ON CONFLICT" not in sql for sql in relevant)
