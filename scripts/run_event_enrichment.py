#!/usr/bin/env python3
"""Backfill real-equity mappings and run the stable event graph."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import database
from core.config import load_config
from services.admission_fence import guarded
from services.event_enrichment import (
    enrich_event_security_mappings,
    event_mapping_migration_audit_status,
    event_mapping_migration_status,
    resolve_event_graph,
    run_event_enrichment_lanes,
)


_MIGRATION_BLOCKING_COUNTS = (
    "old_current_mappings",
    "pending_evidence",
    "dirty_groups",
    "exhausted_groups",
)
_RUN_BLOCKING_COUNTS = (
    "deferred_groups",
    "failed_evidence",
    "unreplayable_groups",
    "exhausted_groups",
)
_MIGRATION_AUDIT_COUNTS = (
    "active_equities",
    "input_evidence",
    "old_current_mappings",
    "old_current_groups",
    "pending_evidence",
    "pending_groups",
    "dirty_groups",
    "mapping_dirty_groups",
    "exhausted_groups",
    "similarity_dirty_groups",
    "terminal_evidence",
    "current_rule_mappings",
    "merge_history_count",
    "orphan_current_groups",
    "foreign_key_violations",
)
_MIGRATION_AUDIT_SHA256_FIELDS = (
    "security_master_fingerprint",
    "input_snapshot_sha256",
    "similarity_dirty_snapshot_sha256",
    "terminal_snapshot_sha256",
    "current_mapping_snapshot_sha256",
    "merge_history_snapshot_sha256",
)

logger = logging.getLogger(__name__)

_GRAPH_RECONCILIATION_COUNTS = (
    "deferred_merges",
    "suppressed_merges",
    "pending_similarity_writes",
    "pending_similarity_deletes",
    "pending_similarity_migrations",
    "pending_similarity_updates",
    "deferred_similarities",
    "deferred_similarity_deletes",
    "deferred_similarity_migrations",
    "deferred_similarity_updates",
)
_MERGE_DRAIN_COUNTS = (
    "qualified_merges",
    "merged",
    "deferred_merges",
    "suppressed_merges",
)


def _migration_failure(
    *,
    phase: str,
    failure_code: str,
    error_type: str = "",
    audit: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "failed",
        "mapping_scope": "all_history",
        "migration_phase": phase,
        "migration_outcome": "failed",
        "migration_complete": False,
        "failure_code": failure_code,
    }
    if error_type:
        payload["error_type"] = error_type
    if isinstance(audit, dict):
        payload["audit_after"] = audit
        payload["mapping_contract"] = str(audit.get("mapping_contract") or "")
        payload["input_snapshot_sha256"] = str(
            audit.get("input_snapshot_sha256") or ""
        )
    return payload


def _migration_preflight_failure(
    audit: object,
    *,
    expected_contract: str,
    expected_input_snapshot: str,
    require_mapping_complete: bool,
) -> str:
    if not isinstance(audit, dict):
        return "migration_audit_invalid"
    contract = audit.get("mapping_contract")
    input_snapshot = audit.get("input_snapshot_sha256")
    if not isinstance(contract, str) or not contract.startswith("asset-map:"):
        return "mapping_contract_invalid"
    if not isinstance(input_snapshot, str) or not _is_sha256(input_snapshot):
        return "mapping_input_snapshot_invalid"
    if expected_contract and contract != expected_contract:
        return "mapping_contract_changed"
    if expected_input_snapshot and input_snapshot != expected_input_snapshot:
        return "mapping_input_changed"
    counts = {
        key: audit.get(key)
        for key in _MIGRATION_AUDIT_COUNTS
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        return "migration_audit_invalid"
    if any(
        not isinstance(audit.get(field), str)
        or not _is_sha256(str(audit.get(field)))
        for field in _MIGRATION_AUDIT_SHA256_FIELDS
    ):
        return "migration_audit_invalid"
    if not isinstance(audit.get("migration_complete"), bool):
        return "migration_audit_invalid"
    if counts["active_equities"] <= 0:
        return "security_master_empty"
    if counts["orphan_current_groups"]:
        return "mapping_orphan_groups_present"
    if counts["foreign_key_violations"]:
        return "mapping_foreign_key_violation"
    if counts["exhausted_groups"]:
        return "mapping_exhausted_groups_present"
    if require_mapping_complete and audit.get("migration_complete") is not True:
        return "mapping_phase_incomplete"
    return ""


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _audit_identity_changed(
    before: dict[str, object], after: dict[str, object]
) -> str:
    for field, failure_code in (
        ("mapping_contract", "mapping_contract_changed"),
        ("active_equities", "security_master_changed"),
        ("security_master_fingerprint", "security_master_changed"),
        ("input_snapshot_sha256", "mapping_input_changed"),
        ("input_evidence", "mapping_input_changed"),
        ("merge_history_count", "event_merge_history_changed"),
        ("merge_history_snapshot_sha256", "event_merge_history_changed"),
    ):
        if before.get(field) != after.get(field):
            return failure_code
    return ""


def _run_migration_audit() -> tuple[int, dict[str, object]]:
    """Pin the candidate image contract and frozen input before any replay write."""
    try:
        audit = event_mapping_migration_audit_status(read_only=True)
    except Exception as exc:
        return 2, _migration_failure(
            phase="audit",
            failure_code="migration_audit_failed",
            error_type=type(exc).__name__,
        )
    failure = _migration_preflight_failure(
        audit,
        expected_contract="",
        expected_input_snapshot="",
        require_mapping_complete=False,
    )
    if failure:
        return 2, _migration_failure(
            phase="audit",
            failure_code=failure,
            audit=audit,
        )
    return 0, {
        "status": "ok",
        "mapping_scope": "all_history",
        "migration_phase": "audit",
        "migration_outcome": "ready",
        "migration_complete": False,
        "mapping_contract": str(audit["mapping_contract"]),
        "input_snapshot_sha256": str(audit["input_snapshot_sha256"]),
        "point_of_no_return": False,
        "mapping_migration": audit,
    }


def _run_mapping_migration_batch(
    *,
    limit: int,
    expected_contract: str,
    expected_input_snapshot: str,
) -> tuple[int, dict[str, object]]:
    try:
        before = event_mapping_migration_audit_status()
    except Exception as exc:
        return 2, _migration_failure(
            phase="mapping",
            failure_code="migration_audit_failed",
            error_type=type(exc).__name__,
        )
    preflight_failure = _migration_preflight_failure(
        before,
        expected_contract=expected_contract,
        expected_input_snapshot=expected_input_snapshot,
        require_mapping_complete=False,
    )
    if preflight_failure:
        return 2, _migration_failure(
            phase="mapping",
            failure_code=preflight_failure,
            audit=before,
        )
    try:
        mappings = enrich_event_security_mappings(since=0, limit=limit, include_legacy=True)
    except Exception as exc:
        try:
            after = event_mapping_migration_audit_status()
        except Exception:
            after = before
        return 2, _migration_failure(
            phase="mapping",
            failure_code="mapping_batch_failed",
            error_type=type(exc).__name__,
            audit=after,
        )
    try:
        after = event_mapping_migration_audit_status()
    except Exception as exc:
        return 2, _migration_failure(
            phase="mapping",
            failure_code="migration_audit_failed",
            error_type=type(exc).__name__,
            audit=before,
        )
    identity_failure = _audit_identity_changed(before, after)
    if identity_failure:
        return 2, _migration_failure(
            phase="mapping",
            failure_code=identity_failure,
            audit=after,
        )
    postflight_failure = _migration_preflight_failure(
        after,
        expected_contract=str(before["mapping_contract"]),
        expected_input_snapshot=str(before["input_snapshot_sha256"]),
        require_mapping_complete=False,
    )
    if postflight_failure:
        return 2, _migration_failure(
            phase="mapping",
            failure_code=postflight_failure,
            audit=after,
        )
    if mappings.get("mapping_contract") != before.get("mapping_contract"):
        return 2, _migration_failure(
            phase="mapping",
            failure_code="mapping_contract_changed",
            audit=after,
        )
    try:
        run_failures = {
            key: int(mappings[key])
            for key in ("failed_evidence", "unreplayable_groups", "exhausted_groups")
        }
    except (KeyError, TypeError, ValueError):
        return 2, _migration_failure(
            phase="mapping",
            failure_code="mapping_batch_result_invalid",
            audit=after,
        )
    if any(value < 0 for value in run_failures.values()):
        return 2, _migration_failure(
            phase="mapping",
            failure_code="mapping_batch_result_invalid",
            audit=after,
        )
    if any(run_failures.values()) or int(after.get("exhausted_groups") or 0):
        return 2, _migration_failure(
            phase="mapping",
            failure_code="mapping_batch_degraded",
            audit=after,
        )
    migration_complete = bool(after.get("migration_complete")) and all(
        int(after.get(key) or 0) == 0 for key in _MIGRATION_BLOCKING_COUNTS
    )
    before_remaining = sum(
        int(before.get(key) or 0) for key in _MIGRATION_BLOCKING_COUNTS[:-1]
    )
    after_remaining = sum(
        int(after.get(key) or 0) for key in _MIGRATION_BLOCKING_COUNTS[:-1]
    )
    wrote_mapping_state = bool(
        int(mappings.get("processed_evidence") or 0)
        or int(mappings.get("retired_mappings") or 0)
        or before.get("terminal_snapshot_sha256")
        != after.get("terminal_snapshot_sha256")
        or before.get("current_mapping_snapshot_sha256")
        != after.get("current_mapping_snapshot_sha256")
    )
    if not migration_complete and after_remaining >= before_remaining:
        return 2, {
            **_migration_failure(
                phase="mapping",
                failure_code="mapping_batch_stalled",
                audit=after,
            ),
            "audit_before": before,
            "mappings": mappings,
            "point_of_no_return": wrote_mapping_state,
        }
    outcome = "mapping_complete" if migration_complete else "progress"
    payload: dict[str, object] = {
        "status": "ok",
        "mapping_scope": "all_history",
        "migration_phase": "mapping",
        "migration_outcome": outcome,
        "migration_complete": False,
        "mapping_contract": str(after["mapping_contract"]),
        "input_snapshot_sha256": str(after["input_snapshot_sha256"]),
        "point_of_no_return": wrote_mapping_state,
        "mappings": mappings,
        "mapping_migration": after,
        "similarity_replay": {
            "dirty_groups": int(after.get("similarity_dirty_groups") or 0),
            "snapshot_sha256": str(
                after.get("similarity_dirty_snapshot_sha256") or ""
            ),
        },
        "audit_before": before,
        "audit_after": after,
    }
    return (0 if migration_complete else 1), payload


def _run_merge_drain_batch(*, max_pairs: int) -> tuple[int, dict[str, object]]:
    """Drain all-history hard merges before the mapping replay boundary."""
    try:
        graph = resolve_event_graph(
            since=0,
            max_pairs=max_pairs,
            allow_merges=True,
        )
    except Exception as exc:
        return 2, {
            **_migration_failure(
                phase="merge_drain",
                failure_code="merge_drain_failed",
                error_type=type(exc).__name__,
            ),
            "point_of_no_return": False,
        }

    try:
        merge_counts: dict[str, int] = {}
        for key in _MERGE_DRAIN_COUNTS:
            value = graph[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(key)
            merge_counts[key] = value
    except (KeyError, TypeError, ValueError):
        return 2, {
            **_migration_failure(
                phase="merge_drain",
                failure_code="merge_drain_batch_result_invalid",
            ),
            "point_of_no_return": False,
            "graph": graph,
        }

    qualified = merge_counts["qualified_merges"]
    merged = merge_counts["merged"]
    deferred = merge_counts["deferred_merges"]
    suppressed = merge_counts["suppressed_merges"]
    if (
        graph.get("merges_allowed") is not True
        or suppressed != 0
        or merged > qualified
        or deferred > qualified
        or merged + deferred > qualified
    ):
        return 2, {
            **_migration_failure(
                phase="merge_drain",
                failure_code="merge_drain_batch_result_invalid",
            ),
            "point_of_no_return": False,
            "graph": graph,
        }

    if qualified and not merged:
        return 2, {
            **_migration_failure(
                phase="merge_drain",
                failure_code="merge_drain_stalled",
            ),
            "point_of_no_return": False,
            "graph": graph,
        }

    complete = qualified == merged == deferred == suppressed == 0
    payload: dict[str, object] = {
        "status": "ok",
        "mapping_scope": "all_history",
        "migration_phase": "merge_drain",
        "migration_outcome": "complete" if complete else "progress",
        "migration_complete": complete,
        "point_of_no_return": False,
        "graph": graph,
    }
    # A batch that merged anything is never completion evidence. The host must
    # observe a subsequent all-zero pass over the post-merge canonical graph.
    return (0 if complete else 1), payload


def _run_similarity_migration_batch(
    *,
    max_pairs: int,
    expected_contract: str,
    expected_input_snapshot: str,
) -> tuple[int, dict[str, object]]:
    try:
        before = event_mapping_migration_audit_status()
    except Exception as exc:
        return 2, _migration_failure(
            phase="similarity",
            failure_code="migration_audit_failed",
            error_type=type(exc).__name__,
        )
    preflight_failure = _migration_preflight_failure(
        before,
        expected_contract=expected_contract,
        expected_input_snapshot=expected_input_snapshot,
        require_mapping_complete=True,
    )
    if preflight_failure:
        return 2, _migration_failure(
            phase="similarity",
            failure_code=preflight_failure,
            audit=before,
        )
    try:
        graph = resolve_event_graph(
            since=0,
            max_pairs=max_pairs,
            allow_merges=False,
        )
    except Exception as exc:
        return 2, _migration_failure(
            phase="similarity",
            failure_code="similarity_reconciliation_failed",
            error_type=type(exc).__name__,
            audit=before,
        )
    try:
        after = event_mapping_migration_audit_status()
    except Exception as exc:
        return 2, _migration_failure(
            phase="similarity",
            failure_code="migration_audit_failed",
            error_type=type(exc).__name__,
            audit=before,
        )
    identity_failure = _audit_identity_changed(before, after)
    if identity_failure:
        return 2, _migration_failure(
            phase="similarity",
            failure_code=identity_failure,
            audit=after,
        )
    postflight_failure = _migration_preflight_failure(
        after,
        expected_contract=str(before["mapping_contract"]),
        expected_input_snapshot=str(before["input_snapshot_sha256"]),
        require_mapping_complete=True,
    )
    if postflight_failure:
        return 2, _migration_failure(
            phase="similarity",
            failure_code=postflight_failure,
            audit=after,
        )
    if graph.get("merges_allowed") is not False or int(graph.get("merged") or 0):
        return 2, _migration_failure(
            phase="similarity",
            failure_code="similarity_reconciliation_merged_event",
            audit=after,
        )
    try:
        graph_blockers = {
            key: int(graph[key]) for key in _GRAPH_RECONCILIATION_COUNTS
        }
    except (KeyError, TypeError, ValueError):
        return 2, _migration_failure(
            phase="similarity",
            failure_code="similarity_batch_result_invalid",
            audit=after,
        )
    if any(value < 0 for value in graph_blockers.values()):
        return 2, _migration_failure(
            phase="similarity",
            failure_code="similarity_batch_result_invalid",
            audit=after,
        )
    dirty_groups = int(after.get("similarity_dirty_groups") or 0)
    complete = dirty_groups == 0 and all(value == 0 for value in graph_blockers.values())
    selected_work = int(graph.get("candidate_pairs") or 0)
    if not complete and (
        dirty_groups >= int(before.get("similarity_dirty_groups") or 0)
        and selected_work == 0
    ):
        return 2, {
            **_migration_failure(
                phase="similarity",
                failure_code="similarity_batch_stalled",
                audit=after,
            ),
            "audit_before": before,
            "graph": graph,
            "point_of_no_return": True,
        }
    payload: dict[str, object] = {
        "status": "ok",
        "mapping_scope": "all_history",
        "migration_phase": "similarity",
        "migration_outcome": "complete" if complete else "progress",
        "migration_complete": complete,
        "mapping_contract": str(after["mapping_contract"]),
        "input_snapshot_sha256": str(after["input_snapshot_sha256"]),
        "point_of_no_return": True,
        "graph": graph,
        "mapping_migration": after,
        "similarity_replay": {
            "dirty_groups": dirty_groups,
            "snapshot_sha256": str(
                after.get("similarity_dirty_snapshot_sha256") or ""
            ),
        },
        "audit_before": before,
        "audit_after": after,
    }
    return (0 if complete else 1), payload


def _run_provenance_shadow(
    *,
    evidence_scan_limit: int,
    max_roots: int,
    max_total_evidence: int,
    configure_storage: bool = True,
) -> dict[str, object]:
    """Run the optional projector without entering the event-core outcome.

    Importing lazily keeps the stable runner available while the independently
    deployable sidecar is absent.  Exception messages are deliberately omitted
    because they can contain source material or deployment paths.
    """
    try:
        from services.provenance_shadow import project_provenance_shadow
    except ModuleNotFoundError as exc:
        if exc.name != "services.provenance_shadow":
            logger.error(
                "[ProvenanceShadow] dependency import failed (%s)",
                type(exc).__name__,
            )
            return {
                "status": "degraded",
                "failure_code": "provenance_shadow_import_failed",
                "error_type": type(exc).__name__,
            }
        return {
            "status": "pending",
            "failure_code": "provenance_shadow_not_installed",
        }
    except Exception as exc:
        logger.error(
            "[ProvenanceShadow] import degraded (%s)",
            type(exc).__name__,
        )
        return {
            "status": "degraded",
            "failure_code": "provenance_shadow_import_failed",
            "error_type": type(exc).__name__,
        }

    try:
        config = load_config()
        with database.db_lock:
            with database.get_connection(
                configure_storage=configure_storage
            ) as conn:
                projected = project_provenance_shadow(
                    conn,
                    config=config,
                    evidence_scan_limit=int(evidence_scan_limit),
                    max_roots=int(max_roots),
                    max_total_evidence=int(max_total_evidence),
                )
        if not isinstance(projected, dict):
            raise TypeError("provenance projector returned a non-object result")
        return projected
    except Exception as exc:
        logger.error(
            "[ProvenanceShadow] projector degraded (%s)",
            type(exc).__name__,
        )
        return {
            "status": "degraded",
            "failure_code": "provenance_shadow_projection_failed",
            "error_type": type(exc).__name__,
        }


def _run_requested_lanes(args, graph_cutoff: int, mapping_cutoff: int):
    """Legacy replay is explicit maintenance, never a scheduled default."""
    return run_event_enrichment_lanes(
        since=graph_cutoff,
        mapping_since=mapping_cutoff,
        mapping_limit=args.limit,
        max_pairs=args.max_pairs,
        rebuild_graph_after_mapping=args.mapping_all_history,
        **({"mapping_include_legacy": True} if args.mapping_all_history else {}),
    )


@guarded("event_enrichment", kind="intake")
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=100_000)
    parser.add_argument("--max-pairs", type=int, default=100_000)
    parser.add_argument(
        "--provenance-limit",
        type=int,
        default=2_000,
        help="maximum provenance evidence projected after core lanes",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mapping-all-history",
        action="store_true",
        help=(
            "explicit resumable all-history mapping backfill; also runs the "
            "historical graph before and after mapping. The scheduled cron "
            "does not enable this flag"
        ),
    )
    mode.add_argument(
        "--migration-audit",
        action="store_true",
        help="run the strict content-free prewrite migration audit",
    )
    mode.add_argument(
        "--migration-merge-drain-batch",
        action="store_true",
        help="run one strict all-history hard-merge drain batch",
    )
    mode.add_argument(
        "--migration-mapping-batch",
        action="store_true",
        help="run one strict all-history mapping-only migration batch",
    )
    mode.add_argument(
        "--migration-similarity-batch",
        action="store_true",
        help="reconcile mapping-driven similarities without merging events",
    )
    parser.add_argument(
        "--expected-mapping-contract",
        default="",
        help="fail before replay if the exact mapping contract changed",
    )
    parser.add_argument(
        "--expected-input-snapshot",
        default="",
        help="fail before replay if the frozen event/evidence snapshot changed",
    )
    args = parser.parse_args()
    migration_mode = bool(
        args.migration_audit
        or args.migration_merge_drain_batch
        or args.migration_mapping_batch
        or args.migration_similarity_batch
    )
    if args.migration_merge_drain_batch and (
        args.expected_mapping_contract or args.expected_input_snapshot
    ):
        parser.error(
            "--migration-merge-drain-batch does not accept "
            "--expected-mapping-contract/--expected-input-snapshot"
        )
    if (args.expected_mapping_contract or args.expected_input_snapshot) and not (
        migration_mode
    ):
        parser.error(
            "--expected-mapping-contract/--expected-input-snapshot require a "
            "migration batch mode"
        )
    if args.migration_audit:
        exit_code, payload = _run_migration_audit()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code
    database.init_db()
    if args.migration_merge_drain_batch:
        exit_code, payload = _run_merge_drain_batch(max_pairs=args.max_pairs)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code
    if args.migration_mapping_batch:
        exit_code, payload = _run_mapping_migration_batch(
            limit=args.limit,
            expected_contract=args.expected_mapping_contract,
            expected_input_snapshot=args.expected_input_snapshot,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code
    if args.migration_similarity_batch:
        exit_code, payload = _run_similarity_migration_batch(
            max_pairs=args.max_pairs,
            expected_contract=args.expected_mapping_contract,
            expected_input_snapshot=args.expected_input_snapshot,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code
    cutoff = int(time.time()) - max(1, min(3650, args.days)) * 86400
    graph_cutoff = 0 if args.mapping_all_history else cutoff
    mapping_cutoff = 0 if args.mapping_all_history else cutoff
    result = _run_requested_lanes(args, graph_cutoff, mapping_cutoff)
    result["mapping_scope"] = (
        "all_history" if args.mapping_all_history else "recent_window"
    )
    core_succeeded = result.get("status") == "ok"
    result["provenance_shadow"] = _run_provenance_shadow(
        evidence_scan_limit=200,
        max_roots=200,
        max_total_evidence=args.provenance_limit,
    )
    migration_succeeded = True
    if args.mapping_all_history:
        try:
            migration = event_mapping_migration_status()
        except Exception as exc:
            migration = {
                "status": "failed",
                "failure_code": "mapping_migration_status_failed",
                "error_type": type(exc).__name__,
                "migration_complete": False,
            }
        result["mapping_migration"] = migration
        persistent_state_clear = all(
            key in migration and int(migration.get(key) or 0) == 0
            for key in _MIGRATION_BLOCKING_COUNTS
        )
        mappings = result.get("mappings") or {}
        current_run_clear = all(
            key in mappings and int(mappings.get(key) or 0) == 0
            for key in _RUN_BLOCKING_COUNTS
        )
        migration_succeeded = bool(
            core_succeeded
            and migration.get("migration_complete")
            and persistent_state_clear
            and current_run_clear
        )
        result["migration_complete"] = migration_succeeded
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if core_succeeded and migration_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
