"""Synthetic files/processes/HTTP; inspect fixtures are not production evidence."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from services import admission_fence as fence  # noqa: E402
from services.receiver_handoff_control import ControlServer, ReceiverControl  # noqa: E402
import production_receiver_driver as driver  # noqa: E402


class FixtureTransport(driver.NativeTransport):
    """Real HTTP, explicitly synthetic inspect rows."""
    def inspect(self, ids):
        assert ids == tuple(row["Id"] for row in self.rows)
        return copy.deepcopy(self.rows)

    def request(self, method, path, value=None):
        self.calls.append((method, path, value))
        result = super().request(method, path, value)
        if method == "POST" and self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("synthetic lost ACK after actual durable operation")
        return result


class ReceiverFixture(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("AI_FILTER_HANDOFF_EVIDENCE_DIR")
        if parent:
            Path(parent).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="receiver-driver-", dir=parent)).resolve()
        now = int(time.time())
        self.binding = {"revision": "a" * 40, "image_id": "sha256:" + "b" * 64,
                        "strategy": "synthetic-only", "source_sha256": fence.source_sha256(), "deployment_nonce": "c" * 32}
        fence.provision(self.root / "fence", self.binding, {"not_before": now - 1, "expires_at": now + 90,
                        "max_admissions": 3, "max_bytes": 12})
        self.fence = fence.Fence(self.root / "fence", self.binding, lock_seconds=0.05)
        self.fence.open_trial("d" * 64, 0, "synthetic bounded test")
        self.control = ReceiverControl(self.fence)
        self.server = ControlServer(self.control, "e" * 64)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.token_file = self.root / "token.json"
        driver.durable_write(self.token_file, {"token": "e" * 64})
        self.transport = FixtureTransport(f"http://127.0.0.1:{self.server.server_port}", self.token_file)
        self.transport.calls, self.transport.lose_ack, self.transport.rows = [], False, []
        components = {}
        for index, role in enumerate(("web", "worker"), 1):
            mounts = [{"Type": "bind", "Source": str(self.root), "Destination": "/fixture", "RW": True}]
            component = {"id": str(index) * 64, "image": self.binding["image_id"], "revision": self.binding["revision"],
                         "started_at": "2026-10-26T00:00:00Z", "restart_count": 0, "role": role,
                         "user": str(os.getuid()), "memory": 768 * 1024**2, "pids": 256, "mounts": mounts}
            components[role] = component
            self.transport.rows.append({"Id": component["id"], "Name": "/" + role, "Image": component["image"],
                "RestartCount": 0, "Config": {"User": component["user"], "Labels": {
                    "org.opencontainers.image.revision": component["revision"]}, "Env": ["AI_FILTER_ROLE=" + role,
                    "AI_FILTER_ADMISSION_MODE=enforced", "AI_FILTER_ADMISSION_ROOT=/fixture/fence",
                    "WEBHOOK_SPOOL_DIR=/fixture/spool",
                    "AI_FILTER_ADMISSION_BINDING=" + fence._encoded(self.binding).decode()]},
                "State": {"Running": True, "OOMKilled": False, "Paused": False, "Restarting": False,
                          "StartedAt": component["started_at"]},
                "HostConfig": {"Memory": component["memory"], "PidsLimit": component["pids"]}, "Mounts": mounts})
        spool = self.root / "spool"
        spool.mkdir(mode=0o700)
        fd = os.open(spool / ".lock", os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.close(fd)
        self.store = driver.receipt_format.ReceiptStore(spool)
        self.record, _ = self.store.accept(b"synthetic original", {"entries": []}, strategy="synthetic", revision="synthetic")
        self.plan = {"schema": 1, "scope": "instrumented_receiver_only", "sources": driver.sources(),
                     "components": components, "registration": self.control.registration(),
                     "host_fence_root": str(self.fence.root), "container_fence_root": "/fixture/fence",
                     "container_spool_root": "/fixture/spool",
                     "spool": str(spool), "spool_identity": driver.spool_identity(spool),
                     "not_before": now - 1, "expires_at": now + 60, "owner": "synthetic operator",
                     "capacity": {"stop_records": 2, "stop_bytes": 200 * 1024**2,
                                  "minimum_free_bytes": driver.receipt_format.MAX_ALIAS_RESERVATION}}
        self.driver = driver.ReceiverDriver(self.plan, self.transport, self.root / "evidence")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # Original private fixture evidence is intentionally retained.

    def posts(self):
        return [row for row in self.transport.calls if row[0] == "POST"]

    def test_stop_new_allows_existing_and_new_durable_consumers_then_freeze(self):
        intake = self.fence.reserve_lease("synthetic_existing", charge_bytes=4)
        receipt = self.driver.operate("stop_new", "f" * 64, "synthetic stop")
        self.assertEqual(receipt["phase"], "draining")
        self.assertFalse(receipt["activity_drained"])
        with self.assertRaises(fence.AdmissionClosed):
            self.fence.reserve_lease("new_intake")
        drain = self.fence.reserve_lease("durable_queue", kind="drain")
        drain.finish(True)
        intake.finish(True)
        frozen = self.driver.operate("freeze", "9" * 64, "synthetic freeze")
        self.assertEqual(frozen["phase"], "frozen")
        self.assertTrue(frozen["activity_drained"])
        self.assertFalse(frozen["queue_checked"])
        self.assertFalse(frozen["switch_authorized"])

    def test_active_real_child_finishes_naturally_then_exact_freeze_query(self):
        code = """import json,sys\nfrom services.admission_fence import Fence\nf=Fence(sys.argv[1],json.loads(sys.argv[2]))\nl=f.reserve_lease('synthetic_child',kind='drain')\nprint('READY',flush=True)\nsys.stdin.readline()\nl.finish(True)\n"""
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.fence.root), fence._encoded(self.binding).decode()],
                                 cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
            self.driver.operate("stop_new", "f" * 64, "stop while child drains")
            with self.assertRaises(Exception):
                self.driver.operate("freeze", "9" * 64, "freeze waits naturally")
            self.assertIsNone(child.poll())
            child.communicate("finish\n", timeout=3)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(self.driver.query("9" * 64)["phase"], "frozen")
            self.assertEqual(len(self.posts()), 2)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=3)

    def test_lost_ack_restart_queries_original_after_window_expiry(self):
        self.transport.lose_ack = True
        with self.assertRaises(TimeoutError):
            self.driver.operate("stop_new", "f" * 64, "synthetic lost ACK")
        restarted = driver.ReceiverDriver(self.plan, self.transport, self.root / "evidence")
        with patch.object(driver.time, "time", return_value=self.plan["expires_at"] + 1):
            self.assertEqual(restarted.query("f" * 64)["phase"], "draining")
            with self.assertRaises(Exception):
                restarted.operate("stop_new", "8" * 64, "must not resend")
        self.assertEqual(len(self.posts()), 1)

    def test_uncertain_original_never_freezes_or_cleans_up(self):
        lease = self.fence.reserve_lease("uncertain_original")
        lease.finish(False)
        self.driver.operate("stop_new", "f" * 64, "stop uncertain intake")
        before = {p: p.read_bytes() for p in (self.fence.root / "leases").iterdir()}
        with self.assertRaises(Exception):
            self.driver.operate("freeze", "9" * 64, "must remain unconfirmed")
        with self.assertRaises(Exception):
            self.driver.query("9" * 64)
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))

    def test_capacity_counts_retained_alias_reservations_and_stops_only_receiver(self):
        self.assertEqual(self.driver.capacity_tick("f" * 64)["status"], "observed")
        alias, _ = self.store.accept(b"different original same task", {"entries": []}, strategy="synthetic", revision="synthetic")
        self.assertEqual(alias["primary_delivery_id"], self.record["delivery_id"])
        self.store.update_status(self.record["delivery_id"], status="terminal", completion={"reason": "synthetic"})
        result = self.driver.capacity_tick("f" * 64)
        self.assertEqual(result["receipt"]["reason"], "receipt_record_limit")
        self.assertFalse(result["sender_stopped"])
        self.assertEqual(len(list((Path(self.plan["spool"]) / "receipts").glob("*.json"))), 2)
        with self.assertRaises(Exception):
            self.driver.capacity_tick("8" * 64)

    def test_partial_receipt_causes_actual_stop_without_deleting_original(self):
        pending = Path(self.plan["spool"]) / "receipts" / ("7" * 64 + ".json.pending")
        pending.write_bytes(b"synthetic incomplete original")
        pending.chmod(0o600)
        result = self.driver.capacity_tick("f" * 64)
        self.assertEqual(result["receipt"]["reason"], "receipt_observation_failed")
        self.assertEqual(pending.read_bytes(), b"synthetic incomplete original")

    def test_fence_admission_byte_budget_enforced_even_without_observer(self):
        lease = self.fence.reserve_lease("limited", charge_bytes=12)
        lease.finish(True)
        with self.assertRaises(fence.AdmissionClosed):
            self.fence.reserve_lease("over_budget", charge_bytes=1)
        durable = self.fence.reserve_lease("existing_durable", kind="drain")
        durable.finish(True)
        self.assertEqual(self.fence.snapshot()["intake_count"], 1)

    def test_unpatched_component_cannot_claim_takeover_from_health_or_empty_queue(self):
        self.transport.rows[0]["Config"]["Env"] = ["AI_FILTER_ROLE=web"]
        with self.assertRaises(Exception):
            self.driver.operate("stop_new", "f" * 64, "old receiver unsupported")
        self.assertEqual(self.posts(), [])
        self.assertFalse(self.control.registration()["legacy_originals_proven"])

    def test_image_restart_mount_role_and_fence_drift_refuse_before_mutation(self):
        original = copy.deepcopy(self.transport.rows)
        for mutation in (lambda rows: rows[0].update(Image="sha256:" + "7" * 64),
                         lambda rows: rows[0].update(RestartCount=1),
                         lambda rows: rows[0]["Mounts"][0].update(Source="/wrong"),
                         lambda rows: rows[0]["Config"]["Env"].append("AI_FILTER_ROLE=worker")):
            self.transport.rows = copy.deepcopy(original)
            mutation(self.transport.rows)
            with self.assertRaises(Exception):
                self.driver.operate("stop_new", "f" * 64, "identity drift")
        self.assertEqual(self.posts(), [])

    def test_source_scope_capacity_and_no_legacy_bool_escape(self):
        for mutate in (lambda p: p.update(scope="production_takeover"),
                       lambda p: p["sources"].update(control="0" * 64),
                       lambda p: p["registration"].update(legacy_originals_proven=True),
                       lambda p: p["capacity"].update(stop_records=4096),
                       lambda p: p["capacity"].update(stop_records=True),
                       lambda p: p["capacity"].update(stop_bytes=256 * 1024**2)):
            value = copy.deepcopy(self.plan)
            mutate(value)
            with self.assertRaises(Exception):
                driver.validate_plan(value)

    def test_spool_binding_and_shadowing_mount_cannot_sample_wrong_storage(self):
        for mutate in (lambda p: p.update(spool=str(self.root / "other-spool")),
                       lambda p: p.update(container_spool_root="/fixture/other-spool"),
                       lambda p: p["components"]["web"]["mounts"].append({"Type": "bind", "Source": "/other",
                           "Destination": "/fixture/spool", "RW": True})):
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.assertRaises(Exception):
                driver.validate_plan(plan)

    def test_wrong_operation_query_or_repeated_action_cannot_mutate(self):
        self.driver.operate("stop_new", "f" * 64, "one stop")
        with self.assertRaises(Exception):
            self.driver.query("9" * 64)
        with self.assertRaises(Exception):
            self.driver.operate("stop_new", "8" * 64, "second stop")
        self.assertEqual(len(self.posts()), 1)

    def test_local_intent_storage_full_has_no_network_write(self):
        with patch.object(driver, "durable_write", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.driver.operate("stop_new", "f" * 64, "no intent no request")
        self.assertEqual(self.posts(), [])

    def test_query_capacity_persists_across_driver_restart(self):
        self.driver.operate("stop_new", "f" * 64, "bounded queries")
        with patch.object(driver, "MAX_QUERIES", 1):
            self.driver.query("f" * 64)
            restarted = driver.ReceiverDriver(self.plan, self.transport, self.root / "evidence")
            with self.assertRaises(Exception):
                restarted.query("f" * 64)

    def test_observation_capacity_persists_across_driver_restart(self):
        with patch.object(driver, "MAX_SAMPLES", 1):
            self.assertEqual(self.driver.capacity_tick("f" * 64)["status"], "observed")
            restarted = driver.ReceiverDriver(self.plan, self.transport, self.root / "evidence")
            result = restarted.capacity_tick("f" * 64)
            self.assertEqual(result["receipt"]["reason"], "receipt_observation_count_limit")
        self.assertEqual(len(self.posts()), 1)

    def test_query_rebinds_after_result_and_preserves_uncertain_receipt(self):
        self.transport.lose_ack = True
        with self.assertRaises(TimeoutError):
            self.driver.operate("stop_new", "f" * 64, "uncertain ACK")
        request = self.transport.request
        def changed(method, path, value=None):
            response = request(method, path, value)
            if path == "/operations/" + "f" * 64:
                self.transport.rows[0]["RestartCount"] = 1
            return response
        with patch.object(self.transport, "request", side_effect=changed):
            with self.assertRaises(Exception):
                self.driver.query("f" * 64)
        self.assertFalse((self.driver.root / ("f" * 64 + ".receipt.json")).exists())
        self.assertEqual(len(self.posts()), 1)

    def test_complete_local_pending_receipt_can_recover_only_exact_operation(self):
        receipt = self.driver.operate("stop_new", "f" * 64, "pending local receipt")
        path = self.driver.root / ("f" * 64 + ".receipt.json")
        path.rename(path.with_name(path.name + ".pending"))
        self.assertEqual(self.driver.query("f" * 64), receipt)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.posts()), 1)

    def test_credentials_not_in_durable_evidence_and_rotation_blocks(self):
        self.driver.operate("stop_new", "f" * 64, "no secret evidence")
        for path in self.driver.root.iterdir():
            self.assertNotIn(("e" * 64).encode(), path.read_bytes())
        driver.durable_write(self.token_file, {"token": "6" * 64})
        with self.assertRaises(Exception):
            self.driver.query("f" * 64)

    def test_unsupported_open_and_untrusted_token_refused(self):
        with self.assertRaises(Exception):
            self.transport.request("POST", "/open", {})
        wrong = self.root / "wrong-token.json"
        driver.durable_write(wrong, {"token": "6" * 64})
        transport = driver.NativeTransport(f"http://127.0.0.1:{self.server.server_port}", wrong)
        with self.assertRaises(Exception):
            transport.request("GET", "/registration")


class TransportBounds(unittest.TestCase):
    def test_native_fixture_refuses_non_native_before_docker_and_ci_covers_modules(self):
        import verify_production_receiver_native as native_receiver
        import verify_handoff_operations_native as operations
        with patch.object(native_receiver.platform, "system", return_value="Darwin"):
            with patch.object(native_receiver.native, "checked", side_effect=AssertionError("Docker reached")):
                with self.assertRaises(ValueError):
                    native_receiver.run(Path("unused"), Path("unused"))
        workflow = (ROOT / ".github/workflows/handoff-operations.yml").read_text()
        pull = workflow.split("  pull_request:", 1)[1].split("  push:", 1)[0]
        push = workflow.split("  push:", 1)[1].split("  workflow_dispatch:", 1)[0]
        for path in ("services/receiver_handoff_control.py", "scripts/production_receiver_driver.py",
                     "scripts/verify_production_receiver_native.py", "tests/test_production_receiver_driver.py"):
            self.assertIn(path, operations.FILES)
            self.assertIn("'" + path + "'", pull)
            self.assertIn("'" + path + "'", push)
            self.assertNotIn("'" + path + ".disabled'", pull)
            self.assertNotIn("'" + path + ".disabled'", push)
        self.assertIn("test_production_receiver_driver.py", operations.CONTAINER_TEST)

    def test_own_inspect_process_output_and_time_are_bounded(self):
        with self.assertRaises(Exception):
            driver._capture([sys.executable, "-c", "import os;os.write(1,b'x'*100000)"], max_bytes=1024)
        started = time.monotonic()
        with self.assertRaises(Exception):
            driver._capture([sys.executable, "-c", "import time;time.sleep(10)"], seconds=0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(driver._capture([sys.executable, "-c", "print('ok')"]), b"ok\n")

    def test_slow_drip_response_cannot_extend_total_http_deadline(self):
        root = Path(tempfile.mkdtemp(prefix="slow-control-"))
        driver.durable_write(root / "token.json", {"token": "1" * 64})
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        def drip():
            connection, _ = listener.accept()
            try:
                connection.recv(8192)
                for byte in b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                    connection.send(bytes([byte]))
                    time.sleep(0.1)
            except OSError:
                pass
            finally:
                connection.close()
                listener.close()
        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        transport = driver.NativeTransport(f"http://127.0.0.1:{listener.getsockname()[1]}", root / "token.json")
        started = time.monotonic()
        with self.assertRaises(Exception):
            transport.request("GET", "/registration")
        self.assertLess(time.monotonic() - started, 3)
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
