#!/usr/bin/env python3
"""Validate the reviewed A-share trading calendar before deploying it.

The file is data, not code.  Add a year only after the exchange publishes its
official closure notice, then run this checker and restart the services.

Exit codes: 0 covered, 1 gap or invalid file, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = 1
DEFAULT_FILE = (
    Path(__file__).resolve().parents[1] / "config" / "trading_calendar.json"
)


def _parse_date(raw: str, year: int) -> date:
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("invalid date")
    day = datetime.strptime(raw, "%Y-%m-%d").date()
    if day.year != year:
        raise ValueError("date outside its year")
    return day


def load_calendar(path: Path) -> dict[int, set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("unsupported calendar schema")
    years = payload.get("years")
    if not isinstance(years, dict) or not years:
        raise ValueError("calendar has no years")
    parsed: dict[int, set[str]] = {}
    for raw_year, dates in years.items():
        year_text = str(raw_year)
        if not re.fullmatch(r"\d{4}", year_text) or not isinstance(dates, list):
            raise ValueError("invalid year entry")
        if not dates:
            raise ValueError("year has no dates")
        year = int(year_text)
        entries: set[str] = set()
        for raw in dates:
            _parse_date(raw, year)
            if raw in entries:
                raise ValueError(f"duplicate date {raw}")
            entries.add(raw)
        parsed[year] = entries
    return parsed


def check(
    calendar: dict[int, set[str]],
    *,
    today: date,
    horizon_days: int,
    required_years: list[int],
) -> dict:
    horizon = today + timedelta(days=max(1, horizon_days))
    missing = sorted(set(required_years) - set(calendar))
    return {
        "years": sorted(calendar),
        "today": today.isoformat(),
        "horizon": horizon.isoformat(),
        "horizon_year": horizon.year,
        "horizon_covered": horizon.year in calendar,
        "required_missing": missing,
        "ok": horizon.year in calendar and not missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(DEFAULT_FILE))
    parser.add_argument("--today")
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--require-year", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        calendar = load_calendar(Path(args.file))
        today = date.fromisoformat(args.today) if args.today else date.today()
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    summary = check(
        calendar,
        today=today,
        horizon_days=args.horizon_days,
        required_years=args.require_year,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
