"""Stdlib executable protocol tests: owned subprocess + HTTP + durable files.

No application/model/Docker/SSH invocation. Fixture component identities are
explicitly synthetic; these tests do not establish a production cutover adapter.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import durable_handoff_cutover as cutover  # noqa: E402
from durable_handoff_cutover_fixture import Fixture  # noqa: E402


class CutoverProtocolTests(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
        if evidence:
            Path(evidence).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=evidence))
        self.run_id = "a" * 32
        self.token = "b" * 64
        self.processes = []
        self.handles = []
        self.port = None
        self.manifest = None

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    self.http("/shutdown", {}, timeout=0.5)
                except (OSError, http.client.HTTPException):
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Only an exact Popen child created by this test is eligible.
                    process.terminate()
                    process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
        for handle in self.handles:
            handle.close()
        # Preserve fixtures, original bytes and stopped-run journals for review.

    def start(self, faults=None, port=0):
        config = self.root / ("config-" + str(len(self.processes)) + ".json")
        cutover.durable_write(config, {"mode": "isolation", "run_id": self.run_id,
                                       "token": self.token, "faults": faults or {}})
        stderr = (self.root / ("stderr-" + str(len(self.processes)) + ".log")).open("xb")
        self.handles.append(stderr)
        process = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "durable_handoff_cutover_fixture.py"),
            "--root", str(self.root / "adapter"), "--config", str(config), "--port", str(port)],
            stdout=subprocess.PIPE, stderr=stderr, text=True)
        self.processes.append(process)
        ready, _, _ = select.select([process.stdout], [], [], 4)
        self.assertTrue(ready, "owned adapter did not announce readiness")
        line = process.stdout.readline()
        self.assertTrue(line, f"owned adapter exited before readiness; stderr retained in {self.root}")
        identity = json.loads(line)
        self.port = identity["port"]
        if self.manifest is None:
            self.manifest = {"schema": 1, "mode": "isolation", "production_adapter_bound": False,
                "run_id": self.run_id, "token": self.token, "endpoint": f"http://127.0.0.1:{self.port}",
                "adapter_identity": identity["adapter_identity"], "adapter_sha256": identity["adapter_sha256"],
                "entrances": list(cutover.ENTRANCES),
                "targets": {role: f"synthetic-new-{role}@sha256:" + letter * 64
                            for role, letter in (("receiver", "c"), ("sender", "d"))},
                "budget": {"total_seconds": 10, "drain_seconds": 0.3, "request_seconds": 0.2, "max_polls": 30}}
        self.assertEqual(self.manifest["endpoint"], f"http://127.0.0.1:{self.port}")
        return process

    def http(self, path, payload=None, raw=None, timeout=2):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            body = raw if raw is not None else cutover.encoded(payload) if payload is not None else None
            conn.request("POST" if body is not None else "GET", path, body=body,
                         headers={"X-Isolation-Token": self.token})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def controller(self):
        return cutover.Controller(self.manifest, self.root / "journal")

    def run_controller(self, resume=False):
        result = self.controller().run(resume)
        path = self.root / ("result-" + str(len(list(self.root.glob("result-*.json")))) + ".json")
        cutover.durable_write(path, result)
        return result

    def paused(self):
        controller = self.controller()
        controller.apply("pause", controller.inspect())
        controller.journal.close()

    def test_happy_protocol_single_switch_and_duplicate_controller(self):
        self.start()
        result = self.run_controller()
        self.assertEqual(result["status"], "passed_isolation_protocol_only")
        self.assertEqual(result["final"]["switch_counts"], {"receiver": 1, "sender": 1})
        self.assertEqual(result["final"]["schema"], 132)
        second = self.run_controller()
        self.assertEqual(second["final"]["switch_counts"], result["final"]["switch_counts"])
        self.assertFalse(result["production_adapter_bound"])

    def test_all_real_http_ingresses_buffer_original_and_resume_once(self):
        self.start()
        # Freeze the existing hard limit before the first controller/journal;
        # fourteen durable transfers are not a 200ms performance assertion.
        self.manifest["budget"]["request_seconds"] = 2
        self.paused()
        bodies = {}
        for entrance in cutover.ENTRANCES:
            raw = cutover.encoded({"synthetic": entrance})
            status, receipt = self.http("/entry/" + entrance, raw=raw)
            self.assertEqual(status, 202)
            self.assertIs(receipt["durable"], True)
            delivery = hashlib.sha256(raw).hexdigest()
            bodies[delivery] = raw
            stored = cutover.read_private(self.root / "adapter/buffer" / (delivery + ".json"))
            self.assertEqual(base64.b64decode(stored["body_b64"]), raw)
        result = self.run_controller()
        self.assertEqual(result["status"], "passed_isolation_protocol_only")
        self.assertEqual(set(result["final"]["receipt_delivery_ids"]), set(bodies))
        self.assertEqual(set(result["final"]["retained_delivery_ids"]), set(bodies))
        for delivery, raw in bodies.items():
            saved = cutover.read_private(self.root / "adapter/receipts" / (delivery + ".json"))
            self.assertEqual(base64.b64decode(saved["body_b64"]), raw)

    def test_inflight_naturally_completes_before_zero_gate(self):
        self.start()
        _, accepted = self.http("/entry/webhook", raw=b"synthetic-inflight")
        self.paused()
        def finish():
            time.sleep(0.06)
            self.http("/fixture/complete", {"kind": "pending", "delivery_id": accepted["delivery_id"]})
        thread = threading.Thread(target=finish)
        thread.start()
        result = self.run_controller()
        thread.join(timeout=2)
        self.assertEqual(result["status"], "passed_isolation_protocol_only")
        self.assertIn(accepted["delivery_id"], result["final"]["retained_delivery_ids"])

    def test_pending_timeout_preserves_bytes_and_never_switches(self):
        self.start()
        _, accepted = self.http("/entry/webhook", raw=b"must-not-drop")
        path = self.root / "adapter/pending" / (accepted["delivery_id"] + ".json")
        original = path.read_bytes()
        result = self.run_controller()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(path.read_bytes(), original)
        _, snapshot = self.http("/control/snapshot")
        self.assertTrue(all(value == "buffering" for value in snapshot["entrances"].values()))
        self.assertEqual(snapshot["switch_counts"], {"receiver": 0, "sender": 0})

    def test_original_zero_counters_cannot_be_omitted_or_nonzero(self):
        self.start()
        controller = self.controller()
        controller.apply("pause", controller.inspect())
        snapshot = controller.inspect()
        for key in cutover.ZERO_COUNTERS:
            for invalid in (1, None, True):
                with self.subTest(counter=key, invalid=invalid):
                    value = dict(snapshot, **{key: invalid})
                    with self.assertRaises(cutover.CutoverError):
                        controller.zero_gate(value)
        controller.journal.close()

    def test_actual_archive_pending_file_blocks_switch(self):
        self.start()
        path = self.root / "adapter/blockers/archive.json"
        cutover.durable_write(path, {"counter": "archive_pending", "count": 1})
        result = self.run_controller()
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(path.exists())
        _, snapshot = self.http("/control/snapshot")
        self.assertEqual(snapshot["archive_pending"], 1)
        self.assertEqual(snapshot["switch_counts"]["receiver"], 0)

    def test_blocker_rows_do_not_disappear_under_derived_zero_counters(self):
        self.start()
        for key in ("old_contract_unfinished", "legacy_inflight", "legacy_unpreserved"):
            cutover.durable_write(self.root / "adapter/blockers" / (key + ".json"), {"counter": key, "count": 1})
        status, snapshot = self.http("/control/snapshot")
        self.assertEqual(status, 200)
        for key in ("old_contract_unfinished", "legacy_inflight", "legacy_unpreserved"):
            self.assertEqual(snapshot[key], 1)
        self.assertEqual(self.run_controller()["status"], "stopped")

    def test_old_async_without_original_stops_instead_of_reconstruction(self):
        self.start()
        path = self.root / "adapter/legacy/unknown.json"
        cutover.durable_write(path, {"delivery_id": "e" * 64, "status": "inflight"})
        result = self.run_controller()
        self.assertEqual(result["status"], "stopped")
        self.assertIn("exact durable preservation", result["reason"])
        self.assertTrue(path.exists())

    def test_old_async_exact_original_is_retained_not_regenerated(self):
        self.start()
        raw = b"frozen-old-sender-byte-layout"
        record = {"delivery_id": hashlib.sha256(raw).hexdigest(), "status": "preserved",
                  "body_b64": base64.b64encode(raw).decode()}
        path = self.root / "adapter/legacy/original.json"
        cutover.durable_write(path, record)
        original = path.read_bytes()
        self.assertEqual(self.run_controller()["status"], "passed_isolation_protocol_only")
        self.assertEqual(path.read_bytes(), original)

    def test_lost_ack_is_queried_on_explicit_resume_not_reissued(self):
        self.start({"delay_after": "pause"})
        first = self.run_controller()
        self.assertEqual(first["status"], "stopped")
        time.sleep(0.25)
        second = self.run_controller(resume=True)
        self.assertEqual(second["status"], "passed_isolation_protocol_only")
        self.assertEqual(second["final"]["generation"], 4)
        self.assertEqual(len(list((self.root / "adapter/operations").glob("*.json"))), 4)

    def test_lost_resume_ack_recovers_even_when_ingress_already_open(self):
        self.start({"delay_after": "resume"})
        self.paused()
        raw = b"one-buffered-original-survives-lost-resume-ack"
        status, accepted = self.http("/entry/webhook", raw=raw)
        self.assertEqual(status, 202)
        delivery = accepted["delivery_id"]
        source = self.root / "adapter/buffer" / (delivery + ".json")
        original = source.read_bytes()
        first = self.run_controller()
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(first["phase"], "verified")
        time.sleep(0.25)
        receipt_path = self.root / "adapter/receipts" / (delivery + ".json")
        retained_path = self.root / "adapter/retained" / (delivery + ".json")
        receipt_bytes = receipt_path.read_bytes()
        self.assertEqual(retained_path.read_bytes(), original)
        result = self.run_controller(resume=True)
        self.assertEqual(result["status"], "passed_isolation_protocol_only")
        self.assertEqual(result["final"]["generation"], 4)
        self.assertEqual(result["final"]["receipt_delivery_ids"], [delivery])
        self.assertEqual(result["final"]["retained_delivery_ids"], [delivery])
        self.assertEqual(result["final"]["buffered"], 0)
        self.assertEqual(result["final"]["switch_counts"], {"receiver": 1, "sender": 1})
        self.assertEqual(len(list((self.root / "adapter/operations").glob("*.json"))), 4)
        self.assertEqual(receipt_path.read_bytes(), receipt_bytes)
        self.assertEqual(retained_path.read_bytes(), original)
        self.assertEqual(base64.b64decode(cutover.read_private(receipt_path)["body_b64"]), raw)

    def test_adapter_abrupt_exit_then_restart_recovers_same_operation(self):
        process = self.start({"exit_after": "switch_receiver"})
        result = self.run_controller()
        self.assertEqual(result["status"], "stopped", {"result": result, "evidence": str(self.root)})
        self.assertEqual(process.wait(timeout=2), 73)
        operation = cutover.digest({"run_id": self.run_id, "action": "switch_receiver"})
        receipt_path = self.root / "adapter/operations" / (operation + ".json")
        original = receipt_path.read_bytes()
        self.start(port=self.port)
        requests = []
        actual_request = cutover.Controller.request
        def observed_request(controller, path, payload=None):
            requests.append({"path": path, "payload": payload})
            return actual_request(controller, path, payload)
        with patch.object(cutover.Controller, "request", observed_request):
            result = self.run_controller(resume=True)
        message = {"result": result, "evidence": str(self.root), "requests": requests}
        self.assertEqual(result["status"], "passed_isolation_protocol_only", message)
        self.assertEqual(requests[0], {"path": "/control/operation/" + operation, "payload": None}, message)
        self.assertEqual([r["payload"]["action"] for r in requests if r["payload"]], ["switch_sender", "resume"], message)
        self.assertEqual(receipt_path.read_bytes(), original, message)
        self.assertEqual(result["final"]["switch_counts"], {"receiver": 1, "sender": 1})

    def test_plain_202_is_not_confirmed_control_receipt(self):
        self.start({"ordinary_202": True})
        result = self.run_controller()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["phase"], "prepared")
        self.assertIsNotNone(cutover.read_private(sorted((self.root / "journal/records").glob("*.json"))[-1])["intent"])

    def test_slow_drip_cannot_extend_absolute_http_budget(self):
        self.start({"slow_drip": True})
        started = time.monotonic()
        result = self.run_controller()
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "stopped")
        self.assertLess(elapsed, 1.0)

    def test_atomic_adapter_cas_refuses_stale_generation(self):
        self.start()
        intent = {"operation_id": "e" * 64, "action": "pause", "expected_generation": 99,
                  "manifest_sha256": cutover.digest(self.manifest), "targets": self.manifest["targets"]}
        self.assertEqual(self.http("/control/apply", intent)[0], 503)
        self.assertEqual(self.http("/control/snapshot")[1]["generation"], 0)

    def test_rollback_not_an_adapter_action(self):
        self.start()
        intent = {"operation_id": "e" * 64, "action": "rollback", "expected_generation": 0,
                  "manifest_sha256": cutover.digest(self.manifest), "targets": self.manifest["targets"]}
        self.assertEqual(self.http("/control/apply", intent)[0], 503)

    def test_production_old_images_or_missing_entrypoint_never_connect(self):
        self.start()
        changes = [{"mode": "production"}, {"endpoint": "http://example.invalid"}, {"schema": True},
                   {"production_adapter_bound": True}, {"entrances": list(cutover.ENTRANCES[:-1])},
                   {"targets": {"receiver": "my-ai-filter:2026.10.22.126", "sender": "old"}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((cutover.CutoverError, ValueError)):
                cutover.validate_manifest(dict(self.manifest, **change))

    def test_budget_hard_limits_and_restart_do_not_reset_deadline(self):
        self.start()
        for key, invalid in (("total_seconds", 61), ("drain_seconds", 31), ("request_seconds", 3),
                             ("max_polls", 101), ("max_polls", True), ("total_seconds", float("nan"))):
            candidate = copy.deepcopy(self.manifest)
            candidate["budget"][key] = invalid
            with self.subTest(key=key), self.assertRaises(cutover.CutoverError):
                cutover.validate_manifest(candidate)
        controller = self.controller()
        deadline = controller.state["expires_at"]
        controller.record(expires_at=time.time() - 1)
        controller.journal.close()
        result = self.run_controller(resume=True)
        self.assertEqual(result["status"], "stopped")
        self.assertIn("expired", result["reason"])
        self.assertLess(cutover.read_private(sorted((self.root / "journal/records").glob("*.json"))[-1])["expires_at"], deadline)

    def test_manifest_change_across_restart_is_rejected(self):
        self.start()
        self.paused()
        self.manifest["budget"]["total_seconds"] = 11
        with self.assertRaises(cutover.CutoverError):
            self.controller()

    def test_two_controller_process_locks_cannot_overlap(self):
        self.start()
        first = self.controller()
        with self.assertRaises(cutover.CutoverError):
            self.controller()
        first.journal.close()

    def test_buffer_full_refuses_new_body_keeps_all_originals(self):
        self.start()
        self.paused()
        for index in range(16):
            self.assertEqual(self.http("/entry/webhook", raw=f"synthetic-{index}".encode())[0], 202)
        originals = {path.name: path.read_bytes() for path in (self.root / "adapter/buffer").glob("*.json")}
        self.assertEqual(self.http("/entry/webhook", raw=b"synthetic-overflow")[0], 503)
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / "adapter/buffer").glob("*.json")}, originals)
        self.assertEqual(self.http("/entry/webhook", raw=b"synthetic-0")[0], 202)

    def test_accept_barrier_failure_cannot_return_202(self):
        self.start({"accept_sync_failure": True})
        self.assertEqual(self.http("/entry/webhook", raw=b"synthetic-failure")[0], 503)
        self.assertEqual(list((self.root / "adapter/pending").glob("*.json")), [])

    def test_strict_write_directory_failure_preserves_unconfirmed_bytes(self):
        path = self.root / "strict.json"
        with patch.object(cutover, "sync_directory", side_effect=OSError("synthetic EIO")):
            with self.assertRaises(OSError):
                cutover.durable_write(path, {"bytes": "original"})
        self.assertEqual(cutover.read_private(path), {"bytes": "original"})
        cutover.durable_write(path, {"bytes": "original"})

    def test_partial_journal_is_preserved_and_never_adopted(self):
        journal = cutover.Journal(self.root / "journal")
        path = journal.records / "0000.json.pending"
        with path.open("xb") as stream:
            stream.write(b'{"phase":')
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            journal.latest()
        self.assertEqual(path.read_bytes(), b'{"phase":')
        journal.close()

    def test_complete_journal_pending_recovers_exact_hash_chain(self):
        journal = cutover.Journal(self.root / "journal")
        original = {"sequence": 0, "previous": None, "phase": "prepared"}
        path = journal.records / "0000.json.pending"
        cutover.durable_write(path, original)
        self.assertEqual(journal.latest(), original)
        self.assertTrue((journal.records / "0000.json").exists())
        journal.close()

    def test_visible_journal_final_requires_reconfirmed_barrier(self):
        journal = cutover.Journal(self.root / "journal")
        state = journal.append(None, {"phase": "prepared"})
        original = (journal.records / "0000.json").read_bytes()
        with patch.object(cutover, "sync_directory", side_effect=OSError("still unconfirmed")):
            with self.assertRaises(OSError):
                journal.latest()
        self.assertEqual((journal.records / "0000.json").read_bytes(), original)
        self.assertEqual(journal.latest(), state)
        journal.close()

    def test_visible_operation_query_reconfirms_barrier_without_reswitch(self):
        fixture = Fixture(self.root / "offline-adapter", self.run_id, self.token)
        request = {"operation_id": "e" * 64, "action": "pause", "expected_generation": 0,
                   "manifest_sha256": "f" * 64, "targets": {}}
        receipt = fixture.apply(request)
        path = fixture.root / "operations" / (request["operation_id"] + ".json")
        original = path.read_bytes()
        with patch.object(cutover, "sync_directory", side_effect=OSError("synthetic unconfirmed directory")):
            with self.assertRaises(OSError):
                fixture.confirmed_operation(request["operation_id"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(fixture.confirmed_operation(request["operation_id"]), receipt)
        self.assertEqual(fixture.snapshot()["generation"], 1)

    def test_private_files_duplicate_fields_and_symlinks_fail_closed(self):
        path = self.root / "unsafe.json"
        with path.open("xb") as stream:
            stream.write(b'{"a":1,"a":2}')
        path.chmod(0o600)
        with self.assertRaises(cutover.CutoverError):
            cutover.read_private(path)
        alias = self.root / "alias.json"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            cutover.read_private(alias)


if __name__ == "__main__":
    unittest.main(verbosity=2)
