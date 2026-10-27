"""Resource/evidence runner guards; these unit checks are not native proof."""
import importlib.util
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
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
                    "tests/test_production_create_scope.py",
                    "tests/test_production_active_verification.py",
                    "tests/test_release_container_lifecycle.py",
                    "tests/test_miniflux_activation_adapter.py",
                    "tests/test_sender_activation_driver.py",
                    "tests/test_production_activation_native.py",
                    "tests/test_production_release_lock.py", "tests/test_production_admission_lifecycle.py",
                    "tests/test_production_admission_supervisor.py", "tests/test_production_admission_watchdog.py",
                    "tests/test_private_release_control_subpaths.py", "tests/test_production_legacy_mount_fixture.py",
                    "tests/test_production_legacy_takeover.py", "tests/test_sender_namespace_http.py",
                    "tests/test_controlled_production_release.py", "tests/test_controlled_release_finalize.py",
                    "tests/test_controlled_release_shell.py", "tests/test_production_release_adapter_workflow_paths.py",
                    "tests/test_production_gateway_switch.py", "tests/test_production_gateway_native.py",
                    "tests/test_production_namespace_native.py", "tests/test_production_admission_image_sources.py",
                    "tests/test_production_admission_environment.py", "tests/test_production_sender_bootstrap.py",
                    "tests/test_production_sender_bootstrap_native.py", "tests/test_production_sender_native_bridge.py",
                    "tests/test_production_receiver_bootstrap.py",
                    "tests/test_production_receiver_bootstrap_native.py",
                    "tests/test_sender_external_postgres_identity.py"}
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
    assert "retain_fixture_storage(fixtures" in runner.CONTAINER_TEST
    assert runner.CONTAINER_TEST.index("retain_fixture_storage(fixtures") < runner.CONTAINER_TEST.index("pytest.main(")
    assert "confirm_fixture_storage(fixtures" in runner.CONTAINER_TEST
    assert "disk_fixtures=True" in Path(runner.__file__).read_text()


def fixture_disk(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    disk = evidence / "disk-fixtures"
    disk.mkdir(mode=0o700)
    fixtures = disk / "cutover-synthetic-exact"
    fixtures.mkdir(mode=0o700)
    return fixtures, evidence


def test_mapping_is_durable_before_tests_and_final_is_separate(tmp_path, monkeypatch):
    fixtures, evidence = fixture_disk(tmp_path)
    original, synced = os.fsync, []
    def sync(fd):
        synced.append(os.fstat(fd).st_ino)
        original(fd)
    monkeypatch.setattr(runner.os, "fsync", sync)
    value, digest = runner.retain_fixture_storage(fixtures, evidence)
    raw = (evidence / "FIXTURE_STORAGE.json").read_bytes()
    assert len(synced) == 4  # both original records and their containing directory
    assert set(value) == {"container_path", "evidence_relative_path", "device", "inode", "retained_in_place", "root_tmpfs_bytes"}
    assert json.loads((evidence / "FIXTURE_STORAGE_INITIAL.json").read_bytes()) == {
        "phase": "initial", "mapping_sha256": digest, "test_completion_confirmed": False}
    assert not (evidence / "FIXTURE_STORAGE_FINAL.json").exists()
    runner.confirm_fixture_storage(fixtures, evidence, value, digest)
    assert (evidence / "FIXTURE_STORAGE.json").read_bytes() == raw
    final = json.loads((evidence / "FIXTURE_STORAGE_FINAL.json").read_bytes())
    assert final["identity_reconfirmed"] is True and final["test_completion_confirmed"] is False
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in evidence.glob("*.json"))
    with pytest.raises(FileExistsError):
        runner.retain_fixture_storage(fixtures, evidence)


@pytest.mark.parametrize("drift", ["mode", "directory", "mapping", "mapping_link"])
def test_mapping_drift_never_creates_final_confirmation(tmp_path, drift):
    fixtures, evidence = fixture_disk(tmp_path)
    value, digest = runner.retain_fixture_storage(fixtures, evidence)
    mapping = evidence / "FIXTURE_STORAGE.json"
    if drift == "mode":
        fixtures.chmod(0o755)
    elif drift == "directory":
        fixtures.rename(fixtures.with_name("retained-old"))
        fixtures.mkdir(mode=0o700)
    elif drift == "mapping":
        mapping.write_bytes(b"changed")
    else:
        mapping.rename(mapping.with_name("retained-mapping.json"))
        mapping.symlink_to(evidence / "retained-mapping.json")
    with pytest.raises((AssertionError, OSError)):
        runner.confirm_fixture_storage(fixtures, evidence, value, digest)
    assert not (evidence / "FIXTURE_STORAGE_FINAL.json").exists()


def test_initial_mapping_sync_failure_is_not_confirmed(tmp_path, monkeypatch):
    fixtures, evidence = fixture_disk(tmp_path)
    monkeypatch.setattr(runner.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("synthetic sync failure")))
    with pytest.raises(OSError, match="synthetic sync failure"):
        runner.retain_fixture_storage(fixtures, evidence)
    assert not (evidence / "FIXTURE_STORAGE_INITIAL.json").exists()
    assert not (evidence / "FIXTURE_STORAGE_FINAL.json").exists()


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "wrong_mount"])
def test_initial_mapping_refuses_unverified_fixture_identity(tmp_path, unsafe):
    fixtures, evidence = fixture_disk(tmp_path)
    if unsafe == "mode":
        fixtures.chmod(0o755)
    elif unsafe == "symlink":
        old = fixtures.with_name("retained-original")
        fixtures.rename(old)
        fixtures.symlink_to(old, target_is_directory=True)
    else:
        other = tmp_path / fixtures.name
        other.mkdir(mode=0o700)
        fixtures = other
    with pytest.raises(AssertionError):
        runner.retain_fixture_storage(fixtures, evidence)
    assert not (evidence / "FIXTURE_STORAGE.json").exists()


def test_diagnostics_bound_phase_times_without_parameter_values(capsys):
    summary = runner.NativeDiagnostics()
    nodeid = "tests/test_synthetic.py::test_original[DO_NOT_PRINT_BODY_OR_TOKEN]"
    summary.pytest_runtest_logstart(nodeid, ())
    for phase in ("setup", "call", "teardown"):
        summary.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, when=phase, duration=0.25,
                                                       passed=True, failed=False, skipped=False))
    summary.pytest_runtest_logfinish(nodeid, ())
    output = capsys.readouterr().out
    assert "DO_NOT_PRINT" not in output
    rows = [json.loads(line) for line in output.splitlines()]
    assert [row["native_test"] for row in rows] == ["start", "setup", "call", "teardown", "end"]
    assert all(row["function"] == "test_original" and row["file"] == "test_synthetic.py" for row in rows)
    assert summary.passed == 1 and summary.failed == summary.skipped == 0
    summary.started = 2000
    summary.pytest_runtest_logstart(nodeid, ())
    summary.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, when="call", duration=0.25,
                                                   passed=True, failed=False, skipped=False))
    assert capsys.readouterr().out == "" and summary.passed == 2
    assert "'faulthandler_timeout=30'" in runner.CONTAINER_TEST
    assert 'command(DOCKER + ["start", "-a", name], output / "tests", 180)' in Path(runner.base_runner().__file__).read_text()


def test_real_nested_pytest_diagnostics_preserve_capture_and_json_stdout(tmp_path):
    path = tmp_path / "test_native_capture.py"
    path.write_text('''import json, os, pytest
@pytest.mark.parametrize("value", [1], ids=["DO_NOT_LOG_PRIVATE_PARAMETER"])
def test_capsys_json(capsys, value):
    assert capsys.readouterr().out == ""
    print(json.dumps({"value": value}))
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"value": 1}
    assert captured.err == ""

def test_capfd_stdout(capfd):
    assert capfd.readouterr().out == ""
    os.write(1, b"exact stdout\\n")
    assert capfd.readouterr().out == "exact stdout\\n"

@pytest.fixture
def setup_output(capsys):
    print("actual fixture setup")

def test_setup_capture(capsys, setup_output):
    assert capsys.readouterr().out == "actual fixture setup\\n"
''')
    code = ("import pytest,sys; from scripts.verify_cutover_completion_native import NativeDiagnostics; "
            "sys.exit(pytest.main(['-q','-o','addopts=','-p','no:cacheprovider',sys.argv[1]], plugins=[NativeDiagnostics()]))")
    result = subprocess.run([sys.executable, "-B", "-c", code, str(path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
    assert "DO_NOT_LOG_PRIVATE_PARAMETER" not in result.stdout + result.stderr
    phases = [json.loads(line[line.index("{"):])["native_test"] for line in result.stdout.splitlines()
              if '"native_test":' in line]
    assert phases == ["start", "setup", "call", "teardown", "end"] * 3


def test_fixed_partition_keeps_every_file_once_and_programs_bind_collection():
    from collections import Counter
    runner.validate_groups()
    assert [(name, len(files)) for name, files in runner.TEST_GROUPS] == [
        ("foundation", 22), ("activation", 12), ("adapters", 22)]
    selected = []
    for name, files in runner.TEST_GROUPS:
        program = runner.native_test_program(name)
        actual = [node.value for node in ast.walk(ast.parse(program)) if isinstance(node, ast.Constant)
                  and isinstance(node.value, str) and node.value.startswith("tests/")]
        assert Counter(actual) == Counter(files)
        assert "collection_verified" in program and "collection_sha256" in program
        assert "NativeDiagnostics(frozen_test_files)" in program
        assert "not summary.skipped" in program
        selected.extend(actual)
    assert Counter(selected) == Counter(runner.TESTS)
    with pytest.raises(ValueError, match="unknown"):
        runner.native_test_program("custom")


@pytest.mark.parametrize("drift", ["missing", "duplicate", "name", "empty"])
def test_partition_drift_stops_before_any_docker_or_build(monkeypatch, tmp_path, drift):
    groups = list(runner.TEST_GROUPS)
    name, files = groups[0]
    if drift == "missing":
        groups[0] = name, files[1:]
    elif drift == "duplicate":
        groups[0] = name, files + (files[0],)
    elif drift == "name":
        groups[0] = "custom", files
    else:
        groups[0] = name, ()
    monkeypatch.setattr(runner, "TEST_GROUPS", tuple(groups))
    with patch.object(runner, "base_runner") as docker:
        with pytest.raises(ValueError, match="fixed native groups"):
            runner.run(ROOT, tmp_path / "evidence")
        docker.assert_not_called()


def test_actual_collection_is_hashed_without_parameter_disclosure(monkeypatch, tmp_path, capsys):
    import hashlib
    monkeypatch.chdir(tmp_path)
    item = SimpleNamespace(path=tmp_path / "tests/test_fixed.py", nodeid="tests/test_fixed.py::test_one[PRIVATE_BODY]")
    summary = runner.NativeDiagnostics(("tests/test_fixed.py",))
    summary.pytest_collection_finish(SimpleNamespace(items=[item]))
    assert summary.collection_verified and summary.collected == 1
    assert summary.collection_sha256 == hashlib.sha256(item.nodeid.encode()).hexdigest()
    assert capsys.readouterr().out == ""
    with pytest.raises(ValueError, match="actual native collection"):
        runner.NativeDiagnostics(("tests/test_other.py",)).pytest_collection_finish(SimpleNamespace(items=[item]))
    summary = runner.NativeDiagnostics(("tests/test_fixed.py",))
    with pytest.raises(ValueError, match="actual native collection"):
        summary.pytest_collection_finish(SimpleNamespace(items=[]))
    assert not summary.collection_verified


@pytest.mark.parametrize("drift", [None, "collection_verified", "collected", "passed", "started", "finished", "failed", "skipped"])
def test_native_success_requires_every_collected_case_to_finish(drift):
    summary = runner.NativeDiagnostics()
    summary.collection_verified = True
    summary.collected = summary.passed = summary.started = summary.finished = 30
    if drift == "collection_verified":
        summary.collection_verified = False
    elif drift in {"failed", "skipped"}:
        setattr(summary, drift, 1)
    elif drift:
        setattr(summary, drift, 29)
    assert summary.complete() is (drift is None)
    assert "summary.complete()" in runner.CONTAINER_TEST


@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped"])
@pytest.mark.parametrize("family", ["fixture", "unittest"])
def test_real_subtests_are_counted_separately_and_never_hide_failure(tmp_path, outcome, family):
    path = tmp_path / "test_native_subtests.py"
    action = {"passed": "assert True", "failed": "assert False", "skipped": "pytest.skip('synthetic skip')"}[outcome]
    if family == "fixture":
        source = ("import pytest\ndef test_two_subtests(subtests):\n"
                  "    with subtests.test(case=1):\n        assert True\n"
                  "    with subtests.test(case=2):\n        " + action + "\n")
    else:
        action = action.replace("pytest.skip", "self.skipTest")
        source = ("import unittest\nclass TestNative(unittest.TestCase):\n"
                  "    def test_two_subtests(self):\n"
                  "        with self.subTest(case=1):\n            assert True\n"
                  "        with self.subTest(case=2):\n            " + action + "\n")
    path.write_text(source)
    code = ("import pytest,sys,json; from scripts.verify_cutover_completion_native import NativeDiagnostics; "
            "s=NativeDiagnostics(); r=pytest.main(['-q','-o','addopts=','-p','no:cacheprovider',sys.argv[1]], plugins=[s]); "
            "print(json.dumps({'complete':s.complete(),'collected':s.collected,'started':s.started,'finished':s.finished,"
            "'passed':s.passed,'subtests_passed':s.subtests_passed,'failed':s.failed,'skipped':s.skipped,"
            "'subtests_failed':s.subtests_failed,'subtests_skipped':s.subtests_skipped})); sys.exit(r)")
    result = subprocess.run([sys.executable, "-B", "-c", code, str(path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    value = json.loads(result.stdout.splitlines()[-1])
    assert value["collected"] == value["started"] == value["finished"] == 1
    assert value["passed"] in ({0, 1} if outcome == "failed" else {1})
    assert value["subtests_passed"] == (2 if outcome == "passed" else 1)
    assert value["subtests_failed"] == int(outcome == "failed")
    assert value["failed"] >= int(outcome == "failed")
    assert value["subtests_skipped"] == value["skipped"] == int(outcome == "skipped")
    assert value["complete"] is (outcome == "passed")
    assert result.returncode == int(outcome == "failed")


@pytest.mark.parametrize("failed_group", [None, "foundation", "activation", "adapters"])
def test_all_groups_required_and_first_failure_stops_later_stages(monkeypatch, tmp_path, failed_group):
    calls, saved = [], []
    def invoke(_repo, output, **kwargs):
        name = output.name.removeprefix("native-")
        calls.append(name)
        assert kwargs["prepared_image"] == "sha256:synthetic" and kwargs["disk_fixtures"] is True
        assert kwargs["container_test"] == runner.native_test_program(name)
        if name == failed_group:
            raise ValueError("original synthetic group failure")
    base = SimpleNamespace(DOCKER=["docker"], MEMORY=768 * 1024**2, PIDS=256, run=invoke,
        save=lambda path, value: saved.append((path.name, value)),
        checked=lambda *_args, **_kwargs: '[{"Id":"sha256:synthetic"}]')
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runner, "base_runner", lambda: base)
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(runner, "inspect_actual_runtime", lambda *_args: calls.append("runtime"))
    if failed_group is None:
        runner.run(ROOT, tmp_path / "evidence")
        assert calls == ["foundation", "activation", "adapters", "runtime"]
        assert saved[-1][0] == "GROUPS_RESULT.json"
    else:
        with pytest.raises(ValueError, match="original synthetic group failure"):
            runner.run(ROOT, tmp_path / "evidence")
        names = [name for name, _files in runner.TEST_GROUPS]
        assert calls == names[:names.index(failed_group) + 1]
        assert not any(name == "GROUPS_RESULT.json" for name, _value in saved)
    policy = next(value for name, value in saved if name == "TEST_GROUPS.json")
    assert policy["per_group_seconds"] == 180 and policy["memory"] == 768 * 1024**2 and policy["pids"] == 256
