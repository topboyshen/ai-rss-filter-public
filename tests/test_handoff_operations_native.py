"""The isolation runner must not accept missing limits or a failed container."""
import copy
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "operations_native", Path(__file__).resolve().parents[1] / "scripts/verify_handoff_operations_native.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OperationsNativeGateTests(unittest.TestCase):
    def good_state(self):
        return {"Image": "sha256:fixture", "Config": {"User": f"{os.getuid()}:{os.getgid()}"}, "HostConfig": {
            "Memory": 805306368, "MemorySwap": 805306368, "PidsLimit": 256,
            "NanoCpus": 1000000000, "Tmpfs": MODULE.TMPFS.copy(), "NetworkMode": "none", "ReadonlyRootfs": True,
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
        }, "State": {"OOMKilled": False, "ExitCode": 0, "Running": False},
            "Mounts": [{"Type": "bind", "RW": False, "Destination": "/work/" + name}
                       for name in MODULE.READONLY_MOUNTS] +
                      [{"Type": "bind", "RW": True, "Destination": "/evidence"}]}

    def test_exact_success(self):
        self.assertTrue(all(MODULE.check_state(self.good_state(), "sha256:fixture").values()))

    def test_zero_client_return_cannot_hide_failed_or_oom_container(self):
        for field, value in (("OOMKilled", True), ("Running", True), ("ExitCode", 1)):
            with self.subTest(field=field):
                state = self.good_state()
                state["State"][field] = value
                with self.assertRaises(ValueError):
                    MODULE.check_state(state, "sha256:fixture")

    def test_all_limits_and_network_must_match(self):
        state = self.good_state()
        for key, value in state["HostConfig"].items():
            with self.subTest(key=key):
                invalid = copy.deepcopy(state)
                invalid["HostConfig"][key] = None
                with self.assertRaises(ValueError):
                    MODULE.check_state(invalid, "sha256:fixture")
        with self.assertRaises(ValueError):
            MODULE.check_state(state, "sha256:other")

    def test_non_native_execution_stops_before_any_docker_access(self):
        with patch.object(MODULE.platform, "system", return_value="Darwin"):
            with patch.object(MODULE, "command", side_effect=AssertionError("Docker reached")):
                with self.assertRaises(ValueError):
                    MODULE.run(Path("unused"), Path("unused"))

    def test_failure_stops_only_owned_running_container_and_keeps_evidence(self):
        import json
        for owned in (True, False):
            with self.subTest(owned=owned):
                state = {"Config": {"Labels": {"ai-filter.synthetic-operations": "test" if owned else "other"}},
                         "State": {"Running": True}}
                with patch.object(MODULE, "command", return_value=(0, json.dumps([state]))) as command:
                    MODULE.preserve_failed_container("test", Path("evidence"))
                stop_calls = [call for call in command.call_args_list if "stop" in call.args[0]]
                self.assertEqual(len(stop_calls), int(owned))
                self.assertIn("failure-container-logs", str(command.call_args_list[-1]))

    def test_mount_must_not_be_writable_or_unexpected(self):
        for change in ({"RW": True}, {"Destination": "/var/run/docker.sock"}, {"Type": "volume"}):
            state = self.good_state()
            state["Mounts"][0].update(change)
            with self.assertRaises(ValueError):
                MODULE.check_state(state, "sha256:fixture")

    def test_governance_sources_and_tests_cannot_silently_drop_from_native_gate(self):
        for source, test in (("production_handoff_adapter.py", "test_production_handoff_adapter.py"),
                             ("observe_handoff_capacity.py", "test_handoff_capacity_observer.py")):
            with self.subTest(source=source):
                self.assertIn("scripts/" + source, MODULE.FILES)
                self.assertIn("tests/" + test, MODULE.FILES)
                self.assertIn(test, MODULE.CONTAINER_TEST)

    def test_all_isolated_fixture_directories_are_preserved_in_evidence_mount(self):
        source = Path(MODULE.__file__).read_text()
        self.assertIn('"AI_FILTER_CUTOVER_EVIDENCE_DIR=/evidence"', source)
        self.assertIn('"AI_FILTER_HANDOFF_EVIDENCE_DIR=/evidence"', source)


if __name__ == "__main__":
    unittest.main()
