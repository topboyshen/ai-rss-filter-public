import fcntl
import hashlib
import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_security_master_sync.sh"
IMAGE_REF = "my-ai-filter:2026.07.22.1"
IMAGE_ID = "sha256:" + "a" * 64


@pytest.fixture
def runner_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    registry = tmp_path / "releases"
    manifests = registry / "manifests"
    manifests.mkdir(parents=True)

    manifest_source = b"""services:\n  security-master-sync:\n    profiles: [\"security-master\"]\n    image: ${IMAGE_REF:?}\n"""
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
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  [ "${FAKE_IMAGE_MISSING:-0}" != "1" ] || exit 1
  case "$*" in
    *"{{.Id}}"*) printf '%s\n' "$FAKE_IMAGE_ID" ;;
  esac
  exit 0
fi
if [ "$1" = "compose" ]; then
  case "$*" in
    *"--profile security-master config --services")
      [ "${FAKE_SERVICE_MISSING:-0}" = "1" ] || printf 'security-master-sync\n'
      exit 0
      ;;
    *"--profile security-master config --format json")
      if [ "${FAKE_CONFIG_INVALID:-0}" = "1" ]; then
        printf 'not-json\n'
      elif [ "${FAKE_SERVICE_MISSING:-0}" = "1" ]; then
        printf '{"services":{}}\n'
      else
        printf '{"services":{"security-master-sync":{"image":"%s"}}}\n' \
          "${FAKE_SERVICE_IMAGE:-$IMAGE_REF}"
      fi
      exit 0
      ;;
    *"--profile security-master run --pull never --rm --no-deps --entrypoint python security-master-sync workers/security_master_collector.py")
      exit "${FAKE_RUN_EXIT:-0}"
      ;;
    *"--profile security-master run --pull never --rm --no-deps --entrypoint python security-master-sync workers/security_master_collector.py --apply")
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
[ "$1" = "--signal=TERM" ]
shift
[ "$1" = "--kill-after=30s" ]
shift
[ "$1" = "900s" ]
shift
exec "$@"
""",
        encoding="utf-8",
    )
    timeout.chmod(0o755)

    flock = fake_bin / "flock"
    flock.write_text(
        """#!/usr/bin/env python3
import fcntl
import sys
import time

args = sys.argv[1:]
shared = False
nonblocking = False
wait_seconds = None
index = 0
while index < len(args) - 1:
    arg = args[index]
    if arg == "-s":
        shared = True
    elif arg == "-n":
        nonblocking = True
    elif arg == "-w":
        index += 1
        wait_seconds = float(args[index])
    else:
        raise SystemExit(64)
    index += 1
try:
    fd = int(args[-1])
except (IndexError, ValueError):
    raise SystemExit(64)

operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
deadline = None if wait_seconds is None else time.monotonic() + wait_seconds
while True:
    try:
        if nonblocking or wait_seconds is not None:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        else:
            fcntl.flock(fd, operation)
        raise SystemExit(0)
    except BlockingIOError:
        if nonblocking or (deadline is not None and time.monotonic() >= deadline):
            raise SystemExit(1)
        time.sleep(0.01)
""",
        encoding="utf-8",
    )
    flock.chmod(0o755)

    env = os.environ.copy()
    for name in (
        "AI_FILTER_MAINTENANCE_FILE",
        "IMAGE_REF",
        "SECURITY_MASTER_LOCK_FILE",
    ):
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_IMAGE_ID": IMAGE_ID,
            "RELEASE_REGISTRY_DIR": str(registry),
            "SECURITY_MASTER_RELEASE_LOCK_TIMEOUT_SECONDS": "1",
            "HEALTH_MAINTENANCE_FILE": str(tmp_path / "maintenance"),
        }
    )
    return env, registry, manifest, docker_log


def _run(env, *args, cwd=PROJECT_ROOT, runner=RUNNER):
    return subprocess.run(
        [str(runner), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def _docker_lines(docker_log):
    if not docker_log.exists():
        return []
    return docker_log.read_text(encoding="utf-8").splitlines()


def test_dry_run_uses_only_the_recorded_digest_and_archived_manifest(runner_env):
    env, _registry, manifest, docker_log = runner_env

    result = _run(env, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "mode=dry-run" in result.stdout
    assert f"release={IMAGE_REF}" in result.stdout
    assert f"digest={IMAGE_ID}" in result.stdout
    lines = _docker_lines(docker_log)
    assert len(lines) == 5
    compose_lines = [line for line in lines if "|compose " in line]
    assert len(compose_lines) == 3
    assert all(line.startswith(f"{IMAGE_ID}|") for line in compose_lines)
    assert all(f"-f {manifest} " in line for line in compose_lines)
    run_line = next(line for line in compose_lines if " run " in line)
    assert run_line.endswith(
        "--entrypoint python security-master-sync "
        "workers/security_master_collector.py"
    )
    assert " --apply" not in run_line


def test_runner_loads_shared_validator_from_any_cwd_and_can_repeat(runner_env):
    env, registry, _manifest, _docker_log = runner_env
    for _ in range(2):
        result = _run(env, "--dry-run", cwd=registry.parent)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("maintenance", [False, True])
def test_runner_missing_shared_validator_fails_before_locks_or_docker(
    runner_env, maintenance
):
    env, registry, _manifest, docker_log = runner_env
    isolated_scripts = registry.parent / "isolated" / "scripts"
    isolated_scripts.mkdir(parents=True)
    copied_runner = isolated_scripts / RUNNER.name
    copied_runner.write_bytes(RUNNER.read_bytes())
    copied_runner.chmod(0o755)
    if maintenance:
        Path(env["HEALTH_MAINTENANCE_FILE"]).touch()

    result = _run(env, "--dry-run", runner=copied_runner)

    assert result.returncode == (75 if maintenance else 78)
    assert ("maintenance active" if maintenance else "validate_lock_path.py") in result.stderr
    assert not (registry / "security-master-sync.lock").exists()
    assert not (registry / "release.lock").exists()
    assert not docker_log.exists()


def test_apply_is_the_only_mode_that_passes_apply_to_the_worker(runner_env):
    env, _registry, _manifest, docker_log = runner_env

    result = _run(env, "--apply")

    assert result.returncode == 0, result.stderr
    assert "mode=apply" in result.stdout
    run_line = next(line for line in _docker_lines(docker_log) if " run " in line)
    assert run_line.endswith(
        "--entrypoint python security-master-sync "
        "workers/security_master_collector.py --apply"
    )


@pytest.mark.parametrize(
    "args",
    [(), ("--unknown",), ("--dry-run", "--apply"), ("--apply", "extra")],
)
def test_runner_rejects_all_other_argument_shapes_before_docker(runner_env, args):
    env, _registry, _manifest, docker_log = runner_env

    result = _run(env, *args)

    assert result.returncode == 1
    assert "Usage:" in result.stderr
    assert not docker_log.exists()


@pytest.mark.parametrize("broken_symlink", [False, True])
def test_maintenance_refuses_before_state_locks_or_docker(
    runner_env, broken_symlink
):
    env, registry, _manifest, docker_log = runner_env
    sentinel = Path(env["HEALTH_MAINTENANCE_FILE"])
    if broken_symlink:
        sentinel.symlink_to("missing-target")
    else:
        sentinel.write_text("maintenance\n", encoding="utf-8")
    (registry / "current.state").write_text("invalid\n", encoding="utf-8")

    result = _run(env, "--apply")

    assert result.returncode == 75
    assert "maintenance active" in result.stderr
    assert not (registry / "security-master-sync.lock").exists()
    assert not docker_log.exists()


@pytest.mark.parametrize(
    "state",
    [
        "truncated\nstate\n",
        f"{IMAGE_REF}\n{IMAGE_ID}\n{'b' * 64}\nredis\nextra\n",
        f"{IMAGE_REF}\n{IMAGE_ID}\n{'b' * 64}\n\n",
        f"my-ai-filter:latest\n{IMAGE_ID}\n{'b' * 64}\nredis\n",
        f"my-ai-filter:main\n{IMAGE_ID}\n{'b' * 64}\nredis\n",
        f"{IMAGE_REF}\nsha256:not-a-digest\n{'b' * 64}\nredis\n",
        f"{IMAGE_REF}\n{IMAGE_ID}\nNOT-A-HASH\nredis\n",
        f"{IMAGE_REF}\r\n{IMAGE_ID}\n{'b' * 64}\nredis\n",
    ],
)
def test_runner_fails_closed_on_invalid_release_state(runner_env, state):
    env, registry, _manifest, docker_log = runner_env
    (registry / "current.state").write_text(state, encoding="utf-8")

    result = _run(env, "--dry-run")

    assert result.returncode != 0
    assert not docker_log.exists()


def test_runner_rejects_a_symlink_release_state(runner_env):
    env, registry, _manifest, docker_log = runner_env
    state = registry / "current.state"
    target = registry / "state-target"
    target.write_bytes(state.read_bytes())
    state.unlink()
    state.symlink_to(target)

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "missing or unsafe" in result.stderr
    assert not docker_log.exists()


def test_runner_fails_closed_on_a_tampered_manifest(runner_env):
    env, _registry, manifest, docker_log = runner_env
    manifest.write_text(manifest.read_text() + "# tampered\n", encoding="utf-8")

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "manifest hash mismatch" in result.stderr
    assert not docker_log.exists()


def test_runner_rejects_a_symlink_archived_manifest(runner_env):
    env, registry, manifest, docker_log = runner_env
    target = registry / "manifest-target"
    target.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(target)

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "missing or unsafe" in result.stderr
    assert not docker_log.exists()


def test_runner_fails_closed_on_image_digest_mismatch(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_IMAGE_ID"] = "sha256:" + "b" * 64

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "image digest mismatch" in result.stderr
    assert not any("|compose " in line for line in _docker_lines(docker_log))


def test_runner_fails_when_recorded_image_is_missing(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_IMAGE_MISSING"] = "1"

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "not available locally" in result.stderr
    assert not any("|compose " in line for line in _docker_lines(docker_log))


def test_old_manifest_without_sync_service_fails_closed(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_SERVICE_MISSING"] = "1"

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "does not define security-master-sync" in result.stderr
    assert not any(" run " in line for line in _docker_lines(docker_log))


def test_runner_rejects_a_service_image_other_than_recorded_digest(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_SERVICE_IMAGE"] = "sha256:" + "b" * 64

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "does not resolve to the recorded" in result.stderr
    assert not any(" run " in line for line in _docker_lines(docker_log))


def test_runner_rejects_an_unparseable_rendered_manifest(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_CONFIG_INVALID"] = "1"

    result = _run(env, "--dry-run")

    assert result.returncode == 3
    assert "Unable to verify" in result.stderr
    assert not any(" run " in line for line in _docker_lines(docker_log))


def test_runner_rejects_overlapping_sync(runner_env):
    env, registry, _manifest, docker_log = runner_env
    lock_path = registry / "security-master-sync.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _run(env, "--dry-run")

    assert result.returncode == 7
    assert "already running" in result.stderr
    assert not docker_log.exists()


def test_runner_times_out_behind_release_lock(runner_env):
    env, registry, _manifest, docker_log = runner_env
    env["SECURITY_MASTER_RELEASE_LOCK_TIMEOUT_SECONDS"] = "0"
    release_lock = registry / "release.lock"
    with release_lock.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _run(env, "--dry-run")

    assert result.returncode == 6
    assert "release lock" in result.stderr
    assert not docker_log.exists()


@pytest.mark.parametrize(
    ("lock_name", "message"),
    [
        ("security-master-sync.lock", "regular non-symlink"),
        ("release.lock", "regular non-symlink"),
    ],
)
def test_runner_rejects_symlink_locks_without_touching_targets(
    runner_env, lock_name, message
):
    env, registry, _manifest, docker_log = runner_env
    target = registry / f"{lock_name}.target"
    target.write_text("unchanged\n", encoding="utf-8")
    (registry / lock_name).symlink_to(target)

    result = _run(env, "--dry-run")

    assert result.returncode == 78
    assert message in result.stderr
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not docker_log.exists()


def test_runner_propagates_worker_failure(runner_env):
    env, _registry, _manifest, _docker_log = runner_env
    env["FAKE_RUN_EXIT"] = "42"

    result = _run(env, "--apply")

    assert result.returncode == 42


@pytest.mark.parametrize("value", ["", "-1", "1.5", "invalid"])
def test_runner_rejects_invalid_release_lock_timeout(runner_env, value):
    env, _registry, _manifest, docker_log = runner_env
    env["SECURITY_MASTER_RELEASE_LOCK_TIMEOUT_SECONDS"] = value

    result = _run(env, "--dry-run")

    assert result.returncode == 2
    assert "non-negative integer" in result.stderr
    assert not docker_log.exists()
