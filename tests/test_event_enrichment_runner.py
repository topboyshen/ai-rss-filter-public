import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "cron_event_enrichment.sh"
IMAGE_REF = "my-ai-filter:2026.08.04.3"
IMAGE_ID = "sha256:" + "a" * 64


@pytest.fixture
def runner_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    app_root = tmp_path / "app"
    (app_root / "data").mkdir(parents=True)
    docker_log = tmp_path / "docker.log"
    registry = tmp_path / "releases"
    manifests = registry / "manifests"
    manifests.mkdir(parents=True)
    manifest_source = b"services:\n  event-enrichment:\n    profiles: [\"cron\"]\n"
    manifest_hash = hashlib.sha256(manifest_source).hexdigest()
    manifest = manifests / f"{manifest_hash}.compose.yml"
    manifest.write_bytes(manifest_source)
    (registry / "current.state").write_text(
        f"{IMAGE_REF}\n{IMAGE_ID}\n{manifest_hash}\nredis ai-filter-api\n",
        encoding="utf-8",
    )

    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf '%s|%s\n' "${IMAGE_REF:-}" "$*" >> "$FAKE_DOCKER_LOG"
if [ "$1" = image ] && [ "$2" = inspect ]; then
  case "$*" in *"{{.Id}}"*) printf '%s\n' "$FAKE_IMAGE_ID";; esac
  exit 0
fi
if [ "$1" = compose ]; then
  case "$*" in
    *"--profile cron config --services") printf 'event-enrichment\n'; exit 0;;
    *"--profile cron run --pull never --rm --no-deps event-enrichment"*)
      if [ -n "${FAKE_RUN_STARTED:-}" ]; then
        printf 'started\n' > "$FAKE_RUN_STARTED"
      fi
      if [ -n "${FAKE_RUN_RELEASE:-}" ]; then
        while [ ! -e "$FAKE_RUN_RELEASE" ]; do sleep 0.01; done
      fi
      printf '%s\n' "$FAKE_RESULT"
      exit "${FAKE_RUN_EXIT:-0}"
      ;;
  esac
fi
exit 90
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    timeout = fake_bin / "timeout"
    timeout.write_text(
        """#!/bin/sh
set -eu
[ "$1" = --signal=TERM ]; shift
[ "$1" = --kill-after=60s ]; shift
case "$1" in *s) ;; *) exit 91;; esac
shift
exec "$@"
""",
        encoding="utf-8",
    )
    timeout.chmod(0o755)
    # Keep the shell test portable on development hosts without util-linux
    # flock while preserving real advisory-lock behavior on inherited FDs.
    flock = fake_bin / "flock"
    flock.write_text(
        """#!/usr/bin/env python3
import fcntl
import sys

args = sys.argv[1:]
operation = fcntl.LOCK_SH if "-s" in args else fcntl.LOCK_EX
if "-n" in args:
    operation |= fcntl.LOCK_NB
try:
    fcntl.flock(int(args[-1]), operation)
except BlockingIOError:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    flock.chmod(0o755)

    state = tmp_path / "state" / "event-enrichment.json"
    result = {
        "status": "ok",
        "mappings": {"updated": 1},
        "graph": {"merged": 0},
    }
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AI_FILTER_ROOT": str(app_root),
            "RELEASE_REGISTRY_DIR": str(registry),
            "AI_FILTER_ENRICHMENT_STATE_FILE": str(state),
            "HEALTH_MAINTENANCE_FILE": str(tmp_path / "maintenance"),
            "ENRICHMENT_RELEASE_LOCK_TIMEOUT_SECONDS": "1",
            "ENRICHMENT_RUN_TIMEOUT_SECONDS": "30",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_IMAGE_ID": IMAGE_ID,
            "FAKE_RESULT": json.dumps(result),
        }
    )
    return env, state, manifest, docker_log


def _run(env):
    return subprocess.run(
        ["bash", str(RUNNER)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def _wait_for_path(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _start_lock_holder(lock_path: Path, ready: Path, release: Path):
    script = """
import fcntl
import pathlib
import sys
import time

lock_path, ready_path, release_path = map(pathlib.Path, sys.argv[1:])
with lock_path.open("a+", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    ready_path.write_text("ready\\n", encoding="utf-8")
    while not release_path.exists():
        time.sleep(0.01)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path), str(ready), str(release)],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_path(ready)
    return process


def _run_refresh_probe(lock_path: Path):
    script = """
import os
from services import refresh

refresh.try_acquire_refresh_lock = lambda: True
refresh.set_refresh_running = lambda _running: None
refresh._execute_feed_refresh = lambda: {"status": "noop"}
print(refresh.run_refresh_command())
"""
    env = os.environ.copy()
    env["AI_FILTER_REFRESH_LOCK"] = str(lock_path)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_runner_uses_recorded_digest_manifest_and_writes_heartbeat(runner_env):
    env, state, manifest, docker_log = runner_env

    result = _run(env)

    assert result.returncode == 0, result.stderr
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert "background_status" not in payload
    assert state.stat().st_mode & 0o777 == 0o600
    lines = docker_log.read_text(encoding="utf-8").splitlines()
    assert all(line.startswith(f"{IMAGE_ID}|") for line in lines[2:])
    assert any(f"-f {manifest} --profile cron config --services" in line for line in lines)
    assert any(
        "event-enrichment python scripts/run_event_enrichment.py --days 7 "
        "--limit 5000 --max-pairs 10000" in line
        for line in lines
    )


def _record_forced_override(env, manifest):
    registry = Path(env["RELEASE_REGISTRY_DIR"])
    override = registry / "forced-runtime.compose.yml"
    override.write_text(
        'services:\n  event-enrichment:\n    environment:\n'
        '      AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE: "0"\n',
        encoding="utf-8",
    )
    digest = hashlib.sha256(override.read_bytes()).hexdigest()
    (registry / "current.override").write_text(str(override) + "\n")
    (registry / "current.override-sha256").write_text(digest + "\n")
    fields = {
        "status": "forced_gate_bypass_healthy", "ref": IMAGE_REF,
        "digest": IMAGE_ID,
        "base_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "override_path": str(override), "override_sha256": digest,
        "migration_gate_enforced": "false",
        "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE": "0",
    }
    receipt = registry / "forced-runtime.receipt"
    receipt.write_text("".join(f"{key}={value}\n" for key, value in fields.items()))
    return override, receipt


def test_runner_uses_active_override_for_config_and_execution(runner_env):
    env, state, manifest, docker_log = runner_env
    override, _ = _record_forced_override(env, manifest)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text())["status"] == "ok"
    compose_lines = [line for line in docker_log.read_text().splitlines() if "|compose " in line]
    assert len(compose_lines) == 2
    assert all(f"-f {manifest} -f {override} --profile cron" in line for line in compose_lines)


@pytest.mark.parametrize("invalid", ["changed_bytes", "different_release", "missing_receipt"])
def test_runner_rejects_unbound_override_before_starting_job(runner_env, invalid):
    env, state, manifest, docker_log = runner_env
    override, receipt = _record_forced_override(env, manifest)
    if invalid == "changed_bytes":
        override.write_text(override.read_text() + "# changed\n")
    elif invalid == "different_release":
        receipt.write_text(receipt.read_text().replace(IMAGE_REF, "my-ai-filter:2099.01.01.1"))
    else:
        receipt.unlink()

    result = _run(env)

    assert result.returncode != 0
    assert "override invalid" in result.stderr
    assert json.loads(state.read_text())["status"] == "failed"
    assert "--profile cron run" not in docker_log.read_text()


def test_runner_persists_mapping_replay_budget_and_failure_counters(runner_env):
    env, state, _manifest, _docker_log = runner_env
    payload = json.loads(env["FAKE_RESULT"])
    payload["mappings"].update(
        deferred_groups=3,
        unreplayable_groups=1,
        exhausted_groups=2,
        failed_evidence=4,
    )
    env["FAKE_RESULT"] = json.dumps(payload)

    result = _run(env)

    assert result.returncode == 0
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["mapping_deferred_groups"] == 3
    assert saved["mapping_unreplayable_groups"] == 1
    assert saved["mapping_exhausted_groups"] == 2
    assert saved["mapping_failed_evidence"] == 4


def test_runner_persists_real_exit_one_core_degradation_payload(runner_env):
    env, state, _manifest, _docker_log = runner_env
    payload = json.loads(env["FAKE_RESULT"])
    payload.update(status="degraded")
    payload["mappings"].update(
        unreplayable_groups=1,
        exhausted_groups=2,
        failed_evidence=4,
    )
    env["FAKE_RESULT"] = json.dumps(payload)
    env["FAKE_RUN_EXIT"] = "1"

    result = _run(env)

    assert result.returncode == 1
    assert "exit=1" in result.stdout
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "degraded"
    assert saved["exit_code"] == 1
    assert saved["mapping_unreplayable_groups"] == 1
    assert saved["mapping_exhausted_groups"] == 2
    assert saved["mapping_failed_evidence"] == 4


def test_runner_propagates_real_container_exit_code(runner_env):
    env, state, _manifest, _docker_log = runner_env
    env["FAKE_RUN_EXIT"] = "42"

    result = _run(env)

    assert result.returncode == 42
    assert "exit=42" in result.stdout
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["exit_code"] == 42


def test_runner_fails_closed_on_tampered_manifest(runner_env):
    env, state, manifest, _docker_log = runner_env
    manifest.write_text(manifest.read_text(encoding="utf-8") + "# drift\n")

    result = _run(env)

    assert result.returncode != 0
    assert "manifest hash mismatch" in result.stdout
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "failed"


def test_refresh_owner_defers_enrichment_without_overwriting_last_success(runner_env):
    env, state, _manifest, docker_log = runner_env
    pipeline_lock = Path(env["AI_FILTER_ROOT"]) / "data" / ".refresh-execution.lock"

    first = _run(env)
    assert first.returncode == 0
    last_success = state.read_bytes()
    docker_before = docker_log.read_bytes()

    ready = pipeline_lock.parent / "refresh-holder.ready"
    release = pipeline_lock.parent / "refresh-holder.release"
    holder = _start_lock_holder(pipeline_lock, ready, release)
    try:
        deferred = _run(env)

        assert deferred.returncode == 0
        assert (
            "status=deferred reason=refresh_execution_lock_busy "
            "retry=next_schedule"
        ) in deferred.stdout
        assert state.read_bytes() == last_success
        assert docker_log.read_bytes() == docker_before
    finally:
        release.touch()
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        assert holder.returncode == 0, (holder_stdout, holder_stderr)

    retried = _run(env)
    assert retried.returncode == 0, retried.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "ok"
    assert len(docker_log.read_text(encoding="utf-8").splitlines()) > len(
        docker_before.splitlines()
    )


def test_enrichment_owner_makes_refresh_busy_then_releases_for_retry(runner_env):
    env, state, _manifest, _docker_log = runner_env
    pipeline_lock = Path(env["AI_FILTER_ROOT"]) / "data" / ".refresh-execution.lock"
    started = pipeline_lock.parent / "enrichment.started"
    release = pipeline_lock.parent / "enrichment.release"
    env["FAKE_RUN_STARTED"] = str(started)
    env["FAKE_RUN_RELEASE"] = str(release)

    enrichment = subprocess.Popen(
        ["bash", str(RUNNER)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_path(started)

        blocked = _run_refresh_probe(pipeline_lock)
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip().splitlines()[-1] == "refresh_busy"
    finally:
        release.touch()
        enrichment_stdout, enrichment_stderr = enrichment.communicate(timeout=5)

    assert enrichment.returncode == 0, (enrichment_stdout, enrichment_stderr)
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "ok"

    retried = _run_refresh_probe(pipeline_lock)
    assert retried.returncode == 0, retried.stderr
    assert retried.stdout.strip().splitlines()[-1] == "True"


def test_refresh_execution_symlink_is_rejected_before_docker(runner_env, tmp_path):
    env, state, _manifest, docker_log = runner_env
    pipeline_lock = Path(env["AI_FILTER_ROOT"]) / "data" / ".refresh-execution.lock"
    target = tmp_path / "unrelated-target"
    target.write_text("unchanged\n", encoding="utf-8")
    pipeline_lock.symlink_to(target)

    result = _run(env)

    assert result.returncode == 78
    assert "regular non-symlink" in result.stderr
    assert "refusing unsafe refresh execution lock path" in result.stdout
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not state.exists()
    assert not docker_log.exists()


def test_existing_worker_created_readable_lock_is_accepted(runner_env):
    env, state, _manifest, _docker_log = runner_env
    pipeline_lock = Path(env["AI_FILTER_ROOT"]) / "data" / ".refresh-execution.lock"
    pipeline_lock.touch(mode=0o644)
    pipeline_lock.chmod(0o644)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["status"] == "ok"


def test_group_writable_refresh_execution_lock_is_rejected(runner_env):
    env, state, _manifest, docker_log = runner_env
    pipeline_lock = Path(env["AI_FILTER_ROOT"]) / "data" / ".refresh-execution.lock"
    pipeline_lock.touch()
    pipeline_lock.chmod(0o620)

    result = _run(env)

    assert result.returncode == 78
    assert "not group/world-writable" in result.stderr
    assert not state.exists()
    assert not docker_log.exists()
