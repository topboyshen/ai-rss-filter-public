"""Resource/evidence runner guards; these unit checks are not native proof."""
import importlib.util
import ast
from pathlib import Path
import unittest

import pytest
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
                    "tests/test_production_startup.py", "tests/test_production_startup_native.py",
                    "tests/test_production_sender_driver.py", "tests/test_sender_drain_contract.py",
                    "tests/test_sender_pg_deadlines.py", "tests/test_production_startup_pg_fixture.py",
                    "tests/test_receiver_drain_transfer.py", "tests/test_receiver_drain_driver.py",
                    "tests/test_takeover_readonly_preflight.py", "tests/test_joint_drain_controller.py",
                    "tests/test_receiver_drain_fixture_runtime.py", "tests/test_sender_receiver_joint_native.py",
                    "tests/test_production_activation.py",
                    "tests/test_production_initial_fence.py",
                    "tests/test_production_initial_candidates.py", "tests/test_production_initial_migration.py",
                    "tests/test_production_container_lifecycle.py",
                    "tests/test_production_active_verification.py",
                    "tests/test_release_container_lifecycle.py",
                    "tests/test_miniflux_activation_adapter.py",
                    "tests/test_sender_activation_driver.py",
                    "tests/test_production_activation_native.py"}
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


def test_only_owned_evidence_child_can_be_the_disk_fixture_mount(tmp_path):
    import os
    base = runner.base_runner()
    root = tmp_path.resolve() / "evidence"
    root.mkdir(mode=0o700)
    args = base.disk_fixture_arguments(root, True)
    assert args == ["--mount", f"type=bind,src={root}/disk-fixtures,dst=/tmp/cutover-fixtures"]
    assert (root / "disk-fixtures").stat().st_mode & 0o777 == 0o700
    assert base.disk_fixture_arguments(root, False) == []
    with pytest.raises(FileExistsError):
        base.disk_fixture_arguments(root, True)
    unsafe = tmp_path.resolve() / "public"
    unsafe.mkdir(mode=0o755)
    # The full gate intentionally uses umask 077; build the actual unsafe mode.
    unsafe.chmod(0o755)
    assert unsafe.stat().st_mode & 0o777 == 0o755
    with pytest.raises(ValueError):
        base.disk_fixture_arguments(unsafe, True)
    link = tmp_path.resolve() / "alias"
    link.symlink_to(root)
    with pytest.raises(ValueError):
        base.disk_fixture_arguments(link, True)
    assert os.getuid() == (root / "disk-fixtures").stat().st_uid


def test_actual_resource_inspect_requires_exact_private_disk_fixture_alias(tmp_path):
    from copy import deepcopy
    import os
    import pytest
    base = runner.base_runner()
    root = tmp_path.resolve() / "evidence"
    root.mkdir(mode=0o700)
    base.disk_fixture_arguments(root, True)
    state = {"Image": "sha256:fixture", "Config": {"User": f"{os.getuid()}:{os.getgid()}"},
        "HostConfig": {"Memory": base.MEMORY, "MemorySwap": base.MEMORY, "PidsLimit": base.PIDS,
            "NanoCpus": 1000000000, "Tmpfs": base.TMPFS.copy(), "NetworkMode": "none", "ReadonlyRootfs": True,
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
        "State": {"OOMKilled": False, "ExitCode": 0, "Running": False},
        "Mounts": [{"Type": "bind", "RW": False, "Destination": "/work/" + name} for name in base.READONLY_MOUNTS] + [
            {"Type": "bind", "RW": True, "Source": str(root), "Destination": "/evidence"},
            {"Type": "bind", "RW": True, "Source": str(root / "disk-fixtures"), "Destination": base.DISK_FIXTURE_TARGET}]}
    assert all(base.check_state(state, "sha256:fixture", disk_fixtures=True, evidence_root=root).values())
    for field, value in (("Source", str(root)), ("Source", "/tmp"), ("Destination", "/tmp"), ("RW", False), ("Type", "volume")):
        wrong = deepcopy(state)
        wrong["Mounts"][-1][field] = value
        with pytest.raises(ValueError):
            base.check_state(wrong, "sha256:fixture", disk_fixtures=True, evidence_root=root)
    for field in ("Memory", "MemorySwap", "PidsLimit", "NanoCpus", "Tmpfs"):
        wrong = deepcopy(state)
        wrong["HostConfig"][field] = None
        with pytest.raises(ValueError):
            base.check_state(wrong, "sha256:fixture", disk_fixtures=True, evidence_root=root)
    wrong = deepcopy(state)
    wrong["State"]["ExitCode"] = 1
    with pytest.raises(ValueError):
        base.check_state(wrong, "sha256:fixture", disk_fixtures=True, evidence_root=root)


def test_cutover_storage_retains_originals_in_place_without_sparse_file_copy():
    parsed = ast.parse(runner.CONTAINER_TEST)
    calls = [node for node in ast.walk(parsed) if isinstance(node, ast.Call)]
    assert not any(isinstance(node.func, ast.Attribute) and node.func.attr == "copytree" for node in calls)
    assert "fixture_mount.samefile('/evidence/disk-fixtures')" in runner.CONTAINER_TEST
    assert "assert fixtures.samefile(evidence_path)" in runner.CONTAINER_TEST
    assert "'retained_in_place':True" in runner.CONTAINER_TEST
    assert "disk_fixtures=True" in Path(runner.__file__).read_text()
