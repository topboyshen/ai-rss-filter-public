"""Native bounded real initializer/process/HTTP/files tests, never production.

Dependencies are built into a uniquely named disposable test image. No release
tag (including .126) is built, edited or retagged. Runtime has no external
network, Docker socket or production mount. Failures keep original artifacts.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import time
import uuid

_DIAGNOSTIC_MONOTONIC = time.monotonic


def base_runner():
    path = Path(__file__).with_name("verify_handoff_operations_native.py")
    spec = importlib.util.spec_from_file_location("completion_native_base", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def write_fixture_record(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return hashlib.sha256(raw).hexdigest()


def fixture_storage_identity(fixtures, evidence_root):
    evidence_path = evidence_root / "disk-fixtures" / fixtures.name
    assert fixtures.samefile(evidence_path)
    for path in (fixtures, evidence_path):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
    info = fixtures.lstat()
    return {"container_path": str(fixtures), "evidence_relative_path": "disk-fixtures/" + fixtures.name,
            "device": info.st_dev, "inode": info.st_ino, "retained_in_place": True, "root_tmpfs_bytes": 134217728}


def retain_fixture_storage(fixtures, evidence_root):
    value = fixture_storage_identity(fixtures, evidence_root)
    digest = write_fixture_record(evidence_root / "FIXTURE_STORAGE.json", value)
    write_fixture_record(evidence_root / "FIXTURE_STORAGE_INITIAL.json",
                         {"phase": "initial", "mapping_sha256": digest, "test_completion_confirmed": False})
    return value, digest


def confirm_fixture_storage(fixtures, evidence_root, initial, digest):
    assert fixture_storage_identity(fixtures, evidence_root) == initial
    path = evidence_root / "FIXTURE_STORAGE.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 8192
        assert hashlib.sha256(os.read(fd, 8193)).hexdigest() == digest
    finally:
        os.close(fd)
    write_fixture_record(evidence_root / "FIXTURE_STORAGE_FINAL.json",
                         {"phase": "finally", "mapping_sha256": digest, "identity_reconfirmed": True,
                          "test_completion_confirmed": False})


def verify_git_fixture(fixtures, evidence_root):
    """Fail before pytest unless the pinned dependency supports real fixtures."""
    directory = fixtures / "git-dependency-smoke"
    directory.mkdir(mode=0o700)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(directory),
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    def invoke(arguments):
        return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *arguments],
            env=environment, capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    version = invoke(["--version"])
    if version != "git version 2.47.3":
        raise ValueError("native fixture Git version differs")
    invoke(["-C", str(directory), "init", "-q"])
    original = b"synthetic native dependency proof\n"
    (directory / "fixture.txt").write_bytes(original)
    invoke(["-C", str(directory), "add", "fixture.txt"])
    invoke(["-C", str(directory), "-c", "user.name=Synthetic fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "synthetic dependency proof"])
    if invoke(["-C", str(directory), "status", "--porcelain=v1"]):
        raise ValueError("native fixture Git commit is not clean")
    write_fixture_record(evidence_root / "GIT_DEPENDENCY.json", {
        "version": version, "init_add_commit_status_passed": True,
        "fixture_sha256": hashlib.sha256(original).hexdigest(), "remote_operations": False})


class NativeDiagnostics:
    """Bounded phase timing only; no parametrized values or failure text."""
    def __init__(self, expected_files=None):
        self.passed = self.failed = self.skipped = self.started = self.finished = 0
        self.subtests_passed = self.subtests_failed = self.subtests_skipped = 0
        self.origin = _DIAGNOSTIC_MONOTONIC()
        self.expected_files = tuple(expected_files) if expected_files is not None else None
        self.collection_verified = False
        self.collected = 0
        self.collection_sha256 = None

    def pytest_collection_finish(self, session):
        if self.expected_files is not None:
            paths = {str(Path(item.path).resolve().relative_to(Path.cwd().resolve())) for item in session.items}
            if paths != set(self.expected_files):
                raise ValueError("actual native collection does not match the fixed group")
        self.collected = len(session.items)
        self.collection_sha256 = hashlib.sha256("\n".join(item.nodeid for item in session.items).encode()).hexdigest()
        self.collection_verified = self.collected > 0

    def complete(self):
        return (self.collection_verified and self.collected > 0
                and self.passed == self.started == self.finished == self.collected
                and not self.failed and not self.skipped)

    def emit(self, nodeid, phase, **fields):
        if self.started > 2000:
            return
        parts = nodeid.split("::")
        filename, function = Path(parts[0]).name, parts[-1].split("[", 1)[0]
        filename = filename if re.fullmatch(r"test_[A-Za-z0-9_]{1,100}\.py", filename) else "redacted"
        function = function if re.fullmatch(r"test_[A-Za-z0-9_]{1,150}", function) else "redacted"
        print(json.dumps({"native_test": phase, "index": self.started, "file": filename, "function": function,
              "elapsed": round(_DIAGNOSTIC_MONOTONIC() - self.origin, 6), **fields}, sort_keys=True), flush=True)

    def pytest_runtest_logstart(self, nodeid, location):
        self.started += 1
        self.emit(nodeid, "start")

    def pytest_runtest_logreport(self, report):
        from _pytest.subtests import SubtestReport
        subtest = isinstance(report, SubtestReport)
        if report.when == "call" and report.passed:
            if subtest:
                self.subtests_passed += 1
            else:
                self.passed += 1
        if report.failed:
            self.failed += 1
            self.subtests_failed += int(subtest)
        if report.skipped:
            self.skipped += 1
            self.subtests_skipped += int(subtest)
        self.emit(report.nodeid, report.when, duration=round(report.duration, 6), subtest=subtest, outcome=report.outcome)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.finished += 1
        self.emit(nodeid, "end")


TESTS = ("tests/test_independent_drain_inventory.py", "tests/test_frozen_cutover.py",
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
         "tests/test_production_release_lock.py",
         "tests/test_production_admission_lifecycle.py",
         "tests/test_production_admission_supervisor.py",
         "tests/test_production_admission_watchdog.py",
         "tests/test_private_release_control_subpaths.py",
         "tests/test_production_legacy_mount_fixture.py",
         "tests/test_production_legacy_takeover.py",
         "tests/test_sender_namespace_http.py",
         "tests/test_controlled_production_release.py",
         "tests/test_controlled_release_finalize.py",
         "tests/test_controlled_release_shell.py",
         "tests/test_production_release_adapter_workflow_paths.py",
         "tests/test_production_gateway_switch.py",
         "tests/test_production_gateway_native.py",
         "tests/test_production_namespace_native.py",
         "tests/test_production_admission_image_sources.py",
         "tests/test_production_admission_environment.py",
         "tests/test_production_sender_bootstrap.py",
         "tests/test_production_sender_bootstrap_native.py",
         "tests/test_production_receiver_bootstrap.py",
         "tests/test_production_receiver_bootstrap_native.py",
         "tests/test_production_sender_native_bridge.py",
         "tests/test_sender_external_postgres_identity.py")
TEST_GROUPS = (("foundation", TESTS[:22]), ("activation", TESTS[22:34]), ("adapters", TESTS[34:]))
MOUNTS = ("scripts", "tests", "services", "routes", "core", "db", "utils", "workers", "modules",
          "templates", "app.py", "worker_main.py", "sse_engine.py", "logging_bootstrap.py",
          "scoring_provenance.py", "scoring_explanation.py", "config.yaml", "docker-compose.yml",
          "ops/isolation", "ops/miniflux", ".github/workflows", "gunicorn.conf.py",
          "Dockerfile", "requirements-prod.txt", "start.sh")
CONTAINER_TEST = r'''
import json, os, pathlib, platform, sys, tempfile, pytest
from scripts.verify_cutover_completion_native import NativeDiagnostics, retain_fixture_storage, confirm_fixture_storage, verify_git_fixture
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
fixture_mount = pathlib.Path('/tmp/cutover-fixtures')
assert fixture_mount.samefile('/evidence/disk-fixtures')
assert fixture_mount.stat().st_uid == os.getuid() and fixture_mount.stat().st_mode & 0o777 == 0o700
fixtures = pathlib.Path(tempfile.mkdtemp(prefix='cutover-synthetic-', dir=str(fixture_mount)))
os.environ['AI_FILTER_CUTOVER_EVIDENCE_DIR'] = str(fixtures)
os.environ['AI_FILTER_HANDOFF_EVIDENCE_DIR'] = str(fixtures)
initial, mapping_sha256 = retain_fixture_storage(fixtures, pathlib.Path('/evidence'))
verify_git_fixture(fixtures, pathlib.Path('/evidence'))
frozen_test_files = ('tests/test_independent_drain_inventory.py',
 'tests/test_frozen_cutover.py',
 'tests/test_handoff_runtime_adapter.py',
 'tests/test_controlled_startup.py',
 'tests/test_frozen_cutover_actual_startup.py',
 'tests/test_miniflux_handoff_adapter.py',
 'tests/test_miniflux_host_joint_native.py',
 'tests/test_durable_handoff_crash_diagnostics.py',
 'tests/test_durable_handoff_cutover.py',
 'tests/test_production_migration.py',
 'tests/test_production_startup.py',
 'tests/test_production_startup_native.py',
 'tests/test_production_sender_driver.py',
 'tests/test_sender_drain_contract.py',
 'tests/test_sender_pg_deadlines.py',
 'tests/test_production_startup_pg_fixture.py',
 'tests/test_receiver_drain_transfer.py',
 'tests/test_receiver_drain_driver.py',
 'tests/test_takeover_readonly_preflight.py',
 'tests/test_joint_drain_controller.py',
 'tests/test_receiver_drain_fixture_runtime.py',
 'tests/test_sender_receiver_joint_native.py',
 'tests/test_production_activation.py',
 'tests/test_production_initial_fence.py',
 'tests/test_production_initial_candidates.py',
 'tests/test_production_initial_migration.py',
 'tests/test_production_container_lifecycle.py',
 'tests/test_production_create_scope.py',
 'tests/test_production_active_verification.py',
 'tests/test_release_container_lifecycle.py',
 'tests/test_miniflux_activation_adapter.py',
 'tests/test_sender_activation_driver.py',
 'tests/test_production_activation_native.py',
 'tests/test_production_release_lock.py',
 'tests/test_production_admission_lifecycle.py',
 'tests/test_production_admission_supervisor.py',
 'tests/test_production_admission_watchdog.py',
 'tests/test_private_release_control_subpaths.py',
 'tests/test_production_legacy_mount_fixture.py',
 'tests/test_production_legacy_takeover.py',
 'tests/test_sender_namespace_http.py',
 'tests/test_controlled_production_release.py',
 'tests/test_controlled_release_finalize.py',
 'tests/test_controlled_release_shell.py',
 'tests/test_production_release_adapter_workflow_paths.py',
 'tests/test_production_gateway_switch.py',
 'tests/test_production_gateway_native.py',
 'tests/test_production_namespace_native.py',
 'tests/test_production_admission_image_sources.py',
 'tests/test_production_admission_environment.py',
 'tests/test_production_sender_bootstrap.py',
 'tests/test_production_sender_bootstrap_native.py',
 'tests/test_production_receiver_bootstrap.py',
 'tests/test_production_receiver_bootstrap_native.py',
 'tests/test_production_sender_native_bridge.py',
 'tests/test_sender_external_postgres_identity.py')
summary = NativeDiagnostics(frozen_test_files)
try:
    code = pytest.main(['-q', '-o', 'addopts=', '-o', 'faulthandler_timeout=30', '-p', 'no:cacheprovider', '--basetemp=' + str(fixtures / 'p')] + list(frozen_test_files), plugins=[summary])
finally:
    # This exact private subtree is already the evidence bind, so preserve
    # original inodes in place, including sparse files and failure originals.
    # Its container path remains under /tmp; the isolation guard is unchanged.
    confirm_fixture_storage(fixtures, pathlib.Path('/evidence'), initial, mapping_sha256)
peak = pathlib.Path('/sys/fs/cgroup/memory.peak')
events = pathlib.Path('/sys/fs/cgroup/memory.events')
ok = code == 0 and summary.passed >= 25 and not summary.failed and not summary.skipped and summary.complete()
print(json.dumps({'status':'passed' if ok else 'failed', 'tests_run':summary.passed,
    'collected':summary.collected,'started':summary.started,'finished':summary.finished,'collection_verified':summary.collection_verified,
    'collection_sha256':summary.collection_sha256,'selected_files':frozen_test_files,
    'subtests_passed':summary.subtests_passed,'subtests_failed':summary.subtests_failed,'subtests_skipped':summary.subtests_skipped,
    'failures':summary.failed,'errors':int(code not in (0,1)), 'skipped':summary.skipped,
    'memory_peak_bytes':int(peak.read_text()) if peak.exists() else None,
    'memory_events':events.read_text() if events.exists() else None,
    'machine':platform.machine(), 'production_access':False, 'model_calls':0,
    'scope':'real SQLite initializer, local processes/HTTP, private synthetic storage; not live cutover'}))
sys.exit(0 if ok else 1)
'''


def validate_groups():
    names = [name for name, _files in TEST_GROUPS]
    selected = [path for _name, files in TEST_GROUPS for path in files]
    if names != ["foundation", "activation", "adapters"] or any(not files for _name, files in TEST_GROUPS):
        raise ValueError("all three fixed native groups are required")
    if Counter(selected) != Counter(TESTS) or any(count != 1 for count in Counter(selected).values()):
        raise ValueError("fixed native groups omit or duplicate tests")


def native_test_program(group_name):
    validate_groups()
    selected = dict(TEST_GROUPS).get(group_name)
    if selected is None:
        raise ValueError("unknown fixed native group")
    tree = ast.parse(CONTAINER_TEST)
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "frozen_test_files" for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError("native collection binding is ambiguous")
    assignments[0].value = ast.Tuple(elts=[ast.Constant(path) for path in selected], ctx=ast.Load())
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def run(repo, output):
    validate_groups()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("native Linux AMD64 required; do not use emulation")
    base = base_runner()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], timeout=10)
    if source.strip():
        raise ValueError("clean source required")
    files = sorted({str(path.relative_to(repo)) for name in MOUNTS for path in
                    ([repo / name] if (repo / name).is_file() else (repo / name).rglob("*.py"))}
                   | {str(path.relative_to(repo)) for path in (repo / "ops/miniflux").iterdir()
                      if path.is_file() and path.suffix in {".json", ".patch"}}
                   | set(TESTS) | {"Dockerfile", "requirements.txt", "requirements-prod.txt", "requirements-ops.txt",
                                  "ops/isolation/Dockerfile.cutover-completion", ".github/workflows/cutover-completion.yml"})
    hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in files}
    base.save(output / "SOURCE.json", {"source_sha256": hashes, "production_access": False,
              "existing_126_modified": False, "model_calls": 0})
    tag = "ai-filter-isolation-cutover:" + uuid.uuid4().hex
    base.checked(base.DOCKER + ["build", "--platform=linux/amd64", "--tag", tag, "--file",
                 str(repo / "ops/isolation/Dockerfile.cutover-completion"), str(repo)], output / "build", 600)
    image = json.loads(base.checked(base.DOCKER + ["image", "inspect", tag], output / "test-image"))[0]
    base.save(output / "TEST_IMAGE.json", {key: image.get(key) for key in ("Id", "Architecture", "Os", "RepoTags")})
    base.READONLY_MOUNTS = MOUNTS
    programs = {name: native_test_program(name) for name, _selected in TEST_GROUPS}
    base.save(output / "TEST_GROUPS.json", {"groups": dict(TEST_GROUPS), "complete_suite_required": True,
        "per_group_seconds": 180, "memory": base.MEMORY, "pids": base.PIDS,
        "program_sha256": {name: hashlib.sha256(program.encode()).hexdigest() for name, program in programs.items()}})
    for name, _selected in TEST_GROUPS:
        base.run(repo, output / ("native-" + name), files=files, container_test=programs[name],
                 prepared_image=image["Id"], disk_fixtures=True)
    inspect_actual_runtime(base, repo, output / "actual-runtime", image["Id"])
    base.save(output / "GROUPS_RESULT.json", {"status": "all_fixed_groups_and_runtime_passed",
              "groups": [name for name, _selected in TEST_GROUPS], "production_access": False, "model_calls": 0})


def inspect_actual_runtime(base, repo, output, image_id):
    """Use default native subprocess transport, not injected Docker-shaped rows."""
    import sys
    sys.path.insert(0, str(repo / "scripts"))
    import handoff_runtime_adapter as runtime
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "data").mkdir(mode=0o700)
    name = "cutover-runtime-" + uuid.uuid4().hex[:16]
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=5).strip()
    created = False
    try:
        container_id = base.checked(base.DOCKER + ["create", "--name", name, "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=768m", "--memory-swap=768m",
            "--pids-limit=256", "--cpus=1", "--user", f"{os.getuid()}:{os.getgid()}",
            "--label", "org.opencontainers.image.revision=" + revision,
            "--label", "io.ai-filter.handoff.role=synthetic-frozen", "--mount",
            "type=bind,src=" + str(output / "data") + ",dst=/synthetic-data",
            image_id, "python", "-c", "import time; time.sleep(120)"], output / "create").strip()
        created = True
        base.checked(base.DOCKER + ["start", container_id], output / "start")
        transport = runtime.NativeDockerTransport({"fixture": {"url": "http://127.0.0.1:1", "token": "a" * 64}},
                                                 {}, docker=shutil.which("docker"))
        rows = transport.inspect((container_id,))
        row = rows[0]
        expected = {name: {"id": row["Id"], "image": row["Image"], "revision": revision,
                    "started_at": row["State"]["StartedAt"], "restart_count": 0,
                    "role": "synthetic-frozen", "user": f"{os.getuid()}:{os.getgid()}",
                    "memory": 768 * 1024**2, "pids": 256,
                    "mounts": [{"Type": "bind", "Source": str(output / "data"),
                                "Destination": "/synthetic-data", "RW": True}]}}
        observed = runtime.inspect_binding(rows, expected)
        bad = json.loads(json.dumps(expected))
        bad[name]["image"] = "sha256:" + "0" * 64
        refused = False
        try:
            runtime.inspect_binding(transport.inspect((container_id,)), bad)
        except Exception:
            refused = True
        if not refused:
            raise RuntimeError("actual runtime image drift was not refused")
        base.save(output / "RESULT.json", {"status": "passed_actual_docker_inspect", "observed": observed,
                  "wrong_image_refused": refused, "production_binding_verified": False,
                  "production_access": False, "model_calls": 0})
    finally:
        if created:
            # Only this runner's exact synthetic ID. Do not remove evidence/data.
            base.checked(base.DOCKER + ["stop", "--time=5", container_id], output / "stop")
            raw = base.checked(base.DOCKER + ["inspect", "--format", "{{json .State}}", container_id], output / "final-state")
            state = json.loads(raw)
            base.save(output / "STOP.json", state)
            if state["Running"] or state.get("OOMKilled"):
                raise RuntimeError("owned synthetic runtime stop not confirmed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    run(options.repo.resolve(), options.output.resolve())
