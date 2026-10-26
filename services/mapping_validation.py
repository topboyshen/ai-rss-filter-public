"""Prospective validity audit for event-to-A-share equity mappings.

Only signal/mapping pairs captured by ``backtest_signal_securities`` and V5
endpoint-price outcomes are eligible.  Older outcomes remain useful in event
detail views, but are intentionally excluded because their real-equity mapping
set was not frozen at signal time.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime, time as datetime_time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from db import database
from services.event_outcomes import (
    MARKET_BENCHMARK_SYMBOL,
    OUTCOME_DATA_VERSION_PREFIX,
    OUTCOME_HORIZON_DAYS,
    OUTCOME_METHOD_VERSION,
    baseline_trade_date,
    direction_hit_for_return,
    horizon_trade_date,
    is_completed_close_price,
    is_current_outcome_contract,
    normalize_mapping_relation,
)
from scoring_provenance import FORMAL_SIGNAL_SCORE_PATHS
from utils.helpers import escape_html


_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = datetime_time(15, 0)
_FORMAL_RELATIONS = ("direct", "upstream", "downstream", "ecosystem")
_ALL_RELATIONS = (*_FORMAL_RELATIONS, "concept")
_RELATION_LABELS = {
    "direct": "直接影响",
    "upstream": "上游",
    "downstream": "下游",
    "ecosystem": "生态关联",
    "concept": "概念候选（排除）",
}
_MIN_MAPPING_SAMPLES = 30
_MIN_INDEPENDENT_EVENTS = 30
_MIN_INDEPENDENT_RESULT_DAYS = 30
_MIN_SECURITIES = 10
_OPTIMIZATION_MIN_MAPPING_SAMPLES = 100
_OPTIMIZATION_MIN_INDEPENDENT_EVENTS = 100
_OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS = 60
_OPTIMIZATION_MIN_SECURITIES = 10
_OPTIMIZATION_MIN_EVIDENCE_COVERAGE = 90.0


def _chunks(values: list[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _canonical_roots(event_rows: list[Any]) -> dict[str, str]:
    parents = {
        str(row["event_id"]): (
            None if row["merged_into"] is None else str(row["merged_into"])
        )
        for row in event_rows
    }

    def root(event_id: str) -> str:
        seen: set[str] = set()
        current = event_id
        while parents.get(current):
            if current in seen:
                return event_id
            seen.add(current)
            current = str(parents[current])
        return current

    return {event_id: root(event_id) for event_id in parents}


def _direction_sign(value: object) -> int | None:
    direction = str(value or "").strip().casefold()
    if direction in {"看多", "bullish", "positive", "up"}:
        return 1
    if direction in {"看空", "bearish", "negative", "down"}:
        return -1
    return None


def _quote_symbol(exchange: object, ticker: object) -> str:
    prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(
        str(exchange or "").upper()
    )
    ticker_value = str(ticker or "")
    if prefix and len(ticker_value) == 6 and ticker_value.isdigit():
        return prefix + ticker_value
    return ""


def _market_date(timestamp: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(timestamp), _MARKET_TZ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _market_close_timestamp(trade_date: str) -> int | None:
    try:
        day = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
        return int(datetime.combine(day, _MARKET_CLOSE, tzinfo=_MARKET_TZ).timestamp())
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _revision_timestamp(revision: dict[str, Any]) -> int | None:
    try:
        source_ts = int(revision.get("source_ts") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return source_ts if source_ts > 0 else _market_close_timestamp(
        str(revision.get("trade_date") or "")
    )


def _wilson_interval(hits: int, total: int, z: float = 1.96) -> dict[str, float] | None:
    if total <= 0:
        return None
    rate = hits / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    center = (rate + z2 / (2.0 * total)) / denominator
    margin = z * math.sqrt(
        (rate * (1.0 - rate) + z2 / (4.0 * total)) / total
    ) / denominator
    return {
        "lower": round(max(0.0, center - margin) * 100.0, 1),
        "upper": round(min(1.0, center + margin) * 100.0, 1),
    }


def _mapping_optimization_state(
    horizons: list[dict[str, Any]], evidence_coverage: float | None
) -> dict[str, Any]:
    coverage = float(evidence_coverage or 0.0)

    def eligible_row(row: dict[str, Any]) -> bool:
        return (
            int(row.get("directional_complete_pairs") or 0)
            >= _OPTIMIZATION_MIN_MAPPING_SAMPLES
            and int(row.get("independent_events") or 0)
            >= _OPTIMIZATION_MIN_INDEPENDENT_EVENTS
            and int(row.get("independent_result_days") or 0)
            >= _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS
            and int(row.get("unique_securities") or 0)
            >= _OPTIMIZATION_MIN_SECURITIES
            and coverage >= _OPTIMIZATION_MIN_EVIDENCE_COVERAGE
        )

    eligible_horizons = [row for row in horizons if eligible_row(row)]
    best = max(
        eligible_horizons or horizons or [{}],
        key=lambda row: (
            int(row.get("independent_events") or 0),
            int(row.get("independent_result_days") or 0),
            int(row.get("directional_complete_pairs") or 0),
        ),
    )
    eligible = bool(eligible_horizons)
    return {
        "stage": "candidate" if eligible else "insufficient",
        "stage_label": "候选优化" if eligible else "样本不足",
        "recommendation": (
            "可生成映射候选规则；需人工语义抽检、结果日分块留出验证后进入影子验证。"
            if eligible
            else "保持现行映射关系、证据门槛与概念排除规则。"
        ),
        "auto_apply": False,
        "semantic_audit_required": True,
        "basis_horizon": str(best.get("horizon") or "T+1"),
        "directional_pairs": int(best.get("directional_complete_pairs") or 0),
        "independent_events": int(best.get("independent_events") or 0),
        "independent_result_days": int(best.get("independent_result_days") or 0),
        "unique_securities": int(best.get("unique_securities") or 0),
        "evidence_coverage": round(coverage, 2),
        "scope": "current_query_window",
        "scope_label": "当前统计窗口",
        "requirements": {
            "directional_pairs": _OPTIMIZATION_MIN_MAPPING_SAMPLES,
            "independent_events": _OPTIMIZATION_MIN_INDEPENDENT_EVENTS,
            "independent_result_days": _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS,
            "unique_securities": _OPTIMIZATION_MIN_SECURITIES,
            "evidence_coverage": _OPTIMIZATION_MIN_EVIDENCE_COVERAGE,
        },
        "roadmap": [
            {"stage": "insufficient", "label": "样本不足"},
            {"stage": "candidate", "label": "候选优化"},
            {"stage": "shadow", "label": "影子验证"},
            {"stage": "releasable", "label": "可发布"},
        ],
    }


def _empty_result(
    *,
    days: int,
    generated_at: int,
    started_at: int | None = None,
    enabled: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": "collecting" if enabled else "not_initialized",
        "status_label": "数据积累中" if enabled else "尚未初始化",
        "reason": reason,
        "days": days,
        "generated_at": generated_at,
        "started_at": started_at,
        "actual_start_date": None,
        "actual_end_date": None,
        "strategy_version": "",
        "mapping_version": "",
        "outcome_method_version": OUTCOME_METHOD_VERSION,
        "selected_signals": 0,
        "ledger_selected_signals": 0,
        "ledger_contract_count": 0,
        "archived_contract_selected_signals": 0,
        "historical_contract_count": 0,
        "historical_contracts_truncated": 0,
        "historical_contracts": [],
        "data_complete": False,
        "evidence_read_failed": False,
        "outcome_read_failed": False,
        "frozen_signals": 0,
        "unfrozen_selected_signals": 0,
        "mapped_signals": 0,
        "formal_mapping_coverage": None,
        "formal_pairs": 0,
        "directional_pairs": 0,
        "unique_events": 0,
        "unique_securities": 0,
        "evidence_backed_pairs": 0,
        "evidence_coverage": None,
        "concept_pairs_excluded": 0,
        "eligibility_funnel": {
            "scope": "current_exact_contract",
            "signals": {
                "selected": 0,
                "valid_frozen_mapping": 0,
                "without_valid_frozen_mapping": 0,
                "formal_mapped": 0,
                "concept_only": 0,
                "without_formal_mapping": 0,
            },
            "pairs": {
                "formal": 0,
                "directional": 0,
                "evidence_backed": 0,
                "concept_excluded": 0,
            },
            "outcomes": [],
            "note": (
                "信号、映射对与事件结果是不同计量单位；资格过滤不代表删除数据。"
            ),
        },
        "legacy_v4_outcomes_excluded": 0,
        "relation_rows": [],
        "horizons": [],
        "min_mapping_samples": _MIN_MAPPING_SAMPLES,
        "min_independent_events": _MIN_INDEPENDENT_EVENTS,
        "min_independent_result_days": _MIN_INDEPENDENT_RESULT_DAYS,
        "min_securities": _MIN_SECURITIES,
        "ci_method": "event_level_wilson_not_result_day_cluster_robust",
        "optimization": _mapping_optimization_state([], None),
    }


def _validated_outcome(
    row: dict[str, Any],
    revisions: dict[str, dict[str, Any]],
    *,
    evaluation_at: int,
) -> dict[str, Any] | None:
    if not is_current_outcome_contract(
        security_id=row.get("security_id"),
        method_version=row.get("method_version"),
        horizon=row.get("horizon"),
        benchmark_security_id=row.get("benchmark_security_id"),
        data_version=row.get("data_version"),
        signal_available_at=row.get("signal_available_at"),
        baseline_at=row.get("baseline_at"),
    ):
        return None
    if str(row.get("observation_state") or "") != "complete":
        return None
    try:
        if (
            int(row["ledger_available_at"]) != int(row["signal_available_at"])
            or int(row["frozen_available_at"]) != int(row["signal_available_at"])
            or int(row["ledger_selected"]) != 1
            or int(row["ledger_similarity_copy"]) != 0
            or int(row["mapping_revision"]) != int(row["frozen_mapping_revision"])
            or int(row["measured_at"]) > int(evaluation_at)
            or int(row["created_at"]) > int(evaluation_at)
        ):
            return None
        quality = float(row["data_quality"])
        if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

    data_version = str(row.get("data_version") or "")
    revision_ids = data_version[len(OUTCOME_DATA_VERSION_PREFIX) :].split(":")
    if len(revision_ids) != 4:
        return None
    referenced = [revisions.get(item) for item in revision_ids]
    if any(item is None for item in referenced):
        return None
    asset_baseline, asset_measured, market_baseline, market_measured = referenced

    try:
        offset = int(str(row["horizon"]).removeprefix("T+"))
        expected_baseline_date = baseline_trade_date(int(row["signal_available_at"]))
        expected_measured_date = (
            None
            if expected_baseline_date is None
            else horizon_trade_date(expected_baseline_date, offset)
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        expected_baseline_date is None
        or expected_measured_date is None
        or _market_date(row.get("baseline_at")) != expected_baseline_date
        or _market_date(row.get("measured_at")) != expected_measured_date
    ):
        return None
    asset_symbol = _quote_symbol(row.get("exchange"), row.get("ticker"))
    roles = (
        (asset_symbol, expected_baseline_date),
        (asset_symbol, expected_measured_date),
        (MARKET_BENCHMARK_SYMBOL, expected_baseline_date),
        (MARKET_BENCHMARK_SYMBOL, expected_measured_date),
    )
    if not asset_symbol or any(
        str(revision["symbol"]).casefold() != symbol
        or not is_completed_close_price(
            revision, trade_date, evaluation_at=int(row["created_at"]), timestamp_key="source_ts"
        )
        for revision, (symbol, trade_date) in zip(referenced, roles)
    ):
        return None
    timestamps = [_revision_timestamp(revision) for revision in referenced]
    try:
        recorded_at = [int(revision["recorded_at"]) for revision in referenced]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (
        any(
            value <= 0
            or value > min(int(evaluation_at), int(row["created_at"]))
            for value in recorded_at
        )
        or int(timestamps[0]) != int(row["baseline_at"])
        or int(timestamps[1]) != int(row["measured_at"])
        or int(timestamps[2]) <= int(row["signal_available_at"])
    ):
        return None
    try:
        asset_base = float(asset_baseline["close_price"])
        asset_target = float(asset_measured["close_price"])
        market_base = float(market_baseline["close_price"])
        market_target = float(market_measured["close_price"])
        if min(asset_base, asset_target, market_base, market_target) <= 0:
            return None
        gross = asset_target / asset_base - 1.0
        market = market_target / market_base - 1.0
        excess = gross - market
        stored = (
            float(row["baseline_price"]),
            float(row["gross_return"]),
            float(row["benchmark_return"]),
            float(row["excess_return"]),
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    calculated = (asset_base, gross, market, excess)
    if not all(
        math.isfinite(actual)
        and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12)
        for actual, expected in zip(stored, calculated)
    ):
        return None
    expected_hit = direction_hit_for_return(str(row.get("mapping_direction") or ""), gross)
    stored_hit = row.get("direction_hit")
    if expected_hit is None:
        if stored_hit is not None:
            return None
    else:
        try:
            if stored_hit is None or int(stored_hit) != int(expected_hit):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    sign = _direction_sign(row.get("mapping_direction"))
    return {
        "gross_return": gross,
        "excess_return": excess,
        "signed_excess": None if sign is None else sign * excess,
        "measured_date": expected_measured_date,
    }


def _runtime_mapping_contract() -> tuple[str, str] | None:
    """Resolve the live contract without inferring it from historical rows."""
    try:
        from core.asset_mapping import get_mapping_version
        from core.config import config
        from core.user_feedback import resolve_scoring_contract

        runtime_cfg = resolve_scoring_contract(
            config.cfg, register_snapshot=False
        )
        strategy_version = str(
            runtime_cfg.get("scoring_strategy_version") or ""
        ).strip()
        mapping_version = str(get_mapping_version() or "").strip()
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if not strategy_version or not mapping_version:
        return None
    return strategy_version, mapping_version


def _contract_key(row: dict[str, Any] | Any) -> tuple[str, str]:
    return (
        str(row.get("strategy_version") or ""),
        str(row.get("signal_mapping_version") or ""),
    )


def _canonical_uuid(value: object) -> bool:
    try:
        text = str(value or "")
        return bool(text) and str(uuid.UUID(text)) == text
    except (AttributeError, TypeError, ValueError):
        return False


def _valid_frozen_mapping_row(
    row: dict[str, Any], roots: dict[str, str]
) -> tuple[bool, str | None]:
    relation = normalize_mapping_relation(row.get("relation_type"))
    event_id = str(row.get("mapping_event_id") or "")
    try:
        exact = (
            bool(row.get("mapping_id"))
            and relation in _ALL_RELATIONS
            and str(row.get("asset_type") or "").casefold() == "equity"
            and int(row.get("frozen_available_at") or 0) == int(row["available_at"])
            and int(row.get("captured_at") or 0) == int(row["recorded_at"])
            and int(row.get("mapping_created_at") or 0) <= int(row["captured_at"])
            and str(row.get("mapping_version") or "")
            == str(row.get("signal_mapping_version") or "")
            and event_id in roots
            and _canonical_uuid(event_id)
            and str(row.get("signal_event_id") or "") == event_id
            and str(row.get("frozen_event_id") or "") == event_id
            and str(row.get("frozen_security_id") or "")
            == str(row.get("security_id") or "")
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        exact = False
    return exact, relation


def _summarize_mapping_contract(
    *,
    contract: tuple[str, str],
    signal_rows: list[dict[str, Any]],
    signal_has_snapshot: set[int],
    signal_has_formal: set[int],
    pairs: dict[tuple[str, str, str], dict[str, Any]],
    evidence_pair_keys: set[tuple[int, str]],
    outcome_by_pair_horizon: dict[tuple[int, str, str], dict[str, Any]],
    generated_at: int,
    reference_only: bool,
    data_complete: bool,
) -> dict[str, Any]:
    selected_signal_ids = {int(row["ledger_signal_id"]) for row in signal_rows}
    formal_pairs = [
        row for row in pairs.values() if row["normalized_relation"] in _FORMAL_RELATIONS
    ]
    concept_pairs = [
        row for row in pairs.values() if row["normalized_relation"] == "concept"
    ]
    relation_counts: dict[str, dict[str, int]] = {
        relation: {"pairs": 0, "directional_pairs": 0, "evidence_backed_pairs": 0}
        for relation in _ALL_RELATIONS
    }
    for row in pairs.values():
        relation = str(row["normalized_relation"])
        relation_counts[relation]["pairs"] += 1
        if _direction_sign(row.get("mapping_direction")) is not None:
            relation_counts[relation]["directional_pairs"] += 1
        identity = (int(row["ledger_signal_id"]), str(row["mapping_id"]))
        if identity in evidence_pair_keys:
            relation_counts[relation]["evidence_backed_pairs"] += 1

    horizon_rows: list[dict[str, Any]] = []
    for offset in OUTCOME_HORIZON_DAYS:
        horizon = f"T+{offset}"
        due = 0
        complete = 0
        directional_complete = 0
        completed_events: set[str] = set()
        completed_securities: set[str] = set()
        result_days: set[str] = set()
        event_signed_excess: dict[str, list[float]] = defaultdict(list)
        for pair in formal_pairs:
            baseline_date = baseline_trade_date(int(pair["available_at"]))
            measured_date = (
                None if baseline_date is None else horizon_trade_date(baseline_date, offset)
            )
            due_at = None if measured_date is None else _market_close_timestamp(measured_date)
            if due_at is not None and generated_at >= due_at:
                due += 1
            outcome = outcome_by_pair_horizon.get(
                (int(pair["ledger_signal_id"]), str(pair["mapping_id"]), horizon)
            )
            if outcome is None:
                continue
            complete += 1
            signed_excess = outcome.get("signed_excess")
            if (
                _direction_sign(pair.get("mapping_direction")) is None
                or not isinstance(signed_excess, (int, float))
            ):
                continue
            directional_complete += 1
            root = str(pair["root_event_id"])
            completed_events.add(root)
            completed_securities.add(str(pair["security_id"]))
            result_days.add(str(outcome["measured_date"]))
            event_signed_excess[root].append(float(signed_excess))
        event_values = [sum(values) / len(values) for values in event_signed_excess.values()]
        hits = sum(value > 0 for value in event_values)
        publishable = (
            data_complete
            and
            directional_complete >= _MIN_MAPPING_SAMPLES
            and len(completed_events) >= _MIN_INDEPENDENT_EVENTS
            and len(result_days) >= _MIN_INDEPENDENT_RESULT_DAYS
            and len(completed_securities) >= _MIN_SECURITIES
        )
        horizon_rows.append(
            {
                "horizon": horizon,
                "due_pairs": due,
                "complete_pairs": complete,
                "missing_due_pairs": max(0, due - complete),
                "directional_complete_pairs": directional_complete,
                "independent_events": len(completed_events),
                "independent_result_days": len(result_days),
                "unique_securities": len(completed_securities),
                "event_level_samples": len(event_values),
                "event_level_hits": hits,
                "publishable": publishable,
                "excess_hit_rate": (
                    round(hits / len(event_values) * 100.0, 1)
                    if publishable and event_values
                    else None
                ),
                "excess_hit_rate_ci95": (
                    _wilson_interval(hits, len(event_values)) if publishable else None
                ),
                "mean_signed_excess": (
                    round(statistics.fmean(event_values) * 100.0, 3)
                    if publishable and event_values
                    else None
                ),
                "median_signed_excess": (
                    round(statistics.median(event_values) * 100.0, 3)
                    if publishable and event_values
                    else None
                ),
            }
        )

    evidence_backed = sum(
        (int(row["ledger_signal_id"]), str(row["mapping_id"])) in evidence_pair_keys
        for row in formal_pairs
    )
    evidence_coverage_raw = (
        evidence_backed / len(formal_pairs) * 100.0 if formal_pairs else None
    )
    selected_times = [int(row["available_at"]) for row in signal_rows]
    mature = data_complete and any(row["publishable"] for row in horizon_rows)
    return {
        "strategy_version": contract[0],
        "mapping_version": contract[1],
        "reference_only": reference_only,
        "status": "threshold_met" if mature else "collecting" if data_complete else "data_failed",
        "status_label": (
            "已达统计门槛" if mature else "样本积累中" if data_complete else "后验读取失败"
        ),
        "data_complete": data_complete,
        "actual_start_date": _market_date(min(selected_times)) if selected_times else None,
        "actual_end_date": _market_date(max(selected_times)) if selected_times else None,
        "selected_signals": len(selected_signal_ids),
        "frozen_signals": len(signal_has_snapshot),
        "unfrozen_selected_signals": len(selected_signal_ids - signal_has_snapshot),
        "mapped_signals": len(signal_has_formal),
        "formal_mapping_coverage": (
            round(len(signal_has_formal) / len(selected_signal_ids) * 100.0, 1)
            if selected_signal_ids
            else None
        ),
        "formal_pairs": len(formal_pairs),
        "directional_pairs": sum(
            _direction_sign(row.get("mapping_direction")) is not None
            for row in formal_pairs
        ),
        "unique_events": len({str(row["root_event_id"]) for row in formal_pairs}),
        "unique_securities": len({str(row["security_id"]) for row in formal_pairs}),
        "evidence_backed_pairs": evidence_backed,
        "evidence_coverage": (
            round(evidence_coverage_raw, 1)
            if evidence_coverage_raw is not None
            else None
        ),
        "concept_pairs_excluded": len(concept_pairs),
        "eligibility_funnel": {
            "scope": "historical_exact_contract" if reference_only else "current_exact_contract",
            "signals": {
                "selected": len(selected_signal_ids),
                "valid_frozen_mapping": len(signal_has_snapshot),
                "without_valid_frozen_mapping": len(
                    selected_signal_ids - signal_has_snapshot
                ),
                "formal_mapped": len(signal_has_formal),
                "concept_only": len(signal_has_snapshot - signal_has_formal),
                "without_formal_mapping": len(
                    selected_signal_ids - signal_has_formal
                ),
            },
            "pairs": {
                "formal": len(formal_pairs),
                "directional": sum(
                    _direction_sign(row.get("mapping_direction")) is not None
                    for row in formal_pairs
                ),
                "evidence_backed": evidence_backed,
                "concept_excluded": len(concept_pairs),
            },
            "outcomes": [
                {
                    "horizon": row["horizon"],
                    "due_pairs": int(row["due_pairs"]),
                    "complete_pairs": int(row["complete_pairs"]),
                    "directional_complete_pairs": int(
                        row["directional_complete_pairs"]
                    ),
                    "independent_events": int(row["independent_events"]),
                    "independent_result_days": int(
                        row["independent_result_days"]
                    ),
                }
                for row in horizon_rows
            ],
            "note": (
                "信号、映射对与事件结果是不同计量单位；资格过滤不代表删除数据。"
            ),
        },
        "relation_rows": [
            {
                "relation": relation,
                "label": _RELATION_LABELS[relation],
                **relation_counts[relation],
            }
            for relation in _ALL_RELATIONS
            if relation_counts[relation]["pairs"] > 0
        ],
        "horizons": horizon_rows,
    }


def run_mapping_validation(
    days: int = 90,
    *,
    now: int | None = None,
    current_contract: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Run posterior validation on event to security mappings.

    Validates whether frozen point-in-time asset mappings show directionally
    consistent price movements relative to the broad benchmark (CSI 300).
    Only direct/upstream/downstream/ecosystem equity relationships are validated.

    Args:
        days: Lookback window in calendar days (default: 90).
        now: Override current timestamp (default: None = system time).
        horizon: Price observation offset (default: 1 = T+1). Must be in {1,3,5,20}.
        min_score: Optional score threshold filter (default: None = stored threshold).

    Returns:
        dict with validation_status, direction_match_rate, settled_samples,
        unique_events, unique_result_days, unique_securities, evidence_coverage.

    See Also:
        docs/model-quality.md, services/event_outcomes.py
    """

    """Aggregate the live contract and bounded per-contract historical references."""
    bounded_days = max(1, min(365, int(days)))
    generated_at = int(time.time() if now is None else now)
    cutoff = generated_at - bounded_days * 86400
    required_tables = {
        "app_meta",
        "backtest_signals",
        "backtest_signal_securities",
        "events",
        "event_securities",
        "event_evidence",
        "event_outcomes",
        "securities",
        "future_price_revisions",
    }
    try:
        with database.db_lock:
            with database.get_connection() as conn:
                conn.execute("BEGIN")
                try:
                    present = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    marker = (
                        conn.execute(
                            "SELECT v FROM app_meta WHERE k = 'mapping_validation_started_at'"
                        ).fetchone()
                        if "app_meta" in present
                        else None
                    )
                    if marker is None or not required_tables.issubset(present):
                        return _empty_result(
                            days=bounded_days,
                            generated_at=generated_at,
                            reason="prospective_mapping_ledger_unavailable",
                        )
                    try:
                        started_at = int(str(marker[0]))
                    except (TypeError, ValueError):
                        return _empty_result(
                            days=bounded_days,
                            generated_at=generated_at,
                            reason="invalid_mapping_validation_marker",
                        )
                    effective_cutoff = max(cutoff, started_at)
                    formal_paths = tuple(sorted(FORMAL_SIGNAL_SCORE_PATHS))
                    formal_placeholders = ",".join("?" for _ in formal_paths)
                    rows = conn.execute(
                        "WITH ranked AS (SELECT signal.*, ROW_NUMBER() OVER ("
                        "PARTITION BY signal.link, signal.strategy_version, signal.mapping_version "
                        "ORDER BY signal.available_at DESC, signal.id DESC) signal_rank "
                        "FROM backtest_signals AS signal) "
                        "SELECT signal.id AS ledger_signal_id, signal.event_id AS signal_event_id, "
                        "signal.event_version, signal.available_at, signal.recorded_at, "
                        "signal.strategy_version, signal.mapping_version AS signal_mapping_version, "
                        "frozen.mapping_id, frozen.event_id AS frozen_event_id, "
                        "frozen.security_id AS frozen_security_id, "
                        "frozen.mapping_revision, frozen.signal_available_at AS frozen_available_at, "
                        "frozen.captured_at, mapping.event_id AS mapping_event_id, "
                        "mapping.security_id, mapping.relation_type, "
                        "mapping.direction AS mapping_direction, mapping.mapping_version, "
                        "mapping.created_at AS mapping_created_at, security.exchange, "
                        "security.ticker, security.asset_type "
                        "FROM ranked AS signal "
                        "LEFT JOIN backtest_signal_securities AS frozen "
                        "ON frozen.ledger_signal_id = signal.id "
                        "LEFT JOIN event_securities AS mapping ON mapping.mapping_id = frozen.mapping_id "
                        "AND mapping.event_id = frozen.event_id "
                        "AND mapping.security_id = frozen.security_id "
                        "AND mapping.revision = frozen.mapping_revision "
                        "LEFT JOIN securities AS security ON security.security_id = frozen.security_id "
                        "WHERE signal.signal_rank = 1 AND signal.selected = 1 "
                        "AND signal.is_similarity_copy = 0 AND signal.event_version >= 1 "
                        "AND LOWER(TRIM(COALESCE(signal.score_path, ''))) IN ("
                        f"{formal_placeholders}) "
                        "AND signal.available_at >= ? ORDER BY signal.available_at, signal.id",
                        (*formal_paths, effective_cutoff),
                    ).fetchall()
                    event_rows = conn.execute(
                        "SELECT event_id, merged_into FROM events"
                    ).fetchall()
                    legacy_v4 = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM event_outcomes WHERE is_current = 1 "
                            "AND observation_state = 'complete' "
                            "AND method_version = 'a-share-event-ohlc-v4'"
                        ).fetchone()[0]
                    )
                finally:
                    conn.rollback()
    except (sqlite3.DatabaseError, OSError):
        return _empty_result(
            days=bounded_days,
            generated_at=generated_at,
            reason="mapping_validation_read_failed",
        )

    base = _empty_result(
        days=bounded_days,
        generated_at=generated_at,
        started_at=started_at,
        enabled=True,
    )
    base["legacy_v4_outcomes_excluded"] = legacy_v4
    base["data_complete"] = True
    if current_contract is None:
        contract = _runtime_mapping_contract()
    else:
        try:
            contract = (
                str(current_contract[0] or "").strip(),
                str(current_contract[1] or "").strip(),
            )
        except (IndexError, TypeError):
            contract = None
        if contract is not None and (not contract[0] or not contract[1]):
            contract = None
    if contract is None:
        base["enabled"] = False
        base["status"] = "data_failed"
        base["status_label"] = "当前契约不可用"
        base["data_complete"] = False
        base["reason"] = "current_mapping_contract_unavailable"
        return base
    base["strategy_version"], base["mapping_version"] = contract
    if not rows:
        return base

    signal_rows: dict[int, dict[str, Any]] = {}
    for raw in rows:
        item = dict(raw)
        signal_rows[int(item["ledger_signal_id"])] = item
    signal_rows_by_contract: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in signal_rows.values():
        signal_rows_by_contract[_contract_key(row)].append(row)
    ledger_contracts = set(signal_rows_by_contract)
    roots = _canonical_roots(event_rows)
    valid_rows_by_contract: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    snapshot_ids_by_contract: dict[tuple[str, str], set[int]] = defaultdict(set)
    formal_ids_by_contract: dict[tuple[str, str], set[int]] = defaultdict(set)
    for raw in rows:
        row = dict(raw)
        row_contract = _contract_key(row)
        exact, relation = _valid_frozen_mapping_row(row, roots)
        if not exact:
            continue
        ledger_id = int(row["ledger_signal_id"])
        snapshot_ids_by_contract[row_contract].add(ledger_id)
        if relation in _FORMAL_RELATIONS:
            formal_ids_by_contract[row_contract].add(ledger_id)
        mapping_event_id = str(row["mapping_event_id"])
        row["root_event_id"] = roots.get(mapping_event_id, mapping_event_id)
        row["normalized_relation"] = relation
        valid_rows_by_contract[row_contract].append(row)

    pairs_by_contract: dict[
        tuple[str, str], dict[tuple[str, str, str], dict[str, Any]]
    ] = {}
    for row_contract, valid_rows in valid_rows_by_contract.items():
        contract_pairs: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in valid_rows:
            pair_key = (
                str(row["root_event_id"]),
                str(row["security_id"]),
                str(row["normalized_relation"]),
            )
            previous = contract_pairs.get(pair_key)
            if previous is None or (
                int(row["available_at"]), int(row["ledger_signal_id"])
            ) < (
                int(previous["available_at"]), int(previous["ledger_signal_id"])
            ):
                contract_pairs[pair_key] = row
        pairs_by_contract[row_contract] = contract_pairs
    all_formal_pairs = [
        row
        for contract_pairs in pairs_by_contract.values()
        for row in contract_pairs.values()
        if row["normalized_relation"] in _FORMAL_RELATIONS
    ]

    evidence_pair_keys: set[tuple[int, str]] = set()
    evidence_read_failed = False
    mapping_ids = sorted({str(row["mapping_id"]) for row in all_formal_pairs})
    if mapping_ids:
        try:
            with database.db_lock:
                with database.get_connection() as conn:
                    conn.execute("BEGIN")
                    try:
                        pairs_by_mapping_revision: dict[
                            tuple[str, int], list[dict[str, Any]]
                        ] = defaultdict(list)
                        for pair in all_formal_pairs:
                            pairs_by_mapping_revision[
                                (str(pair["mapping_id"]), int(pair["mapping_revision"]))
                            ].append(pair)
                        for chunk in _chunks(mapping_ids):
                            placeholders = ",".join("?" for _ in chunk)
                            evidence_rows = conn.execute(
                                "SELECT mapping_id, mapping_revision, observed_at, created_at, "
                                "updated_at, stance "
                                "FROM event_evidence WHERE mapping_id IN ("
                                + placeholders
                                + ")",
                                tuple(chunk),
                            ).fetchall()
                            for evidence in evidence_rows:
                                key = (
                                    str(evidence["mapping_id"]),
                                    int(evidence["mapping_revision"]),
                                )
                                if str(evidence["stance"]) != "supports":
                                    continue
                                for pair in pairs_by_mapping_revision.get(key, []):
                                    if (
                                        int(evidence["observed_at"]) <= int(pair["available_at"])
                                        and int(evidence["created_at"]) <= int(pair["captured_at"])
                                        and int(evidence["updated_at"]) <= int(pair["captured_at"])
                                    ):
                                        evidence_pair_keys.add(
                                            (
                                                int(pair["ledger_signal_id"]),
                                                str(pair["mapping_id"]),
                                            )
                                        )
                    finally:
                        conn.rollback()
        except (sqlite3.DatabaseError, OSError):
            evidence_pair_keys.clear()
            evidence_read_failed = True

    pair_lookup = {
        (int(row["ledger_signal_id"]), str(row["mapping_id"])): row
        for row in all_formal_pairs
    }
    outcome_rows: list[dict[str, Any]] = []
    revisions: dict[str, dict[str, Any]] = {}
    outcome_read_failed = False
    if pair_lookup:
        try:
            with database.db_lock:
                with database.get_connection() as conn:
                    conn.execute("BEGIN")
                    try:
                        ledger_ids = sorted({key[0] for key in pair_lookup})
                        for start in range(0, len(ledger_ids), 300):
                            batch = ledger_ids[start : start + 300]
                            placeholders = ",".join("?" for _ in batch)
                            fetched = conn.execute(
                                "SELECT outcome.*, ledger.available_at AS ledger_available_at, "
                                "ledger.selected AS ledger_selected, "
                                "ledger.is_similarity_copy AS ledger_similarity_copy, "
                                "frozen.signal_available_at AS frozen_available_at, "
                                "frozen.mapping_revision AS frozen_mapping_revision, "
                                "mapping.direction AS mapping_direction, "
                                "security.exchange, security.ticker "
                                "FROM event_outcomes AS outcome "
                                "JOIN backtest_signals AS ledger ON ledger.id = outcome.signal_ledger_id "
                                "JOIN backtest_signal_securities AS frozen "
                                "ON frozen.ledger_signal_id = outcome.signal_ledger_id "
                                "AND frozen.mapping_id = outcome.mapping_id "
                                "AND frozen.event_id = outcome.event_id "
                                "AND frozen.security_id = outcome.security_id "
                                "AND frozen.mapping_revision = outcome.mapping_revision "
                                "JOIN event_securities AS mapping ON mapping.mapping_id = outcome.mapping_id "
                                "AND mapping.event_id = outcome.event_id "
                                "AND mapping.security_id = outcome.security_id "
                                "AND mapping.revision = outcome.mapping_revision "
                                "JOIN securities AS security ON security.security_id = outcome.security_id "
                                "WHERE outcome.is_current = 1 AND outcome.observation_state = 'complete' "
                                "AND outcome.method_version = ? AND outcome.signal_ledger_id IN ("
                                + placeholders
                                + ")",
                                (OUTCOME_METHOD_VERSION, *batch),
                            ).fetchall()
                            outcome_rows.extend(dict(row) for row in fetched)
                        revision_ids: set[str] = set()
                        for row in outcome_rows:
                            value = str(row.get("data_version") or "")
                            if value.startswith(OUTCOME_DATA_VERSION_PREFIX):
                                parts = value[len(OUTCOME_DATA_VERSION_PREFIX) :].split(":")
                                if len(parts) == 4:
                                    try:
                                        canonical = [str(uuid.UUID(part)) for part in parts]
                                    except (TypeError, ValueError, AttributeError):
                                        continue
                                    if canonical == parts:
                                        revision_ids.update(parts)
                        for chunk in _chunks(sorted(revision_ids)):
                            placeholders = ",".join("?" for _ in chunk)
                            price_rows = conn.execute(
                                "SELECT revision_id, symbol, trade_date, close_price, source_ts, "
                                "recorded_at "
                                "FROM future_price_revisions WHERE revision_id IN ("
                                + placeholders
                                + ")",
                                tuple(chunk),
                            ).fetchall()
                            revisions.update(
                                {str(row["revision_id"]): dict(row) for row in price_rows}
                            )
                    finally:
                        conn.rollback()
        except (sqlite3.DatabaseError, OSError):
            outcome_rows = []
            revisions = {}
            outcome_read_failed = True

    outcome_by_pair_horizon: dict[tuple[int, str, str], dict[str, Any]] = {}
    for row in outcome_rows:
        pair_key = (int(row["signal_ledger_id"]), str(row["mapping_id"]))
        if pair_key not in pair_lookup:
            continue
        validated = _validated_outcome(
            row, revisions, evaluation_at=generated_at
        )
        if validated is not None:
            outcome_by_pair_horizon[(pair_key[0], pair_key[1], str(row["horizon"]))] = validated

    data_complete = not evidence_read_failed and not outcome_read_failed
    current_summary = _summarize_mapping_contract(
        contract=contract,
        signal_rows=signal_rows_by_contract.get(contract, []),
        signal_has_snapshot=snapshot_ids_by_contract.get(contract, set()),
        signal_has_formal=formal_ids_by_contract.get(contract, set()),
        pairs=pairs_by_contract.get(contract, {}),
        evidence_pair_keys=evidence_pair_keys,
        outcome_by_pair_horizon=outcome_by_pair_horizon,
        generated_at=generated_at,
        reference_only=False,
        data_complete=data_complete,
    )
    historical_keys = sorted(
        (item for item in ledger_contracts if item != contract),
        key=lambda item: max(
            int(row["available_at"]) for row in signal_rows_by_contract[item]
        ),
        reverse=True,
    )
    displayed_history = historical_keys[:8]
    historical_contracts = [
        _summarize_mapping_contract(
            contract=item,
            signal_rows=signal_rows_by_contract[item],
            signal_has_snapshot=snapshot_ids_by_contract.get(item, set()),
            signal_has_formal=formal_ids_by_contract.get(item, set()),
            pairs=pairs_by_contract.get(item, {}),
            evidence_pair_keys=evidence_pair_keys,
            outcome_by_pair_horizon=outcome_by_pair_horizon,
            generated_at=generated_at,
            reference_only=True,
            data_complete=data_complete,
        )
        for item in displayed_history
    ]
    current_optimization = _mapping_optimization_state(
        current_summary["horizons"], current_summary["evidence_coverage"]
    )
    if not data_complete:
        current_optimization.update(
            {
                "stage": "data_failed",
                "stage_label": "数据读取失败",
                "recommendation": "停止发布后验结论与映射策略候选，待只读数据链路恢复。",
            }
        )
    base.update(current_summary)
    base.update(
        {
            "status_label": (
                "已达统计门槛"
                if current_summary["status"] == "threshold_met"
                else "数据积累中"
                if current_summary["status"] == "collecting"
                else "后验读取失败"
            ),
            "data_complete": data_complete,
            "evidence_read_failed": evidence_read_failed,
            "outcome_read_failed": outcome_read_failed,
            "ledger_selected_signals": len(signal_rows),
            "ledger_contract_count": len(ledger_contracts),
            "archived_contract_selected_signals": max(
                0, len(signal_rows) - int(current_summary["selected_signals"])
            ),
            "historical_contract_count": len(historical_keys),
            "historical_contracts_truncated": max(
                0, len(historical_keys) - len(displayed_history)
            ),
            "historical_contracts": historical_contracts,
            "optimization": current_optimization,
        }
    )
    return base


def _pct(value: object, digits: int = 1) -> str:
    return f"{float(value):.{digits}f}%" if isinstance(value, (int, float)) else "—"


def _render_mapping_optimization_html(
    result: dict[str, Any], *, compact: bool
) -> str:
    state = result.get("optimization") or _mapping_optimization_state(
        result.get("horizons") or [], result.get("evidence_coverage")
    )
    requirements = state.get("requirements") or {}
    stage = str(state.get("stage") or "insufficient")
    color = (
        "#ef4444"
        if stage == "data_failed"
        else "#60a5fa"
        if stage == "candidate"
        else "#fbbf24"
    )
    progress = (
        f"{str(state.get('scope_label') or '当前统计窗口')} · "
        f"{str(state.get('basis_horizon') or 'T+1')} 后验进度：已结算方向样本 "
        f"{int(state.get('directional_pairs') or 0)}/"
        f"{int(requirements.get('directional_pairs') or _OPTIMIZATION_MIN_MAPPING_SAMPLES)} · "
        f"已结算独立事件 {int(state.get('independent_events') or 0)}/"
        f"{int(requirements.get('independent_events') or _OPTIMIZATION_MIN_INDEPENDENT_EVENTS)} · "
        f"已覆盖结果交易日 {int(state.get('independent_result_days') or 0)}/"
        f"{int(requirements.get('independent_result_days') or _OPTIMIZATION_MIN_INDEPENDENT_RESULT_DAYS)} · "
        f"已结算证券 {int(state.get('unique_securities') or 0)}/"
        f"{int(requirements.get('unique_securities') or _OPTIMIZATION_MIN_SECURITIES)} · "
        f"当前映射证据覆盖 {float(state.get('evidence_coverage') or 0):.2f}%/"
        f"{float(requirements.get('evidence_coverage') or _OPTIMIZATION_MIN_EVIDENCE_COVERAGE):.2f}%"
    )
    roadmap = " → ".join(
        str(item.get("label") or "") for item in state.get("roadmap") or []
    )
    margin = "margin:6px 0 0;" if compact else "margin:0 0 10px;"
    summary_html = (
        f'<div class="mapping-optimization-state" style="{margin}padding:8px 10px;border-radius:10px;'
        f'background:{color}12;border:1px solid {color}2e;font-size:11px;line-height:1.55;">'
        f'<div><b style="color:{color};margin-right:6px;">映射策略：{escape_html(state.get("stage_label") or "样本不足")}</b>'
        f'{escape_html(state.get("recommendation") or "保持现行映射标准。")} '
        '<span style="color:var(--color-text-subdue,inherit);">不自动应用</span></div>'
        f'<div style="color:var(--color-text-subdue,inherit);">{escape_html(progress)}'
        + (f' · {escape_html(roadmap)}' if roadmap and not compact else "")
        + '</div></div>'
    )
    return summary_html


def _short_contract_version(value: object) -> str:
    text = str(value or "—")
    prefix, separator, suffix = text.rpartition(":")
    if separator and len(suffix) > 8:
        return f"{prefix}:{suffix[:8]}"
    return text if len(text) <= 24 else text[:21] + "…"


def _render_mapping_contract_ledger(result: dict[str, Any]) -> str:
    current = {
        key: result.get(key)
        for key in (
            "strategy_version",
            "mapping_version",
            "actual_start_date",
            "actual_end_date",
            "selected_signals",
            "frozen_signals",
            "formal_pairs",
            "directional_pairs",
            "concept_pairs_excluded",
            "horizons",
            "status",
        )
    }
    cohorts = [(True, current)] + [
        (False, dict(item)) for item in result.get("historical_contracts") or []
    ]
    current_row = ""
    historical_rows: list[str] = []
    for is_current, cohort in cohorts:
        horizons = list(cohort.get("horizons") or [])
        t1 = next((row for row in horizons if row.get("horizon") == "T+1"), {})
        display_horizon = next(
            (row for row in horizons if bool(row.get("publishable"))), t1
        )
        if not bool(cohort.get("data_complete", result.get("data_complete", True))):
            posterior = "后验读取失败"
            maturity = "本契约暂停发布"
        elif display_horizon.get("publishable"):
            ci = display_horizon.get("excess_hit_rate_ci95") or {}
            posterior = (
                f"{display_horizon.get('horizon') or 'T+1'} 命中 "
                f"{_pct(display_horizon.get('excess_hit_rate'))} · "
                f"CI {ci.get('lower', '—')}–{ci.get('upper', '—')}%"
            )
            maturity = "本契约已达门槛"
        else:
            posterior = (
                f"{display_horizon.get('horizon') or 'T+1'} 到期/结算 "
                f"{int(display_horizon.get('due_pairs') or 0)}/"
                f"{int(display_horizon.get('complete_pairs') or 0)}"
            )
            maturity = "本契约未达门槛"
        date_start = str(cohort.get("actual_start_date") or "—")
        date_end = str(cohort.get("actual_end_date") or "—")
        date_label = date_start if date_start == date_end else f"{date_start} 至 {date_end}"
        badge = "当前正式" if is_current else "历史参考"
        badge_color = "#60a5fa" if is_current else "#a1a1aa"
        row_html = (
            '<div class="mapping-contract-row">'
            '<div class="mapping-contract-id">'
            f'<span style="color:{badge_color};border-color:{badge_color};">{badge}</span>'
            f'<b>{escape_html(_short_contract_version(cohort.get("strategy_version")))} × '
            f'{escape_html(_short_contract_version(cohort.get("mapping_version")))}</b>'
            f'<small>{escape_html(date_label)} · {escape_html(maturity)}</small></div>'
            '<div class="mapping-contract-stat"><span>入选 / 冻结</span>'
            f'<b>{escape_html(int(cohort.get("selected_signals") or 0))} / '
            f'{escape_html(int(cohort.get("frozen_signals") or 0))}</b></div>'
            '<div class="mapping-contract-stat"><span>正式映射 / 方向假设</span>'
            f'<b>{escape_html(int(cohort.get("formal_pairs") or 0))} / '
            f'{escape_html(int(cohort.get("directional_pairs") or 0))}</b>'
            f'<small>概念排除 {escape_html(int(cohort.get("concept_pairs_excluded") or 0))}</small></div>'
            '<div class="mapping-contract-stat"><span>独立后验</span>'
            f'<b>{escape_html(posterior)}</b>'
            f'<small>方向 {escape_html(int(display_horizon.get("directional_complete_pairs") or 0))} · '
            f'事件 {escape_html(int(display_horizon.get("independent_events") or 0))} · '
            f'结果日 {escape_html(int(display_horizon.get("independent_result_days") or 0))}</small></div>'
            '</div>'
        )
        if is_current:
            current_row = row_html
        else:
            historical_rows.append(row_html)
    truncated = int(result.get("historical_contracts_truncated") or 0)
    truncation_note = (
        f"；另有 {truncated} 个更早契约未载入本页" if truncated > 0 else ""
    )
    history_count = int(
        result.get("historical_contract_count") or len(historical_rows)
    )
    archived_selected = int(
        result.get("archived_contract_selected_signals")
        or sum(
            int(item.get("selected_signals") or 0)
            for item in result.get("historical_contracts") or []
        )
    )
    history_html = ""
    if historical_rows:
        history_html = (
            '<details class="mapping-contract-history">'
            '<summary><b>'
            f'历史契约 {escape_html(history_count)} 个 · 旧契约入选 {escape_html(archived_selected)} 条'
            '</b><span>默认折叠 · 仅作分版本参考'
            f'{escape_html(truncation_note)}</span></summary>'
            + "".join(historical_rows)
            + '</details>'
        )
    return (
        '<div class="mapping-contract-ledger">'
        '<div class="mapping-contract-title"><b>契约后验账本</b>'
        '<span>当前契约直接展示并用于正式结论；旧契约逐个独立结算，禁止跨契约合并命中率。</span></div>'
        + current_row
        + history_html
        + '</div>'
    )


def _render_mapping_eligibility_funnel_html(result: dict[str, Any]) -> str:
    funnel = result.get("eligibility_funnel")
    if not isinstance(funnel, dict):
        return ""
    signals = funnel.get("signals") if isinstance(funnel.get("signals"), dict) else {}
    pairs = funnel.get("pairs") if isinstance(funnel.get("pairs"), dict) else {}
    outcomes = funnel.get("outcomes") if isinstance(funnel.get("outcomes"), list) else []
    t1 = next(
        (
            row
            for row in outcomes
            if isinstance(row, dict) and row.get("horizon") == "T+1"
        ),
        {},
    )

    def card(label: str, value: object, note: str) -> str:
        return (
            '<div class="mapping-funnel-card">'
            f'<span>{escape_html(label)}</span><b>{escape_html(int(value or 0))}</b>'
            f'<small>{escape_html(note)}</small></div>'
        )

    return (
        '<section class="mapping-funnel">'
        '<div class="mapping-funnel-title"><b>映射资格漏斗</b>'
        '<span>先按信号检查冻结关系，再单列映射对和到期结果。</span></div>'
        '<div class="mapping-funnel-label">信号单位</div>'
        '<div class="mapping-funnel-grid">'
        + card("最终入选", signals.get("selected"), "当前精确契约")
        + card(
            "无有效冻结关系",
            signals.get("without_valid_frozen_mapping"),
            "保持缺失，不反向补写",
        )
        + card("仅概念关系", signals.get("concept_only"), "不进入正式方向分母")
        + card("正式个股映射", signals.get("formal_mapped"), "排除仅概念关系")
        + '</div><div class="mapping-funnel-label">映射对与事件结果（不同计量单位）</div>'
        '<div class="mapping-funnel-grid">'
        + card("正式映射对", pairs.get("formal"), "direct/upstream/downstream/ecosystem")
        + card("有方向假设", pairs.get("directional"), "正式映射对的子集")
        + card("时点证据支持", pairs.get("evidence_backed"), "与方向假设正交")
        + card(
            "T+1已结算方向",
            t1.get("directional_complete_pairs"),
            f"{int(t1.get('independent_events') or 0)}事件 / {int(t1.get('independent_result_days') or 0)}结果日",
        )
        + '</div><div class="mapping-funnel-note">'
        f'{escape_html(funnel.get("note") or "")} '
        f'T+1到期/完成 {escape_html(int(t1.get("due_pairs") or 0))}/'
        f'{escape_html(int(t1.get("complete_pairs") or 0))}；概念映射对排除 '
        f'{escape_html(int(pairs.get("concept_excluded") or 0))}。</div></section>'
    )


def render_mapping_validation_compact_html(result: dict[str, Any]) -> str:
    enabled = bool(result.get("enabled"))
    formal_pairs = int(result.get("formal_pairs") or 0)
    directional_pairs = int(result.get("directional_pairs") or 0)
    t1 = next(
        (row for row in result.get("horizons") or [] if row.get("horizon") == "T+1"),
        {},
    )
    if result.get("status") == "data_failed":
        summary = "后验只读链路失败 · 已停止发布结论"
        color = "#ef4444"
    elif not enabled:
        summary = "前瞻冻结账本尚未初始化"
        color = "#a1a1aa"
    elif formal_pairs == 0:
        summary = "前瞻冻结已启用 · 新样本 0"
        color = "#fbbf24"
    else:
        summary = (
            f"明确映射 {formal_pairs} · 有方向假设 {directional_pairs} · "
            f"T+1已结算 {int(t1.get('complete_pairs') or 0)}"
        )
        color = "#34d399" if bool(t1.get("publishable")) else "#fbbf24"
    ledger_selected = int(result.get("ledger_selected_signals") or 0)
    current_selected = int(result.get("selected_signals") or 0)
    contract_count = int(result.get("ledger_contract_count") or 0)
    archived_selected = int(result.get("archived_contract_selected_signals") or 0)
    if ledger_selected > 0:
        summary += (
            f"；前瞻账本 {ledger_selected} 条/{contract_count}个契约，"
            f"当前契约 {current_selected} 条"
        )
        if archived_selected > 0:
            summary += f"，旧契约 {archived_selected} 条分版本保留"
    status_label = str(result.get("status_label") or "待积累")
    if status_label == "数据积累中":
        status_label = "当前契约积累中"
    summary_html = (
        '<div class="mapping-validity-compact" style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'margin-top:9px;padding:9px 2px 1px;border-top:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.16);'
        'display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:11px;line-height:1.5;">'
        '<span style="min-width:0;color:var(--color-text-subdue,inherit);">'
        '<b style="color:var(--color-text-base,inherit);margin-right:5px;">个股映射后验</b>'
        f'{escape_html(summary)}；历史关联，不等于语义准确。</span>'
        f'<span style="color:{color};white-space:nowrap;">{escape_html(status_label)}</span>'
        '</div>'
    )
    return summary_html + _render_mapping_optimization_html(result, compact=True)


def render_mapping_validation_html(result: dict[str, Any]) -> str:
    enabled = bool(result.get("enabled"))
    relation_rows = []
    for row in result.get("relation_rows") or []:
        coverage = (
            row.get("evidence_backed_pairs", 0) / row.get("pairs", 1) * 100.0
            if int(row.get("pairs") or 0) > 0
            else None
        )
        relation_rows.append(
            '<div class="mapping-validity-relation">'
            f'<b>{escape_html(row.get("label"))}</b>'
            f'<span>{escape_html(row.get("pairs", 0))}对 · 方向{escape_html(row.get("directional_pairs", 0))} · '
            f'证据{escape_html(_pct(coverage))}</span></div>'
        )
    horizon_rows = []
    for row in result.get("horizons") or []:
        if row.get("publishable"):
            ci = row.get("excess_hit_rate_ci95") or {}
            association = (
                f"{_pct(row.get('excess_hit_rate'))} "
                f"(95%CI {ci.get('lower', '—')}–{ci.get('upper', '—')}%)"
            )
            excess = f"均值 {_pct(row.get('mean_signed_excess'), 3)}"
        elif result.get("status") == "data_failed":
            association = "读取失败"
            excess = "停止发布"
        else:
            association = "待积累"
            excess = "暂不展示"
        horizon_rows.append(
            '<div class="mapping-validity-horizon">'
            f'<b>{escape_html(row.get("horizon"))}</b>'
            f'<span>到期/结算 {escape_html(row.get("due_pairs", 0))}/{escape_html(row.get("complete_pairs", 0))}</span>'
            f'<span>已结算方向 n={escape_html(row.get("directional_complete_pairs", 0))} · '
            f'{escape_html(row.get("independent_events", 0))}事件 · '
            f'{escape_html(row.get("independent_result_days", 0))}日</span>'
            f'<span>{escape_html(association)}</span><span>{escape_html(excess)}</span>'
            '</div>'
        )
    if not horizon_rows:
        horizon_rows.append(
            '<div class="mapping-validity-empty">前瞻冻结已启用，等待新的最终入选映射形成。</div>'
        )
    started = (
        _market_date(result.get("started_at")) if result.get("started_at") else "—"
    )
    status_color = (
        "#34d399"
        if result.get("status") == "threshold_met"
        else "#ef4444"
        if result.get("status") == "data_failed"
        else "#fbbf24"
    )
    if not enabled:
        status_color = "#a1a1aa"
    status_label = str(result.get("status_label") or "待积累")
    if status_label == "数据积累中":
        status_label = "当前契约积累中"
    cohort_history = _render_mapping_contract_ledger(result)
    return (
        '<section class="mapping-validity" style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        'margin-top:14px;padding:13px 2px 5px;border-top:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.18);">'
        '<style>'
        '.mapping-validity *{box-sizing:border-box}.mapping-validity-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:10px}'
        '.mapping-validity-sub{font-size:11px;color:var(--color-text-subdue,inherit);line-height:1.5;margin-top:3px}'
        '.mapping-validity-badge{font-size:11px;padding:4px 8px;border-radius:999px;white-space:nowrap}'
        '.mapping-validity-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-bottom:9px}'
        '.mapping-validity-metric,.mapping-validity-relation{padding:7px 8px;border-radius:9px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.28);min-width:0}'
        '.mapping-validity-metric span,.mapping-validity-relation span{display:block;font-size:10px;color:var(--color-text-subdue,inherit);margin-bottom:2px}'
        '.mapping-validity-metric b,.mapping-validity-relation b{font-size:13px;color:var(--color-text-base,inherit);overflow-wrap:anywhere}'
        '.mapping-validity-relations{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:8px 0}'
        '.mapping-validity-relation span{margin:3px 0 0;line-height:1.35}'
        '.mapping-validity-horizon{display:grid;grid-template-columns:.45fr .9fr 1.3fr 1.25fr .75fr;gap:7px;align-items:center;padding:8px 9px;border-radius:8px;margin-bottom:5px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 2%),.24);font-size:11px;line-height:1.45;color:var(--color-text-subdue,inherit)}'
        '.mapping-validity-horizon b{color:var(--color-text-base,inherit)}.mapping-validity-empty{font-size:11px;color:var(--color-text-subdue,inherit);padding:9px;border:1px dashed hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.18);border-radius:8px}'
        '.mapping-validity-note{font-size:11px;color:var(--color-text-subdue,inherit);line-height:1.6;margin-top:8px}'
        '.mapping-contract-ledger{margin:0 0 10px;border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.14);border-radius:10px;overflow:hidden}'
        '.mapping-contract-title{display:flex;justify-content:space-between;gap:10px;padding:8px 10px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.28);font-size:11px;line-height:1.5}.mapping-contract-title span{color:var(--color-text-subdue,inherit);text-align:right}'
        '.mapping-contract-row{display:grid;grid-template-columns:minmax(190px,1.35fr) minmax(75px,.55fr) minmax(85px,.65fr) minmax(180px,1.2fr);gap:8px;align-items:center;padding:8px 9px;border-top:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.1)}'
        '.mapping-contract-id,.mapping-contract-stat{min-width:0}.mapping-contract-id span{display:inline-block;margin-right:5px;padding:2px 5px;border:1px solid;border-radius:999px;font-size:10px;vertical-align:1px}.mapping-contract-id b,.mapping-contract-stat b{font-size:11px;color:var(--color-text-base,inherit);overflow-wrap:anywhere}.mapping-contract-id small,.mapping-contract-stat span,.mapping-contract-stat small{display:block;margin-top:2px;font-size:10px;line-height:1.4;color:var(--color-text-subdue,inherit)}'
        '.mapping-contract-history{border-top:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.12)}.mapping-contract-history summary{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:7px;padding:8px 10px;cursor:pointer;font-size:11px;line-height:1.5;color:var(--color-text-base,inherit);list-style:none}.mapping-contract-history summary::-webkit-details-marker{display:none}.mapping-contract-history summary::before{content:"▸";color:var(--color-text-subdue,inherit)}.mapping-contract-history[open] summary::before{content:"▾"}.mapping-contract-history summary span{color:var(--color-text-subdue,inherit);text-align:right;font-weight:400}.mapping-contract-history summary:focus-visible{outline:2px solid var(--color-primary,#60a5fa);outline-offset:-2px}.mapping-contract-history[open] summary{background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.2)}'
        '.mapping-funnel{margin:0 0 10px;padding:9px 10px;border-radius:10px;border:1px solid hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 18%),.14)}.mapping-funnel-title{display:flex;justify-content:space-between;gap:8px;font-size:11px;line-height:1.45}.mapping-funnel-title span,.mapping-funnel-label,.mapping-funnel-note{color:var(--color-text-subdue,inherit)}.mapping-funnel-label{font-size:10px;font-weight:650;margin:8px 0 4px}.mapping-funnel-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}.mapping-funnel-card{min-width:0;padding:6px 7px;border-radius:8px;background:hsla(var(--bgh),var(--bgs),calc(var(--bgl) + 3%),.24)}.mapping-funnel-card span,.mapping-funnel-card small{display:block;font-size:10px;line-height:1.4;color:var(--color-text-subdue,inherit);overflow-wrap:anywhere}.mapping-funnel-card b{display:block;font-size:13px;color:var(--color-text-base,inherit);margin:2px 0}.mapping-funnel-note{font-size:10px;line-height:1.5;margin-top:7px}'
        '@media(max-width:520px){.mapping-validity-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.mapping-validity-relations{grid-template-columns:1fr 1fr}.mapping-validity-horizon{grid-template-columns:.5fr 1fr 1.4fr}.mapping-validity-horizon span:nth-last-child(-n+2){display:none}.mapping-contract-title{display:block}.mapping-contract-title span{display:block;text-align:left;margin-top:2px}.mapping-contract-history summary{grid-template-columns:auto minmax(0,1fr)}.mapping-contract-history summary span{grid-column:2;text-align:left}.mapping-contract-row,.mapping-funnel-grid{grid-template-columns:1fr 1fr}.mapping-contract-id{grid-column:1/-1}.mapping-contract-stat:last-child{grid-column:1/-1}}'
        '</style>'
        '<div class="mapping-validity-head"><div>'
        '<div style="font-size:14px;font-weight:850;color:var(--color-text-base,inherit);">个股映射后验关联</div>'
        f'<div class="mapping-validity-sub">前瞻冻结起始 {escape_html(started)} · 当前策略 {escape_html(result.get("strategy_version") or "—")} · '
        f'映射契约 {escape_html(result.get("mapping_version") or "—")}</div></div>'
        f'<div class="mapping-validity-badge" style="color:{status_color};border:1px solid {status_color};">'
        f'{escape_html(status_label)}</div></div>'
        f'{_render_mapping_optimization_html(result, compact=False)}'
        f'{cohort_history}'
        f'{_render_mapping_eligibility_funnel_html(result)}'
        '<div class="mapping-validity-metrics">'
        f'<div class="mapping-validity-metric"><span>最终入选信号</span><b>{escape_html(result.get("selected_signals", 0))}</b></div>'
        f'<div class="mapping-validity-metric"><span>明确映射对</span><b>{escape_html(result.get("formal_pairs", 0))}</b></div>'
        f'<div class="mapping-validity-metric"><span>有方向假设</span><b>{escape_html(result.get("directional_pairs", 0))}</b></div>'
        f'<div class="mapping-validity-metric"><span>证据完备率</span><b>{escape_html(_pct(result.get("evidence_coverage")))}</b></div>'
        '</div>'
        f'<div class="mapping-validity-sub">明确映射覆盖 {escape_html(_pct(result.get("formal_mapping_coverage")))} · '
        f'{escape_html(result.get("unique_events", 0))}个独立事件 · {escape_html(result.get("unique_securities", 0))}只股票 · '
        f'概念候选排除 {escape_html(result.get("concept_pairs_excluded", 0))} 对</div>'
        f'<div class="mapping-validity-relations">{"".join(relation_rows)}</div>'
        f'{"".join(horizon_rows)}'
        '<div class="mapping-validity-note">'
        f'发布门槛：已结算方向样本≥{escape_html(result.get("min_mapping_samples", _MIN_MAPPING_SAMPLES))}、'
        f'已结算独立事件≥{escape_html(result.get("min_independent_events", _MIN_INDEPENDENT_EVENTS))}、'
        f'独立结果交易日≥{escape_html(result.get("min_independent_result_days", _MIN_INDEPENDENT_RESULT_DAYS))}、'
        f'已结算证券≥{escape_html(result.get("min_securities", _MIN_SECURITIES))}；未达门槛不展示命中率和收益。'
        f' 已排除 {escape_html(result.get("legacy_v4_outcomes_excluded", 0))} 条映射未冻结的旧 V4 结果。'
        '“证据完备”只表示映射有当时可见的支持材料；市场表现是后验关联，不证明因果、荐股价值或语义映射正确。'
        '真正的映射 precision/recall 仍需人工标注；Wilson 区间未校正同一结果日跨事件相关性，'
        '候选规则还须按结果日分块留出验证。 '
        '<a href="/events" style="color:var(--color-primary,inherit);text-decoration:none;">查看映射样本 →</a>'
        '</div></section>'
    )


__all__ = [
    "render_mapping_validation_compact_html",
    "render_mapping_validation_html",
    "run_mapping_validation",
]
