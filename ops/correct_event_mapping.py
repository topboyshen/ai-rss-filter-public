"""Retire one proven false title mapping; never replay an unbounded cohort.

An explicit manifest and database path are required. Dry-run is read-only.
Apply requires the exact dry-run plan digest and a new, durable audit file.
Run only with the approved fixed code; this is not a deployment command.

A row written under a legacy mapping contract (with merged members and/or more
than ``MAX_EVIDENCE`` evidence rows) is refused unless it is an exact entry of
``ops.correct_event_mapping_allowlist``.  That path only flips ``is_current`` on
the one approved mapping; it appends no ledger row, writes no mapping row and
never touches signals or the integration outbox.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.asset_mapping import map_article_to_assets  # noqa: E402
from core.source_contract import is_current_mapping_writer, is_current_source_claim  # noqa: E402
from db import database  # noqa: E402
from db.event_core import canonical_event_group  # noqa: E402
from ops import correct_event_mapping_allowlist as allowlist  # noqa: E402
from services import event_enrichment as enrichment  # noqa: E402

MAX_EVIDENCE = 20


class CorrectionRefused(RuntimeError):
    """Preconditions do not exactly describe a safe empty-output correction."""


def _digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorrectionRefused(message)


def _positive_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _legacy_entry(manifest: dict):
    """Exact allowlist entry for this manifest, or ``None`` (strict rules apply)."""
    mapping = manifest["mapping"]
    entry = allowlist.lookup(
        manifest["event_id"], mapping["mapping_id"], mapping["revision"], mapping["mapping_version"],
    )
    if entry is None:
        return None
    _require(entry.event_version == manifest["event_version"]
             and entry.security_id == mapping["security_id"]
             and entry.evidence_count <= allowlist.LEGACY_EVIDENCE_CEILING,
             "manifest differs from the allowlisted legacy correction")
    return entry


def validate_manifest(value: dict) -> dict:
    _require(isinstance(value, dict), "manifest must be an object")
    _require(set(value) == {
        "schema", "event_id", "event_version", "mapping", "evidence", "reason",
    }, "manifest fields differ from schema 1")
    _require(type(value["schema"]) is int and value["schema"] == 1, "unsupported manifest schema")
    _require(isinstance(value["event_id"], str) and bool(re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["event_id"],
    )), "event_id must be an explicit UUID")
    _require(_positive_integer(value["event_version"]), "event_version must be positive")
    _require(isinstance(value["reason"], str) and 1 <= len(value["reason"].strip()) <= 300,
             "a bounded correction reason is required")
    mapping = value["mapping"]
    _require(isinstance(mapping, dict) and set(mapping) == {
        "mapping_id", "security_id", "revision", "mapping_version", "relation_type",
    }, "exact previous mapping identity is required")
    _require(all(isinstance(mapping[key], str) and mapping[key].strip() for key in (
        "mapping_id", "security_id", "mapping_version", "relation_type",
    )), "mapping identity strings must be nonempty")
    _require(_positive_integer(mapping["revision"]), "mapping revision must be positive")
    _require(mapping["relation_type"] == "direct", "only a direct issuer mapping may be retired")
    entry = _legacy_entry(value)
    _require(entry is not None or is_current_mapping_writer(mapping["mapping_version"]),
             "legacy mapping is outside scope")
    evidence = value["evidence"]
    cap = MAX_EVIDENCE if entry is None else entry.evidence_count
    _require(isinstance(evidence, list) and 1 <= len(evidence) <= cap,
             "manifest must contain 1..20 exact evidence identities" if entry is None
             else "manifest evidence count differs from the allowlisted group")
    _require(entry is None or len(evidence) == cap, "manifest evidence count differs from the allowlisted group")
    for row in evidence:
        _require(isinstance(row, dict) and set(row) == {
            "evidence_id", "version", "source_uri", "snapshot_hash",
        }, "evidence identity fields differ from schema 1")
        _require(all(isinstance(row[key], str) and row[key] for key in (
            "evidence_id", "source_uri", "snapshot_hash",
        )), "evidence identity strings must be nonempty")
        _require(_positive_integer(row["version"]), "evidence version must be positive")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", row["snapshot_hash"])), "invalid snapshot digest")
    _require(len({row["evidence_id"] for row in evidence}) == len(evidence), "duplicate evidence id")
    return value


def _rows(conn, table: str, key: str, event_id: str) -> list[dict]:
    # Table/key pairs below are internal constants, never CLI input.
    order = {
        "events": "event_id", "event_securities": "mapping_id",
        "event_evidence": "evidence_id",
        "event_mapping_replay_ledger": "evidence_id,mapping_version,input_fingerprint",
        "event_mapping_replay_dirty_roots": "root_event_id",
        "event_similarity_replay_dirty_roots": "root_event_id",
    }[table]
    rows = [dict(row) for row in conn.execute(
        f"SELECT * FROM {table} WHERE {key} = ? ORDER BY {order} LIMIT 101", (event_id,),
    ).fetchall()]
    _require(len(rows) <= 100, "target record history exceeds correction bound")
    return rows


def _record_state(conn, event_id: str) -> dict:
    return {
        "events": _rows(conn, "events", "event_id", event_id),
        "mappings": _rows(conn, "event_securities", "event_id", event_id),
        "evidence": _rows(conn, "event_evidence", "event_id", event_id),
        "ledger": _rows(conn, "event_mapping_replay_ledger", "root_event_id", event_id),
        "mapping_dirty": _rows(conn, "event_mapping_replay_dirty_roots", "root_event_id", event_id),
        "similarity_dirty": _rows(conn, "event_similarity_replay_dirty_roots", "root_event_id", event_id),
    }


def _validate_state(state: dict, manifest: dict) -> None:
    _require(len(state["events"]) == 1, "event is absent")
    root = state["events"][0]
    _require(root["version"] == manifest["event_version"] and root["merge_state"] == "active"
             and root["merged_into"] is None, "root identity/version changed")
    current = [row for row in state["mappings"] if row["is_current"]]
    expected = manifest["mapping"]
    _require(len(current) == 1 and all(current[0][key] == value for key, value in expected.items()),
             "current mappings differ from the single approved mapping")
    evidence = state["evidence"]
    _require(1 <= len(evidence) <= MAX_EVIDENCE, "evidence count is outside the bounded scope")
    actual = [{key: row[key] for key in manifest["evidence"][0]} for row in evidence]
    _require(sorted(actual, key=lambda row: row["evidence_id"]) == sorted(
        manifest["evidence"], key=lambda row: row["evidence_id"],
    ), "evidence/source/hash set changed")
    for row in evidence:
        _require(is_current_source_claim(row["claim_key"]), "non-title evidence is outside scope")
        _require(row["snapshot_text"] == row["claim_text"] and not row["excerpt"],
                 "only frozen publisher-title evidence is allowed")
        _require(hashlib.sha256(row["claim_text"].encode()).hexdigest() == row["snapshot_hash"],
                 "frozen title does not match its digest")


def _require_empty_outputs(conn, root: dict, evidence: list[dict], *,
                           allowed_security_ids: frozenset = frozenset(),
                           also_snapshot: bool = False) -> None:
    """The fixed resolver must not associate anything beyond ``allowed_security_ids``.

    The default (empty) set is the original rule: no security and no market
    reference at all.  Allowlisted multi-mapping events pass the exact security
    ids of the mappings that stay; the retired issuer can never be among them.
    """
    _require(bool(evidence), "no frozen publisher-title evidence to inspect")
    for row in evidence:
        texts = [str(row["claim_text"])]
        if also_snapshot and str(row["snapshot_text"]) not in texts:
            texts.append(str(row["snapshot_text"]))
        for title in texts:
            mapping = map_article_to_assets({
                "title": title, "raw_title": title, "tag": "", "logic": "", "summary": "",
                "direction": root["direction"],
            })
            resolved = database._resolved_real_event_mapping_candidates(
                conn, article_text=title, source_text=title,
                candidates=list(mapping.get("security_candidates") or []),
            )
            unexpected = {str(item[1][0]) for item in resolved} - allowed_security_ids
            _require(not unexpected and not mapping.get("market_references"),
                     "fixed resolver still produces a security/market association; refuse correction")


def _implementation_digest() -> str:
    paths = (
        "ops/correct_event_mapping.py", "ops/correct_event_mapping_allowlist.py",
        "services/event_enrichment.py",
        "db/database.py", "db/event_core.py", "db/title_mentions.py", "core/asset_mapping.py",
    )
    return _digest({path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths})


def build_plan(conn, manifest: dict, db_path: Path) -> tuple[dict, dict, dict, list[dict]]:
    entry = _legacy_entry(manifest)
    if entry is not None:
        return _build_legacy_plan(conn, manifest, db_path, entry)
    event_id = manifest["event_id"]
    state = _record_state(conn, event_id)
    _validate_state(state, manifest)
    _require(conn.execute("SELECT 1 FROM events WHERE merged_into = ? LIMIT 1", (event_id,)).fetchone()
             is None, "merged event members are outside scope")
    contract, active_equities = enrichment._mapping_contract_version(conn)
    _require(active_equities > 0, "security master is empty")
    _require(contract != manifest["mapping"]["mapping_version"], "fixed mapping contract must differ")
    root, evidence = enrichment._mapping_group_rows(conn, event_id)
    _require(bool(evidence), "no unbound publisher-title evidence to replay")
    # Bound historical sources can independently support the issuer. Review
    # every approved frozen title, not only the unbound rows being replayed.
    _require_empty_outputs(conn, root, state["evidence"])
    _require(not any(row["mapping_version"] == contract for row in state["ledger"]),
             "target already has replay records under this contract")
    stat = db_path.stat()
    plan = {
        "schema": 1, "event_id": event_id, "event_version": root["version"],
        "database": str(db_path), "database_identity": [stat.st_dev, stat.st_ino],
        "manifest_sha256": _digest(manifest), "record_state_sha256": _digest(state),
        "implementation_sha256": _implementation_digest(), "mapping_contract": contract,
        "previous_mapping": manifest["mapping"], "reason": manifest["reason"],
        "evidence_count": len(state["evidence"]), "replay_evidence_count": len(evidence),
        "effect": "retire_one_current_mapping_and_append_no_match_ledger",
    }
    plan["plan_sha256"] = _digest(plan)
    return plan, state, root, evidence


def _group_state(conn, group_ids: tuple[str, ...]) -> dict:
    """Frozen rows of every merged member; the root is covered by ``_record_state``."""
    return {member: {
        "events": _rows(conn, "events", "event_id", member),
        "mappings": _rows(conn, "event_securities", "event_id", member),
        "evidence": _rows(conn, "event_evidence", "event_id", member),
    } for member in group_ids[1:]}


def _legacy_evidence(state: dict, group_ids: tuple[str, ...], entry) -> list[dict]:
    rows = list(state["evidence"])
    for member in group_ids[1:]:
        rows.extend(state["group"][member]["evidence"])
    _require(len(rows) == entry.evidence_count <= allowlist.LEGACY_EVIDENCE_CEILING,
             "allowlisted evidence group changed size")
    return rows


def _validate_legacy_state(conn, state: dict, group_ids: tuple[str, ...], manifest: dict, entry) -> list[dict]:
    root_rows, expected = state["events"], manifest["mapping"]
    _require(len(root_rows) == 1, "event is absent")
    root = root_rows[0]
    _require(root["version"] == entry.event_version and root["merge_state"] == "active"
             and root["merged_into"] is None, "root identity/version changed")
    members = [(member, state["group"][member]["events"]) for member in group_ids[1:]]
    _require(all(len(rows) == 1 and rows[0]["merge_state"] == "merged" for _m, rows in members),
             "merged member state changed")
    actual_group = ((group_ids[0], root["version"]),
                    *((member, rows[0]["version"]) for member, rows in members))
    _require(actual_group == entry.group, "merged-member set or versions changed")
    _require(all(not any(row["is_current"] for row in state["group"][member]["mappings"])
                 for member in group_ids[1:]), "a merged member holds a current mapping")
    current = [row for row in state["mappings"] if row["is_current"]]
    target = [row for row in current if row["mapping_id"] == expected["mapping_id"]]
    _require(len(target) == 1 and all(target[0][key] == value for key, value in expected.items()),
             "current mappings differ from the single approved mapping")
    retained = sorted((row["mapping_id"], row["revision"]) for row in current if row is not target[0])
    _require(retained == sorted(entry.retained_mappings), "other current mappings changed")
    security = conn.execute(
        "SELECT exchange, ticker FROM securities WHERE security_id = ?", (expected["security_id"],),
    ).fetchone()
    _require(security is not None and f"{security[0]}:{security[1]}" == entry.security_label,
             "approved security identity changed")
    evidence = _legacy_evidence(state, group_ids, entry)
    actual = [{key: row[key] for key in manifest["evidence"][0]} for row in evidence]
    _require(sorted(actual, key=lambda row: row["evidence_id"]) == sorted(
        manifest["evidence"], key=lambda row: row["evidence_id"],
    ), "evidence/source/hash set changed")
    _require(allowlist.evidence_sha256(evidence) == entry.evidence_sha256,
             "allowlisted evidence content changed since review")
    for row in evidence:
        _require(hashlib.sha256(row["snapshot_text"].encode()).hexdigest() == row["snapshot_hash"],
                 "frozen snapshot does not match its digest")
        _require(str(row["snapshot_text"]).startswith(str(row["claim_text"])),
                 "claim text is not the start of its frozen snapshot")
    _require(not state["mapping_dirty"], "target has pending mapping replay work")
    return evidence


def _downstream_counts(conn, group_ids: tuple[str, ...], mapping_id: str) -> dict:
    """Read-only facts for the reviewer; the correction never writes these tables."""
    marks = ",".join("?" for _ in group_ids)
    count = lambda sql, *args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
    return {
        "signals_on_group_events": count(
            f"SELECT COUNT(*) FROM backtest_signals WHERE event_id IN ({marks})", *group_ids),
        "signal_security_snapshots_for_target_mapping": count(
            "SELECT COUNT(*) FROM backtest_signal_securities WHERE mapping_id = ?", mapping_id),
        "outbox_rows_on_group_events": count(
            f"SELECT COUNT(*) FROM integration_signal_outbox WHERE domain_event_id IN ({marks})", *group_ids),
        "outbox_rows_for_target_mapping": count(
            "SELECT COUNT(*) FROM integration_signal_outbox WHERE event_mapping_id = ?", mapping_id),
        "written_by_correction": 0,
    }


_GUARDED_TABLES = (
    "backtest_signals", "backtest_signal_securities", "integration_signal_outbox",
    "event_mapping_replay_ledger", "event_evidence", "events",
)


def _table_counts(conn) -> dict:
    counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
              for table in _GUARDED_TABLES}
    counts["event_securities"] = conn.execute("SELECT COUNT(*) FROM event_securities").fetchone()[0]
    counts["current_event_securities"] = conn.execute(
        "SELECT COUNT(*) FROM event_securities WHERE is_current = 1").fetchone()[0]
    return counts


def _build_legacy_plan(conn, manifest: dict, db_path: Path, entry) -> tuple[dict, dict, dict, list[dict]]:
    event_id = manifest["event_id"]
    group_ids = canonical_event_group(conn, event_id)
    _require(group_ids == tuple(member for member, _version in entry.group),
             "merged-member set or versions changed")
    state = _record_state(conn, event_id)
    state["group"] = _group_state(conn, group_ids)
    evidence = _validate_legacy_state(conn, state, group_ids, manifest, entry)
    root = {"event_id": event_id, "version": state["events"][0]["version"],
            "direction": state["events"][0]["direction"]}
    retained_ids = frozenset(
        row["security_id"] for row in state["mappings"]
        if row["is_current"] and row["mapping_id"] != manifest["mapping"]["mapping_id"])
    _require(manifest["mapping"]["security_id"] not in retained_ids, "approved issuer is not unique")
    _require_empty_outputs(conn, root, evidence, allowed_security_ids=retained_ids, also_snapshot=True)
    contract, active_equities = enrichment._mapping_contract_version(conn)
    _require(active_equities > 0, "security master is empty")
    _require(contract != manifest["mapping"]["mapping_version"], "fixed mapping contract must differ")
    _require(not any(row["mapping_version"] == contract for row in state["ledger"]),
             "target already has replay records under this contract")
    stat = db_path.stat()
    plan = {
        "schema": 1, "event_id": event_id, "event_version": root["version"],
        "database": str(db_path), "database_identity": [stat.st_dev, stat.st_ino],
        "manifest_sha256": _digest(manifest), "record_state_sha256": _digest(state),
        "implementation_sha256": _implementation_digest(), "mapping_contract": contract,
        "previous_mapping": manifest["mapping"], "reason": manifest["reason"],
        "evidence_count": len(evidence), "replay_evidence_count": 0,
        "legacy_allowlist": {
            "key": list(entry.key), "group": [list(item) for item in entry.group],
            "evidence_sha256": entry.evidence_sha256,
            "retained_mappings": [list(item) for item in entry.retained_mappings],
        },
        "effect": "retire_one_allowlisted_legacy_mapping_no_ledger_no_new_rows",
    }
    plan["plan_sha256"] = _digest(plan)
    return plan, state, root, evidence


def _verify_after_legacy(conn, manifest: dict, before: dict, counts_before: dict) -> None:
    group_ids = canonical_event_group(conn, manifest["event_id"])
    after = _record_state(conn, manifest["event_id"])
    after["group"] = _group_state(conn, group_ids)
    expected_mappings = [dict(row) for row in before["mappings"]]
    for row in expected_mappings:
        if row["mapping_id"] == manifest["mapping"]["mapping_id"]:
            row["is_current"] = 0
    _require(after["mappings"] == expected_mappings, "unexpected mapping payload/output change")
    _require(after["events"] == before["events"] and after["evidence"] == before["evidence"]
             and after["ledger"] == before["ledger"] and after["group"] == before["group"],
             "frozen event/evidence/ledger/member state changed")
    _require(not after["mapping_dirty"] and len(after["similarity_dirty"]) == 1,
             "target dirty-queue handoff is incomplete")
    expected_counts = dict(counts_before)
    expected_counts["current_event_securities"] -= 1
    _require(_table_counts(conn) == expected_counts, "a table other than the one approved row changed")


_QUEUE_TABLES = frozenset({"event_mapping_replay_dirty_roots", "event_similarity_replay_dirty_roots"})
_DENY_DDL = frozenset(
    getattr(sqlite3, name) for name in dir(sqlite3)
    if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_", "SQLITE_ALTER_", "SQLITE_ATTACH", "SQLITE_DETACH"))
)


def _only_flip_is_current(action, arg1, arg2, _database, _source):
    """SQLite authorizer: the only writes allowed are the approved flag and the dirty queues."""
    if action == sqlite3.SQLITE_UPDATE:
        allowed = (arg1, arg2) == ("event_securities", "is_current") or arg1 in _QUEUE_TABLES
    elif action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
        allowed = arg1 in _QUEUE_TABLES
    else:
        allowed = action not in _DENY_DDL
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY


def _apply_legacy(conn, manifest: dict, plan: dict, before: dict) -> dict:
    now = int(time.time())
    # Counted inside the write transaction, where no other writer can interleave.
    counts_before = _table_counts(conn)
    # Same write gate the ordinary mapping writer honours; never bypassed.
    database._require_event_mapping_write_authorized(plan["mapping_contract"], conn)
    conn.set_authorizer(_only_flip_is_current)  # signals/outbox/ledger/evidence are unwritable here
    try:
        return _flip_and_verify(conn, manifest, before, counts_before, now)
    finally:
        conn.set_authorizer(None)


def _flip_and_verify(conn, manifest: dict, before: dict, counts_before: dict, now: int) -> dict:
    mapping = manifest["mapping"]
    changed = conn.execute(
        "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ? AND event_id = ? "
        "AND revision = ? AND mapping_version = ? AND security_id = ? AND is_current = 1",
        (mapping["mapping_id"], manifest["event_id"], mapping["revision"],
         mapping["mapping_version"], mapping["security_id"]),
    ).rowcount
    _require(changed == 1, "materialized changes differ from the approved plan")
    enrichment._clear_mapping_group_dirty(conn, manifest["event_id"])
    enrichment._mark_mapping_group_similarity_dirty(conn, manifest["event_id"], dirty_at=now)
    _verify_after_legacy(conn, manifest, before, counts_before)
    return {"retired": 1, "mapped": 0, "mapping_rows": 0, "ledger_rows": 0, "processed": 0}


def _verify_after(conn, manifest: dict, before: dict, contract: str, evidence: list[dict]) -> None:
    after = _record_state(conn, manifest["event_id"])
    expected_mappings = [dict(row) for row in before["mappings"]]
    for row in expected_mappings:
        if row["mapping_id"] == manifest["mapping"]["mapping_id"]:
            row["is_current"] = 0
    _require(after["mappings"] == expected_mappings, "unexpected mapping payload/output change")
    _require(after["events"] == before["events"] and after["evidence"] == before["evidence"],
             "frozen event/evidence changed")
    old_rows = [row for row in after["ledger"] if row["mapping_version"] != contract]
    new_rows = [row for row in after["ledger"] if row["mapping_version"] == contract]
    _require(old_rows == before["ledger"], "previous replay ledger changed")
    _require(len(new_rows) == len(evidence) and all(
        row["status"] == "no_match" and row["mapped_count"] == 0 for row in new_rows
    ), "new no-match ledger does not match the approved plan")
    _require(not after["mapping_dirty"] and len(after["similarity_dirty"]) == 1,
             "target dirty-queue handoff is incomplete")


def _append_audit(handle, value: dict) -> None:
    handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _sync_audit_directory(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _apply(conn, manifest: dict, plan: dict, before: dict, root: dict, evidence: list[dict]) -> dict:
    if "legacy_allowlist" in plan:
        return _apply_legacy(conn, manifest, plan, before)
    contract = plan["mapping_contract"]
    now = int(time.time())
    result = enrichment._materialize_mapping_group(
        conn, root=root, evidence_rows=evidence, mapping_contract=contract,
        input_fingerprint=enrichment._mapping_group_input_fingerprint(root, evidence, contract),
        processing_time=now,
    )
    _require(result == {
        "processed": len(evidence), "mapped": 0, "mapping_rows": 0, "retired": 1,
    }, "materialized changes differ from the approved plan")
    enrichment._clear_mapping_group_dirty(conn, manifest["event_id"])
    enrichment._mark_mapping_group_similarity_dirty(conn, manifest["event_id"], dirty_at=now)
    _verify_after(conn, manifest, before, contract, evidence)
    return result


def run_correction(db_path: Path, manifest: dict, *, apply: bool = False,
                   plan_sha256: str = "", audit_path: Path | None = None) -> dict:
    validate_manifest(manifest)
    _require(db_path.is_absolute() and db_path.is_file(), "an explicit existing absolute database path is required")
    db_path = db_path.resolve(strict=True)
    _require(not apply or bool(re.fullmatch(r"[0-9a-f]{64}", plan_sha256)), "apply needs the dry-run plan digest")
    _require(not apply or (audit_path is not None and audit_path.is_absolute()), "apply needs an explicit new absolute audit path")
    _require(apply or not plan_sha256, "plan digest is only accepted with --apply")
    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode={mode}", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + 10
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1_000)
    committed = False
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if not apply:
            conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        plan, before, root, evidence = build_plan(conn, manifest, db_path)
        if not apply:
            report = {"status": "dry_run", "plan": plan}
            if "legacy_allowlist" in plan:
                report["downstream_read_only"] = _downstream_counts(
                    conn, canonical_event_group(conn, manifest["event_id"]),
                    manifest["mapping"]["mapping_id"])
            return report
        _require(plan["plan_sha256"] == plan_sha256, "plan changed; repeat read-only review")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(audit_path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as audit:
            # Persist the new directory entry as well as file contents before
            # allowing any database mutation/commit.
            _sync_audit_directory(audit_path)
            _append_audit(audit, {"phase": "prepared", "plan": plan, "at": int(time.time())})
            try:
                result = _apply(conn, manifest, plan, before, root, evidence)
                _append_audit(audit, {"phase": "validated", "result": result})
                conn.commit()
                committed = True
                _append_audit(audit, {"phase": "committed", "at": int(time.time())})
            except BaseException as exc:
                if not committed:
                    conn.rollback()
                _append_audit(audit, {"phase": "audit_incomplete_after_commit" if committed else "rolled_back",
                                      "error_type": type(exc).__name__, "at": int(time.time())})
                raise
        return {"status": "applied", "plan": plan, "result": result, "audit_path": str(audit_path)}
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256", default="")
    parser.add_argument("--audit-file", type=Path)
    args = parser.parse_args(argv)
    try:
        raw = args.manifest.read_bytes()
        _require(len(raw) <= 32_768, "manifest exceeds 32 KiB")
        result = run_correction(
            args.db, json.loads(raw), apply=args.apply,
            plan_sha256=args.plan_sha256, audit_path=args.audit_file,
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused_or_failed", "error_type": type(exc).__name__,
                          "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
