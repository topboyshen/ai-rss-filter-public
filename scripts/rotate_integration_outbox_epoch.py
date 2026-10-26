#!/usr/bin/env python3
"""Invalidate AI -> Quant cursors after restoring or rebuilding SQLite.

The API and every writer must be stopped before this command is run.  The
explicit confirmation is intentional: a WAL read can otherwise continue on
the pre-rotation snapshot even while this maintenance transaction commits.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import sys
from pathlib import Path
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

from db.integration_outbox import (  # noqa: E402
    get_integration_outbox_state,
    rotate_integration_outbox_epoch,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rotate the durable AI -> Quant Outbox epoch after recovery"
    )
    parser.add_argument("--db", required=True, help="explicit restored cache.db path")
    parser.add_argument(
        "--confirm-writes-stopped",
        action="store_true",
        help="confirm API and every SQLite writer are stopped",
    )
    parser.add_argument(
        "--clear-retained",
        action="store_true",
        help=(
            "destructively discard every retained Outbox revision while rotating "
            "the epoch"
        ),
    )
    parser.add_argument(
        "--confirm-drop-retained-replay",
        action="store_true",
        help=(
            "confirm Quant has been stopped and the retained replay window may be "
            "irreversibly discarded"
        ),
    )
    parser.add_argument(
        "--busy-timeout-ms",
        type=int,
        default=5000,
        help=argparse.SUPPRESS,
    )
    return parser


def _guard_regular_database(path: Path) -> tuple[int, os.stat_result]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError("database path is not a readable existing file") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("database must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("database cannot be opened without following links") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        os.close(descriptor)
        raise ValueError("database path changed during validation")
    return descriptor, opened


def rotate_database(
    path: Path,
    *,
    busy_timeout_ms: int,
    clear_retained: bool = False,
) -> dict[str, object]:
    guard, guarded_metadata = _guard_regular_database(path)
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{quote(os.fspath(path), safe='/')}?mode=rw"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            guarded_metadata.st_dev,
            guarded_metadata.st_ino,
        ):
            raise ValueError("database path changed while it was opened")
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            check = connection.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]) != "ok":
                raise RuntimeError("restored database quick_check failed")
            before = get_integration_outbox_state(connection)
            retained_rows = max(
                0, int(before["last_seq"]) - int(before["floor_seq"]) + 1
            )
            new_epoch = rotate_integration_outbox_epoch(
                connection,
                clear=clear_retained,
            )
            after = get_integration_outbox_state(connection)
            for field in ("ledger_id", "last_seq"):
                if after[field] != before[field]:
                    raise RuntimeError("epoch rotation changed Outbox stream bounds")
            expected_floor = (
                int(before["last_seq"]) + 1
                if clear_retained
                else int(before["floor_seq"])
            )
            if int(after["floor_seq"]) != expected_floor:
                raise RuntimeError("epoch rotation produced an invalid Outbox floor")
            if after["epoch"] != new_epoch or new_epoch == before["epoch"]:
                raise RuntimeError("epoch rotation did not produce a new epoch")
            connection.commit()
            return {
                "ledger_id": after["ledger_id"],
                "epoch": new_epoch,
                "floor_seq": after["floor_seq"],
                "last_seq": after["last_seq"],
                "cleared_retained_rows": retained_rows if clear_retained else 0,
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
    finally:
        if connection is not None:
            connection.close()
        os.close(guard)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.confirm_writes_stopped:
        parser.error("--confirm-writes-stopped is required")
    if args.confirm_drop_retained_replay and not args.clear_retained:
        parser.error("--confirm-drop-retained-replay requires --clear-retained")
    if args.clear_retained and not args.confirm_drop_retained_replay:
        parser.error(
            "--confirm-drop-retained-replay is required with --clear-retained"
        )
    if not 0 <= args.busy_timeout_ms <= 60_000:
        parser.error("busy timeout is out of range")
    raw_path = str(args.db or "")
    if not raw_path or "\x00" in raw_path:
        parser.error("--db is invalid")
    path = Path(os.path.abspath(raw_path))
    try:
        state = rotate_database(
            path,
            busy_timeout_ms=args.busy_timeout_ms,
            clear_retained=args.clear_retained,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR: Outbox epoch rotation failed: {exc}", file=sys.stderr)
        return 1
    action = "cleared and epoch rotated" if args.clear_retained else "epoch rotated"
    print(
        f"OK integration Outbox {action} "
        f"floor_seq={state['floor_seq']} last_seq={state['last_seq']} "
        f"cleared_retained_rows={state['cleared_retained_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
