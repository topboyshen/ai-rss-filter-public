"""Only verified session closes may become completed event-study outcomes."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from db import database
from db.event_core import upsert_event_outcome, upsert_security
from services import event_outcomes, event_radar_query, mapping_validation


TZ = ZoneInfo("Asia/Shanghai")
SYMBOL = "sh688256"
SECURITY_ID = "SSE:688256"
CONTRACT = ("close-contract-test", "asset-map:close-test")
BASE_DATE = "2026-07-13"
TARGET_DATE = "2026-07-14"
ENDPOINTS = (
    (SYMBOL, BASE_DATE),
    (SYMBOL, TARGET_DATE),
    ("sh000300", BASE_DATE),
    ("sh000300", TARGET_DATE),
)


def _ts(value):
    return int(
        datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    )


@pytest.fixture
def close_case(monkeypatch):
    clock = {"value": _ts(f"{BASE_DATE} 10:00")}
    monkeypatch.setattr(database.time, "time", lambda: clock["value"])
    database.init_db()
    with database.get_connection() as conn:
        upsert_security(
            conn,
            exchange="SSE",
            ticker="688256",
            name="寒武纪",
            security_id=SECURITY_ID,
            asset_type="equity",
            aliases=["寒武纪", "688256"],
            now=clock["value"] - 60,
        )
        conn.execute(
            "UPDATE app_meta SET v='1' WHERE k='mapping_validation_started_at'"
        )
        conn.commit()
    signal = {
        "link": "https://example.com/close-contract",
        "raw_title": "寒武纪披露经营变化",
        "available_at": clock["value"],
        "scored_at": clock["value"],
        "score": 9.0,
        "direction": "看多",
        "score_confidence": 0.8,
        "strategy_version": CONTRACT[0],
        "mapping_version": CONTRACT[1],
        "score_path": "stage2",
        "selected": True,
        "aggregation_threshold": 7.0,
        "security_candidates": [
            {
                "security_id": SECURITY_ID,
                "relation_type": "direct",
                "direction": "看多",
                "confidence": 0.96,
                "relevance": 10.0,
                "rationale": "原文直接点名寒武纪",
            }
        ],
    }
    assert database.store_backtest_signal(signal)
    with database.get_connection() as conn:
        frozen = dict(
            conn.execute("SELECT * FROM backtest_signal_securities").fetchone()
        )
        ledger = dict(conn.execute("SELECT * FROM backtest_signals").fetchone())
    clock["value"] = _ts(f"{TARGET_DATE} 16:00")
    return SimpleNamespace(clock=clock, signal=signal, frozen=frozen, ledger=ledger)


def _quotes(*, target_price=110.0, source_time="15:00"):
    closes = (100.0, target_price, 1000.0, 1010.0)
    return [
        {
            "symbol": symbol,
            "trade_date": day,
            "name": symbol,
            "close_price": close,
            "open_price": close,
            "high_price": close * 1.01,
            "low_price": close * 0.99,
            "prev_close": close,
            "change_amt": 0.0,
            "change_pct": 0.0,
            "suspension_state": "trading",
            "limit_state": "not_at_limit",
            "tradability_state": "trading_ohlc",
            "ts": _ts(f"{day} {source_time}"),
        }
        for (symbol, day), close in zip(ENDPOINTS, closes)
    ]


def _t1_rows(case):
    view = event_radar_query.load_event_radar(
        event_id=case.ledger["event_id"], tab="reaction"
    )
    return [row for row in view["detail"]["outcomes"] if row["horizon"] == "T+1"]


def _quality_t1(case):
    report = mapping_validation.run_mapping_validation(
        days=90,
        now=case.clock["value"],
        current_contract=CONTRACT,
    )
    return next(row for row in report["horizons"] if row["horizon"] == "T+1")


def _legacy_outcome(case, quotes):
    """Seed a pre-fix outcome using the existing low-level immutable store API."""
    with database.get_connection() as conn:
        revisions = {
            (row["symbol"], row["trade_date"]): row["revision_id"]
            for row in conn.execute(
                "SELECT symbol,trade_date,revision_id FROM future_prices"
            )
        }
        upsert_security(
            conn,
            exchange="SSE",
            ticker="000300",
            name="沪深300",
            security_id="sh000300",
            asset_type="index",
            now=case.clock["value"],
        )
        gross = quotes[1]["close_price"] / quotes[0]["close_price"] - 1
        market = quotes[3]["close_price"] / quotes[2]["close_price"] - 1
        upsert_event_outcome(
            conn,
            case.ledger["event_id"],
            SECURITY_ID,
            mapping_id=case.frozen["mapping_id"],
            horizon="T+1",
            baseline_at=quotes[0]["ts"] or _ts(f"{BASE_DATE} 15:00"),
            measured_at=quotes[1]["ts"] or _ts(f"{TARGET_DATE} 15:00"),
            baseline_price=quotes[0]["close_price"],
            gross_return=gross,
            benchmark_return=market,
            excess_return=gross - market,
            direction_hit=gross > 0,
            benchmark_security_id="sh000300",
            signal_ledger_id=case.ledger["id"],
            signal_available_at=case.signal["available_at"],
            observation_state="complete",
            method_version=event_outcomes.OUTCOME_METHOD_VERSION,
            data_version=event_outcomes.OUTCOME_DATA_VERSION_PREFIX
            + ":".join(revisions[key] for key in ENDPOINTS),
            data_quality=1.0,
        )
        conn.commit()


@pytest.mark.parametrize(
    "endpoint",
    range(4),
    ids=["asset-base", "asset-target", "market-base", "market-target"],
)
@pytest.mark.parametrize("invalid_time", ["intraday", "missing"])
def test_any_non_close_endpoint_remains_unsettled(close_case, endpoint, invalid_time):
    quotes = _quotes()
    quotes[endpoint]["ts"] = (
        None if invalid_time == "missing" else _ts(f"{ENDPOINTS[endpoint][1]} 14:00")
    )
    database.store_future_prices(quotes)

    assert event_outcomes.materialize_event_outcomes(TARGET_DATE) == {
        "eligible": 0,
        "materialized": 0,
    }
    with database.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_outcomes").fetchone()[0] == 0


@pytest.mark.parametrize("endpoint", [1, 3], ids=["asset-target", "market-target"])
def test_future_source_timestamp_is_skipped_without_poisoning_materialization(
    close_case, endpoint
):
    quotes = _quotes()
    quotes[endpoint]["ts"] = _ts(f"{TARGET_DATE} 18:00")
    database.store_future_prices(quotes)

    assert event_outcomes.materialize_event_outcomes(TARGET_DATE) == {
        "eligible": 0,
        "materialized": 0,
    }


@pytest.mark.parametrize(
    "endpoint",
    range(4),
    ids=["asset-base", "asset-target", "market-base", "market-target"],
)
def test_after_close_intraday_quote_still_reports_missing_close(close_case, endpoint):
    quotes = _quotes()
    quotes[endpoint]["ts"] = _ts(f"{ENDPOINTS[endpoint][1]} 14:00")
    database.store_future_prices(quotes)

    pending = event_outcomes.get_pending_quote_requirements(TARGET_DATE)
    assert pending["pairs"] == [
        {"symbol": ENDPOINTS[endpoint][0], "trade_date": ENDPOINTS[endpoint][1]}
    ]
    rows = _t1_rows(close_case)
    assert len(rows) == 1 and rows[0]["materialized"] is False
    assert rows[0]["data_status"] == (
        "missing_asset_close" if endpoint < 2 else "missing_benchmark_close"
    )
    assert rows[0]["gross_return"] is None


def test_before_close_intraday_price_does_not_complete_a_not_due_horizon(close_case):
    close_case.clock["value"] = _ts(f"{TARGET_DATE} 10:30")
    quotes = _quotes()
    for endpoint in (1, 3):
        quotes[endpoint]["ts"] = _ts(f"{TARGET_DATE} 10:00")
    database.store_future_prices(quotes)
    assert _t1_rows(close_case)[0]["data_status"] == "not_due"

    assert event_outcomes.materialize_event_outcomes(TARGET_DATE) == {
        "eligible": 0,
        "materialized": 0,
    }
    assert _t1_rows(close_case)[0]["data_status"] == "not_due"


@pytest.mark.parametrize("source_time", ["15:00", "15:05"])
def test_valid_closes_settle_and_preserve_negative_returns(close_case, source_time):
    database.store_future_prices(_quotes(target_price=90.0, source_time=source_time))

    assert event_outcomes.materialize_event_outcomes(TARGET_DATE) == {
        "eligible": 1,
        "materialized": 1,
    }
    actual = next(row for row in _t1_rows(close_case) if row["materialized"])
    assert actual["method_supported"] is True
    assert actual["data_status"] == "complete_close_only"
    assert actual["gross_return"] == pytest.approx(-0.10)
    assert actual["excess_return"] == pytest.approx(-0.11)
    assert actual["direction_hit"] is False
    assert _quality_t1(close_case)["complete_pairs"] == 1


def test_genuinely_missing_quote_keeps_missing_status(close_case):
    database.store_future_prices(_quotes()[1:])

    assert event_outcomes.materialize_event_outcomes(TARGET_DATE) == {
        "eligible": 0,
        "materialized": 0,
    }
    row = _t1_rows(close_case)[0]
    assert row["materialized"] is False
    assert row["data_status"] == "missing_asset_close"
    assert row["gross_return"] is None
    assert _quality_t1(close_case)["complete_pairs"] == 0


@pytest.mark.parametrize(
    "endpoint",
    range(4),
    ids=["asset-base", "asset-target", "market-base", "market-target"],
)
@pytest.mark.parametrize("invalid_time", ["intraday", "missing"])
def test_existing_non_close_outcome_is_audit_only_in_reader(
    close_case, endpoint, invalid_time
):
    quotes = _quotes()
    quotes[endpoint]["ts"] = (
        None if invalid_time == "missing" else _ts(f"{ENDPOINTS[endpoint][1]} 14:00")
    )
    database.store_future_prices(quotes)
    _legacy_outcome(close_case, quotes)

    actual = next(row for row in _t1_rows(close_case) if row["materialized"])
    assert actual["method_supported"] is False
    assert actual["data_status"] == "legacy_unknown"
    assert actual["gross_return"] == pytest.approx(0.10)  # Audit data is retained.


def test_existing_intraday_outcome_is_not_a_historical_hit(close_case):
    quotes = _quotes()
    quotes[1]["ts"] = _ts(f"{TARGET_DATE} 14:00")
    database.store_future_prices(quotes)
    _legacy_outcome(close_case, quotes)
    probe = {
        "signal_available_at": _ts("2026-07-15 10:00"),
        "security_id": SECURITY_ID,
        "horizon": "T+1",
        "method_version": event_outcomes.OUTCOME_METHOD_VERSION,
        "benchmark_security_id": "sh000300",
        "relation_type": "direct",
        "ledger_strategy_version": CONTRACT[0],
        "ledger_mapping_version": CONTRACT[1],
    }

    with database.get_connection() as conn:
        assert event_radar_query._historical_outcome_stats(conn, probe) == (0, 0, 0)


@pytest.mark.parametrize("invalid_time", ["intraday", "missing"])
def test_existing_non_close_outcome_is_excluded_from_mapping_quality(
    close_case, invalid_time
):
    quotes = _quotes()
    quotes[1]["ts"] = None if invalid_time == "missing" else _ts(f"{TARGET_DATE} 14:00")
    database.store_future_prices(quotes)
    _legacy_outcome(close_case, quotes)

    horizon = _quality_t1(close_case)
    assert horizon["due_pairs"] == 1
    assert horizon["complete_pairs"] == 0
    assert horizon["directional_complete_pairs"] == 0


@pytest.mark.parametrize("invalid_time", ["intraday", "missing"])
def test_corrected_close_does_not_validate_an_old_outcome_without_rematerializing(
    close_case, invalid_time
):
    quotes = _quotes()
    quotes[1]["ts"] = None if invalid_time == "missing" else _ts(f"{TARGET_DATE} 14:00")
    database.store_future_prices(quotes)
    _legacy_outcome(close_case, quotes)
    with database.get_connection() as conn:
        original = dict(conn.execute("SELECT * FROM event_outcomes").fetchone())
    probe = {
        "signal_available_at": _ts("2026-07-15 10:00"),
        "security_id": SECURITY_ID,
        "horizon": "T+1",
        "method_version": event_outcomes.OUTCOME_METHOD_VERSION,
        "benchmark_security_id": "sh000300",
        "relation_type": "direct",
        "ledger_strategy_version": CONTRACT[0],
        "ledger_mapping_version": CONTRACT[1],
    }

    close_case.clock["value"] += 60
    database.store_future_prices(_quotes(target_price=90.0))
    before = next(row for row in _t1_rows(close_case) if row["materialized"])
    assert before["method_supported"] is False
    assert before["data_status"] == "legacy_unknown"
    assert before["gross_return"] == pytest.approx(0.10)
    assert _quality_t1(close_case)["complete_pairs"] == 0
    with database.get_connection() as conn:
        assert dict(conn.execute("SELECT * FROM event_outcomes").fetchone()) == original
        assert event_radar_query._historical_outcome_stats(conn, probe) == (0, 0, 0)

    assert event_outcomes.materialize_event_outcomes(TARGET_DATE) == {
        "eligible": 1,
        "materialized": 1,
    }
    after = next(row for row in _t1_rows(close_case) if row["materialized"])
    assert after["method_supported"] is True
    assert after["data_status"] == "complete_close_only"
    assert after["gross_return"] == pytest.approx(-0.10)
    assert after["direction_hit"] is False
    assert _quality_t1(close_case)["complete_pairs"] == 1
    with database.get_connection() as conn:
        revisions = [
            dict(row)
            for row in conn.execute("SELECT * FROM event_outcomes ORDER BY revision")
        ]
        assert event_radar_query._historical_outcome_stats(conn, probe) == (1, 1, 0)
    assert len(revisions) == 2
    assert revisions[0] == {**original, "is_current": 0}
    assert revisions[1]["is_current"] == 1
    assert revisions[1]["outcome_id"] == original["outcome_id"]
    assert revisions[1]["outcome_revision_id"] != original["outcome_revision_id"]
    assert revisions[1]["data_version"] != original["data_version"]


@pytest.mark.parametrize("field", ["close_price", "ts", "evaluation_at"])
@pytest.mark.parametrize(
    "invalid_value",
    [True, False, float("nan"), float("inf"), float("-inf")],
    ids=["true", "false", "nan", "positive-infinity", "negative-infinity"],
)
def test_close_contract_rejects_boolean_and_non_finite_values(field, invalid_value):
    row = {
        "close_price": 100.0,
        "trade_date": TARGET_DATE,
        "ts": _ts(f"{TARGET_DATE} 15:00"),
    }
    evaluation_at = _ts(f"{TARGET_DATE} 16:00")
    if field == "evaluation_at":
        evaluation_at = invalid_value
    else:
        row[field] = invalid_value

    assert not event_outcomes.is_completed_close_price(
        row, TARGET_DATE, evaluation_at=evaluation_at
    )


@pytest.mark.parametrize(
    "source_time,expected",
    [("14:59", False), ("15:00", True), ("15:01", True)],
)
def test_close_contract_uses_exact_session_close_boundary(source_time, expected):
    row = {
        "close_price": 100.0,
        "trade_date": TARGET_DATE,
        "ts": _ts(f"{TARGET_DATE} {source_time}"),
    }

    assert (
        event_outcomes.is_completed_close_price(
            row, TARGET_DATE, evaluation_at=_ts(f"{TARGET_DATE} 16:00")
        )
        is expected
    )


@pytest.mark.parametrize("mismatch", ["row-date", "source-date"])
def test_close_contract_rejects_a_price_from_another_day(mismatch):
    row = {
        "close_price": 100.0,
        "trade_date": TARGET_DATE,
        "ts": _ts(f"{TARGET_DATE} 15:00"),
    }
    if mismatch == "row-date":
        row["trade_date"] = BASE_DATE
    else:
        row["ts"] = _ts(f"{BASE_DATE} 15:00")

    assert not event_outcomes.is_completed_close_price(
        row, TARGET_DATE, evaluation_at=_ts(f"{TARGET_DATE} 16:00")
    )
