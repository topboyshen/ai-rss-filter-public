"""Stdlib-only native isolation cases; full pytest integration runs separately."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from services import admission_fence as fence


class AdmissionNativeTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AI_FILTER_HANDOFF_EVIDENCE_DIR")
        self.evidence = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=base))
        self.root = self.evidence / "fence"
        self.binding = {"revision": "1" * 40, "image_id": "sha256:" + "2" * 64,
                        "strategy": "synthetic-native", "source_sha256": fence.source_sha256(),
                        "deployment_nonce": "3" * 32}
        now = int(time.time())
        fence.provision(self.root, self.binding, {"not_before": now - 1, "expires_at": now + 120,
                                               "max_admissions": 32, "max_bytes": 3200})
        self.gate = fence.Fence(self.root, self.binding)
        self.gate.open_trial("1" * 64, 0, "isolated native trial")

    def child(self, source, *, asynchronous=False):
        argv = [sys.executable, "-B", "-c", source, str(self.root), json.dumps(self.binding)]
        if asynchronous:
            return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return subprocess.run(argv, capture_output=True, text=True, timeout=5)

    def test_actual_child_crash_is_not_empty_or_finished(self):
        source = "from services.admission_fence import Fence;import sys,json,os;g=Fence(sys.argv[1],json.loads(sys.argv[2]));l=g.reserve_lease('child','drain');os._exit(0)"
        result = self.child(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gate.snapshot()["orphaned"], 1)
        with self.assertRaises(fence.AdmissionUncertain):
            self.gate.reserve_lease("after_crash")
        self.gate.stop_new("2" * 64, 1, "isolated stop")
        with self.assertRaises(fence.AdmissionUncertain):
            self.gate.freeze("3" * 64, 2, "isolated freeze")

    def test_actual_process_lock_freeze_timeout_and_original_operation_query(self):
        self._assert_process_freeze_waits_for_explicit_release()

    def test_slow_parent_cannot_release_child_before_freeze_timeout(self):
        # Exceed the former child's .3s timer deliberately: elapsed time is no
        # longer permission for the child to finish its lease.
        self._assert_process_freeze_waits_for_explicit_release(parent_delay=0.4)

    def _assert_process_freeze_waits_for_explicit_release(self, parent_delay=0):
        source = "from services.admission_fence import Fence;import sys,json;g=Fence(sys.argv[1],json.loads(sys.argv[2]));l=g.reserve_lease('child','drain');print('held',flush=True);assert sys.stdin.readline()=='release\\n';l.finish()"
        child = self.child(source, asynchronous=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            if parent_delay:
                time.sleep(parent_delay)
            self.gate.stop_new("2" * 64, 1, "isolated stop")
            with self.assertRaises(fence.AdmissionBusy):
                self.gate.freeze("3" * 64, 2, "isolated freeze", wait_seconds=0)
            self.assertIsNone(child.poll())
            stdout, stderr = child.communicate("release\n", timeout=5)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
            receipt = self.gate.query_operation("3" * 64)
            self.assertTrue(receipt["activity_drained"])
            self.assertFalse(receipt["switch_authorized"])
        finally:
            try:
                if not child.stdin.closed:
                    # Even a failed parent assertion releases the known child;
                    # never leave it blocked or kill it to satisfy the test.
                    child.communicate("release\n", timeout=5)
                else:
                    child.wait(timeout=5)
            finally:
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()

    def test_concurrent_exact_quota_then_existing_drain_not_double_charged(self):
        def attempt(_):
            try:
                lease = fence.Fence(self.root, self.binding, lock_seconds=2).reserve_lease("native_http", charge_bytes=100)
                lease.finish()
                return True
            except fence.AdmissionClosed:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(attempt, range(40))), 32)
        self.assertEqual(self.gate.snapshot()["charged_bytes"], 3200)
        self.gate.reserve_lease("existing_task", "drain").finish()
        self.assertEqual(self.gate.snapshot()["intake_count"], 32)

    def test_durable_expiry_and_restart_do_not_reset_monotonic_window(self):
        clock = fence._read(self.root / "state.json")["opening_clock"]
        with patch.object(fence, "_elapsed", return_value=clock["elapsed"] + 121):
            with self.assertRaises(fence.AdmissionClosed):
                fence.Fence(self.root, self.binding).reserve_lease("expired")

    def test_strict_finish_failure_preserves_original_uncertainty(self):
        lease = self.gate.reserve_lease("write")
        original = (self.root / "leases" / (lease.row["lease_id"] + ".json")).read_bytes()
        with patch.object(fence, "_write", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                lease.finish()
        self.assertEqual((self.root / "leases" / (lease.row["lease_id"] + ".json")).read_bytes(), original)
        self.assertEqual(self.gate.snapshot()["orphaned"], 1)

    def test_finish_and_freeze_before_activation_cannot_revive_ended_lease(self):
        lease = self.gate.reserve_lease("claimed", "drain")
        checked, resume = threading.Event(), threading.Event()
        original = fence._require
        entered = []

        def pause_after_fast_check(condition, message):
            original(condition, message)
            if message == "lease ended or inherited after fork" and not checked.is_set():
                checked.set()
                self.assertTrue(resume.wait(3))

        def activate():
            with self.assertRaises(fence.AdmissionClosed):
                with lease.activate():
                    entered.append(True)
            return fence.current_kind()

        with patch.object(fence, "_require", side_effect=pause_after_fast_check):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(activate)
                try:
                    self.assertTrue(checked.wait(3))
                    lease.finish()
                    self.gate.stop_new("2" * 64, 1, "isolated stop")
                    self.assertTrue(self.gate.freeze("3" * 64, 2, "isolated freeze")["activity_drained"])
                finally:
                    resume.set()
                self.assertEqual(future.result(timeout=3), "intake")
        self.assertEqual(entered, [])
        self.assertTrue(lease.activation.acquire(blocking=False))
        lease.activation.release()

    def test_unknown_partial_state_is_retained_and_refuses_new_work(self):
        pending = self.root / "operations" / ("4" * 64 + ".intent.json.pending")
        pending.write_bytes(b"{partial-original")
        pending.chmod(0o600)
        with self.assertRaises(fence.AdmissionClosed):
            self.gate.reserve_lease("blocked")
        self.assertEqual(pending.read_bytes(), b"{partial-original")

    def test_ended_leases_have_no_automatic_gc_and_capacity_is_strict(self):
        for _ in range(3):
            self.gate.reserve_lease("durable_work", "drain").finish()
        with patch.object(fence, "MAX_LEASES", 3):
            with self.assertRaises(fence.AdmissionClosed):
                self.gate.reserve_lease("capacity", "drain")
        self.assertEqual(len(list((self.root / "leases").iterdir())), 3)
        self.assertEqual(len(list((self.root / "finishes").iterdir())), 3)


if __name__ == "__main__":
    unittest.main()
