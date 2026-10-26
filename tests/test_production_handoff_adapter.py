"""Real OS locks/files/processes; production remains explicitly unavailable."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import production_handoff_adapter as adapter  # noqa: E402
import durable_handoff_cutover as disk  # noqa: E402


class ProductionAdapterTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("AI_FILTER_HANDOFF_EVIDENCE_DIR")
        if evidence:
            Path(evidence).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=evidence))
        self.identity = {"scope": "isolation", "run_id": "a" * 32,
                         "adapter_sha256": hashlib.sha256((SCRIPTS / "production_handoff_adapter.py").read_bytes()).hexdigest()}
        self.manifest = {"mode": "isolation", "run_id": "a" * 32, "identity": self.identity,
                         "entrances": list(adapter.ENTRANCES)}
        self.gate = self.root / "gate"
        adapter.init_isolation(self.gate, self.manifest)
        self.backend = adapter.ProductionHandoffAdapter(self.gate, self.manifest, mode="isolation", lock_seconds=0.05)
        self.operation = "b" * 64
        self.children = []

    def tearDown(self):
        for child in self.children:
            # Owned synthetic children are bounded and allowed to exit naturally.
            child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()
        # All originals, markers and crash evidence intentionally retained.

    def test_static_audit_has_exact_file_hashes_and_explicit_gap(self):
        report = adapter.audit_source(SCRIPTS.parent)
        self.assertEqual(report["status"], "blocked_missing_shared_admission")
        self.assertFalse(report["production_adapter_bound"])
        self.assertEqual(report["expected_upstream_revision"], adapter.SOURCE_REVISION)
        for relative, evidence in report["files"].items():
            self.assertEqual(evidence["sha256"], hashlib.sha256((SCRIPTS.parent / relative).read_bytes()).hexdigest())
            self.assertTrue(all(evidence["anchors"].values()))

    def test_live_mode_and_caller_booleans_cannot_enable_stop(self):
        manifest = dict(self.manifest, production_adapter_bound=True, all_sources_closed=True)
        live = adapter.ProductionHandoffAdapter(self.gate, manifest, mode="production")
        before = (self.gate / "state.json").read_bytes()
        with self.assertRaises(adapter.AdapterBlocked):
            live.stop(self.operation, "capacity", 0)
        self.assertEqual((self.gate / "state.json").read_bytes(), before)
        self.assertEqual(list((self.gate / "operations").iterdir()), [])

    def test_actual_admissions_count_and_stop_all_known_entries(self):
        for entrance in adapter.ENTRANCES:
            self.backend.accept_isolated(entrance, b"exact-original")
        snapshot = self.backend.snapshot(self.identity, 0)
        self.assertEqual(snapshot["admission_total"], len(adapter.ENTRANCES))
        self.backend.accept_isolated("webhook", b"exact-original")
        self.assertEqual(self.backend.snapshot(self.identity, 0)["admission_total"], len(adapter.ENTRANCES))
        receipt = self.backend.stop(self.operation, "count_limit", 0)
        self.assertTrue(receipt["durable"])
        self.assertEqual(receipt["previous_generation"], 0)
        self.assertEqual(receipt["generation"], 1)
        for entrance in adapter.ENTRANCES:
            with self.subTest(entrance=entrance), self.assertRaises(adapter.AdapterBlocked):
                self.backend.accept_isolated(entrance, b"refused-new-body")
        self.assertEqual(self.backend.snapshot(self.identity, 1)["admission_total"], len(adapter.ENTRANCES))

    def test_stop_has_no_automatic_deploy_permission(self):
        self.backend.stop(self.operation, "time_limit", 0)
        evidence = self.backend.verify_switch_gate(self.identity, 1)
        self.assertEqual(evidence["status"], "isolated_zero_observed_only")
        self.assertFalse(evidence["switch_authorized"])
        self.assertEqual(set(evidence["locks"]), {"release", "refresh", "admission"})

    def test_every_original_nonzero_work_state_blocks_switch(self):
        self.backend.stop(self.operation, "stop", 0)
        counters = self.backend.snapshot(self.identity, 1)["counters"]
        for number, kind in enumerate(counters):
            disk.durable_write(self.gate / "work" / f"{number}.json", {"kind": kind, "identity": "c" * 64})
        snapshot = self.backend.snapshot(self.identity, 1)
        self.assertTrue(all(value == 1 for value in snapshot["counters"].values()))
        with self.assertRaises(Exception):
            self.backend.verify_switch_gate(self.identity, 1)
        self.assertEqual(len(snapshot["raw_work"]), len(counters))

    def test_unknown_work_and_producer_fail_closed(self):
        with self.assertRaises(Exception):
            self.backend.accept_isolated("unknown_bypass", b"data")
        disk.durable_write(self.gate / "work/unknown.json", {"kind": "unknown", "identity": "c" * 64})
        with self.assertRaises(Exception):
            self.backend.snapshot(self.identity, 0)

    def test_release_and_refresh_real_flock_contention_refuses_stop(self):
        for name in ("release.lock", "refresh.lock"):
            with self.subTest(lock=name), adapter.held_lock(self.gate / "locks" / name, True, 0):
                with self.assertRaises(adapter.AdapterBusy):
                    self.backend.stop(self.operation, "limit", 0)
        self.assertEqual(disk.read_private(self.gate / "state.json")["generation"], 0)

    def test_real_inflight_child_blocks_until_natural_completion(self):
        source = (
            "import sys,json,time;sys.path.insert(0,sys.argv[1]);import production_handoff_adapter as a;"
            "b=a.ProductionHandoffAdapter(sys.argv[2],json.loads(sys.argv[3]),mode='isolation');"
            "\nwith b.admission('webhook'):\n print('entered',flush=True);time.sleep(.35)\n"
        )
        child = subprocess.Popen([sys.executable, "-B", "-c", source, str(SCRIPTS), str(self.gate), json.dumps(self.manifest)],
                                 stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "entered")
        with self.assertRaises(adapter.AdapterBusy):
            self.backend.stop(self.operation, "capacity", 0)
        self.assertEqual(disk.read_private(self.gate / "state.json")["generation"], 0)
        self.assertEqual(child.wait(timeout=2), 0)
        self.assertEqual(self.backend.stop(self.operation, "capacity", 0)["state"], "closed")

    def test_duplicate_and_lost_ack_query_exact_receipt_without_new_generation(self):
        receipt = self.backend.stop(self.operation, "capacity", 0)
        original = (self.gate / "operations" / (self.operation + ".json")).read_bytes()
        restarted = adapter.ProductionHandoffAdapter(self.gate, self.manifest, mode="isolation")
        self.assertEqual(restarted.query_operation(self.operation), receipt)
        self.assertEqual(restarted.stop(self.operation, "capacity", 0), receipt)
        self.assertEqual((self.gate / "operations" / (self.operation + ".json")).read_bytes(), original)
        self.assertEqual(restarted.snapshot(self.identity, 1)["generation"], 1)
        with self.assertRaises(Exception):
            restarted.stop(self.operation, "different_reason", 0)

    def test_crash_after_transition_before_state_recovers_exact_stop(self):
        original_write = adapter.durable_write
        def fault(path, value):
            if path.name == "state.json":
                raise OSError("synthetic crash before state persistence")
            original_write(path, value)
        with patch.object(adapter, "durable_write", side_effect=fault), self.assertRaises(OSError):
            self.backend.stop(self.operation, "capacity", 0)
        self.assertEqual(disk.read_private(self.gate / "state.json")["generation"], 0)
        transition = (self.gate / "transition.json").read_bytes()
        restarted = adapter.ProductionHandoffAdapter(self.gate, self.manifest, mode="isolation")
        with self.assertRaises(adapter.AdapterBlocked):
            restarted.accept_isolated("webhook", b"must-not-slip-through-old-open-state")
        with self.assertRaises(Exception):
            restarted.stop("d" * 64, "other_stop", 0)
        self.assertEqual(restarted.query_operation(self.operation)["state"], "closed")
        self.assertEqual((self.gate / "transition.json").read_bytes(), transition)
        self.assertEqual(restarted.snapshot(self.identity, 1)["generation"], 1)

    def test_directory_sync_failure_never_confirms_visible_stop(self):
        self.backend.stop(self.operation, "stop", 0)
        path = self.gate / "operations" / (self.operation + ".json")
        original = path.read_bytes()
        with patch.object(disk, "sync_directory", side_effect=OSError("EIO")), self.assertRaises(OSError):
            self.backend.query_operation(self.operation)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.backend.query_operation(self.operation)["durable"])

    def test_transition_cannot_reopen_gate_under_closed_ack(self):
        self.backend.stop(self.operation, "stop", 0)
        path = self.gate / "transition.json"
        value = disk.read_private(path)
        value["state"]["entrances"]["filter"] = "open"
        disk.durable_write(path, value)
        with self.assertRaises(Exception):
            self.backend.query_operation(self.operation)

    def test_source_identity_generation_and_marker_mismatch(self):
        with self.assertRaises(Exception):
            self.backend.snapshot(dict(self.identity, run_id="d" * 32), 0)
        with self.assertRaises(Exception):
            self.backend.snapshot(self.identity, True)
        manifest = copy.deepcopy(self.manifest)
        manifest["identity"]["adapter_sha256"] = "e" * 64
        with self.assertRaises(Exception):
            adapter.ProductionHandoffAdapter(self.gate, manifest, mode="isolation").snapshot(manifest["identity"], 0)

    def test_invalid_open_generation_cannot_admit_snapshot_or_stop(self):
        for bad in (False, 0.0, -1, adapter.MAX_GENERATION + 1):
            state = {"generation": bad, "entrances": dict.fromkeys(adapter.ENTRANCES, "open")}
            disk.durable_write(self.gate / "state.json", state)
            before = (self.gate / "state.json").read_bytes()
            actions = (lambda: self.backend.snapshot(self.identity, 0),
                       lambda: self.backend.stop(self.operation, "limit", 0),
                       lambda: self.backend.accept_isolated("webhook", b"must-not-admit"))
            for action in actions:
                with self.subTest(generation=bad, action=action), self.assertRaises(disk.CutoverError):
                    action()
            self.assertEqual((self.gate / "state.json").read_bytes(), before)
            self.assertEqual(list((self.gate / "admissions").iterdir()), [])
            self.assertEqual(list((self.gate / "operations").iterdir()), [])

    def test_invalid_closed_generation_cannot_be_repaired_into_confirmation(self):
        self.backend.stop(self.operation, "limit", 0)
        for bad in (True, 1.0, -1, adapter.MAX_GENERATION + 1):
            state = {"generation": bad, "entrances": dict.fromkeys(adapter.ENTRANCES, "closed")}
            disk.durable_write(self.gate / "state.json", state)
            before = (self.gate / "state.json").read_bytes()
            for action in (lambda: self.backend.snapshot(self.identity, 1),
                           lambda: self.backend.query_operation(self.operation),
                           lambda: self.backend.stop(self.operation, "limit", 0)):
                with self.subTest(generation=bad), self.assertRaises(disk.CutoverError):
                    action()
            self.assertEqual((self.gate / "state.json").read_bytes(), before)

    def test_bool_or_discontinuous_receipt_generation_is_never_confirmed(self):
        self.backend.stop(self.operation, "limit", 0)
        path = self.gate / "transition.json"
        original = disk.read_private(path)
        for field, bad in (("previous_generation", False), ("generation", True),
                           ("previous_generation", 0.0), ("generation", 2),
                           ("previous_generation", -1), ("generation", adapter.MAX_GENERATION + 1)):
            value = copy.deepcopy(original)
            value["receipt"][field] = bad
            disk.durable_write(path, value)
            before = path.read_bytes()
            with self.subTest(field=field, value=bad), self.assertRaises(disk.CutoverError):
                self.backend.query_operation(self.operation)
            self.assertEqual(path.read_bytes(), before)
        disk.durable_write(path, original)
        self.assertTrue(self.backend.query_operation(self.operation)["durable"])

    def test_transition_state_bool_and_duplicate_argument_bool_fail_closed(self):
        self.backend.stop(self.operation, "limit", 0)
        with self.assertRaises(disk.CutoverError):
            self.backend.stop(self.operation, "limit", False)
        path = self.gate / "transition.json"
        value = disk.read_private(path)
        value["state"]["generation"] = True
        disk.durable_write(path, value)
        before = path.read_bytes()
        for action in (lambda: self.backend.snapshot(self.identity, 1),
                       lambda: self.backend.query_operation(self.operation)):
            with self.assertRaises(disk.CutoverError):
                action()
        self.assertEqual(path.read_bytes(), before)

    def test_gate_state_shape_and_generation_exhaustion_reject_before_mutation(self):
        original = disk.read_private(self.gate / "state.json")
        variants = [dict(original, generation=adapter.MAX_GENERATION), dict(original, unexpected=True)]
        incomplete = copy.deepcopy(original)
        incomplete["entrances"].pop("filter")
        variants.append(incomplete)
        partial = copy.deepcopy(original)
        partial["entrances"]["filter"] = "closed"
        variants.append(partial)
        for value in variants:
            disk.durable_write(self.gate / "state.json", value)
            before = (self.gate / "state.json").read_bytes()
            with self.subTest(state=value), self.assertRaises(disk.CutoverError):
                self.backend.stop(self.operation, "limit", value["generation"])
            self.assertEqual((self.gate / "state.json").read_bytes(), before)
            self.assertFalse((self.gate / "transition.json").exists())

    def test_raw_component_inspect_binding_rejects_changed_restart_or_image(self):
        binding = {"id": "a" * 64, "image": "sha256:" + "b" * 64, "started_at": "2026-10-23T00:00:00Z",
                   "restart_count": 0, "revision": adapter.SOURCE_REVISION}
        row = {"Name": "/api", "Id": binding["id"], "Image": binding["image"], "RestartCount": 0,
               "State": {"StartedAt": binding["started_at"], "Running": True, "OOMKilled": False},
               "Config": {"Labels": {"org.opencontainers.image.revision": binding["revision"]}}}
        self.assertEqual(adapter.validate_inspect([row], {"api": binding}), {"api": binding})
        for field, value in (("RestartCount", 1), ("RestartCount", False), ("Image", "sha256:" + "c" * 64)):
            wrong = copy.deepcopy(row)
            wrong[field] = value
            with self.subTest(field=field), self.assertRaises(Exception):
                adapter.validate_inspect([wrong], {"api": binding})

    def test_permission_symlink_and_lock_budget_fail_closed(self):
        with self.assertRaises(Exception):
            with adapter.held_lock(self.gate / "locks/invalid.lock", True, 3):
                pass
        target = self.root / "target"
        target.write_bytes(b"")
        target.chmod(0o600)
        (self.gate / "locks/release.lock").symlink_to(target)
        with self.assertRaises(OSError):
            self.backend.stop(self.operation, "stop", 0)

    def test_incomplete_stop_transition_never_reopens_ingress(self):
        path = self.gate / "transition.json.pending"
        with path.open("xb") as stream:
            stream.write(b'{"receipt":')
        path.chmod(0o600)
        with self.assertRaises(adapter.AdapterBlocked):
            self.backend.accept_isolated("webhook", b"refused")
        with self.assertRaises(Exception):
            self.backend.query_operation(self.operation)
        self.assertEqual(path.read_bytes(), b'{"receipt":')

    def test_broken_pending_symlink_is_unconfirmed_not_absent(self):
        path = self.gate / "transition.json.pending"
        path.symlink_to(self.root / "does-not-exist")
        with self.assertRaises(adapter.AdapterBlocked):
            self.backend.accept_isolated("webhook", b"refused")
        with self.assertRaises(Exception):
            self.backend.stop(self.operation, "stop", 0)
        self.assertTrue(path.is_symlink())

    def test_inventory_count_and_base64_size_bounds(self):
        with patch.object(adapter, "MAX_RECORDS", 1):
            self.backend.accept_isolated("webhook", b"first")
            with self.assertRaises(Exception):
                self.backend.accept_isolated("webhook", b"second")
        row = {"body_b64": "A" * (((adapter.MAX_BODY + 2) // 3) * 4 + 1),
               "body_sha256": "a" * 64, "entrance": "webhook"}
        disk.durable_write(self.gate / "admissions/oversize.json", row)
        with patch.object(adapter.base64, "b64decode", side_effect=AssertionError("must reject before decode")):
            # Scan may visit the valid first row; test the oversized-only directory separately.
            isolated = self.root / "only-oversize"
            adapter.init_isolation(isolated, self.manifest)
            disk.durable_write(isolated / "admissions/oversize.json", row)
            with self.assertRaises(disk.CutoverError):
                adapter.ProductionHandoffAdapter(isolated, self.manifest, mode="isolation").snapshot(self.identity, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
