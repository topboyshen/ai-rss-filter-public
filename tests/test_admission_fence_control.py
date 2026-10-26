"""Isolation-only operation authorization; real core combinations added below."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fence_control_test", ROOT / "scripts/control_admission_fence.py")
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


def write_private(path, value):
    raw = json.dumps(value, sort_keys=True).encode() if not isinstance(value, bytes) else value
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


class FenceControlTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("AI_FILTER_HANDOFF_EVIDENCE_DIR")
        if evidence:
            Path(evidence).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=evidence)).resolve()
        self.binding = self.root / "binding.json"
        self.policy = self.root / "policy.json"
        self.authorization = self.root / "authorization.json"
        self.now = int(time.time())
        write_private(self.binding, {"synthetic": "binding is not live inspect"})
        write_private(self.policy, {"not_before": self.now - 1, "expires_at": self.now + 60,
                                    "max_admissions": 10, "max_bytes": 65536})

    def request(self, action="snapshot", *, generation=0, operation=None):
        operation = operation if action in {"snapshot", "provision"} else operation or "a" * 64
        args = SimpleNamespace(action=action, root=self.root / "fence", binding=self.binding,
                               policy=self.policy if action == "provision" else None,
                               authorization=self.authorization, operation_id=operation,
                               expected_generation=generation, reason="synthetic_operator", wait_seconds=0.1)
        authorization = {"schema": 1, "scope": "isolation", "action": action, "root": str(args.root),
                         "binding_sha256": hashlib.sha256(self.binding.read_bytes()).hexdigest(),
                         "policy_sha256": hashlib.sha256(self.policy.read_bytes()).hexdigest() if args.policy else None,
                         "operation_id": operation, "expected_generation": generation, "reason": args.reason,
                         "not_before": self.now - 1, "expires_at": self.now + 60}
        write_private(self.authorization, authorization)
        return args, authorization

    def test_authorization_is_bound_to_exact_action_input_bytes_and_root(self):
        args, _ = self.request("provision")
        root, binding, policy, auth = control.validate_request(args, self.now)
        self.assertEqual(root, self.root / "fence")
        self.assertEqual(binding["synthetic"], "binding is not live inspect")
        self.assertEqual(policy["max_admissions"], 10)
        self.assertEqual(auth["scope"], "isolation")
        self.assertFalse(root.exists())

    def test_production_scope_unknown_keys_expiry_and_equal_bool_fail_before_core(self):
        changes = [{"scope": "production"}, {"extra": True}, {"expires_at": self.now},
                   {"not_before": self.now + 1}, {"schema": True}, {"expected_generation": False},
                   {"action": "open-trial"}, {"binding_sha256": "f" * 64}]
        for change in changes:
            args, authorization = self.request()
            write_private(self.authorization, {**authorization, **change})
            with self.subTest(change=change), patch.object(control, "core_module") as load, \
                    self.assertRaises(control.ControlRefused):
                control.execute(args, now=self.now)
            load.assert_not_called()
            self.assertFalse(args.root.exists())

    def test_changed_binding_bytes_are_not_accepted_as_same_logical_json(self):
        args, _ = self.request()
        original = self.binding.read_bytes()
        write_private(self.binding, original + b"\n")
        with self.assertRaises(control.ControlRefused):
            control.validate_request(args, self.now)

    def test_private_input_rejects_permissions_symlinks_duplicates_and_oversize(self):
        bad = self.root / "bad.json"
        for raw in (b'{"same":1,"same":2}', b'{"bad":NaN}', b"x" * (control.MAX_INPUT + 1)):
            write_private(bad, raw)
            with self.subTest(raw=raw[:30]), self.assertRaises(control.ControlRefused):
                control.private_input(bad)
        write_private(bad, {})
        bad.chmod(0o644)
        with self.assertRaises(control.ControlRefused):
            control.private_input(bad)
        link = self.root / "link.json"
        link.symlink_to(self.binding)
        with self.assertRaises(OSError):
            control.private_input(link)

    def test_operation_generation_wait_and_policy_misuse_rejected(self):
        cases = [("query", "operation_id", "short"), ("snapshot", "operation_id", "a" * 64),
                 ("snapshot", "expected_generation", True), ("snapshot", "wait_seconds", float("nan")),
                 ("snapshot", "wait_seconds", 5.1), ("snapshot", "policy", self.policy)]
        for action, key, value in cases:
            args, _ = self.request(action)
            setattr(args, key, value)
            with self.subTest(key=key, value=value), self.assertRaises(control.ControlRefused):
                control.validate_request(args, self.now)

    def test_cli_makes_one_action_and_never_upgrades_result_to_queue_or_switch_proof(self):
        calls = []
        original = {"state": "frozen", "durable": True, "queue_checked": False, "switch_authorized": False}
        class Core:
            @staticmethod
            def provision(root, binding, policy):
                calls.append(("provision", root))
                return original
            class Fence:
                def __init__(self, root, binding, lock_seconds):
                    self.root = root
                def open_trial(self, *args):
                    calls.append(("open-trial", args))
                    return original
                def stop_new(self, *args):
                    calls.append(("stop-new", args))
                    return original
                def freeze(self, *args, **kwargs):
                    calls.append(("freeze", args))
                    return original
                def query_operation(self, *args, **kwargs):
                    calls.append(("query", args))
                    return original
                def snapshot(self):
                    calls.append(("snapshot",))
                    return original
        for action in control.ACTIONS:
            args, _ = self.request(action)
            with self.subTest(action=action), patch.object(control, "core_module", return_value=Core):
                before = len(calls)
                result = control.execute(args, now=self.now)
                self.assertEqual(len(calls), before + 1)
                self.assertEqual(calls[-1][0], action)
                self.assertIs(result["result"], original)
                self.assertFalse(result["switch_authorized"])
                self.assertFalse(result["queue_checked"])
                self.assertFalse(result["runtime_inspect_verified"])

    def test_unknown_operation_failure_is_not_retried_or_given_success_exit(self):
        args, _ = self.request("query")
        argv = ["query", "--root", str(args.root), "--binding", str(args.binding),
                "--authorization", str(args.authorization), "--operation-id", args.operation_id,
                "--reason", args.reason]
        with patch.object(control, "execute", side_effect=TimeoutError("synthetic")) as execute, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(control.main(argv), 2)
        self.assertEqual(execute.call_count, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "not_confirmed")
        self.assertFalse(result["retry_performed"])
        self.assertFalse(result["switch_authorized"])

    def actual_core(self):
        module = control.core_module()
        binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64,
                   "strategy": "synthetic-policy", "source_sha256": module.source_sha256(),
                   "deployment_nonce": "c" * 32}
        write_private(self.binding, binding)
        return module, binding

    def test_actual_core_two_phase_stop_inflight_timeout_exact_query_and_no_auto_resume(self):
        module, binding = self.actual_core()
        provision, _ = self.request("provision")
        result = control.execute(provision, now=self.now)
        self.assertEqual(result["result"]["status"], "provisioned_frozen")
        fence = module.Fence(provision.root, binding)
        self.assertEqual(fence.snapshot()["phase"], "frozen")
        with self.assertRaises(module.AdmissionClosed):
            fence.reserve_lease("synthetic-intake", "intake", 10)
        opening, _ = self.request("open-trial", operation="1" * 64)
        self.assertEqual(control.execute(opening, now=self.now)["result"]["phase"], "open")
        first = fence.reserve_lease("synthetic-intake", "intake", 10)
        first.finish(True)
        stopping, _ = self.request("stop-new", generation=1, operation="2" * 64)
        self.assertEqual(control.execute(stopping, now=self.now)["result"]["phase"], "draining")
        with self.assertRaises(module.AdmissionClosed):
            fence.reserve_lease("synthetic-intake", "intake", 10)
        draining = fence.reserve_lease("synthetic-persisted-consumer", "drain")
        freezing, _ = self.request("freeze", generation=2, operation="3" * 64)
        try:
            with self.assertRaises(Exception) as failed:
                control.execute(freezing, now=self.now)
            self.assertEqual(type(failed.exception).__name__, "AdmissionBusy")
            self.assertEqual(fence.snapshot()["phase"], "freezing")
            self.assertTrue((provision.root / "operations" / ("3" * 64 + ".intent.json")).exists())
        finally:
            draining.finish(True)
        querying, _ = self.request("query", generation=2, operation="3" * 64)
        recovered = control.execute(querying, now=self.now)
        self.assertEqual(recovered["result"]["phase"], "frozen")
        self.assertTrue(recovered["result"]["activity_drained"])
        self.assertFalse(recovered["result"]["queue_checked"])
        self.assertFalse(recovered["switch_authorized"])
        receipt = (provision.root / "operations" / ("3" * 64 + ".json")).read_bytes()
        self.assertEqual(control.execute(querying, now=self.now)["result"], recovered["result"])
        self.assertEqual((provision.root / "operations" / ("3" * 64 + ".json")).read_bytes(), receipt)
        for kind in ("intake", "drain"):
            with self.subTest(kind=kind), self.assertRaises(module.AdmissionClosed):
                module.Fence(provision.root, binding).reserve_lease("synthetic-after-restart", kind)

    def test_actual_core_lease_uncertainty_never_becomes_successful_freeze(self):
        module, binding = self.actual_core()
        args, _ = self.request("provision")
        control.execute(args, now=self.now)
        args, _ = self.request("open-trial", operation="1" * 64)
        control.execute(args, now=self.now)
        fence = module.Fence(args.root, binding)
        lease = fence.reserve_lease("synthetic-uncertain", "intake", 10)
        lease.finish(False)
        args, _ = self.request("stop-new", generation=1, operation="2" * 64)
        control.execute(args, now=self.now)
        args, _ = self.request("freeze", generation=2, operation="3" * 64)
        with self.assertRaises(Exception) as failed:
            control.execute(args, now=self.now)
        self.assertEqual(type(failed.exception).__name__, "AdmissionUncertain")
        original = {path: path.read_bytes() for folder in ("leases", "finishes")
                    for path in (args.root / folder).iterdir()}
        query, _ = self.request("query", generation=2, operation="3" * 64)
        with self.assertRaises(Exception):
            control.execute(query, now=self.now)
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertFalse((args.root / "operations" / ("3" * 64 + ".json")).exists())

    def test_actual_core_wrong_source_binding_and_expired_policy_never_open(self):
        module, binding = self.actual_core()
        write_private(self.binding, {**binding, "source_sha256": "f" * 64})
        args, _ = self.request("provision")
        with self.assertRaises(Exception):
            control.execute(args, now=self.now)
        self.assertFalse(args.root.exists())
        write_private(self.binding, binding)
        write_private(self.policy, {"not_before": self.now - 60, "expires_at": self.now - 1,
                                    "max_admissions": 10, "max_bytes": 65536})
        args, _ = self.request("provision")
        control.execute(args, now=self.now)
        args, _ = self.request("open-trial", operation="1" * 64)
        with self.assertRaises(Exception) as failed:
            control.execute(args, now=self.now)
        self.assertEqual(type(failed.exception).__name__, "AdmissionError")
        self.assertEqual(module.Fence(args.root, binding).snapshot()["phase"], "frozen")
        with self.assertRaises(module.AdmissionClosed):
            module.Fence(args.root, binding).reserve_lease("synthetic-expired", "intake", 1)


if __name__ == "__main__":
    unittest.main()
