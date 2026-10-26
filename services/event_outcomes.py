"""Materialize auditable event outcomes from versioned A-share daily OHLC.

The event-study baseline remains the first close strictly after signal
availability.  Daily open/high/low and explicit suspension/limit states are
retained in the immutable price revision, so the UI can disclose a separate
tradable-baseline candidate without pretending the close was an executable
fill.
"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from db import database
from db.event_core import upsert_event_outcome, upsert_security
from scoring_provenance import (
    FORMAL_SIGNAL_SCORE_PATHS,
    normalize_score_path,
)


_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_OPEN = datetime_time(9, 30)
_MARKET_CLOSE = datetime_time(15, 0)

LEGACY_OUTCOME_METHOD_VERSION = "a-share-event-ohlc-v4"
OUTCOME_METHOD_VERSION = "a-share-event-ohlc-v5-frozen-mapping"
OUTCOME_HORIZON_DAYS = (1, 3, 5, 20)
PENDING_QUOTE_LOOKBACK_DAYS = 120
MARKET_BENCHMARK_SYMBOL = "sh000300"
# Compatibility export used by older readers.  V5 accepts only exact equity
# mapping revisions frozen beside a signal ledger row.
OUTCOME_PROXY_SYMBOLS = frozenset(
    {"sh000001", "sh000300", "sz399001", "sz399006"}
)
LEGACY_OUTCOME_DATA_VERSION_PREFIX = "future-prices-event-ohlc-v4:"
OUTCOME_DATA_VERSION_PREFIX = "future-prices-event-ohlc-v5-frozen-mapping:"
_FORMAL_RELATIONS = frozenset({"direct", "upstream", "downstream", "ecosystem"})


def baseline_trade_date(available_at: int) -> str | None:
    if isinstance(available_at, bool):
        return None
    try:
        timestamp = int(available_at)
        observed = datetime.fromtimestamp(timestamp, _MARKET_TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    day = observed.date()
    for _ in range(370):
        value = day.isoformat()
        try:
            if database._is_trading_day_local(value):
                close_at = datetime.combine(day, _MARKET_CLOSE, tzinfo=_MARKET_TZ)
                if close_at.timestamp() > timestamp:
                    return value
        except ValueError:
            return None
        day += timedelta(days=1)
    return None


def tradable_baseline_trade_date(available_at: int) -> str | None:
    """Return the first session open occurring strictly after availability."""
    try:
        timestamp = int(available_at)
        observed = datetime.fromtimestamp(timestamp, _MARKET_TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    day = observed.date()
    for _ in range(370):
        value = day.isoformat()
        try:
            if database._is_trading_day_local(value):
                open_at = datetime.combine(day, _MARKET_OPEN, tzinfo=_MARKET_TZ)
                if open_at.timestamp() > timestamp:
                    return value
        except ValueError:
            return None
        day += timedelta(days=1)
    return None


def horizon_trade_date(baseline_date: str, offset: int) -> str | None:
    if isinstance(offset, bool) or offset not in OUTCOME_HORIZON_DAYS:
        raise ValueError(
            "outcome horizon must be one of "
            + ", ".join(str(value) for value in OUTCOME_HORIZON_DAYS)
        )
    try:
        return database._trading_day_after(str(baseline_date), offset)
    except (TypeError, ValueError):
        return None


def is_current_outcome_contract(
    *,
    security_id: object,
    method_version: object,
    horizon: object,
    benchmark_security_id: object,
    data_version: object,
    signal_available_at: object,
    baseline_at: object,
) -> bool:
    if str(method_version or "") != OUTCOME_METHOD_VERSION:
        return False
    if not str(security_id or "").strip():
        return False
    if str(horizon or "") not in {f"T+{offset}" for offset in OUTCOME_HORIZON_DAYS}:
        return False
    if str(benchmark_security_id or "").casefold() != MARKET_BENCHMARK_SYMBOL:
        return False
    raw_version = str(data_version or "")
    if not raw_version.startswith(OUTCOME_DATA_VERSION_PREFIX):
        return False
    revision_ids = raw_version[len(OUTCOME_DATA_VERSION_PREFIX) :].split(":")
    if len(revision_ids) != 4:
        return False
    try:
        if [str(uuid.UUID(value)) for value in revision_ids] != revision_ids:
            return False
        signal_at = int(signal_available_at)
        baseline = int(baseline_at)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False
    return signal_at > 0 and baseline > signal_at


def _market_timestamp(trade_date: str, value: datetime_time) -> int:
    return int(
        datetime.combine(
            datetime.strptime(trade_date, "%Y-%m-%d").date(),
            value,
            tzinfo=_MARKET_TZ,
        ).timestamp()
    )


def _timestamp_for_price(row: dict[str, Any]) -> int:
    raw = int(row.get("ts") or 0)
    return raw if raw > 0 else _market_timestamp(str(row["trade_date"]), _MARKET_CLOSE)


def is_completed_close_price(
    row: Any, trade_date: str, *, evaluation_at: Any, timestamp_key: str = "ts",
) -> bool:
    """Require an observed, finite close on the exact day, never a synthetic 15:00."""
    try:
        values = (row["close_price"], row[timestamp_key], evaluation_at)
        if any(isinstance(value, bool) for value in values):
            return False
        close, source_timestamp, evaluated = (float(value) for value in values)
        if (
            not all(math.isfinite(value) for value in (close, source_timestamp, evaluated))
            or close <= 0 or source_timestamp <= 0 or source_timestamp > evaluated
        ):
            return False
        if "trade_date" in row.keys() and row["trade_date"] != trade_date:
            return False
        observed = datetime.fromtimestamp(source_timestamp, _MARKET_TZ)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OSError, OverflowError):
        return False
    return observed.date().isoformat() == trade_date and observed.time() >= _MARKET_CLOSE


def _quote_symbol(exchange: object, ticker: object, security_id: object) -> str:
    exchange_value = str(exchange or "").upper()
    ticker_value = str(ticker or "")
    prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange_value)
    if prefix and len(ticker_value) == 6 and ticker_value.isdigit():
        return prefix + ticker_value
    legacy = str(security_id or "").casefold()
    return legacy if legacy in OUTCOME_PROXY_SYMBOLS else ""


def direction_hit_for_return(direction: str, gross_return: float) -> bool | None:
    value = str(direction or "中性").strip()
    if value in {"看多", "bullish", "positive", "up"}:
        return gross_return > 0
    if value in {"看空", "bearish", "negative", "down"}:
        return gross_return < 0
    return None


def normalize_mapping_relation(value: object) -> str:
    relation = str(value or "").strip().casefold()
    if relation in {"direct", "direct_impact", "affected"}:
        return "direct"
    if relation in {"upstream", "downstream", "ecosystem", "concept"}:
        return relation
    return ""


def _canonical_roots(event_rows: list[Any]) -> dict[str, str]:
    parents = {
        str(row["event_id"]): (
            None if row["merged_into"] is None else str(row["merged_into"])
        )
        for row in event_rows
    }

    def root(event_id: str) -> str:
        seen = set()
        current = event_id
        while parents.get(current):
            if current in seen:
                return event_id
            seen.add(current)
            current = str(parents[current])
        return current

    return {event_id: root(event_id) for event_id in parents}


def _canonical_uuid(value: object) -> bool:
    try:
        text = str(value or "")
        return bool(text) and str(uuid.UUID(text)) == text
    except (AttributeError, TypeError, ValueError):
        return False


def _read_frozen_mapping_snapshot(
    conn: Any, *, available_since: int | None = None,
    available_through: int | None = None,
) -> tuple[list[Any], list[Any], Any]:
    # Discover recent event contracts, but keep their complete ledger families.
    # An older valid anchor for the same frozen mapping must win over a recent
    # replay, and a later withdrawal must never resurrect an earlier revision.
    where = (
        " WHERE (link, strategy_version, mapping_version) IN ("
        "SELECT link, strategy_version, mapping_version FROM backtest_signals "
        "WHERE event_id IN (SELECT event_id FROM backtest_signals "
        "WHERE available_at >= ?))"
        if available_since is not None else ""
    )
    parameters = [available_since] if available_since is not None else []
    # Probe newer revisions through the existing link/revision index instead
    # of sorting every full ledger row with ROW_NUMBER. The newer probe must
    # not filter by date or selected state: a later withdrawal still hides an
    # earlier selected revision, even for a historical target date.
    signal_where = where.replace(
        "(link, strategy_version, mapping_version) IN",
        "(signal.link, signal.strategy_version, signal.mapping_version) IN",
        1,
    ).replace(" WHERE ", " AND ", 1)
    through = " AND signal.available_at < ? " if available_through is not None else " "
    signal_mappings = conn.execute(
        "SELECT signal.id AS ledger_signal_id, signal.event_id AS signal_event_id, "
        "signal.event_version, signal.available_at, signal.recorded_at, signal.selected, "
        "signal.is_similarity_copy, signal.score_path, "
        "signal.direction AS signal_direction, "
        "signal.strategy_version, signal.mapping_version AS signal_mapping_version, "
        "frozen.mapping_id AS frozen_mapping_id, "
        "frozen.mapping_revision AS frozen_mapping_revision, "
        "frozen.event_id AS frozen_event_id, frozen.security_id AS frozen_security_id, "
        "frozen.signal_available_at AS frozen_available_at, frozen.captured_at, "
        "mapping.event_id AS mapping_event_id, mapping.security_id, "
        "mapping.direction AS mapping_direction, mapping.revision, "
        "mapping.relation_type, mapping.relevance, mapping.mapping_version, "
        "mapping.created_at AS mapping_created_at, "
        "security.exchange, security.ticker, security.asset_type "
        "FROM backtest_signals AS signal "
        "LEFT JOIN backtest_signal_securities AS frozen "
        "ON frozen.ledger_signal_id = signal.id "
        "LEFT JOIN event_securities AS mapping "
        "ON mapping.mapping_id = frozen.mapping_id "
        "AND mapping.event_id = frozen.event_id "
        "AND mapping.security_id = frozen.security_id "
        "AND mapping.revision = frozen.mapping_revision "
        "LEFT JOIN securities AS security ON security.security_id = frozen.security_id "
        "WHERE signal.selected = 1 AND signal.is_similarity_copy = 0"
        + signal_where +
        " AND NOT EXISTS (SELECT 1 FROM backtest_signals AS newer "
        "WHERE newer.strategy_version = signal.strategy_version "
        "AND newer.link = signal.link "
        "AND newer.mapping_version = signal.mapping_version "
        "AND (newer.available_at, newer.id) > (signal.available_at, signal.id))"
        + through +
        "ORDER BY signal.available_at, signal.id, frozen.mapping_id",
        parameters + ([available_through] if available_through is not None else []),
    ).fetchall()
    # Only the relevant events and their ancestors are needed for bounded reads.
    events = conn.execute(
        "WITH RECURSIVE relevant(event_id, merged_into) AS ("
        "SELECT event_id, merged_into FROM events WHERE event_id IN ("
        "SELECT event_id FROM backtest_signals" + where + ") UNION "
        "SELECT parent.event_id, parent.merged_into FROM events AS parent "
        "JOIN relevant AS child ON parent.event_id = child.merged_into) "
        "SELECT event_id, merged_into FROM relevant",
        parameters,
    ).fetchall()
    marker = conn.execute(
        "SELECT v FROM app_meta WHERE k = 'mapping_validation_started_at'"
    ).fetchone()
    return signal_mappings, events, marker


def _eligible_frozen_mappings(
    signal_mappings: list[Any], events: list[Any], marker: Any,
) -> list[dict[str, Any]]:
    """The shared V5 prospective eligibility contract (including retired rows)."""

    if marker is None:
        return []
    try:
        started_at = int(str(marker[0]))
    except (TypeError, ValueError):
        return []
    roots = _canonical_roots(events)
    eligible_rows: list[dict[str, Any]] = []
    for raw in signal_mappings:
        row = dict(raw)
        if (
            not bool(row.get("selected"))
            or bool(row.get("is_similarity_copy"))
            or normalize_score_path(row.get("score_path"))
            not in FORMAL_SIGNAL_SCORE_PATHS
            or not str(row.get("signal_event_id") or "")
            or int(row.get("event_version") or 0) < 1
            or int(row.get("available_at") or 0) < started_at
        ):
            continue
        eligible_rows.append(row)
    if not eligible_rows:
        return []

    frozen_pairs: dict[
        tuple[str, str, str, str, str, str, int], dict[str, Any]
    ] = {}
    for row in eligible_rows:
        relation = normalize_mapping_relation(row.get("relation_type"))
        event_id = str(row.get("mapping_event_id") or "")
        if (
            not row.get("frozen_mapping_id")
            or relation not in _FORMAL_RELATIONS
            or str(row.get("asset_type") or "").casefold() != "equity"
            or int(row.get("frozen_available_at") or 0) != int(row["available_at"])
            or int(row.get("captured_at") or 0) != int(row.get("recorded_at") or 0)
            or int(row.get("mapping_created_at") or 0) > int(row.get("captured_at") or 0)
            or int(row.get("frozen_mapping_revision") or 0) != int(row.get("revision") or 0)
            or str(row.get("mapping_version") or "")
            != str(row.get("signal_mapping_version") or "")
            or event_id not in roots
            or not _canonical_uuid(event_id)
            or str(row.get("signal_event_id") or "") != event_id
            or str(row.get("frozen_event_id") or "") != event_id
            or str(row.get("frozen_security_id") or "") != str(row.get("security_id") or "")
        ):
            continue
        root = roots.get(event_id, event_id)
        row["root_event_id"] = root
        row["normalized_relation"] = relation
        row["price_symbol"] = _quote_symbol(
            row.get("exchange"), row.get("ticker"), row.get("security_id")
        )
        if not row["price_symbol"]:
            continue
        pair_key = (
            str(row.get("strategy_version") or ""),
            str(row.get("signal_mapping_version") or ""),
            root,
            str(row["security_id"]),
            relation,
            str(row["frozen_mapping_id"]),
            int(row["frozen_mapping_revision"]),
        )
        previous = frozen_pairs.get(pair_key)
        if previous is None or (
            int(row["available_at"]), int(row["ledger_signal_id"])
        ) < (
            int(previous["available_at"]), int(previous["ledger_signal_id"])
        ):
            frozen_pairs[pair_key] = row

    return list(frozen_pairs.values())


@contextmanager
def _pending_quote_read_only_connection(db_path: str | Path | None):
    if db_path is None:
        with database.get_read_only_connection() as conn:
            yield conn
        return
    conn = sqlite3.connect(
        Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RuntimeError("SQLite query-only enforcement is unavailable")
        yield conn
    finally:
        conn.close()


def get_pending_quote_requirements(
    target_date: str, *, start_date: str | None = None, limit: int = 500,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return bounded missing exact-date prices for the frozen V5 contracts.

    Discover signal contracts from the preceding 120 calendar days, not the
    latest mapped stock universe. Older rows of those contracts are read only to
    preserve their true original anchor; anchors older than 120 days are omitted.
    A retired mapping remains eligible when its frozen revision
    still passes the same prospective rules as outcome materialization.  Return
    newest dates first, then symbols ascending; ``total`` and ``truncated`` expose
    demand beyond the bounded result.  ``start_date=target_date`` selects only
    today's due prices for the real-time collector.  This is read-only and never
    substitutes a nearby trading day or changes a historical signal. An explicit
    ``db_path`` reads that snapshot without changing the application's database.
    """
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    if target.isoformat() != target_date:
        raise ValueError("target_date must be YYYY-MM-DD")
    earliest = target - timedelta(days=PENDING_QUOTE_LOOKBACK_DAYS)
    if start_date is not None:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        if start.isoformat() != start_date or start > target:
            raise ValueError("start_date must be YYYY-MM-DD at or before target_date")
        earliest_price = max(start, earliest).isoformat()
    else:
        earliest_price = earliest.isoformat()
    available_since = _market_timestamp(earliest.isoformat(), datetime_time())
    bounded_limit = max(1, min(5_000, int(limit)))
    empty = {"pairs": [], "total": 0, "truncated": False}
    with database.db_lock:
        with _pending_quote_read_only_connection(db_path) as conn:
            conn.execute("BEGIN")
            try:
                snapshot = _read_frozen_mapping_snapshot(
                    conn,
                    available_since=available_since,
                    available_through=_market_timestamp(
                        (target + timedelta(days=1)).isoformat(), datetime_time(),
                    ),
                )
                needed: set[tuple[str, str]] = set()
                dates_by_baseline: dict[str, list[str | None]] = {}
                for mapping in _eligible_frozen_mappings(*snapshot):
                    if int(mapping["available_at"]) < available_since:
                        continue
                    baseline = baseline_trade_date(int(mapping["available_at"]))
                    if baseline is None:
                        continue
                    if baseline not in dates_by_baseline:
                        dates_by_baseline[baseline] = [baseline] + [
                            horizon_trade_date(baseline, offset)
                            for offset in OUTCOME_HORIZON_DAYS
                        ]
                    for trade_date in dates_by_baseline[baseline]:
                        if trade_date is None or not earliest_price <= trade_date <= target_date:
                            continue
                        needed.add((str(mapping["price_symbol"]), trade_date))
                        needed.add((MARKET_BENCHMARK_SYMBOL, trade_date))
                # Fetch only exact requested pairs, batched below SQLite's old
                # 999-parameter limit.  No per-signal reads or full price scan.
                ordered = sorted(needed)
                present: set[tuple[str, str]] = set()
                for offset in range(0, len(ordered), 400):
                    batch = ordered[offset:offset + 400]
                    placeholders = ",".join("(?, ?)" for _ in batch)
                    rows = conn.execute(
                        "WITH requested(symbol, trade_date) AS (VALUES "
                        + placeholders + ") SELECT price.symbol, price.trade_date, "
                        "price.close_price, price.ts, price.revision_id, "
                        "revision.revision_id AS verified_revision "
                        "FROM requested JOIN future_prices AS price "
                        "ON price.symbol = requested.symbol "
                        "AND price.trade_date = requested.trade_date "
                        "LEFT JOIN future_price_revisions AS revision "
                        "ON revision.revision_id = price.revision_id "
                        "AND revision.symbol = price.symbol "
                        "AND revision.trade_date = price.trade_date "
                        "AND revision.close_price = price.close_price "
                        "AND revision.source_ts = price.ts",
                        [value for pair in batch for value in pair],
                    ).fetchall()
                    for row in rows:
                        try:
                            close = float(row["close_price"])
                            observed = datetime.fromtimestamp(int(row["ts"]), _MARKET_TZ)
                        except (TypeError, ValueError, OverflowError, OSError):
                            continue
                        if (
                            math.isfinite(close) and close > 0
                            and observed.date().isoformat() == row["trade_date"]
                            and observed.time() >= _MARKET_CLOSE
                            and _canonical_uuid(row["revision_id"])
                            and row["verified_revision"] == row["revision_id"]
                        ):
                            present.add((row["symbol"], row["trade_date"]))
            except sqlite3.OperationalError as exc:
                # Match the existing collector's empty legacy event-core
                # universe; malformed columns and all other errors still raise.
                if str(exc).casefold() != "no such table: backtest_signals":
                    raise
                return empty
            finally:
                conn.rollback()
    missing = sorted(needed - present, key=lambda pair: pair[0])
    missing.sort(key=lambda pair: pair[1], reverse=True)
    return {
        "pairs": [
            {"symbol": symbol, "trade_date": trade_date}
            for symbol, trade_date in missing[:bounded_limit]
        ],
        "total": len(missing),
        "truncated": len(missing) > bounded_limit,
    }


def _read_candidates(target_date: str | None) -> list[dict[str, Any]]:
    """Read only exact, prospective signal-to-mapping snapshots for V5."""
    evaluation_at = time.time()
    with database.db_lock:
        with database.get_connection() as conn:
            conn.execute("BEGIN")
            try:
                snapshot = _read_frozen_mapping_snapshot(conn)
                price_rows = conn.execute(
                    "SELECT symbol, trade_date, close_price, open_price, high_price, "
                    "low_price, suspension_state, limit_state, tradability_state, "
                    "ts, revision_id FROM future_prices "
                    + ("WHERE trade_date <= ? " if target_date else "")
                    + "ORDER BY trade_date, symbol",
                    (target_date,) if target_date else (),
                ).fetchall()
            finally:
                conn.rollback()
    frozen_mappings = _eligible_frozen_mappings(*snapshot)

    prices = {
        (str(row["symbol"]).casefold(), str(row["trade_date"])): dict(row)
        for row in price_rows
        if is_completed_close_price(row, str(row["trade_date"]), evaluation_at=evaluation_at)
    }
    calculations: list[dict[str, Any]] = []
    for mapping in frozen_mappings:
        available_at = int(mapping["available_at"])
        baseline_date = baseline_trade_date(available_at)
        if baseline_date is None:
            continue
        benchmark_baseline = prices.get((MARKET_BENCHMARK_SYMBOL, baseline_date))
        if benchmark_baseline is None:
            continue
        benchmark_baseline_price = float(benchmark_baseline["close_price"])
        symbol = str(mapping["price_symbol"]).casefold()
        baseline = prices.get((symbol, baseline_date))
        if baseline is None:
            continue
        baseline_price = float(baseline["close_price"])
        baseline_at = _timestamp_for_price(baseline)
        if baseline_at <= available_at:
            continue
        for offset in OUTCOME_HORIZON_DAYS:
            measured_date = horizon_trade_date(baseline_date, offset)
            if measured_date is None or (target_date and measured_date > target_date):
                continue
            measured = prices.get((symbol, measured_date))
            benchmark_measured = prices.get((MARKET_BENCHMARK_SYMBOL, measured_date))
            if measured is None or benchmark_measured is None:
                continue
            measured_price = float(measured["close_price"])
            benchmark_price = float(benchmark_measured["close_price"])
            gross_return = measured_price / baseline_price - 1.0
            benchmark_return = benchmark_price / benchmark_baseline_price - 1.0
            revisions = (
                str(baseline.get("revision_id") or ""),
                str(measured.get("revision_id") or ""),
                str(benchmark_baseline.get("revision_id") or ""),
                str(benchmark_measured.get("revision_id") or ""),
            )
            if any(not value for value in revisions):
                continue
            direction = str(
                mapping.get("mapping_direction")
                or mapping.get("signal_direction")
                or "中性"
            )
            calculations.append(
                {
                    "event_id": str(mapping["mapping_event_id"]),
                    "security_id": str(mapping["security_id"]),
                    "mapping_id": str(mapping["frozen_mapping_id"]),
                    "signal_ledger_id": int(mapping["ledger_signal_id"]),
                    "signal_available_at": available_at,
                    "horizon": f"T+{offset}",
                    "baseline_at": baseline_at,
                    "measured_at": _timestamp_for_price(measured),
                    "baseline_price": baseline_price,
                    "gross_return": gross_return,
                    "benchmark_return": benchmark_return,
                    "excess_return": gross_return - benchmark_return,
                    # V5 freezes endpoint revisions only; path excursions are
                    # intentionally withheld until every path revision is frozen.
                    "mfe": None,
                    "mae": None,
                    "direction_hit": direction_hit_for_return(direction, gross_return),
                    "data_version": OUTCOME_DATA_VERSION_PREFIX + ":".join(revisions),
                    "data_quality": 1.0,
                }
            )
    return calculations


def materialize_event_outcomes(target_date: str | None = None) -> dict[str, int]:
    calculations = _read_candidates(target_date)
    if not calculations:
        return {"eligible": 0, "materialized": 0}
    outcome_ids: set[str] = set()
    with database.db_lock:
        with database.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            benchmark_security_id = upsert_security(
                conn,
                exchange="SSE",
                ticker="000300",
                name="沪深300",
                security_id=MARKET_BENCHMARK_SYMBOL,
                asset_type="index",
                currency="CNY",
                aliases=[MARKET_BENCHMARK_SYMBOL, "000300"],
                industry="市场指数",
                concepts=[],
                now=int(time.time()),
            )
            for item in calculations:
                outcome_ids.add(
                    upsert_event_outcome(
                        conn,
                        item["event_id"],
                        item["security_id"],
                        mapping_id=item["mapping_id"],
                        horizon=item["horizon"],
                        baseline_at=item["baseline_at"],
                        measured_at=item["measured_at"],
                        baseline_price=item["baseline_price"],
                        gross_return=item["gross_return"],
                        benchmark_return=item["benchmark_return"],
                        excess_return=item["excess_return"],
                        mfe=item["mfe"],
                        mae=item["mae"],
                        direction_hit=item["direction_hit"],
                        benchmark_security_id=benchmark_security_id,
                        signal_ledger_id=item["signal_ledger_id"],
                        signal_available_at=item["signal_available_at"],
                        observation_state="complete",
                        method_version=OUTCOME_METHOD_VERSION,
                        data_version=item["data_version"],
                        data_quality=item["data_quality"],
                    )
                )
            conn.commit()
    return {"eligible": len(calculations), "materialized": len(outcome_ids)}


__all__ = [
    "LEGACY_OUTCOME_DATA_VERSION_PREFIX",
    "LEGACY_OUTCOME_METHOD_VERSION",
    "MARKET_BENCHMARK_SYMBOL",
    "OUTCOME_DATA_VERSION_PREFIX",
    "OUTCOME_HORIZON_DAYS",
    "OUTCOME_METHOD_VERSION",
    "OUTCOME_PROXY_SYMBOLS",
    "baseline_trade_date",
    "direction_hit_for_return",
    "get_pending_quote_requirements",
    "horizon_trade_date",
    "is_completed_close_price",
    "is_current_outcome_contract",
    "materialize_event_outcomes",
    "normalize_mapping_relation",
    "tradable_baseline_trade_date",
]
