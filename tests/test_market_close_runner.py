import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_market_close.sh"
IMAGE_REF = "my-ai-filter:2026.07.13.2"
IMAGE_ID = "sha256:" + "a" * 64


@pytest.fixture
def runner_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    registry = tmp_path / "releases"
    manifests = registry / "manifests"
    manifests.mkdir(parents=True)

    manifest_source = b"""services:\n  market-close-collector:\n    profiles: [\"cron\"]\n    image: ${IMAGE_REF:?}\n"""
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
    *"--profile cron config --services")
      [ "${FAKE_SERVICE_MISSING:-0}" = "1" ] || printf 'market-close-collector\n'
      exit 0
      ;;
    *"--profile cron run --pull never --rm --no-deps -e MARKET_CLOSE_RUN_ID="*" market-close-collector")
      printf '%s\n' "${FAKE_RUN_OUTPUT:-}"
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
case "$1" in
  *s) ;;
  *) exit 91 ;;
esac
shift
exec "$@"
""",
        encoding="utf-8",
    )
    timeout.chmod(0o755)

    env = os.environ.copy()
    env.pop("AI_FILTER_MAINTENANCE_FILE", None)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_IMAGE_ID": IMAGE_ID,
            "RELEASE_REGISTRY_DIR": str(registry),
            "MARKET_CLOSE_RELEASE_LOCK_TIMEOUT_SECONDS": "1",
            "MARKET_CLOSE_RUN_TIMEOUT_SECONDS": "30",
            "HEALTH_MAINTENANCE_FILE": str(tmp_path / "maintenance"),
            "MARKET_CLOSE_RESULT_DIR": str(tmp_path / "market_close"),
        }
    )
    return env, registry, manifest, docker_log


def _run(env, *, cwd=PROJECT_ROOT, runner=RUNNER):
    return subprocess.run(
        [str(runner)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_runner_uses_atomic_current_release_state(runner_env):
    env, _registry, manifest, docker_log = runner_env

    result = _run(env)

    assert result.returncode == 0, result.stderr
    artifact = Path(env["MARKET_CLOSE_RESULT_DIR"]) / "collector_last.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["detail"] == "host-fallback"
    assert len(payload["run_id"]) == 32
    assert f"release={IMAGE_REF}" in result.stdout
    assert f"digest={IMAGE_ID}" in result.stdout
    lines = docker_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert all(line.startswith(f"{IMAGE_ID}|") for line in lines[2:])
    assert not any(
        line.startswith(f"{IMAGE_REF}|") and "|compose " in line for line in lines
    )
    assert any(f"-f {manifest} --profile cron config --services" in line for line in lines)
    assert any(
        f"-f {manifest} --profile cron run --pull never --rm --no-deps "
        f"-e MARKET_CLOSE_RUN_ID={payload['run_id']} market-close-collector"
        in line
        for line in lines
    )


def test_runner_loads_shared_validator_from_any_cwd_and_can_repeat(runner_env):
    env, registry, _manifest, _docker_log = runner_env
    for _ in range(2):
        result = _run(env, cwd=registry.parent)
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

    result = _run(env, runner=copied_runner)

    assert result.returncode == (75 if maintenance else 78)
    assert ("maintenance active" if maintenance else "validate_lock_path.py") in result.stderr
    assert not (registry / "market-close.lock").exists()
    assert not (registry / "release.lock").exists()
    assert not docker_log.exists()


def test_runner_new_failure_replaces_previous_recent_receipt(runner_env):
    env, _registry, _manifest, _docker_log = runner_env
    artifact = Path(env["MARKET_CLOSE_RESULT_DIR"]) / "collector_last.json"
    artifact.parent.mkdir()
    artifact.write_text(json.dumps({
        "version": 1, "kind": "collector", "status": "success", "run_id": "a" * 32,
    }))
    env["FAKE_RUN_EXIT"] = "1"
    result = _run(env)
    assert result.returncode == 1
    payload = json.loads(artifact.read_text())
    assert payload["status"] == "error"
    assert payload["run_id"] != "a" * 32


def test_runner_nontrading_skip_restores_prior_receipt(runner_env):
    env, _registry, _manifest, _docker_log = runner_env
    artifact = Path(env["MARKET_CLOSE_RESULT_DIR"]) / "collector_last.json"
    artifact.parent.mkdir()
    previous = {"version": 1, "kind": "collector", "status": "success",
                "run_id": "a" * 32, "trade_date": "2026-09-11"}
    artifact.write_text(json.dumps(previous))
    env["FAKE_RUN_OUTPUT"] = "[MarketClose] 非交易日，跳过采集"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert json.loads(artifact.read_text()) == previous


def test_runner_skips_all_docker_work_during_global_maintenance(runner_env):
    env, registry, _manifest, docker_log = runner_env
    sentinel = Path(env["HEALTH_MAINTENANCE_FILE"])
    sentinel.write_text("event-core maintenance\n", encoding="utf-8")
    # Even an invalid release state must not be read once maintenance is live.
    (registry / "current.state").write_text("incomplete\n", encoding="utf-8")

    result = _run(env)

    assert result.returncode == 75
    assert "maintenance active; collection skipped" in result.stderr
    assert not docker_log.exists()
    artifact = Path(env["MARKET_CLOSE_RESULT_DIR"]) / "collector_last.json"
    payload = json.loads(artifact.read_text(encoding="ascii"))
    assert payload["status"] == "skipped_maintenance"
    assert payload["exit_code"] == 75


def test_runner_treats_broken_maintenance_symlink_as_active(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    Path(env["HEALTH_MAINTENANCE_FILE"]).symlink_to("missing-sentinel-target")

    result = _run(env)

    assert result.returncode == 75
    assert "maintenance active; collection skipped" in result.stderr
    assert not docker_log.exists()


@pytest.mark.parametrize(
    "state",
    [
        "truncated\nstate\n",
        f"{IMAGE_REF}\n{IMAGE_ID}\n{'b' * 64}\nredis\nextra\n",
        f"{IMAGE_REF}\n{IMAGE_ID}\n{'b' * 64}\n\n",
        f"my-ai-filter:latest\n{IMAGE_ID}\n{'b' * 64}\nredis\n",
        f"my-ai-filter:main\n{IMAGE_ID}\n{'b' * 64}\nredis\n",
    ],
)
def test_runner_fails_closed_on_invalid_release_state(runner_env, state):
    env, registry, _manifest, docker_log = runner_env
    (registry / "current.state").write_text(state, encoding="utf-8")

    result = _run(env)

    assert result.returncode != 0
    assert not docker_log.exists()


def test_runner_fails_closed_on_tampered_manifest(runner_env):
    env, _registry, manifest, docker_log = runner_env
    manifest.write_text(manifest.read_text() + "# tampered\n", encoding="utf-8")

    result = _run(env)

    assert result.returncode != 0
    assert "manifest hash mismatch" in result.stderr
    assert not docker_log.exists()


def test_runner_fails_closed_on_image_digest_mismatch(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_IMAGE_ID"] = "sha256:" + "b" * 64

    result = _run(env)

    assert result.returncode != 0
    assert "image digest mismatch" in result.stderr
    assert not any("|compose " in line for line in docker_log.read_text().splitlines())


def test_runner_rejects_overlapping_collection(runner_env):
    env, registry, _manifest, docker_log = runner_env
    lock_path = registry / "market-close.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _run(env)

    assert result.returncode != 0
    assert "already running" in result.stderr
    assert not docker_log.exists()


def test_runner_rejects_symlink_market_lock_without_touching_target(runner_env):
    env, registry, _manifest, docker_log = runner_env
    target = registry / "lock-target"
    target.write_text("unchanged\n", encoding="utf-8")
    (registry / "market-close.lock").symlink_to(target)

    result = _run(env)

    assert result.returncode == 78
    assert "regular non-symlink" in result.stderr
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not docker_log.exists()


def test_runner_rejects_symlink_release_lock_without_touching_target(runner_env):
    env, registry, _manifest, docker_log = runner_env
    target = registry / "release-target"
    target.write_text("unchanged\n", encoding="utf-8")
    (registry / "release.lock").symlink_to(target)

    result = _run(env)

    assert result.returncode == 78
    assert "regular non-symlink" in result.stderr
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert not docker_log.exists()


def test_runner_rejects_world_writable_lock_before_docker(runner_env):
    env, registry, _manifest, docker_log = runner_env
    lock = registry / "market-close.lock"
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o666)

    result = _run(env)

    assert result.returncode == 78
    assert "group/world-writable" in result.stderr
    assert not docker_log.exists()


def test_runner_propagates_collector_failure(runner_env):
    env, _registry, _manifest, _docker_log = runner_env
    env["FAKE_RUN_EXIT"] = "42"

    result = _run(env)

    assert result.returncode == 42


def test_runner_fails_when_recorded_image_is_missing(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_IMAGE_MISSING"] = "1"

    result = _run(env)

    assert result.returncode != 0
    assert "not available locally" in result.stderr
    assert not any("|compose " in line for line in docker_log.read_text().splitlines())


def test_runner_fails_when_collector_is_missing_from_manifest(runner_env):
    env, _registry, _manifest, docker_log = runner_env
    env["FAKE_SERVICE_MISSING"] = "1"

    result = _run(env)

    assert result.returncode != 0
    assert "does not define market-close-collector" in result.stderr
    assert not any(" run " in line for line in docker_log.read_text().splitlines())


def test_runner_times_out_behind_release(runner_env):
    env, registry, _manifest, docker_log = runner_env
    env["MARKET_CLOSE_RELEASE_LOCK_TIMEOUT_SECONDS"] = "0"
    release_lock = registry / "release.lock"
    with release_lock.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _run(env)

    assert result.returncode == 6
    assert "release lock" in result.stderr
    assert not docker_log.exists()


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "invalid"])
def test_runner_rejects_invalid_collection_timeout(runner_env, value):
    env, _registry, _manifest, docker_log = runner_env
    env["MARKET_CLOSE_RUN_TIMEOUT_SECONDS"] = value

    result = _run(env)

    assert result.returncode == 2
    assert "positive integer" in result.stderr
    assert not docker_log.exists()
