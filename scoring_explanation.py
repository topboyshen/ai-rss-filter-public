"""Nullable score decomposition; diagnostics never decide a scoring path.

The frozen threshold is the article pass threshold, not the notification or
Radar aggregation threshold.  ``score_confidence`` remains a legacy field and
is intentionally neither computed nor reinterpreted here.
"""

import math
from collections.abc import Mapping

from scoring_provenance import FORMAL_SIGNAL_SCORE_PATHS, normalize_score_path


EXPLANATION_FIELDS = (
    "raw_score",
    "score_adjustment",
    "score_margin",
    "stage1_stage2_delta",
    "fast_pass_hint_score",
    "score_threshold",
)


def empty_score_explanation() -> dict:
    return dict.fromkeys(EXPLANATION_FIELDS)


def _number(value, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number, not a boolean")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def normalize_score_explanation(payload, *, score, score_path) -> dict:
    """Validate a complete known decomposition, or preserve explicit unknowns."""
    if normalize_score_path(score_path) not in FORMAL_SIGNAL_SCORE_PATHS:
        return empty_score_explanation()
    if payload is None:
        return empty_score_explanation()
    if not isinstance(payload, Mapping):
        raise ValueError("score explanation must be a mapping")
    result = {
        field: None if payload.get(field) is None else _number(payload[field], field)
        for field in EXPLANATION_FIELDS
    }
    if all(value is None for value in result.values()):
        return result
    for field in ("raw_score", "score_adjustment", "score_margin", "score_threshold"):
        if result[field] is None:
            raise ValueError(f"known score explanation requires {field}")
    final_score = _number(score, "score")
    for field, expected in (
        ("score_adjustment", final_score - result["raw_score"]),
        ("score_margin", final_score - result["score_threshold"]),
    ):
        if not math.isclose(result[field], expected, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"score explanation {field} differs from its frozen basis")
    return result


def rebase_score_explanation(payload, *, score, score_path) -> dict:
    """Recompute diagnostics after Radar changes a score, never invent history."""
    if (
        normalize_score_path(score_path) not in FORMAL_SIGNAL_SCORE_PATHS
        or not isinstance(payload, Mapping)
        or payload.get("raw_score") is None
        or payload.get("score_threshold") is None
    ):
        return empty_score_explanation()
    final_score = _number(score, "score")
    result = {field: payload.get(field) for field in EXPLANATION_FIELDS}
    result["score_adjustment"] = final_score - _number(result["raw_score"], "raw_score")
    result["score_margin"] = final_score - _number(
        result["score_threshold"], "score_threshold"
    )
    return normalize_score_explanation(result, score=score, score_path=score_path)


def _comparable_stage_score(value):
    try:
        number = _number(value, "stage_score")
    except ValueError:
        return None
    return number if 0 <= number <= 10 else None


def build_score_explanation(
    *,
    raw_score,
    score,
    score_path,
    score_threshold,
    stage1_score=None,
    stage2_score=None,
    stage1_model="",
    stage2_model="",
    fast_pass_hint=None,
) -> dict:
    """Capture the adopted source before cap/time changes; skip sentinel paths."""
    if normalize_score_path(score_path) not in FORMAL_SIGNAL_SCORE_PATHS:
        return empty_score_explanation()
    delta = None
    first, second = (
        _comparable_stage_score(stage1_score),
        _comparable_stage_score(stage2_score),
    )
    if (
        normalize_score_path(score_path) == "stage2"
        and first is not None
        and second is not None
        and str(stage1_model or "").strip()
        and str(stage2_model or "").strip()
        and "fallback_local" not in (stage1_model, stage2_model)
    ):
        delta = second - first
    result = {
        "raw_score": _number(raw_score, "raw_score"),
        "score_threshold": _number(score_threshold, "score_threshold"),
        "stage1_stage2_delta": delta,
        "fast_pass_hint_score": (
            fast_pass_hint.get("score") if isinstance(fast_pass_hint, Mapping) else None
        ),
    }
    return rebase_score_explanation(result, score=score, score_path=score_path)
