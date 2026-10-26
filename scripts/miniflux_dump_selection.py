"""Bound only the explicitly summarized synthetic byte fixture's dump data.

No Docker/production access. All other schemas and smaller fixtures stay full.
Missing/invalid preservation evidence fails; it never silently widens exclusion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def dump_arguments(root):
    plan_path = root / "PLAN.json"
    if not plan_path.exists():
        return []  # Legacy verifier never started: retain the original full dump.
    plan = json.loads(plan_path.read_bytes())
    source = plan.get("names", {}).get("SOURCE")
    if (plan.get("production") is not False or plan.get("models") != 0
            or not isinstance(source, str) or not re.fullmatch(r"held_source_isolation_[0-9a-f]{16}", source)):
        raise ValueError("exact synthetic legacy source required")
    export = json.loads((root / "SYNTHETIC_DUMP.json").read_bytes())
    if (export.get("exit_code") != 0 or export.get("large_byte_fixture_raw_not_exported") is not True
            or not isinstance(export.get("sql"), str)
            or hashlib.sha256(export["sql"].encode()).hexdigest() != export.get("sha256")):
        raise ValueError("bounded synthetic export not confirmed")
    arguments = []
    for table in ("webhook_outbox", "webhook_legacy_originals"):
        rows = json.loads((root / ("BYTE_FIXTURE_" + table + ".json")).read_bytes())
        if not isinstance(rows, list) or not rows or len(rows) > 5000:
            raise ValueError("byte fixture identity summaries unavailable")
        for row in rows:
            if (not re.fullmatch(r"[0-9a-f]{64}", str(row.get("delivery_id")))
                    or row.get("delivery_id") != row.get("body_sha256") or row.get("state") != "held"
                    or type(row.get("body_bytes")) is not int or row["body_bytes"] != 1048576
                    or type(row.get("charged_bytes")) is not int or row["charged_bytes"] <= row["body_bytes"]):
                raise ValueError("byte fixture row identity incomplete")
        arguments.append("--exclude-table-data=" + source + "_bytes." + table)
    return arguments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    for value in dump_arguments(args.evidence):
        print(value)


if __name__ == "__main__":
    main()
