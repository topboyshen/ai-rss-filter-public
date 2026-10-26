#!/usr/bin/env python3
"""Strict two-phase human CSV import. Copies labels; never predicts/fills them."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.export_score_gold_candidates import annotation_csv, annotation_row  # noqa: E402
from services.score_gold import (  # noqa: E402
    SOURCE_FIELDS,
    REASON_FIELDS,
    LABEL_FIELDS,
    human_consensus,
    parse_human_cell,
    seal_pack,
    validate_candidates,
    validate_finalized,
    validate_labels,
)
from utils.score_artifacts import (
    content_hash,
    private_directory,
    write_private_bytes,
    write_private_json,
)  # noqa: E402


def read_human_csv(path: Path, items: list[dict], phase: str) -> tuple[dict, str]:
    data = path.read_bytes()
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != list(annotation_row(items[0], phase)):
        raise ValueError("CSV schema mismatch")
    expected = {item["gold_id"]: annotation_row(item, phase) for item in items}
    fields = (
        SOURCE_FIELDS
        if phase == "source"
        else REASON_FIELDS
        if phase == "reason"
        else LABEL_FIELDS
    )
    seen, result = set(), {}
    for row in reader:
        gold_id = row.get("gold_id")
        if (
            gold_id not in expected
            or gold_id in seen
            or None in row
            or None in row.values()
        ):
            raise ValueError("unknown/duplicate/malformed CSV row")
        seen.add(gold_id)
        if any(
            row[key] != value
            for key, value in expected[gold_id].items()
            if key not in (*fields, "annotator_id")
        ):
            raise ValueError("CSV identity/source modified")
        if (
            phase == "adjudication"
            and not row["annotator_id"]
            and all(not row[field] for field in fields)
        ):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["annotator_id"]):
            raise ValueError("invalid human annotator ID")
        labels = {field: parse_human_cell(field, row[field]) for field in fields}
        validate_labels(labels, fields)
        result[gold_id] = {"annotator_id": row["annotator_id"], "labels": labels}
    if phase != "adjudication" and seen != set(expected):
        raise ValueError("CSV missing candidates")
    if (
        phase != "adjudication"
        and len({row["annotator_id"] for row in result.values()}) != 1
    ):
        raise ValueError("one reviewer identity required per input CSV")
    return result, hashlib.sha256(data).hexdigest()


def import_phase(
    pack: dict,
    phase: str,
    csv_paths: list[Path],
    *,
    human_attestation: bool,
    adjudication=None,
) -> dict:
    if human_attestation is not True:
        raise ValueError("explicit human-only attestation required")
    validate_candidates(pack)
    expected_status = "candidates" if phase == "source" else "source_reviewed"
    if (
        phase not in {"source", "reason"}
        or pack["manifest"].get("status") != expected_status
        or len(csv_paths) != 2
    ):
        raise ValueError("invalid review phase/order")
    imported = [read_human_csv(path, pack["items"], phase) for path in csv_paths]
    arbitration = (
        read_human_csv(adjudication, pack["items"], "adjudication")
        if adjudication
        else ({}, "")
    )
    if phase == "source" and adjudication:
        raise ValueError("adjudication follows both review phases")
    result = deepcopy(pack)
    for item in result["items"]:
        reviews = item.setdefault("human_review", {})
        reviews[phase] = [rows[item["gold_id"]] for rows, _ in imported]
        if len({review["annotator_id"] for review in reviews[phase]}) != 2:
            raise ValueError("two distinct human reviewers required")
        if phase == "reason":
            if item["gold_id"] in arbitration[0]:
                reviews["adjudication"] = arbitration[0][item["gold_id"]]
            item["labels"] = human_consensus(item, reviews)
    manifest = result["manifest"]
    manifest.update(
        status="source_reviewed" if phase == "source" else "finalized",
        unlabeled=phase == "source",
        ready_for_metrics=phase == "reason",
        human_attestation="operator_attested_human_only",
    )
    manifest.setdefault("review_imports", []).append(
        {
            "phase": phase,
            "parent_pack_hash": content_hash(pack),
            "csv_sha256": [digest for _, digest in imported],
            "adjudication_sha256": arbitration[1],
        }
    )
    seal_pack(result)
    if phase == "reason":
        validate_finalized(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--phase", choices=("source", "reason"), required=True)
    parser.add_argument("--csv", type=Path, nargs=2, required=True)
    parser.add_argument("--adjudication", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--human-attestation", action="store_true")
    args = parser.parse_args(argv)
    try:
        pack = json.loads(args.pack.read_text(encoding="utf-8"))
        result = import_phase(
            pack,
            args.phase,
            args.csv,
            human_attestation=args.human_attestation,
            adjudication=args.adjudication,
        )
        private_directory(args.out_dir.parent)
        args.out_dir.mkdir(mode=0o700)
        filename = (
            "source-reviewed.json" if args.phase == "source" else "finalized.json"
        )
        write_private_json(args.out_dir / filename, result)
        if args.phase == "source":
            for phase in ("reason", "adjudication"):
                write_private_bytes(
                    args.out_dir / f"{phase}.csv",
                    annotation_csv(result["items"], phase),
                )
    except (OSError, ValueError, TypeError, KeyError, IndexError, csv.Error) as error:
        print(f"human import refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
