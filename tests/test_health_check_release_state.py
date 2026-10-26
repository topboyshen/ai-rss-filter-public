import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEALTH_SCRIPT = PROJECT_ROOT / "deploy" / "health_check.sh"
DIGEST = "sha256:" + "a" * 64
OLD_SERVICES = "redis rsshub ai-filter-web webhook-worker"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _health_fixture_files(tmp_path):
    project = tmp_path / "ai-rss-filter"
    registry = project / "data" / "releases"
    manifests = registry / "manifests"
    manifests.mkdir(parents=True)
    manifest = manifests / "placeholder.compose.yml"
    manifest.write_text(
        """services:
  redis: {image: redis:7}
  rsshub: {image: rsshub:1}
  ai-filter-web: {image: ${IMAGE_REF:?}}
  webhook-worker: {image: ${IMAGE_REF:?}}
""",
        encoding="utf-8",
    )
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    archived = manifests / f"{manifest_hash}.compose.yml"
    manifest.rename(archived)
    (registry / "current.state").write_text(
        f"my-ai-filter:2026.07.11.2\n{DIGEST}\n{manifest_hash}\n{OLD_SERVICES}\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    restart_marker = tmp_path / "restarted"
    post_recovery_probe_counter = tmp_path / "post-recovery-probes"

    _write_executable(
        fake_bin / "timeout",
        """#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*) shift ;;
    [0-9]*s|[0-9]*.[0-9]*s) shift; break ;;
    *) break ;;
  esac
done
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "flock",
        """#!/bin/sh
set -eu
if [ "$*" = "-w 1 8" ]; then
  if [ "${FAKE_RECOVERY_LOCK_BUSY:-0}" = 1 ]; then exit 1; fi
  if [ -n "${FAKE_BUSY_ONCE_MARKER:-}" ] && [ ! -f "$FAKE_BUSY_ONCE_MARKER" ]; then
    : > "$FAKE_BUSY_ONCE_MARKER"
    exit 1
  fi
  if [ -n "${FAKE_CHANGED_STATE:-}" ]; then
    cp "$FAKE_CHANGED_STATE" "$HEALTH_RELEASE_REGISTRY_DIR/current.state"
  fi
  if [ -n "${FAKE_MAINTENANCE_AT_LOCK:-}" ]; then
    : > "$FAKE_MAINTENANCE_AT_LOCK"
  fi
fi
if [ "$*" = "-n -E 75 9" ] && [ "${FAKE_HEALTH_LOCK_BUSY:-0}" = 1 ]; then
  exit 75
fi
exit 0
""",
    )
    _write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "curl", "#!/bin/sh\nprintf '200'\n")
    return (
        project, registry, archived, fake_bin, docker_log, restart_marker,
        post_recovery_probe_counter,
    )


def _write_fake_docker(fake_bin):
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
printf '%s|%s\n' "${IMAGE_REF:-}" "$*" >> "$FAKE_DOCKER_LOG"

if [ "$1" = "inspect" ]; then
  container=""
  for argument in "$@"; do container="$argument"; done
  service="${container#cid-}"
  unhealthy=0
  case " ${FAKE_UNHEALTHY_SERVICES:-${FAKE_UNHEALTHY_SERVICE:-}} " in
    *" $service "*) unhealthy=1 ;;
  esac
  if [ "$unhealthy" = 1 ] && \
     [ ! -f "$FAKE_RESTART_MARKER" ]; then
    status='unhealthy'
  elif [ -n "${FAKE_POST_RECOVERY_FAILS:-}" ] && \
     [ "$service" = "${FAKE_UNHEALTHY_SERVICE:-}" ] && \
     [ -f "$FAKE_RESTART_MARKER" ]; then
    count=0
    if [ -f "$FAKE_POST_RECOVERY_PROBE_COUNTER" ]; then
      count=$(cat "$FAKE_POST_RECOVERY_PROBE_COUNTER")
    fi
    count=$((count + 1))
    printf '%s\n' "$count" > "$FAKE_POST_RECOVERY_PROBE_COUNTER"
    if [ "$count" -le "$FAKE_POST_RECOVERY_FAILS" ]; then
      status='unhealthy'
    else
      status='healthy'
    fi
  elif [ -n "${FAKE_RUNNING_SERVICE:-}" ] && [ "$service" = "$FAKE_RUNNING_SERVICE" ]; then
    status='running'
  else
    status='healthy'
  fi
  image_id="$FAKE_EXPECTED_IMAGE_ID"
  if [ -n "${FAKE_DRIFT_SERVICE:-}" ] && [ "$service" = "$FAKE_DRIFT_SERVICE" ] && \
     [ ! -f "$FAKE_RESTART_MARKER" ]; then
    image_id="$FAKE_DRIFT_IMAGE_ID"
  fi
  printf '%s|%s\n' "$status" "$image_id"
  exit 0
fi

if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  printf '%s\n' "$FAKE_EXPECTED_IMAGE_ID"
  exit 0
fi

[ "$1" = "compose" ] || exit 90
shift
project=""
manifest=""
override=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --project-directory) project="$2"; shift 2 ;;
    -f)
      if [ -z "$manifest" ]; then manifest="$2"; else override="$2"; fi
      shift 2 ;;
    *) break ;;
  esac
done
[ "$project" = "$FAKE_PROJECT_ROOT" ] || exit 91
[ "$manifest" = "$FAKE_MANIFEST" ] || exit 92
[ "$override" = "${FAKE_OVERRIDE:-}" ] || exit 101
subcommand="$1"
shift
case "$subcommand" in
  config)
    if [ "${1:-}" = "--services" ]; then
      for service in $FAKE_CONFIGURED_SERVICES; do printf '%s\n' "$service"; done
    elif [ "${1:-}" = "--format" ] && [ "${2:-}" = "json" ]; then
      printf '{"services":{'
      separator=''
      for service in $FAKE_CONFIGURED_SERVICES; do
        printf '%s"%s":{"image":"fake:release"}' "$separator" "$service"
        separator=','
      done
      printf '}}\n'
    else
      exit 93
    fi
    ;;
  ps)
    [ "${1:-}" = "-q" ] || exit 94
    printf 'cid-%s\n' "$2"
    ;;
  up)
    [ "${1:-}" = "-d" ] || exit 95
    [ "${2:-}" = "--no-deps" ] || exit 96
    [ "${3:-}" = "--force-recreate" ] || exit 97
    [ "${4:-}" = "--pull" ] || exit 98
    [ "${5:-}" = "never" ] || exit 99
    [ "$#" -eq 6 ] || exit 100
    if [ -n "${FAKE_NATIVE_FLOCK:-}" ] && \
       "$FAKE_NATIVE_FLOCK" -n "$HEALTH_RELEASE_REGISTRY_DIR/release.lock" true; then
      exit 102
    fi
    if [ "${FAKE_RECOVERY_EXIT:-0}" != 0 ]; then exit "$FAKE_RECOVERY_EXIT"; fi
    : > "$FAKE_RESTART_MARKER"
    ;;
  *) exit 96 ;;
esac
""",
    )

@pytest.fixture
def health_env(tmp_path):
    (
        project, registry, archived, fake_bin, docker_log, restart_marker,
        post_recovery_probe_counter,
    ) = _health_fixture_files(tmp_path)
    _write_fake_docker(fake_bin)
    log = tmp_path / "health.log"
    state_dir = tmp_path / "health-state"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HEALTH_CHECK_TIMEOUT_CHILD": "1",
            "HEALTH_LOG": str(log),
            "HEALTH_LOCK": str(tmp_path / "health.lock"),
            "HEALTH_STATE_DIR": str(state_dir),
            "HEALTH_MAINTENANCE_FILE": str(tmp_path / "no-maintenance"),
            "AI_FILTER_ROOT": str(project),
            "HEALTH_RELEASE_REGISTRY_DIR": str(registry),
            "HEALTH_FAILURE_THRESHOLD": "3",
            "HEALTH_RECOVERY_COOLDOWN_SECONDS": "1",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_PROJECT_ROOT": str(project),
            "FAKE_MANIFEST": str(archived),
            "FAKE_CONFIGURED_SERVICES": OLD_SERVICES,
            "FAKE_EXPECTED_IMAGE_ID": DIGEST,
            "FAKE_DRIFT_IMAGE_ID": "sha256:" + "b" * 64,
            "FAKE_RESTART_MARKER": str(restart_marker),
            "FAKE_POST_RECOVERY_PROBE_COUNTER": str(post_recovery_probe_counter),
        }
    )
    return env, registry, archived, docker_log, restart_marker, log


def _run_health(env):
    return subprocess.run(
        [str(HEALTH_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_health_uses_recorded_old_manifest_and_health_service_set(health_env):
    env, _registry, archived, docker_log, _marker, _log = health_env
    env["FAKE_RUNNING_SERVICE"] = "webhook-worker"

    result = _run_health(env)

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert f"-f {archived} config --services" in calls
    for service in OLD_SERVICES.split():
        assert f"-f {archived} ps -q {service}" in calls
    assert "ps -q ai-filter-api" not in calls
    assert "docker-compose.yml" not in calls
    compose_calls = [line for line in calls.splitlines() if "|compose " in line]
    assert compose_calls
    assert all(line.startswith(f"{DIGEST}|") for line in compose_calls)


def test_tampered_archived_manifest_fails_closed_without_restart(health_env):
    env, _registry, archived, docker_log, _marker, log = health_env
    archived.write_text(archived.read_text(encoding="utf-8") + "# tampered\n")

    result = _run_health(env)

    assert result.returncode == 1
    assert not docker_log.exists()
    assert "current.state or archived release manifest is invalid" in log.read_text()


def test_health_reconciles_failed_old_service_with_recorded_manifest(health_env):
    env, _registry, archived, docker_log, restart_marker, _log = health_env
    env["HEALTH_FAILURE_THRESHOLD"] = "1"
    env["FAKE_UNHEALTHY_SERVICE"] = "webhook-worker"

    result = _run_health(env)

    # A run that had to recover remains non-zero so cron still records the
    # incident, even though the post-restart probe succeeds.
    assert result.returncode == 1
    assert restart_marker.exists()
    calls = docker_log.read_text(encoding="utf-8")
    assert f"{DIGEST}|compose --project-directory" in calls
    assert f"-f {archived} up -d --no-deps --force-recreate --pull never webhook-worker" in calls
    assert "--pull never ai-filter-worker" not in calls


def _recovery_path(env, key="ai-release-webhook-worker"):
    return Path(env["HEALTH_STATE_DIR"]) / f"{key}.last-recovery"


def _enable_recovery(env):
    env["HEALTH_FAILURE_THRESHOLD"] = "1"
    env["HEALTH_RECOVERY_COOLDOWN_SECONDS"] = "1800"
    env["FAKE_UNHEALTHY_SERVICE"] = "webhook-worker"


def test_busy_recovery_does_not_consume_cooldown_and_next_run_recovers(health_env):
    env, _registry, _archived, docker_log, marker, _log = health_env
    _enable_recovery(env)
    env["FAKE_RECOVERY_LOCK_BUSY"] = "1"
    assert _run_health(env).returncode == 1
    assert not marker.exists()
    assert not _recovery_path(env).exists()
    assert " up -d " not in docker_log.read_text()

    env.pop("FAKE_RECOVERY_LOCK_BUSY")
    assert _run_health(env).returncode == 1
    assert marker.exists()
    assert int(_recovery_path(env).read_text()) > 0


def test_changed_release_does_not_consume_cooldown(health_env, tmp_path):
    env, registry, _archived, docker_log, marker, _log = health_env
    _enable_recovery(env)
    old_state = (registry / "current.state").read_text()
    changed = tmp_path / "changed.state"
    changed.write_text(old_state.replace(DIGEST, "sha256:" + "b" * 64))
    env["FAKE_CHANGED_STATE"] = str(changed)
    assert _run_health(env).returncode == 1
    assert not marker.exists()
    assert not _recovery_path(env).exists()
    assert " up -d " not in docker_log.read_text()


@pytest.mark.parametrize("kind", ["global", "service"])
def test_maintenance_rechecked_under_recovery_lock(health_env, kind):
    env, _registry, _archived, docker_log, marker, _log = health_env
    _enable_recovery(env)
    sentinel = Path(env["HEALTH_MAINTENANCE_FILE"])
    if kind == "service":
        sentinel = Path(env["HEALTH_STATE_DIR"]) / "ai-release-webhook-worker.maintenance"
    env["FAKE_MAINTENANCE_AT_LOCK"] = str(sentinel)
    assert _run_health(env).returncode == 1
    assert sentinel.exists()
    assert not marker.exists()
    assert not _recovery_path(env).exists()
    assert " up -d " not in docker_log.read_text()


@pytest.mark.parametrize("failure", ["1", "75", "124"])
def test_executed_failure_or_timeout_keeps_cooldown(health_env, failure):
    env, _registry, _archived, docker_log, marker, log = health_env
    _enable_recovery(env)
    env["FAKE_RECOVERY_EXIT"] = failure
    assert _run_health(env).returncode == 1
    assert not marker.exists()
    recorded = _recovery_path(env).read_text()
    assert int(recorded) > 0
    assert _run_health(env).returncode == 1
    assert _recovery_path(env).read_text() == recorded
    assert docker_log.read_text().count(" up -d ") == 1
    assert "suppressed by 1800s cooldown" in log.read_text()
    assert "targeted recovery command failed" in log.read_text()


def test_busy_first_service_leaves_budget_for_second_service(health_env, tmp_path):
    env, _registry, _archived, docker_log, marker, _log = health_env
    _enable_recovery(env)
    env["FAKE_UNHEALTHY_SERVICES"] = "redis webhook-worker"
    env["FAKE_BUSY_ONCE_MARKER"] = str(tmp_path / "first-lock-refused")
    assert _run_health(env).returncode == 1
    assert marker.exists()
    assert not _recovery_path(env, "ai-redis").exists()
    assert _recovery_path(env).exists()
    assert docker_log.read_text().count(" up -d ") == 1


def test_attempted_first_recovery_keeps_one_attempt_per_run(health_env):
    env, _registry, _archived, docker_log, _marker, log = health_env
    _enable_recovery(env)
    env["FAKE_UNHEALTHY_SERVICES"] = "redis webhook-worker"
    env["FAKE_RECOVERY_EXIT"] = "1"
    assert _run_health(env).returncode == 1
    assert _recovery_path(env, "ai-redis").exists()
    assert not _recovery_path(env).exists()
    assert docker_log.read_text().count(" up -d ") == 1
    assert "already attempted one recovery" in log.read_text()


def test_concurrent_health_run_still_exits_without_probes(health_env):
    env, _registry, _archived, docker_log, marker, _log = health_env
    env["FAKE_HEALTH_LOCK_BUSY"] = "1"
    assert _run_health(env).returncode == 0
    assert not docker_log.exists()
    assert not marker.exists()


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("flock") is None,
    reason="native release-lock integration requires Linux util-linux flock",
)
def test_native_release_lock_held_through_docker_then_released(health_env):
    env, registry, _archived, _docker_log, marker, _log = health_env
    _enable_recovery(env)
    native_flock = shutil.which("flock")
    fake_flock = Path(env["PATH"].split(os.pathsep)[0]) / "flock"
    fake_flock.unlink()
    fake_flock.symlink_to(native_flock)
    env["FAKE_NATIVE_FLOCK"] = native_flock

    assert _run_health(env).returncode == 1
    assert marker.exists(), "Docker must run while release.lock is held exclusively"
    result = subprocess.run(
        [native_flock, "-n", str(registry / "release.lock"), "true"],
        check=False,
    )
    assert result.returncode == 0


def test_health_rejects_service_not_present_in_archived_manifest(health_env):
    env, registry, archived, docker_log, _marker, log = health_env
    state = (registry / "current.state").read_text(encoding="utf-8")
    (registry / "current.state").write_text(
        state.replace(OLD_SERVICES, f"{OLD_SERVICES} invented-worker"),
        encoding="utf-8",
    )

    result = _run_health(env)

    assert result.returncode == 1
    calls = docker_log.read_text(encoding="utf-8")
    assert f"-f {archived} config --services" in calls
    assert " ps -q " not in calls
    assert " restart " not in calls
    assert "current.state or archived release manifest is invalid" in log.read_text()


def test_health_rejects_container_running_wrong_image(health_env):
    env, _registry, _archived, docker_log, restart_marker, log = health_env
    env["FAKE_DRIFT_SERVICE"] = "webhook-worker"

    result = _run_health(env)

    assert result.returncode == 1
    assert not restart_marker.exists()
    assert "AI-Filter webhook-worker health probe failed (1/3)" in log.read_text()
    assert "inspect --format" in docker_log.read_text()


def test_health_reconciles_drifted_image_to_recorded_manifest(health_env):
    env, _registry, archived, docker_log, restart_marker, log = health_env
    env["HEALTH_FAILURE_THRESHOLD"] = "1"
    env["FAKE_DRIFT_SERVICE"] = "webhook-worker"

    result = _run_health(env)

    assert result.returncode == 1
    assert restart_marker.exists()
    calls = docker_log.read_text()
    assert f"-f {archived} up -d --no-deps --force-recreate --pull never webhook-worker" in calls
    assert "recovered after targeted reconciliation" in log.read_text()


def test_api_recovery_waits_beyond_the_old_six_second_window(health_env):
    env, registry, archived, _docker_log, restart_marker, log = health_env
    replacement = archived.read_text(encoding="utf-8").replace(
        "webhook-worker", "ai-filter-api"
    )
    replacement_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
    replacement_manifest = archived.with_name(f"{replacement_hash}.compose.yml")
    replacement_manifest.write_text(replacement, encoding="utf-8")
    archived.unlink()
    (registry / "current.state").write_text(
        f"my-ai-filter:2026.07.11.2\n{DIGEST}\n{replacement_hash}\n"
        "redis rsshub ai-filter-web ai-filter-api\n",
        encoding="utf-8",
    )
    env.update(
        {
            "FAKE_MANIFEST": str(replacement_manifest),
            "FAKE_CONFIGURED_SERVICES": "redis rsshub ai-filter-web ai-filter-api",
            "FAKE_UNHEALTHY_SERVICE": "ai-filter-api",
            "FAKE_POST_RECOVERY_FAILS": "5",
            "HEALTH_FAILURE_THRESHOLD": "1",
        }
    )

    result = _run_health(env)

    assert result.returncode == 1
    assert restart_marker.exists()
    assert "recovered after targeted reconciliation" in log.read_text()


def test_api_recovery_has_a_wall_clock_window_inside_the_total_deadline():
    source = HEALTH_SCRIPT.read_text(encoding="utf-8")

    assert 'HEALTH_TOTAL_TIMEOUT_SECONDS:-360' in source
    assert 'post_restart_deadline=$(($(date +%s) + 90))' in source
    assert '$(date +%s) < post_restart_deadline' in source
    assert 'command_timeout_seconds=55' in source


def _record_forced_override(health_env):
    env, registry, archived, _docker_log, _marker, _log = health_env
    override = registry / "force-no-migration-fence.compose.yml"
    override.write_text(
        'services:\n  webhook-worker:\n    environment:\n'
        '      AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE: "0"\n',
        encoding="utf-8",
    )
    override_hash = hashlib.sha256(override.read_bytes()).hexdigest()
    (registry / "current.override").write_text(f"{override}\n", encoding="utf-8")
    (registry / "current.override-sha256").write_text(
        f"{override_hash}\n", encoding="utf-8"
    )
    ref = (registry / "current.state").read_text().splitlines()[0]
    receipt = registry / "force-no-migration-fence.receipt"
    receipt.write_text(
        "status=forced_gate_bypass_healthy\n"
        f"ref={ref}\ndigest={DIGEST}\n"
        f"base_manifest_sha256={archived.name.removesuffix('.compose.yml')}\n"
        f"override_path={override}\noverride_sha256={override_hash}\n"
        "migration_gate_enforced=false\n"
        "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE=0\n",
        encoding="utf-8",
    )
    env["FAKE_OVERRIDE"] = str(override)
    return override, receipt


def test_health_preserves_active_forced_override_in_probes_and_recovery(health_env):
    env, _registry, archived, docker_log, marker, log = health_env
    override, _receipt = _record_forced_override(health_env)
    env["HEALTH_FAILURE_THRESHOLD"] = "1"
    env["FAKE_UNHEALTHY_SERVICE"] = "webhook-worker"

    result = _run_health(env)

    assert result.returncode == 1
    assert marker.exists()
    calls = docker_log.read_text()
    compose_calls = [line for line in calls.splitlines() if "|compose " in line]
    assert compose_calls
    assert all(f"-f {archived} -f {override}" in line for line in compose_calls)
    assert "up -d --no-deps --force-recreate --pull never webhook-worker" in calls
    assert "recovered after targeted reconciliation" in log.read_text()


def test_healthy_forced_release_does_not_trigger_recovery_or_false_alarm(health_env):
    env, _registry, _archived, _docker_log, marker, log = health_env
    _record_forced_override(health_env)

    result = _run_health(env)

    assert result.returncode == 0
    assert not marker.exists()
    assert "health probe failed" not in log.read_text()


@pytest.mark.parametrize("damage", ["override", "missing_receipt", "ref", "digest", "manifest"])
def test_invalid_forced_override_cannot_recreate_a_service(health_env, damage):
    env, registry, _archived, docker_log, marker, log = health_env
    override, receipt = _record_forced_override(health_env)
    env["HEALTH_FAILURE_THRESHOLD"] = "1"
    env["FAKE_UNHEALTHY_SERVICE"] = "webhook-worker"
    if damage == "override":
        override.write_text(override.read_text() + "# changed\n")
    elif damage == "missing_receipt":
        receipt.unlink()
    else:
        state = (registry / "current.state").read_text().splitlines()
        index = {"ref": 0, "digest": 1, "manifest": 2}[damage]
        old = state[index]
        replacement = "my-ai-filter:2026.09.06.20" if index == 0 else "c" * 64
        if index == 1:
            replacement = "sha256:" + replacement
        receipt.write_text(receipt.read_text().replace(old, replacement))

    result = _run_health(env)

    assert result.returncode == 1
    assert not marker.exists()
    assert not docker_log.exists()
    assert "forced release override invalid; automatic recovery disabled" in log.read_text()
