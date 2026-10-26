"""Run the offline handoff operation regressions in a native bounded container.

This runner has no production adapter. It uses a local Linux/AMD64 Docker
daemon, a pinned Python image, read-only test mounts and a private tmpfs.
All attempts, including failures, keep their original logs and inspect data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import uuid

IMAGE = "python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
MEMORY = 768 * 1024 * 1024
PIDS = 256
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
      'machine': platform.machine(), 'production_access': False, 'model_calls': 0}))
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
"""


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


def check_state(state, image_id):
    config, runtime = state.get("HostConfig", {}), state.get("State", {})
    checks = {
        "exact_image": state.get("Image") == image_id,
        "unprivileged_fixture_identity": state.get("Config", {}).get("User") == f"{os.getuid()}:{os.getgid()}",
        "memory": config.get("Memory") == MEMORY,
        "memory_swap": config.get("MemorySwap") == MEMORY,
        "pids": config.get("PidsLimit") == PIDS,
        "cpu": config.get("NanoCpus") == 1_000_000_000,
        "network_none": config.get("NetworkMode") == "none",
        "readonly_root": config.get("ReadonlyRootfs") is True,
        "cap_drop": "ALL" in (config.get("CapDrop") or []),
        "no_new_privileges": any(v in {"no-new-privileges", "no-new-privileges:true"}
                                 for v in (config.get("SecurityOpt") or [])),
        "readonly_test_mounts": len(state.get("Mounts", [])) == len(READONLY_MOUNTS) + 1 and all(
            mount.get("Type") == "bind" and mount.get("RW") is False
            and mount.get("Destination") in {"/work/" + name for name in READONLY_MOUNTS}
            for mount in state.get("Mounts", []) if mount.get("Destination") != "/evidence"
        ) and {m.get("Destination") for m in state.get("Mounts", [])} ==
            {"/work/" + name for name in READONLY_MOUNTS} | {"/evidence"},
        "owned_evidence_mount": len([m for m in state.get("Mounts", []) if
            m.get("Destination") == "/evidence" and m.get("Type") == "bind" and m.get("RW") is True]) == 1,
        "not_oom": runtime.get("OOMKilled") is False,
        "exited_zero": runtime.get("ExitCode") == 0 and runtime.get("Running") is False,
    }
    if not all(checks.values()):
        raise ValueError("isolation/resource/state mismatch: " + ",".join(k for k, v in checks.items() if not v))
    return checks


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


def run(repo, output, *, files=FILES, container_test=CONTAINER_TEST, prepared_image=None):
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
        args = DOCKER + ["create", "--name", name, "--label", "ai-filter.synthetic-operations=" + name,
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network=none", "--read-only", "--no-healthcheck", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--memory=768m", "--memory-swap=768m",
                "--pids-limit=256", "--cpus=1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=134217728",
                *[argument for name in READONLY_MOUNTS for argument in
                  ("--mount", "type=bind,src=" + str(repo / name) + ",dst=/work/" + name + ",readonly")],
                "--mount", "type=bind,src=" + str(output / "fixtures") + ",dst=/evidence",
                "--workdir=/work", "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint=python",
                "--env", "AI_FILTER_CUTOVER_EVIDENCE_DIR=/evidence",
                "--env", "AI_FILTER_HANDOFF_EVIDENCE_DIR=/evidence",
                image["Id"], "-B", "-u", "-c", container_test]
        checked(args, output / "create")
        created = True
        rc, stdout = command(DOCKER + ["start", "-a", name], output / "tests", 180)
        state = json.loads(checked(DOCKER + ["inspect", name], output / "container-inspect"))[0]
        checks = check_state(state, image["Id"])
        if rc != 0:
            raise RuntimeError("isolated operation tests failed")
        summary = json.loads(stdout.strip().splitlines()[-1])
        if summary.get("status") != "passed" or summary.get("skipped") != 0:
            raise ValueError("missing or incomplete operation test proof")
        save(output / "RESULT.json", {"status": "passed", "tests": summary, "checks": checks,
             "container": name, "production_adapter_verified": False,
             "model_calls": 0, "production_access": False, "existing_126_modified": False})
    except Exception as error:
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
