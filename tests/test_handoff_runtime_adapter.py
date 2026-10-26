"""Real file/process/loopback tests; injected inspect is NOT Docker evidence."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import handoff_host_cron as host  # noqa: E402
import handoff_runtime_adapter as runtime  # noqa: E402
from durable_handoff_cutover import CutoverError, durable_write, encoded, read_private  # noqa: E402
from handoff_control_endpoint import ControlServer  # noqa: E402


def fixture_directory():
    parent = os.environ.get("AI_FILTER_CUTOVER_EVIDENCE_DIR")
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="runtime-adapter-", dir=parent))


class LocalTransport:
    """Explicit synthetic inspect, actual registered gates. Never production proof."""
    def __init__(self, gates, rows, coverage):
        self.gates, self.rows, self.raw_coverage = gates, rows, coverage
        self.stops, self.queries = [], []
        self.lost_ack = False
        self.raw_ack = None

    def inspect(self, _ids):
        return copy.deepcopy(self.rows)

    def coverage(self):
        return copy.deepcopy(self.raw_coverage)

    def stop(self, name, operation, generation, reason):
        self.stops.append((name, operation))
        receipt = self.gates[name].stop(operation, generation, reason)
        if self.lost_ack:
            raise TimeoutError("synthetic lost HTTP ACK after real strict stop")
        return self.raw_ack if self.raw_ack is not None else receipt

    def query(self, name, operation):
        self.queries.append((name, operation))
        return self.gates[name].query(operation)


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.root = fixture_directory()
        self.gate = self.make_gate("all", list(runtime.ENTRANCES))
        wrapper = self.root / "wrapper.py"
        wrapper.write_bytes((SCRIPTS / "handoff_host_cron.py").read_bytes())
        wrapper.chmod(0o600)
        helper = self.root / "durable_handoff_cutover.py"
        helper.write_bytes((SCRIPTS / "durable_handoff_cutover.py").read_bytes())
        helper.chmod(0o600)
        self.wrapper = wrapper
        self.line = f"0 * * * * {sys.executable} -B {wrapper} --root {self.gate.root} --command noop --isolation"
        self.crontab = "# synthetic isolated crontab, not installed on host\n" + self.line + "\n"
        component = {"id": "1" * 64, "image": "sha256:" + "2" * 64, "revision": "3" * 40,
                     "started_at": "2026-10-23T00:00:00.123456789Z", "restart_count": 0, "role": "web",
                     "user": "0", "memory": 768 * 1024 * 1024, "pids": 256,
                     "mounts": [{"Type": "bind", "Source": str(self.root), "Destination": "/evidence", "RW": True}]}
        self.rows = [{"Id": component["id"], "Name": "/isolated", "Image": component["image"],
                      "RestartCount": 0, "Config": {"Env": ["AI_FILTER_ROLE=web"], "User": "",
                      "Labels": {"org.opencontainers.image.revision": component["revision"]}},
                      "State": {"Running": True, "OOMKilled": False, "Paused": False, "Restarting": False,
                                "StartedAt": component["started_at"]},
                      "HostConfig": {"Memory": component["memory"], "PidsLimit": component["pids"]}, "Mounts": component["mounts"]}]
        wrappers = {str(wrapper): {"sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(), "uid": os.geteuid(), "mode": 0o600}}
        registration = self.gate.registration()
        now = int(time.time())
        self.plan = {"schema": 1, "scope": "isolation", "run_id": "4" * 32, "not_before": now - 1, "expires_at": now + 120,
                     "components": {"isolated": component}, "controls": {"all": {**registration, "generation": 0}},
                     "cron": {"crontab_sha256": hashlib.sha256(self.crontab.encode()).hexdigest(), "wrappers": wrappers,
                              "jobs": [{"line": self.line, "control_id": "all"}]}}
        self.coverage = {"crontab": self.crontab, "wrappers": wrappers, "registrations": {"all": registration}}
        self.transport = LocalTransport({"all": self.gate}, self.rows, self.coverage)
        self.adapter = runtime.RuntimeHandoffAdapter(self.plan, self.transport, self.root / "evidence")
        self.operation = "5" * 64

    def make_gate(self, name, entrances):
        root = self.root / name
        manifest = {"schema": 1, "scope": "isolation", "run_id": "a" * 32, "control_id": name,
                    "source_sha256": host.source_sha256(), "entrances": entrances,
                    "commands": {"noop": [sys.executable, "-c", "raise SystemExit(0)"]}}
        host.provision(root, manifest)
        return host.HostCronGate(root)

    def child_activity(self, crash=False):
        code = (
            "import sys,os,select;sys.path.insert(0,sys.argv[1]);from handoff_host_cron import HostCronGate\n"
            "with HostCronGate(sys.argv[2]).activity('noop') as result:\n"
            " print('entered',flush=True)\n"
            + (" os._exit(7)\n" if crash else
               " ready=select.select([sys.stdin],[],[],5)[0]\n if ready: sys.stdin.readline()\n result['returncode']=0 if ready else 1\n")
        )
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(SCRIPTS), str(self.gate.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertTrue(select.select([child.stdout], [], [], 4)[0], "child handshake missing")
        self.assertEqual(child.stdout.readline().strip(), "entered")
        return child

    def finish_child(self, child):
        output, error = child.communicate("continue\n", timeout=4)
        self.assertEqual(child.returncode, 0, (output, error))


class RuntimeAdapterTests(RuntimeFixture):
    def test_installed_wrapper_cli_runs_exact_command_and_records_exit(self):
        result = subprocess.run([sys.executable, "-B", str(self.wrapper), "--root", str(self.gate.root),
                                 "--command", "noop", "--isolation"], capture_output=True, text=True,
                                timeout=4, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"returncode": 0, "scope": "isolation", "switch_authorized": False})
        self.assertEqual(len(list((self.gate.root / "finishes").glob("*.json"))), 1)

    def test_real_gate_stop_exact_receipt_not_queue_empty(self):
        self.gate.run_registered("noop")
        result = self.adapter.stop_producers(self.operation, "bounded trial complete")
        self.assertEqual(result["status"], "listed_producers_stopped")
        self.assertFalse(result["queue_checked"])
        self.assertFalse(result["switch_authorized"])
        self.assertFalse(result["production_adapter_bound"])
        self.assertEqual(len(self.transport.stops), 1)
        self.assertEqual(len(self.transport.queries), 1)
        again = self.adapter.query_operation(self.operation)
        self.assertEqual(again["receipts"], result["receipts"])
        self.assertEqual(len(self.transport.stops), 1)

    def test_inflight_naturally_exits_after_stop_then_query(self):
        child = self.child_activity()
        try:
            with self.assertRaises(TimeoutError):
                self.adapter.stop_producers(self.operation, "close before old refresh lock")
            with self.assertRaises(CutoverError):
                self.gate.run_registered("noop")
        finally:
            self.finish_child(child)
        result = self.adapter.query_operation(self.operation)
        self.assertEqual(result["status"], "listed_producers_stopped")
        self.assertEqual(len(self.transport.stops), 1)
        self.assertEqual(len(list((self.gate.root / "runs").glob("*.json"))), 1)

    def test_process_crash_remains_unknown_without_kill_or_fake_finish(self):
        child = self.child_activity(crash=True)
        child.communicate(timeout=4)
        self.assertEqual(child.returncode, 7)
        with self.assertRaisesRegex(CutoverError, "unobserved"):
            self.adapter.stop_producers(self.operation, "orphan cannot count as drained")
        with self.assertRaises(CutoverError):
            self.adapter.query_operation(self.operation)
        self.assertEqual(len(list((self.gate.root / "finishes").glob("*.json"))), 0)
        self.assertFalse((self.gate.root / "receipt.json").exists())

    def test_lost_ack_queries_only_exact_operation_after_reconstruct(self):
        self.transport.lost_ack = True
        with self.assertRaises(TimeoutError):
            self.adapter.stop_producers(self.operation, "lost response")
        restored = runtime.RuntimeHandoffAdapter(self.plan, self.transport, self.adapter.root)
        self.assertEqual(restored.query_operation(self.operation)["status"], "listed_producers_stopped")
        self.assertEqual(len(self.transport.stops), 1)
        with self.assertRaises(CutoverError):
            restored.stop_producers(self.operation, "lost response")

    def test_partial_controls_not_reported_complete_or_blindly_started(self):
        second = self.make_gate("second", [runtime.ENTRANCES[-1]])
        # Re-provisioning is not allowed: use an independently bound first gate.
        first = self.make_gate("first", list(runtime.ENTRANCES[:-1]))
        self.plan["controls"] = {name: {**gate.registration(), "generation": 0} for name, gate in (("first", first), ("second", second))}
        self.plan["cron"]["jobs"][0]["control_id"] = "first"
        line = self.line.replace(str(self.gate.root), str(first.root))
        self.plan["cron"]["jobs"][0]["line"] = line
        self.coverage["crontab"] = line + "\n"
        self.plan["cron"]["crontab_sha256"] = hashlib.sha256(self.coverage["crontab"].encode()).hexdigest()
        self.coverage["registrations"] = {name: gate.registration() for name, gate in (("first", first), ("second", second))}
        transport = LocalTransport({"first": first, "second": second}, self.rows, self.coverage)
        transport.lost_ack = True
        adapter = runtime.RuntimeHandoffAdapter(self.plan, transport, self.root / "partial")
        with self.assertRaises(TimeoutError):
            adapter.stop_producers(self.operation, "first ack lost")
        result = adapter.query_operation(self.operation)
        self.assertEqual(result["status"], "stop_incomplete")
        self.assertEqual(result["remaining_controls"], ["second"])
        self.assertEqual(len(transport.stops), 1)
        self.assertFalse((second.root / "intent.json").exists())

    def test_regular_202_or_boolean_not_accepted(self):
        self.transport.raw_ack = {"status": 202, "stopped": True}
        with self.assertRaisesRegex(CutoverError, "stop receipt"):
            self.adapter.stop_producers(self.operation, "not a strict receipt")
        self.assertEqual(len(self.transport.queries), 0)

    def test_bool_generation_cannot_impersonate_exact_receipt(self):
        receipt = self.gate.stop("a" * 64, 0, "x")
        receipt["previous_generation"] = False
        with self.assertRaises(CutoverError):
            runtime._receipt(receipt, "all", self.plan["controls"]["all"], "a" * 64, "x")

    def test_local_intent_write_failure_no_remote_operation(self):
        original = runtime.durable_write
        def fail(path, value):
            if str(path).endswith(".intent.json"):
                raise OSError("synthetic ENOSPC before intent ACK")
            return original(path, value)
        with patch.object(runtime, "durable_write", side_effect=fail), self.assertRaises(OSError):
            self.adapter.stop_producers(self.operation, "no local durable intent")
        self.assertEqual(self.transport.stops, [])

    def test_visible_receipt_sync_failure_query_recovers_without_new_stop(self):
        original = runtime.durable_write
        def fail(path, value):
            original(path, value)
            if str(path).endswith(".receipt.json"):
                raise OSError("synthetic directory fsync uncertain after visibility")
        with patch.object(runtime, "durable_write", side_effect=fail), self.assertRaises(OSError):
            self.adapter.stop_producers(self.operation, "receipt sync uncertain")
        self.assertEqual(self.adapter.query_operation(self.operation)["status"], "listed_producers_stopped")
        self.assertEqual(len(self.transport.stops), 1)

    def test_conflicting_saved_receipt_is_preserved_not_overwritten(self):
        self.adapter.stop_producers(self.operation, "original")
        path = next(self.adapter.root.glob("*.receipt.json"))
        bad = read_private(path)
        bad["reason"] = "conflict"
        durable_write(path, bad)
        original = path.read_bytes()
        with self.assertRaises(CutoverError):
            self.adapter.query_operation(self.operation)
        self.assertEqual(path.read_bytes(), original)

    def test_wrong_operation_does_not_reissue_or_confirm(self):
        self.adapter.stop_producers(self.operation, "original")
        with self.assertRaises((FileNotFoundError, CutoverError)):
            self.adapter.query_operation("6" * 64)
        self.assertEqual(len(self.transport.stops), 1)

    def test_missing_or_duplicate_entrance_blocks_before_stop(self):
        for replacement in (list(runtime.ENTRANCES[:-1]), list(runtime.ENTRANCES) + [runtime.ENTRANCES[0]]):
            with self.subTest(replacement=replacement):
                bad = copy.deepcopy(self.plan)
                bad["controls"]["all"]["entrances"] = replacement
                with self.assertRaises(CutoverError):
                    runtime.RuntimeHandoffAdapter(bad, self.transport, self.root / "bad")
        self.assertEqual(self.transport.stops, [])

    def test_actual_runtime_identity_changes_block(self):
        changes = (("Image", "sha256:" + "9" * 64), ("RestartCount", 1))
        for key, value in changes:
            rows = copy.deepcopy(self.rows)
            rows[0][key] = value
            with self.subTest(key=key), self.assertRaises(CutoverError):
                runtime.inspect_binding(rows, self.plan["components"])
        for path, value in (("Memory", 0), ("PidsLimit", 512)):
            rows = copy.deepcopy(self.rows)
            rows[0]["HostConfig"][path] = value
            with self.subTest(path=path), self.assertRaises(CutoverError):
                runtime.inspect_binding(rows, self.plan["components"])

    def test_cron_extra_job_or_shell_suffix_not_covered(self):
        bad = copy.deepcopy(self.coverage)
        bad["crontab"] += "* * * * * /uncontrolled\n"
        with self.assertRaises(CutoverError):
            runtime.coverage_binding(bad, self.plan)
        for suffix in (" ; /bin/true", " >> /tmp/log", " && /bin/true"):
            with self.subTest(suffix=suffix), self.assertRaises(CutoverError):
                runtime._cron_command(self.line + suffix, self.plan["cron"]["wrappers"])

    def test_expired_or_production_plan_cannot_issue(self):
        with patch.object(runtime.time, "time", return_value=self.plan["expires_at"]):
            with self.assertRaises(CutoverError):
                self.adapter.stop_producers(self.operation, "expired")
        bad = copy.deepcopy(self.plan)
        bad["scope"] = "production"
        with self.assertRaises(CutoverError):
            runtime.RuntimeHandoffAdapter(bad, self.transport, self.root / "production")
        self.assertEqual(self.transport.stops, [])

    def test_gate_source_manifest_or_directory_copy_not_identity(self):
        replica = self.make_gate("replica", list(runtime.ENTRANCES))
        self.assertNotEqual(replica.registration()["binding_sha256"], self.gate.registration()["binding_sha256"])
        value = read_private(self.gate.root / "manifest.json")
        value["source_sha256"] = "0" * 64
        durable_write(self.gate.root / "manifest.json", value)
        with self.assertRaises(CutoverError):
            self.gate.registration()

    def test_gate_strict_capacity_and_private_permissions(self):
        with patch.object(host, "MAX_RUNS", 1):
            self.gate.run_registered("noop")
            with self.assertRaises(CutoverError):
                self.gate.run_registered("noop")
        (self.gate.root / "state.json").chmod(0o644)
        with self.assertRaises(CutoverError):
            self.gate.stop(self.operation, 0, "unsafe")


class LoopbackControlTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.token = "f" * 64
        self.server = ControlServer(self.gate.root, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.close_server)
        self.endpoint = {"all": {"url": f"http://127.0.0.1:{self.server.server_port}", "token": self.token}}

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers={"Authorization": "Bearer " + (token or self.token),
                               "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_authenticated_stop_query_and_rejection(self):
        transport = runtime.NativeDockerTransport(self.endpoint, self.plan["cron"]["wrappers"])
        self.assertEqual(transport._http("all", "GET", "/registration"), self.gate.registration())
        receipt = transport.stop("all", self.operation, 0, "actual HTTP")
        self.assertTrue(receipt["durable"])
        self.assertEqual(receipt, transport.query("all", self.operation))
        self.assertEqual(self.request("GET", "/registration", token="0" * 64)[0], 401)
        self.assertEqual(self.request("GET", "/unknown")[0], 404)
        self.assertEqual(self.request("POST", "/stop", encoded({"operation_id": self.operation, "generation": 0, "reason": "again"}))[0], 409)

    def test_strict_body_size_duplicate_keys_and_bool_generation(self):
        for body in (b"{" + b" " * 8192 + b"}", b'{"operation_id":"x","generation":0,"generation":0,"reason":"x"}',
                     encoded({"operation_id": self.operation, "generation": False, "reason": "x"})):
            with self.subTest(size=len(body)):
                self.assertIn(self.request("POST", "/stop", body)[0], (400, 409))
        self.assertFalse((self.gate.root / "intent.json").exists())

    def test_actual_http_inflight_unconfirmed_then_same_query_finishes(self):
        child = self.child_activity()
        transport = runtime.NativeDockerTransport(self.endpoint, self.plan["cron"]["wrappers"])
        try:
            with self.assertRaises(CutoverError):
                transport.stop("all", self.operation, 0, "HTTP wait")
            self.assertEqual(self.request("GET", "/operations/" + self.operation)[0], 409)
        finally:
            self.finish_child(child)
        self.assertEqual(transport.query("all", self.operation)["state"], "closed")

    def test_native_transport_reads_actual_wrapper_and_cron_gate(self):
        calls = []
        def process(argv, **kwargs):
            calls.append((argv, kwargs))
            output = self.crontab.encode() if argv == ["/usr/bin/crontab", "-l"] else encoded(self.rows)
            return subprocess.CompletedProcess(argv, 0, output, b"")
        transport = runtime.NativeDockerTransport(self.endpoint, self.plan["cron"]["wrappers"], runner=process)
        adapter = runtime.RuntimeHandoffAdapter(self.plan, transport, self.root / "http-evidence")
        self.assertEqual(adapter.stop_producers(self.operation, "protocol integration")["status"], "listed_producers_stopped")
        self.assertEqual(calls[0][0], ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "inspect", "1" * 64])
        self.assertTrue(all(call[1]["shell"] is False and call[1]["timeout"] == 5 for call in calls))

    def test_non_loopback_or_redirectable_control_target_rejected(self):
        for url in ("https://127.0.0.1:1", "http://localhost:1", "http://192.0.2.1:1", "http://127.0.0.1:1/path"):
            with self.subTest(url=url), self.assertRaises(CutoverError):
                runtime.NativeDockerTransport({"all": {"url": url, "token": self.token}}, {})


class LegacyCronBindingTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.old_line = "0 * * * * " + shlex.join(self.gate.manifest["commands"]["noop"])
        self.original = "# complete prior isolation cron; never installed\nSHELL=/bin/sh\n" + self.old_line + "\n"
        self.installed = "# replacement isolation cron; never installed\nSHELL=/bin/sh\n" + self.line + "\n"
        self.replacements = [{"old_line": self.old_line, "new_line": self.line, "control_id": "all"}]

    def bind(self, original=None, installed=None, replacements=None):
        return runtime.legacy_cron_binding(self.original if original is None else original,
                                          self.installed if installed is None else installed,
                                          self.replacements if replacements is None else replacements,
                                          self.plan["cron"]["wrappers"])

    def test_complete_old_bytes_actual_registered_command_and_schedule(self):
        result = self.bind()
        self.assertEqual(result["original_crontab"], self.original)
        self.assertEqual(result["original_sha256"], hashlib.sha256(self.original.encode()).hexdigest())
        self.assertEqual(result["replacements"][0]["registration"], self.gate.registration())
        self.assertFalse(result["installed_on_production"])

    def test_unlisted_old_job_or_duplicate_mapping_rejected(self):
        with self.assertRaises(CutoverError):
            self.bind(original=self.original + "* * * * * /bin/true\n")
        with self.assertRaises(CutoverError):
            self.bind(replacements=self.replacements * 2)

    def test_same_count_changed_old_bytes_is_not_coverage(self):
        with self.assertRaises(CutoverError):
            self.bind(original=self.original.replace("0 *", " 0 *"))

    def test_environment_or_schedule_change_blocks(self):
        with self.assertRaises(CutoverError):
            self.bind(installed=self.installed.replace("SHELL=/bin/sh", "SHELL=/bin/bash"))
        line = self.line.replace("0 *", "5 *")
        with self.assertRaises(CutoverError):
            self.bind(installed=self.installed.replace(self.line, line),
                      replacements=[{**self.replacements[0], "new_line": line}])

    def test_name_only_wrong_registered_argv_not_accepted(self):
        line = "0 * * * * /bin/true"
        with self.assertRaises(CutoverError):
            self.bind(original=self.original.replace(self.old_line, line),
                      replacements=[{**self.replacements[0], "old_line": line}])

    def test_unknown_special_schedule_and_shell_are_not_reinterpreted(self):
        for line in ("@reboot /bin/true", "0 * * * * /bin/true ; /bin/false", "0 * * * * /bin/true >> /tmp/x"):
            with self.subTest(line=line), self.assertRaises(CutoverError):
                self.bind(original=self.original.replace(self.old_line, line),
                          replacements=[{**self.replacements[0], "old_line": line}])

    def test_full_legacy_binding_must_match_observed_evidence(self):
        bound = self.bind()
        plan = copy.deepcopy(self.plan)
        plan["cron"]["legacy_binding"] = bound
        coverage = copy.deepcopy(self.coverage)
        coverage["legacy_binding"] = bound
        self.assertEqual(runtime.coverage_binding(coverage, plan), coverage)
        coverage["legacy_binding"] = {**bound, "original_crontab": "different"}
        with self.assertRaises(CutoverError):
            runtime.coverage_binding(coverage, plan)

    def test_native_transport_reads_private_original_and_actual_registered_gate(self):
        archive = self.root / "old-cron.json"
        durable_write(archive, {"crontab": self.original})
        def process(argv, **_kwargs):
            self.assertEqual(argv, ["/usr/bin/crontab", "-l"])
            return subprocess.CompletedProcess(argv, 0, self.installed.encode(), b"")
        transport = runtime.NativeDockerTransport({}, self.plan["cron"]["wrappers"], runner=process,
                    controls={"all": self.gate}, legacy_cron={"archive": str(archive), "replacements": self.replacements})
        self.assertEqual(transport.coverage()["legacy_binding"], self.bind())
        archive.chmod(0o644)
        with self.assertRaises(CutoverError):
            transport.coverage()


if __name__ == "__main__":
    unittest.main()
