"""Gold schema and strict human-label validation. No inferred human labels."""

from __future__ import annotations

import math

from scoring_provenance import (
    SAFE_NEUTRAL_CONTRACT_VALUES,
    SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION,
)
from utils.score_artifacts import content_hash

SCHEMA = "score-gold-v2"
SOURCE_FIELDS = ("relevance", "direction", "score_band", "should_be_selected")
REASON_FIELDS = ("factual_claim_correct", "theme_correct", "reason_is_supported")
LABEL_FIELDS = (*SOURCE_FIELDS, *REASON_FIELDS)
LABEL_VALUES = {
    "relevance": {0, 1},
    "should_be_selected": {0, 1},
    "reason_is_supported": {0, 1},
    "direction": {"看多", "看空", "中性"},
    "score_band": {"high", "mid", "low"},
    "factual_claim_correct": {0, 1, "na"},
    "theme_correct": {0, 1, "na"},
}
DIRECTIONS = {
    "看多": "看多",
    "看空": "看空",
    "中性": "中性",
    "📈": "看多",
    "📉": "看空",
    "⚖️": "中性",
}


def blank_labels() -> dict:
    return dict.fromkeys(LABEL_FIELDS)


def seal_pack(pack: dict) -> dict:
    pack.pop("artifact_hash", None)
    pack["artifact_hash"] = content_hash(pack)
    return pack


def validate_labels(labels: dict, fields=LABEL_FIELDS) -> None:
    if set(labels) != set(fields):
        raise ValueError("unexpected or missing human label fields")
    for field in fields:
        value = labels[field]
        if type(value) not in (str, int) or value not in LABEL_VALUES[field]:
            raise ValueError(f"invalid human label: {field}")


def parse_human_cell(field: str, cell: str):
    if cell in {"0", "1"} and field not in {"direction", "score_band"}:
        return int(cell)
    return cell


def score_band(value) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
    ):
        return "invalid"
    return (
        "high"
        if value >= 7.5
        else "mid"
        if value >= 5
        else "low"
        if value >= 0
        else "invalid"
    )


def attempt_path(attempt: dict) -> str:
    stage = attempt["stage"]
    if not attempt["accepted"]:
        return "rejected"
    if tuple(attempt["parsed"]) != SAFE_NEUTRAL_CONTRACT_VALUES:
        return "stage2" if stage == "stage2" else "llm"
    resolution = (
        "safe_neutral_initial"
        if attempt["attempt"] == "initial"
        else (
            "safe_neutral_contract"
            if attempt["repair_trigger"] == "invalid_score_contract"
            else "safe_neutral_grounding"
        )
    )
    return SAFE_NEUTRAL_SCORE_PATHS_BY_RESOLUTION[resolution][stage]


def candidate_from_attempt(evidence: dict, index: int) -> dict:
    attempt = evidence["attempts"][index]
    score, tag, logic, direction = attempt["parsed"]
    identity = {"evidence_id": evidence["evidence_id"], "attempt_index": index}
    return {
        **identity,
        "gold_id": content_hash(identity),
        "strategy_version": evidence["strategy_version"],
        "event_group": evidence["event_group"],
        "event_id": evidence["event_id"],
        "article_id": evidence["article_id"],
        "input_hash": evidence["input_hash"],
        "source": dict(evidence["source"]),
        "evidence_complete": evidence["evidence_complete"],
        "evaluation_unit": "validated_model_response",
        "prediction_origin": "routed_attempt",
        "prediction": {
            "score": score,
            "tag": tag,
            "logic": logic,
            "direction": DIRECTIONS.get(direction, direction),
            "score_path": attempt_path(attempt),
            "model_name": attempt["model_name"],
            "backend_name": attempt["backend_name"],
            "stage": attempt["stage"],
            "attempt": attempt["attempt"],
            "raw_response": attempt["raw_response"],
            "accepted": attempt["accepted"],
            "failure_reason": attempt["failure_reason"],
            "fact_failure_kind": attempt["fact_failure_kind"],
        },
        "labels": blank_labels(),
    }


def validate_evidence(evidence: dict) -> None:
    unsigned = {key: value for key, value in evidence.items() if key != "evidence_id"}
    if evidence.get("schema") != "score-evidence-v1" or content_hash(
        unsigned
    ) != evidence.get("evidence_id"):
        raise ValueError("invalid evidence hash/schema")
    if content_hash(evidence["source"]) != evidence["input_hash"]:
        raise ValueError("source hash mismatch")
    if (
        not evidence.get("strategy_version")
        or not evidence.get("event_group")
        or not evidence.get("event_id")
    ):
        raise ValueError("missing evidence identity")
    source = evidence["source"]
    if set(source) != {"raw_title", "summary"} or any(
        not isinstance(value, str) for value in source.values()
    ):
        raise ValueError("invalid source fields")
    if (
        type(evidence.get("evidence_complete")) is not bool
        or not 1 <= len(evidence["attempts"]) <= 16
    ):
        raise ValueError("invalid evidence completeness/attempt count")
    for attempt in evidence["attempts"]:
        request_hash = attempt.get("request_hash", "")
        if (
            not isinstance(request_hash, str)
            or len(request_hash) != 64
            or any(c not in "0123456789abcdef" for c in request_hash)
        ):
            raise ValueError("missing request hash")
        if type(attempt.get("source_in_request")) is not bool:
            raise ValueError("missing request/source binding")
        if (
            evidence["evidence_complete"]
            and attempt["attempt"] == "initial"
            and not attempt["source_in_request"]
        ):
            raise ValueError("initial request did not contain the frozen source")
        if type(attempt["accepted"]) is not bool or attempt["accepted"] != (
            not attempt["failure_reason"]
        ):
            raise ValueError("invalid acceptance state")
        if not isinstance(attempt["raw_response"], str) or not isinstance(
            attempt["parsed"], list
        ):
            raise ValueError("invalid response evidence")
        if attempt["stage"] not in {"stage1", "stage2"} or attempt["attempt"] not in {
            "initial",
            "repair",
        }:
            raise ValueError("unknown attempt identity")
        if (
            not attempt["model_name"]
            or not attempt["backend_name"]
            or len(attempt["parsed"]) != 4
        ):
            raise ValueError("missing model identity/parsed values")
        score, tag, logic, direction = attempt["parsed"]
        if (
            type(score) not in (int, float)
            or not math.isfinite(score)
            or not -1 <= score <= 10
        ):
            raise ValueError("invalid parsed score")
        if (
            not isinstance(tag, str)
            or not isinstance(logic, str)
            or direction not in DIRECTIONS
        ):
            raise ValueError("invalid parsed explanation/direction")
        if attempt["accepted"] and score < 0:
            raise ValueError("invalid accepted score")
        if attempt["attempt"] == "repair" and attempt["repair_trigger"] not in {
            "invalid_score_contract",
            "ungrounded_score_facts",
            "ungrounded_score_theme",
        }:
            raise ValueError("unknown repair provenance")


def validate_candidates(pack: dict, *, require_evidence=True) -> None:
    manifest, items = pack["manifest"], pack["items"]
    if content_hash(
        {key: value for key, value in pack.items() if key != "artifact_hash"}
    ) != pack.get("artifact_hash"):
        raise ValueError("Gold artifact hash mismatch")
    if manifest.get("schema") != SCHEMA or manifest.get("candidate_count") != len(
        items
    ):
        raise ValueError("invalid Gold schema/count")
    strategy = manifest.get("strategy_version")
    if not strategy or not items or not 1 <= len(items) <= 500:
        raise ValueError("invalid Gold size/contract")
    if require_evidence and manifest.get("evidence_complete") is not True:
        raise ValueError("evidence incomplete")
    for field in (
        ("gold_id", "event_group", "event_id")
        if require_evidence
        else ("gold_id", "event_group")
    ):
        values = [item[field] for item in items]
        if not all(values) or len(set(values)) != len(values):
            raise ValueError(f"missing/duplicate {field}")
    for item in items:
        if item["strategy_version"] != strategy:
            raise ValueError("cannot pool strategy versions")
        if require_evidence:
            if item.get("evidence_complete") is not True or not item.get("event_id"):
                raise ValueError("evidence incomplete")
            source = item["source"]
            if (
                not isinstance(source.get("raw_title"), str)
                or not source["raw_title"]
                or not isinstance(source.get("summary"), str)
            ):
                raise ValueError("missing actual scoring source")
            if (
                content_hash(source) != item["input_hash"]
                or item["evaluation_unit"] != "validated_model_response"
            ):
                raise ValueError("invalid input hash/evaluation unit")
            evidence = pack.get("evidence", {}).get(item["evidence_id"])
            if not evidence:
                raise ValueError("missing original attempt evidence")
            validate_evidence(evidence)
            index = item["attempt_index"]
            if type(index) is not int or not 0 <= index < len(evidence["attempts"]):
                raise ValueError("invalid attempt index")
            expected = candidate_from_attempt(evidence, index)
            if any(
                item.get(key) != value
                for key, value in expected.items()
                if key != "labels"
            ):
                raise ValueError(
                    "candidate source/prediction differs from frozen attempt"
                )


def human_consensus(item: dict, review: dict) -> dict:
    """Copy exact reviewed cells; any disagreement needs a third human decision."""
    result = {}
    reviewers = set()
    disagreement = False
    for phase, fields in (("source", SOURCE_FIELDS), ("reason", REASON_FIELDS)):
        entries = review.get(phase, [])
        if len(entries) != 2 or len({entry["annotator_id"] for entry in entries}) != 2:
            raise ValueError("two distinct human reviewers required per phase")
        for entry in entries:
            if not entry["annotator_id"]:
                raise ValueError("missing human reviewer")
            validate_labels(entry["labels"], fields)
            reviewers.add(entry["annotator_id"])
        for field in fields:
            first, second = (entry["labels"][field] for entry in entries)
            if first != second:
                disagreement = True
            result[field] = first
    if disagreement:
        adjudication = review.get("adjudication") or {}
        if (
            not adjudication.get("annotator_id")
            or adjudication["annotator_id"] in reviewers
        ):
            raise ValueError("unresolved dispute; independent adjudicator required")
        validate_labels(adjudication["labels"])
        result = dict(adjudication["labels"])
    return result


def validate_finalized(pack: dict) -> None:
    manifest = pack["manifest"]
    if (
        manifest.get("unlabeled") is not False
        or manifest.get("status") != "finalized"
        or manifest.get("ready_for_metrics") is not True
    ):
        raise ValueError("gold set is unlabeled or unfinalized; metrics forbidden")
    validate_candidates(pack)
    if (
        not 200 <= len(pack["items"]) <= 500
        or manifest.get("human_attestation") != "operator_attested_human_only"
    ):
        raise ValueError("Gold requires 200–500 items and human provenance")
    for item in pack["items"]:
        validate_labels(item["labels"])
        if human_consensus(item, item.get("human_review") or {}) != item["labels"]:
            raise ValueError("labels do not match human review")
