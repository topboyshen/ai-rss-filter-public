#!/usr/bin/env python3
"""Write-once, per-contract blind annotation packs; never generate human labels."""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.score_gold import (  # noqa: E402
    SCHEMA,
    LABEL_FIELDS,
    SOURCE_FIELDS,
    REASON_FIELDS,
    blank_labels,
    candidate_from_attempt,
    score_band,
    seal_pack,
    validate_evidence,
)
from utils.score_artifacts import (
    content_hash,
    private_directory,
    write_private_bytes,
    write_private_json,
)  # noqa: E402

QUOTAS = {
    "high": 40,
    "mid": 70,
    "low": 50,
    "kimi": 50,
    "qwen": 40,
    "stage1": 60,
    "stage2": 40,
    "usable": 60,
    "safe_neutral": 50,
    "theme_failure": 20,
    "numeric_fact_failure": 20,
    "event_fact_failure": 20,
}
IDENTITY_FIELDS = ("gold_id", "input_hash", "strategy_version")


def _tags(item: dict) -> set[str]:
    prediction = item.get("prediction", item)
    path = str(prediction.get("score_path") or "")
    tags = {
        score_band(prediction.get("score")),
        str(prediction.get("stage") or "stage1"),
    }
    model = str(prediction.get("model_name") or "").lower()
    tags.update(name for name in ("kimi", "qwen") if name in model)
    if "safe_neutral" in path:
        tags.add("safe_neutral")
    if path in {"llm", "stage2"}:
        tags.add("usable")  # accepted response, not necessarily adopted final score
    if prediction.get("failure_reason") == "ungrounded_score_theme":
        tags.add("theme_failure")
    if prediction.get("fact_failure_kind"):
        tags.add(prediction["fact_failure_kind"])
    return tags


def _event_units(rows: list[dict]) -> dict[str, str]:
    """Union both canonical event IDs and normalized event keys across sightings."""
    parents = {}

    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in rows:
        group = root("group:" + row["event_group"])
        if row.get("event_id"):
            event = root("event:" + row["event_id"])
            parents[max(group, event)] = min(group, event)
    return {row["gold_id"]: root("group:" + row["event_group"]) for row in rows}


def sample_candidates(
    rows: list[dict], target: int, seed: int
) -> tuple[list[dict], dict]:
    """Capacity-aware greedy joint coverage; high quota reserved first, no truncation.

    Every selected event contributes to ALL its strata. Remaining shortfalls are
    explicit, not a claim that an exact set-cover solution is impossible.
    """
    if not 1 <= target <= 500:
        raise ValueError("target must be 1–500")
    ordered = sorted(rows, key=lambda row: row["gold_id"])
    if len({row["gold_id"] for row in ordered}) != len(ordered):
        raise ValueError("duplicate candidate ID")
    rng = random.Random(seed)
    rng.shuffle(ordered)
    tags = {row["gold_id"]: _tags(row) for row in ordered}
    units = _event_units(ordered)
    inventory = {
        tag: len({units[row["gold_id"]] for row in rows if tag in tags[row["gold_id"]]})
        for tag in QUOTAS
    }
    selected, groups, counts = [], set(), Counter()
    while len(selected) < target:
        remaining = [row for row in ordered if units[row["gold_id"]] not in groups]
        if not remaining:
            break
        reserve_high = counts["high"] < min(QUOTAS["high"], inventory["high"], target)
        eligible = [
            row
            for row in remaining
            if not reserve_high or "high" in tags[row["gold_id"]]
        ]

        def gain(row):
            return sum(
                1 / max(1, inventory[tag])
                for tag in tags[row["gold_id"]]
                if tag in QUOTAS and counts[tag] < QUOTAS[tag]
            )

        picked = max(eligible, key=gain)
        selected.append(picked)
        groups.add(units[picked["gold_id"]])
        counts.update(tags[picked["gold_id"]])
    return sorted(selected, key=lambda row: row["gold_id"]), {
        "algorithm": "joint-greedy-high-reservation-v2",
        "seed": seed,
        "target": target,
        "ordered_input_hash": content_hash(sorted(row["gold_id"] for row in rows)),
        "quota": QUOTAS,
        "inventory_events": inventory,
        "achieved": dict(counts),
        "shortfall": {
            tag: max(0, quota - counts[tag]) for tag, quota in QUOTAS.items()
        },
        "shortfall_note": "inventory/capacity/overlap; greedy selection is not a feasibility proof",
        "event_deduplication": True,
        "production_weighting": None,
    }


def candidate_record(row: dict) -> dict:
    """Legacy diagnostic ONLY: cache summary/title fallback cannot recreate source."""
    identity = {
        "article_id": content_hash(str(row.get("link") or "")),
        "strategy_version": row.get("strategy_version") or "unknown",
    }
    raw_title = row.get("raw_title") or ""
    if re.match(r"^\[[⭐★]", raw_title) or "💡" in raw_title:
        raw_title = ""
    return {
        **identity,
        "gold_id": content_hash(identity),
        "event_group": identity["article_id"],
        "event_id": "",
        "evidence_complete": False,
        "evidence_incomplete_reason": "no_frozen_scoring_input",
        "input_hash": "",
        "source": {"raw_title": raw_title, "summary": None},
        "prediction": {
            key: row.get(key)
            for key in (
                "score",
                "tag",
                "logic",
                "direction",
                "model_name",
                "score_path",
            )
        },
        "evaluation_unit": "legacy_cache_diagnostic",
        "labels": blank_labels(),
    }


def load_news_cache(path: str) -> list[dict]:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [
            candidate_record(dict(row))
            for row in conn.execute("SELECT * FROM news_cache ORDER BY link")
        ]


def load_evidence(directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or path.stat().st_size > 1048576:
            raise ValueError("invalid evidence file")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        validate_evidence(evidence)
        for index, attempt in enumerate(evidence["attempts"]):
            if score_band(attempt["parsed"][0]) == "invalid":
                continue  # malformed JSON has no score/direction to compare with Gold
            item = candidate_from_attempt(evidence, index)
            item["_evidence"] = evidence
            rows.append(item)
    return rows


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@"))
        or text.startswith(("\t", "\r"))
        else text
    )


def annotation_row(item: dict, phase: str) -> dict:
    row = {field: item[field] for field in IDENTITY_FIELDS}
    row.update(
        raw_title=_cell(item["source"]["raw_title"]),
        summary=_cell(item["source"]["summary"]),
    )
    if phase == "reason":
        row.update(
            tag=_cell(item["prediction"]["tag"]),
            logic=_cell(item["prediction"]["logic"]),
        )
    fields = (
        SOURCE_FIELDS
        if phase == "source"
        else REASON_FIELDS
        if phase == "reason"
        else LABEL_FIELDS
    )
    row.update(annotator_id="", **dict.fromkeys(fields, ""))
    return row


def annotation_csv(items: list[dict], phase: str) -> bytes:
    rows = [annotation_row(item, phase) for item in items]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def build_pack(rows: list[dict], target: int, seed: int) -> dict:
    versions = {row["strategy_version"] for row in rows}
    if len(versions) != 1:
        raise ValueError("exactly one strategy_version required per pack")
    selected, sampling = sample_candidates(rows, target, seed)
    evidence = {
        row["_evidence"]["evidence_id"]: row["_evidence"]
        for row in selected
        if "_evidence" in row
    }
    items = [
        {key: value for key, value in row.items() if key != "_evidence"}
        for row in selected
    ]
    return seal_pack(
        {
            "manifest": {
                "schema": SCHEMA,
                "strategy_version": next(iter(versions)),
                "candidate_count": len(items),
                "size_ok_for_gold": 200 <= len(items) <= 500,
                "status": "candidates",
                "unlabeled": True,
                "not_accuracy": True,
                "cannot_pool_strategy_versions": True,
                "ready_for_metrics": False,
                "evidence_complete": all(item["evidence_complete"] for item in items),
                "sampling": sampling,
                "population": "captured validated model responses, routed inputs; not production decisions",
            },
            "items": items,
            "evidence": evidence,
        }
    )


def write_pack(pack: dict, directory: Path) -> None:
    directory.mkdir(mode=0o700)  # run/contract directories are write-once
    write_private_json(directory / "candidates.json", pack)
    # Release reason material only after both source-blind reviews are frozen.
    write_private_bytes(
        directory / "source.csv", annotation_csv(pack["items"], "source")
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--db", help="legacy diagnostic only; never evidence-complete Gold"
    )
    source.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--strategy-version")
    parser.add_argument("--target", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260911)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.run_id):
            raise ValueError("invalid run ID")
        rows = load_news_cache(args.db) if args.db else load_evidence(args.evidence_dir)
        if args.strategy_version:
            rows = [
                row for row in rows if row["strategy_version"] == args.strategy_version
            ]
        if not rows:
            raise ValueError("no eligible candidates")
        packs = [
            build_pack(
                [row for row in rows if row["strategy_version"] == version],
                args.target,
                args.seed,
            )
            for version in sorted({row["strategy_version"] for row in rows})
        ]
        root = private_directory(args.out_dir)
        run = root / args.run_id
        run.mkdir(mode=0o700)
        index = []
        for pack in packs:
            version = pack["manifest"]["strategy_version"]
            name = content_hash(version)[:24]
            write_pack(pack, run / name)
            index.append(
                {
                    "strategy_version": version,
                    "directory": name,
                    "candidate_count": len(pack["items"]),
                }
            )
        write_private_json(
            run / "index.json",
            {"run_id": args.run_id, "contracts": index, "unlabeled": True},
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"export refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
