"""Build a conservative, preview-only compatibility manifest from evidence."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Callable, Iterable

from db import database
from scoring_explanation import EXPLANATION_FIELDS
from scoring_provenance import NON_SUBSTANTIVE_SCORE_PATHS


ARTIFACT_SCHEMA_VERSION = "quality-salvage-provenance-v1"
POLICY_VERSION = "score-bridge-preview-v1"
_REQUIRED_TABLES = {
    "meta",
    "signals",
    "cache_provenance",
    "evidence",
    "frozen_mappings",
    "contract_snapshot",
}
# These exact v1 field sets participate in historical hashes. New score
# diagnostics are not recoverable from v1 and must not become required columns.
_OUTPUT_FIELDS = (
    "ledger_signal_id",
    "signal_key",
    "link",
    "event_id",
    "event_version",
    "available_at",
    "scored_at",
    "recorded_at",
    "score",
    "direction",
    "score_confidence",
    "strategy_version",
    "mapping_version",
    "mapped_theme",
    "backtest_symbol",
    "score_path",
    "revision_key",
    "revision_seq",
    "revision_state_key",
    "aggregation_threshold",
    "selected",
    "is_similarity_copy",
)
_PROVENANCE_FIELDS = (
    "raw_title",
    "summary",
    "feed_url",
    "score_path",
    "prompt_key",
    "model_name",
    "stage1_score",
    "stage1_model",
    "stage2_score",
    "stage2_model",
    "score_confidence",
    "review_reason",
    "direction",
    "cache_score",
    "cache_strategy_version",
)


class QualityBridgeError(RuntimeError):
    """The evidence artifact cannot produce a trustworthy preview."""


def _canonical_json(value: object) -> str:
    """Serialize to canonical JSON for hashing (sorted keys)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    """Compute SHA-256 digest of a string."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    """Compute file content hash (SHA-256)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_connection(path: Path) -> sqlite3.Connection:
    """Get database connection for quality artifacts."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise QualityBridgeError("evidence artifact is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise QualityBridgeError("evidence artifact must be a regular non-symlink file")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _decoded_meta(connection: sqlite3.Connection) -> dict[str, object]:
    """Decode metadata from quality artifact."""
    values: dict[str, object] = {}
    for row in connection.execute("SELECT key, value FROM meta ORDER BY key"):
        try:
            values[str(row[0])] = json.loads(str(row[1]))
        except json.JSONDecodeError as exc:
            raise QualityBridgeError("evidence artifact metadata is invalid") from exc
    return values


def _model_is_gpt(value: object) -> bool:
    return "gpt" in str(value or "").strip().casefold()


def _same_number(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError, OverflowError):
        return False


def _classify(
    signal: dict[str, object],
    provenance: dict[str, object] | None,
    source_strategy_version: str,
) -> tuple[str, str]:
    """Classify quality state from metrics."""
    if provenance is not None and any(
        _model_is_gpt(provenance.get(field))
        for field in ("model_name", "stage1_model", "stage2_model")
    ):
        return "ineligible", "explicit_gpt_participation"
    if provenance is None:
        return "unknown", "missing_cache_provenance"
    if str(provenance.get("cache_strategy_version") or "") != source_strategy_version:
        return "unknown", "provenance_strategy_mismatch"
    if not _same_number(provenance.get("cache_score"), signal.get("score")):
        return "unknown", "provenance_output_mismatch"
    if str(provenance.get("direction") or "") != str(signal.get("direction") or ""):
        return "unknown", "provenance_output_mismatch"
    path = str(provenance.get("score_path") or "").strip()
    if path != str(signal.get("score_path") or "").strip():
        return "unknown", "provenance_output_mismatch"
    if path in NON_SUBSTANTIVE_SCORE_PATHS:
        return "unknown", "non_substantive_semantic_resolution"
    selected_model = str(provenance.get("model_name") or "").strip()
    stage1_model = str(provenance.get("stage1_model") or "").strip()
    stage2_model = str(provenance.get("stage2_model") or "").strip()
    if path == "llm" and (
        not selected_model or not stage1_model or selected_model != stage1_model
    ):
        return "unknown", "incomplete_stage_provenance"
    if path == "stage2" and (
        not selected_model
        or not stage1_model
        or not stage2_model
        or selected_model != stage2_model
    ):
        return "unknown", "incomplete_stage_provenance"
    if path == "fallback_local" and selected_model != "fallback_local":
        return "unknown", "incomplete_stage_provenance"
    if path not in {"llm", "stage2", "fallback_local", "local_reject"}:
        return "unknown", "unsupported_score_path"
    return "eligible", "verified_non_gpt_provenance"


def _row_payload(row: sqlite3.Row, fields: Iterable[str]) -> dict[str, object]:
    return {field: row[field] for field in fields}


def _read_artifact(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, object], list[dict[str, object]], dict[int, dict[str, object]]]:
    expected = str(expected_sha256 or "").strip().casefold()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("expected artifact SHA-256 must be 64 lowercase hex characters")
    try:
        info = path.lstat()
    except OSError as exc:
        raise QualityBridgeError("evidence artifact is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise QualityBridgeError("evidence artifact must be a regular non-symlink file")
    actual = _hash_file(path)
    if not hmac.compare_digest(actual, expected):
        raise QualityBridgeError("evidence artifact SHA-256 does not match")
    connection = _artifact_connection(path)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]) != "ok":
            raise QualityBridgeError("evidence artifact quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise QualityBridgeError("evidence artifact foreign_key_check failed")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not _REQUIRED_TABLES.issubset(tables):
            raise QualityBridgeError("evidence artifact schema is incomplete")
        meta = _decoded_meta(connection)
        if meta.get("artifact_schema") != ARTIFACT_SCHEMA_VERSION:
            raise QualityBridgeError("unsupported evidence artifact schema")
        if meta.get("preview_only") is not True or meta.get("formal_gate_effect") is not False:
            raise QualityBridgeError("evidence artifact is not preview-only")
        if meta.get("population_rule") != "latest link×strategy×mapping; non-similarity":
            raise QualityBridgeError("evidence artifact population rule is unsafe")
        signals = [
            _row_payload(row, _OUTPUT_FIELDS)
            for row in connection.execute(
                "SELECT " + ", ".join(_OUTPUT_FIELDS) + " FROM signals "
                "ORDER BY ledger_signal_id"
            )
        ]
        if any(bool(row["is_similarity_copy"]) for row in signals):
            raise QualityBridgeError("evidence artifact contains similarity copies")
        provenance = {
            int(row["ledger_signal_id"]): _row_payload(row, _PROVENANCE_FIELDS)
            for row in connection.execute(
                "SELECT ledger_signal_id, " + ", ".join(_PROVENANCE_FIELDS)
                + " FROM cache_provenance ORDER BY ledger_signal_id"
            )
        }
        declared = meta.get("counts")
        explicit_gpt = sum(
            1
            for row in provenance.values()
            if any(
                _model_is_gpt(row.get(field))
                for field in ("model_name", "stage1_model", "stage2_model")
            )
        )
        if (
            not isinstance(declared, dict)
            or int(declared.get("signals", -1)) != len(signals)
            or int(declared.get("cache_provenance", -1)) != len(provenance)
            or int(declared.get("explicit_gpt", -1)) != explicit_gpt
        ):
            raise QualityBridgeError("evidence artifact counts do not match")
        return meta, signals, provenance
    except sqlite3.DatabaseError as exc:
        raise QualityBridgeError("evidence artifact cannot be read safely") from exc
    finally:
        connection.close()


def _validate_live_source(
    signals: list[dict[str, object]],
    source_strategy_version: str,
    source_mapping_version: str,
) -> None:
    with database.db_lock:
        with database.get_connection() as connection:
            rows = connection.execute(
                "SELECT " + ", ".join(
                    ("id AS ledger_signal_id",) + _OUTPUT_FIELDS[1:]
                )
                + " FROM backtest_signals WHERE strategy_version = ? "
                "AND mapping_version = ? ORDER BY available_at, id",
                (source_strategy_version, source_mapping_version),
            ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest[str(row["link"])] = row
    effective = {
        int(row["ledger_signal_id"]): row
        for row in latest.values()
        if not bool(row["is_similarity_copy"])
    }
    artifact_ids = {int(row["ledger_signal_id"]) for row in signals}
    if artifact_ids != set(effective):
        raise QualityBridgeError("artifact is not the current final source population")
    for signal in signals:
        live = effective[int(signal["ledger_signal_id"])]
        live_payload = _row_payload(live, _OUTPUT_FIELDS)
        if _digest(live_payload) != _digest(signal):
            raise QualityBridgeError("artifact output differs from source signal ledger")


def _preview_diagnostics(members: list[dict[str, object]]) -> dict[str, object]:
    """Add display-only context without changing v1 eligibility or stored hashes."""
    return {
        "selected_candidate_count": sum(
            int(member["selected"])
            for member in members
            if member["eligibility"] == "eligible"
        ),
        "explicit_gpt_count": sum(
            1 for member in members
            if member["reason_code"] == "explicit_gpt_participation"
        ),
        "score_explanation_diagnostics": {
            "status": "unknown",
            "reason": "artifact_v1_does_not_capture_score_explanation",
            "fields": dict.fromkeys(EXPLANATION_FIELDS),
        },
    }


def build_score_bridge_preview(
    *,
    artifact_path: str | os.PathLike[str],
    artifact_sha256: str,
    target_strategy_version: str,
    target_mapping_version: str,
    snapshot_dir: str | os.PathLike[str] | None = None,
    refresh_enqueuer: Callable[[str], object] | None = None,
    created_at: int | None = None,
) -> dict[str, object]:
    """Persist a pending preview and return its durable refresh receipt."""
    path = Path(artifact_path).absolute()
    meta, signals, provenance_by_id = _read_artifact(path, artifact_sha256)
    source_strategy = str(meta.get("source_strategy_version") or "").strip()
    source_mapping = str(meta.get("source_mapping_version") or "").strip()
    target_strategy = str(target_strategy_version or "").strip()
    target_mapping = str(target_mapping_version or "").strip()
    if not all((source_strategy, source_mapping, target_strategy, target_mapping)):
        raise ValueError("source and target contract versions are required")
    if source_mapping != target_mapping:
        raise QualityBridgeError("source and target mapping versions must match")
    if any(
        str(signal["strategy_version"]) != source_strategy
        or str(signal["mapping_version"]) != source_mapping
        for signal in signals
    ):
        raise QualityBridgeError("artifact signals cross contract boundaries")
    _validate_live_source(signals, source_strategy, source_mapping)

    members: list[dict[str, object]] = []
    manifest_members: list[dict[str, object]] = []
    counts = {status: 0 for status in ("eligible", "ineligible", "unknown")}
    for signal in signals:
        ledger_id = int(signal["ledger_signal_id"])
        provenance = provenance_by_id.get(ledger_id)
        eligibility, reason = _classify(signal, provenance, source_strategy)
        counts[eligibility] += 1
        stage_metadata_json = _canonical_json(provenance or {})
        output_hash = _digest(signal)
        evidence_hash = hashlib.sha256(stage_metadata_json.encode("utf-8")).hexdigest()
        member = {
            "ledger_signal_id": ledger_id,
            "source_signal_key": str(signal["signal_key"]),
            "source_available_at": int(signal["available_at"]),
            "selected": 1 if bool(signal["selected"]) else 0,
            "eligibility": eligibility,
            "reason_code": reason,
            "stage_metadata_json": stage_metadata_json,
            "output_hash": output_hash,
            "evidence_hash": evidence_hash,
        }
        members.append(member)
        manifest_members.append({key: value for key, value in member.items() if key != "stage_metadata_json"})
    manifest = {
        "schema": 1,
        "policy_version": POLICY_VERSION,
        "artifact_sha256": str(artifact_sha256).casefold(),
        "source_strategy_version": source_strategy,
        "target_strategy_version": target_strategy,
        "source_mapping_version": source_mapping,
        "target_mapping_version": target_mapping,
        "members": manifest_members,
    }
    manifest_hash = _digest(manifest)
    run_id = hashlib.sha256(
        ("score-bridge-run-v1\0" + manifest_hash).encode("utf-8")
    ).hexdigest()
    for member in members:
        member["run_id"] = run_id
    run = {
        "run_id": run_id,
        "source_strategy_version": source_strategy,
        "target_strategy_version": target_strategy,
        "source_mapping_version": source_mapping,
        "target_mapping_version": target_mapping,
        "policy_version": POLICY_VERSION,
        "mode": "preview_only",
        "approval_status": "pending",
        "approved_at": None,
        "approved_by": "",
        "created_at": int(created_at or time.time()),
        "artifact_name": path.name,
        "artifact_sha256": str(artifact_sha256).casefold(),
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "source_row_count": len(members),
        "eligible_count": counts["eligible"],
        "ineligible_count": counts["ineligible"],
        "unknown_count": counts["unknown"],
        "manifest_hash": manifest_hash,
    }

    from services.dashboard_snapshots import model_quality_feedback_invalidation

    created = False
    invalidation_token = ""
    with model_quality_feedback_invalidation(
        snapshot_dir=snapshot_dir,
        db_path=database.DB_PATH,
    ) as invalidation:
        created = database.store_score_bridge_preview(run, members)
        if created:
            invalidation.keep_invalidated()
            invalidation_token = invalidation.token
    refresh_outcome: object | None = None
    if created and refresh_enqueuer is not None:
        refresh_outcome = refresh_enqueuer(invalidation_token)
    return {
        **run,
        "created": created,
        "invalidation_token": invalidation_token,
        "refresh_required": created,
        "refresh_outcome": refresh_outcome,
        **_preview_diagnostics(members),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--target-strategy-version", required=True)
    parser.add_argument("--target-mapping-version", required=True)
    parser.add_argument("--db-path")
    parser.add_argument("--snapshot-dir")
    args = parser.parse_args(argv)
    if args.db_path:
        database.DB_PATH = os.fspath(Path(args.db_path))
    result = build_score_bridge_preview(
        artifact_path=args.artifact,
        artifact_sha256=args.artifact_sha256,
        target_strategy_version=args.target_strategy_version,
        target_mapping_version=args.target_mapping_version,
        snapshot_dir=args.snapshot_dir,
    )
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
