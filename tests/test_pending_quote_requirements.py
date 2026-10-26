from contextlib import contextmanager
from datetime import datetime
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from db import database
from db.event_core import upsert_event_security, upsert_security
from services import event_outcomes


EQUITY_ID = "SSE:688306"
SYMBOL = "sh688306"
TZ = ZoneInfo("Asia/Shanghai")


def _ts(value):
    return int(
        datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    )


def _seed(monkeypatch, available="2026-09-01 10:00", **overrides):
    database.init_db()
    available_at = _ts(available)
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688306",
            name="均普智能",
            security_id=EQUITY_ID,
            asset_type="equity",
            aliases=["均普智能"],
            now=available_at - 60,
        )
        conn.execute(
            "UPDATE app_meta SET v = '1' WHERE k = 'mapping_validation_started_at'"
        )
        conn.commit()
    monkeypatch.setattr(database.time, "time", lambda: available_at)
    signal = {
        "link": "https://example.com/pending-price",
        "available_at": available_at,
        "scored_at": available_at,
        "score": 9.0,
        "direction": "看多",
        "strategy_version": "historical-strategy",
        "mapping_version": "asset-map:v1",
        "score_path": "stage2",
        "selected": True,
        "aggregation_threshold": 7.0,
        "security_candidates": [
            {
                "security_id": EQUITY_ID,
                "relation_type": "direct",
                "direction": "看多",
                "confidence": 0.95,
                "relevance": 10.0,
                "rationale": "公司获得订单",
            }
        ],
        **overrides,
    }
    assert database.store_backtest_signal(signal)
    return signal


def _price(symbol, day):
    return {
        "symbol": symbol,
        "trade_date": day,
        "name": symbol,
        "close_price": 10.0,
        "change_amt": 0.0,
        "change_pct": 0.0,
        "ts": _ts(f"{day} 15:00"),
    }


def _pairs(result):
    return [(row["symbol"], row["trade_date"]) for row in result["pairs"]]


def test_exact_due_baseline_and_horizons_include_benchmark(monkeypatch):
    _seed(monkeypatch)
    result = event_outcomes.get_pending_quote_requirements("2026-09-07")
    assert _pairs(result) == [
        (symbol, day)
        for day in ["2026-09-04", "2026-09-02", "2026-09-01"]
        for symbol in ["sh000300", SYMBOL]
    ]
    assert result["total"] == 6
    assert result["truncated"] is False
    # September 7 is T+4, not a substitute for missing T+3 on September 4.
    assert (
        event_outcomes.get_pending_quote_requirements(
            "2026-09-07",
            start_date="2026-09-07",
        )["pairs"]
        == []
    )
    assert _pairs(
        event_outcomes.get_pending_quote_requirements(
            "2026-09-08",
            start_date="2026-09-08",
        )
    ) == [("sh000300", "2026-09-08"), (SYMBOL, "2026-09-08")]
    assert {
        day
        for _, day in _pairs(
            event_outcomes.get_pending_quote_requirements(
                "2026-09-30",
            )
        )
    } == {"2026-09-01", "2026-09-02", "2026-09-04", "2026-09-08", "2026-09-30"}


def test_after_close_baseline_crosses_weekend_and_holiday(monkeypatch):
    _seed(monkeypatch, "2026-09-24 15:00")
    assert _pairs(event_outcomes.get_pending_quote_requirements("2026-09-29")) == [
        ("sh000300", "2026-09-29"),
        (SYMBOL, "2026-09-29"),
        ("sh000300", "2026-09-28"),
        (SYMBOL, "2026-09-28"),
    ]


def test_retired_frozen_mapping_survives_current_universe_removal(monkeypatch):
    signal = _seed(monkeypatch)
    with database.get_connection() as conn:
        mapping_id, event_id = conn.execute(
            "SELECT mapping_id, event_id FROM backtest_signal_securities"
        ).fetchone()
        replacement = upsert_event_security(
            conn,
            event_id,
            EQUITY_ID,
            relation_type="direct",
            direction="看空",
            confidence=0.95,
            relevance=10.0,
            rationale="后续修订",
            mapping_version="asset-map:v2",
            now=signal["available_at"] + 60,
        )
        assert replacement != mapping_id
        assert (
            conn.execute(
                "SELECT is_current FROM event_securities WHERE mapping_id = ?",
                (mapping_id,),
            ).fetchone()[0]
            == 0
        )
        conn.execute(
            "UPDATE securities SET is_active = 0 WHERE security_id = ?", (EQUITY_ID,)
        )
        conn.commit()
    assert database.get_event_quote_universe() == []
    assert (SYMBOL, "2026-09-04") in _pairs(
        event_outcomes.get_pending_quote_requirements("2026-09-04")
    )


def test_latest_withdrawal_is_not_resurrected_by_historical_target(monkeypatch):
    signal = _seed(monkeypatch, "2026-09-04 10:00")
    withdrew_at = _ts("2026-09-05 10:00")
    monkeypatch.setattr(database.time, "time", lambda: withdrew_at)
    assert database.store_backtest_signal(
        {
            **signal,
            "available_at": withdrew_at,
            "scored_at": withdrew_at,
            "revision_key": "radar:1",
            "revision_seq": 1,
            "score_path": "radar",
            "signal_stage": "radar",
            "selected": False,
        }
    )
    assert event_outcomes.get_pending_quote_requirements("2026-09-04")["pairs"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"selected": 0},
        {"is_similarity_copy": 1},
        {"score_path": "parse_error"},
        {"score_path": "llm_safe_neutral_contract"},
        {"event_version": 0},
        {"relation_type": "concept"},
        {"asset_type": "index"},
        {"frozen_mapping_id": None},
        {"frozen_available_at": 1},
        {"captured_at": 1},
        {"mapping_created_at": 9_999_999_999},
        {"frozen_mapping_revision": 2},
        {"mapping_version": "other"},
        {"signal_event_id": "wrong"},
        {"frozen_event_id": "wrong"},
        {"frozen_security_id": "wrong"},
    ],
)
def test_pending_reuses_exact_v5_snapshot_eligibility(monkeypatch, changes):
    _seed(monkeypatch)
    with database.get_connection() as conn:
        rows, events, marker = event_outcomes._read_frozen_mapping_snapshot(conn)
    invalid = {**dict(rows[0]), **changes}
    monkeypatch.setattr(
        event_outcomes,
        "_read_frozen_mapping_snapshot",
        lambda *_args, **_kwargs: ([invalid], events, marker),
    )
    assert event_outcomes.get_pending_quote_requirements("2026-09-04")["pairs"] == []
    assert event_outcomes.materialize_event_outcomes("2026-09-04")["eligible"] == 0


@pytest.mark.parametrize("marker", [None, "invalid", "9999999999"])
def test_validation_marker_is_required(monkeypatch, marker):
    _seed(monkeypatch)
    with database.get_connection() as conn:
        if marker is None:
            conn.execute(
                "DELETE FROM app_meta WHERE k = 'mapping_validation_started_at'"
            )
        else:
            conn.execute(
                "UPDATE app_meta SET v = ? WHERE k = 'mapping_validation_started_at'",
                (marker,),
            )
        conn.commit()
    assert event_outcomes.get_pending_quote_requirements("2026-09-04")["total"] == 0


def test_present_prices_are_skipped_and_missing_pairs_deduplicated(monkeypatch):
    _seed(monkeypatch)
    _seed(
        monkeypatch,
        link="https://example.com/second-pending",
        strategy_version="other-strategy",
    )
    database.store_future_prices(
        [
            _price("sh000300", "2026-09-01"),
            _price(SYMBOL, "2026-09-01"),
            _price("sh000300", "2026-09-02"),
            _price(SYMBOL, "2026-09-02"),
            _price("sh000300", "2026-09-04"),
            _price(SYMBOL, "2026-09-03"),  # Must not replace the missing T+3 date.
        ]
    )
    assert event_outcomes.get_pending_quote_requirements("2026-09-04") == {
        "pairs": [{"symbol": SYMBOL, "trade_date": "2026-09-04"}],
        "total": 1,
        "truncated": False,
    }


def test_limit_reports_overflow_and_preserves_newest_dates_first(monkeypatch):
    _seed(monkeypatch)
    result = event_outcomes.get_pending_quote_requirements("2026-09-04", limit=3)
    assert result == {
        "pairs": [
            {"symbol": "sh000300", "trade_date": "2026-09-04"},
            {"symbol": SYMBOL, "trade_date": "2026-09-04"},
            {"symbol": "sh000300", "trade_date": "2026-09-02"},
        ],
        "total": 6,
        "truncated": True,
    }


@pytest.mark.parametrize(
    "column, value",
    [
        ("close_price", 0.0),
        ("close_price", float("inf")),
        ("ts", _ts("2026-09-01 14:59")),
        ("ts", _ts("2026-09-02 15:00")),
        ("revision_id", None),
    ],
)
def test_unusable_stored_price_is_still_pending(monkeypatch, column, value):
    _seed(monkeypatch)
    database.store_future_prices(
        [
            _price(SYMBOL, "2026-09-01"),
            _price("sh000300", "2026-09-01"),
        ]
    )
    original = database.get_read_only_connection

    # Current pointer triggers correctly prohibit corrupting the fixture DB.
    # Simulate an unusable legacy read result without disabling those triggers.
    @contextmanager
    def legacy_read():
        with original() as conn:

            def execute(sql, parameters=()):
                cursor = conn.execute(sql, parameters)
                if "JOIN future_prices" not in sql:
                    return cursor
                rows = [dict(row) for row in cursor.fetchall()]
                for row in rows:
                    if row["symbol"] == SYMBOL:
                        row[column] = value
                return SimpleNamespace(fetchall=lambda: rows)

            yield SimpleNamespace(execute=execute, rollback=conn.rollback)

    monkeypatch.setattr(database, "get_read_only_connection", legacy_read)
    assert _pairs(event_outcomes.get_pending_quote_requirements("2026-09-01")) == [
        (SYMBOL, "2026-09-01"),
    ]


def test_signal_lookback_is_explicitly_bounded(monkeypatch):
    _seed(monkeypatch, "2026-04-01 10:00")
    assert event_outcomes.get_pending_quote_requirements("2026-09-04")["total"] == 0


def test_recent_replay_keeps_true_anchor_before_lookback(monkeypatch):
    _seed(monkeypatch, "2026-04-01 10:00")
    with database.get_connection() as conn:
        event_id = conn.execute("SELECT event_id FROM backtest_signals").fetchone()[0]
    _seed(
        monkeypatch,
        "2026-09-01 10:00",
        event_id=event_id,
        link="https://example.com/reused-event",
    )
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT mapping_id) FROM backtest_signal_securities"
        ).fetchone()[:] == (2, 1)
        full = event_outcomes._eligible_frozen_mappings(
            *event_outcomes._read_frozen_mapping_snapshot(conn)
        )
        assert len(full) == 1
        assert full[0]["available_at"] == _ts("2026-04-01 10:00")
    # A recent replay cannot invent a September baseline that V5 won't settle.
    assert event_outcomes.get_pending_quote_requirements("2026-09-04")["total"] == 0


def test_reads_only_demanded_pairs_in_one_price_batch_and_never_writes(monkeypatch):
    _seed(monkeypatch)
    statements = []
    original = database.get_read_only_connection

    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(database, "get_read_only_connection", traced)
    event_outcomes.get_pending_quote_requirements("2026-09-30")
    price_reads = [sql for sql in statements if "JOIN future_prices" in sql]
    assert len(price_reads) == 1
    assert "WITH requested(symbol, trade_date) AS (VALUES" in price_reads[0]
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )


def test_frozen_snapshot_probes_latest_revision_with_existing_indexes(monkeypatch):
    _seed(monkeypatch)
    plans = []
    original = database.get_read_only_connection

    @contextmanager
    def explained():
        with original() as conn:
            def execute(sql, parameters=()):
                if "SELECT signal.id AS ledger_signal_id" in sql:
                    assert "ROW_NUMBER" not in sql
                    plans.extend(
                        row[3] for row in conn.execute(
                            "EXPLAIN QUERY PLAN " + sql, parameters,
                        )
                    )
                return conn.execute(sql, parameters)
            yield SimpleNamespace(execute=execute, rollback=conn.rollback)

    monkeypatch.setattr(database, "get_read_only_connection", explained)
    assert event_outcomes.get_pending_quote_requirements("2026-09-07")["total"] == 6
    assert any("idx_backtest_signals_selected_available" in plan for plan in plans)
    assert any("idx_backtest_signals_link_revision" in plan for plan in plans)


def test_large_exact_pair_demand_is_batched_and_horizon_dates_are_cached(monkeypatch):
    _seed(monkeypatch)
    with database.get_connection() as conn:
        rows, events, marker = event_outcomes._read_frozen_mapping_snapshot(conn)
    # Expand the already validated row shape to exercise the price-read bound,
    # without hundreds of unrelated event-core fixture writes.
    mappings = []
    for number in range(550):
        ticker = str(600000 + number)
        mappings.append(
            {
                **dict(rows[0]),
                "ticker": ticker,
                "security_id": f"SSE:{ticker}",
                "frozen_security_id": f"SSE:{ticker}",
            }
        )
    monkeypatch.setattr(
        event_outcomes,
        "_read_frozen_mapping_snapshot",
        lambda *_args, **_kwargs: (mappings, events, marker),
    )
    statements, horizons = [], []
    original_connection = database.get_read_only_connection
    original_horizon = event_outcomes.horizon_trade_date

    @contextmanager
    def traced():
        with original_connection() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    def horizon(day, offset):
        horizons.append((day, offset))
        return original_horizon(day, offset)

    monkeypatch.setattr(database, "get_read_only_connection", traced)
    monkeypatch.setattr(event_outcomes, "horizon_trade_date", horizon)
    result = event_outcomes.get_pending_quote_requirements("2026-09-01", limit=25)
    assert result["total"] == 551
    assert len(result["pairs"]) == 25
    assert result["truncated"] is True
    assert len([sql for sql in statements if "JOIN future_prices" in sql]) == 2
    assert len(horizons) == 4


def test_legacy_empty_database_does_not_mask_missing_modern_tables(monkeypatch):
    with database.get_connection():
        pass
    assert event_outcomes.get_pending_quote_requirements("2026-09-04")["total"] == 0
    _seed(monkeypatch)
    with database.get_connection() as conn:
        conn.execute(
            "ALTER TABLE backtest_signal_securities RENAME TO missing_frozen_snapshots"
        )
        conn.commit()
    with pytest.raises(sqlite3.OperationalError, match="backtest_signal_securities"):
        event_outcomes.get_pending_quote_requirements("2026-09-04")


@pytest.mark.parametrize(
    "target, start",
    [
        ("2026-9-4", None),
        ("2026-02-30", None),
        ("2026-09-04", "2026-09-05"),
    ],
)
def test_invalid_date_arguments_are_rejected(target, start):
    with pytest.raises(ValueError):
        event_outcomes.get_pending_quote_requirements(target, start_date=start)
