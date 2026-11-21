#!/usr/bin/env python3
"""Read-only report of short-title exact-snapshot merge candidates.

This script never writes events, merge history or similarity rows. Review the
output before enabling AI_FILTER_SHORT_TITLE_MERGE.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# The script is copied into /app/scripts without /app on sys.path when invoked
# as `python /app/scripts/...`; bootstrap the repository root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.event_enrichment import list_short_title_merge_candidates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    days = max(1, min(3650, int(args.days)))
    since = int(time.time()) - days * 86400
    candidates = list_short_title_merge_candidates(since=since, limit=args.limit)
    print(
        json.dumps(
            {
                "schema": 1,
                "generated_at": int(time.time()),
                "since": since,
                "days": days,
                "count": len(candidates),
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
