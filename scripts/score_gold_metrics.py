#!/usr/bin/env python3
"""Descriptive, contract-isolated human Gold metrics, never investment accuracy."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.score_gold import score_band, validate_finalized  # noqa: E402


def rate(numerator: int, denominator: int) -> dict:
    if not denominator:
        return {
            "numerator": numerator,
            "denominator": 0,
            "estimate": None,
            "wilson95": None,
        }
    estimate = numerator / denominator
    z = 1.959963984540054
    scale = 1 + z * z / denominator
    center = (estimate + z * z / (2 * denominator)) / scale
    radius = (
        z
        * math.sqrt(
            estimate * (1 - estimate) / denominator + z * z / (4 * denominator**2)
        )
        / scale
    )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "estimate": estimate,
        "wilson95": [max(0.0, center - radius), min(1.0, center + radius)],
    }


def _human_neutral(item: dict) -> bool:
    gold = item["labels"]
    return (
        gold["direction"] == "中性"
        and gold["score_band"] == "low"
        and gold["should_be_selected"] == 0
    )


def _safe_neutral(item: dict) -> bool:
    return "safe_neutral" in item["prediction"]["score_path"]


def metrics_for(items: list[dict]) -> dict:
    high = [item for item in items if score_band(item["prediction"]["score"]) == "high"]
    neutral = [item for item in items if _safe_neutral(item)]
    human_neutral = [item for item in items if _human_neutral(item)]
    metrics = {
        "n": len(items),
        "high_band_agreement": rate(
            sum(item["labels"]["score_band"] == "high" for item in high), len(high)
        ),
        "high_selection_precision": rate(
            sum(
                item["labels"]["relevance"] == 1
                and item["labels"]["should_be_selected"] == 1
                for item in high
            ),
            len(high),
        ),
        "safe_neutral_precision": rate(
            sum(_human_neutral(item) for item in neutral), len(neutral)
        ),
        "safe_neutral_recall_on_gold_neutral": rate(
            sum(_safe_neutral(item) for item in human_neutral), len(human_neutral)
        ),
        "sample_safe_neutral_share": rate(len(neutral), len(items)),
        "direction_agreement": rate(
            sum(
                item["labels"]["direction"] == item["prediction"]["direction"]
                for item in items
            ),
            len(items),
        ),
        "reason_supported_rate": rate(
            sum(item["labels"]["reason_is_supported"] == 1 for item in items),
            len(items),
        ),
    }
    for field in ("factual_claim_correct", "theme_correct"):
        applicable = [item for item in items if item["labels"][field] != "na"]
        metrics[field] = rate(
            sum(item["labels"][field] == 1 for item in applicable), len(applicable)
        )
    return metrics


def evaluate(pack: dict) -> dict:
    validate_finalized(pack)
    items = pack["items"]
    result = metrics_for(items)
    for field in ("backend_name", "model_name", "score_path", "stage", "attempt"):
        result["by_" + field] = {
            value: metrics_for(
                [item for item in items if item["prediction"][field] == value]
            )
            for value in sorted({item["prediction"][field] for item in items})
        }
    return {
        "strategy_version": pack["manifest"]["strategy_version"],
        "metrics": result,
        "scope": "sample-descriptive, event-deduplicated, validated model responses",
        "not_investment_accuracy": True,
        "not_population_weighted": True,
        "cannot_pool_strategy_versions": True,
        "limitations": [
            "quota sample; intervals are conditional descriptions, not population confidence claims",
            "routed backend inputs are not independent paired model arms",
            "accepted response does not imply adopted final decision or factual correctness",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(json.loads(args.input.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
        print(f"metrics refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
