"""Run the offline handoff operation regressions in a native bounded container.

This runner has no production adapter. It uses a local Linux/AMD64 Docker
daemon, a pinned Python image, read-only test mounts and a private tmpfs.
All attempts, including failures, keep their original logs and inspect data.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import subprocess
import uuid

IMAGE = "python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
MEMORY = 768 * 1024 * 1024
PIDS = 256
DISK_FIXTURE_TARGET = "/tmp/cutover-fixtures"
TMPFS = {"/tmp": "rw,nosuid,nodev,size=134217728"}
READONLY_MOUNTS = ("scripts", "tests", "services", "routes", "app.py", "docker-compose.yml", "worker_main.py",
                   ".github/workflows/handoff-operations.yml")
FILES = (
    ".github/workflows/handoff-operations.yml",
    "scripts/verify_handoff_operations_native.py",
    "services/webhook_receipts.py",
    "scripts/plan_webhook_receipt_trial.py",
    "scripts/durable_handoff_cutover.py",
    "scripts/durable_handoff_cutover_fixture.py",
    "tests/test_webhook_receipt_trial_plan.py",
    "tests/test_durable_handoff_cutover.py",
    "tests/test_handoff_operations_native.py",
    "scripts/production_handoff_adapter.py",
    "scripts/observe_handoff_capacity.py",
    "tests/test_production_handoff_adapter.py",
    "tests/test_handoff_capacity_observer.py",
    "services/admission_fence.py",
    "services/receiver_handoff_control.py",
    "scripts/production_receiver_driver.py",
    "scripts/verify_production_receiver_native.py",
    "scripts/handoff_runtime_adapter.py",
    "scripts/handoff_host_cron.py",
    "tests/test_production_receiver_driver.py",
    "app.py",
    "docker-compose.yml",
    "worker_main.py",
    "routes/feed_routes.py",
    "routes/admin_routes.py",
    "routes/dashboard_routes.py",
    "routes/webhook_routes.py",
    "services/refresh.py",
    "scripts/release.sh",
    "scripts/cron_refresh.sh",
    "scripts/cron_event_enrichment.sh",
)
CONTAINER_TEST = """
import json, pathlib, platform, sys, unittest
from scripts.verify_handoff_operations_native import test_failure_coordinates
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
suite = unittest.TestSuite()
for pattern in ('test_webhook_receipt_trial_plan.py', 'test_durable_handoff_cutover.py',
                'test_handoff_operations_native.py', 'test_production_handoff_adapter.py',
                'test_handoff_capacity_observer.py', 'test_production_receiver_driver.py'):
    suite.addTests(unittest.defaultTestLoader.discover('/work/tests', pattern=pattern))
assert suite.countTestCases() > 0
result = unittest.TextTestRunner(verbosity=2).run(suite)
peak = pathlib.Path('/sys/fs/cgroup/memory.peak')
print(json.dumps({'status': 'passed' if result.wasSuccessful() else 'failed',
      'tests_run': result.testsRun, 'failures': len(result.failures),
      'errors': len(result.errors), 'skipped': len(result.skipped),
      'memory_peak_bytes': int(peak.read_text()) if peak.exists() else None,
      'machine': platform.machine(), 'production_access': False, 'model_calls': 0,
      'failure_coordinates': test_failure_coordinates(result)}))
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
"""


def test_failure_coordinates(result):
    """Coordinates only; exception messages and subtest parameters stay private."""
    rows = []
    for _test, trace in (result.failures[:8] + result.errors[:8]):
        if type(trace) is not str or len(trace) > 65536:
            continue
        for match in re.finditer(r'  File "/work/((?:scripts|services|tests)/[A-Za-z_][A-Za-z0-9_]{0,100}\.py)", line ([1-9][0-9]{0,5}), in ', trace):
            rows.append({"file": match[1], "line": int(match[2])})
    return rows[-8:]


def _test_location(row, repo, files):
    if type(row) is not dict or set(row) != {"file", "line"}:
        return None
    name, line = row["file"], row["line"]
    if (type(name) is not str or name not in files or not re.fullmatch(
            r"(?:scripts|services|tests)/[A-Za-z_][A-Za-z0-9_]{0,100}\.py", name)
            or type(line) is not int or not 1 <= line <= 100000):
        return None
    path = repo / name
    if path.resolve() != path:
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 256 * 1024:
            return None
        raw = os.read(fd, 256 * 1024 + 1)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(raw) != before.st_size or any(getattr(after, k) != getattr(before, k) for k in fields):
            return None
        nodes = list(ast.walk(ast.parse(raw)))
    finally:
        os.close(fd)
    if line > max((getattr(node, "end_lineno", 0) or 0 for node in nodes), default=0):
        return None
    scopes = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and n.lineno <= line <= n.end_lineno]
    scope = min(scopes, key=lambda n: n.end_lineno - n.lineno).name if scopes else "<module>"
    return {"file": name, "line": line, "function": scope}


def _test_summary(stdout, repo, files):
    if type(stdout) is not str or len(stdout) > 512 * 1024:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate summary key")
            result[key] = value
        return result
    value = json.loads(stdout.strip().splitlines()[-1], object_pairs_hook=unique)
    if type(value) is not dict or value.get("status") not in {"passed", "failed"}:
        return None
    counts = {key: value.get(key) for key in ("tests_run", "failures", "errors", "skipped")}
    if any(type(v) is not int or not 0 <= v <= 100000 for v in counts.values()):
        return None
    rows = value.get("failure_coordinates", [])
    if type(rows) is not list or len(rows) > 8:
        return None
    locations = [_test_location(row, repo, files) for row in rows]
    return {"status": value["status"], **counts, "locations": [r for r in locations if r is not None]}


def emit_failure_diagnostic(stdout, state, rc, repo, files):
    """Optional bounded job-log projection; never makes a test or gate pass."""
    try:
        runtime = state.get("State", {}) if type(state) is dict else {}
        code = runtime.get("ExitCode")
        value = {"schema": "native-test-failure-v1", "execution_proof": False,
                 "full_raw_archive_verified": False,
                 "client_returncode": rc if type(rc) is int and -255 <= rc <= 255 else None,
                 "exit_code": code if type(code) is int and -255 <= code <= 255 else None,
                 "running": runtime.get("Running") if type(runtime.get("Running")) is bool else None,
                 "oom_killed": runtime.get("OOMKilled") if type(runtime.get("OOMKilled")) is bool else None}
        try:
            value["tests"] = _test_summary(stdout, repo, files)
        except BaseException:
            value["tests"] = None
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        if len(raw.encode()) <= 4096:
            sys.stderr.write(raw)
            sys.stderr.flush()
    except BaseException:
        pass  # Keep the primary error even if diagnostics cannot be produced.


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def command(argv, output, timeout=60):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    with (output / "stdout.log").open("x") as stdout, (output / "stderr.log").open("x") as stderr:
        try:
            result = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=timeout, check=False)
            rc, error = result.returncode, None
        except subprocess.TimeoutExpired:
            rc, error = None, "client_timeout"
    save(output / "COMMAND.json", {"argv": argv, "exit_code": rc, "error": error,
         "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat()})
    return rc, (output / "stdout.log").read_text()


def checked(argv, output, timeout=60):
    rc, raw = command(argv, output, timeout)
    if rc != 0:
        raise RuntimeError("isolated command failed; original evidence: " + output.name)
    return raw


def native_host(info):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("native Linux AMD64 host required")
    if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
        raise ValueError("native Linux AMD64 Docker daemon required")
    for path in Path("/proc/sys/fs/binfmt_misc").glob("*"):
        if any(word in path.name.lower() for word in ("x86_64", "amd64", "rosetta")):
            if "enabled" in path.read_text(errors="replace"):
                raise ValueError("AMD64 emulator is not allowed")


def check_state(state, image_id, *, disk_fixtures=False, evidence_root=None):
    if type(disk_fixtures) is not bool:
        raise ValueError("explicit disk fixture mode required")
    writable = {"/evidence", DISK_FIXTURE_TARGET} if disk_fixtures else {"/evidence"}
    config, runtime = state.get("HostConfig", {}), state.get("State", {})
    checks = {
        "exact_image": state.get("Image") == image_id,
        "unprivileged_fixture_identity": state.get("Config", {}).get("User") == f"{os.getuid()}:{os.getgid()}",
        "memory": config.get("Memory") == MEMORY,
        "memory_swap": config.get("MemorySwap") == MEMORY,
        "pids": config.get("PidsLimit") == PIDS,
        "cpu": config.get("NanoCpus") == 1_000_000_000,
        "tmpfs": config.get("Tmpfs") == TMPFS,
        "network_none": config.get("NetworkMode") == "none",
        "readonly_root": config.get("ReadonlyRootfs") is True,
        "cap_drop": "ALL" in (config.get("CapDrop") or []),
        "no_new_privileges": any(v in {"no-new-privileges", "no-new-privileges:true"}
                                 for v in (config.get("SecurityOpt") or [])),
        "readonly_test_mounts": len(state.get("Mounts", [])) == len(READONLY_MOUNTS) + len(writable) and all(
            mount.get("Type") == "bind" and mount.get("RW") is False
            and mount.get("Destination") in {"/work/" + name for name in READONLY_MOUNTS}
            for mount in state.get("Mounts", []) if mount.get("Destination") not in writable
        ) and {m.get("Destination") for m in state.get("Mounts", [])} ==
            {"/work/" + name for name in READONLY_MOUNTS} | writable,
        "owned_evidence_mount": len([m for m in state.get("Mounts", []) if
            m.get("Destination") == "/evidence" and m.get("Type") == "bind" and m.get("RW") is True]) == 1,
        "not_oom": runtime.get("OOMKilled") is False,
        "exited_zero": runtime.get("ExitCode") == 0 and runtime.get("Running") is False,
    }
    if disk_fixtures:
        checks["owned_disk_fixture_mount"] = _check_disk_fixture_mount(state, evidence_root)
    if not all(checks.values()):
        raise ValueError("isolation/resource/state mismatch: " + ",".join(k for k, v in checks.items() if not v))
    return checks



def _check_disk_fixture_mount(state, evidence_root):
    if evidence_root is None:
        return False
    root = Path(evidence_root)
    if not root.is_absolute() or root.resolve() != root:
        return False
    actual = {m.get("Destination"): m for m in state.get("Mounts", [])
              if m.get("Destination") in {"/evidence", DISK_FIXTURE_TARGET}}
    expected = {"/evidence": root, DISK_FIXTURE_TARGET: root / "disk-fixtures"}
    return len(actual) == 2 and all(actual[target].get("Type") == "bind"
        and actual[target].get("RW") is True and actual[target].get("Source") == str(source)
        for target, source in expected.items())


def disk_fixture_arguments(evidence_root, enabled):
    if type(enabled) is not bool:
        raise ValueError("explicit disk fixture mode required")
    if not enabled:
        return []
    root = Path(evidence_root)
    info = root.lstat()
    if not root.is_absolute() or root.resolve() != root or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
        raise ValueError("owned private evidence directory required")
    child = root / "disk-fixtures"
    child.mkdir(mode=0o700, exist_ok=False)
    return ["--mount", "type=bind,src=" + str(child) + ",dst=" + DISK_FIXTURE_TARGET]


def preserve_failed_container(name, output):
    rc, raw = command(DOCKER + ["inspect", name], output / "failure-inspect", 15)
    if rc != 0:
        return
    state = json.loads(raw)[0]
    owned = (state.get("Config", {}).get("Labels") or {}).get("ai-filter.synthetic-operations") == name
    if owned and state.get("State", {}).get("Running") is True:
        command(DOCKER + ["stop", "--time=1", name], output / "stop-owned-container", 15)
        command(DOCKER + ["inspect", name], output / "after-stop-inspect", 15)
    command(DOCKER + ["logs", name], output / "failure-container-logs", 15)


def run(repo, output, *, files=FILES, container_test=CONTAINER_TEST, prepared_image=None, disk_fixtures=False):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("native Linux AMD64 required before Docker access")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=10).strip()
    if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], timeout=10).strip():
        raise ValueError("native operation test source must be a clean checkout")
    hashes = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in files}
    save(output / "BINDING.json", {"revision": revision, "source_sha256": hashes, "production_access": False,
         "models": 0, "existing_126_modified": False, "image": IMAGE})
    name = "handoff-operations-" + uuid.uuid4().hex[:16]
    created = False
    rc, stdout, state = None, None, None
    try:
        info = json.loads(checked(DOCKER + ["info", "--format", "{{json .}}"], output / "docker-info"))
        native_host(info)
        save(output / "HOST.json", {"system": platform.system(), "machine": platform.machine(),
             "daemon_architecture": info["Architecture"], "native_amd64": True})
        if prepared_image is None:
            checked(DOCKER + ["pull", "--platform=linux/amd64", IMAGE], output / "pull", 180)
        elif not __import__("re").fullmatch(r"sha256:[0-9a-f]{64}", prepared_image):
            raise ValueError("prepared test image requires exact local ID")
        image = json.loads(checked(DOCKER + ["image", "inspect", prepared_image or IMAGE], output / "image-inspect"))[0]
        if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
            raise ValueError("fixed test image platform mismatch")
        save(output / "IMAGE.json", {key: image.get(key) for key in ("Id", "RepoDigests", "Architecture", "Os")})
        (output / "fixtures").mkdir(mode=0o700)
        disk_arguments = disk_fixture_arguments(output / "fixtures", disk_fixtures)
        args = DOCKER + ["create", "--name", name, "--label", "ai-filter.synthetic-operations=" + name,
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network=none", "--read-only", "--no-healthcheck", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--memory=768m", "--memory-swap=768m",
                "--pids-limit=256", "--cpus=1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=134217728",
                *[argument for name in READONLY_MOUNTS for argument in
                  ("--mount", "type=bind,src=" + str(repo / name) + ",dst=/work/" + name + ",readonly")],
                "--mount", "type=bind,src=" + str(output / "fixtures") + ",dst=/evidence", *disk_arguments,
                "--workdir=/work", "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint=python",
                "--env", "AI_FILTER_CUTOVER_EVIDENCE_DIR=/evidence",
                "--env", "AI_FILTER_HANDOFF_EVIDENCE_DIR=/evidence",
                image["Id"], "-B", "-u", "-c", container_test]
        checked(args, output / "create")
        created = True
        rc, stdout = command(DOCKER + ["start", "-a", name], output / "tests", 180)
        state = json.loads(checked(DOCKER + ["inspect", name], output / "container-inspect"))[0]
        checks = check_state(state, image["Id"], disk_fixtures=disk_fixtures, evidence_root=output / "fixtures")
        if rc != 0:
            raise RuntimeError("isolated operation tests failed")
        summary = json.loads(stdout.strip().splitlines()[-1])
        if summary.get("status") != "passed" or summary.get("skipped") != 0:
            raise ValueError("missing or incomplete operation test proof")
        save(output / "RESULT.json", {"status": "passed", "tests": summary, "checks": checks,
             "container": name, "production_adapter_verified": False,
             "model_calls": 0, "production_access": False, "existing_126_modified": False})
    except Exception as error:
        emit_failure_diagnostic(stdout, state, rc, repo, files)
        save(output / "FAILURE.json", {"type": type(error).__name__, "message": str(error), "container": name})
        if created:
            preserve_failed_container(name, output)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    run(options.repo.resolve(), options.output.resolve())
