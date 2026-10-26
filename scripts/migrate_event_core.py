#!/usr/bin/env python3
"""Offline maintenance entrypoint for the stable event-core backfill."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.event_migration import (  # noqa: E402
    backfill_stable_event_core,
    backfill_stable_event_core_in_transaction,
)


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _backup_under_write_lock(
    lock_conn: sqlite3.Connection,
    source: Path,
    destination: Path,
) -> None:
    """Back up the committed snapshot protected by caller's write lock."""
    if not lock_conn.in_transaction:
        raise RuntimeError("backup requires an active source write transaction")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite backup: {destination}") from exc
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    source_conn = _connection(source)
    backup_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(backup_conn)
        result = backup_conn.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError("backup quick_check failed")
    finally:
        backup_conn.close()
        source_conn.close()
    os.chmod(destination, 0o600)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill stable UUID events during a paused-writer maintenance window."
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("AI_FILTER_DB_PATH", "/app/data/cache.db"),
    )
    parser.add_argument(
        "--backup",
        help="Backup path; defaults next to the database with a timestamp.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up the source and commit the offline migration.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Compatibility flag: completed databases are validated read-only; "
            "frozen history is never replayed or rewritten."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")

    if not args.apply:
        if args.backup:
            parser.error("--backup requires --apply")
        check_conn = _connection(database)
        try:
            check = check_conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError("source database quick_check failed")
        finally:
            check_conn.close()
        result = _dry_run(database, force=args.force)
        print(
            json.dumps(
                {**result, "database": str(database), "dry_run": True},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    backup_path = (
        Path(args.backup).expanduser().resolve()
        if args.backup
        else database.with_name(
            f"{database.name}.pre-event-core-{time.strftime('%Y%m%d-%H%M%S')}.bak"
        )
    )
    if backup_path.exists():
        raise RuntimeError(f"refusing to overwrite backup: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _connection(database)
    try:
        # The same IMMEDIATE transaction guards source validation, the backup
        # snapshot and all migration writes.  A second read connection performs
        # SQLite's online backup because sqlite3_backup cannot run from the
        # connection that owns an active write transaction; the held lock still
        # prevents any writer from changing the committed snapshot in between.
        conn.execute("BEGIN IMMEDIATE")
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError("source database quick_check failed")
        _backup_under_write_lock(conn, database, backup_path)
        result = backfill_stable_event_core_in_transaction(
            conn, force=args.force
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        json.dumps(
            {**result, "database": str(database), "backup": str(backup_path)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _dry_run(database: Path, *, force: bool) -> dict:
    source = _connection(database)
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        source.backup(memory)
        memory.execute("PRAGMA foreign_keys=ON")
        return backfill_stable_event_core(memory, force=force)
    finally:
        memory.close()
        source.close()


def cli() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(cli())
