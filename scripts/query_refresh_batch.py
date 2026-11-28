#!/usr/bin/env python3
"""Read-only correlation of one refresh batch with its model calls.

Usage:
    python -I scripts/query_refresh_batch.py <refresh_id> [--db PATH]

``llm_call_logs`` has no ``refresh_id`` column.  It joins to
``news_processing_logs`` on ``processing_id`` instead.  Output is a bounded
JSON summary with counts only; no titles, links, prompts, or responses.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "/root/ai-rss-filter/data/cache.db"


def _db_path(value: str | None) -> Path:
    raw = value or os.environ.get("AI_FILTER_DB") or DEFAULT_DB
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise SystemExit("database path must be an existing absolute file")
    return path


def _summary(conn: sqlite3.Connection, refresh_id: str) -> dict:
    processing = conn.execute(
        "SELECT outcome, score_path, COUNT(*) "
        "FROM news_processing_logs WHERE refresh_id=? "
        "GROUP BY outcome, score_path ORDER BY COUNT(*) DESC",
        (refresh_id,),
    ).fetchall()
    calls = conn.execute(
        "SELECT l.backend_name, l.purpose, l.success, l.error_type, COUNT(*) "
        "FROM llm_call_logs AS l "
        "JOIN news_processing_logs AS p ON p.processing_id = l.processing_id "
        "WHERE p.refresh_id=? "
        "GROUP BY l.backend_name, l.purpose, l.success, l.error_type "
        "ORDER BY COUNT(*) DESC",
        (refresh_id,),
    ).fetchall()
    return {
        "refresh_id": refresh_id,
        "processing": [
            {"outcome": row[0], "score_path": row[1], "count": int(row[2])}
            for row in processing
        ],
        "model_calls": [
            {
                "backend": row[0],
                "purpose": row[1],
                "success": bool(row[2]),
                "error_type": row[3],
                "count": int(row[4]),
            }
            for row in calls
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("refresh_id")
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    refresh_id = str(args.refresh_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", refresh_id):
        raise SystemExit("refresh_id must be 1-64 safe characters")
    path = _db_path(args.db)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            payload = _summary(conn, refresh_id)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SystemExit(f"database read failed: {type(exc).__name__}") from exc
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
