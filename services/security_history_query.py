"""Read-only stock history model joining stable events to versioned daily OHLC.

The reverse view deliberately starts from ``event_securities`` and resolves
merged events to their canonical UUID.  It never reclusters headlines and it
never invents an event from a price move.
"""

from __future__ import annotations

import base64
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from db.database import db_lock, get_connection
from services.event_outcomes import (
    OUTCOME_METHOD_VERSION,
    baseline_trade_date,
    tradable_baseline_trade_date,
)


_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})
_TICKER_RE = re.compile(r"[0-9]{6}\Z")
_PRICE_WINDOWS = frozenset({60, 120, 240})
_RELATION_LABELS = {
    "direct": "直接影响",
    "direct_impact": "直接影响",
    "affected": "直接影响",
    "upstream": "上游",
    "downstream": "下游",
    "ecosystem": "生态",
    "concept": "概念",
}
_RELATION_VALUES = tuple(_RELATION_LABELS)
_MAX_EVENTS = 100
_MAX_EVIDENCE_PER_EVENT = 3
_CURSOR_VERSION = 1
_MAX_CURSOR_BYTES = 384


class SecurityHistoryQueryError(ValueError):
    """The requested exchange, ticker, or window is malformed."""


class SecurityHistoryNotFoundError(LookupError):
    """The requested active A-share equity does not exist."""


class SecurityHistoryUnavailableError(RuntimeError):
    """The stock history read model could not be loaded consistently."""


def _parse_exchange(value: object) -> str:
    exchange = str(value or "").strip().upper()
    if exchange not in _EXCHANGES:
        raise SecurityHistoryQueryError("unsupported exchange")
    return exchange


def _parse_ticker(value: object) -> str:
    ticker = str(value or "").strip()
    if not _TICKER_RE.fullmatch(ticker):
        raise SecurityHistoryQueryError("ticker must be six digits")
    return ticker


def _parse_window(value: object) -> int:
    if value in (None, ""):
        return 120
    if isinstance(value, bool):
        raise SecurityHistoryQueryError("window must be 60, 120, or 240")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SecurityHistoryQueryError("window must be 60, 120, or 240") from exc
    if str(value).strip() != str(parsed) or parsed not in _PRICE_WINDOWS:
        raise SecurityHistoryQueryError("window must be 60, 120, or 240")
    return parsed


def _encode_cursor(event: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "occurred_at": int(event["occurred_at"]),
            "priority": float(event["priority"]),
            "event_id": str(event["event_id"]),
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _parse_cursor(value: object) -> tuple[int, float, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > _MAX_CURSOR_BYTES or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise SecurityHistoryQueryError("invalid event cursor")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        payload = json.loads(decoded.decode("utf-8"))
        event_id = str(uuid.UUID(str(payload["event_id"])))
        occurred_at = int(payload["occurred_at"])
        priority = float(payload["priority"])
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise SecurityHistoryQueryError("invalid event cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or occurred_at < 0
        or not math.isfinite(priority)
        or not 0 <= priority <= 10
    ):
        raise SecurityHistoryQueryError("invalid event cursor")
    return occurred_at, priority, event_id


def _quote_symbol(exchange: str, ticker: str) -> str:
    return {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}[exchange] + ticker


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_strings(value: object, *, maximum: int = 30) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:255])
        if len(result) >= maximum:
            break
    return result


def _safe_source_uri(value: object) -> str:
    raw = str(value or "").strip()
    if len(raw) > 2048:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return raw


def _event_date(timestamp: object) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp), _MARKET_TZ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _load_event_rows(
    conn: sqlite3.Connection,
    security_id: str,
    cursor: tuple[int, float, str] | None,
) -> tuple[list[dict[str, Any]], bool, str]:
    relation_placeholders = ",".join("?" for _ in _RELATION_VALUES)
    cursor_sql = ""
    cursor_params: tuple[object, ...] = ()
    if cursor is not None:
        occurred_at, priority, event_id = cursor
        cursor_sql = (
            " AND (occurred_at < ? OR (occurred_at = ? AND priority < ?) "
            "OR (occurred_at = ? AND priority = ? AND event_id < ?))"
        )
        cursor_params = (
            occurred_at,
            occurred_at,
            priority,
            occurred_at,
            priority,
            event_id,
        )
    rows = conn.execute(
        "WITH RECURSIVE mapping_chain(mapping_id, mapped_event_id, root_event_id, depth) AS ("
        " SELECT mapping_id, event_id, event_id, 0 FROM event_securities "
        " WHERE security_id = ? AND is_current = 1 "
        f" AND lower(relation_type) IN ({relation_placeholders})"
        " UNION ALL "
        " SELECT chain.mapping_id, chain.mapped_event_id, event.merged_into, chain.depth + 1 "
        " FROM mapping_chain AS chain JOIN events AS event "
        " ON event.event_id = chain.root_event_id "
        " WHERE event.merged_into IS NOT NULL AND chain.depth < 100"
        "), roots AS ("
        " SELECT mapping_id, mapped_event_id, root_event_id, depth, "
        " ROW_NUMBER() OVER (PARTITION BY mapping_id ORDER BY depth DESC) AS root_rank "
        " FROM mapping_chain"
        "), ranked AS ("
        " SELECT root.event_id, root.title, root.event_type, root.direction AS event_direction, "
        " root.priority, root.occurred_at, root.available_at, root.version AS event_version, "
        " mapping.mapping_id, mapping.event_id AS mapped_event_id, "
        " lower(mapping.relation_type) AS relation_type, mapping.direction, "
        " mapping.expected_gap, mapping.confidence, mapping.relevance, mapping.rationale, "
        " mapping.mapping_version, mapping.revision AS mapping_revision, mapping.created_at, "
        " ROW_NUMBER() OVER (PARTITION BY root.event_id ORDER BY mapping.relevance DESC, "
        " mapping.confidence DESC, mapping.created_at DESC, mapping.mapping_id DESC) AS event_rank "
        " FROM roots JOIN event_securities AS mapping ON mapping.mapping_id = roots.mapping_id "
        " JOIN events AS root ON root.event_id = roots.root_event_id "
        " WHERE roots.root_rank = 1 AND root.merge_state = 'active'"
        ") SELECT * FROM ranked WHERE event_rank = 1 "
        + cursor_sql
        + " ORDER BY occurred_at DESC, priority DESC, event_id DESC LIMIT ?",
        (security_id, *_RELATION_VALUES, *cursor_params, _MAX_EVENTS + 1),
    ).fetchall()
    truncated = len(rows) > _MAX_EVENTS
    events = [dict(row) for row in rows[:_MAX_EVENTS]]
    if not events:
        return [], truncated, ""

    mapping_ids = [str(item["mapping_id"]) for item in events]
    placeholders = ",".join("?" for _ in mapping_ids)
    evidence_rows = conn.execute(
        "WITH source_ranked AS ("
        " SELECT evidence.*, ROW_NUMBER() OVER (PARTITION BY evidence.mapping_id, "
        " evidence.source_uri ORDER BY evidence.reliability DESC, "
        " COALESCE(evidence.announcement_at, evidence.observed_at) DESC, "
        " evidence.evidence_id DESC) AS source_rank "
        " FROM event_evidence AS evidence "
        f" WHERE evidence.mapping_id IN ({placeholders})"
        "), ranked AS ("
        " SELECT source_ranked.*, ROW_NUMBER() OVER (PARTITION BY mapping_id "
        " ORDER BY reliability DESC, COALESCE(announcement_at, observed_at) DESC, "
        " evidence_id DESC) AS evidence_rank FROM source_ranked WHERE source_rank = 1"
        ") SELECT * FROM ranked WHERE evidence_rank <= ? "
        " ORDER BY mapping_id, evidence_rank",
        (*mapping_ids, _MAX_EVIDENCE_PER_EVENT),
    ).fetchall()
    evidence_by_mapping: dict[str, list[dict[str, Any]]] = {
        mapping_id: [] for mapping_id in mapping_ids
    }
    for row in evidence_rows:
        evidence_by_mapping.setdefault(str(row["mapping_id"]), []).append(
            {
                "claim_text": str(row["claim_text"] or "")[:4000],
                "excerpt": str(row["excerpt"] or "")[:8000],
                "source_uri": _safe_source_uri(row["source_uri"]),
                "source_name": str(row["source_name"] or "")[:255],
                "reliability": float(row["reliability"]),
                "announcement_at": row["announcement_at"],
                "observed_at": int(row["observed_at"]),
            }
        )

    event_ids = [str(item["event_id"]) for item in events]
    event_placeholders = ",".join("?" for _ in event_ids)
    outcome_rows = conn.execute(
        "WITH RECURSIVE outcome_chain(outcome_revision_id, root_event_id, depth) AS ("
        " SELECT outcome.outcome_revision_id, outcome.event_id, 0 FROM event_outcomes AS outcome "
        " WHERE outcome.security_id = ? AND outcome.is_current = 1 "
        " AND outcome.method_version = ? AND outcome.observation_state = 'complete'"
        " UNION ALL "
        " SELECT chain.outcome_revision_id, event.merged_into, chain.depth + 1 "
        " FROM outcome_chain AS chain JOIN events AS event "
        " ON event.event_id = chain.root_event_id "
        " WHERE event.merged_into IS NOT NULL AND chain.depth < 100"
        "), roots AS ("
        " SELECT outcome_revision_id, root_event_id, "
        " ROW_NUMBER() OVER (PARTITION BY outcome_revision_id ORDER BY depth DESC) AS root_rank "
        " FROM outcome_chain"
        "), ranked AS ("
        " SELECT roots.root_event_id, outcome.*, ROW_NUMBER() OVER ("
        " PARTITION BY roots.root_event_id, outcome.horizon "
        " ORDER BY outcome.measured_at DESC, outcome.revision DESC, "
        " outcome.outcome_revision_id DESC) AS horizon_rank "
        " FROM roots JOIN event_outcomes AS outcome "
        " ON outcome.outcome_revision_id = roots.outcome_revision_id "
        " WHERE roots.root_rank = 1"
        ") SELECT * FROM ranked WHERE horizon_rank = 1 "
        f" AND root_event_id IN ({event_placeholders}) "
        " ORDER BY root_event_id, CASE horizon WHEN 'T+1' THEN 1 WHEN 'T+3' THEN 3 "
        " WHEN 'T+5' THEN 5 WHEN 'T+20' THEN 20 ELSE 999 END",
        (security_id, OUTCOME_METHOD_VERSION, *event_ids),
    ).fetchall()
    outcomes_by_event: dict[str, list[dict[str, Any]]] = {
        event_id: [] for event_id in event_ids
    }
    for row in outcome_rows:
        outcomes_by_event.setdefault(str(row["root_event_id"]), []).append(
            {
                "horizon": str(row["horizon"]),
                "gross_return": _finite(row["gross_return"]),
                "benchmark_return": _finite(row["benchmark_return"]),
                "excess_return": _finite(row["excess_return"]),
                "direction_hit": row["direction_hit"],
                "baseline_at": int(row["baseline_at"]),
                "measured_at": int(row["measured_at"]),
            }
        )

    for number, event in enumerate(events, start=1):
        event_id = str(event["event_id"])
        relation_type = str(event["relation_type"])
        event["history_number"] = number
        event["relation_label"] = _RELATION_LABELS.get(relation_type, relation_type)
        event["expected_gap"] = _finite(event["expected_gap"])
        event["mapping_confidence"] = float(event["confidence"])
        event["evidence"] = evidence_by_mapping.get(str(event["mapping_id"]), [])
        event["outcomes"] = outcomes_by_event.get(event_id, [])
        event["event_date"] = _event_date(event["available_at"])
        event["baseline_trade_date"] = baseline_trade_date(int(event["available_at"]))
        event["tradable_trade_date"] = tradable_baseline_trade_date(
            int(event["available_at"])
        )
    next_cursor = _encode_cursor(events[-1]) if truncated else ""
    return events, truncated, next_cursor


def load_security_history(
    exchange: object,
    ticker: object,
    *,
    window: object = 120,
    cursor: object = None,
) -> dict[str, Any]:
    """Return one consistent security, OHLC, and reverse-event snapshot."""
    parsed_exchange = _parse_exchange(exchange)
    parsed_ticker = _parse_ticker(ticker)
    parsed_window = _parse_window(window)
    parsed_cursor = _parse_cursor(cursor)
    symbol = _quote_symbol(parsed_exchange, parsed_ticker)
    try:
        with db_lock:
            with get_connection() as conn:
                conn.execute("BEGIN")
                try:
                    security_row = conn.execute(
                        "SELECT * FROM securities WHERE upper(exchange) = ? AND ticker = ? "
                        "AND lower(asset_type) = 'equity' AND is_active = 1 LIMIT 1",
                        (parsed_exchange, parsed_ticker),
                    ).fetchone()
                    if security_row is None:
                        raise SecurityHistoryNotFoundError("security not found")
                    security = dict(security_row)
                    price_rows = conn.execute(
                        "SELECT symbol, trade_date, name, close_price, change_amt, change_pct, "
                        "open_price, high_price, low_price, prev_close, volume, amount, "
                        "suspension_state, limit_state, tradability_state, ts, revision_id "
                        "FROM future_prices WHERE lower(symbol) = ? "
                        "ORDER BY trade_date DESC LIMIT ?",
                        (symbol, parsed_window),
                    ).fetchall()
                    events, events_truncated, next_cursor = _load_event_rows(
                        conn, str(security["security_id"]), parsed_cursor
                    )
                finally:
                    if conn.in_transaction:
                        conn.rollback()
    except (SecurityHistoryQueryError, SecurityHistoryNotFoundError):
        raise
    except sqlite3.Error as exc:
        raise SecurityHistoryUnavailableError("security history unavailable") from exc

    prices: list[dict[str, Any]] = []
    for raw in reversed(price_rows):
        row = dict(raw)
        for key in (
            "close_price",
            "change_amt",
            "change_pct",
            "open_price",
            "high_price",
            "low_price",
            "prev_close",
            "volume",
            "amount",
        ):
            row[key] = _finite(row.get(key))
        prices.append(row)

    dates = {str(row["trade_date"]) for row in prices}
    first_date = str(prices[0]["trade_date"]) if prices else ""
    last_date = str(prices[-1]["trade_date"]) if prices else ""
    for event in events:
        marker_date = str(event.get("tradable_trade_date") or "")
        if marker_date in dates:
            marker_state = "on_chart"
        elif not marker_date:
            marker_state = "unknown"
        elif last_date and marker_date > last_date:
            marker_state = "not_due"
        elif first_date and marker_date < first_date:
            marker_state = "outside_window"
        else:
            marker_state = "missing_price"
        event["marker_state"] = marker_state

    security["aliases"] = _json_strings(security.get("aliases"))
    security["concepts"] = _json_strings(security.get("concepts"))
    security["quote_symbol"] = symbol
    return {
        "security": security,
        "prices": prices,
        "price_window": parsed_window,
        "events": events,
        "events_truncated": events_truncated,
        "event_cursor": str(cursor or ""),
        "next_event_cursor": next_cursor,
        "price_start_date": first_date,
        "price_end_date": last_date,
    }


__all__ = [
    "SecurityHistoryNotFoundError",
    "SecurityHistoryQueryError",
    "SecurityHistoryUnavailableError",
    "load_security_history",
]
