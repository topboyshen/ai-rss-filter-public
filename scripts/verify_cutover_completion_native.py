"""Native bounded real initializer/process/HTTP/files tests, never production.

Dependencies are built into a uniquely named disposable test image. No release
tag (including .126) is built, edited or retagged. Runtime has no external
network, Docker socket or production mount. Failures keep original artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import uuid


def base_runner():
    path = Path(__file__).with_name("verify_handoff_operations_native.py")
    spec = importlib.util.spec_from_file_location("completion_native_base", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


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
         "tests/test_receiver_drain_fixture_runtime.py", "tests/test_sender_receiver_joint_native.py")
MOUNTS = ("scripts", "tests", "services", "routes", "core", "db", "utils", "workers", "modules",
          "templates", "app.py", "worker_main.py", "sse_engine.py", "logging_bootstrap.py",
          "scoring_provenance.py", "scoring_explanation.py", "config.yaml", "docker-compose.yml",
          "ops/isolation", "ops/miniflux", ".github/workflows", "gunicorn.conf.py")
CONTAINER_TEST = r'''
import json, os, pathlib, platform, shutil, sys, tempfile, pytest
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
fixtures = pathlib.Path(tempfile.mkdtemp(prefix='cutover-synthetic-', dir='/tmp'))
os.environ['AI_FILTER_CUTOVER_EVIDENCE_DIR'] = str(fixtures)
os.environ['AI_FILTER_HANDOFF_EVIDENCE_DIR'] = str(fixtures)
class Summary:
    passed = failed = skipped = 0
    def pytest_runtest_logreport(self, report):
        if report.when == 'call' and report.passed: self.passed += 1
        if report.failed: self.failed += 1
        if report.skipped: self.skipped += 1
summary = Summary()
try:
    code = pytest.main(['-q', '-o', 'addopts=', '-p', 'no:cacheprovider', '--basetemp=' + str(fixtures / 'pytest'),
        'tests/test_independent_drain_inventory.py', 'tests/test_frozen_cutover.py',
        'tests/test_handoff_runtime_adapter.py', 'tests/test_controlled_startup.py',
        'tests/test_frozen_cutover_actual_startup.py', 'tests/test_miniflux_handoff_adapter.py',
        'tests/test_miniflux_host_joint_native.py', 'tests/test_durable_handoff_crash_diagnostics.py',
        'tests/test_durable_handoff_cutover.py', 'tests/test_production_migration.py',
        'tests/test_production_startup.py', 'tests/test_production_startup_native.py',
        'tests/test_production_sender_driver.py', 'tests/test_sender_drain_contract.py',
        'tests/test_sender_pg_deadlines.py', 'tests/test_production_startup_pg_fixture.py',
        'tests/test_receiver_drain_transfer.py', 'tests/test_receiver_drain_driver.py',
        'tests/test_takeover_readonly_preflight.py', 'tests/test_joint_drain_controller.py',
        'tests/test_receiver_drain_fixture_runtime.py', 'tests/test_sender_receiver_joint_native.py'], plugins=[summary])
finally:
    # Preserve only this process's own private synthetic fixtures on all exits.
    # The startup runner continues refusing non-temporary database paths.
    shutil.copytree(fixtures, '/evidence/fixtures-final', symlinks=True)
peak = pathlib.Path('/sys/fs/cgroup/memory.peak')
events = pathlib.Path('/sys/fs/cgroup/memory.events')
ok = code == 0 and summary.passed >= 25 and not summary.failed and not summary.skipped
print(json.dumps({'status':'passed' if ok else 'failed', 'tests_run':summary.passed,
    'failures':summary.failed,'errors':int(code not in (0,1)), 'skipped':summary.skipped,
    'memory_peak_bytes':int(peak.read_text()) if peak.exists() else None,
    'memory_events':events.read_text() if events.exists() else None,
    'machine':platform.machine(), 'production_access':False, 'model_calls':0,
    'scope':'real SQLite initializer, local processes/HTTP, private synthetic storage; not live cutover'}))
sys.exit(0 if ok else 1)
'''


def run(repo, output):
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
    base.run(repo, output / "native", files=files, container_test=CONTAINER_TEST, prepared_image=image["Id"])
    inspect_actual_runtime(base, repo, output / "actual-runtime", image["Id"])


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
