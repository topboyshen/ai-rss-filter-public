"""Rejection contract of the stable price-outcome validator (real SQLite).

``validate_stable_event_core`` re-derives every price outcome from the frozen
signal ledger and the immutable ``future_price_revisions`` rows it cites.  Each
test below builds one fully consistent ``a-share-event-ohlc-v4`` outcome in a
real database, proves it validates, and then corrupts exactly one fact the
validator is responsible for, asserting the specific error.  The validator is
read-only, so every case also checks that it changed nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from db import database
from db import event_migration as migration
from db.event_core import resolve_or_create_event, upsert_event_security, upsert_security
from services.event_outcomes import (
    MARKET_BENCHMARK_SYMBOL,
    baseline_trade_date,
    direction_hit_for_return,
)

SH = ZoneInfo("Asia/Shanghai")
V4 = "a-share-event-ohlc-v4"
V4_PREFIX = "future-prices-event-ohlc-v4:"
AVAILABLE_AT = int(datetime(2026, 9, 15, 10, 0, tzinfo=SH).timestamp())  # Tuesday
ASSET_SYMBOL = "sh990001"
CLOSES = {"asset_base": 10.0, "asset_measured": 11.0, "bench_base": 4000.0, "bench_measured": 4040.0}


def _close_ts(trade_date: str) -> int:
    day = datetime.strptime(trade_date, "%Y-%m-%d").date()
    return int(datetime.combine(day, migration._MARKET_CLOSE, tzinfo=SH).timestamp())


def _uuid() -> str:
    return str(uuid.uuid4())


def _build(conn, *, outcome=None, ledger=None, revisions=None, direction="看多", horizon_days=1):
    """Insert one consistent v4 outcome, then apply per-test overrides."""
    base_date = baseline_trade_date(AVAILABLE_AT)
    measured_date = database._trading_day_after(base_date, horizon_days)
    event_id = resolve_or_create_event(conn, {"wire": _uuid()}, title="测试事件", now=AVAILABLE_AT - 60)
    asset_id = upsert_security(conn, exchange="SH", ticker="990001", name="测试证券",
                               security_id=ASSET_SYMBOL, now=AVAILABLE_AT - 60)
    upsert_security(conn, exchange="SH", ticker="000300", name="沪深300", asset_type="index",
                    security_id=MARKET_BENCHMARK_SYMBOL, now=AVAILABLE_AT - 60)
    mapping_id = upsert_event_security(conn, event_id, asset_id, relation_type="direct",
                                       direction=direction, now=AVAILABLE_AT - 30)

    roles = [
        ("asset_base", ASSET_SYMBOL, base_date),
        ("asset_measured", ASSET_SYMBOL, measured_date),
        ("bench_base", MARKET_BENCHMARK_SYMBOL, base_date),
        ("bench_measured", MARKET_BENCHMARK_SYMBOL, measured_date),
    ]
    revision_ids = []
    for role, symbol, trade_date in roles:
        row = {
            "revision_id": _uuid(), "symbol": symbol, "trade_date": trade_date,
            "close_price": CLOSES[role], "source_ts": None,
            "recorded_at": _close_ts(measured_date) + 600,
        }
        row.update((revisions or {}).get(role, {}))
        conn.execute(
            "INSERT INTO future_price_revisions (revision_id, symbol, trade_date, close_price, "
            "source_ts, recorded_at) VALUES (:revision_id, :symbol, :trade_date, :close_price, "
            ":source_ts, :recorded_at)", row,
        )
        revision_ids.append(row["revision_id"])

    signal = {
        "signal_key": _uuid(), "link": "https://example.test/a", "available_at": AVAILABLE_AT,
        "scored_at": AVAILABLE_AT, "recorded_at": AVAILABLE_AT, "score": 8.0,
        "direction": direction, "aggregation_threshold": 7.0, "selected": 1,
        "is_similarity_copy": 0, "event_id": event_id, "event_version": 1,
        "event_mapping_id": mapping_id, "backtest_symbol": ASSET_SYMBOL,
    }
    signal.update(ledger or {})
    columns = ", ".join(signal)
    ledger_id = conn.execute(
        f"INSERT INTO backtest_signals ({columns}) VALUES ({', '.join(':' + k for k in signal)})",
        signal,
    ).lastrowid

    gross = CLOSES["asset_measured"] / CLOSES["asset_base"] - 1.0
    bench = CLOSES["bench_measured"] / CLOSES["bench_base"] - 1.0
    hit = direction_hit_for_return(direction, gross)
    row = {
        "outcome_revision_id": _uuid(), "outcome_id": _uuid(), "event_id": event_id,
        "security_id": asset_id, "mapping_id": mapping_id, "mapping_revision": 1,
        "signal_ledger_id": ledger_id, "signal_available_at": AVAILABLE_AT,
        "observation_state": "complete", "horizon": f"T+{horizon_days}",
        "baseline_at": _close_ts(base_date), "measured_at": _close_ts(measured_date),
        "baseline_price": CLOSES["asset_base"], "gross_return": gross,
        "benchmark_return": bench, "excess_return": gross - bench,
        "mfe": None, "mae": None, "direction_hit": None if hit is None else int(hit),
        "benchmark_security_id": MARKET_BENCHMARK_SYMBOL, "method_version": V4,
        "data_version": V4_PREFIX + ":".join(revision_ids), "data_quality": 1.0,
        "revision": 1, "is_current": 1, "created_at": _close_ts(measured_date) + 3600,
    }
    for key, value in (outcome or {}).items():
        row[key] = value(row) if callable(value) else value
    conn.execute(
        f"INSERT INTO event_outcomes ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})",
        row,
    )
    conn.commit()
    return row


@pytest.fixture
def conn():
    database.init_db()
    with database.get_connection() as connection:
        yield connection


def _validate(conn):
    before = conn.total_changes
    try:
        return migration._validate_stable_outcome_prices(conn)
    finally:
        assert conn.total_changes == before


def test_consistent_v4_outcome_validates(conn):
    _build(conn)
    assert _validate(conn) == 1


@pytest.mark.parametrize("horizon_days", [3, 20])
def test_longer_supported_horizons_validate(conn, horizon_days):
    _build(conn, horizon_days=horizon_days)
    assert _validate(conn) == 1


def test_neutral_direction_requires_null_hit(conn):
    _build(conn, direction="中性")
    assert _validate(conn) == 1


def _swap_uuid_case(row):
    prefix, ids = row["data_version"][: len(V4_PREFIX)], row["data_version"][len(V4_PREFIX):]
    return prefix + ids.upper()


def _drop_one_id(row):
    return row["data_version"].rsplit(":", 1)[0]


def _missing_revision(row):
    ids = row["data_version"][len(V4_PREFIX):].split(":")
    ids[-1] = _uuid()
    return V4_PREFIX + ":".join(ids)


PRICE_REFERENCE_CASES = [
    ("wrong prefix", {"data_version": lambda r: "future-prices-v2:" + r["data_version"][len(V4_PREFIX):]},
     "event_outcomes contains an invalid price data_version"),
    ("three revisions", {"data_version": _drop_one_id},
     "event_outcomes contains an invalid price data_version"),
    ("non-canonical uuid", {"data_version": _swap_uuid_case},
     "event_outcomes contains an invalid price data_version"),
    ("not a uuid", {"data_version": V4_PREFIX + "a:b:c:d"},
     "event_outcomes contains an invalid price data_version"),
    ("missing revision", {"data_version": _missing_revision},
     "event_outcomes references a missing future price revision"),
]


@pytest.mark.parametrize("case,outcome,message", PRICE_REFERENCE_CASES, ids=[c[0] for c in PRICE_REFERENCE_CASES])
def test_price_reference_corruption_is_rejected(conn, case, outcome, message):
    _build(conn, outcome=outcome)
    with pytest.raises(RuntimeError, match=f"^{message}$"):
        _validate(conn)


SCALAR = "v4 event_outcome violates its scalar contract"
LEDGER = "v4 event_outcome does not match its frozen signal ledger"
DATES = "v4 event_outcome uses invalid horizon dates"
ROLES = "v4 event_outcome price revisions have invalid roles"
TIMES = "v4 event_outcome timestamps do not match price revisions"
RETURNS = "v4 event_outcome returns do not match price revisions"
HIT = "v4 event_outcome direction result does not match its frozen signal"
ONE_DAY = 86_400

OUTCOME_CASES = [
    ("not complete", {"observation_state": "not_due"}, SCALAR),
    ("unsupported horizon", {"horizon": "T+2"}, SCALAR),
    ("wrong benchmark", {"benchmark_security_id": "sh990001"}, SCALAR),
    ("measured date shifted", {"measured_at": lambda r: r["measured_at"] + ONE_DAY}, DATES),
    ("baseline off by a minute", {"baseline_at": lambda r: r["baseline_at"] - 60}, TIMES),
    ("signal before ledger availability", {"signal_available_at": AVAILABLE_AT + 1}, LEDGER),
    ("baseline price", {"baseline_price": 10.5}, RETURNS),
    ("gross return", {"gross_return": lambda r: r["gross_return"] + 1e-6}, RETURNS),
    ("excess return", {"excess_return": lambda r: r["excess_return"] - 0.01}, RETURNS),
    ("flipped hit", {"direction_hit": 0}, HIT),
    ("hit missing", {"direction_hit": None}, HIT),
]


@pytest.mark.parametrize("case,outcome,message", OUTCOME_CASES, ids=[c[0] for c in OUTCOME_CASES])
def test_outcome_field_corruption_is_rejected(conn, case, outcome, message):
    _build(conn, outcome=outcome)
    with pytest.raises(RuntimeError, match=f"^{message}$"):
        _validate(conn)


LEDGER_CASES = [
    ("not selected", {"selected": 0}),
    ("similarity copy", {"is_similarity_copy": 1}),
    ("different availability", {"available_at": AVAILABLE_AT - 1}),
    ("available after baseline", {"available_at": AVAILABLE_AT + 30 * ONE_DAY}),
]


@pytest.mark.parametrize("case,ledger", LEDGER_CASES, ids=[c[0] for c in LEDGER_CASES])
def test_ledger_mismatch_is_rejected(conn, case, ledger):
    outcome = {"signal_available_at": ledger["available_at"]} if case == "available after baseline" else None
    _build(conn, ledger=ledger, outcome=outcome)
    with pytest.raises(RuntimeError, match=f"^{LEDGER}$"):
        _validate(conn)


REVISION_CASES = [
    ("benchmark symbol", {"bench_base": {"symbol": "sz399001"}}, ROLES),
    ("asset measured date", {"asset_measured": {"trade_date": "2026-09-30"}}, ROLES),
    ("asset base different symbol", {"asset_measured": {"symbol": "sh990002"}}, ROLES),
    ("source timestamp drift", {"asset_base": {"source_ts": 1}}, TIMES),
    ("non-positive close", {"bench_measured": {"close_price": 0.0}},
     "v4 event_outcome references invalid close prices"),
]


@pytest.mark.parametrize("case,revisions,message", REVISION_CASES, ids=[c[0] for c in REVISION_CASES])
def test_price_revision_corruption_is_rejected(conn, case, revisions, message):
    _build(conn, revisions=revisions)
    with pytest.raises(RuntimeError, match=f"^{message}$"):
        _validate(conn)


def test_legacy_outcome_with_fabricated_ledger_metadata_is_rejected(conn):
    _build(conn, outcome={
        "method_version": "legacy-close-v1", "data_version": "legacy",
        "observation_state": "complete",
    })
    with pytest.raises(
        RuntimeError, match="^legacy event_outcomes contains fabricated signal observation metadata$"
    ):
        _validate(conn)


def test_legacy_unknown_outcome_without_ledger_is_ignored(conn):
    _build(conn, outcome={
        "method_version": "legacy-close-v1", "data_version": "legacy",
        "observation_state": "legacy_unknown", "signal_ledger_id": None,
        "signal_available_at": None,
    })
    assert _validate(conn) == 0
