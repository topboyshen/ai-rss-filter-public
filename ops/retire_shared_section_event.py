"""Retire every current security mapping of ONE shared-section event.

Before #204, feeds whose items all carried a publisher section page as their
link (``https://www.cls.cn/telegraph``) collapsed unrelated articles into one
event, and each article's mappings piled onto it.  This tool retires (flips
``event_securities.is_current`` 1 -> 0) the current mappings of exactly one such
event.  It never deletes or edits evidence, events, signals, outcomes or the
replay ledger: a SQLite authorizer denies every other write.

Workflow (never batch, never skip the dry run)::

  1. python ops/retire_shared_section_event.py --db <abs cache.db> --event-id <uuid>
     (read-only, ``mode=ro``) -> review the plan and ``plan_sha256``.
  2. After explicit approval, the same command with
     ``--apply --plan-sha256 <digest> --backup-file <new abs path> --audit-file <new abs path>``.
     The backup holds only the affected rows (event, identities, all mappings,
     all evidence and queue rows of that event), never the whole database.
  3. Rollback: ``--rollback --backup-file <that backup> --audit-file <new abs path>``
     re-inserts each retired mapping as a new current revision (mapping rows are
     immutable, so a retired revision cannot be re-activated in place).

Every query is keyed by the event id or a primary key; no table scans.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import database  # noqa: E402
from db.event_core import canonical_event_group  # noqa: E402
from core.article_identity import shared_section_urls  # noqa: E402
from services import event_enrichment as enrichment  # noqa: E402

MAX_EVIDENCE = 2000
MAX_CURRENT_MAPPINGS = 200
EFFECT = "retire_current_mappings_of_one_shared_section_event"


class RetireRefused(RuntimeError):
    """The live state is not exactly one in-scope shared-section event."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RetireRefused(message)


def _digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _implementation_digest() -> str:
    paths = ("ops/retire_shared_section_event.py", "services/event_enrichment.py",
             "db/event_core.py", "routes/webhook_routes.py")
    return _digest({path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths})


def _rows(conn, sql: str, args: tuple) -> list[dict]:
    cursor = conn.execute(sql, args)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _state(conn, event_id: str) -> dict:
    """Every row this tool may read or affect, keyed by the event id only."""
    return {
        "events": _rows(conn, "SELECT * FROM events WHERE event_id = ?", (event_id,)),
        "identities": _rows(conn, "SELECT * FROM event_identities WHERE event_id = ? "
                            "ORDER BY identity_type, identity_key", (event_id,)),
        "mappings": _rows(conn, "SELECT * FROM event_securities WHERE event_id = ? "
                          "ORDER BY mapping_id", (event_id,)),
        "evidence": _rows(conn, "SELECT * FROM event_evidence WHERE event_id = ? "
                          "ORDER BY evidence_id", (event_id,)),
        "mapping_dirty": _rows(conn, "SELECT * FROM event_mapping_replay_dirty_roots "
                               "WHERE root_event_id = ?", (event_id,)),
        "similarity_dirty": _rows(conn, "SELECT * FROM event_similarity_replay_dirty_roots "
                                  "WHERE root_event_id = ?", (event_id,)),
    }


def _section_identity(event_state: dict, cfg) -> str:
    """The single section URL whose identity owns this event, or refuse."""
    shared = sorted(set(shared_section_urls(cfg).values()))
    keys = {database._normalized_article_identity(url): url for url in shared}
    identities = [(row["identity_type"], row["identity_key"]) for row in event_state["identities"]]
    _require(len(identities) == 1 and identities[0] in keys,
             "event identity is not exactly one configured shared section URL")
    return keys[identities[0]]


def build_plan(conn, event_id: str, db_path: Path, cfg=None) -> tuple[dict, dict]:
    state = _state(conn, event_id)
    _require(len(state["events"]) == 1, "event is absent")
    root = state["events"][0]
    _require(root["merge_state"] == "active" and root["merged_into"] is None, "event is not an active root")
    _require(canonical_event_group(conn, event_id) == (event_id,), "event has merged members")
    section = _section_identity(state, cfg)
    evidence = state["evidence"]
    _require(1 <= len(evidence) <= MAX_EVIDENCE, "evidence count is outside the bounded scope")
    _require(all(row["source_uri"] == section for row in evidence),
             "event carries evidence from a non-section source")
    current = [row for row in state["mappings"] if row["is_current"] == 1]
    _require(1 <= len(current) <= MAX_CURRENT_MAPPINGS, "no current mappings (or too many) to retire")
    stat = db_path.stat()
    plan = {
        "schema": 1, "effect": EFFECT, "event_id": event_id, "event_version": root["version"],
        "section_url": section, "database": str(db_path),
        "database_identity": [stat.st_dev, stat.st_ino],
        "implementation_sha256": _implementation_digest(),
        "state_sha256": _digest(state),
        "evidence_count": len(evidence),
        "evidence_bound_to_current": sum(
            1 for row in evidence if row["mapping_id"] in {m["mapping_id"] for m in current}),
        "mapping_count": len(state["mappings"]),
        "retire": [{key: row[key] for key in ("mapping_id", "revision", "security_id", "relation_type",
                                             "mapping_version")} for row in current],
        "writes": {"event_securities.is_current": len(current), "dirty_queues": "<=2 rows"},
    }
    plan["plan_sha256"] = _digest(plan)
    return plan, state


def _open(db_path: Path, write: bool) -> sqlite3.Connection:
    _require(db_path.is_absolute() and db_path.is_file(), "--db must be an existing absolute path")
    if write:
        conn = sqlite3.connect(str(db_path), timeout=3, isolation_level=None)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        conn.execute("PRAGMA query_only = 1")
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


_QUEUE_TABLES = frozenset({"event_mapping_replay_dirty_roots", "event_similarity_replay_dirty_roots"})
_DENY_DDL = frozenset(
    getattr(sqlite3, name) for name in dir(sqlite3)
    if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_", "SQLITE_ALTER_", "SQLITE_ATTACH", "SQLITE_DETACH"))
)


def _authorizer(insert_mappings: bool):
    def check(action, arg1, arg2, _database, _source):
        if action == sqlite3.SQLITE_UPDATE:
            allowed = (arg1, arg2) == ("event_securities", "is_current") or arg1 in _QUEUE_TABLES
        elif action == sqlite3.SQLITE_INSERT:
            allowed = arg1 in _QUEUE_TABLES or (insert_mappings and arg1 == "event_securities")
        elif action == sqlite3.SQLITE_DELETE:
            allowed = arg1 in _QUEUE_TABLES
        else:
            allowed = action not in _DENY_DDL
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
    return check


def _new_file(path: Path, payload: str) -> str:
    _require(path.is_absolute(), "output files must be absolute paths")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(payload.encode()).hexdigest()


def _handoff_queues(conn, event_id: str, now: int) -> None:
    # Same handoff as ops/correct_event_mapping.py: the mapping replayer must
    # not re-materialize what was just retired; the graph must recompute.
    enrichment._clear_mapping_group_dirty(conn, event_id)
    enrichment._mark_mapping_group_similarity_dirty(conn, event_id, dirty_at=now)


def _verify_retired(conn, event_id: str, before: dict, plan: dict) -> None:
    after = _state(conn, event_id)
    retired = {row["mapping_id"] for row in plan["retire"]}
    expected = [dict(row, is_current=0) if row["mapping_id"] in retired else row
                for row in before["mappings"]]
    _require(after["mappings"] == expected, "mapping rows changed beyond the approved flags")
    for key in ("events", "identities", "evidence"):
        _require(after[key] == before[key], f"{key} changed")
    _require(not after["mapping_dirty"] and len(after["similarity_dirty"]) == 1,
             "dirty-queue handoff is incomplete")


def apply(db_path: Path, event_id: str, plan_sha256: str, backup_file: Path, audit_file: Path,
          cfg=None) -> dict:
    _require(not backup_file.exists() and not audit_file.exists(), "backup/audit files must be new")
    conn = _open(db_path, write=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan, before = build_plan(conn, event_id, db_path, cfg)
        _require(plan["plan_sha256"] == plan_sha256, "live state differs from the reviewed plan")
        contract, _active = enrichment._mapping_contract_version(conn)
        database._require_event_mapping_write_authorized(contract, conn)
        backup_sha = _new_file(backup_file, json.dumps(
            {"plan": plan, "rows": before}, ensure_ascii=False, sort_keys=True, default=str))
        now = int(time.time())
        conn.set_authorizer(_authorizer(insert_mappings=False))
        try:
            changed = 0
            for row in plan["retire"]:
                changed += conn.execute(
                    "UPDATE event_securities SET is_current = 0 WHERE mapping_id = ? AND event_id = ? "
                    "AND revision = ? AND is_current = 1",
                    (row["mapping_id"], event_id, row["revision"])).rowcount
            _require(changed == len(plan["retire"]), "changed rows differ from the plan")
            _handoff_queues(conn, event_id, now)
            _verify_retired(conn, event_id, before, plan)
        finally:
            conn.set_authorizer(None)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    result = {"plan_sha256": plan_sha256, "retired": changed, "backup_file": str(backup_file),
              "backup_sha256": backup_sha, "applied_at": now}
    _new_file(audit_file, json.dumps(result, sort_keys=True) + "\n")
    return result


def _rollback_rows(conn, event_id: str, backup: dict) -> list[dict]:
    retired = {row["mapping_id"] for row in backup["plan"]["retire"]}
    live = {row["mapping_id"]: row for row in _state(conn, event_id)["mappings"]}
    rows = []
    for row in backup["rows"]["mappings"]:
        if row["mapping_id"] not in retired:
            continue
        current = live.get(row["mapping_id"])
        _require(current is not None and current["is_current"] == 0, "retired mapping is missing or current")
        rows.append(row)
    return rows


def rollback(db_path: Path, backup_file: Path, audit_file: Path) -> dict:
    _require(not audit_file.exists(), "audit file must be new")
    backup = json.loads(backup_file.read_text(encoding="utf-8"))
    _require(backup.get("plan", {}).get("effect") == EFFECT, "not a backup written by this tool")
    event_id = backup["plan"]["event_id"]
    conn = _open(db_path, write=True)
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = _rollback_rows(conn, event_id, backup)
        conn.set_authorizer(_authorizer(insert_mappings=True))
        try:
            restored = []
            for row in rows:
                top = conn.execute(
                    "SELECT MAX(revision) FROM event_securities WHERE event_id = ? AND security_id = ? "
                    "AND relation_type = ?", (event_id, row["security_id"], row["relation_type"])).fetchone()[0]
                new = dict(row, mapping_id=str(uuid.uuid4()), revision=int(top) + 1, is_current=1, created_at=now)
                columns = ",".join(new)
                conn.execute(f"INSERT INTO event_securities ({columns}) VALUES "  # noqa: S608
                             f"({','.join('?' for _ in new)})", tuple(new.values()))
                restored.append({"from": row["mapping_id"], "to": new["mapping_id"], "revision": new["revision"]})
            _handoff_queues(conn, event_id, now)
        finally:
            conn.set_authorizer(None)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    result = {"rolled_back_from": str(backup_file), "restored": restored, "at": now}
    _new_file(audit_file, json.dumps(result, sort_keys=True) + "\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--event-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--plan-sha256", default="")
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--audit-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            _require(args.backup_file and args.audit_file and not args.apply, "--rollback needs backup+audit")
            result = rollback(args.db, args.backup_file, args.audit_file)
        elif args.apply:
            _require(bool(args.event_id and args.plan_sha256 and args.backup_file and args.audit_file),
                     "--apply needs --event-id, --plan-sha256, --backup-file and --audit-file")
            result = apply(args.db, args.event_id, args.plan_sha256, args.backup_file, args.audit_file)
        else:
            _require(bool(args.event_id), "--event-id is required")
            conn = _open(args.db, write=False)
            try:
                result, _state_rows = build_plan(conn, args.event_id, args.db)
            finally:
                conn.close()
    except (RetireRefused, sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"refused": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
