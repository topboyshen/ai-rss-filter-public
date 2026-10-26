#!/usr/bin/env python3
"""Run one controlled batch of the optional provenance shadow projector."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_event_enrichment import _run_provenance_shadow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit",
        type=int,
        default=2_000,
        help="maximum evidence projected in this controlled batch",
    )
    args = parser.parse_args()
    result = _run_provenance_shadow(
        evidence_scan_limit=min(args.limit, 10_000),
        max_roots=min(args.limit, 1_000),
        max_total_evidence=args.limit,
        configure_storage=False,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" and result.get("complete") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
