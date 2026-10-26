"""Read-only projection for immutable score-contract bridge manifests.

The bridge is deliberately a presentation-only cohort.  It never returns
articles, outcomes, or booleans that can be consumed by the score/control
gates.  Missing tables and malformed manifests fail closed to ``None`` so
deployments without bridge storage retain the existing model-quality response
exactly.  Complete pending manifests are visible because the writer is
intentionally preview-only and cannot approve its own output; their formal
eligibility remains false.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from db import database as DB


logger = logging.getLogger(__name__)

_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_RUN_TABLE = "score_bridge_runs"
_MEMBER_TABLE = "score_bridge_members"
_EXPLICIT_GPT_REASON = "explicit_gpt_participation"

_REQUIRED_RUN_COLUMNS = frozenset(
    {
        "run_id",
        "source_strategy_version",
        "target_strategy_version",
        "source_mapping_version",
        "target_mapping_version",
        "policy_version",
        "mode",
        "approval_status",
        "approved_at",
        "approved_by",
        "created_at",
        "source_row_count",
        "eligible_count",
        "ineligible_count",
        "unknown_count",
        "manifest_hash",
    }
)
_REQUIRED_MEMBER_COLUMNS = frozenset(
    {
        "run_id",
        "ledger_signal_id",
        "source_signal_key",
        "source_available_at",
        "selected",
        "eligibility",
        "reason_code",
        "stage_metadata_json",
        "output_hash",
        "evidence_hash",
    }
)


def _table_columns(connection: Any, table: str) -> frozenset[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return frozenset(str(row[1]) for row in rows)


def _schema_available(connection: Any) -> bool:
    return (
        _REQUIRED_RUN_COLUMNS.issubset(_table_columns(connection, _RUN_TABLE))
        and _REQUIRED_MEMBER_COLUMNS.issubset(
            _table_columns(connection, _MEMBER_TABLE)
        )
    )


def _preview_runs(
    connection: Any,
    *,
    target_strategy_version: str,
    target_mapping_version: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT run_id, source_strategy_version, target_strategy_version, "
        "source_mapping_version, target_mapping_version, policy_version, mode, "
        "approval_status, approved_at, approved_by, created_at, "
        "source_row_count, eligible_count, ineligible_count, unknown_count, "
        "manifest_hash FROM score_bridge_runs "
        "WHERE target_strategy_version = ? AND target_mapping_version = ? "
        "AND mode = 'preview_only' "
        "AND approval_status IN ('pending', 'approved') "
        "AND trim(manifest_hash) <> '' "
        "ORDER BY created_at DESC, approved_at DESC, run_id DESC",
        (target_strategy_version, target_mapping_version),
    ).fetchall()

    # A newer immutable rerun supersedes an earlier manifest for the same
    # source contract.  Keeping only the latest avoids double-counting.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        source_contract = (
            str(row.get("source_strategy_version") or "").strip(),
            str(row.get("source_mapping_version") or "").strip(),
        )
        target_contract = (
            str(row.get("target_strategy_version") or "").strip(),
            str(row.get("target_mapping_version") or "").strip(),
        )
        if (
            not all(source_contract)
            or not all(target_contract)
            or source_contract == target_contract
        ):
            continue
        latest.setdefault(source_contract, row)
    return list(latest.values())


def _runs_data_version(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "none"
    identities = sorted(
        "\0".join(
            (
                str(row.get("run_id") or ""),
                str(row.get("manifest_hash") or ""),
                str(row.get("approved_at") or ""),
                str(row.get("policy_version") or ""),
            )
        )
        for row in runs
    )
    payload = "\n".join(identities).encode("utf-8", errors="strict")
    return hashlib.sha256(payload).hexdigest()[:20]


def bridge_preview_data_version(
    target_strategy_version: str,
    target_mapping_version: str,
) -> str:
    """Return a stable cache discriminator for visible preview manifests."""

    strategy = str(target_strategy_version or "").strip()
    mapping = str(target_mapping_version or "").strip()
    if not strategy or not mapping:
        return "none"
    try:
        with DB.db_lock:
            with DB.get_connection(configure_storage=False) as connection:
                if not _schema_available(connection):
                    return "none"
                runs = _preview_runs(
                    connection,
                    target_strategy_version=strategy,
                    target_mapping_version=mapping,
                )
                valid_runs = [
                    run
                    for run in runs
                    if _validated_run_members(connection, run) is not None
                ]
                return _runs_data_version(valid_runs)
    except Exception:
        # This discriminator is an optional read boundary.  A database/schema
        # failure must bypass the preview, not take down the quality endpoint.
        logger.warning("Score bridge preview cache version unavailable", exc_info=True)
        return "none"


def _signal_day(timestamp: int) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=_MARKET_TZ).date().isoformat()


def _validated_run_members(
    connection: Any, run: dict[str, Any]
) -> list[dict[str, Any]] | None:
    rows = connection.execute(
        "SELECT m.run_id, m.ledger_signal_id, m.source_signal_key, "
        "m.source_available_at, m.selected AS bridge_selected, m.eligibility, "
        "m.reason_code, m.stage_metadata_json, m.output_hash, m.evidence_hash, "
        "s.strategy_version, s.mapping_version, s.available_at, "
        "s.selected AS ledger_selected, s.is_similarity_copy "
        "FROM score_bridge_members AS m "
        "JOIN backtest_signals AS s ON s.id = m.ledger_signal_id "
        "WHERE m.run_id = ? ORDER BY m.ledger_signal_id",
        (str(run.get("run_id") or ""),),
    ).fetchall()
    members = [dict(row) for row in rows]
    expected_total = int(run.get("source_row_count") or 0)
    expected_counts = {
        "eligible": int(run.get("eligible_count") or 0),
        "ineligible": int(run.get("ineligible_count") or 0),
        "unknown": int(run.get("unknown_count") or 0),
    }
    if expected_total <= 0 or expected_total != sum(expected_counts.values()):
        return None
    if len(members) != expected_total:
        return None

    actual_counts: dict[str, int] = defaultdict(int)
    source_strategy = str(run.get("source_strategy_version") or "")
    source_mapping = str(run.get("source_mapping_version") or "")
    for member in members:
        eligibility = str(member.get("eligibility") or "").strip()
        if eligibility not in expected_counts:
            return None
        actual_counts[eligibility] += 1
        try:
            available_at = int(member.get("available_at") or 0)
            source_available_at = int(member.get("source_available_at") or 0)
            bridge_selected = int(member.get("bridge_selected"))
            ledger_selected = int(member.get("ledger_selected"))
        except (TypeError, ValueError):
            return None
        if (
            str(member.get("strategy_version") or "") != source_strategy
            or str(member.get("mapping_version") or "") != source_mapping
            or available_at <= 0
            or source_available_at != available_at
            or bridge_selected not in {0, 1}
            or bridge_selected != ledger_selected
            or not str(member.get("source_signal_key") or "").strip()
            or not str(member.get("output_hash") or "").strip()
            or not str(member.get("evidence_hash") or "").strip()
        ):
            return None
        if eligibility == "eligible" and bool(member.get("is_similarity_copy")):
            return None
    if any(actual_counts[key] != value for key, value in expected_counts.items()):
        return None
    return members


def load_bridge_preview(
    target_strategy_version: str,
    target_mapping_version: str,
) -> dict[str, Any] | None:
    """Load point-in-time compatible manifest rows for display only.

    The return shape intentionally contains no posterior metrics and marks all
    gate effects false.  Callers must not merge these counts into exact-current
    articles, control cohorts, optimization state, or mapping validation.
    """

    strategy = str(target_strategy_version or "").strip()
    mapping = str(target_mapping_version or "").strip()
    if not strategy or not mapping:
        return None
    try:
        with DB.db_lock:
            with DB.get_connection(configure_storage=False) as connection:
                if not _schema_available(connection):
                    return None
                runs = _preview_runs(
                    connection,
                    target_strategy_version=strategy,
                    target_mapping_version=mapping,
                )
                if not runs:
                    return None

                valid: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
                for run in runs:
                    members = _validated_run_members(connection, run)
                    if members is None:
                        logger.warning(
                            "Ignoring malformed score bridge preview run_id=%s",
                            run.get("run_id"),
                        )
                        continue
                    valid.append((run, members))
    except Exception:
        logger.warning("Score bridge preview unavailable", exc_info=True)
        return None

    if not valid:
        return None

    source_total = 0
    candidate = 0
    ineligible = 0
    unknown = 0
    explicit_gpt = 0
    selected_candidate = 0
    signal_days: set[str] = set()
    source_contracts: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    valid_runs: list[dict[str, Any]] = []
    for run, members in valid:
        valid_runs.append(run)
        run_candidate = int(run.get("eligible_count") or 0)
        run_ineligible = int(run.get("ineligible_count") or 0)
        run_unknown = int(run.get("unknown_count") or 0)
        run_selected_candidate = sum(
            1
            for member in members
            if member.get("eligibility") == "eligible"
            and int(member.get("bridge_selected") or 0) == 1
        )
        run_explicit_gpt = sum(
            1
            for member in members
            if member.get("eligibility") == "ineligible"
            and str(member.get("reason_code") or "") == _EXPLICIT_GPT_REASON
        )
        for member in members:
            if member.get("eligibility") != "eligible":
                continue
            day = _signal_day(int(member.get("source_available_at") or 0))
            if day:
                signal_days.add(day)
        source_total += int(run.get("source_row_count") or 0)
        candidate += run_candidate
        ineligible += run_ineligible
        unknown += run_unknown
        explicit_gpt += run_explicit_gpt
        selected_candidate += run_selected_candidate
        source_contracts.append(
            {
                "strategy_version": str(run.get("source_strategy_version") or ""),
                "mapping_version": str(run.get("source_mapping_version") or ""),
                "source_total": int(run.get("source_row_count") or 0),
                "candidate": run_candidate,
                "explicit_gpt": run_explicit_gpt,
                "unknown": run_unknown,
                "selected_candidate": run_selected_candidate,
            }
        )
        manifests.append(
            {
                "run_id": str(run.get("run_id") or ""),
                "policy_version": str(run.get("policy_version") or ""),
                "approval_status": str(run.get("approval_status") or "pending"),
                "approved_at": int(run.get("approved_at") or 0),
                "approved_by": str(run.get("approved_by") or ""),
                "manifest_hash": str(run.get("manifest_hash") or ""),
            }
        )

    return {
        "mode": "preview_only",
        "scope": "audited_compatibility_preview",
        "target_strategy_version": strategy,
        "target_mapping_version": mapping,
        "data_version": _runs_data_version(valid_runs),
        "manifest_count": len(valid),
        "approval_required": any(
            str(run.get("approval_status") or "pending") == "pending"
            for run, _members in valid
        ),
        "source_total": source_total,
        "candidate": candidate,
        "explicit_gpt": explicit_gpt,
        "ineligible": ineligible,
        "unknown": unknown,
        "selected_candidate": selected_candidate,
        "candidate_signal_days": len(signal_days),
        "source_contracts": source_contracts,
        "manifests": manifests,
        "formal_eligible": False,
        "included_in_current_gate": False,
        "formal_gate_effect": False,
        "posterior_gate_effect": False,
        "control_gate_effect": False,
        "candidate_gate_effect": False,
        "auto_apply": False,
        "note": (
            "固定兼容候选清单仅作旁路预览；不进入当前精确契约的后验、"
            "全分数对照、候选优化或映射门槛。"
        ),
    }
