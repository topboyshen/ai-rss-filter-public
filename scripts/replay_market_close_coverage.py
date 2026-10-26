#!/usr/bin/env python3
"""Read-only replay of same-day vs historical pending close coverage.

Opens the database URI read-only. Never writes future_prices or outcomes.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.event_outcomes import get_pending_quote_requirements  # noqa: E402

LIMIT = 32
DEFAULT_DATES = ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")


def _readonly_uri(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_file():
        raise SystemExit(f"database is missing: {resolved}")
    return resolved.as_uri() + "?mode=ro"


def replay_date(
    trade_date: str, *, limit: int = LIMIT, db_path: str | Path | None = None,
) -> dict:
    options = {"db_path": db_path} if db_path is not None else {}
    all_pending = get_pending_quote_requirements(trade_date, limit=limit, **options)
    same_day = get_pending_quote_requirements(
        trade_date, start_date=trade_date, limit=limit, **options
    )
    same_dates = {item["trade_date"] for item in same_day["pairs"]}
    errors: list[str] = []
    if same_day["total"] > all_pending["total"]:
        errors.append("same-day total exceeds all-pending total")
    if all_pending["truncated"] and len(all_pending["pairs"]) != limit:
        errors.append("truncated all-pending did not return the pair cap")
    if same_day["truncated"] and len(same_day["pairs"]) != limit:
        errors.append("truncated same-day did not return the pair cap")
    if same_dates and same_dates != {trade_date}:
        errors.append("same-day selection leaked another trade_date")
    return {
        "trade_date": trade_date,
        "all_total": all_pending["total"],
        "all_truncated": all_pending["truncated"],
        "same_total": same_day["total"],
        "same_truncated": same_day["truncated"],
        "same_selected": len(same_day["pairs"]),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(ROOT / "data" / "cache.db"),
        help="SQLite path; opened read-only",
    )
    parser.add_argument("--dates", nargs="+", default=list(DEFAULT_DATES))
    parser.add_argument("--limit", type=int, default=LIMIT)
    args = parser.parse_args([] if argv is None else argv)
    uri = _readonly_uri(Path(args.db))
    probe = sqlite3.connect(uri, uri=True)
    try:
        probe.execute("SELECT 1").fetchone()
    finally:
        probe.close()
    failed = False
    for trade_date in args.dates:
        row = replay_date(trade_date, limit=max(1, int(args.limit)), db_path=args.db)
        status = "FAIL" if row["errors"] else "OK"
        failed = failed or bool(row["errors"])
        print(
            f"{status} {row['trade_date']} all={row['all_total']}"
            f" trunc={row['all_truncated']} same={row['same_total']}"
            f" same_trunc={row['same_truncated']} selected={row['same_selected']}"
        )
        for item in row["errors"]:
            print(f"  {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
