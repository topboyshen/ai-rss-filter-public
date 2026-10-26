#!/usr/bin/env python3
"""Explicit, one-shot A-share security-master synchronization."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.security_master_sync import run_security_master_sync
from services.admission_fence import guarded


_REQUIRED_SECURITY_COLUMNS = {
    "security_id",
    "exchange",
    "ticker",
    "name",
    "asset_type",
    "currency",
    "aliases",
    "industry",
    "concepts",
    "is_active",
    "version",
    "created_at",
    "updated_at",
}


def _database_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise RuntimeError("refusing a symlink database path")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"database does not exist: {path}") from exc
    if not resolved.is_file():
        raise RuntimeError(f"database is not a regular file: {resolved}")
    return resolved


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=30.0,
        )
    else:
        conn = sqlite3.connect(str(path), timeout=30.0)
    conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("SQLite foreign-key enforcement is unavailable")
    conn.execute("PRAGMA busy_timeout=30000")
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _validate_database(conn: sqlite3.Connection) -> None:
    check = conn.execute("PRAGMA quick_check").fetchone()
    if not check or str(check[0]).casefold() != "ok":
        raise RuntimeError("database quick_check failed")
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(securities)").fetchall()
    }
    missing = _REQUIRED_SECURITY_COLUMNS - columns
    if missing:
        raise RuntimeError(
            "security-master schema is incomplete: " + ",".join(sorted(missing))
        )
    has_natural_key = False
    for index in conn.execute("PRAGMA index_list(securities)").fetchall():
        if int(index[2]) != 1 or int(index[4]) != 0:
            continue
        index_name = str(index[1]).replace('"', '""')
        indexed_columns = [
            str(row[2])
            for row in conn.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        ]
        if indexed_columns == ["exchange", "ticker"]:
            has_natural_key = True
            break
    if not has_natural_key:
        raise RuntimeError(
            "security-master schema lacks UNIQUE(exchange,ticker)"
        )
    event_mapping = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'event_securities'"
    ).fetchone()
    if event_mapping is None:
        raise RuntimeError("event-core schema is incomplete: event_securities")


def _run_path(path: Path, *, apply: bool) -> dict[str, int]:
    conn = _connect(path, read_only=not apply)
    try:
        _validate_database(conn)
        return run_security_master_sync(conn, dry_run=not apply)
    finally:
        conn.close()


@guarded("security_master", kind="intake")
def run(database: str, *, apply: bool = False) -> dict[str, int]:
    return _run_path(_database_path(database), apply=apply)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and synchronize confirmed A-share security master rows. "
            "The default is a read-only dry run."
        )
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("AI_FILTER_DB_PATH", "/app/data/cache.db"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically insert new rows; existing rows are never updated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = _database_path(args.database)
    result = _run_path(path, apply=args.apply)
    print(
        json.dumps(
            {
                **result,
                "database": str(path),
                "dry_run": not args.apply,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
