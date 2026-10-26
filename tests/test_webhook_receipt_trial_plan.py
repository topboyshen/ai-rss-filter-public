"""Offline trial-policy tests; stdlib unittest, no Docker, network or model."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("trial_plan", ROOT / "scripts/plan_webhook_receipt_trial.py")
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)
NOW = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)


def document():
    return {"schema": 1, "input_kind": "synthetic", "snapshot": {
        "observed_utc": "2026-10-23T00:00:00Z", "revision": "a" * 40,
        "image_id": "sha256:" + "b" * 64, "evidence_sha256": "c" * 64,
        "records": 10, "reserved_bytes": 1024 * 1024, "active_tasks": 0,
        "held_receipts": 0, "unknown_receipts": 0,
        "all_states_accounted": True, "permissions_verified": True,
    }, "workload": {"duration_minutes": 10, "max_new_primary": 4, "max_new_alias": 1,
        "primary_per_hour_estimate": 24, "alias_per_hour_estimate": 6,
        "primary_reserved_bytes_estimate": 45000, "alias_reserved_bytes_estimate": 65000,
        "estimate_basis": "synthetic known-message estimate, not measured production traffic",
        "observation_interval_seconds": 30, "stop_approval_reference": "DRAFT-LOCAL-NOT-PRODUCTION"}}


class ReceiptTrialPlanTest(unittest.TestCase):
    def calculate(self, value=None):
        return planner.plan(document() if value is None else value, as_of=NOW)

    def test_small_bounded_trial_separates_forecast_and_hard_reservation(self):
        value = self.calculate()
        self.assertEqual(value["status"], "capacity_candidate_only")
        self.assertFalse(value["deployment_authorized"])
        self.assertFalse(value["runtime_enforcement_implemented"])
        self.assertEqual(value["forecast_not_guarantee"]["reserved_growth_bytes"], 245000)
        self.assertEqual(value["conservative_budget"]["projected"]["records"], 15)
        self.assertGreater(value["conservative_budget"]["reserved_growth_upper_bound"], 80 * 1024 * 1024)
        self.assertEqual(value["proposed_trial_end_utc"], "2026-10-23T00:10:00Z")

    def test_average_size_cannot_replace_hard_upper_bound(self):
        small, large = document(), document()
        large["workload"]["primary_reserved_bytes_estimate"] = planner.MAX_PRIMARY_RESERVATION
        self.assertEqual(self.calculate(small)["conservative_budget"], self.calculate(large)["conservative_budget"])
        self.assertNotEqual(self.calculate(small)["forecast_not_guarantee"], self.calculate(large)["forecast_not_guarantee"])

    def test_hard_limits_stay_bound_to_actual_store_constants(self):
        spec = importlib.util.spec_from_file_location("receipt_constants", ROOT / "services/webhook_receipts.py")
        actual = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(actual)
        self.assertEqual(planner.MAX_RECORDS, actual.MAX_RECORDS)
        self.assertEqual(planner.MAX_BYTES, actual.MAX_BYTES)
        self.assertEqual(planner.MAX_ALIAS_RESERVATION, actual.MAX_ALIAS_RESERVATION)
        self.assertEqual(planner.MAX_RECORD_BYTES, actual.MAX_RECORD_BYTES)
        self.assertEqual(planner.MAX_COMPLETION_BYTES, actual.MAX_COMPLETION_BYTES)

    def test_no_cleanup_credit_and_full_reservation_for_partial_aliases(self):
        plan = self.calculate()
        required = 4 * planner.MAX_PRIMARY_RESERVATION + planner.MAX_ALIAS_RESERVATION
        self.assertEqual(plan["conservative_budget"]["reserved_growth_upper_bound"], required)
        self.assertEqual(plan["conservative_budget"]["initial_alias_headroom_bytes"], 25221120)
        self.assertIn("Do not count on consumption", " ".join(plan["required_external_preconditions"]))

    def test_warning_threshold_exact_bytes_is_not_success(self):
        value = document()
        growth = self.calculate(value)["conservative_budget"]["reserved_growth_upper_bound"]
        value["snapshot"]["reserved_bytes"] = (planner.MAX_BYTES * 80 + 99) // 100 - growth
        result = self.calculate(value)
        self.assertEqual(result["status"], "warning_requires_review")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("projected_reserved_bytes_reaches_advisory_warning", result["warning_reasons"])

    def test_stop_threshold_exact_bytes_is_not_runtime_permission(self):
        value = document()
        growth = self.calculate(value)["conservative_budget"]["reserved_growth_upper_bound"]
        value["snapshot"]["reserved_bytes"] = (planner.MAX_BYTES * 90 + 99) // 100 - growth
        result = self.calculate(value)
        self.assertEqual(result["status"], "stop_recommended")
        self.assertIn("projected_reserved_bytes_reaches_advisory_stop", result["stop_reasons"])
        self.assertFalse(result["deployment_authorized"])

    def test_both_record_and_byte_caps_are_independent(self):
        for field in ("records", "reserved_bytes"):
            value = document()
            value["snapshot"][field] = planner.MAX_RECORDS if field == "records" else planner.MAX_BYTES
            result = self.calculate(value)
            self.assertEqual(result["status"], "stop_recommended")
            self.assertIn(f"projected_{field}_exceeds_hard_capacity", result["stop_reasons"])

    def test_queue_cannot_assume_worker_drain(self):
        value = document()
        value["snapshot"]["active_tasks"] = 1
        self.assertIn("cutover_requires_queue_quiescence_not_assumed_drain", self.calculate(value)["stop_reasons"])
        value["workload"]["max_new_primary"] = 128
        self.assertIn("new_primary_budget_exceeds_queue_without_assuming_consumption", self.calculate(value)["stop_reasons"])

    def test_held_and_unknown_do_not_become_success_or_release(self):
        for field in ("held_receipts", "unknown_receipts"):
            value = document()
            value["snapshot"][field] = 1
            result = self.calculate(value)
            self.assertIn("held_or_unknown_requires_separate_manual_disposition", result["stop_reasons"])
            self.assertEqual(result["retained_baseline"][field], 1)

    def test_alias_needs_maximum_initial_headroom_even_for_tiny_estimate(self):
        value = document()
        value["snapshot"]["reserved_bytes"] = planner.MAX_BYTES - planner.MAX_ALIAS_RESERVATION + 1
        value["workload"]["alias_reserved_bytes_estimate"] = 1
        self.assertIn("initial_alias_max_slot_headroom_missing", self.calculate(value)["stop_reasons"])

    def test_no_alias_plan_does_not_require_alias_headroom(self):
        value = document()
        value["workload"].update(max_new_alias=0, alias_per_hour_estimate=0)
        self.assertEqual(self.calculate(value)["conservative_budget"]["initial_alias_headroom_bytes"], 0)

    def test_forecast_is_rounded_up_and_must_fit_explicit_count_budget(self):
        value = document()
        value["workload"].update(primary_per_hour_estimate=25, alias_per_hour_estimate=7)
        result = self.calculate(value)
        self.assertEqual(result["forecast_not_guarantee"]["primary"], 5)
        self.assertEqual(result["forecast_not_guarantee"]["alias"], 2)
        self.assertIn("forecast_primary_exceeds_explicit_delivery_budget", result["stop_reasons"])
        self.assertIn("forecast_alias_exceeds_explicit_delivery_budget", result["stop_reasons"])

    def test_snapshot_freshness_has_exact_boundary_and_rejects_future(self):
        planner.plan(document(), as_of=NOW + timedelta(seconds=300))
        for delta in (-1, 301):
            with self.assertRaises(planner.InvalidPlan):
                planner.plan(document(), as_of=NOW + timedelta(seconds=delta))

    def test_no_timezone_or_naive_reference_is_not_valid_evidence(self):
        value = document()
        value["snapshot"]["observed_utc"] = "2026-10-23T00:00:00"
        with self.assertRaises(planner.InvalidPlan):
            self.calculate(value)
        with self.assertRaises(planner.InvalidPlan):
            planner.plan(document(), as_of=NOW.replace(tzinfo=None))

    def test_invalid_integers_and_numeric_strings_fail_closed(self):
        for bad in (True, False, -1, 1.5, "1", None, float("inf"), float("nan")):
            value = document()
            value["snapshot"]["records"] = bad
            with self.subTest(value=bad), self.assertRaises(planner.InvalidPlan):
                self.calculate(value)

    def test_missing_or_extra_input_cannot_lower_policy(self):
        for group, key in (("snapshot", "unknown_receipts"), ("workload", "stop_approval_reference")):
            value = document()
            del value[group][key]
            with self.assertRaises(planner.InvalidPlan):
                self.calculate(value)
        value = document()
        value["workload"]["stop_percent"] = 100
        with self.assertRaises(planner.InvalidPlan):
            self.calculate(value)

    def test_false_permission_or_incomplete_census_is_insufficient(self):
        for field in ("permissions_verified", "all_states_accounted"):
            for bad in (False, 1, "true"):
                value = document()
                value["snapshot"][field] = bad
                with self.assertRaises(planner.InvalidPlan):
                    self.calculate(value)

    def test_identity_binding_requires_full_revision_image_and_evidence_hash(self):
        for field, bad in (("revision", "9ac4939"), ("image_id", "latest"), ("evidence_sha256", "pending")):
            value = document()
            value["snapshot"][field] = bad
            with self.assertRaises(planner.InvalidPlan):
                self.calculate(value)

    def test_trial_duration_and_observation_interval_stay_bounded(self):
        for field, bad in (("duration_minutes", 0), ("duration_minutes", 121),
                           ("observation_interval_seconds", 0), ("observation_interval_seconds", 61)):
            value = document()
            value["workload"][field] = bad
            with self.assertRaises(planner.InvalidPlan):
                self.calculate(value)

    def test_baseline_counts_and_charge_cannot_conflict(self):
        for change in ({"records": 0}, {"reserved_bytes": 0}, {"held_receipts": 6, "unknown_receipts": 5}):
            value = document()
            value["snapshot"].update(change)
            with self.assertRaises(planner.InvalidPlan):
                self.calculate(value)

    def test_caller_claims_and_planning_clock_remain_explicit_limitations(self):
        result = self.calculate()
        text = " ".join(result["limitations"])
        self.assertIn("caller assertions", text)
        self.assertIn("not proof", text)
        self.assertIn("No receipt expiry", text)
        self.assertIn("do not free live capacity", text)
        self.assertEqual(result["production_queries"], 0)

    def run_cli(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plan.json"
            source.write_bytes(payload)
            before = source.read_bytes()
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/plan_webhook_receipt_trial.py"),
                                     "--input", str(source), "--as-of", "2026-10-23T00:00:00Z"],
                                    capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["plan.json"])
        return result, json.loads(result.stdout)

    def test_cli_only_reads_local_input_and_returns_hash(self):
        payload = json.dumps(document()).encode()
        result, output = self.run_cli(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output["status"], "capacity_candidate_only")
        self.assertEqual(len(output["input_sha256"]), 64)

    def test_cli_exit_one_for_advisory_stop(self):
        value = document()
        value["snapshot"]["held_receipts"] = 1
        result, output = self.run_cli(json.dumps(value).encode())
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output["deployment_authorized"])

    def test_cli_duplicate_keys_nonfinite_oversize_and_malformed_return_two(self):
        payloads = [b'{"schema":1,"schema":1}', b'{"schema":NaN}', b"x" * (planner.MAX_INPUT_BYTES + 1), b"{broken"]
        for payload in payloads:
            result, output = self.run_cli(payload)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output["status"], "invalid_or_insufficient_input")
            self.assertFalse(output["deployment_authorized"])

    def test_does_not_mutate_document(self):
        value = document()
        original = copy.deepcopy(value)
        self.calculate(value)
        self.assertEqual(value, original)


if __name__ == "__main__":
    unittest.main()
