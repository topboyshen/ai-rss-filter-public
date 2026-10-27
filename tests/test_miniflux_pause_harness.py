"""Offline guards for the native actual-image pause harness; no Docker here."""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import miniflux_pause_fixture as fixture  # noqa: E402
import verify_miniflux_pause_native as native  # noqa: E402


class FixtureTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="miniflux-pause-fixture-", dir=parent))
        self.fixture = fixture.Fixture(self.root, "a" * 64)

    def test_exact_request_bytes_are_preserved_and_hashed(self):
        body = b'{"event_type":"new_entries","entries":[{"title":"synthetic"}]}'
        status, ack = self.fixture.webhook(body, {"X-Miniflux-Signature": "b" * 64})
        self.assertEqual(status, 202)
        record = json.loads((self.root / "event-00001.json").read_bytes())
        self.assertEqual(base64.b64decode(record["body_b64"]), body)
        self.assertEqual(ack["delivery_id"], hashlib.sha256(body).hexdigest())
        self.assertEqual(record["body_sha256"], ack["delivery_id"])
        self.assertEqual((self.root / "event-00001.json").stat().st_mode & 0o777, 0o600)

    def test_actual_hold_release_handshake_not_sleep(self):
        self.fixture.control({"items": ["seed"], "hold": True, "release": False})
        results = []
        thread = threading.Thread(target=lambda: results.append(self.fixture.webhook(b"{}", {}, legacy=True)))
        thread.start()
        with self.fixture.condition:
            self.assertTrue(self.fixture.condition.wait_for(lambda: len(self.fixture.events) == 1, timeout=2))
        self.assertTrue(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(self.fixture.events[0]["kind"], "legacy_received")
        self.fixture.control({"items": ["seed"], "hold": False, "release": True})
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0][0], 202)
        self.assertEqual(self.fixture.events[1]["kind"], "legacy_response")

    def test_no_ack_on_original_persistence_failure(self):
        with patch.object(fixture.os, "fsync", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.fixture.webhook(b"exact original", {})
        self.assertEqual(self.fixture.events, [])
        self.assertEqual(len(list(self.root.glob("event-*.json"))), 1)

    def test_control_cannot_inject_external_target_or_bad_items(self):
        for value in ({"url": "https://production.invalid"}, {"items": ["../outside"], "hold": False, "release": False},
                      {"items": ["seed"], "hold": 1, "release": False}, {"items": [], "hold": False, "release": False}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fixture.control(value)

    def test_synthetic_rss_has_stable_full_item_identity(self):
        self.fixture.control({"items": ["seed", "scheduler"], "hold": False, "release": False})
        import xml.etree.ElementTree as ET
        rows = ET.fromstring(self.fixture.rss()).findall("./channel/item")
        self.assertEqual([r.findtext("guid") for r in rows], ["seed", "scheduler"])
        self.assertEqual(rows[1].findtext("link"), "https://example.invalid/scheduler")


FIXTURE_PROCESS = r'''
import json, linecache, os, pathlib, signal, sys, threading
sys.path.insert(0, sys.argv[1])
import miniflux_pause_fixture as fixture
root = pathlib.Path(sys.argv[2])
server = fixture.ThreadingHTTPServer(('127.0.0.1', 0), fixture.Handler)
server.fixture = fixture.Fixture(root / 'events', 'a' * 64)
if sys.argv[3] == 'nested_term':
    def nested(frame, event, arg):
        line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip() if event == 'line' else ''
        locked_event = (frame.f_code is threading.Event.set.__code__ and line == 'self._flag = True'
                        and frame.f_back.f_code.co_name == 'request_stop')
        plain_assignment = frame.f_code.co_name == 'request_stop' and line == 'stopping = True'
        if locked_event or plain_assignment:
            sys.settrace(None)
            (root / 'nested-term.json').write_text(json.dumps({'signal': 'SIGTERM', 'handler_active': True}))
            # Test-only bound: reverting to Event.set must fail, never leave a
            # subprocess deadlocked while its signal handler holds that lock.
            signal.alarm(2)
            os.kill(os.getpid(), signal.SIGTERM)
        return nested
    sys.settrace(nested)
if sys.argv[3].startswith('failure'):
    def fail(**kwargs):
        if sys.argv[3] == 'failure_term':
            os.kill(os.getpid(), signal.SIGTERM)
        raise RuntimeError('synthetic serve failure retained')
    server.serve_forever = fail
# Test-only input releases a request already accepted before TERM. It does not
# add a release endpoint or change the production fixture's hold deadline.
def release():
    if sys.stdin.readline() == 'release\n':
        server.fixture.control({'items': ['seed'], 'hold': False, 'release': True})
threading.Thread(target=release, daemon=True).start()
(root / 'port.json').write_text(json.dumps(server.server_port))
fixture.serve_until_term(server)
'''


class FixtureProcessTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="miniflux-term-fixture-", dir=parent))
        self.process = None

    def tearDown(self):
        if self.process is not None:
            try:
                if self.process.poll() is None:
                    self.release()
                    self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=8)
            finally:
                self.process.stdin.close()

    def start(self, mode="normal"):
        with (self.root / "stdout.log").open("wb") as stdout, (self.root / "stderr.log").open("wb") as stderr:
            self.process = subprocess.Popen([sys.executable, "-I", "-B", "-c", FIXTURE_PROCESS,
                                             str(SCRIPTS), str(self.root), mode],
                                            stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, bufsize=0)
        if mode.startswith("failure"):
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.assertIsNone(self.process.poll(), (self.root / "stderr.log").read_text())
            try:
                self.port = json.loads((self.root / "port.json").read_bytes())
                status, _ = self.request("GET", "/health")
                if status == 200:
                    return
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            time.sleep(0.01)
        self.fail("synthetic HTTP fixture did not become ready")

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers={"Authorization": "Bearer " + "a" * 64})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def release(self):
        try:
            self.process.stdin.write(b"release\n")
            self.process.stdin.flush()
        except BrokenPipeError:
            pass

    def test_real_request_then_term_exits_zero_and_preserves_original_event(self):
        self.start()
        status, body = self.request("GET", "/feed")
        self.assertEqual(status, 200)
        self.assertIn(b"<guid>seed</guid>", body)
        event = self.root / "events/event-00001.json"
        original = event.read_bytes()
        self.assertEqual(json.loads(original)["kind"], "feed")
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=3), 0)
        self.assertEqual(event.read_bytes(), original)
        self.assertEqual((self.root / "stderr.log").read_bytes(), b"")

    def test_repeated_term_waits_for_inflight_original_and_response(self):
        self.start()
        status, _ = self.request("POST", "/control", fixture.canonical({"items": ["seed"], "hold": True, "release": False}))
        self.assertEqual(status, 200)
        body, response, failures = b'{"synthetic":"original whitespace"} ', [], []

        def deliver():
            try:
                response.append(self.request("POST", "/hook", body))
            except Exception as error:
                failures.append(error)

        request = threading.Thread(target=deliver)
        request.start()
        try:
            deadline, events = time.monotonic() + 2, []
            while time.monotonic() < deadline:
                status, raw = self.request("GET", "/events")
                self.assertEqual(status, 200)
                events = json.loads(raw)["events"]
                if events:
                    break
                time.sleep(0.01)
            self.assertEqual(len(events), 1)
            self.assertIs(events[0]["held"], True)
            event = self.root / "events/event-00001.json"
            original = event.read_bytes()
            for _ in range(2):
                self.process.send_signal(signal.SIGTERM)
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.process.wait(timeout=0.15)
                self.assertEqual(response, [])
                self.assertEqual(event.read_bytes(), original)
            self.release()
            request.join(timeout=3)
            self.assertFalse(request.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(response[0][0], 202)
            self.assertEqual(json.loads(response[0][1])["body_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(self.process.wait(timeout=3), 0)
            self.assertEqual(event.read_bytes(), original)
            self.assertEqual(base64.b64decode(json.loads(original)["body_b64"]), body)
            self.assertEqual(json.loads((self.root / "events/event-00002.json").read_bytes())["kind"], "webhook_response")
            self.assertEqual((self.root / "stderr.log").read_bytes(), b"")
        finally:
            self.release()
            request.join(timeout=3)

    def test_term_reentering_active_signal_handler_exits_zero(self):
        self.start("nested_term")
        status, _ = self.request("GET", "/feed")
        self.assertEqual(status, 200)
        event = self.root / "events/event-00001.json"
        original = event.read_bytes()
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=3), 0)
        self.assertEqual(json.loads((self.root / "nested-term.json").read_bytes()),
                         {"signal": "SIGTERM", "handler_active": True})
        self.assertEqual(event.read_bytes(), original)
        self.assertEqual((self.root / "stderr.log").read_bytes(), b"")

    def test_server_exception_is_retained_and_never_claims_normal_term_exit(self):
        root = self.root
        for attempt in range(4):
            for mode in ("failure", "failure_term"):
                with self.subTest(attempt=attempt, mode=mode):
                    self.root = root / f"{mode}-{attempt}"
                    self.root.mkdir(mode=0o700)
                    self.start(mode)
                    self.assertEqual(self.process.wait(timeout=3), 1)
                    self.process.stdin.close()
                    self.assertIn(b"RuntimeError: synthetic serve failure retained", (self.root / "stderr.log").read_bytes())
                    self.assertEqual(list((self.root / "events").iterdir()), [])


class NativeGuardTests(unittest.TestCase):
    def test_added_drain_and_existing_joint_scenarios_use_distinct_resume_operations(self):
        import miniflux_host_joint_native as joint

        class ReachedResume(Exception):
            pass

        harness = object.__new__(native.Harness)
        harness.output = Path("unused-synthetic-evidence")
        harness.owner = "a" * 32
        # Exercise both real scenario entry points up to their first control
        # request. No Docker, cron child, or synthetic file creation is needed.
        operations = []
        with patch.object(harness, "resume", side_effect=ReachedResume) as resume, \
                patch.object(joint, "old_cron_proof", return_value={}):
            for scenario in (harness.stop_new_drain_case, lambda prior: joint.run(harness, prior)):
                with self.assertRaises(ReachedResume):
                    scenario("4" * 64)
                operations.append(resume.call_args.args[1])
        self.assertEqual(len(set(operations)), 2)
        for operation in operations:
            self.assertRegex(operation, r"^[0-9a-f]{64}$")

    def test_stop_new_confirmation_requires_exact_producer_counts_and_distinct_state(self):
        harness = object.__new__(native.Harness)
        harness.manifest = "a" * 64
        receipt = {"confirmed": True, "result": {"schema": 1, "manifest": harness.manifest,
            "operation_id": "b" * 64, "action": "stop_new", "state": "draining", "durable": True,
            "confirmed": True, "previous_generation": 0, "generation": 1,
            "active_leases": 2, "uncertain_leases": 0,
            "active_producer_leases": 0, "uncertain_producer_leases": 0}}
        self.assertEqual(harness.checked_draining(receipt, "b" * 64), receipt["result"])
        for field in ("active_producer_leases", "uncertain_producer_leases"):
            for value in (None, True, -1, 1):
                wrong = copy.deepcopy(receipt)
                wrong["result"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    harness.checked_draining(wrong, "b" * 64)
        for field, value in (("action", "pause"), ("state", "closed"), ("generation", 3)):
            wrong = copy.deepcopy(receipt)
            wrong["result"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                harness.checked_draining(wrong, "b" * 64)
        pending = copy.deepcopy(receipt)
        pending["confirmed"] = pending["result"]["confirmed"] = False
        pending["result"]["active_producer_leases"] = 1
        self.assertEqual(harness.checked_draining(pending, "b" * 64, confirmed=False), pending["result"])

    def test_consecutive_commands_share_remaining_phase_budget(self):
        with tempfile.TemporaryDirectory(prefix="phase-command-") as directory:
            harness = object.__new__(native.Harness)
            harness.output = Path(directory)
            harness.sequence = 0
            harness.started = time.monotonic()
            harness.phase_deadline = harness.started + 1
            harness.env = {}
            harness.password, harness.token, harness.auth = "unused-pass", "unused-token", "unused-auth"
            budgets = []

            def command(argv, **kwargs):
                budgets.append(kwargs["timeout"])
                time.sleep(0.03)
                return subprocess.CompletedProcess(argv, 0, "synthetic", "")

            with patch.object(native.subprocess, "run", side_effect=command):
                harness.command(["inspect", "owned-synthetic"], timeout=30)
                harness.command(["inspect", "owned-synthetic"], timeout=30)
            self.assertTrue(0 < budgets[1] < budgets[0] <= 1)
            self.assertEqual(len(list(Path(directory).glob("command-*.json"))), 2)

    def test_oom_evidence_missing_or_malformed_never_becomes_zero(self):
        for peak, events in (("", "oom 0\noom_kill 0\n"), ("12", ""),
                             ("12", "oom 0\n"), ("12", "oom 0\noom_kill -1\n"),
                             ("12", "oom 1\noom 0\noom_kill 0\n")):
            with self.subTest(peak=peak, events=events), self.assertRaises(ValueError):
                native.memory_evidence(peak, events)
        value = native.memory_evidence("123\n", "low 0\noom 0\noom_kill 0\n")
        self.assertEqual(value["peak_bytes"], 123)
        self.assertEqual(value["events"]["oom_kill"], 0)

    def test_late_success_is_not_accepted_and_deadline_is_restored(self):
        harness = object.__new__(native.Harness)
        harness.phase_deadline = None

        def late():
            time.sleep(0.03)
            return True

        with self.assertRaises(TimeoutError):
            harness.wait(late, seconds=0.01)
        self.assertIsNone(harness.phase_deadline)

    def profile(self):
        return {"Image": "sha256:" + "a" * 64, "Config": {"Labels": {native.LABEL: "owner"}},
                "State": {"OOMKilled": False}, "HostConfig": {"NetworkMode": "internal-test", "PortBindings": {},
                "Memory": native.MEMORY, "MemorySwap": native.MEMORY, "PidsLimit": native.PIDS,
                "NanoCpus": 1000000000, "Privileged": False, "SecurityOpt": ["no-new-privileges:true"]}}

    def test_resource_profile_requires_exact_fixed_identity_and_hard_limits(self):
        row = self.profile()
        self.assertTrue(all(native.resource_checks(row, row["Image"], "owner", "internal-test").values()))
        for key, value in (("Memory", 0), ("MemorySwap", -1), ("PidsLimit", 512), ("NetworkMode", "host"),
                           ("Privileged", True), ("PortBindings", {"8080/tcp": [{"HostPort": "8080"}]}),
                           ("SecurityOpt", [])):
            bad = copy.deepcopy(row)
            bad["HostConfig"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                native.resource_checks(bad, row["Image"], "owner", "internal-test")

    def test_changed_image_owner_or_oom_not_adopted(self):
        row = self.profile()
        for image, owner in (("sha256:" + "b" * 64, "owner"), (row["Image"], "other")):
            with self.subTest(image=image, owner=owner), self.assertRaises(ValueError):
                native.resource_checks(row, image, owner, "internal-test")
        row["State"]["OOMKilled"] = True
        with self.assertRaises(ValueError):
            native.resource_checks(row, row["Image"], "owner", "internal-test")

    def test_receipt_checks_do_not_accept_regular_202_or_boolean_counters(self):
        harness = object.__new__(native.Harness)
        harness.manifest = "a" * 64
        receipt = {"confirmed": True, "result": {"schema": 1, "manifest": harness.manifest,
                   "operation_id": "b" * 64, "action": "pause", "state": "closed", "durable": True, "confirmed": True,
                   "generation": 1, "previous_generation": 0, "active_leases": 0, "uncertain_leases": 0}}
        self.assertEqual(harness.checked_receipt(receipt, "b" * 64, "closed"), receipt["result"])
        bad_values = ({"status": 202}, {"confirmed": True}, {"confirmed": False, "result": receipt["result"]})
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                harness.checked_receipt(bad, "b" * 64, "closed")
        for field, value in (("schema", True), ("generation", True), ("previous_generation", False),
                             ("active_leases", False), ("uncertain_leases", False), ("durable", 1),
                             ("operation_id", "c" * 64), ("manifest", "d" * 64), ("active_leases", 1)):
            bad = copy.deepcopy(receipt)
            bad["result"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                harness.checked_receipt(bad, "b" * 64, "closed")

    def test_non_native_host_rejected_before_docker(self):
        with patch.object(native.platform, "system", return_value="Darwin"), patch.object(native.subprocess, "run") as command:
            with self.assertRaisesRegex(ValueError, "native"):
                native.Harness(Path("/none"), Path("/none"), "sha256:" + "a" * 64, Path("/none"), Path("/none"))
            command.assert_not_called()

    def test_default_cli_is_plan_only_even_without_sources(self):
        argv = ["verify", "--source", "/not-used", "--sender-image", "not-inspected", "--binary", "/not-used", "--output", "/not-created"]
        with patch.object(sys, "argv", argv), patch.object(native, "run") as run:
            self.assertEqual(native.main(), 0)
            run.assert_not_called()


class ImageIdentityTests(unittest.TestCase):
    def image(self):
        # Exact identity from failed native prepare, not a running container.
        return {"Id": "sha256:541096ae9b05d3b850341b78790e2ed31e94d5aa0ea086933c6ff0c09ffa2e63",
                "Architecture": "amd64", "Os": "linux",
                "RepoDigests": ["python@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"]}

    def test_tagged_fixed_reference_matches_same_repository_untagged_digest(self):
        row = self.image()
        native.image_checks(row, native.PYTHON_IMAGE)
        for repository in ("python", "docker.io/library/python", "index.docker.io/library/python"):
            with self.subTest(repository=repository):
                row["RepoDigests"] = [repository + "@" + native.PYTHON_IMAGE.split("@")[1]]
                native.image_checks(row, native.PYTHON_IMAGE)

    def test_registry_port_is_not_removed_as_an_image_tag(self):
        digest = "sha256:" + "a" * 64
        self.assertEqual(native.canonical_repo_digest("registry.example:5443/team/python:fixed@" + digest),
                         "registry.example:5443/team/python@" + digest)
        row = self.image()
        row["RepoDigests"] = ["registry.example:5444/team/python@" + digest]
        with self.assertRaisesRegex(ValueError, "repository digest"):
            native.image_checks(row, "registry.example:5443/team/python:fixed@" + digest)

    def test_same_digest_from_other_repository_or_registry_is_rejected(self):
        row = self.image()
        digest = native.PYTHON_IMAGE.split("@")[1]
        for repository in ("postgres", "team/python", "evil.example/library/python", "docker.io/library/python-extra"):
            row["RepoDigests"] = [repository + "@" + digest]
            with self.subTest(repository=repository), self.assertRaisesRegex(ValueError, "repository digest"):
                native.image_checks(row, native.PYTHON_IMAGE)

    def test_missing_changed_or_partial_digest_is_rejected(self):
        row = self.image()
        for values in (None, [], ["python@sha256:" + "b" * 64], ["python@sha256:e88e9763"], [native.PYTHON_IMAGE.split("@")[0]]):
            row["RepoDigests"] = values
            with self.subTest(values=values), self.assertRaises(ValueError):
                native.image_checks(row, native.PYTHON_IMAGE)
        for reference in ("python:latest", "python@sha256:short", "python@@sha256:" + "a" * 64,
                          "https://python@sha256:" + "a" * 64, "python/../other@sha256:" + "a" * 64):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                native.canonical_repo_digest(reference)

    def test_sender_id_and_dependency_platform_checks_remain_strict(self):
        row = self.image()
        native.image_checks(row, row["Id"])
        with self.assertRaisesRegex(ValueError, "sender image ID"):
            native.image_checks(row, "sha256:" + "b" * 64)
        for field, value in (("Architecture", "arm64"), ("Os", "windows"), ("Id", "541096ae"), ("Id", None)):
            invalid = {**row, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                native.image_checks(invalid, native.PYTHON_IMAGE)


class PostgresRestartOracleTests(unittest.TestCase):
    def harness(self, unavailable_confirmed=False):
        harness = object.__new__(native.Harness)
        harness.names = {"pg": "owned-synthetic-pg"}
        events = []
        harness.gate_snapshot = lambda: {"state": "closed"}
        harness.sample_memory = lambda *args: events.append(("memory", args))
        harness.command = lambda args, **kwargs: events.append(("command", args)) or "ready"
        harness.wait = lambda function, **kwargs: function()
        harness.http = lambda role, path, **kwargs: events.append(("http", role, path, kwargs))
        harness.private_control = lambda path, **kwargs: events.append(("private", path, kwargs)) or {"confirmed": unavailable_confirmed}
        harness.query_closed = lambda operation: events.append(("query", operation)) or {"state": "closed"}
        harness.refresh = lambda **kwargs: events.append(("refresh", kwargs))
        harness.case = lambda name, evidence: events.append(("case", name, evidence))
        return harness, events

    def test_unavailable_auth_401_is_not_closed_proof_then_exact_operation_and_503(self):
        harness, events = self.harness()
        operation = "c" * 64
        harness.postgres_restart_case(operation)
        self.assertEqual(events[1:4], [
            ("command", ["stop", "--time=15", "owned-synthetic-pg"]),
            ("http", "sender", "/v1/categories", {"expected": 401}),
            ("private", "/operations/" + operation, {"expected": 409})])
        self.assertEqual(events[4], ("command", ["start", "owned-synthetic-pg"]))
        self.assertEqual(events[6:8], [("query", operation), ("refresh", {"expected": 503})])
        self.assertIs(events[8][2]["unavailable"]["confirmed"], False)

    def test_unavailable_private_query_cannot_claim_confirmed_or_advance(self):
        harness, events = self.harness(unavailable_confirmed=True)
        with self.assertRaisesRegex(ValueError, "unavailable returned confirmed"):
            harness.postgres_restart_case("c" * 64)
        self.assertFalse(any(event[0] in {"query", "refresh", "case"} for event in events))
        self.assertFalse(any(event[0] == "command" and event[1][0] == "start" for event in events))


class WorkflowEvidenceTests(unittest.TestCase):
    def preservation_step(self):
        workflow = SCRIPTS.parent / ".github/workflows/miniflux-pause.yml"
        after = workflow.read_text().split("      - name: Preserve actual unit-test PostgreSQL state and identity\n", 1)[1]
        block = after.split("        run: |\n", 1)[1].split("      - uses:", 1)[0]
        return textwrap.dedent(block)

    def execute_preservation(self, failed_commands="", legacy=None):
        parent = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
        root = Path(tempfile.mkdtemp(prefix="pause-workflow-evidence-", dir=parent))
        binary = root / "bin"
        binary.mkdir()
        if legacy is not None:
            destination = root / "legacy-held-evidence"
            destination.mkdir()
            for name, value in legacy.items():
                (destination / name).write_text(json.dumps(value))
        docker = binary / "docker"
        docker.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import json,os,sys
            from pathlib import Path
            args=sys.argv[1:]
            with Path(os.environ['CALLS']).open('a') as out:
                out.write(json.dumps(args)+'\\n')
            name='inspect' if 'inspect' in args else ('dump' if 'pg_dump' in args else ('schemas' if 'psql' in args else ('events' if '/sys/fs/cgroup/memory.events' in args else 'peak')))
            if name in os.environ['FAIL_COMMANDS'].split(','):
                print('preserved partial '+name)
                print('synthetic '+name+' failure',file=sys.stderr)
                sys.exit(7)
            print({'inspect':'{}','peak':'1234','events':'oom 0\\noom_kill 0','schemas':'["handoff_test_1","outbox_test_1","pause_api_1"]',
                'dump':'-- full dedicated synthetic database including future schemas'}[name])
            '''))
        docker.chmod(0o700)
        environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
                       "RUNNER_TEMP": str(root), "PG_ID": "a" * 64, "CALLS": str(root / "calls.jsonl"),
                       "FAIL_COMMANDS": failed_commands}
        result = subprocess.run(["/bin/bash", "-e", "-c", self.preservation_step()], env=environment,
                                capture_output=True, text=True, timeout=5, check=False)
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        return root / "pause-source-evidence", result, calls

    def test_full_dedicated_database_and_schema_inventory_are_preserved(self):
        evidence, result, calls = self.execute_preservation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 5)
        dump = next(argv for argv in calls if "pg_dump" in argv)
        self.assertEqual(dump[-4:], ["-U", "postgres", "-d", "synthetic_outbox"])
        self.assertFalse(any(arg.startswith("--schema") for arg in dump))
        inventory = json.loads((evidence / "PG_SCHEMAS.json").read_text())
        self.assertIn("pause_api_1", inventory)
        for path in evidence.glob("*.exit_code"):
            self.assertEqual(path.read_text(), "0\n")
        self.assertEqual((evidence / "SYNTHETIC_TEST_DATABASE.sql").stat().st_mode & 0o777, 0o600)

    def test_failed_inspect_and_partial_dump_do_not_erase_other_evidence_or_pass(self):
        evidence, result, calls = self.execute_preservation("inspect,dump")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(calls), 5)
        for name in ("PG_INSPECT.json", "SYNTHETIC_TEST_DATABASE.sql"):
            self.assertEqual((evidence / (name + ".exit_code")).read_text(), "7\n")
            self.assertIn("preserved partial", (evidence / name).read_text())
            self.assertIn("failure", (evidence / (name + ".stderr")).read_text())
        for name in ("PG_SCHEMAS.json", "PG_MEMORY_PEAK.txt", "PG_MEMORY_EVENTS.txt"):
            self.assertEqual((evidence / (name + ".exit_code")).read_text(), "0\n")
            self.assertTrue((evidence / name).read_bytes())

    def test_missing_memory_events_is_preserved_and_fails_without_skipping_dump(self):
        evidence, result, calls = self.execute_preservation("events")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(calls), 5)
        self.assertEqual((evidence / "PG_MEMORY_EVENTS.txt.exit_code").read_text(), "7\n")
        self.assertEqual((evidence / "SYNTHETIC_TEST_DATABASE.sql.exit_code").read_text(), "0\n")
        self.assertIn("full dedicated synthetic", (evidence / "SYNTHETIC_TEST_DATABASE.sql").read_text())

    def test_exact_summarized_byte_fixture_exclusions_reach_dump(self):
        row = {"delivery_id": "b" * 64, "body_sha256": "b" * 64,
               "state": "held", "body_bytes": 1048576, "charged_bytes": 1049600}
        legacy = {"PLAN.json": {"production": False, "models": 0,
                     "names": {"SOURCE": "held_source_isolation_" + "a" * 16}},
                  "SYNTHETIC_DUMP.json": {"exit_code": 0, "large_byte_fixture_raw_not_exported": True,
                     "sql": "small", "sha256": hashlib.sha256(b"small").hexdigest()},
                  "BYTE_FIXTURE_webhook_outbox.json": [row],
                  "BYTE_FIXTURE_webhook_legacy_originals.json": [row]}
        _, result, calls = self.execute_preservation(legacy=legacy)
        self.assertEqual(result.returncode, 0, result.stderr)
        dump = next(argv for argv in calls if "pg_dump" in argv)
        self.assertEqual([v for v in dump if v.startswith("--exclude")], [
            "--exclude-table-data=held_source_isolation_" + "a" * 16 + "_bytes." + table
            for table in ("webhook_outbox", "webhook_legacy_originals")])
        self.assertFalse(any(v.startswith("--schema") for v in dump))

    def test_incomplete_byte_fixture_evidence_is_not_silently_excluded(self):
        evidence, result, calls = self.execute_preservation(legacy={"PLAN.json": {"names": {"SOURCE": "*"}}})
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any("pg_dump" in argv for argv in calls))
        self.assertNotEqual((evidence / "PG_DUMP_EXCLUSIONS.txt.exit_code").read_text(), "0\n")
        self.assertEqual((evidence / "SYNTHETIC_TEST_DATABASE.sql.exit_code").read_text(), "1\n")
        self.assertIn("partial exports retained", (evidence / "SYNTHETIC_TEST_DATABASE.sql.stderr").read_text())


if __name__ == "__main__":
    unittest.main()
