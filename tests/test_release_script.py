import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = PROJECT_ROOT / "scripts" / "release.sh"
DIAGNOSTICS_HELPER = PROJECT_ROOT / "scripts" / "capture_release_diagnostics.py"
RELEASE_GATE_RUNNER = PROJECT_ROOT / "scripts" / "run-release-gate.sh"
IMAGE_ID = "sha256:" + "a" * 64
OLD_IMAGE_ID = "sha256:" + "b" * 64
OLD_IMAGE_REF = "my-ai-filter:2026.08.23.14"
NEW_HEALTH_SERVICES = (
    "redis rsshub ai-filter-api ai-filter-web ai-filter-worker"
)
OLD_HEALTH_SERVICES = "redis rsshub ai-filter-web webhook-worker"


def _cleanup_release_test_artifacts(secret_files, secret_dir, registry):
    for secret_file in secret_files:
        secret_file.unlink(missing_ok=True)
    try:
        secret_dir.rmdir()
    except OSError:
        pass
    # Production diagnostics remain sealed. Only restore directory writes
    # in this fixture's disposable registry, after all test assertions, so
    # the outer release runner can remove it as an unprivileged user.
    diagnostics = registry / "diagnostics"
    if not diagnostics.is_symlink():
        for root, _dirs, _files in os.walk(diagnostics, followlinks=False):
            directory = Path(root)
            directory.chmod(directory.stat().st_mode | stat.S_IWUSR)


@pytest.fixture
def release_env(tmp_path):
    secret_dir = PROJECT_ROOT / "secrets"
    secret_names = (
        "dashboard_session_secret",
        "ai_filter_integration_token",
        "quant_market_context_token",
    )
    secret_files = [secret_dir / name for name in secret_names]
    assert not any(path.exists() for path in secret_files), (
        "tests must not overwrite a real runtime secret"
    )
    secret_dir.mkdir(exist_ok=True)
    for secret_file in secret_files:
        secret_file.write_text(f"release-test-{secret_file.name}-0123456789abcdef\n")
        secret_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if shutil.which("flock") is None:
        flock = fake_bin / "flock"
        flock.write_text(
            """#!/usr/bin/env python3
import fcntl
import sys

arguments = sys.argv[1:]
conflict_exit = 1
shared = False
unlock = False
index = 0
descriptor = None
while index < len(arguments):
    argument = arguments[index]
    if argument == "-E":
        conflict_exit = int(arguments[index + 1])
        index += 2
    elif argument in {"-n", "--nonblock"}:
        index += 1
    elif argument in {"-s", "--shared"}:
        shared = True
        index += 1
    elif argument in {"-u", "--unlock"}:
        unlock = True
        index += 1
    else:
        descriptor = int(argument)
        index += 1
if descriptor is None:
    raise SystemExit(64)
operation = fcntl.LOCK_UN if unlock else (
    fcntl.LOCK_SH if shared else fcntl.LOCK_EX
)
if not unlock:
    operation |= fcntl.LOCK_NB
try:
    fcntl.flock(descriptor, operation)
except BlockingIOError:
    raise SystemExit(conflict_exit)
""",
            encoding="utf-8",
        )
        flock.chmod(0o755)
    gnu_stat_probe = subprocess.run(
        ["stat", "-c", "%s", str(PROJECT_ROOT / "Dockerfile")],
        text=True,
        capture_output=True,
        check=False,
    )
    if gnu_stat_probe.returncode != 0:
        stat_tool = fake_bin / "stat"
        stat_tool.write_text(
            """#!/usr/bin/env python3
import os
import stat
import sys

arguments = sys.argv[1:]
if arguments[:1] != ["-c"] or len(arguments) < 3:
    os.execv("/usr/bin/stat", ["stat", *arguments])
format_string = arguments[1]
paths = [value for value in arguments[2:] if value != "--"]
if len(paths) != 1:
    raise SystemExit(64)
metadata = os.lstat(paths[0])
values = {
    "%u": str(metadata.st_uid),
    "%a": format(stat.S_IMODE(metadata.st_mode), "o"),
    "%h": str(metadata.st_nlink),
    "%s": str(metadata.st_size),
    "%d": str(metadata.st_dev),
    "%i": str(metadata.st_ino),
}
rendered = format_string
for key, value in values.items():
    rendered = rendered.replace(key, value)
print(rendered)
""",
            encoding="utf-8",
        )
        stat_tool.chmod(0o755)
    log = tmp_path / "docker.log"
    isolation_root = tmp_path / "release-isolation"
    isolation_root.mkdir(mode=0o700)
    registry = isolation_root / "releases"
    old_manifest = tmp_path / "old-compose.yml"
    old_manifest.write_text(
        """services:
  ai-filter-web:
    image: ${IMAGE_REF:?}
  redis:
    image: redis:7
  webhook-worker:
    image: ${IMAGE_REF:?}
  rsshub:
    image: rsshub:1
  market-close-collector:
    profiles: [\"cron\"]
    image: ${IMAGE_REF:?}
""",
        encoding="utf-8",
    )
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf '%s|%s\n' "${IMAGE_REF:-}" "$*" >> "$FAKE_DOCKER_LOG"

metadata_value() {
  key="$1"
  [ -f "$FAKE_BUILD_METADATA" ] || return 0
  awk -F= -v wanted="$key" '
    $1 == wanted {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "$FAKE_BUILD_METADATA"
}

if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  if [ "${FAKE_MISSING_UNTIL_BUILD:-0}" = "1" ] && [ ! -f "$FAKE_BUILD_MARKER" ]; then
    exit 1
  fi
  image_id="$FAKE_IMAGE_ID"
  if [ -n "${FAKE_ALT_IMAGE_REF:-}" ] && \
     { [ "${3:-}" = "$FAKE_ALT_IMAGE_REF" ] || [ "${3:-}" = "${FAKE_ALT_IMAGE_ID:-}" ]; }; then
    image_id="$FAKE_ALT_IMAGE_ID"
  fi
  case "$*" in
    *"{{.Id}}"*) printf '%s\n' "$image_id" ;;
    *"RepoDigests"*) printf '["my-ai-filter@%s"]\n' "${FAKE_REPO_DIGEST:-$image_id}" ;;
    *"org.opencontainers.image.version"*) key="org.opencontainers.image.version" ;;
    *"org.opencontainers.image.revision"*) key="org.opencontainers.image.revision" ;;
    *"io.ai-filter.compose-manifest.sha256"*) key="io.ai-filter.compose-manifest.sha256" ;;
    *"io.ai-filter.config.sha256"*) key="io.ai-filter.config.sha256" ;;
    *"io.ai-filter.source.identity"*) key="io.ai-filter.source.identity" ;;
    *"io.ai-filter.event-mapping-generation"*) key="io.ai-filter.event-mapping-generation" ;;
  esac
  if [ -n "${key:-}" ]; then
    if [ "$key" = "io.ai-filter.event-mapping-generation" ] && \
       [ -n "${FAKE_ALT_IMAGE_REF:-}" ] && \
       { [ "${3:-}" = "$FAKE_ALT_IMAGE_REF" ] || [ "${3:-}" = "${FAKE_ALT_IMAGE_ID:-}" ]; }; then
      value="${FAKE_ALT_MAPPING_GENERATION:-}"
    else
      value="$(metadata_value "$key")"
    fi
    if [ "${FAKE_BAD_LABEL_KEY:-}" = "$key" ]; then
      value="tampered"
    fi
    printf '%s\n' "$value"
  fi
  exit 0
fi
if [ "$1" = "build" ]; then
  shift
  : > "$FAKE_BUILD_MARKER"
  : > "$FAKE_BUILD_METADATA"
  context=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --tag)
        printf 'org.opencontainers.image.version=%s\n' "$2" >> "$FAKE_BUILD_METADATA"
        shift 2
        ;;
      --build-arg)
        case "$2" in
          AI_FILTER_BUILD_GIT_SHA=*)
            printf 'org.opencontainers.image.revision=%s\n' "${2#*=}" >> "$FAKE_BUILD_METADATA"
            ;;
          AI_FILTER_BUILD_MANIFEST_SHA=*)
            printf 'io.ai-filter.compose-manifest.sha256=%s\n' "${2#*=}" >> "$FAKE_BUILD_METADATA"
            ;;
          AI_FILTER_BUILD_CONFIG_SHA=*)
            printf 'io.ai-filter.config.sha256=%s\n' "${2#*=}" >> "$FAKE_BUILD_METADATA"
            ;;
          AI_FILTER_BUILD_SOURCE_ID=*)
            printf 'io.ai-filter.source.identity=%s\n' "${2#*=}" >> "$FAKE_BUILD_METADATA"
            ;;
          AI_FILTER_MAPPING_MIGRATION_GENERATION=*)
            printf 'io.ai-filter.event-mapping-generation=%s\n' "${2#*=}" >> "$FAKE_BUILD_METADATA"
            ;;
        esac
        shift 2
        ;;
      *)
        context="$1"
        shift
        ;;
    esac
  done
  [ -n "$context" ] && [ -d "$context" ] || exit 88
  config_hash="$(sha256sum "$context/config.yaml" | awk '{print $1}')"
  printf 'IMAGE_CONFIG_SHA=%s\n' "$config_hash" >> "$FAKE_BUILD_METADATA"
  if [ -n "${FAKE_CONTEXT_RECORD:-}" ]; then
    printf '%s\n%s\n%s\n' \
      "$context" "$(stat -c '%a' "$context")" "$config_hash" > "$FAKE_CONTEXT_RECORD"
  fi
  if [ -n "${FAKE_REQUIRE_CONTEXT_FILE:-}" ]; then
    [ -f "$context/$FAKE_REQUIRE_CONTEXT_FILE" ] || exit 89
  fi
  if [ -n "${FAKE_MUTATE_SOURCE_PATH:-}" ]; then
    printf '\n# changed after snapshot\n' >> "$FAKE_MUTATE_SOURCE_PATH"
  fi
  exit 0
fi
if [ "$1" = "pull" ]; then
  exit 0
fi
if [ "$1" = "run" ]; then
  value="$(metadata_value "IMAGE_CONFIG_SHA")"
  if [ "${FAKE_BAD_IMAGE_CONFIG:-0}" = "1" ]; then
    value="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  fi
  printf '%s\n' "$value"
  exit 0
fi
if [ "$1" = "diff" ]; then
  if [ -n "${FAKE_SOURCE_DRIFT_SERVICE:-}" ] && \
     [ "$2" = "cid-$FAKE_SOURCE_DRIFT_SERVICE" ]; then
    printf 'C /app/core/processor.py\n'
  fi
  if [ -n "${FAKE_CONFIG_DRIFT_SERVICE:-}" ] && \
     [ "$2" = "cid-$FAKE_CONFIG_DRIFT_SERVICE" ]; then
    printf 'C /app/config.yaml\n'
  fi
  exit 0
fi
if [ "$1" = "inspect" ]; then
  container_id=""
  for argument in "$@"; do
    case "$argument" in cid-*) container_id="$argument" ;; esac
  done
  unhealthy=0
  if [ -n "${FAKE_FAIL_REF:-}" ] && [ "${IMAGE_REF:-}" = "$FAKE_FAIL_REF" ]; then
    unhealthy=1
  elif [ -n "${FAKE_FAIL_SERVICE:-}" ] && \
       [ "$container_id" = "cid-$FAKE_FAIL_SERVICE" ]; then
    unhealthy=1
  fi
  case "$*" in
    *"{{if .State.Health}}"*)
      if [ "$unhealthy" = "1" ]; then
        printf 'unhealthy\n'
      else
        printf 'healthy\n'
      fi
      ;;
    *)
      if [ "${FAKE_DIAGNOSTIC_INSPECT_FAIL:-0}" = "1" ]; then
        exit 73
      fi
      service="${container_id#cid-}"
      if [ "$unhealthy" = "1" ]; then
        status="running"
        health="unhealthy"
        exit_code=0
      else
        status="running"
        health="healthy"
        exit_code=0
      fi
      printf '{"Id":"%s","Name":"/%s","RestartCount":0,"State":{"Status":"%s","Running":true,"ExitCode":%s,"OOMKilled":false,"Error":"","Health":{"Status":"%s","FailingStreak":3,"Log":[{"Start":"2026-09-02T01:00:00Z","End":"2026-09-02T01:00:01Z","ExitCode":1,"Output":"{\\"token\\":\\"json-health-secret\\"}"}]}}}\n' \
        "$container_id" "$service" "$status" "$exit_code" "$health"
      ;;
  esac
  exit 0
fi
if [ "$1" = "logs" ]; then
  if [ "${FAKE_DIAGNOSTIC_LOGS_FAIL:-0}" = "1" ]; then
    exit 74
  fi
  printf '%s\n' \
    '2026-09-02T01:00:00Z redis ready' \
    '2026-09-02T01:00:01Z password=redis-log-secret' \
    '2026-09-02T01:00:02Z {"token":"json-log-secret"}'
  printf '%s\n' \
    '2026-09-02T01:00:03Z {"authorization":"Bearer stderr-secret"}' >&2
  exit 0
fi

if [ "$1" = "compose" ]; then
  shift
  project_directory=""
  manifest=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --project-directory) project_directory="$2"; shift 2 ;;
      -f) manifest="$2"; shift 2 ;;
      --profile) shift 2 ;;
      *) break ;;
    esac
  done
  [ -n "$project_directory" ] || exit 91
  [ -n "$manifest" ] || exit 92
  subcommand="$1"
  shift
  case "$subcommand" in
    config)
      if [ "${1:-}" = "--quiet" ] && \
         [ -n "${FAKE_CONFIG_FAIL_REF:-}" ] && \
         [ "${IMAGE_REF:-}" = "$FAKE_CONFIG_FAIL_REF" ]; then
        exit 1
      fi
      if [ "${1:-}" = "--format" ] && [ "${2:-}" = "json" ]; then
        if [ "${FAKE_ENABLE_MAPPING_DECLARATION:-0}" = "1" ] && \
           grep -q 'AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE' "$manifest"; then
          declaration='"AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE":"1","AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION":"event-mapping-store-v5-provider-attribution-v1"'
        else
          declaration=''
        fi
        printf '{"services":{"ai-filter-api":{"environment":{%s}},"ai-filter-worker":{"environment":{%s}},"event-enrichment":{"environment":{%s}}}}\n' \
          "$declaration" "$declaration" "$declaration"
      fi
      if [ "${1:-}" = "--services" ]; then
        printf 'redis\nrsshub\nai-filter-web\n'
        if grep -q '^  webhook-worker:' "$manifest"; then
          printf 'webhook-worker\nmarket-close-collector\n'
        else
          printf 'ai-filter-api\nai-filter-worker\n'
        fi
      fi
      exit 0
      ;;
    ps)
      if [ "${1:-}" = "--all" ]; then
        shift
      fi
      [ "${1:-}" = "-q" ] || exit 93
      service="$2"
      if [ -n "${FAKE_MISSING_SERVICE:-}" ] && [ "$service" = "$FAKE_MISSING_SERVICE" ]; then
        exit 0
      fi
      printf 'cid-%s\n' "$service"
      exit 0
      ;;
    build)
      : > "$FAKE_BUILD_MARKER"
      exit 0
      ;;
    up)
      if [ "${FAKE_SET_MAPPING_PONR_ON_UP:-0}" = "1" ] && \
         [ "${IMAGE_REF:-}" = "$FAKE_IMAGE_ID" ]; then
        python3 -c 'import json, os, pathlib; p=pathlib.Path(os.environ["AI_FILTER_MAPPING_MIGRATION_STATE_FILE"]); v=json.loads(p.read_text()); v.update({"status":"running","phase":"mapping","point_of_no_return":True,"automatic_rollback_allowed":False,"write_attempted":True}); p.write_text(json.dumps(v, sort_keys=True)+"\\n"); p.chmod(0o600)'
      fi
      if [ -n "${FAKE_COMPOSE_FAIL_REF:-}" ] && [ "${IMAGE_REF:-}" = "$FAKE_COMPOSE_FAIL_REF" ]; then
        exit 1
      fi
      if [ "${FAKE_GATEWAY_RECREATE_FAIL:-0}" = "1" ]; then
        case " $* " in
          *" --force-recreate ai-filter-web "*) exit 1 ;;
        esac
      fi
      exit 0
      ;;
    run)
      if [ "${FAKE_GATEWAY_PREFLIGHT_FAIL:-0}" = "1" ]; then
        exit 1
      fi
      [ "${1:-}" = "--rm" ] || exit 94
      [ "${2:-}" = "--no-deps" ] || exit 95
      [ "${3:-}" = "ai-filter-web" ] || exit 96
      [ "${4:-}" = "nginx" ] || exit 97
      [ "${5:-}" = "-t" ] || exit 98
      exit 0
      ;;
  esac
fi
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    gate_python = fake_bin / "release-test-python"
    gate_python.write_text(
        """#!/bin/sh
set -eu
if [ "${1:-}" = "-I" ] && [ "${2:-}" = "-c" ]; then
  printf 'CPython|3.11.99\n'
  exit 0
fi
exit 97
""",
        encoding="utf-8",
    )
    gate_python.chmod(0o755)
    attestation_dir = tmp_path / "test-attestations"
    test_gate = registry / "mapping-gate.json"
    test_state = registry / "mapping-state.json"
    release_copy = tmp_path / "release.sh"
    release_source = RELEASE_SCRIPT.read_text(encoding="utf-8")
    root_literal = 'ROOT="$(cd "$(dirname "$0")/.." && pwd)"'
    assert release_source.count(root_literal) == 1
    release_source = release_source.replace(
        root_literal,
        f"ROOT={shlex.quote(str(PROJECT_ROOT))}",
    )
    release_copy.write_text(release_source, encoding="utf-8")
    release_copy.chmod(0o700)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(mode=0o700)
    env = {
        "HOME": str(isolated_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "TMPDIR": str(tmp_path),
    }
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_IMAGE_ID": IMAGE_ID,
        "FAKE_BUILD_MARKER": str(tmp_path / "built"),
        "FAKE_BUILD_METADATA": str(tmp_path / "build-metadata"),
        "RELEASE_REGISTRY_DIR": str(registry),
        "RELEASE_HEALTH_TIMEOUT_SECONDS": "1",
        "RELEASE_HEALTH_POLL_SECONDS": "0.01",
        # Legacy behavior remains available only as an explicit test override;
        # production defaults to requiring a matching build contract.
        "RELEASE_REQUIRE_BUILD_CONTRACT": "0",
        "RELEASE_BUILD_CONTRACT_OVERRIDE_ACTOR": "pytest",
        "RELEASE_BUILD_CONTRACT_OVERRIDE_REASON": "legacy deploy-path fixture",
        # The release entrypoint accepts these paths only as a complete tuple
        # below an owned private isolation root. The fake docker executable
        # also consumes them when it simulates a cutover race.
        "AI_FILTER_MAPPING_MIGRATION_GATE_FILE": str(test_gate),
        "AI_FILTER_MAPPING_MIGRATION_STATE_FILE": str(test_state),
        "AI_FILTER_RELEASE_ISOLATION_ROOT": str(isolation_root),
        "RELEASE_TEST_SCRIPT": str(release_copy),
        # The repository running this suite may intentionally contain local
        # work. Individual guard tests remove this audited exception.
        "RELEASE_ALLOW_DIRTY_TREE": "1",
        "RELEASE_DIRTY_TREE_OVERRIDE_ACTOR": "pytest",
        "RELEASE_DIRTY_TREE_OVERRIDE_REASON": "release-script test fixture",
        "AI_FILTER_TEST_PYTHON": str(gate_python),
        "RELEASE_TEST_ATTESTATION_DIR": str(attestation_dir),
    })
    _write_valid_test_attestation(env)
    try:
        yield env, log, registry, old_manifest
    finally:
        _cleanup_release_test_artifacts(secret_files, secret_dir, registry)


def _run_release(env, *args, pass_fds=()):
    release_script = Path(env.get("RELEASE_TEST_SCRIPT", RELEASE_SCRIPT))
    return subprocess.run(
        [str(release_script), *args],
        cwd=PROJECT_ROOT,
        env=env,
        pass_fds=pass_fds,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_release_entrypoint_defaults_to_fixed_event_mapping_operational_paths():
    source = RELEASE_SCRIPT.read_text(encoding="utf-8")

    assert (
        'DEFAULT_EVENT_MAPPING_MIGRATION_GATE_FILE="${ROOT}/data/'
        '.event-mapping-migration-required"'
    ) in source
    assert (
        'DEFAULT_EVENT_MAPPING_MIGRATION_STATE_FILE="/var/lib/ai-filter-ops-check/'
        'event-mapping-migration.json"'
    ) in source


def test_runtime_mapping_gate_is_ignored_without_hiding_other_data_files():
    gate = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "check-ignore",
            "-q",
            "data/.event-mapping-migration-required",
        ],
        check=False,
    )
    unrelated = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "check-ignore",
            "-q",
            "data/.unexpected-release-file",
        ],
        check=False,
    )

    assert gate.returncode == 0
    assert unrelated.returncode == 1


def test_isolated_event_mapping_paths_must_be_paired_and_below_isolation_root(
    release_env, tmp_path
):
    env, log, registry, _old_manifest = release_env
    root_only_env = env.copy()
    root_only_env.pop("AI_FILTER_MAPPING_MIGRATION_GATE_FILE")
    root_only_env.pop("AI_FILTER_MAPPING_MIGRATION_STATE_FILE")

    root_only = _run_release(
        root_only_env, "verify", "my-ai-filter:2026.07.12.1"
    )

    assert root_only.returncode == 2
    assert "all required" in root_only.stderr
    assert not registry.exists()
    assert not log.exists()

    unpaired_env = env.copy()
    unpaired_env.pop("AI_FILTER_MAPPING_MIGRATION_STATE_FILE")

    unpaired = _run_release(
        unpaired_env, "verify", "my-ai-filter:2026.07.12.1"
    )

    assert unpaired.returncode == 2
    assert "all required" in unpaired.stderr
    assert not registry.exists()
    assert not log.exists()

    outside_path_env = env.copy()
    outside_path_env["AI_FILTER_MAPPING_MIGRATION_GATE_FILE"] = str(
        tmp_path / "outside.json"
    )

    outside = _run_release(
        outside_path_env, "verify", "my-ai-filter:2026.07.12.1"
    )

    assert outside.returncode == 2
    assert "below AI_FILTER_RELEASE_ISOLATION_ROOT" in outside.stderr
    assert not registry.exists()
    assert not log.exists()

    outside_registry = tmp_path / "outside-registry"
    outside_registry_env = env.copy()
    outside_registry_env["RELEASE_REGISTRY_DIR"] = str(outside_registry)

    escaped_registry = _run_release(
        outside_registry_env, "verify", "my-ai-filter:2026.07.12.1"
    )

    assert escaped_registry.returncode == 2
    assert "below AI_FILTER_RELEASE_ISOLATION_ROOT" in escaped_registry.stderr
    assert not outside_registry.exists()
    assert not registry.exists()
    assert not log.exists()


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_valid_test_attestation(env, *, completed_at=None):
    head = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--verify", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    attestation_dir = Path(env["RELEASE_TEST_ATTESTATION_DIR"])
    attestation_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    attestation_dir.chmod(0o700)
    lock_file = attestation_dir / "attestation.lock"
    lock_file.touch()
    lock_file.chmod(0o600)
    log_file = attestation_dir / f"{head}.log"
    log_file.write_text("synthetic release-script fixture: full suite passed\n")
    log_file.chmod(0o600)
    completed = int(time.time()) if completed_at is None else completed_at
    started = completed - 1
    python_path = Path(env["AI_FILTER_TEST_PYTHON"]).resolve()
    identity_hash = hashlib.sha256(b"CPython|3.11.99").hexdigest()
    values = [
        "ai-filter-release-test-attestation-v1",
        f"{head}:clean",
        "pytest-full-cov75-v1",
        _hash(RELEASE_GATE_RUNNER),
        str(python_path),
        _hash(python_path),
        identity_hash,
        str(started),
        str(completed),
        _hash(log_file),
        "passed",
    ]
    state_file = attestation_dir / f"{head}.state"
    state_file.write_text("\n".join(values) + "\n", encoding="utf-8")
    state_file.chmod(0o600)
    return state_file, log_file


def _create_dirty_marker(tmp_path):
    marker = PROJECT_ROOT / f".release-dirty-test-{os.getpid()}-{tmp_path.name}"
    marker.write_text("dirty-tree guard test\n", encoding="utf-8")
    return marker


def _enable_mapping_guard(env):
    env["FAKE_ENABLE_MAPPING_DECLARATION"] = "1"
    metadata = Path(env["FAKE_BUILD_METADATA"])
    metadata.write_text(
        "io.ai-filter.event-mapping-generation="
        "event-mapping-store-v5-provider-attribution-v1\n",
        encoding="utf-8",
    )


def _write_secure_json(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _install_completed_mapping_gate(env, ref, *, manifest_hash=None, status="complete"):
    env["FAKE_ENABLE_MAPPING_DECLARATION"] = "1"
    generation = "event-mapping-store-v5-provider-attribution-v1"
    manifest_hash = manifest_hash or _hash(PROJECT_ROOT / "docker-compose.yml")
    gate = {
        "version": 1,
        "kind": "event_mapping_gate",
        "status": "complete",
        "generation": generation,
        "target_image_ref": ref,
        "target_image_digest": IMAGE_ID,
        "manifest_hash": manifest_hash,
        "mapping_contract": "asset-map:event-mapping-store-v5:test",
        "input_snapshot_sha256": "2" * 64,
        "verification_sha256": "3" * 64,
        "completed_at": 1,
    }
    _write_secure_json(env["AI_FILTER_MAPPING_MIGRATION_GATE_FILE"], gate)
    _write_secure_json(
        env["AI_FILTER_MAPPING_MIGRATION_STATE_FILE"],
        {
            "version": 1,
            "status": status,
            "phase": "complete",
            "generation": generation,
            "target_image_ref": ref,
            "target_digest": IMAGE_ID,
            "migration_complete": True,
            "point_of_no_return": True,
            "write_attempted": True,
            "automatic_rollback_allowed": False,
            "release": {
                "image_ref": ref,
                "digest": IMAGE_ID,
                "manifest_hash": manifest_hash,
            },
        },
    )
    return gate


def _read_recorded_release(registry, name="current"):
    values = (Path(registry) / f"{name}.state").read_text(encoding="utf-8").splitlines()
    assert len(values) == 4
    return {
        "image_ref": values[0],
        "digest": values[1],
        "manifest_hash": values[2],
        "health_services": values[3],
    }


def _install_prepared_mapping_gate(env, source_release, *, status="prepared"):
    generation = "event-mapping-store-v5-provider-attribution-v1"
    ref = "my-ai-filter:2026.08.24.15"
    gate_path = Path(env["AI_FILTER_MAPPING_MIGRATION_GATE_FILE"])
    gate = {
        "version": 1,
        "kind": "event_mapping_gate",
        "status": "prepared",
        "generation": generation,
        "token": "7" * 64,
        "target_image_ref": ref,
        "target_image_digest": IMAGE_ID,
        "created_at": 1,
    }
    _write_secure_json(gate_path, gate)
    state = {
        "version": 1,
        "status": status,
        "generation": generation,
        "target_image_ref": ref,
        "target_digest": IMAGE_ID,
        "point_of_no_return": False,
        "write_attempted": False,
        "automatic_rollback_allowed": True,
        "gate": {"sha256": _hash(gate_path)},
        "source_release": source_release,
    }
    _write_secure_json(env["AI_FILTER_MAPPING_MIGRATION_STATE_FILE"], state)
    return ref, gate_path, Path(env["AI_FILTER_MAPPING_MIGRATION_STATE_FILE"])


def _set_mapping_state(state_path, **updates):
    value = json.loads(Path(state_path).read_text(encoding="utf-8"))
    value.update(updates)
    _write_secure_json(state_path, value)
    return value


def test_mapping_generation_deploy_requires_prepared_gate(release_env):
    env, log, _registry, _old_manifest = release_env
    _enable_mapping_guard(env)

    result = _run_release(env, "deploy", "my-ai-filter:2026.08.24.15")

    assert result.returncode == 3
    assert "Prepare the event-mapping migration" in result.stderr
    assert not any(" compose " in line and " up " in line for line in log.read_text().splitlines())


@pytest.mark.parametrize("generation_side", ("image", "manifest"))
def test_mapping_generation_requires_matching_image_and_manifest_declaration(
    release_env,
    generation_side,
):
    env, log, _registry, _old_manifest = release_env
    if generation_side == "image":
        _enable_mapping_guard(env)
        env["FAKE_ENABLE_MAPPING_DECLARATION"] = "0"
    else:
        env["FAKE_ENABLE_MAPPING_DECLARATION"] = "1"

    result = _run_release(env, "deploy", "my-ai-filter:2026.08.24.15")

    assert result.returncode == 3
    assert "Image and release manifest disagree" in result.stderr
    assert not any(
        " compose " in line and " up " in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_mapping_generation_prepared_gate_allows_exact_target(release_env):
    env, _log, _registry, _source, ref, _gate, _state = (
        _prepare_guarded_release(release_env)
    )

    result = _run_release(env, "deploy", ref)

    assert result.returncode == 0, result.stderr


def test_completed_mapping_generation_blocks_legacy_image(release_env):
    env, log, _registry, old_manifest = release_env
    _enable_mapping_guard(env)
    generation = "event-mapping-store-v5-provider-attribution-v1"
    _write_secure_json(
        env["AI_FILTER_MAPPING_MIGRATION_GATE_FILE"],
        {
            "version": 1,
            "kind": "event_mapping_gate",
            "status": "complete",
            "generation": generation,
            "target_image_ref": "my-ai-filter:2026.08.24.15",
            "target_image_digest": IMAGE_ID,
            "manifest_hash": "1" * 64,
            "mapping_contract": "asset-map:event-mapping-store-v5:test",
            "input_snapshot_sha256": "2" * 64,
            "verification_sha256": "3" * 64,
            "completed_at": 1,
        },
    )
    legacy_ref = "my-ai-filter:2026.08.23.14"
    env["FAKE_ALT_IMAGE_REF"] = legacy_ref
    env["FAKE_ALT_IMAGE_ID"] = OLD_IMAGE_ID
    env["RELEASE_COMPOSE_FILE"] = str(old_manifest)
    env["RELEASE_HEALTH_SERVICES"] = OLD_HEALTH_SERVICES

    result = _run_release(env, "deploy", legacy_ref)

    assert result.returncode == 3
    assert "Completed event-mapping migration forbids" in result.stderr
    assert not any(" compose " in line and " up " in line for line in log.read_text().splitlines())


def _deploy_old(env, old_manifest, old_ref="my-ai-filter:2026.07.11.2"):
    env["FAKE_ALT_IMAGE_REF"] = old_ref
    env["FAKE_ALT_IMAGE_ID"] = OLD_IMAGE_ID
    env["RELEASE_COMPOSE_FILE"] = str(old_manifest)
    env["RELEASE_HEALTH_SERVICES"] = OLD_HEALTH_SERVICES
    result = _run_release(env, "deploy", old_ref)
    assert result.returncode == 0, result.stderr
    env.pop("RELEASE_COMPOSE_FILE")
    env["RELEASE_HEALTH_SERVICES"] = NEW_HEALTH_SERVICES
    return old_ref


def _prepare_guarded_release(release_env, *, status="prepared"):
    env, log, registry, old_manifest = release_env
    source_ref = _deploy_old(env, old_manifest, OLD_IMAGE_REF)
    source = _read_recorded_release(registry)
    assert source["image_ref"] == source_ref
    _enable_mapping_guard(env)
    target_ref, gate_path, state_path = _install_prepared_mapping_gate(
        env,
        source,
        status=status,
    )
    return env, log, registry, source, target_ref, gate_path, state_path


def test_preparing_state_cannot_deploy_target_generation(release_env):
    env, log, _registry, _source, target_ref, _gate, _state = (
        _prepare_guarded_release(release_env, status="preparing")
    )
    log.write_text("", encoding="utf-8")

    refused = _run_release(env, "deploy", target_ref)

    assert refused.returncode == 3
    assert "verified release pair" in refused.stderr
    assert " up " not in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("point_of_no_return", (False, True))
def test_mapping_guard_manual_source_rollback_respects_point_of_no_return(
    release_env,
    point_of_no_return,
):
    env, log, _registry, source, target_ref, _gate, state = (
        _prepare_guarded_release(release_env)
    )
    deployed = _run_release(env, "deploy", target_ref)
    assert deployed.returncode == 0, deployed.stderr
    if point_of_no_return:
        _set_mapping_state(
            state,
            status="running",
            phase="mapping",
            point_of_no_return=True,
            automatic_rollback_allowed=False,
            write_attempted=True,
        )
    log.write_text("", encoding="utf-8")

    rolled_back = _run_release(env, "rollback", source["image_ref"])

    old_up = any(
        line.startswith(f"{source['digest']}|") and " up " in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )
    if point_of_no_return:
        assert rolled_back.returncode == 3
        assert "Forward-only event-mapping migration forbids" in rolled_back.stderr
        assert old_up is False
    else:
        assert rolled_back.returncode == 0, rolled_back.stderr
        assert old_up is True


@pytest.mark.parametrize("point_of_no_return", (False, True))
def test_mapping_guard_automatic_source_rollback_respects_point_of_no_return(
    release_env,
    point_of_no_return,
):
    env, log, _registry, source, target_ref, _gate, _state = (
        _prepare_guarded_release(release_env)
    )
    env["FAKE_FAIL_REF"] = IMAGE_ID
    if point_of_no_return:
        env["FAKE_SET_MAPPING_PONR_ON_UP"] = "1"
    log.write_text("", encoding="utf-8")

    failed_deploy = _run_release(env, "deploy", target_ref)

    old_up = any(
        line.startswith(f"{source['digest']}|") and " up " in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )
    if point_of_no_return:
        assert failed_deploy.returncode == 5
        assert "Forward-only event-mapping migration forbids" in failed_deploy.stderr
        assert old_up is False
    else:
        assert failed_deploy.returncode == 4
        assert old_up is True


def test_mapping_guard_source_rollback_binds_manifest_hash(release_env):
    env, log, _registry, source, target_ref, _gate, state = (
        _prepare_guarded_release(release_env)
    )
    deployed = _run_release(env, "deploy", target_ref)
    assert deployed.returncode == 0, deployed.stderr
    migration = json.loads(state.read_text(encoding="utf-8"))
    migration["source_release"]["manifest_hash"] = "0" * 64
    _write_secure_json(state, migration)
    log.write_text("", encoding="utf-8")

    refused = _run_release(env, "rollback", source["image_ref"])

    assert refused.returncode == 3
    assert "Forward-only event-mapping migration forbids" in refused.stderr
    assert not any(
        line.startswith(f"{source['digest']}|") and " up " in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_mapping_target_preflight_requires_exact_current_rollback_source(
    release_env,
):
    env, log, _registry, _source, target_ref, _gate, state = (
        _prepare_guarded_release(release_env)
    )
    migration = json.loads(state.read_text(encoding="utf-8"))
    migration["source_release"]["manifest_hash"] = "0" * 64
    _write_secure_json(state, migration)
    log.write_text("", encoding="utf-8")

    refused = _run_release(env, "deploy", target_ref)

    assert refused.returncode == 3
    assert "verified release pair" in refused.stderr
    assert not any(
        " compose " in line and " up " in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"point_of_no_return": 1},
        {"point_of_no_return": "true"},
        {
            "point_of_no_return": False,
            "write_attempted": True,
            "automatic_rollback_allowed": False,
        },
    ),
)
def test_mapping_guard_rejects_malformed_or_inconsistent_rollback_boundary(
    release_env,
    updates,
):
    env, log, _registry, source, target_ref, _gate, state = (
        _prepare_guarded_release(release_env)
    )
    deployed = _run_release(env, "deploy", target_ref)
    assert deployed.returncode == 0, deployed.stderr
    _set_mapping_state(state, **updates)
    log.write_text("", encoding="utf-8")

    refused = _run_release(env, "rollback", source["image_ref"])

    assert refused.returncode == 3
    assert "migration state" in refused.stderr.lower()
    assert not any(
        line.startswith(f"{source['digest']}|") and " up " in line
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_prewrite_source_rollback_requires_abort_before_target_redeploy(
    release_env,
):
    env, log, _registry, source, target_ref, _gate, state = (
        _prepare_guarded_release(release_env)
    )
    deployed = _run_release(env, "deploy", target_ref)
    assert deployed.returncode == 0, deployed.stderr
    _set_mapping_state(
        state,
        status="progress",
        phase="mapping",
        mapping_contract="asset-map:event-mapping-store-v5:test",
        input_snapshot_sha256="2" * 64,
    )
    rolled_back = _run_release(env, "rollback", source["image_ref"])
    assert rolled_back.returncode == 0, rolled_back.stderr
    log.write_text("", encoding="utf-8")

    refused = _run_release(env, "deploy", target_ref)

    assert refused.returncode == 3
    assert "verified release pair" in refused.stderr
    assert " up " not in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("state_status", "requested_ref", "expected_returncode"),
    (
        ("activation_pending", "my-ai-filter:2026.08.24.15", 0),
        ("activation_pending", "my-ai-filter:2026.08.24.16", 3),
        ("complete", "my-ai-filter:2026.08.24.16", 0),
    ),
)
def test_complete_gate_binds_activation_then_allows_compatible_upgrades(
    release_env,
    state_status,
    requested_ref,
    expected_returncode,
):
    env, log, _registry, _old_manifest = release_env
    _enable_mapping_guard(env)
    _install_completed_mapping_gate(
        env,
        "my-ai-filter:2026.08.24.15",
        status=state_status,
    )

    result = _run_release(env, "deploy", requested_ref)

    assert result.returncode == expected_returncode, result.stderr
    if expected_returncode:
        assert "Event-mapping activation is bound" in result.stderr
        assert " up " not in log.read_text(encoding="utf-8")


def test_legacy_five_line_build_contract_still_validates_on_rollback(release_env):
    env, _log, registry, old_manifest = release_env
    ref = _deploy_old(env, old_manifest, OLD_IMAGE_REF)
    recorded = _read_recorded_release(registry)
    config_hash = _hash(PROJECT_ROOT / "config.yaml")
    source_id = "4" * 40 + ":clean"
    metadata = Path(env["FAKE_BUILD_METADATA"])
    metadata.write_text(
        "\n".join(
            (
                f"org.opencontainers.image.version={ref}",
                f"org.opencontainers.image.revision={'4' * 40}",
                f"io.ai-filter.compose-manifest.sha256={recorded['manifest_hash']}",
                f"io.ai-filter.config.sha256={config_hash}",
                f"io.ai-filter.source.identity={source_id}",
                f"IMAGE_CONFIG_SHA={config_hash}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    contracts = registry / "build-contracts"
    contracts.mkdir(exist_ok=True)
    key = hashlib.sha256(ref.encode("utf-8")).hexdigest()
    contract = contracts / f"{key}.state"
    contract.write_text(
        f"{ref}\n{recorded['digest']}\n{recorded['manifest_hash']}\n"
        f"{config_hash}\n{source_id}\n",
        encoding="utf-8",
    )
    contract.chmod(0o600)

    rolled_back = _run_release(env, "rollback", ref)

    assert rolled_back.returncode == 0, rolled_back.stderr


@pytest.mark.parametrize("ref", ["latest", "my-ai-filter:latest", "my-ai-filter:main", "my-ai-filter:dev"])
def test_release_rejects_floating_tags(release_env, ref):
    env, log, _registry, _old_manifest = release_env

    result = _run_release(env, "deploy", ref)

    assert result.returncode != 0
    assert not log.exists()


@pytest.mark.parametrize("command", ["build", "deploy"])
def test_build_and_deploy_fail_closed_on_dirty_tree(
    release_env, tmp_path, command
):
    env, log, registry, _old_manifest = release_env
    env.pop("RELEASE_ALLOW_DIRTY_TREE")
    env.pop("RELEASE_DIRTY_TREE_OVERRIDE_ACTOR")
    env.pop("RELEASE_DIRTY_TREE_OVERRIDE_REASON")
    marker = _create_dirty_marker(tmp_path)
    try:
        result = _run_release(env, command, "my-ai-filter:2026.07.12.90")
    finally:
        marker.unlink(missing_ok=True)

    assert result.returncode == 7
    assert "dirty Git worktree" in result.stderr
    if command == "build":
        assert "attestation covers only clean HEAD" in result.stderr
    else:
        assert "RELEASE_DIRTY_TREE_OVERRIDE_REASON" in result.stderr
    assert not log.exists()
    assert not (registry / "dirty-overrides.tsv").exists()


def test_dirty_tree_override_requires_audit_reason(release_env, tmp_path):
    env, log, registry, _old_manifest = release_env
    env.pop("RELEASE_DIRTY_TREE_OVERRIDE_REASON")
    marker = _create_dirty_marker(tmp_path)
    try:
        result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.91")
    finally:
        marker.unlink(missing_ok=True)

    assert result.returncode == 2
    assert "RELEASE_DIRTY_TREE_OVERRIDE_REASON is required" in result.stderr
    assert not log.exists()
    assert not (registry / "dirty-overrides.tsv").exists()


def test_dirty_tree_override_is_fingerprinted_and_audited(release_env, tmp_path):
    env, _log, registry, _old_manifest = release_env
    env["RELEASE_DIRTY_TREE_OVERRIDE_ACTOR"] = "release-operator"
    env["RELEASE_DIRTY_TREE_OVERRIDE_REASON"] = "INC-1234 emergency release"
    marker = _create_dirty_marker(tmp_path)
    try:
        first = _run_release(env, "deploy", "my-ai-filter:2026.07.12.92")
        marker.write_text("different emergency payload\n", encoding="utf-8")
        second = _run_release(env, "deploy", "my-ai-filter:2026.07.12.93")
    finally:
        marker.unlink(missing_ok=True)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "accepted audited dirty-tree override" in first.stderr
    records = [
        line.split("\t")
        for line in (registry / "dirty-overrides.tsv").read_text().splitlines()
    ]
    first_fields, second_fields = records[-2:]
    assert first_fields[1:3] == ["deploy", "my-ai-filter:2026.07.12.92"]
    assert second_fields[1:3] == ["deploy", "my-ai-filter:2026.07.12.93"]
    assert len(first_fields[3]) in {40, 64}
    assert len(first_fields[4]) == 64
    assert len(second_fields[4]) == 64
    assert first_fields[4] != second_fields[4]
    assert first_fields[5:] == ["release-operator", "INC-1234 emergency release"]
    assert second_fields[5:] == ["release-operator", "INC-1234 emergency release"]


def test_build_rejects_dirty_tree_even_with_audited_override(release_env, tmp_path):
    env, log, registry, _old_manifest = release_env
    marker = _create_dirty_marker(tmp_path)
    try:
        result = _run_release(env, "build", "my-ai-filter:2026.07.12.94")
    finally:
        marker.unlink(missing_ok=True)

    assert result.returncode == 7
    assert "attestation covers only clean HEAD" in result.stderr
    assert not log.exists()
    assert not (registry / "dirty-overrides.tsv").exists()


def test_build_requires_full_suite_attestation_before_docker(release_env):
    env, log, _registry, _old_manifest = release_env
    state_file, _log_file = _write_valid_test_attestation(env)
    state_file.unlink()

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.95")

    assert result.returncode == 8
    assert "not a trusted regular file" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    ("field", "replacement", "expected"),
    [
        (0, "ai-filter-release-test-attestation-v0", "does not match"),
        (1, f"{'0' * 40}:clean", "does not match"),
        (2, "pytest-selected-tests-v1", "does not match"),
        (3, "0" * 64, "does not match"),
        (10, "failed", "does not match"),
    ],
)
def test_build_rejects_mismatched_test_attestation(
    release_env, field, replacement, expected
):
    env, log, _registry, _old_manifest = release_env
    state_file, _log_file = _write_valid_test_attestation(env)
    values = state_file.read_text(encoding="utf-8").splitlines()
    values[field] = replacement
    state_file.write_text("\n".join(values) + "\n", encoding="utf-8")
    state_file.chmod(0o600)

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.96")

    assert result.returncode == 8
    assert expected in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("completed_offset", [-21660, 3600])
def test_build_rejects_stale_or_future_test_attestation(
    release_env, completed_offset
):
    env, log, _registry, _old_manifest = release_env
    _write_valid_test_attestation(
        env, completed_at=int(time.time()) + completed_offset
    )

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.97")

    assert result.returncode == 8
    assert "stale or has invalid timestamps" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("unsafe_kind", ["mode", "symlink", "hardlink", "extra"])
def test_build_rejects_unsafe_or_malformed_test_attestation(
    release_env, tmp_path, unsafe_kind
):
    env, log, _registry, _old_manifest = release_env
    state_file, _log_file = _write_valid_test_attestation(env)
    if unsafe_kind == "mode":
        state_file.chmod(0o644)
    elif unsafe_kind == "symlink":
        replacement = tmp_path / "copied-attestation"
        replacement.write_bytes(state_file.read_bytes())
        replacement.chmod(0o600)
        state_file.unlink()
        state_file.symlink_to(replacement)
    elif unsafe_kind == "hardlink":
        hardlink = tmp_path / "hardlinked-attestation"
        os.link(state_file, hardlink)
    else:
        with state_file.open("a", encoding="utf-8") as handle:
            handle.write("unexpected\n")

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.98")

    assert result.returncode == 8
    assert not log.exists()


def test_build_rejects_external_compose_even_with_valid_attestation(
    release_env, tmp_path
):
    env, log, _registry, _old_manifest = release_env
    external_compose = tmp_path / "compose.yml"
    external_compose.write_bytes(
        (PROJECT_ROOT / "docker-compose.yml").read_bytes()
    )
    env["RELEASE_COMPOSE_FILE"] = str(external_compose)

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.99")

    assert result.returncode == 8
    assert "committed Compose manifest only" in result.stderr
    assert not log.exists()


def test_deploy_does_not_require_current_test_attestation(release_env):
    env, _log, _registry, _old_manifest = release_env
    state_file, log_file = _write_valid_test_attestation(env)
    state_file.unlink()
    log_file.unlink()

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.100")

    assert result.returncode == 0, result.stderr


def test_deploy_archives_manifest_and_records_complete_state(release_env):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.1"

    result = _run_release(env, "deploy", ref)

    assert result.returncode == 0, result.stderr
    manifest_hash = _hash(PROJECT_ROOT / "docker-compose.yml")
    assert (registry / "current.ref").read_text().strip() == ref
    assert (registry / "current.digest").read_text().strip() == IMAGE_ID
    assert (registry / "current.manifest").read_text().strip() == manifest_hash
    assert (registry / "current.health-services").read_text().strip() == NEW_HEALTH_SERVICES
    assert (registry / "current.state").read_text().splitlines() == [
        ref,
        IMAGE_ID,
        manifest_hash,
        NEW_HEALTH_SERVICES,
    ]
    archived = registry / "manifests" / f"{manifest_hash}.compose.yml"
    assert archived.read_bytes() == (PROJECT_ROOT / "docker-compose.yml").read_bytes()
    calls = log.read_text()
    assert "compose --project-directory" in calls
    assert f"-f {archived}" in calls
    assert "up -d --no-build --pull never redis rsshub" in calls
    assert "up -d --no-build --pull never --no-deps --force-recreate ai-filter-api" in calls
    assert "up -d --no-build --pull never --remove-orphans" in calls
    assert "up -d --no-build --pull never --no-deps --force-recreate ai-filter-web" in calls
    assert "up -d --no-build --pull never --no-deps --force-recreate ai-filter-worker" in calls
    infra_up = calls.index("up -d --no-build --pull never redis rsshub")
    api_up = calls.index("up -d --no-build --pull never --no-deps --force-recreate ai-filter-api")
    web_up = calls.index("up -d --no-build --pull never --no-deps --force-recreate ai-filter-web")
    worker_up = calls.index("up -d --no-build --pull never --no-deps --force-recreate ai-filter-worker")
    full_up = calls.index("up -d --no-build --pull never --remove-orphans")
    assert infra_up < api_up < web_up < worker_up < full_up
    compose_calls = [line for line in calls.splitlines() if "|compose " in line]
    assert compose_calls
    assert all(line.startswith(f"{IMAGE_ID}|") for line in compose_calls)
    assert not any(
        line.startswith(f"{ref}|") and (" up " in line or " ps " in line)
        for line in compose_calls
    )
    assert ":latest" not in calls


@pytest.mark.parametrize("preflight_failure", [False, True])
def test_standard_redeploy_retires_forced_override_only_after_success(
    release_env, preflight_failure
):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.1"
    result = _run_release(env, "deploy", ref)
    assert result.returncode == 0, result.stderr
    override = registry / "force-no-migration-fence.compose.yml"
    receipt = registry / "force-no-migration-fence.receipt"
    override.write_text("services: {}\n")
    receipt.write_text("historical forced deployment record\n")
    path_pointer = registry / "current.override"
    hash_pointer = registry / "current.override-sha256"
    path_pointer.write_text(f"{override}\n")
    hash_pointer.write_text(_hash(override) + "\n")
    if preflight_failure:
        (PROJECT_ROOT / "secrets" / "dashboard_session_secret").unlink()
    log.write_text("")

    result = _run_release(env, "deploy", ref)

    assert (result.returncode != 0) == preflight_failure, result.stderr
    assert path_pointer.exists() == preflight_failure
    assert hash_pointer.exists() == preflight_failure
    assert override.exists()
    assert receipt.exists()
    assert str(override) not in log.read_text()


def test_deploy_fails_before_compose_up_when_dashboard_secret_is_missing(
    release_env,
):
    env, log, registry, _old_manifest = release_env
    (PROJECT_ROOT / "secrets" / "dashboard_session_secret").unlink()

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.9")

    assert result.returncode != 0
    assert "Runtime secret" in result.stderr
    calls = log.read_text()
    assert " up -d " not in calls
    assert "preflight_failed" in (registry / "history.tsv").read_text()


@pytest.mark.parametrize(
    "secret_name",
    ["ai_filter_integration_token", "quant_market_context_token"],
)
def test_deploy_fails_before_compose_up_when_service_secret_is_missing(
    release_env, secret_name
):
    env, log, registry, _old_manifest = release_env
    (PROJECT_ROOT / "secrets" / secret_name).unlink()

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.9")

    assert result.returncode != 0
    assert secret_name in result.stderr
    assert " up -d " not in log.read_text()
    assert "preflight_failed" in (registry / "history.tsv").read_text()


@pytest.mark.parametrize("mode", [0o644, 0o440, 0o700])
def test_deploy_rejects_insecure_runtime_secret_mode(release_env, mode):
    env, log, _registry, _old_manifest = release_env
    secret = PROJECT_ROOT / "secrets" / "ai_filter_integration_token"
    secret.chmod(mode)

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.9")

    assert result.returncode != 0
    assert "mode 0400 or 0600" in result.stderr
    assert " up -d " not in log.read_text()


def test_deploy_rejects_symlink_and_whitespace_runtime_secrets(release_env, tmp_path):
    env, log, _registry, _old_manifest = release_env
    secret = PROJECT_ROOT / "secrets" / "ai_filter_integration_token"
    secret.unlink()
    target = tmp_path / "secret"
    target.write_text("a" * 40)
    target.chmod(0o600)
    secret.symlink_to(target)

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.9")

    assert result.returncode != 0
    assert "non-symlink" in result.stderr
    assert " up -d " not in log.read_text()

    secret.unlink()
    secret.write_text("a" * 20 + " " + "b" * 20)
    secret.chmod(0o600)
    log.write_text("")
    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.10")
    assert result.returncode != 0
    assert "ASCII graphic bytes" in result.stderr
    assert " up -d " not in log.read_text()


@pytest.mark.parametrize(
    ("secret_name", "payload"),
    [
        ("dashboard_session_secret", b"A" * 32 + b"\x00"),
        ("ai_filter_integration_token", b"A" * 31 + b"\x80"),
        ("quant_market_context_token", b"A" * 32 + b"\n\n"),
        ("ai_filter_integration_token", b"A" * 16 + b"\n" + b"B" * 16),
    ],
    ids=["nul", "non-ascii", "multiple-final-lf", "embedded-lf"],
)
def test_deploy_rejects_non_graphic_runtime_secret_bytes(
    release_env, secret_name, payload
):
    env, log, registry, _old_manifest = release_env
    secret = PROJECT_ROOT / "secrets" / secret_name
    secret.write_bytes(payload)
    secret.chmod(0o600)

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.11")

    assert result.returncode != 0
    assert "32-4096 ASCII graphic bytes" in result.stderr
    assert secret_name in result.stderr
    assert " up -d " not in log.read_text()
    assert "preflight_failed" in (registry / "history.tsv").read_text()


def test_deploy_accepts_graphic_secret_without_trailing_lf(release_env):
    env, _log, _registry, _old_manifest = release_env
    secret = PROJECT_ROOT / "secrets" / "ai_filter_integration_token"
    secret.write_bytes(b"A" * 32)
    secret.chmod(0o400)

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.12")

    assert result.returncode == 0, result.stderr


def test_digest_ref_must_resolve_to_requested_digest(release_env):
    env, _log, _registry, _old_manifest = release_env
    wrong = "b" * 64

    result = _run_release(env, "verify", f"my-ai-filter@sha256:{wrong}")

    assert result.returncode != 0
    assert "Digest mismatch" in result.stderr


def test_health_failure_restores_old_webhook_worker_manifest(release_env):
    env, log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    old_hash = _hash(old_manifest)
    new_ref = "my-ai-filter:2026.07.12.1"
    log.write_text("", encoding="utf-8")
    env["FAKE_FAIL_REF"] = IMAGE_ID

    result = _run_release(env, "deploy", new_ref)

    assert result.returncode != 0
    assert (registry / "current.ref").read_text().strip() == old_ref
    assert (registry / "current.manifest").read_text().strip() == old_hash
    calls = log.read_text()
    new_hash = _hash(PROJECT_ROOT / "docker-compose.yml")
    assert f"{IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{new_hash}.compose.yml up -d --no-build --pull never redis rsshub" in calls
    assert f"{IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{new_hash}.compose.yml up -d --no-build --pull never --remove-orphans" not in calls
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml up -d --no-build --pull never --remove-orphans" in calls
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml ps --all -q webhook-worker" in calls
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml ps --all -q ai-filter-worker" not in calls
    assert not any(
        line.startswith((f"{new_ref}|", f"{old_ref}|"))
        and (" up " in line or " ps " in line)
        for line in calls.splitlines()
    )
    history = (registry / "history.tsv").read_text()
    assert "compose_failed" in history
    assert "auto-rollback" in history
    assert "|logs " not in calls
    assert not (registry / "diagnostics").exists()


def test_enabled_health_diagnostics_capture_redis_before_rollback(release_env):
    env, log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    old_hash = _hash(old_manifest)
    log.write_text("", encoding="utf-8")
    env["RELEASE_CAPTURE_HEALTH_DIAGNOSTICS"] = "1"
    env["FAKE_FAIL_REF"] = IMAGE_ID

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 4, result.stderr
    run_dirs = [
        path
        for path in (registry / "diagnostics").iterdir()
        if path.is_dir()
    ]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert json.loads((run_dir / "index.json").read_text(encoding="utf-8"))
    assert (run_dir / "SHA256SUMS").is_file()
    assert (run_dir / "SEALED").is_file()
    assert not (run_dir / "PARTIAL").exists()
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o500
    assert stat.S_IMODE((run_dir / "SEALED").stat().st_mode) == 0o400

    redis_dir = next((run_dir / "services").glob("*-redis"))
    state_file = next(redis_dir.glob("container-*-state.json"))
    health_file = next(redis_dir.glob("container-*-health-history.json"))
    logs_file = next(redis_dir.glob("container-*-logs.json"))
    evidence = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (state_file, health_file, logs_file)
    )
    assert "json-health-secret" not in evidence
    assert "redis-log-secret" not in evidence
    assert "json-log-secret" not in evidence
    assert "stderr-secret" not in evidence
    logs = json.loads(logs_file.read_text(encoding="utf-8"))
    assert logs["policy"] == "redis_metadata_only"
    assert logs["stdout"]["categories"]["ready"] >= 1
    all_artifacts = b"\n".join(
        path.read_bytes() for path in run_dir.rglob("*") if path.is_file()
    )
    for secret in (
        b"json-health-secret",
        b"redis-log-secret",
        b"json-log-secret",
        b"stderr-secret",
    ):
        assert secret not in all_artifacts

    calls = log.read_text(encoding="utf-8").splitlines()
    inspect_index = next(
        index
        for index, call in enumerate(calls)
        if "|inspect --type container --format " in call
        and call.endswith(" cid-redis")
    )
    logs_index = next(
        index
        for index, call in enumerate(calls)
        if "|logs --timestamps --tail 200 cid-redis" in call
    )
    assert not any(
        "|logs --timestamps --tail 200 cid-rsshub" in call for call in calls
    )
    rollback_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith(f"{OLD_IMAGE_ID}|")
        and (
            f"-f {registry}/manifests/{old_hash}.compose.yml up -d"
            in call
        )
    )
    assert inspect_index < logs_index < rollback_index
    assert (registry / "current.ref").read_text().strip() == old_ref


@pytest.mark.parametrize("test_body_raises", [False, True])
def test_release_env_teardown_allows_sealed_diagnostics_cleanup(
    tmp_path, test_body_raises
):
    fixture = release_env.__wrapped__(tmp_path)
    _env, _log, registry, _old_manifest = next(fixture)
    outside_registry = tmp_path / "outside-registry"
    outside_registry.mkdir(mode=0o500)
    try:
        run_dir = registry / "diagnostics" / "test-sealed-run"
        service_dir = run_dir / "services" / "000-redis"
        service_dir.mkdir(parents=True)
        (run_dir / "external-link").symlink_to(outside_registry, target_is_directory=True)
        artifact = service_dir / "state.json"
        content = b'{"status":"test-only"}\n'
        artifact.write_bytes(content)
        artifact.chmod(0o400)
        directories = [run_dir, service_dir.parent, service_dir]
        for directory in directories:
            directory.chmod(0o500)

        if test_body_raises:
            with pytest.raises(RuntimeError, match="original test failure"):
                fixture.throw(RuntimeError("original test failure"))
        else:
            with pytest.raises(StopIteration):
                next(fixture)

        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o400
        assert artifact.read_bytes() == content
        assert stat.S_IMODE(outside_registry.stat().st_mode) == 0o500
        # No privileged cleanup or onerror handler: this must work on CI's
        # ordinary user, including when the test body originally failed.
        shutil.rmtree(registry)
        assert not registry.exists()
    finally:
        fixture.close()
        outside_registry.chmod(0o700)


def test_health_diagnostics_helper_failure_preserves_recovery(release_env):
    env, _log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    diagnostics_blocker = registry / "diagnostics"
    diagnostics_blocker.write_text("block helper output\n", encoding="utf-8")
    env["RELEASE_CAPTURE_HEALTH_DIAGNOSTICS"] = "1"
    env["FAKE_FAIL_REF"] = IMAGE_ID

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 4
    assert "Health diagnostics capture failed" in result.stderr
    assert (registry / "current.ref").read_text().strip() == old_ref
    history = (registry / "history.tsv").read_text(encoding="utf-8")
    assert "compose_failed" in history
    assert "auto-rollback" in history


def test_health_failed_version_ref_cannot_change_digest_on_retry(release_env):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.9"
    env["FAKE_FAIL_REF"] = IMAGE_ID

    first = _run_release(env, "deploy", ref)

    assert first.returncode != 0
    assert "compose_failed" in (registry / "history.tsv").read_text()

    env.pop("FAKE_FAIL_REF")
    env["FAKE_IMAGE_ID"] = "sha256:" + "b" * 64
    log.write_text("", encoding="utf-8")

    retry = _run_release(env, "deploy", ref)

    assert retry.returncode != 0
    assert "Immutable ref changed digest" in retry.stderr
    assert " up " not in log.read_text()


def test_conflicting_history_digests_fail_closed_before_compose_up(release_env):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.9"
    env["FAKE_FAIL_REF"] = IMAGE_ID
    first = _run_release(env, "deploy", ref)
    assert first.returncode != 0

    with (registry / "history.tsv").open("a", encoding="utf-8") as history:
        history.write(
            f"2026-07-13T00:00:00Z\tdeploy\t{ref}\tsha256:{'b' * 64}\thealth_failed\tmanifest\tservices\n"
        )
    env.pop("FAKE_FAIL_REF")
    env["FAKE_IMAGE_ID"] = "sha256:" + "b" * 64
    log.write_text("", encoding="utf-8")

    retry = _run_release(env, "deploy", ref)

    assert retry.returncode != 0
    assert "Conflicting release history digests" in retry.stderr
    assert " up " not in log.read_text()


def test_compose_failure_automatically_restores_previous_manifest(release_env):
    env, log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    old_hash = _hash(old_manifest)
    new_ref = "my-ai-filter:2026.07.12.1"
    log.write_text("", encoding="utf-8")
    env["FAKE_COMPOSE_FAIL_REF"] = IMAGE_ID

    result = _run_release(env, "deploy", new_ref)

    assert result.returncode != 0
    calls = log.read_text()
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml up -d --no-build --pull never --remove-orphans" in calls
    assert not any(
        line.startswith((f"{new_ref}|", f"{old_ref}|"))
        and (" up " in line or " ps " in line)
        for line in calls.splitlines()
    )
    assert "compose_failed" in (registry / "history.tsv").read_text()


def test_gateway_recreate_failure_stops_before_worker_and_full_reconcile(
    release_env,
):
    env, log, registry, _old_manifest = release_env
    env["FAKE_GATEWAY_RECREATE_FAIL"] = "1"

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    calls = log.read_text()
    assert "--force-recreate ai-filter-web" in calls
    assert "--force-recreate ai-filter-worker" not in calls
    assert "up -d --no-build --pull never --remove-orphans" not in calls
    assert "compose_failed" in (registry / "history.tsv").read_text()


@pytest.mark.parametrize(
    ("failure_variable", "expected_status"),
    [
        ("FAKE_COMPOSE_FAIL_REF", "compose_failed"),
        ("FAKE_FAIL_REF", "compose_failed"),
    ],
)
def test_disabled_auto_rollback_never_starts_previous_release(
    release_env, failure_variable, expected_status
):
    env, log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    old_hash = _hash(old_manifest)
    log.write_text("", encoding="utf-8")
    env["RELEASE_AUTO_ROLLBACK"] = "0"
    env[failure_variable] = IMAGE_ID

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 4
    assert "recovery to the maintenance caller" in result.stderr
    calls = log.read_text()
    old_up = (
        f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} "
        f"-f {registry}/manifests/{old_hash}.compose.yml up -d"
    )
    assert old_up not in calls
    assert (registry / "current.ref").read_text().strip() == old_ref
    history = (registry / "history.tsv").read_text()
    assert expected_status in history
    assert "auto-rollback" not in history


@pytest.mark.parametrize("value", ["false", "no", "00", "2", "-1"])
def test_invalid_auto_rollback_value_fails_before_docker(release_env, value):
    env, log, _registry, _old_manifest = release_env
    env["RELEASE_AUTO_ROLLBACK"] = value

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 2
    assert "exactly 0 or 1" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("value", ["false", "yes", "00", "2", "-1"])
def test_invalid_health_diagnostics_value_fails_before_docker(
    release_env, value
):
    env, log, _registry, _old_manifest = release_env
    env["RELEASE_CAPTURE_HEALTH_DIAGNOSTICS"] = value

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 2
    assert "RELEASE_CAPTURE_HEALTH_DIAGNOSTICS" in result.stderr
    assert "exactly 0 or 1" in result.stderr
    assert not log.exists()


def test_health_diagnostics_require_isolated_release_paths(release_env):
    env, log, _registry, _old_manifest = release_env
    env["RELEASE_CAPTURE_HEALTH_DIAGNOSTICS"] = "1"
    env.pop("AI_FILTER_RELEASE_ISOLATION_ROOT")
    env.pop("AI_FILTER_MAPPING_MIGRATION_GATE_FILE")
    env.pop("AI_FILTER_MAPPING_MIGRATION_STATE_FILE")

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 2
    assert "only inside an isolated release rehearsal" in result.stderr
    assert not log.exists()


def test_diagnostics_helper_argument_error_does_not_echo_values():
    secret = "OPAQUE_PARSE_SECRET"

    result = subprocess.run(
        [
            "python3",
            "-I",
            str(DIAGNOSTICS_HELPER),
            "--unknown",
            secret,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "invalid diagnostic capture arguments" in result.stderr


def test_bootstrap_requires_previous_manifest_and_health_services_before_up(release_env):
    env, log, registry, _old_manifest = release_env
    registry.mkdir()
    (registry / "current.ref").write_text("my-ai-filter:2026.07.11.2\n", encoding="utf-8")
    (registry / "current.digest").write_text(f"{IMAGE_ID}\n", encoding="utf-8")

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "PREVIOUS_COMPOSE_FILE" in result.stderr
    assert not log.exists()
    assert not (registry / "current.manifest").exists()


def test_bootstrap_old_manifest_enables_real_automatic_rollback(release_env):
    env, log, registry, old_manifest = release_env
    old_ref = "my-ai-filter:2026.07.11.2"
    new_ref = "my-ai-filter:2026.07.12.1"
    registry.mkdir()
    (registry / "current.ref").write_text(f"{old_ref}\n", encoding="utf-8")
    (registry / "current.digest").write_text(f"{OLD_IMAGE_ID}\n", encoding="utf-8")
    env["FAKE_ALT_IMAGE_REF"] = old_ref
    env["FAKE_ALT_IMAGE_ID"] = OLD_IMAGE_ID
    env["PREVIOUS_COMPOSE_FILE"] = str(old_manifest)
    env["PREVIOUS_HEALTH_SERVICES"] = OLD_HEALTH_SERVICES
    env["FAKE_FAIL_REF"] = IMAGE_ID

    result = _run_release(env, "deploy", new_ref)

    assert result.returncode != 0
    old_hash = _hash(old_manifest)
    assert (registry / "current.manifest").read_text().strip() == old_hash
    assert (registry / "current.health-services").read_text().strip() == OLD_HEALTH_SERVICES
    calls = log.read_text()
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml ps --all -q webhook-worker" in calls
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml up -d --no-build --pull never --remove-orphans" in calls


def test_successful_deploy_rotates_all_current_fields_to_previous(release_env):
    env, _log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    old_hash = _hash(old_manifest)
    new_ref = "my-ai-filter:2026.07.12.1"

    result = _run_release(env, "deploy", new_ref)

    assert result.returncode == 0, result.stderr
    assert (registry / "previous.ref").read_text().strip() == old_ref
    assert (registry / "previous.digest").read_text().strip() == OLD_IMAGE_ID
    assert (registry / "previous.manifest").read_text().strip() == old_hash
    assert (registry / "previous.health-services").read_text().strip() == OLD_HEALTH_SERVICES
    assert (registry / "current.ref").read_text().strip() == new_ref
    assert (registry / "current.manifest").read_text().strip() == _hash(PROJECT_ROOT / "docker-compose.yml")
    assert (registry / "current.health-services").read_text().strip() == NEW_HEALTH_SERVICES


def test_manual_rollback_uses_recorded_old_manifest_and_health_set(release_env):
    env, log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    new_ref = "my-ai-filter:2026.07.12.1"
    result = _run_release(env, "deploy", new_ref)
    assert result.returncode == 0, result.stderr
    log.write_text("", encoding="utf-8")

    result = _run_release(env, "rollback", old_ref)

    assert result.returncode == 0, result.stderr
    old_hash = _hash(old_manifest)
    calls = log.read_text()
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml up -d --no-build --pull never --remove-orphans" in calls
    assert f"{OLD_IMAGE_ID}|compose --project-directory {PROJECT_ROOT} -f {registry}/manifests/{old_hash}.compose.yml ps --all -q webhook-worker" in calls
    assert not any(
        line.startswith(f"{old_ref}|") and (" up " in line or " ps " in line)
        for line in calls.splitlines()
    )
    assert (registry / "current.ref").read_text().strip() == old_ref
    assert (registry / "current.digest").read_text().strip() == OLD_IMAGE_ID


def test_legacy_config_bind_is_sealed_before_becoming_previous(release_env, tmp_path):
    env, log, registry, _old_manifest = release_env
    legacy_manifest = tmp_path / "legacy-bind-compose.yml"
    legacy_manifest.write_text(
        """services:
  ai-filter-web:
    image: ${IMAGE_REF:?}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
  redis:
    image: redis:7
  webhook-worker:
    image: ${IMAGE_REF:?}
  rsshub:
    image: rsshub:1
""",
        encoding="utf-8",
    )
    old_ref = _deploy_old(env, legacy_manifest)
    original_config = (PROJECT_ROOT / "config.yaml").read_bytes()

    sealed = _run_release(env, "seal-current", old_ref)
    assert sealed.returncode == 0, sealed.stderr

    deployed = _run_release(env, "deploy", "my-ai-filter:2026.07.12.87")
    assert deployed.returncode == 0, deployed.stderr
    sealed_hash = (registry / "previous.manifest").read_text().strip()
    assert sealed_hash != _hash(legacy_manifest)
    sealed_manifest = registry / "manifests" / f"{sealed_hash}.compose.yml"
    sealed_text = sealed_manifest.read_text(encoding="utf-8")
    assert "./config.yaml:/app/config.yaml" not in sealed_text
    match = re.search(r"'([^']+):/app/config\.yaml:ro'", sealed_text)
    assert match is not None
    snapshot = Path(match.group(1))
    assert snapshot.is_absolute()
    assert snapshot.parent == registry / "configs"
    assert snapshot.read_bytes() == original_config
    assert snapshot.stat().st_mode & 0o777 == 0o600

    try:
        (PROJECT_ROOT / "config.yaml").write_bytes(
            original_config + b"\n# host config changed after cutover\n"
        )
        log.write_text("", encoding="utf-8")
        rolled_back = _run_release(env, "rollback", old_ref)
        assert rolled_back.returncode == 0, rolled_back.stderr
        assert snapshot.read_bytes() == original_config
        assert f"-f {sealed_manifest} up -d --no-build" in log.read_text()
        assert (registry / "current.manifest").read_text().strip() == sealed_hash
    finally:
        (PROJECT_ROOT / "config.yaml").write_bytes(original_config)


def test_fallback_seal_updates_current_state_before_new_preflight_failure(
    release_env, tmp_path
):
    env, log, registry, _old_manifest = release_env
    legacy_manifest = tmp_path / "legacy-failure-compose.yml"
    legacy_manifest.write_text(
        """services:
  ai-filter-web:
    image: ${IMAGE_REF:?}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
  redis:
    image: redis:7
  webhook-worker:
    image: ${IMAGE_REF:?}
  rsshub:
    image: rsshub:1
""",
        encoding="utf-8",
    )
    old_ref = _deploy_old(env, legacy_manifest)
    old_hash = _hash(legacy_manifest)
    log.write_text("", encoding="utf-8")
    env["FAKE_CONFIG_FAIL_REF"] = IMAGE_ID

    failed = _run_release(env, "deploy", "my-ai-filter:2026.07.12.88")

    assert failed.returncode != 0
    assert "fallback-sealed current config" in failed.stderr
    assert (registry / "current.ref").read_text().strip() == old_ref
    sealed_hash = (registry / "current.manifest").read_text().strip()
    assert sealed_hash != old_hash
    assert (registry / "current.state").read_text().splitlines()[2] == sealed_hash
    sealed_text = (
        registry / "manifests" / f"{sealed_hash}.compose.yml"
    ).read_text(encoding="utf-8")
    assert "./config.yaml:/app/config.yaml" not in sealed_text
    assert "fallback_sealed" in (registry / "history.tsv").read_text()
    assert " up -d " not in log.read_text()


def test_preflight_failure_never_changes_containers(release_env):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.1"
    env["FAKE_CONFIG_FAIL_REF"] = IMAGE_ID

    result = _run_release(env, "deploy", ref)

    assert result.returncode != 0
    assert "config --quiet" in log.read_text()
    assert " up " not in log.read_text()
    assert "preflight_failed" in (registry / "history.tsv").read_text()


def test_gateway_mounted_nginx_preflight_failure_never_changes_containers(release_env):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.1"
    env["FAKE_GATEWAY_PREFLIGHT_FAIL"] = "1"

    result = _run_release(env, "deploy", ref)

    assert result.returncode != 0
    calls = log.read_text()
    assert "run --rm --no-deps ai-filter-web nginx -t" in calls
    assert " up " not in calls
    assert "preflight_failed" in (registry / "history.tsv").read_text()


def test_old_archived_manifest_skips_gateway_specific_preflight(release_env):
    env, log, _registry, old_manifest = release_env
    env["RELEASE_COMPOSE_FILE"] = str(old_manifest)
    env["RELEASE_HEALTH_SERVICES"] = OLD_HEALTH_SERVICES
    env["FAKE_GATEWAY_PREFLIGHT_FAIL"] = "1"

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.11.2")

    assert result.returncode == 0, result.stderr
    assert " run --rm --no-deps " not in log.read_text()


def test_gateway_preflight_failure_blocks_build_before_image_build(release_env):
    env, log, _registry, _old_manifest = release_env
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"
    env["FAKE_GATEWAY_PREFLIGHT_FAIL"] = "1"

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.8")

    assert result.returncode != 0
    calls = log.read_text()
    assert "run --rm --no-deps ai-filter-web nginx -t" in calls
    assert not any("|build " in line for line in calls.splitlines())


def test_build_uses_private_snapshot_and_archived_manifest(release_env, tmp_path):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:v2026.07.12.1"
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"
    context_record = tmp_path / "context-record"
    env["FAKE_CONTEXT_RECORD"] = str(context_record)
    env["FAKE_REQUIRE_CONTEXT_FILE"] = "services/backtest.py"

    result = _run_release(env, "build", ref)

    assert result.returncode == 0, result.stderr
    manifest_hash = _hash(PROJECT_ROOT / "docker-compose.yml")
    build_call = next(
        line for line in log.read_text().splitlines() if "|build " in line
    )
    assert build_call.startswith(f"{ref}|build --tag {ref} ")
    assert f"--build-arg AI_FILTER_BUILD_VERSION={ref}" in build_call
    assert "--build-arg AI_FILTER_BUILD_GIT_SHA=" in build_call
    assert f"--build-arg AI_FILTER_BUILD_MANIFEST_SHA={manifest_hash}" in build_call
    assert "--build-arg AI_FILTER_BUILD_CONFIG_SHA=" in build_call
    assert "--build-arg AI_FILTER_BUILD_SOURCE_ID=" in build_call
    context, mode, snapshot_config_hash = context_record.read_text().splitlines()
    assert Path(context) != PROJECT_ROOT
    assert mode == "700"
    assert snapshot_config_hash == _hash(PROJECT_ROOT / "config.yaml")
    assert build_call.endswith(f" {context}")
    assert "\tbuild\t" in (registry / "history.tsv").read_text()


def test_deploy_requires_a_matching_local_build_contract(release_env):
    env, log, registry, _old_manifest = release_env
    env["RELEASE_REQUIRE_BUILD_CONTRACT"] = "1"

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.81")

    assert result.returncode != 0
    assert "No trusted build contract" in result.stderr
    assert " up -d " not in log.read_text()
    assert "preflight_failed" in (registry / "history.tsv").read_text()


def test_build_contract_binds_image_manifest_config_and_source(
    release_env, tmp_path
):
    env, _log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.82"
    env["RELEASE_REQUIRE_BUILD_CONTRACT"] = "1"
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"

    built = _run_release(env, "build", ref)
    assert built.returncode == 0, built.stderr
    contracts = list((registry / "build-contracts").glob("*.state"))
    assert len(contracts) == 1
    assert contracts[0].stat().st_mode & 0o777 == 0o600
    contract = contracts[0].read_text(encoding="utf-8").splitlines()
    assert contract[:3] == [ref, IMAGE_ID, _hash(PROJECT_ROOT / "docker-compose.yml")]
    assert len(contract[3]) == 64
    source_head, source_state = contract[4].split(":", 1)
    assert len(source_head) in {40, 64}
    assert source_state == "clean" or len(source_state) == 64

    _install_completed_mapping_gate(env, ref)
    deployed = _run_release(env, "deploy", ref)
    assert deployed.returncode == 0, deployed.stderr

    changed_manifest = tmp_path / "changed-compose.yml"
    changed_manifest.write_bytes(
        (PROJECT_ROOT / "docker-compose.yml").read_bytes() + b"\n# drift\n"
    )
    env["RELEASE_COMPOSE_FILE"] = str(changed_manifest)
    rejected = _run_release(env, "deploy", ref)
    assert rejected.returncode != 0
    assert "Build/deploy contract mismatch" in rejected.stderr

    env.pop("RELEASE_COMPOSE_FILE")
    config_path = PROJECT_ROOT / "config.yaml"
    original_config = config_path.read_bytes()
    try:
        config_path.write_bytes(original_config + b"\n# drift\n")
        rejected = _run_release(env, "deploy", ref)
        assert rejected.returncode != 0
        assert "Build/deploy contract mismatch" in rejected.stderr
    finally:
        config_path.write_bytes(original_config)

    marker = _create_dirty_marker(tmp_path)
    try:
        rejected = _run_release(env, "deploy", ref)
        assert rejected.returncode != 0
        assert "Build/deploy contract mismatch" in rejected.stderr
    finally:
        marker.unlink(missing_ok=True)


def test_build_rejects_ref_recorded_in_history_even_when_image_is_missing(
    release_env,
):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.83"
    registry.mkdir()
    (registry / "history.tsv").write_text(
        f"2026-07-12T00:00:00Z\tbuild\t{ref}\t{IMAGE_ID}\tbuilt\t"
        f"{'1' * 64}\t{NEW_HEALTH_SERVICES}\n",
        encoding="utf-8",
    )
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"

    result = _run_release(env, "build", ref)

    assert result.returncode != 0
    assert "previously registered immutable version" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    "bad_label",
    [
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "io.ai-filter.compose-manifest.sha256",
        "io.ai-filter.config.sha256",
        "io.ai-filter.source.identity",
        "io.ai-filter.event-mapping-generation",
    ],
)
def test_build_rejects_mismatched_oci_contract_label(release_env, bad_label):
    env, _log, registry, _old_manifest = release_env
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"
    env["FAKE_BAD_LABEL_KEY"] = bad_label

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.84")

    assert result.returncode != 0
    assert bad_label in result.stderr
    assert not list((registry / "build-contracts").glob("*.state"))
    assert "contract_failed" in (registry / "history.tsv").read_text()


def test_build_rejects_image_config_hash_mismatch(release_env):
    env, _log, registry, _old_manifest = release_env
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"
    env["FAKE_BAD_IMAGE_CONFIG"] = "1"

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.85")

    assert result.returncode != 0
    assert "/app/config.yaml hash mismatch" in result.stderr
    assert not list((registry / "build-contracts").glob("*.state"))
    assert "contract_failed" in (registry / "history.tsv").read_text()


def test_contract_enabled_deploy_and_rollback_revalidate_image_contract(release_env):
    env, log, _registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.86"
    env["RELEASE_REQUIRE_BUILD_CONTRACT"] = "1"
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"

    built = _run_release(env, "build", ref)
    _install_completed_mapping_gate(env, ref)
    deployed = _run_release(env, "deploy", ref)
    rolled_back = _run_release(env, "rollback", ref)

    assert built.returncode == 0, built.stderr
    assert deployed.returncode == 0, deployed.stderr
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert log.read_text().count(" up -d --no-build --pull never --remove-orphans") == 2
    assert log.read_text().count("io.ai-filter.source.identity") >= 3


def test_verify_revalidates_all_contract_labels_and_embedded_config(release_env):
    env, log, _registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.07.12.89"
    env["FAKE_MISSING_UNTIL_BUILD"] = "1"
    built = _run_release(env, "build", ref)
    assert built.returncode == 0, built.stderr
    log.write_text("", encoding="utf-8")

    verified = _run_release(env, "verify", ref)

    assert verified.returncode == 0, verified.stderr
    calls = log.read_text()
    for label in (
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "io.ai-filter.compose-manifest.sha256",
        "io.ai-filter.config.sha256",
        "io.ai-filter.source.identity",
        "io.ai-filter.event-mapping-generation",
    ):
        assert label in calls
    assert "|run --rm --network none --read-only --cap-drop ALL" in calls
    assert "/app/config.yaml" in calls

    env["FAKE_BAD_LABEL_KEY"] = "io.ai-filter.source.identity"
    rejected_label = _run_release(env, "verify", ref)
    assert rejected_label.returncode != 0
    assert "io.ai-filter.source.identity" in rejected_label.stderr

    env.pop("FAKE_BAD_LABEL_KEY")
    env["FAKE_BAD_IMAGE_CONFIG"] = "1"
    rejected_config = _run_release(env, "verify", ref)
    assert rejected_config.returncode != 0
    assert "/app/config.yaml hash mismatch" in rejected_config.stderr


def test_build_refuses_to_overwrite_existing_version(release_env):
    env, log, _registry, _old_manifest = release_env

    result = _run_release(env, "build", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "compose" not in log.read_text()


def test_release_lock_rejects_concurrent_commands(release_env):
    env, log, registry, _old_manifest = release_env
    registry.mkdir()
    lock_path = registry / "release.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 6
    assert "already running" in result.stderr
    assert "flock exit=75" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("value", ["", "-1", "301", "1.5", "abc", "01", "9" * 100])
def test_release_rejects_invalid_lock_wait_before_docker(release_env, value):
    env, log, _registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = value

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 2
    assert "integer from 0 through 300" in result.stderr
    assert not log.exists()


@pytest.mark.skipif(os.uname().sysname != "Linux", reason="requires real Linux flock")
@pytest.mark.parametrize("value", ["0", "30", "300"])
def test_release_lock_wait_accepts_bounds_when_free(release_env, value):
    env, _log, _registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = value

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.uname().sysname != "Linux", reason="requires real Linux flock")
def test_release_waits_for_shared_lock_then_executes_once(release_env):
    env, log, registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = "3"
    registry.mkdir()
    with (registry / "release.lock").open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        with subprocess.Popen(
            [env["RELEASE_TEST_SCRIPT"], "deploy", "my-ai-filter:2026.07.12.1"],
            cwd=PROJECT_ROOT, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                time.sleep(0.3)
                assert process.poll() is None
                assert not log.exists()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, stderr
    assert stdout.count("deploy: my-ai-filter:2026.07.12.1") == 1


@pytest.mark.skipif(os.uname().sysname != "Linux", reason="requires real Linux flock")
def test_release_keeps_lock_while_docker_is_running(release_env, tmp_path):
    env, _log, registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = "3"
    bin_dir = Path(env["PATH"].split(os.pathsep)[0])
    docker = bin_dir / "docker"
    original = bin_dir / "fixture-docker"
    docker.rename(original)
    entered, resume = tmp_path / "docker-entered", tmp_path / "docker-resume"
    env.update({"FAKE_LOCK_ENTERED": str(entered), "FAKE_LOCK_RESUME": str(resume),
                "FAKE_LOCK_DOCKER": str(original)})
    docker.write_text(
        '#!/bin/sh\nif [ ! -e "$FAKE_LOCK_ENTERED" ]; then\n'
        '  touch "$FAKE_LOCK_ENTERED"\n'
        '  while [ ! -e "$FAKE_LOCK_RESUME" ]; do sleep 0.01; done\nfi\n'
        'exec "$FAKE_LOCK_DOCKER" "$@"\n'
    )
    docker.chmod(0o755)
    with subprocess.Popen(
        [env["RELEASE_TEST_SCRIPT"], "deploy", "my-ai-filter:2026.07.12.1"],
        cwd=PROJECT_ROOT, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            deadline = time.monotonic() + 3
            while not entered.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.01)
            assert entered.exists()
            with (registry / "release.lock").open("a") as other_file:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(other_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            resume.touch()
        _stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, stderr


@pytest.mark.skipif(os.uname().sysname != "Linux", reason="requires real Linux flock")
def test_release_lock_timeout_does_not_enter_docker(release_env):
    env, log, registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = "1"
    registry.mkdir()
    with (registry / "release.lock").open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        started = time.monotonic()
        result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")
        elapsed = time.monotonic() - started

    assert result.returncode == 6
    assert "Timed out waiting" in result.stderr
    assert "flock exit=75; wait=1s" in result.stderr
    assert 0.8 <= elapsed < 5
    assert not log.exists()


@pytest.mark.parametrize("value", ["0", "1"])
def test_release_lock_execution_error_keeps_original_reason(release_env, value):
    env, log, _registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = value
    flock = Path(env["PATH"].split(os.pathsep)[0]) / "flock"
    flock.write_text("#!/bin/sh\nprintf 'lock subsystem unavailable\\n' >&2\nexit 69\n")
    flock.chmod(0o755)

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 6
    assert "flock exit=69" in result.stderr
    assert "lock subsystem unavailable" in result.stderr
    assert "already running" not in result.stderr
    assert "Timed out" not in result.stderr
    assert not log.exists()


def test_release_accepts_prelocked_inherited_release_fd(release_env):
    env, _log, registry, _old_manifest = release_env
    env["RELEASE_LOCK_WAIT_SECONDS"] = "30"
    registry.mkdir()
    lock_path = registry / "release.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited_fd = fcntl.fcntl(lock_file.fileno(), fcntl.F_DUPFD, 10)
        try:
            env["RELEASE_LOCK_FD"] = str(inherited_fd)
            result = _run_release(
                env,
                "deploy",
                "my-ai-filter:2026.07.12.1",
                pass_fds=(inherited_fd,),
            )
        finally:
            os.close(inherited_fd)

    assert result.returncode == 0, result.stderr


def test_release_rejects_inherited_fd_for_a_different_file(release_env, tmp_path):
    env, log, registry, _old_manifest = release_env
    registry.mkdir()
    (registry / "release.lock").touch()
    other_path = tmp_path / "not-release.lock"
    with other_path.open("w") as other_file:
        fcntl.flock(other_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited_fd = fcntl.fcntl(other_file.fileno(), fcntl.F_DUPFD, 10)
        try:
            env["RELEASE_LOCK_FD"] = str(inherited_fd)
            result = _run_release(
                env,
                "deploy",
                "my-ai-filter:2026.07.12.1",
                pass_fds=(inherited_fd,),
            )
        finally:
            os.close(inherited_fd)

    assert result.returncode == 6
    assert "does not refer" in result.stderr
    assert not log.exists()


def test_release_rejects_unlocked_inherited_release_fd(release_env):
    env, log, registry, _old_manifest = release_env
    registry.mkdir()
    lock_path = registry / "release.lock"
    with lock_path.open("w") as lock_file:
        inherited_fd = fcntl.fcntl(lock_file.fileno(), fcntl.F_DUPFD, 10)
        try:
            env["RELEASE_LOCK_FD"] = str(inherited_fd)
            result = _run_release(
                env,
                "deploy",
                "my-ai-filter:2026.07.12.1",
                pass_fds=(inherited_fd,),
            )
        finally:
            os.close(inherited_fd)

    assert result.returncode == 6
    assert "does not already hold" in result.stderr
    assert not log.exists()


def test_incomplete_previous_state_fails_closed_before_up(release_env):
    env, log, registry, old_manifest = release_env
    _deploy_old(env, old_manifest)
    (registry / "previous.ref").write_text("my-ai-filter:2026.07.10.1\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "Incomplete previous release state" in result.stderr
    assert " up " not in log.read_text()


def test_corrupt_atomic_state_bundle_fails_closed_even_if_mirrors_exist(release_env):
    env, log, registry, old_manifest = release_env
    _deploy_old(env, old_manifest)
    (registry / "current.state").write_text("truncated\nstate\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "PREVIOUS_COMPOSE_FILE" in result.stderr
    assert " up " not in log.read_text()


def test_tampered_archived_manifest_fails_closed_before_up(release_env):
    env, log, registry, old_manifest = release_env
    _deploy_old(env, old_manifest)
    manifest_hash = _hash(old_manifest)
    archived = registry / "manifests" / f"{manifest_hash}.compose.yml"
    archived.write_text(archived.read_text() + "# tampered\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "manifest was modified" in result.stderr
    assert " up " not in log.read_text()


def test_tampered_atomic_state_digest_fails_closed_before_up(release_env):
    env, log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    manifest_hash = _hash(old_manifest)
    (registry / "current.state").write_text(
        f"{old_ref}\nsha256:{'c' * 64}\n{manifest_hash}\n{OLD_HEALTH_SERVICES}\n",
        encoding="utf-8",
    )
    log.write_text("", encoding="utf-8")

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "Recorded release digest mismatch" in result.stderr
    assert " up " not in log.read_text()


def test_every_compose_call_has_project_directory_and_manifest(release_env):
    env, log, _registry, _old_manifest = release_env

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode == 0, result.stderr
    for line in log.read_text().splitlines():
        if "|compose " in line:
            assert "compose --project-directory" in line
            assert " -f " in line


def test_release_dockerfile_and_ci_share_exact_contract_label_names():
    expected = {
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "io.ai-filter.compose-manifest.sha256",
        "io.ai-filter.config.sha256",
        "io.ai-filter.source.identity",
        "io.ai-filter.event-mapping-generation",
        "io.ai-filter.private-release.sha256",
        "io.ai-filter.runtime-config.sha256",
    }
    release = RELEASE_SCRIPT.read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    release_keys_block = release.split("local -a keys=(", 1)[1].split("  )", 1)[0]
    release_labels = set(re.findall(r'"([^"\n]+)"', release_keys_block))
    release_labels.update(re.findall(r'keys\+=\("([^"\n]+)"\)', release))
    dockerfile_labels = set(
        re.findall(r"^LABEL ([^=\s]+)=", dockerfile, flags=re.MULTILINE)
    )
    ci_labels = set(re.findall(r"assert_image_label ([^\s]+) ", workflow))

    assert release_labels == expected
    assert dockerfile_labels == expected
    assert ci_labels == expected


@pytest.mark.parametrize("override", [
    {"RELEASE_ALLOW_DIRTY_TREE": "1"},
    {"RELEASE_REQUIRE_BUILD_CONTRACT": "0"},
    {"RELEASE_AUTO_ROLLBACK": "1"},
    {"RELEASE_COMPOSE_FILE": "/private/unreviewed-compose.json"},
])
def test_private_release_rejects_existing_escape_hatches_before_docker(release_env, override):
    env, log, _registry, _old_manifest = release_env
    env.update({"RELEASE_PRIVATE_CONTRACT": "/private/missing-contract.json",
                "RELEASE_ALLOW_DIRTY_TREE": "0", "RELEASE_REQUIRE_BUILD_CONTRACT": "1",
                "RELEASE_AUTO_ROLLBACK": "0", **override})
    result = _run_release(env, "build", "my-ai-filter:2026.10.26.1")
    assert result.returncode == 8
    assert "Private release requires clean source" in result.stderr
    assert not log.exists()


def test_private_build_contract_cannot_be_verified_as_public_release(release_env):
    env, log, registry, _old_manifest = release_env
    ref = "my-ai-filter:2026.10.26.2"
    contracts = registry / "build-contracts"
    contracts.mkdir(parents=True)
    key = hashlib.sha256(ref.encode()).hexdigest()
    (contracts / f"{key}.state").write_text("\n".join([
        ref, IMAGE_ID, "a" * 64, "b" * 64, "c" * 40 + ":clean",
        "event-mapping-store-v5-provider-attribution-v1", "d" * 64,
    ]) + "\n")
    result = _run_release(env, "verify", ref)
    assert result.returncode == 3
    assert "Private build contract is missing or mismatched" in result.stderr
    assert " up " not in log.read_text()


def test_compose_and_ci_forbid_latest_and_target_master():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "AI_FILTER_IMAGE_TAG" not in compose
    assert "my-ai-filter:latest" not in compose
    image_services = set(
        re.findall(
            r"^  ([a-z0-9-]+):\n(?:(?:    .*|\s*)\n)*?"
            r"    image: \$\{IMAGE_REF:\?",
            compose,
            flags=re.MULTILINE,
        )
    )
    assert image_services == {
        "ai-filter-api",
        "ai-filter-worker",
        "market-close-collector",
        "event-enrichment",
        "security-master-sync",
    }
    assert "branches: [master]" in workflow
    assert "branches: [main]" not in workflow


def test_runtime_source_drift_fails_deploy_and_rolls_back(release_env):
    env, _log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    env["FAKE_SOURCE_DRIFT_SERVICE"] = "ai-filter-worker"

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "Runtime source/config drift detected" in result.stderr
    assert (registry / "current.ref").read_text().strip() == old_ref


def test_runtime_config_drift_fails_deploy_and_rolls_back(release_env):
    env, _log, registry, old_manifest = release_env
    old_ref = _deploy_old(env, old_manifest)
    env["FAKE_CONFIG_DRIFT_SERVICE"] = "ai-filter-worker"

    result = _run_release(env, "deploy", "my-ai-filter:2026.07.12.1")

    assert result.returncode != 0
    assert "Runtime source/config drift detected" in result.stderr
    assert (registry / "current.ref").read_text().strip() == old_ref
