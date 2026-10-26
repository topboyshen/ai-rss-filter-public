"""Completed-close existence checks remain local to the selected event."""

from datetime import datetime
import sqlite3

import pytest

from services import event_radar_query as radar


BASE_DAY = "2026-07-13"
TARGET_DAY = "2026-07-14"


def _ts(day, hour=15):
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+08:00").timestamp())


READ_AT = _ts(TARGET_DAY, 16)
MAPPING = {"exchange": "SSE", "ticker": "600000", "available_at": _ts(BASE_DAY, 10)}


class _PriceConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith("WITH requested("):
            self.price_queries.append((sql, tuple(parameters)))
        return super().execute(sql, parameters)


@pytest.fixture
def price_conn(monkeypatch):
    conn = sqlite3.connect(":memory:", factory=_PriceConnection)
    conn.row_factory = sqlite3.Row
    conn.price_queries = []
    conn.executescript(
        "CREATE TABLE future_price_revisions (revision_id TEXT PRIMARY KEY, "
        "symbol TEXT, trade_date TEXT, close_price REAL, source_ts INTEGER, recorded_at INTEGER);"
        "CREATE INDEX idx_future_price_revisions_symbol_date "
        "ON future_price_revisions(symbol, trade_date, recorded_at DESC);"
    )
    monkeypatch.setattr(radar, "baseline_trade_date", lambda _: BASE_DAY)
    monkeypatch.setattr(radar, "horizon_trade_date", lambda *_: TARGET_DAY)
    try:
        yield conn
    finally:
        conn.close()


def _measured_read(conn):
    steps = [0]

    def count():
        steps[0] += 1
        return 0

    conn.set_progress_handler(count, 1)
    try:
        pairs = radar._completed_price_pairs(conn, READ_AT, [MAPPING])
    finally:
        conn.set_progress_handler(None, 0)
    return pairs, steps[0]


def test_unrelated_revisions_are_not_returned_or_checked(price_conn, monkeypatch):
    price_conn.executemany(
        "INSERT INTO future_price_revisions VALUES (?, ?, ?, ?, ?, ?)",
        [
            (f"wanted-{symbol}-{day}", symbol, day, 10.0, _ts(day), READ_AT)
            for symbol in ("Sh600000", "SH000300")
            for day in (BASE_DAY, TARGET_DAY)
        ],
    )
    original = radar.is_completed_close_price
    checked = []

    def observe(row, *args, **kwargs):
        checked.append((row["symbol"], row["trade_date"]))
        return original(row, *args, **kwargs)

    monkeypatch.setattr(radar, "is_completed_close_price", observe)
    initial, initial_steps = _measured_read(price_conn)
    assert len(initial) == len(checked) == 4
    price_conn.executemany(
        "INSERT INTO future_price_revisions VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                f"unrelated-{number}",
                f"sh{number:06d}",
                TARGET_DAY,
                20.0,
                _ts(TARGET_DAY),
                READ_AT,
            )
            for number in range(1, 20_001)
            if number != 300
        ]
        + [
            (f"unrelated-date-{number}", "sh600000", "2025-01-02", 30.0 + number, 1, 1)
            for number in range(2_000)
        ],
    )
    checked.clear()
    grown, grown_steps = _measured_read(price_conn)
    assert grown == initial
    assert set(checked) == initial and len(checked) == 4
    query, parameters = price_conn.price_queries[-1]
    plan = [
        row[3] for row in price_conn.execute("EXPLAIN QUERY PLAN " + query, parameters)
    ]
    assert any(
        "SEARCH price USING INDEX idx_future_price_revisions_symbol_date" in step
        for step in plan
    ), plan
    assert not any("SCAN price" in step or "AUTOMATIC" in step for step in plan), plan
    assert grown_steps <= initial_steps + 500, (initial_steps, grown_steps, plan)


@pytest.mark.parametrize(
    ("close", "source_at", "recorded_at"),
    [
        (10.0, _ts(TARGET_DAY, 14), READ_AT),
        (float("inf"), _ts(TARGET_DAY), READ_AT),
        (float("nan"), _ts(TARGET_DAY), READ_AT),
        (10.0, _ts(TARGET_DAY, 17), READ_AT),
        (10.0, _ts(BASE_DAY), READ_AT),
        (10.0, None, READ_AT),
        (10.0, _ts(TARGET_DAY), READ_AT + 1),
        (10.0, _ts(TARGET_DAY), 0),
    ],
)
def test_pair_bound_keeps_completed_close_contract(
    price_conn, close, source_at, recorded_at
):
    price_conn.execute(
        "INSERT INTO future_price_revisions VALUES ('invalid', 'sh600000', ?, ?, ?, ?)",
        (TARGET_DAY, close, source_at, recorded_at),
    )
    assert radar._completed_price_pairs(price_conn, READ_AT, [MAPPING]) == set()


def test_empty_event_does_not_read_price_revisions(price_conn):
    assert radar._completed_price_pairs(price_conn, READ_AT, []) == set()
    assert price_conn.price_queries == []
