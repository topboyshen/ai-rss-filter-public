"""Bridge protocol regressions; injected PG replies are not native PG proof."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import miniflux_handoff_adapter as bridge  # noqa: E402
import handoff_runtime_adapter as runtime  # noqa: E402
from durable_handoff_cutover import CutoverError, durable_write, read_private  # noqa: E402


class ProtocolClient:
    """Exact Go private HTTP schema emulator; actual Go+PG tested separately."""
    def __init__(self, binding):
        self.binding = binding
        self.calls = []
        self.active, self.uncertain = 0, 0
        self.current = binding["initial_operation_id"]
        self.generation = binding["generation"]
        self.action, self.state = "initialize", "open"
        self.lost = False
        self.transform = lambda status, raw: (status, raw)

    def receipt(self):
        confirmed = self.state == "open" or not (self.active or self.uncertain)
        if self.state != "open" and confirmed:
            self.state = "closed"
        return {"schema": 1, "manifest": self.binding["manifest"], "operation_id": self.current,
                "action": self.action, "previous_generation": self.generation - 1, "generation": self.generation,
                "state": self.state, "durable": True, "confirmed": confirmed,
                "active_leases": self.active, "uncertain_leases": self.uncertain}

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path == "/registration":
            return self.transform(200, {"schema": 1, "manifest": self.binding["manifest"],
                                       "protocol": "miniflux-pg-handoff-v1", "business_open_not_assumed": True})
        if method == "POST":
            if path == "/pause":
                assert self.state == "open"
                self.action, self.state = "pause", "closing"
            elif path == "/resume":
                assert self.state == "closed" and payload["closed_operation_id"] == self.current
                self.action, self.state = "resume", "open"
            else:
                raise AssertionError("unexpected mutation")
            self.current, self.generation = payload["operation_id"], self.generation + 1
            if self.lost:
                raise TimeoutError("fixture lost ACK after original PG operation")
        elif path != "/operations/" + self.current:
            return 409, {"confirmed": False, "result": {}}
        receipt = self.receipt()
        return self.transform(200 if receipt["confirmed"] else 409, {"confirmed": receipt["confirmed"], "result": receipt})


class MinifluxBridgeTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="miniflux-host-bridge-", dir=parent))
        component = {"id": "1" * 64, "image": "sha256:" + "2" * 64, "revision": "3" * 40,
                     "started_at": "2026-10-23T00:00:00.123456789Z", "restart_count": 0,
                     "role": "miniflux", "user": "0", "memory": 768 * 1024 * 1024, "pids": 256,
                     "mounts": [{"Type": "volume", "Source": str(self.root), "Destination": "/state", "RW": True}]}
        self.rows = [{"Id": component["id"], "Name": "/sender", "Image": component["image"], "RestartCount": 0,
                      "Config": {"Env": [], "User": "", "Labels": {"org.opencontainers.image.revision": component["revision"],
                                                                          "io.ai-filter.handoff.role": "miniflux"}},
                      "State": {"Running": True, "OOMKilled": False, "Paused": False, "Restarting": False,
                                "StartedAt": component["started_at"]},
                      "HostConfig": {"Memory": component["memory"], "PidsLimit": component["pids"]}, "Mounts": component["mounts"]}]
        self.binding = {"schema": 1, "scope": "isolation", "run_id": "4" * 32, "control_id": "miniflux",
                        "component_name": "sender", "component": component, "manifest": "5" * 64,
                        "application_source_sha256": "6" * 64, "source_sha256": bridge.source_sha256(),
                        "generation": 0, "initial_operation_id": "7" * 64, "entrances": list(bridge.ENTRANCES)}
        self.client = ProtocolClient(self.binding)
        self.control = bridge.MinifluxPauseControl(self.binding, self.client, lambda: copy.deepcopy(self.rows), self.root / "evidence")
        self.operation = "8" * 64

    def stop(self):
        return self.control.stop(self.operation, 0, "explicit isolation stop")

    def approval(self):
        return {"schema": 1, "scope": "isolation", "binding_sha256": self.control.binding_sha256,
                "closed_operation_id": self.operation, "closed_generation": 1, "operation_id": "9" * 64,
                "separately_approved": True, "approval_reference": "synthetic-test-only-explicit-approval"}

    def posts(self):
        return [call for call in self.client.calls if call[0] == "POST"]

    def test_bound_actual_protocol_stop_and_same_operation_query(self):
        result = self.stop()
        runtime._receipt(result, "miniflux", {**self.control.registration(), "generation": 0},
                         self.operation, "explicit isolation stop")
        self.assertEqual(result, self.control.query(self.operation))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0][1], "/pause")
        self.assertEqual(read_private(self.root / "evidence/pause.confirmed.json")["pg_receipt"]["state"], "closed")

    def test_inflight_pause_keeps_original_and_query_after_natural_finish(self):
        self.client.active = 2
        with self.assertRaises(bridge.PausePending):
            self.stop()
        path = self.root / "evidence/pause-response.json"
        original = path.read_bytes()
        self.assertEqual(read_private(path)["status"], 409)
        self.assertEqual(read_private(path)["response"]["result"]["active_leases"], 2)
        self.client.active = 0
        self.assertEqual(self.control.query(self.operation)["inflight"], 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.posts()), 1)

    def test_unpreserved_unknown_does_not_become_zero(self):
        self.client.uncertain = 1
        with self.assertRaises(CutoverError):
            self.stop()
        with self.assertRaises(CutoverError):
            self.control.query(self.operation)
        self.assertFalse((self.root / "evidence/pause.confirmed.json").exists())
        self.assertEqual(len(self.posts()), 1)

    def test_lost_ack_reconstruct_queries_no_resend(self):
        self.client.lost = True
        with self.assertRaises(TimeoutError):
            self.stop()
        restored = bridge.MinifluxPauseControl(self.binding, self.client, lambda: self.rows, self.root / "evidence")
        self.assertTrue(restored.query(self.operation)["durable"])
        with self.assertRaises(CutoverError):
            restored.stop(self.operation, 0, "explicit isolation stop")
        self.assertEqual(len(self.posts()), 1)

    def test_old_or_wrong_manifest_registration_never_issues_pause(self):
        def transform(status, raw):
            if "protocol" in raw:
                raw["protocol"] = "legacy-ordinary-202"
            return status, raw
        self.client.transform = transform
        with self.assertRaises(CutoverError):
            self.stop()
        self.assertEqual(self.posts(), [])

    def test_initial_operation_changed_blocks_before_pause(self):
        self.client.current = "a" * 64
        with self.assertRaises(CutoverError):
            self.stop()
        self.assertEqual(self.posts(), [])

    def test_actual_image_or_start_changed_blocks_before_http(self):
        self.rows[0]["Image"] = "sha256:" + "a" * 64
        with self.assertRaises(CutoverError):
            self.stop()
        self.assertEqual(self.client.calls, [])

    def test_identity_changed_after_http_cannot_confirm(self):
        def transform(status, raw):
            if raw.get("result", {}).get("action") == "pause":
                self.rows[0]["RestartCount"] = 1
            return status, raw
        self.client.transform = transform
        with self.assertRaises(CutoverError):
            self.stop()
        self.assertFalse((self.root / "evidence/pause.confirmed.json").exists())

    def test_receipt_bool_manifest_generation_action_corruption_rejected(self):
        original = self.client.receipt()
        original.update(action="pause", operation_id=self.operation, previous_generation=0, generation=1, state="closed")
        for key, value in (("schema", True), ("generation", True), ("active_leases", False),
                           ("manifest", "a" * 64), ("action", "resume"), ("operation_id", "b" * 64),
                           ("confirmed", False), ("uncertain_leases", 1)):
            bad = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(CutoverError):
                bridge._pg_receipt(bad, self.binding["manifest"], self.operation, 1, "pause", "closed")

    def test_local_intent_sync_failure_does_not_call_pause(self):
        original = bridge.durable_write
        def fail(path, value):
            if path.name == "pause.intent.json":
                raise OSError("synthetic fsync failure")
            return original(path, value)
        with patch.object(bridge, "durable_write", side_effect=fail), self.assertRaises(OSError):
            self.stop()
        self.assertEqual(self.posts(), [])

    def test_visible_confirmed_receipt_sync_failure_no_success_reconstruct_query(self):
        original = bridge.durable_write
        def fail(path, value):
            original(path, value)
            if path.name == "pause.confirmed.json":
                raise OSError("synthetic directory sync failure")
        with patch.object(bridge, "durable_write", side_effect=fail), self.assertRaises(OSError):
            self.stop()
        self.assertTrue(self.control.query(self.operation)["durable"])
        self.assertEqual(len(self.posts()), 1)

    def test_conflicting_original_receipt_not_overwritten(self):
        self.stop()
        path = self.root / "evidence/pause.confirmed.json"
        bad = read_private(path)
        bad["pg_receipt"]["generation"] = 2
        durable_write(path, bad)
        before = path.read_bytes()
        with self.assertRaises(CutoverError):
            self.control.query(self.operation)
        self.assertEqual(path.read_bytes(), before)

    def test_production_or_source_drift_or_other_entrances_forbidden(self):
        for key, value in (("scope", "production"), ("source_sha256", "a" * 64),
                           ("entrances", list(bridge.ENTRANCES) + ["webhook"]), ("generation", False)):
            with self.subTest(key=key), self.assertRaises(CutoverError):
                bridge.checked_binding({**self.binding, key: value})

    def test_query_unknown_does_not_pause(self):
        with self.assertRaises(FileNotFoundError):
            self.control.query(self.operation)
        self.assertEqual(self.posts(), [])

    def test_query_evidence_capacity_fails_closed_no_cleanup(self):
        self.stop()
        for _ in range(32):
            self.control.query(self.operation)
        with self.assertRaises(CutoverError):
            self.control.query(self.operation)
        self.assertEqual(len(list((self.root / "evidence").glob("query-*.json"))), 32)

    def test_resume_never_implicit_requires_separate_exact_approval(self):
        self.stop()
        self.control.query(self.operation)
        for key, value in (("scope", "production"), ("separately_approved", False),
                           ("binding_sha256", "a" * 64), ("closed_generation", True),
                           ("closed_operation_id", "a" * 64)):
            with self.subTest(key=key), self.assertRaises((CutoverError, FileNotFoundError)):
                self.control.resume({**self.approval(), key: value})
        self.assertEqual(len(self.posts()), 1)
        result = self.control.resume(self.approval())
        self.assertEqual(result["receipt"]["generation"], 2)
        self.assertFalse(result["switch_authorized"])
        self.assertEqual(len(self.posts()), 2)

    def test_lost_resume_ack_queries_original_only(self):
        self.stop()
        self.client.lost = True
        approval = self.approval()
        with self.assertRaises(TimeoutError):
            self.control.resume(approval)
        self.assertEqual(self.control.query_resume(approval)["receipt"]["state"], "open")
        with self.assertRaises(CutoverError):
            self.control.resume(approval)
        self.assertEqual(len(self.posts()), 2)

    def test_native_transport_delegates_bridge_without_boolean_shortcut(self):
        transport = runtime.NativeDockerTransport({}, {}, controls={"miniflux": self.control})
        first = transport.stop("miniflux", self.operation, 0, "explicit isolation stop")
        self.assertEqual(first, transport.query("miniflux", self.operation))

    def test_frozen_binding_mutation_rejected(self):
        self.binding["application_source_sha256"] = "a" * 64
        with self.assertRaises(CutoverError):
            self.control.registration()

    def test_application_source_hash_is_binding_not_runtime_source_claim(self):
        first = self.control.registration()
        self.assertEqual(first["binding_sha256"], hashlib.sha256(bridge.encoded(self.binding)).hexdigest())
        self.assertNotIn("source_proven_by_label", first)

    def test_only_exact_durable_pending_is_distinct_from_fatal_failure(self):
        self.client.active = 1
        with self.assertRaises(bridge.PausePending):
            self.stop()
        original = self.client.receipt()
        for key, value in (("manifest", "a" * 64), ("operation_id", "b" * 64), ("generation", True),
                           ("previous_generation", 1), ("durable", False), ("active_leases", False),
                           ("uncertain_leases", -1), ("state", "closed"), ("action", "resume")):
            with self.subTest(key=key), self.assertRaises(CutoverError) as raised:
                self.control._confirmed(409, {"confirmed": False, "result": {**original, key: value}},
                                        self.operation, 1, "pause", "closed")
            self.assertNotIsInstance(raised.exception, bridge.PausePending)
        self.rows[0]["RestartCount"] = 1
        with self.assertRaises(CutoverError) as raised:
            self.control.query(self.operation)
        self.assertNotIsInstance(raised.exception, bridge.PausePending)

    def test_unknown_ordinary_409_or_202_are_not_pollable_pending(self):
        for status, raw in ((409, {"confirmed": False, "result": {}}), (202, {"accepted": True}),
                            (409, {"confirmed": True, "result": self.client.receipt()})):
            with self.subTest(status=status), self.assertRaises(CutoverError) as raised:
                self.control._confirmed(status, raw, self.operation, 1, "pause", "closed")
            self.assertNotIsInstance(raised.exception, bridge.PausePending)


if __name__ == "__main__":
    unittest.main()
