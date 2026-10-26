"""Beijing day/time bucketing must not depend on the process TZ."""
import time
from datetime import datetime, timezone

import pytest

from utils import beijing_time

# 2026-10-16 17:30:00 UTC == 2026-10-17 01:30:00 Beijing: the UTC and Beijing
# calendar days differ, so a TZ-dependent implementation picks the wrong day.
INSTANT = datetime(2026, 10, 16, 17, 30, 0, tzinfo=timezone.utc)
TS = INSTANT.timestamp()


@pytest.fixture
def utc_process(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    assert time.strftime("%Y-%m-%d", time.localtime(TS)) == "2026-10-16"

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            value = INSTANT.astimezone(tz) if tz else INSTANT.astimezone().replace(tzinfo=None)
            return value

    monkeypatch.setattr(beijing_time, "datetime", Frozen)
    yield Frozen
    monkeypatch.undo()
    time.tzset()


def test_helpers_return_beijing_wall_clock_under_utc(utc_process):
    assert beijing_time.beijing_now() == datetime(2026, 10, 17, 1, 30)
    assert beijing_time.beijing_now().tzinfo is None
    assert beijing_time.beijing_fromtimestamp(TS).isoformat() == "2026-10-17T01:30:00"
    assert beijing_time.utc_naive_fromtimestamp(TS).isoformat() + "Z" == "2026-10-16T17:30:00Z"


def test_market_collector_today_is_beijing_day(utc_process, monkeypatch):
    from workers import market_close_collector as collector

    # Freeze the module clock too, so a host-local implementation is caught.
    monkeypatch.setattr(collector, "datetime", utc_process)

    assert collector._today_local() == "2026-10-17"


def test_score_distribution_buckets_by_beijing_day(utc_process, monkeypatch):
    from db import database

    monkeypatch.setattr(database, "datetime", utc_process)

    database.init_db()
    database.record_score(7.2)
    with database.get_connection() as conn:
        rows = conn.execute("SELECT date, s7 FROM score_distribution").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("2026-10-17", 1)]


def test_market_snapshot_trade_date_is_beijing_day(utc_process):
    from routes import dashboard_routes

    quotes = {
        symbol: {"symbol": symbol, "name": symbol, "price": 10.0, "last_close": 9.0,
                 "change_amt": 1.0, "change_pct": 11.1}
        for symbol in dashboard_routes._MARKET_QUOTE_SYMBOLS
    }
    _ordered, rows = dashboard_routes._validated_market_snapshot(quotes, int(TS))
    assert {row["trade_date"] for row in rows} == {"2026-10-17"}
