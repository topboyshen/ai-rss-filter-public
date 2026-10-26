"""Plan a bounded receipt-retention trial from a frozen JSON snapshot, offline.

No SSH, HTTP, model, Docker, receipt recovery, deletion or runtime changes. Exit
0 is only a capacity calculation eligible for review, 1 is an advisory stop or
warning, and 2 is invalid/incomplete input. None authorizes production enablement.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

MAX_RECORDS = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_ACTIVE_TASKS = 128
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_COMPLETION_BYTES = 16 * 1024
INDEX_RESERVE = 2048
MAX_PRIMARY_RESERVATION = 2 * (MAX_RECORD_BYTES + MAX_COMPLETION_BYTES + 2048) + INDEX_RESERVE
MAX_ALIAS_RESERVATION = 3 * (MAX_RECORD_BYTES + MAX_COMPLETION_BYTES + 2048)
WARNING_PERCENT = 80
STOP_PERCENT = 90
MAX_TRIAL_MINUTES = 120
MAX_SNAPSHOT_AGE_SECONDS = 300
MAX_INPUT_BYTES = 64 * 1024


class InvalidPlan(ValueError):
    """Missing or inconsistent evidence cannot become an enablement proposal."""


def integer(value, name, maximum, *, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidPlan(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def exact_object(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise InvalidPlan(f"{name} fields must be exactly {','.join(sorted(fields))}")
    return value


def timestamp(value, name):
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value) is None:
        raise InvalidPlan(f"{name} must be an explicit UTC ISO timestamp ending Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise InvalidPlan(f"invalid {name}") from exc


def identifier(value, name, *, image=False):
    expression = r"sha256:[0-9a-f]{64}" if image else r"[0-9a-f]{40}"
    if not isinstance(value, str) or re.fullmatch(expression, value) is None:
        raise InvalidPlan(f"invalid {name}")
    return value


def validate_snapshot(value, as_of):
    snapshot = exact_object(value, {
        "observed_utc", "revision", "image_id", "evidence_sha256", "records",
        "reserved_bytes", "active_tasks", "held_receipts", "unknown_receipts",
        "all_states_accounted", "permissions_verified",
    }, "snapshot")
    observed = timestamp(snapshot["observed_utc"], "observed_utc")
    age = (as_of - observed).total_seconds()
    if not 0 <= age <= MAX_SNAPSHOT_AGE_SECONDS:
        raise InvalidPlan("snapshot is future-dated or older than the 300-second planning allowance")
    identifier(snapshot["revision"], "revision")
    identifier(snapshot["image_id"], "image_id", image=True)
    proof = snapshot["evidence_sha256"]
    if not isinstance(proof, str) or re.fullmatch(r"[0-9a-f]{64}", proof) is None:
        raise InvalidPlan("evidence_sha256 must bind the separately retained snapshot evidence")
    for field, limit in (("records", MAX_RECORDS), ("reserved_bytes", MAX_BYTES),
                         ("active_tasks", MAX_ACTIVE_TASKS), ("held_receipts", MAX_RECORDS),
                         ("unknown_receipts", MAX_RECORDS)):
        integer(snapshot[field], field, limit)
    if (snapshot["records"] == 0) != (snapshot["reserved_bytes"] == 0):
        raise InvalidPlan("receipt count and reserved bytes disagree")
    if snapshot["held_receipts"] + snapshot["unknown_receipts"] > snapshot["records"]:
        raise InvalidPlan("held and unknown must be disjoint counted receipt subsets")
    for field in ("all_states_accounted", "permissions_verified"):
        if snapshot[field] is not True:
            raise InvalidPlan(f"{field} must be explicitly verified in retained evidence")
    return snapshot, age


def validate_workload(value):
    workload = exact_object(value, {
        "duration_minutes", "max_new_primary", "max_new_alias", "primary_per_hour_estimate",
        "alias_per_hour_estimate", "primary_reserved_bytes_estimate", "alias_reserved_bytes_estimate",
        "estimate_basis", "observation_interval_seconds", "stop_approval_reference",
    }, "workload")
    integer(workload["duration_minutes"], "duration_minutes", MAX_TRIAL_MINUTES, minimum=1)
    for field in ("max_new_primary", "max_new_alias", "primary_per_hour_estimate", "alias_per_hour_estimate"):
        integer(workload[field], field, MAX_RECORDS)
    if workload["max_new_primary"] + workload["max_new_alias"] == 0:
        raise InvalidPlan("a trial needs a non-zero explicitly bounded delivery budget")
    integer(workload["primary_reserved_bytes_estimate"], "primary_reserved_bytes_estimate",
            MAX_PRIMARY_RESERVATION, minimum=1)
    integer(workload["alias_reserved_bytes_estimate"], "alias_reserved_bytes_estimate",
            MAX_ALIAS_RESERVATION, minimum=1)
    integer(workload["observation_interval_seconds"], "observation_interval_seconds", 60, minimum=1)
    for field in ("estimate_basis", "stop_approval_reference"):
        if not isinstance(workload[field], str) or not 1 <= len(workload[field].strip()) <= 512:
            raise InvalidPlan(f"{field} must explicitly describe the local planning assumption")
    return workload


def forecast(workload):
    counts = {kind: (workload[f"{kind}_per_hour_estimate"] * workload["duration_minutes"] + 59) // 60
              for kind in ("primary", "alias")}
    expected = sum(counts[kind] * workload[f"{kind}_reserved_bytes_estimate"] for kind in counts)
    worst = (workload["max_new_primary"] * MAX_PRIMARY_RESERVATION
             + workload["max_new_alias"] * MAX_ALIAS_RESERVATION)
    return counts, expected, worst


def threshold_reasons(snapshot, projected, workload, counts):
    stopped, warned = [], []
    for label, current, total, limit in (
        ("records", snapshot["records"], projected["records"], MAX_RECORDS),
        ("reserved_bytes", snapshot["reserved_bytes"], projected["reserved_bytes"], MAX_BYTES),
    ):
        if total > limit:
            stopped.append(f"projected_{label}_exceeds_hard_capacity")
        elif total * 100 >= limit * STOP_PERCENT:
            stopped.append(f"projected_{label}_reaches_advisory_stop")
        elif total * 100 >= limit * WARNING_PERCENT:
            warned.append(f"projected_{label}_reaches_advisory_warning")
        if current * 100 >= limit * STOP_PERCENT:
            stopped.append(f"current_{label}_reaches_advisory_stop")
    if snapshot["active_tasks"]:
        stopped.append("cutover_requires_queue_quiescence_not_assumed_drain")
    if projected["active_tasks_without_consumption"] > MAX_ACTIVE_TASKS:
        stopped.append("new_primary_budget_exceeds_queue_without_assuming_consumption")
    if snapshot["held_receipts"] or snapshot["unknown_receipts"]:
        stopped.append("held_or_unknown_requires_separate_manual_disposition")
    if workload["max_new_alias"] and MAX_BYTES - snapshot["reserved_bytes"] < MAX_ALIAS_RESERVATION:
        stopped.append("initial_alias_max_slot_headroom_missing")
    for kind in counts:
        if counts[kind] > workload[f"max_new_{kind}"]:
            stopped.append(f"forecast_{kind}_exceeds_explicit_delivery_budget")
    return stopped, warned


def plan(document, *, as_of):
    """Pure calculation: inputs are assertions, not independently verified state."""
    value = exact_object(document, {"schema", "input_kind", "snapshot", "workload"}, "plan")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise InvalidPlan("unsupported schema")
    if value["input_kind"] not in {"synthetic", "frozen_observation"}:
        raise InvalidPlan("input_kind must distinguish synthetic from retained observations")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise InvalidPlan("planning reference must be timezone aware")
    reference = as_of.astimezone(timezone.utc)
    snapshot, age = validate_snapshot(value["snapshot"], reference)
    workload = validate_workload(value["workload"])
    counts, estimated_growth, worst_growth = forecast(workload)
    projected = {"records": snapshot["records"] + workload["max_new_primary"] + workload["max_new_alias"],
                 "reserved_bytes": snapshot["reserved_bytes"] + worst_growth,
                 "active_tasks_without_consumption": snapshot["active_tasks"] + workload["max_new_primary"]}
    stopped, warned = threshold_reasons(snapshot, projected, workload, counts)
    status = "stop_recommended" if stopped else "warning_requires_review" if warned else "capacity_candidate_only"
    return {
        "schema": 1, "status": status, "exit_code": int(bool(stopped or warned)),
        "deployment_authorized": False, "runtime_enforcement_implemented": False,
        "network_access": False, "production_queries": 0, "model_calls": 0,
        "input_kind": value["input_kind"], "snapshot_identity": {
            key: snapshot[key] for key in ("revision", "image_id", "observed_utc", "evidence_sha256")},
        "reference_utc": reference.isoformat().replace("+00:00", "Z"), "snapshot_age_seconds": age,
        "proposed_trial_end_utc": (reference + timedelta(minutes=workload["duration_minutes"])).isoformat().replace("+00:00", "Z"),
        "hard_limits": {"records": MAX_RECORDS, "reserved_bytes": MAX_BYTES, "active_tasks": MAX_ACTIVE_TASKS},
        "advisory_policy_not_runtime_config": {"warning_percent": WARNING_PERCENT, "stop_percent": STOP_PERCENT,
            "max_duration_minutes": MAX_TRIAL_MINUTES, "snapshot_max_age_seconds": MAX_SNAPSHOT_AGE_SECONDS},
        "retained_baseline": {key: snapshot[key] for key in ("records", "reserved_bytes", "active_tasks", "held_receipts", "unknown_receipts")},
        "forecast_not_guarantee": {"primary": counts["primary"], "alias": counts["alias"],
                                  "reserved_growth_bytes": estimated_growth, "basis": workload["estimate_basis"]},
        "conservative_budget": {"new_primary": workload["max_new_primary"], "new_alias": workload["max_new_alias"],
            "per_primary_reserved_upper_bound": MAX_PRIMARY_RESERVATION,
            "per_alias_reserved_upper_bound": MAX_ALIAS_RESERVATION,
            "reserved_growth_upper_bound": worst_growth, "projected": projected,
            "initial_alias_headroom_bytes": MAX_ALIAS_RESERVATION if workload["max_new_alias"] else 0,
            "hard_remaining_records": MAX_RECORDS - projected["records"],
            "hard_remaining_reserved_bytes": MAX_BYTES - projected["reserved_bytes"]},
        "stop_reasons": stopped, "warning_reasons": warned,
        "observation_interval_seconds": workload["observation_interval_seconds"],
        "stop_approval_reference": workload["stop_approval_reference"],
        "required_external_preconditions": [
            "Freeze exact receiver/sender versions and validate all producers, consumers and in-flight custody before cutover.",
            "Separately verify sender retained backlog, capacity, retry-age budget and held disposition; receiver planning does not reserve sender capacity.",
            "An approved bounded controller must stop NEW admission at the first of time, delivery count, warning/stop or fault; this script cannot enforce or authorize it.",
            "Do not count on consumption, completion, cleanup or archival releasing any existing receipt reservation.",
            "Verify physical free space, private ownership, strict fsync behavior and snapshot evidence outside this pure calculator.",
            "No automatic receipt/index/intent deletion, held release, resigning or historical replay; preserve originals on stop and rollback.",
        ],
        "limitations": [
            "Snapshot verification booleans and evidence hash are caller assertions; the calculator never reads the referenced original or a live spool.",
            "The as-of timestamp is a planning reference, not proof a production observation occurred at that time.",
            "Primary/alias hard upper bounds intentionally over-reserve. Average-size and rate estimates are not admission guarantees.",
            "No receipt expiry exists. Passing one bounded trial does not solve indefinite retention or a safe old-version rollback.",
            "Copied offline archives do not free live capacity or preserve live deduplication if originals/indexes are removed.",
        ],
    }


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPlan(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--as-of", help="UTC planning reference only; defaults to actual local clock")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise InvalidPlan("input exceeds 64 KiB local planning limit")
        document = json.loads(raw, object_pairs_hook=reject_duplicates,
                              parse_constant=lambda value: (_ for _ in ()).throw(InvalidPlan(f"non-finite JSON: {value}")))
        reference = timestamp(args.as_of, "as_of") if args.as_of else datetime.now(timezone.utc)
        result = plan(document, as_of=reference)
        result["input_sha256"] = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, OverflowError) as error:
        result = {"schema": 1, "status": "invalid_or_insufficient_input", "exit_code": 2,
                  "deployment_authorized": False, "runtime_enforcement_implemented": False,
                  "production_queries": 0, "model_calls": 0, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
