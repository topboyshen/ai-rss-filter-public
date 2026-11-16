"""Deterministic isolated failure boundaries, not a historical CI root-cause claim."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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


class _DeadlineMocks:
    """The real Controller with fake clocks, HTTP, sockets, Timer and journal."""

    def __init__(self, fire=False, error=None, shutdown_error=False):
        self.fire, self.error, self.shutdown_error = fire, error, shutdown_error
        self.connections, self.wires, self.timers, self.flags_at_shutdown = [], [], [], []
        self.now, self.monotonic = 999.0, 10.0
        self.http_exception = cutover.http.client.HTTPException
        self.controller = cutover.Controller.__new__(cutover.Controller)
        self.controller.endpoint = SimpleNamespace(port=12345)
        self.controller.started = 10.0
        self.controller.manifest = {"run_id": "a" * 32, "token": "b" * 64,
            "adapter_identity": "synthetic-cutover-v1", "adapter_sha256": "c" * 64,
            "targets": {"receiver": "synthetic-new-receiver", "sender": "synthetic-new-sender"},
            "budget": {"total_seconds": 10, "drain_seconds": 0.3, "request_seconds": 0.2, "max_polls": 30}}
        self.controller.state = {"expires_at": 1000.0, "phase": "verified", "intent": None, "stopped": None}
        self.controller.last_request = None

    def timer(self, interval, callback):
        timer = SimpleNamespace(interval=interval, callback=callback, start=Mock(), cancel=Mock())
        self.timers.append(timer)
        return timer

    def connection(self, host, port, timeout):
        wire = SimpleNamespace(closed=False)

        def shutdown(_how):
            self.flags_at_shutdown.append(self.controller.last_request.get("deadline_abort_entered"))
            if wire.closed or self.shutdown_error:
                raise OSError("synthetic shutdown error")

        wire.shutdown = Mock(side_effect=shutdown)
        wire.close = Mock(side_effect=lambda: setattr(wire, "closed", True))
        self.wires.append(wire)
        connection = SimpleNamespace(host=host, port=port, timeout=timeout,
            sock=SimpleNamespace(dup=Mock(return_value=wire)), connect=Mock(), request=Mock(), close=Mock())

        def response():
            if self.fire:
                self.timers[-1].callback()
            if self.error is not None:
                raise self.error
            raw = cutover.encoded({"run_id": "a" * 32, "mode": "isolation",
                "adapter_identity": "synthetic-cutover-v1", "adapter_sha256": "c" * 64})
            return SimpleNamespace(status=200, read1=Mock(side_effect=[raw, b""]))

        connection.getresponse = Mock(side_effect=response)
        self.connections.append(connection)
        return connection

    @contextmanager
    def patched(self):
        with patch.object(cutover, "time", SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.monotonic)), \
                patch.object(cutover, "threading", SimpleNamespace(Timer=self.timer)), \
                patch.object(cutover, "http", SimpleNamespace(client=SimpleNamespace(
                    HTTPConnection=self.connection, HTTPException=self.http_exception))):
            yield self

    def request(self):
        return self.controller.request("/control/apply", {"action": "resume"})

    def stopped_run(self):
        controller = self.controller

        def record(**changes):
            controller.state = {**controller.state, **changes}

        controller.record = record
        controller.inspect = lambda: {"generation": 3, "entrances": dict.fromkeys(cutover.ENTRANCES, "buffering"),
                                      **dict.fromkeys(cutover.ZERO_COUNTERS, 0)}
        controller.journal = SimpleNamespace(close=Mock())
        return controller.run()


class RequestDeadlineObservationTests(unittest.TestCase):
    """No real HTTP, timer threads, socket operations or durable files."""

    def test_request_bool_starts_false_and_original_request_budget_is_preserved(self):
        environment = _DeadlineMocks()
        with environment.patched():
            value = environment.request()
        self.assertEqual(value["mode"], "isolation")
        self.assertIs(environment.controller.last_request["deadline_abort_entered"], False)
        self.assertEqual(environment.controller.last_request["outcome"], "returned")
        self.assertAlmostEqual(environment.timers[0].interval, 0.2)
        self.assertEqual(environment.connections[0].host, "127.0.0.1")
        self.assertEqual(environment.connections[0].timeout, 0.2)
        environment.connections[0].connect.assert_called_once_with()
        environment.connections[0].close.assert_called_once_with()
        environment.wires[0].close.assert_called_once_with()

    def test_abort_marks_its_observation_before_original_shutdown_and_preserves_error(self):
        error = protocol_tests.http.client.RemoteDisconnected("synthetic no ACK")
        environment = _DeadlineMocks(fire=True, error=error)
        with environment.patched(), self.assertRaises(protocol_tests.http.client.RemoteDisconnected) as caught:
            environment.request()
        self.assertIs(caught.exception, error)
        self.assertIs(environment.controller.last_request["deadline_abort_entered"], True)
        self.assertEqual(environment.flags_at_shutdown, [True])
        environment.wires[0].shutdown.assert_called_once_with(cutover.socket.SHUT_RDWR)
        environment.timers[0].cancel.assert_called_once_with()

    def test_cancelled_timer_does_not_invent_entry_or_add_wait(self):
        environment = _DeadlineMocks()
        with environment.patched():
            environment.request()
        environment.timers[0].start.assert_called_once_with()
        environment.timers[0].cancel.assert_called_once_with()
        self.assertTrue(environment.timers[0].daemon)
        self.assertFalse(hasattr(environment.timers[0], "join"))
        self.assertIs(environment.controller.last_request["deadline_abort_entered"], False)
        environment.wires[0].shutdown.assert_not_called()

    def test_late_old_callback_cannot_mark_a_later_request(self):
        environment = _DeadlineMocks()
        with environment.patched():
            environment.request()
            original, timer = environment.controller.last_request, environment.timers[0]
            environment.request()
            later = environment.controller.last_request
            timer.callback()  # The old closed wire's original OSError is still handled.
        self.assertIsNot(original, later)
        self.assertIs(original["deadline_abort_entered"], True)
        self.assertIs(later["deadline_abort_entered"], False)
        environment.wires[0].shutdown.assert_called_once()
        environment.wires[1].shutdown.assert_not_called()
        self.assertEqual(len(environment.connections), 2)

    def test_shutdown_oserror_does_not_reset_flag_or_replace_http_primary(self):
        error = protocol_tests.http.client.RemoteDisconnected("synthetic original")
        environment = _DeadlineMocks(fire=True, error=error, shutdown_error=True)
        with environment.patched(), self.assertRaises(protocol_tests.http.client.RemoteDisconnected) as caught:
            environment.request()
        self.assertIs(caught.exception, error)
        self.assertIs(environment.controller.last_request["deadline_abort_entered"], True)
        environment.wires[0].shutdown.assert_called_once()
        environment.connections[0].close.assert_called_once_with()

    def test_observed_deadline_failure_stays_stopped_keeps_intent_without_retry(self):
        error = protocol_tests.http.client.RemoteDisconnected("synthetic unknown")
        environment = _DeadlineMocks(fire=True, error=error)
        with environment.patched():
            result = environment.stopped_run()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["phase"], "verified")
        self.assertIs(result["last_request"]["deadline_abort_entered"], True)
        self.assertEqual(environment.controller.state["intent"]["action"], "resume")
        self.assertFalse(result["automatic_rollback"])
        self.assertEqual(len(environment.connections), 1)
        environment.connections[0].request.assert_called_once()
        self.assertEqual(environment.connections[0].request.call_args.args[:2], ("POST", "/control/apply"))
        environment.controller.journal.close.assert_called_once_with()
        self.assertEqual(environment.controller.manifest["budget"],
                         {"total_seconds": 10, "drain_seconds": 0.3, "request_seconds": 0.2, "max_polls": 30})

    def test_unobserved_disconnect_is_still_unknown_and_never_promoted_to_success(self):
        error = protocol_tests.http.client.RemoteDisconnected("synthetic unclassified close")
        environment = _DeadlineMocks(error=error)
        with environment.patched():
            result = environment.stopped_run()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["error_type"], "RemoteDisconnected")
        self.assertIs(result["last_request"]["deadline_abort_entered"], False)
        self.assertEqual(environment.controller.state["intent"]["action"], "resume")
        self.assertEqual(len(environment.connections), 1)
        environment.wires[0].shutdown.assert_not_called()

    def test_expired_total_budget_blocks_before_http_with_flag_false(self):
        environment = _DeadlineMocks()
        environment.now = 1000.0
        with environment.patched(), self.assertRaises(cutover.CutoverError):
            environment.request()
        self.assertEqual(environment.connections, [])
        self.assertEqual(environment.timers, [])
        self.assertIs(environment.controller.last_request["deadline_abort_entered"], False)
        self.assertEqual(environment.controller.last_request["stage"], "budget")


if __name__ == "__main__":
    unittest.main(verbosity=2)
