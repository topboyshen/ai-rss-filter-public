"""Resource/evidence runner guards; these unit checks are not native proof."""
import importlib.util
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("completion_runner", ROOT / "scripts/verify_cutover_completion_native.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class CompletionNativeGuards(unittest.TestCase):
    def test_non_native_refuses_before_docker_or_build(self):
        with patch.object(runner.platform, "system", return_value="Darwin"), patch.object(runner, "base_runner") as base:
            with self.assertRaises(ValueError):
                runner.run(Path("unused"), Path("unused"))
            base.assert_not_called()

    def test_all_domains_and_historical_crash_recovery_run_under_actual_limits(self):
        expected = {"tests/test_independent_drain_inventory.py", "tests/test_frozen_cutover.py",
                    "tests/test_handoff_runtime_adapter.py", "tests/test_controlled_startup.py",
                    "tests/test_frozen_cutover_actual_startup.py", "tests/test_miniflux_handoff_adapter.py",
                    "tests/test_miniflux_host_joint_native.py", "tests/test_durable_handoff_crash_diagnostics.py",
                    "tests/test_durable_handoff_cutover.py", "tests/test_production_migration.py",
                    "tests/test_production_sender_driver.py", "tests/test_sender_drain_contract.py",
                    "tests/test_sender_pg_deadlines.py", "tests/test_production_startup_pg_fixture.py",
                    "tests/test_receiver_drain_transfer.py", "tests/test_receiver_drain_driver.py",
                    "tests/test_takeover_readonly_preflight.py"}
        actual = {node.value for node in ast.walk(ast.parse(runner.CONTAINER_TEST))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("tests/")}
        self.assertEqual(set(runner.TESTS), expected)
        self.assertEqual(len(runner.TESTS), len(expected))
        self.assertEqual(actual, expected)
        for name in ("app.py", "worker_main.py", "core", "db", "services", "config.yaml", "modules",
                     "ops/miniflux", ".github/workflows", "gunicorn.conf.py"):
            self.assertIn(name, runner.MOUNTS)
        self.assertIn("memory.peak", runner.CONTAINER_TEST)
        self.assertIn("memory.events", runner.CONTAINER_TEST)

    def test_temporary_dependency_image_never_uses_release_tag(self):
        source = Path(runner.__file__).read_text()
        self.assertIn('"ai-filter-isolation-cutover:"', source)
        self.assertNotIn('"my-ai-filter:', source)
        self.assertIn("prepared_image=image[\"Id\"]", source)
        self.assertIn("inspect_actual_runtime", source)


if __name__ == "__main__":
    unittest.main()
