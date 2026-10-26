"""Deterministic isolated failure boundaries, not a historical CI root-cause claim."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import durable_handoff_cutover as cutover  # noqa: E402
from durable_handoff_cutover_fixture import Fixture, Handler  # noqa: E402
import test_durable_handoff_cutover as protocol_tests  # noqa: E402


class CrashRecoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.protocol = protocol_tests.CutoverProtocolTests("test_adapter_abrupt_exit_then_restart_recovers_same_operation")
        self.protocol._testMethodName = self._testMethodName
        self.protocol.setUp()
        self.root = self.protocol.root

    def tearDown(self):
        self.protocol.tearDown()

    def crash_before_ack(self):
        process = self.protocol.start({"exit_after": "switch_receiver"})
        result = self.protocol.run_controller()
        self.assertEqual(result["status"], "stopped", result)
        self.assertEqual(process.wait(timeout=2), 73)
        self.assertEqual(self.protocol.manifest["budget"]["request_seconds"], 0.2)
        operation = cutover.digest({"run_id": self.protocol.run_id, "action": "switch_receiver"})
        receipt = self.root / "adapter/operations" / (operation + ".json")
        return operation, receipt, receipt.read_bytes(), result

    @contextmanager
    def restarted_server(self):
        fixture = Fixture(self.root / "adapter", self.protocol.run_id, self.protocol.token)
        fixture.recover()
        server = ThreadingHTTPServer(("127.0.0.1", self.protocol.port), Handler)
        server.fixture = fixture
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield fixture
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def assert_unconfirmed(self, operation, receipt, original, result, fixture):
        self.assertEqual(result["status"], "stopped", result)
        self.assertEqual(result["phase"], "drained", result)
        request = result["last_request"]
        self.assertEqual(request["path"], "/control/operation/" + operation)
        self.assertEqual(request["method"], "GET")
        self.assertIsNone(request["action"])
        self.assertEqual(request["request_seconds"], 0.2)
        self.assertEqual(request["timeout_seconds"], 0.2)
        self.assertEqual(request["outcome"], "raised")
        saved = cutover.read_private(sorted((self.root / "journal/records").glob("*.json"))[-1])
        self.assertEqual(saved["stopped"]["last_request"], request)
        self.assertEqual(saved["intent"]["operation_id"], operation)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(fixture.snapshot()["switch_counts"], {"receiver": 1, "sender": 0})
        self.assertEqual(len(list((self.root / "adapter/operations").glob("*.json"))), 2)
        self.assertNotIn(self.protocol.token, cutover.encoded(result).decode())

    def test_restart_query_deadline_preserves_exact_intent_without_reswitch(self):
        operation, receipt, original, _ = self.crash_before_ack()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        with self.restarted_server() as fixture:
            actual_query = fixture.confirmed_operation
            queries = []

            def held_query(value):
                queries.append(value)
                entered.set()
                try:
                    cutover.require(release.wait(timeout=2), "test query release missing")
                    return actual_query(value)
                finally:
                    finished.set()

            with patch.object(fixture, "confirmed_operation", held_query):
                try:
                    result = self.protocol.run_controller(resume=True)
                    self.assertTrue(entered.is_set(), result)
                    self.assertFalse(finished.is_set(), result)
                    self.assertEqual(queries, [operation])
                    self.assert_unconfirmed(operation, receipt, original, result, fixture)
                    self.assertEqual(result["last_request"]["stage"], "response_headers")
                    self.assertGreaterEqual(result["last_request"]["elapsed_seconds"], 0.15)
                    self.assertLess(result["last_request"]["elapsed_seconds"], 1.0)
                finally:
                    release.set()
                    self.assertTrue(finished.wait(timeout=2))
            # Late server completion must not change the already-stopped controller.
            self.assertEqual(receipt.read_bytes(), original)
            self.assertEqual(fixture.snapshot()["switch_counts"], {"receiver": 1, "sender": 0})

    def test_restart_query_directory_barrier_failure_is_not_confirmed(self):
        operation, receipt, original, _ = self.crash_before_ack()
        with self.restarted_server() as fixture:
            real_sync = cutover.sync_directory
            calls = []

            def failed_operation_sync(path):
                if path == fixture.root / "operations":
                    calls.append(str(path))
                    raise OSError("synthetic query operation directory EIO")
                return real_sync(path)

            with patch.object(cutover, "sync_directory", failed_operation_sync):
                result = self.protocol.run_controller(resume=True)
            self.assertEqual(calls, [str(fixture.root / "operations")])
            self.assert_unconfirmed(operation, receipt, original, result, fixture)
            self.assertEqual(result["last_request"]["http_status"], 503)
            self.assertEqual(result["last_request"]["stage"], "validate")
            self.assertEqual(result["error_type"], "CutoverError")

    def test_crash_diagnostics_identify_mutation_without_saving_payload_or_token(self):
        operation, _, _, result = self.crash_before_ack()
        request = result["last_request"]
        self.assertEqual(request["path"], "/control/apply")
        self.assertEqual(request["action"], "switch_receiver")
        self.assertEqual(request["phase"], "drained")
        self.assertEqual(request["stage"], "response_headers")
        self.assertEqual(request["outcome"], "raised")
        self.assertEqual(request["request_seconds"], 0.2)
        saved = cutover.read_private(sorted((self.root / "journal/records").glob("*.json"))[-1])
        self.assertEqual(saved["intent"]["operation_id"], operation)
        self.assertEqual(saved["stopped"]["last_request"], request)
        self.assertNotIn("payload", request)
        self.assertNotIn("headers", request)
        self.assertNotIn(self.protocol.token, cutover.encoded(result).decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
