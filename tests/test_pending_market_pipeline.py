"""Real SQLite coverage from frozen signal demand through persisted outcomes."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from db import database
from db.event_core import upsert_event_security, upsert_security
from scripts import repair_market_close as repair
from services import event_outcomes
from workers import market_close_collector as collector


SYMBOL = "sh688256"
SECURITY = "SSE:688256"
BASELINE = "2026-07-13"
TARGET = "2026-07-14"
TZ = ZoneInfo("Asia/Shanghai")


def _ts(value):
    return int(
        datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    )


def _quote(symbol, trade_date):
    close = 100.0 if trade_date == BASELINE else 110.0
    return {
        "symbol": symbol,
        "name": symbol,
        "trade_date": trade_date,
        "source_trade_date": trade_date,
        "close_price": close,
        "open_price": close,
        "high_price": close,
        "low_price": close,
        "prev_close": 100.0,
        "change_amt": close - 100.0,
        "change_pct": close - 100.0,
        "volume": 1000.0,
        "ts": _ts(trade_date + " 15:00"),
    }


def _seed_retired_frozen_signal(monkeypatch):
    database.init_db()
    available_at = _ts(BASELINE + " 10:00")
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id=SECURITY,
            asset_type="equity",
            now=available_at - 60,
        )
        conn.execute(
            "UPDATE app_meta SET v='1' WHERE k='mapping_validation_started_at'"
        )
        conn.commit()
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    assert database.store_backtest_signal(
        {
            "link": "https://example.com/pending-real-pipeline",
            "event_key": "pending-real-pipeline",
            "available_at": available_at,
            "scored_at": available_at,
            "published_at": available_at - 60,
            "score": 9.0,
            "direction": "看多",
            "score_confidence": 0.8,
            "strategy_version": "strategy-v1",
            "mapping_version": "asset-map:v1",
            "mapped_theme": "AI算力",
            "backtest_symbol": "sz399006",
            "score_path": "stage2",
            "aggregation_threshold": 7.0,
            "selected": True,
            "security_candidates": [
                {
                    "security_id": SECURITY,
                    "relation_type": "direct",
                    "direction": "看多",
                    "confidence": 0.96,
                    "relevance": 10.0,
                    "rationale": "原文直接点名寒武纪",
                }
            ],
        }
    )
    with database.get_connection() as conn:
        frozen_id, event_id = conn.execute(
            "SELECT mapping_id, event_id FROM backtest_signal_securities LIMIT 1"
        ).fetchone()
        replacement = upsert_event_security(
            conn,
            event_id,
            SECURITY,
            relation_type="direct",
            direction="看空",
            confidence=0.95,
            relevance=10.0,
            rationale="后续修订",
            mapping_version="asset-map:v2",
            now=available_at + 3600,
        )
        assert replacement != frozen_id
        assert (
            conn.execute(
                "SELECT is_current FROM event_securities WHERE mapping_id=?",
                (frozen_id,),
            ).fetchone()[0]
            == 0
        )
        conn.commit()
    monkeypatch.setattr(database.time, "time", lambda: _ts(TARGET + " 15:25"))
    return frozen_id


def _assert_persisted_result(frozen_id):
    assert event_outcomes.get_pending_quote_requirements(TARGET)["total"] == 0
    with database.get_read_only_connection() as conn:
        rows = conn.execute(
            "SELECT mapping_id, horizon, gross_return, excess_return "
            "FROM event_outcomes WHERE is_current=1"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0:2] == (frozen_id, "T+1")
        assert rows[0][2] == pytest.approx(0.1)
        assert rows[0][3] == pytest.approx(0.0)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM future_prices WHERE trade_date > ?", (TARGET,)
            ).fetchone()[0]
            == 0
        )
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("future_prices", "future_price_revisions", "event_outcomes")
        )


def test_collector_persists_due_retired_frozen_stock_without_current_universe(
    monkeypatch,
):
    frozen_id = _seed_retired_frozen_signal(monkeypatch)
    database.store_future_prices(
        [
            _quote(SYMBOL, BASELINE),
            _quote("sh000300", BASELINE),
        ]
    )
    monkeypatch.setattr(collector, "_today_local", lambda: TARGET)
    monkeypatch.setattr(collector.database, "get_event_quote_universe", lambda **kw: [])
    requests = []

    def fetch(symbols):
        requests.extend(symbols)
        return [_quote(symbol, TARGET) for symbol in symbols]

    monkeypatch.setattr(collector, "fetch_index_close_prices", fetch)
    monkeypatch.setattr(
        collector,
        "fetch_tencent_daily_close_prices",
        lambda *a, **kw: pytest.fail(
            "complete live response must not call daily source"
        ),
    )
    assert collector.run(TARGET) == 5
    assert SYMBOL in requests
    counts = _assert_persisted_result(frozen_id)

    requests.clear()
    assert collector.run(TARGET) == 4
    assert SYMBOL not in requests
    assert _assert_persisted_result(frozen_id) == counts


def test_same_day_repair_does_not_fill_historical_baseline(monkeypatch, tmp_path):
    _seed_retired_frozen_signal(monkeypatch)
    database.store_future_prices([_quote(symbol, TARGET) for symbol in repair.REQUIRED])
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", str(tmp_path / "market_close"))
    monkeypatch.setattr(repair, "DB", Path(database.DB_PATH))
    monkeypatch.setattr(repair, "_expected_trade_date", lambda now: TARGET)
    assert repair._missing(TARGET) == []
    requests = []

    def fetch(symbols, *, trade_date):
        requests.extend((symbol, trade_date) for symbol in symbols)
        return [_quote(symbol, trade_date) for symbol in symbols]

    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", fetch)
    monkeypatch.setattr(
        repair,
        "_sina_daily",
        lambda *a: pytest.fail("complete exact-date response"),
    )
    assert repair.main() == 0
    assert (SYMBOL, BASELINE) not in requests
    assert ("sh000300", BASELINE) not in requests
    leftover = event_outcomes.get_pending_quote_requirements(TARGET)
    assert leftover["total"] > 0
    assert any(item["trade_date"] == BASELINE for item in leftover["pairs"])


def test_history_repair_fills_historical_baseline(monkeypatch, tmp_path):
    frozen_id = _seed_retired_frozen_signal(monkeypatch)
    database.store_future_prices([_quote(symbol, TARGET) for symbol in repair.REQUIRED])
    monkeypatch.setenv("MARKET_CLOSE_RESULT_DIR", str(tmp_path / "market_close"))
    monkeypatch.setattr(repair, "DB", Path(database.DB_PATH))
    monkeypatch.setattr(repair, "_expected_trade_date", lambda now: TARGET)
    monkeypatch.setattr(repair, "_previous_trading_day", lambda date: BASELINE)
    requests = []

    def fetch(symbols, *, trade_date):
        requests.extend((symbol, trade_date) for symbol in symbols)
        return [_quote(symbol, trade_date) for symbol in symbols]

    monkeypatch.setattr(repair, "fetch_tencent_daily_close_prices", fetch)
    monkeypatch.setattr(
        repair,
        "_sina_daily",
        lambda *a: pytest.fail("complete exact-date response"),
    )
    assert repair.main() == 0
    requests.clear()
    assert repair.main(["--scope", "history"]) == 0
    assert (SYMBOL, BASELINE) in set(requests)
    assert (SYMBOL, TARGET) not in set(requests)
    counts = _assert_persisted_result(frozen_id)
    requests.clear()
    assert repair.main(["--scope", "history"]) == 0
    assert requests == []
    assert _assert_persisted_result(frozen_id) == counts
