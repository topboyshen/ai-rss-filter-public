"""Bounded reads from the durable AI -> Quant integration outbox."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from db.database import db_lock, get_connection
from db.integration_outbox import (
    FIELD_BYTES,
    bounded_text,
    get_integration_outbox_state,
    is_exportable_score_path,
    stable_signal_revision_id,
    stable_text_hash,
)
from scoring_provenance import normalize_score_path


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
ITEMS_BUDGET_BYTES = 48 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


@dataclass
class CursorProblem(Exception):
    code: str
    status: int
    state: dict[str, Any]


class OutboxIntegrityError(RuntimeError):
    pass


_SIGNAL_REVISION_ID_RE = re.compile(r"[0-9a-f]{64}\Z")


def _uuid_field(value: object, field: str, *, required: bool = True) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise OutboxIntegrityError(f"outbox {field} is missing")
        return None
    try:
        return str(uuid.UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise OutboxIntegrityError(f"outbox {field} is not a UUID") from exc


def _integer_field(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise OutboxIntegrityError(f"invalid outbox {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OutboxIntegrityError(f"invalid outbox {field}") from exc
    if isinstance(value, float) and not value.is_integer():
        raise OutboxIntegrityError(f"invalid outbox {field}")
    return parsed


def _flag_field(value: object, field: str) -> int:
    parsed = _integer_field(value, field)
    if parsed not in {0, 1}:
        raise OutboxIntegrityError(f"invalid outbox {field}")
    return parsed


def _ledger_mismatch(field: str) -> OutboxIntegrityError:
    return OutboxIntegrityError(f"outbox {field} does not match signal ledger")


def _validate_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate one retained export row against its immutable ledger source.

    The outbox may legitimately outlive its source ledger row, so a missing
    ledger preserves the self-contained retained revision.  When the ledger is
    still present, every exported business field must match the canonical value
    used by the writer.  A damaged row fails the whole requested page; it is
    never filtered in a way that could advance a consumer past it.
    """
    outbox_score_path = normalize_score_path(row.get("score_path"))
    if not is_exportable_score_path(outbox_score_path):
        raise OutboxIntegrityError("outbox score_path is not exportable")
    if _flag_field(row.get("is_similarity_copy"), "is_similarity_copy"):
        raise OutboxIntegrityError("outbox similarity copy is not exportable")

    # Keep legacy retained rows usable after their source ledger ages out, but
    # canonicalize the path before exposing it on either wire version.
    row["score_path"] = outbox_score_path
    if row.get("authoritative_ledger_id") is None:
        return row

    ledger_score_path = normalize_score_path(row.get("ledger_score_path"))
    if not is_exportable_score_path(ledger_score_path):
        raise OutboxIntegrityError("signal ledger score_path is not exportable")
    if _flag_field(
        row.get("ledger_is_similarity_copy"), "ledger is_similarity_copy"
    ):
        raise OutboxIntegrityError("signal ledger similarity copy is not exportable")

    ledger_direction = bounded_text(
        row.get("ledger_direction"), FIELD_BYTES["direction"]
    )
    if ledger_direction not in {"看多", "看空", "中性"}:
        ledger_direction = "中性"
    ledger_event_version = _integer_field(
        row.get("ledger_event_version"), "ledger event_version"
    )
    if ledger_event_version < 1:
        raise OutboxIntegrityError("signal ledger event_version is invalid")

    expected_text = {
        "signal_revision_id": stable_signal_revision_id(
            row.get("ledger_signal_key")
        ),
        "domain_event_id": _uuid_field(
            row.get("ledger_domain_event_id"), "ledger event_id"
        ),
        "event_mapping_id": _uuid_field(
            row.get("ledger_event_mapping_id"),
            "ledger event_mapping_id",
            required=False,
        ),
        "direction": ledger_direction,
        "strategy_version": bounded_text(
            row.get("ledger_strategy_version"),
            FIELD_BYTES["strategy_version"],
        ),
        "mapping_version": bounded_text(
            row.get("ledger_mapping_version"),
            FIELD_BYTES["mapping_version"],
        ),
        "mapped_theme": bounded_text(
            row.get("ledger_mapped_theme"), FIELD_BYTES["mapped_theme"]
        ),
        "backtest_symbol": bounded_text(
            row.get("ledger_backtest_symbol"),
            FIELD_BYTES["backtest_symbol"],
        ) or None,
        "score_path": bounded_text(
            ledger_score_path, FIELD_BYTES["score_path"]
        ),
        "revision_key": bounded_text(
            row.get("ledger_revision_key") or "base",
            FIELD_BYTES["revision_key"],
        ),
        "signal_stage": bounded_text(
            row.get("ledger_signal_stage") or "base",
            FIELD_BYTES["signal_stage"],
        ),
        "revision_state_key": bounded_text(
            row.get("ledger_revision_state_key"),
            FIELD_BYTES["revision_state_key"],
        ),
        "independence_id": stable_text_hash(
            row.get("ledger_independence_key")
        ),
    }
    for field, expected in expected_text.items():
        if row.get(field) != expected:
            raise _ledger_mismatch(field)
        row[field] = expected

    expected_integers = {
        "event_version": ledger_event_version,
        "available_at": _integer_field(
            row.get("ledger_available_at"), "ledger available_at"
        ),
        "recorded_at": _integer_field(
            row.get("ledger_recorded_at"), "ledger recorded_at"
        ),
        "revision_seq": _integer_field(
            row.get("ledger_revision_seq"), "ledger revision_seq"
        ),
        "selected": _flag_field(row.get("ledger_selected"), "ledger selected"),
        "is_similarity_copy": 0,
    }
    for field, expected in expected_integers.items():
        if _integer_field(row.get(field), field) != expected:
            raise _ledger_mismatch(field)
        row[field] = expected

    expected_numbers = {
        "score": _finite_in_range(
            row.get("ledger_score"), "ledger score", -1.0, 10.0
        ),
        "score_confidence": _finite_in_range(
            row.get("ledger_score_confidence"),
            "ledger score_confidence",
            0.0,
            1.0,
        ),
        "mapping_confidence": _finite_in_range(
            row.get("ledger_mapping_confidence"),
            "ledger mapping_confidence",
            0.0,
            1.0,
        ),
        "mapping_relevance": _finite_in_range(
            row.get("ledger_mapping_relevance"),
            "ledger mapping_relevance",
            0.0,
            10.0,
        ),
        "aggregation_threshold": _finite_in_range(
            row.get("ledger_aggregation_threshold"),
            "ledger aggregation_threshold",
            0.0,
            10.0,
        ),
    }
    for field, expected in expected_numbers.items():
        if _finite(row.get(field), field) != expected:
            raise _ledger_mismatch(field)
        row[field] = expected

    expected_gap = _optional_finite_in_range(
        row.get("ledger_expected_gap"),
        "ledger expected_gap",
        -100.0,
        100.0,
    )
    actual_gap = _optional_finite_in_range(
        row.get("expected_gap"), "expected_gap", -100.0, 100.0
    )
    if actual_gap != expected_gap:
        raise _ledger_mismatch("expected_gap")
    row["expected_gap"] = expected_gap
    return row


def read_signal_revision_page(
    *,
    ledger_id: str | None,
    epoch: str | None,
    after_seq: int | None,
    limit: int,
    wire_version: int = 3,
) -> dict[str, Any]:
    """Read one contiguous prefix without advancing over an omitted row.

    A new consumer omits all three cursor fields and starts at the retained
    floor.  Every later request supplies the stream ``ledger_id``, ``epoch``
    and committed ``after_seq`` together.  ``seq`` is revision apply order;
    ``available_at`` remains event time only.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be in 1..{MAX_PAGE_SIZE}")
    if isinstance(wire_version, bool) or wire_version not in {3, 4}:
        raise ValueError("wire_version must be 3 or 4")
    cursor_parts = (ledger_id, epoch, after_seq)
    bootstrap = all(value is None for value in cursor_parts)
    if not bootstrap and any(value is None for value in cursor_parts):
        raise ValueError("ledger_id, epoch and after_seq must be supplied together")
    if after_seq is not None and (
        isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0
    ):
        raise ValueError("after_seq must be a non-negative integer")

    with db_lock:
        with get_connection() as conn:
            try:
                # API and Worker use different processes/containers, so the
                # Python lock cannot serialize their connections.  An explicit
                # deferred read transaction pins state metadata and page rows
                # to one WAL snapshot while still allowing the writer to commit.
                conn.execute("BEGIN")
                try:
                    state = get_integration_outbox_state(conn)
                except Exception as exc:
                    raise OutboxIntegrityError(str(exc)) from exc
                if bootstrap:
                    committed = int(state["floor_seq"]) - 1
                else:
                    if ledger_id != state["ledger_id"]:
                        raise CursorProblem("cursor_ledger_mismatch", 409, state)
                    if epoch != state["epoch"]:
                        raise CursorProblem("cursor_epoch_mismatch", 409, state)
                    committed = int(after_seq)
                if committed > int(state["last_seq"]):
                    # A cursor from a newer restored-over database must never be
                    # allowed to wait until sequence numbers are reused.
                    raise CursorProblem("cursor_ahead", 409, state)
                if committed < int(state["floor_seq"]) - 1:
                    raise CursorProblem("cursor_expired", 410, state)

                rows = conn.execute(
                    "SELECT outbox.seq, "
                    "outbox.event_id AS signal_revision_id, "
                    "outbox.domain_event_id, outbox.event_version, "
                    "outbox.event_mapping_id, outbox.available_at, "
                    "outbox.recorded_at, outbox.score, outbox.direction, "
                    "outbox.score_confidence, outbox.strategy_version, "
                    "outbox.mapping_version, outbox.mapped_theme, "
                    "outbox.backtest_symbol, outbox.score_path, "
                    "outbox.revision_key, outbox.signal_stage, "
                    "outbox.revision_seq, outbox.revision_state_key, "
                    "outbox.independence_id, outbox.aggregation_threshold, "
                    "outbox.selected, outbox.is_similarity_copy, "
                    "outbox.mapping_confidence, outbox.mapping_relevance, "
                    "outbox.expected_gap, outbox.ledger_signal_id, "
                    "ledger.id AS authoritative_ledger_id, "
                    "ledger.signal_key AS ledger_signal_key, "
                    "ledger.event_id AS ledger_domain_event_id, "
                    "ledger.event_version AS ledger_event_version, "
                    "ledger.event_mapping_id AS ledger_event_mapping_id, "
                    "ledger.available_at AS ledger_available_at, "
                    "ledger.recorded_at AS ledger_recorded_at, "
                    "ledger.score AS ledger_score, "
                    "ledger.direction AS ledger_direction, "
                    "ledger.score_confidence AS ledger_score_confidence, "
                    "ledger.strategy_version AS ledger_strategy_version, "
                    "ledger.mapping_version AS ledger_mapping_version, "
                    "ledger.mapped_theme AS ledger_mapped_theme, "
                    "ledger.backtest_symbol AS ledger_backtest_symbol, "
                    "ledger.score_path AS ledger_score_path, "
                    "ledger.revision_key AS ledger_revision_key, "
                    "ledger.signal_stage AS ledger_signal_stage, "
                    "ledger.revision_seq AS ledger_revision_seq, "
                    "ledger.revision_state_key AS ledger_revision_state_key, "
                    "ledger.independence_key AS ledger_independence_key, "
                    "ledger.aggregation_threshold "
                    "AS ledger_aggregation_threshold, "
                    "ledger.selected AS ledger_selected, "
                    "ledger.is_similarity_copy AS ledger_is_similarity_copy, "
                    "ledger.mapping_confidence AS ledger_mapping_confidence, "
                    "ledger.mapping_relevance AS ledger_mapping_relevance, "
                    "ledger.expected_gap AS ledger_expected_gap "
                    "FROM integration_signal_outbox AS outbox "
                    "LEFT JOIN backtest_signals AS ledger "
                    "ON ledger.id = outbox.ledger_signal_id "
                    "WHERE outbox.seq > ? "
                    "ORDER BY outbox.seq ASC LIMIT ?",
                    (committed, limit + 1),
                ).fetchall()
            finally:
                if conn.in_transaction:
                    conn.rollback()

    expected = committed + 1
    items: list[dict[str, Any]] = []
    used_bytes = 2
    candidate_rows = rows[:limit]
    validated_rows: list[dict[str, Any]] = []
    for raw_row in candidate_rows:
        row = dict(raw_row)
        seq = int(row.get("seq") or 0)
        if seq != expected:
            raise OutboxIntegrityError("integration outbox page is not contiguous")
        expected += 1
        validated_rows.append(_validate_export_row(row))

    for row in validated_rows:
        item = _wire_signal(row, wire_version=wire_version)
        encoded = json.dumps(
            item,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        cost = len(encoded) + (1 if items else 0)
        if cost + used_bytes > ITEMS_BUDGET_BYTES:
            if not items:
                raise OutboxIntegrityError("integration outbox item exceeds page budget")
            break
        items.append(item)
        used_bytes += cost

    if not rows and committed < int(state["last_seq"]):
        raise OutboxIntegrityError("integration outbox tail is missing")
    has_more = len(rows) > limit or len(items) < len(candidate_rows)
    next_after_seq = int(items[-1]["seq"]) if items else committed
    return {
        "state": state,
        "items": items,
        "after_seq": committed,
        "next_after_seq": next_after_seq,
        "has_more": has_more,
        "limit": limit,
    }


def _wire_signal(row: dict[str, Any], *, wire_version: int = 3) -> dict[str, Any]:
    raw_direction = bounded_text(row.get("direction"), FIELD_BYTES["direction"])
    direction = raw_direction if raw_direction in {"看多", "看空", "中性"} else "中性"
    signal_revision_id = bounded_text(
        row.get("signal_revision_id"), FIELD_BYTES["signal_revision_id"]
    )
    if not _SIGNAL_REVISION_ID_RE.fullmatch(signal_revision_id):
        raise OutboxIntegrityError("outbox signal_revision_id is invalid")
    event_id = _uuid_field(row.get("domain_event_id"), "event_id")
    try:
        event_version = int(row.get("event_version") or 0)
    except (TypeError, ValueError) as exc:
        raise OutboxIntegrityError("outbox event_version is invalid") from exc
    if event_version < 1:
        raise OutboxIntegrityError("outbox event_version is invalid")
    event_mapping_id = _uuid_field(
        row.get("event_mapping_id"), "event_mapping_id", required=False
    )
    score_value = _finite_in_range(row.get("score"), "score", -1.0, 10.0)
    item = {
        "seq": int(row["seq"]),
        "signal_revision_id": signal_revision_id,
        "event_id": event_id,
        "event_version": event_version,
        "event_mapping_id": event_mapping_id,
        "available_at": int(row.get("available_at") or 0),
        "score": score_value,
        "direction": direction,
        "score_confidence": _finite_in_range(
            row.get("score_confidence"), "score_confidence", 0.0, 1.0
        ),
        "strategy_version": bounded_text(
            row.get("strategy_version"), FIELD_BYTES["strategy_version"]
        ),
        "mapping_version": bounded_text(
            row.get("mapping_version"), FIELD_BYTES["mapping_version"]
        ),
        "mapped_theme": bounded_text(row.get("mapped_theme"), FIELD_BYTES["mapped_theme"]),
        "backtest_symbol": bounded_text(
            row.get("backtest_symbol"), FIELD_BYTES["backtest_symbol"]
        ) or None,
        "score_path": bounded_text(row.get("score_path"), FIELD_BYTES["score_path"]),
        "revision_key": bounded_text(
            row.get("revision_key"), FIELD_BYTES["revision_key"]
        ),
        "signal_stage": bounded_text(
            row.get("signal_stage"), FIELD_BYTES["signal_stage"]
        ),
        "revision_seq": int(row.get("revision_seq") or 0),
        "revision_state_key": bounded_text(
            row.get("revision_state_key"), FIELD_BYTES["revision_state_key"]
        ),
        "independence_id": bounded_text(row.get("independence_id"), 64),
        "aggregation_threshold": _finite_in_range(
            row.get("aggregation_threshold"),
            "aggregation_threshold",
            0.0,
            10.0,
        ),
        "selected": bool(row.get("selected")),
        "is_similarity_copy": bool(row.get("is_similarity_copy")),
    }
    if wire_version == 4:
        item.update(
            {
                # ``-1`` is the legacy parse-error sentinel, not an importance
                # value.  V4 keeps it in ``score`` for replay compatibility and
                # exposes importance as unavailable instead of violating 0..10.
                "importance_score": score_value if score_value >= 0 else None,
                "mapping_confidence": _finite_in_range(
                    row.get("mapping_confidence"),
                    "mapping_confidence",
                    0.0,
                    1.0,
                ),
                "mapping_relevance": _finite_in_range(
                    row.get("mapping_relevance"),
                    "mapping_relevance",
                    0.0,
                    10.0,
                ),
                "expected_gap": _optional_finite_in_range(
                    row.get("expected_gap"),
                    "expected_gap",
                    -100.0,
                    100.0,
                ),
            }
        )
    return item


def _finite(value: object, field: str) -> float:
    import math

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OutboxIntegrityError(f"invalid outbox {field}") from exc
    if not math.isfinite(parsed):
        raise OutboxIntegrityError(f"invalid outbox {field}")
    return parsed


def _finite_in_range(
    value: object,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    parsed = _finite(value, field)
    if not minimum <= parsed <= maximum:
        raise OutboxIntegrityError(f"outbox {field} is outside its contract")
    return parsed


def _optional_finite_in_range(
    value: object,
    field: str,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    return _finite_in_range(value, field, minimum, maximum)
