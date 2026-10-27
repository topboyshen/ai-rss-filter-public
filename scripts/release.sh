#!/usr/bin/env bash
# Immutable, manifest-aware release helper for AI-Filter.
#
# A release state is the tuple:
#   image ref + image digest + archived Compose manifest + health services.
# Keeping the manifest and health set with the image is essential: an older image
# may have different service names, commands, and health checks.
#
# One-time migration when current.ref already exists without manifest metadata:
#   PREVIOUS_COMPOSE_FILE=/path/to/old-compose.yml \
#   PREVIOUS_HEALTH_SERVICES='redis rsshub ai-filter-web webhook-worker' \
#     ./scripts/release.sh deploy my-ai-filter:2026.07.13.1
#
# Schema-maintenance callers may keep the release lock across the database and
# application cutover by passing an already-locked, inherited descriptor:
#   RELEASE_LOCK_FD=71 RELEASE_AUTO_ROLLBACK=0 ./scripts/release.sh deploy ...
# The descriptor must refer to this registry's release.lock and must already
# hold its flock. RELEASE_AUTO_ROLLBACK accepts only 0 or 1; it defaults to 1.
#
# Build and deploy fail closed when Git has tracked or untracked changes. A
# deploy-only emergency exception must be both explicit and auditable:
#   RELEASE_ALLOW_DIRTY_TREE=1 \
#   RELEASE_DIRTY_TREE_OVERRIDE_REASON='INC-1234: emergency fix' \
#     ./scripts/release.sh deploy ...
# Accepted deploy exceptions are fingerprinted in data/releases/dirty-overrides.tsv.
# Builds are stricter: they require a clean HEAD and a recent full-suite test
# attestation produced by scripts/run-release-gate.sh. Build and deploy are
# additionally joined by a local, mode-0600 build contract that binds the
# runtime tuple to the exact config and source identity.
# RELEASE_LOCK_WAIT_SECONDS accepts 0..300 (default 0). Set it explicitly for
# bounded lock waiting; a timeout never retries a build or deployment.
#
# An explicitly approved isolated rehearsal can preserve bounded health-timeout
# evidence before rollback with RELEASE_CAPTURE_HEALTH_DIAGNOSTICS=1. The
# default is 0 so production releases do not begin collecting logs silently.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGISTRY_DIR="${RELEASE_REGISTRY_DIR:-${ROOT}/data/releases}"
MANIFEST_DIR="${REGISTRY_DIR}/manifests"
BUILD_CONTRACT_DIR="${REGISTRY_DIR}/build-contracts"
CONFIG_SNAPSHOT_DIR="${REGISTRY_DIR}/configs"
COMPOSE_FILE="${RELEASE_COMPOSE_FILE:-${ROOT}/docker-compose.yml}"
# Dockerfile COPY is intentionally fixed to this path. Allowing an alternate
# hash input would let the build contract describe a config absent from image.
CONFIG_FILE="${ROOT}/config.yaml"
# A private contract changes deployment inputs only, never Docker build context.
PRIVATE_CONTRACT="${RELEASE_PRIVATE_CONTRACT:-}"
PRIVATE_CONTRACT_SHA=""
PRIVATE_RUNTIME_CONFIG_SHA=""
PRIVATE_HELPER="${ROOT}/scripts/private_release_contract.py"
private_contract() {
  python3 -I "$PRIVATE_HELPER" --root "$ROOT" --contract "$PRIVATE_CONTRACT" "$@"
}
release_compose() {
  if [[ -n "$PRIVATE_CONTRACT" ]]; then
    private_contract compose --image "${IMAGE_REF:-}" -- "$@"
  else
    docker compose --project-directory "$ROOT" "$@"
  fi
}
HEALTH_TIMEOUT="${RELEASE_HEALTH_TIMEOUT_SECONDS:-180}"
HEALTH_POLL="${RELEASE_HEALTH_POLL_SECONDS:-2}"
HEALTH_SERVICES="${RELEASE_HEALTH_SERVICES:-redis rsshub ai-filter-api ai-filter-web ai-filter-worker}"
CAPTURE_HEALTH_DIAGNOSTICS="${RELEASE_CAPTURE_HEALTH_DIAGNOSTICS:-0}"
AUTO_ROLLBACK="${RELEASE_AUTO_ROLLBACK:-1}"
ALLOW_DIRTY_TREE="${RELEASE_ALLOW_DIRTY_TREE:-0}"
REQUIRE_BUILD_CONTRACT="${RELEASE_REQUIRE_BUILD_CONTRACT:-1}"
LOCK_WAIT_SECONDS="${RELEASE_LOCK_WAIT_SECONDS-0}"
TEST_ATTESTATION_DIR="${RELEASE_TEST_ATTESTATION_DIR:-/run/ai-filter-release-gate/attestations}"
TEST_ATTESTATION_SCHEMA="ai-filter-release-test-attestation-v1"
TEST_ATTESTATION_SUITE="pytest-full-cov75-v1"
TEST_ATTESTATION_MAX_AGE_SECONDS=21600
TEST_ATTESTATION_MAX_DURATION_SECONDS=7200
TEST_ATTESTATION_FUTURE_SKEW_SECONDS=300
EVENT_MAPPING_MIGRATION_GENERATION="event-mapping-store-v5-provider-attribution-v1"
DEFAULT_EVENT_MAPPING_MIGRATION_GATE_FILE="${ROOT}/data/.event-mapping-migration-required"
DEFAULT_EVENT_MAPPING_MIGRATION_STATE_FILE="/var/lib/ai-filter-ops-check/event-mapping-migration.json"
EVENT_MAPPING_MIGRATION_GATE_FILE="${AI_FILTER_MAPPING_MIGRATION_GATE_FILE:-}"
EVENT_MAPPING_MIGRATION_STATE_FILE="${AI_FILTER_MAPPING_MIGRATION_STATE_FILE:-}"
EVENT_MAPPING_ISOLATION_ROOT="${AI_FILTER_RELEASE_ISOLATION_ROOT:-}"
EVENT_MAPPING_PATHS_ISOLATED=0
if [[ -n "$EVENT_MAPPING_MIGRATION_GATE_FILE" || \
      -n "$EVENT_MAPPING_MIGRATION_STATE_FILE" || \
      -n "$EVENT_MAPPING_ISOLATION_ROOT" ]]; then
  if [[ -z "$EVENT_MAPPING_MIGRATION_GATE_FILE" || \
        -z "$EVENT_MAPPING_MIGRATION_STATE_FILE" || \
        -z "$EVENT_MAPPING_ISOLATION_ROOT" ]]; then
    echo "AI_FILTER_RELEASE_ISOLATION_ROOT, AI_FILTER_MAPPING_MIGRATION_GATE_FILE, and AI_FILTER_MAPPING_MIGRATION_STATE_FILE are all required for isolated release state" >&2
    exit 2
  fi
  EVENT_MAPPING_PATHS_ISOLATED=1
else
  EVENT_MAPPING_MIGRATION_GATE_FILE="$DEFAULT_EVENT_MAPPING_MIGRATION_GATE_FILE"
  EVENT_MAPPING_MIGRATION_STATE_FILE="$DEFAULT_EVENT_MAPPING_MIGRATION_STATE_FILE"
fi
RELEASE_SOURCE_ID=""
BUILD_TEMP_DIR=""
BUILD_SNAPSHOT_DIR=""

validate_isolated_event_mapping_paths() {
  if [[ "$EVENT_MAPPING_PATHS_ISOLATED" != "1" ]]; then
    return 0
  fi
  python3 -I - \
    "$EVENT_MAPPING_ISOLATION_ROOT" \
    "$REGISTRY_DIR" \
    "$EVENT_MAPPING_MIGRATION_GATE_FILE" \
    "$EVENT_MAPPING_MIGRATION_STATE_FILE" <<'PY'
import os
import sys
from pathlib import Path


isolation_text, registry_text, gate_text, state_text = sys.argv[1:]
values = (isolation_text, registry_text, gate_text, state_text)
if any(
    not value
    or not Path(value).is_absolute()
    or value == "/"
    or "\n" in value
    or "\r" in value
    for value in values
):
    print(
        "Isolated event-mapping paths must be safe absolute paths",
        file=sys.stderr,
    )
    raise SystemExit(2)

isolation_path = Path(isolation_text)
try:
    isolation_metadata = isolation_path.lstat()
except OSError:
    isolation_metadata = None
if (
    isolation_metadata is None
    or isolation_path.is_symlink()
    or not isolation_path.is_dir()
    or isolation_metadata.st_uid != os.geteuid()
    or isolation_metadata.st_mode & 0o077
):
    print(
        "AI_FILTER_RELEASE_ISOLATION_ROOT must be an owned private directory",
        file=sys.stderr,
    )
    raise SystemExit(2)

isolation = isolation_path.resolve(strict=True)
registry = Path(registry_text).resolve(strict=False)
gate = Path(gate_text).resolve(strict=False)
state = Path(state_text).resolve(strict=False)
if gate == state:
    print("Isolated event-mapping gate and state paths must differ", file=sys.stderr)
    raise SystemExit(2)
for candidate in (registry, gate, state):
    try:
        common = Path(os.path.commonpath((str(isolation), str(candidate))))
    except ValueError:
        common = None
    if common != isolation or candidate == isolation:
        print(
            "Isolated release paths must stay below AI_FILTER_RELEASE_ISOLATION_ROOT",
            file=sys.stderr,
        )
        raise SystemExit(2)
PY
}

validate_isolated_event_mapping_paths
mkdir -p "$REGISTRY_DIR" "$MANIFEST_DIR" "$BUILD_CONTRACT_DIR" "$CONFIG_SNAPSHOT_DIR"
chmod 0700 "$BUILD_CONTRACT_DIR" "$CONFIG_SNAPSHOT_DIR"

cleanup_build_snapshot() {
  if [[ -n "$BUILD_TEMP_DIR" && -d "$BUILD_TEMP_DIR" && "$BUILD_TEMP_DIR" != "/" ]]; then
    rm -rf -- "$BUILD_TEMP_DIR"
  fi
  BUILD_TEMP_DIR=""
  BUILD_SNAPSHOT_DIR=""
}

trap cleanup_build_snapshot EXIT

case "$AUTO_ROLLBACK" in
  0|1) ;;
  *)
    echo "RELEASE_AUTO_ROLLBACK must be exactly 0 or 1" >&2
    exit 2
    ;;
esac

case "$CAPTURE_HEALTH_DIAGNOSTICS" in
  0|1) ;;
  *)
    echo "RELEASE_CAPTURE_HEALTH_DIAGNOSTICS must be exactly 0 or 1" >&2
    exit 2
    ;;
esac

if [[ "$CAPTURE_HEALTH_DIAGNOSTICS" == "1" ]]; then
  if [[ "$EVENT_MAPPING_PATHS_ISOLATED" != "1" ]]; then
    echo "Health diagnostics may be enabled only inside an isolated release rehearsal" >&2
    exit 2
  fi
  if ! command -v timeout >/dev/null 2>&1; then
    echo "Health diagnostics require the timeout command" >&2
    exit 2
  fi
fi

case "$ALLOW_DIRTY_TREE" in
  0|1) ;;
  *)
    echo "RELEASE_ALLOW_DIRTY_TREE must be exactly 0 or 1" >&2
    exit 2
    ;;
esac

case "$REQUIRE_BUILD_CONTRACT" in
  0|1) ;;
  *)
    echo "RELEASE_REQUIRE_BUILD_CONTRACT must be exactly 0 or 1" >&2
    exit 2
    ;;
esac

if [[ ! "$LOCK_WAIT_SECONDS" =~ ^(0|[1-9][0-9]{0,2})$ ]] || \
   (( LOCK_WAIT_SECONDS > 300 )); then
  echo "RELEASE_LOCK_WAIT_SECONDS must be an integer from 0 through 300" >&2
  exit 2
fi

acquire_release_lock() {
  local lock_path="${REGISTRY_DIR}/release.lock"
  local inherited_fd="${RELEASE_LOCK_FD:-}" descriptor_path probe_status
  local lock_status lock_error
  local -a lock_args=(-n -E 75)

  if [[ -z "$inherited_fd" ]]; then
    # Every invocation owns its lock for its complete lifetime. Waiting only
    # changes acquisition; the release command is still executed once.
    exec 9>"$lock_path"
    if (( LOCK_WAIT_SECONDS > 0 )); then
      lock_args=(-w "$LOCK_WAIT_SECONDS" -E 75)
    fi
    if lock_error="$(flock "${lock_args[@]}" 9 2>&1)"; then
      return 0
    else
      lock_status=$?
    fi
    if [[ "$lock_status" -eq 75 ]]; then
      if (( LOCK_WAIT_SECONDS == 0 )); then
        echo "Another release command is already running (lock contention; flock exit=75; wait=0s)" >&2
      else
        echo "Timed out waiting for the release lock (flock exit=75; wait=${LOCK_WAIT_SECONDS}s)" >&2
      fi
    else
      echo "Could not acquire release lock (flock exit=${lock_status}; wait=${LOCK_WAIT_SECONDS}s): ${lock_error:0:512}" >&2
    fi
    exit 6
  fi

  # Keep inherited mode deliberately narrow. In particular, never interpolate
  # an arbitrary value into redirection/eval syntax or accept stdio/script FDs.
  if [[ ! "$inherited_fd" =~ ^[1-9][0-9]{1,2}$ ]] || \
     (( inherited_fd < 10 || inherited_fd > 199 )); then
    echo "RELEASE_LOCK_FD must be an inherited numeric descriptor from 10 through 199" >&2
    exit 6
  fi

  descriptor_path="/proc/self/fd/${inherited_fd}"
  if [[ ! -e "$descriptor_path" ]]; then
    echo "RELEASE_LOCK_FD is not open in the Linux release process" >&2
    exit 6
  fi
  if [[ ! "$descriptor_path" -ef "$lock_path" ]]; then
    echo "RELEASE_LOCK_FD does not refer to ${lock_path}" >&2
    exit 6
  fi

  # A same-file check alone is insufficient: an unlocked descriptor would let
  # an ordinary environment mistake bypass the outer maintenance lock. Probe
  # through an independent open description and require explicit contention,
  # then reassert the lock through the inherited description. These two checks
  # distinguish an inherited owner from both an unlocked FD and another owner.
  exec 9>"$lock_path"
  probe_status=0
  if flock -n -E 75 9; then
    flock -u 9 || true
    exec 9>&-
    echo "RELEASE_LOCK_FD does not already hold the release lock" >&2
    exit 6
  else
    probe_status=$?
  fi
  exec 9>&-
  if [[ "$probe_status" -ne 75 ]]; then
    echo "Could not verify the inherited release lock" >&2
    exit 6
  fi
  if ! flock -n "$inherited_fd"; then
    echo "RELEASE_LOCK_FD is not the owner of the release lock" >&2
    exit 6
  fi
  if [[ ! "$descriptor_path" -ef "$lock_path" ]]; then
    echo "Release lock path changed while validating RELEASE_LOCK_FD" >&2
    exit 6
  fi
}

acquire_release_lock

cmd="${1:-}"
image_ref="${2:-${IMAGE_REF:-}}"

if [[ -n "$PRIVATE_CONTRACT" ]]; then
  if [[ "$ALLOW_DIRTY_TREE" != "0" || "$REQUIRE_BUILD_CONTRACT" != "1" ||
        "$AUTO_ROLLBACK" != "0" || "$CAPTURE_HEALTH_DIAGNOSTICS" != "0" ||
        -n "${RELEASE_COMPOSE_FILE:-}" || "$cmd" == "rollback" || "$cmd" == "seal-current" ]]; then
    echo "Private release requires clean source, build contract, automatic rollback/diagnostics off, and no manifest override or direct rollback" >&2
    exit 8
  fi
  umask 077
  private_contract check
  PRIVATE_CONTRACT_SHA="$(private_contract field --field sha256)"
  PRIVATE_RUNTIME_CONFIG_SHA="$(private_contract field --field runtime_config_sha256)"
  COMPOSE_FILE="$(private_contract field --field manifest)"
  private_health_services="$(private_contract field --field health_services)"
  if [[ -n "${RELEASE_HEALTH_SERVICES:-}" && "$HEALTH_SERVICES" != "$private_health_services" ]]; then
    echo "Private release health service override differs from sealed contract" >&2
    exit 8
  fi
  HEALTH_SERVICES="$private_health_services"
  chmod 0700 "$MANIFEST_DIR"
fi

usage() {
  echo "Usage: $0 {build|deploy|rollback|verify|seal-current} <immutable-image-ref>" >&2
  exit 1
}

validate_image_ref() {
  local ref="$1"
  if [[ -z "$ref" || "$ref" == *$'\n'* || "$ref" == *$'\r'* ]]; then
    echo "IMAGE_REF is required" >&2
    return 2
  fi
  if [[ "$ref" == "latest" || "$ref" == *":latest" ]]; then
    echo "Refusing floating latest image: $ref" >&2
    return 2
  fi
  if [[ "$ref" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    return 0
  fi
  if [[ "$ref" =~ ^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$ ]]; then
    return 0
  fi
  # Version tags must start with a digit or v+digit. This intentionally rejects
  # main/master/dev/stable/edge and other mutable channel tags.
  if [[ "$ref" =~ ^[A-Za-z0-9._:/-]+:v?[0-9][A-Za-z0-9._-]*$ ]]; then
    return 0
  fi
  echo "IMAGE_REF must be a sha256 digest or immutable version tag: $ref" >&2
  return 2
}

validate_dirty_override_field() {
  local name="$1" value="$2" max_length="$3"
  if [[ -z "$value" ]]; then
    echo "$name is required for a dirty-tree override" >&2
    return 2
  fi
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *$'\t'* ]]; then
    echo "$name must be a single audit-log field without tabs or newlines" >&2
    return 2
  fi
  if (( ${#value} > max_length )); then
    echo "$name must be at most $max_length characters" >&2
    return 2
  fi
}

require_clean_release_tree() {
  local action="$1" ref="$2" status head fingerprint reason actor audit_file path
  if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Release root is not a readable Git worktree: $ROOT" >&2
    return 7
  fi
  if ! status="$(git -C "$ROOT" status --porcelain=v1 --untracked-files=all)"; then
    echo "Could not inspect Git worktree state; refusing $action" >&2
    return 7
  fi
  if [[ -z "$status" ]]; then
    if ! head="$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null)" || \
       [[ ! "$head" =~ ^[0-9a-f]{40,64}$ ]]; then
      echo "Could not resolve a commit for the release source" >&2
      return 7
    fi
    RELEASE_SOURCE_ID="${head}:clean"
    return 0
  fi

  # A detached runner can attest only the committed tree. A dirty build would
  # therefore package bytes that were never covered by that attestation. Keep
  # the audited override for deploy/recovery, but never use it for builds.
  if [[ "$action" == "build" ]]; then
    echo "Refusing build from a dirty Git worktree; the full-suite attestation covers only clean HEAD" >&2
    return 7
  fi

  if [[ "$ALLOW_DIRTY_TREE" != "1" ]]; then
    echo "Refusing $action from a dirty Git worktree; commit or stash all tracked and untracked changes" >&2
    echo "Emergency override requires RELEASE_ALLOW_DIRTY_TREE=1 and RELEASE_DIRTY_TREE_OVERRIDE_REASON" >&2
    return 7
  fi

  reason="${RELEASE_DIRTY_TREE_OVERRIDE_REASON:-}"
  actor="${RELEASE_DIRTY_TREE_OVERRIDE_ACTOR:-${SUDO_USER:-$(id -un)}}"
  validate_dirty_override_field "RELEASE_DIRTY_TREE_OVERRIDE_REASON" "$reason" 512 || return $?
  validate_dirty_override_field "RELEASE_DIRTY_TREE_OVERRIDE_ACTOR" "$actor" 128 || return $?
  if ! head="$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null)" || \
     [[ ! "$head" =~ ^[0-9a-f]{40,64}$ ]]; then
    echo "Could not resolve a commit for the dirty-tree audit record" >&2
    return 7
  fi
  # Hash the actual tracked diff and every untracked blob, not only the status
  # paths. Otherwise two different emergency payloads touching the same files
  # would receive the same audit fingerprint.
  if ! fingerprint="$(
    {
      git -C "$ROOT" diff --binary --no-ext-diff HEAD --
      while IFS= read -r -d '' path; do
        printf 'untracked\0%s\0' "$path"
        git -C "$ROOT" hash-object --no-filters -- "$path"
      done < <(git -C "$ROOT" ls-files --others --exclude-standard -z)
    } | sha256sum | awk '{print $1}'
  )" || \
     [[ ! "$fingerprint" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Could not fingerprint dirty Git state; refusing $action" >&2
    return 7
  fi

  audit_file="${REGISTRY_DIR}/dirty-overrides.tsv"
  touch "$audit_file"
  chmod 0600 "$audit_file"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$action" "$ref" "$head" \
    "$fingerprint" "$actor" "$reason" >> "$audit_file"
  RELEASE_SOURCE_ID="${head}:${fingerprint}"
  echo "WARNING: accepted audited dirty-tree override for $action (fingerprint $fingerprint)" >&2
}

release_test_python_metadata() {
  local configured="${AI_FILTER_TEST_PYTHON:-${ROOT}/.venv-release-gate/bin/python}"
  local configured_dir invocation resolved owner mode mode_value identity

  if [[ "$configured" != /* || ! -x "$configured" ]]; then
    echo "AI_FILTER_TEST_PYTHON must be an absolute executable path" >&2
    return 8
  fi
  if ! configured_dir="$(cd -- "$(dirname -- "$configured")" 2>/dev/null && pwd -P)"; then
    echo "Could not resolve the release-test Python directory" >&2
    return 8
  fi
  invocation="${configured_dir}/$(basename -- "$configured")"
  if [[ ! -x "$invocation" ]]; then
    echo "Release-test Python must be executable" >&2
    return 8
  fi
  if ! resolved="$(readlink -f -- "$invocation" 2>/dev/null)" || \
     [[ "$resolved" != /* || ! -f "$resolved" || -L "$resolved" ]]; then
    echo "Release-test Python must resolve to a regular executable file" >&2
    return 8
  fi
  read -r owner mode < <(stat -c '%u %a' -- "$resolved") || return 8
  if [[ ! "$owner" =~ ^[0-9]+$ ]] || \
     { [[ "$owner" != "0" ]] && [[ "$owner" != "$(id -u)" ]]; }; then
    echo "Release-test Python must be owned by root or the release user" >&2
    return 8
  fi
  if [[ ! "$mode" =~ ^[0-7]{3,4}$ ]]; then
    echo "Could not validate release-test Python permissions" >&2
    return 8
  fi
  mode_value=$((8#$mode))
  if (( (mode_value & 0022) != 0 )); then
    echo "Release-test Python must not be group/world writable" >&2
    return 8
  fi
  # Invoke through the configured venv path. Executing the final symlink target
  # directly would discard pyvenv.cfg and silently test the system environment.
  if ! identity="$("$invocation" -I -c \
      'import platform, sys; print(f"{platform.python_implementation()}|{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' \
      2>/dev/null)" || \
     [[ ! "$identity" =~ ^CPython\|3\.11\.[0-9]+$ ]]; then
    echo "Release-test Python must be CPython 3.11" >&2
    return 8
  fi

  RELEASE_TEST_PYTHON_REALPATH="$invocation"
  RELEASE_TEST_PYTHON_SHA256="$(sha256sum -- "$resolved" | awk '{print $1}')"
  RELEASE_TEST_PYTHON_IDENTITY_SHA256="$(printf '%s' "$identity" | sha256sum | awk '{print $1}')"
  if [[ ! "$RELEASE_TEST_PYTHON_SHA256" =~ ^[0-9a-f]{64}$ || \
        ! "$RELEASE_TEST_PYTHON_IDENTITY_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Could not fingerprint release-test Python" >&2
    return 8
  fi
}

validate_release_test_node() {
  local path="$1" expected_mode="$2" kind="$3"
  local owner mode links

  if [[ "$kind" == "directory" ]]; then
    if [[ ! -d "$path" || -L "$path" ]]; then
      echo "Release-test attestation directory is not trusted: $path" >&2
      return 8
    fi
  elif [[ ! -f "$path" || -L "$path" ]]; then
    echo "Release-test attestation node is not a trusted regular file: $path" >&2
    return 8
  fi
  read -r owner mode links < <(stat -c '%u %a %h' -- "$path") || return 8
  if [[ "$owner" != "$(id -u)" || "$mode" != "$expected_mode" ]]; then
    echo "Release-test attestation node has unsafe owner or mode: $path" >&2
    return 8
  fi
  if [[ "$kind" != "directory" && "$links" != "1" ]]; then
    echo "Release-test attestation files must not be hard linked: $path" >&2
    return 8
  fi
}

require_valid_release_test_attestation() {
  local source_id="$1" head="${1%%:*}" target log_file lock_file
  local runner_sha python_sha identity_sha log_sha now started completed
  local before_inode after_inode size value gate_fd attestation_realpath
  local -a values=()

  if [[ ! "$source_id" =~ ^[0-9a-f]{40,64}:clean$ ]]; then
    echo "A full-suite attestation can authorize only a clean commit" >&2
    return 8
  fi
  if [[ "$TEST_ATTESTATION_DIR" != /* || "$TEST_ATTESTATION_DIR" == "/" ]]; then
    echo "Release-test attestation directory must be an absolute non-root path" >&2
    return 8
  fi
  validate_release_test_node "$TEST_ATTESTATION_DIR" 700 directory || return $?
  attestation_realpath="$(readlink -f -- "$TEST_ATTESTATION_DIR" 2>/dev/null)" || return 8
  if [[ "$attestation_realpath" != "$TEST_ATTESTATION_DIR" ]]; then
    echo "Release-test attestation directory must not traverse symlinked path components" >&2
    return 8
  fi

  target="${TEST_ATTESTATION_DIR}/${head}.state"
  log_file="${TEST_ATTESTATION_DIR}/${head}.log"
  lock_file="${TEST_ATTESTATION_DIR}/attestation.lock"
  validate_release_test_node "$lock_file" 600 file || return $?
  exec {gate_fd}<"$lock_file"
  if ! flock -n -s "$gate_fd"; then
    echo "Full-suite release tests are still running" >&2
    exec {gate_fd}>&-
    return 8
  fi

  validate_release_test_node "$target" 600 file || {
    exec {gate_fd}>&-
    return 8
  }
  validate_release_test_node "$log_file" 600 file || {
    exec {gate_fd}>&-
    return 8
  }
  size="$(stat -c '%s' -- "$target")"
  if [[ ! "$size" =~ ^[0-9]+$ ]] || (( size == 0 || size > 4096 )); then
    echo "Release-test attestation is empty or too large" >&2
    exec {gate_fd}>&-
    return 8
  fi
  before_inode="$(stat -c '%d:%i' -- "$target")"
  while IFS= read -r value; do
    values[${#values[@]}]="$value"
  done < "$target"
  after_inode="$(stat -c '%d:%i' -- "$target")"
  if [[ "$before_inode" != "$after_inode" || "${#values[@]}" -ne 11 ]]; then
    echo "Release-test attestation is incomplete or changed while reading" >&2
    exec {gate_fd}>&-
    return 8
  fi

  runner_sha="$(sha256sum -- "${ROOT}/scripts/run-release-gate.sh" | awk '{print $1}')"
  release_test_python_metadata || {
    exec {gate_fd}>&-
    return 8
  }
  python_sha="$RELEASE_TEST_PYTHON_SHA256"
  identity_sha="$RELEASE_TEST_PYTHON_IDENTITY_SHA256"
  log_sha="$(sha256sum -- "$log_file" | awk '{print $1}')"
  started="${values[7]}"
  completed="${values[8]}"
  now="$(date +%s)"

  if [[ "${values[0]}" != "$TEST_ATTESTATION_SCHEMA" || \
        "${values[1]}" != "$source_id" || \
        "${values[2]}" != "$TEST_ATTESTATION_SUITE" || \
        "${values[3]}" != "$runner_sha" || \
        "${values[4]}" != "$RELEASE_TEST_PYTHON_REALPATH" || \
        "${values[5]}" != "$python_sha" || \
        "${values[6]}" != "$identity_sha" || \
        "${values[9]}" != "$log_sha" || \
        "${values[10]}" != "passed" ]]; then
    echo "Release-test attestation does not match this source, runner, Python, or log" >&2
    exec {gate_fd}>&-
    return 8
  fi
  if [[ ! "$runner_sha" =~ ^[0-9a-f]{64}$ || \
        ! "$started" =~ ^[0-9]{1,12}$ || ! "$completed" =~ ^[0-9]{1,12}$ || \
        ! "$now" =~ ^[0-9]{1,12}$ ]]; then
    echo "Release-test attestation contains invalid fields" >&2
    exec {gate_fd}>&-
    return 8
  fi
  if (( started > completed || \
        completed > now + TEST_ATTESTATION_FUTURE_SKEW_SECONDS || \
        completed + TEST_ATTESTATION_MAX_AGE_SECONDS < now || \
        completed - started > TEST_ATTESTATION_MAX_DURATION_SECONDS )); then
    echo "Release-test attestation is stale or has invalid timestamps" >&2
    exec {gate_fd}>&-
    return 8
  fi

  flock -u "$gate_fd"
  exec {gate_fd}>&-
}

validate_runtime_digest() {
  local digest="$1"
  if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "Production Compose requires a validated image digest, got: $digest" >&2
    return 3
  fi
}

ensure_image() {
  local ref="$1"
  if ! docker image inspect "$ref" >/dev/null 2>&1; then
    docker pull "$ref"
  fi
  docker image inspect "$ref" >/dev/null
}

resolved_image_id() {
  docker image inspect "$1" --format '{{.Id}}'
}

verify_requested_digest() {
  local ref="$1"
  local expected=""
  if [[ "$ref" == *@sha256:* ]]; then
    expected="${ref##*@}"
  elif [[ "$ref" == sha256:* ]]; then
    expected="$ref"
  else
    return 0
  fi
  local image_id repo_digests
  image_id="$(resolved_image_id "$ref")"
  repo_digests="$(docker image inspect "$ref" --format '{{json .RepoDigests}}' 2>/dev/null || true)"
  if [[ "$image_id" != "$expected" && "$repo_digests" != *"@$expected"* ]]; then
    echo "Digest mismatch for $ref: resolved $image_id" >&2
    return 3
  fi
}

record_history() {
  local action="$1" ref="$2" digest="$3" status="$4"
  local manifest="${5:-}" services="${6:-}"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$action" "$ref" "$digest" \
     "$status" "$manifest" "$services" >> "${REGISTRY_DIR}/history.tsv"
}

registry_file_contains_ref() {
  local path="$1" field="$2" ref="$3"
  [[ -e "$path" ]] || return 1
  if [[ ! -f "$path" || ! -r "$path" || -L "$path" ]]; then
    echo "Release registry record is not a readable regular file: $path" >&2
    return 3
  fi
  awk -F '\t' -v field="$field" -v wanted="$ref" '
    $field == wanted { found=1 }
    END { exit(found ? 0 : 1) }
  ' "$path"
}

state_records_ref() {
  local prefix="$1" ref="$2" bundle mirror first path
  bundle="$(state_bundle_file "$prefix")"
  mirror="$(state_file "$prefix" ref)"
  for path in "$bundle" "$mirror"; do
    [[ -e "$path" ]] || continue
    if [[ ! -f "$path" || ! -r "$path" || -L "$path" ]]; then
      echo "Release state record is not a readable regular file: $path" >&2
      return 3
    fi
    IFS= read -r first < "$path" || true
    [[ "$first" == "$ref" ]] && return 0
  done
  return 1
}

assert_ref_unused_for_build() {
  local ref="$1" contract status=1

  registry_file_contains_ref "${REGISTRY_DIR}/history.tsv" 3 "$ref" && status=0 || status=$?
  [[ "$status" -eq 1 ]] || {
    [[ "$status" -eq 0 ]] && \
      echo "Refusing to reuse previously registered immutable version: $ref" >&2
    return 3
  }
  registry_file_contains_ref "${REGISTRY_DIR}/dirty-overrides.tsv" 3 "$ref" && status=0 || status=$?
  [[ "$status" -eq 1 ]] || {
    [[ "$status" -eq 0 ]] && \
      echo "Refusing to reuse previously registered immutable version: $ref" >&2
    return 3
  }
  registry_file_contains_ref "${REGISTRY_DIR}/build-contract-overrides.tsv" 2 "$ref" && status=0 || status=$?
  [[ "$status" -eq 1 ]] || {
    [[ "$status" -eq 0 ]] && \
      echo "Refusing to reuse previously registered immutable version: $ref" >&2
    return 3
  }

  contract="$(build_contract_path "$ref")"
  if [[ -e "$contract" || -L "$contract" ]]; then
    echo "Refusing to reuse previously registered immutable version: $ref" >&2
    return 3
  fi
  for prefix in current previous; do
    state_records_ref "$prefix" "$ref" && status=0 || status=$?
    [[ "$status" -eq 1 ]] || {
      [[ "$status" -eq 0 ]] && \
        echo "Refusing to reuse previously registered immutable version: $ref" >&2
      return 3
    }
  done
}

known_digest_for_ref() {
  local ref="$1"
  [[ -f "${REGISTRY_DIR}/history.tsv" ]] || return 0
  # The first successfully resolved digest permanently owns a version ref,
  # regardless of whether compose/config/health later failed. Conflicting or
  # malformed history is treated as tampering and fails closed.
  awk -F '\t' -v wanted="$ref" '
    $3 == wanted {
      if (length($4) != 71 || $4 !~ /^sha256:[0-9a-f]+$/) {
        print "Invalid release history digest for " wanted > "/dev/stderr"
        exit 3
      }
      if (digest == "") {
        digest=$4
      } else if (digest != $4) {
        print "Conflicting release history digests for " wanted > "/dev/stderr"
        exit 3
      }
    }
    END {if (digest != "") print digest}
  ' "${REGISTRY_DIR}/history.tsv"
}

assert_ref_consistency() {
  local ref="$1" digest="$2" known
  known="$(known_digest_for_ref "$ref")"
  if [[ -n "$known" && "$known" != "$digest" ]]; then
    echo "Immutable ref changed digest: $ref was $known, now $digest" >&2
    return 3
  fi
}

write_atomic() {
  local target="$1" value="$2" tmp
  tmp="${target}.tmp.$$"
  printf '%s\n' "$value" > "$tmp"
  mv -f "$tmp" "$target"
}

release_config_hash() {
  local config_file="${1:-$CONFIG_FILE}" hash
  if [[ ! -f "$config_file" || ! -r "$config_file" || -L "$config_file" ]]; then
    echo "Release config must be a readable regular non-symlink file: $config_file" >&2
    return 2
  fi
  hash="$(sha256sum -- "$config_file" | awk '{print $1}')"
  if [[ ! "$hash" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Could not hash release config: $config_file" >&2
    return 2
  fi
  printf '%s\n' "$hash"
}

capture_dirty_snapshot() {
  local snapshot="$1" head="$2" expected_fingerprint="$3"
  local diff_file="${BUILD_TEMP_DIR}/tracked.diff"
  local untracked_file="${BUILD_TEMP_DIR}/untracked.paths"
  local path parent fingerprint

  git -C "$ROOT" diff --binary --no-ext-diff "$head" -- > "$diff_file"
  git -C "$ROOT" ls-files --others --exclude-standard -z > "$untracked_file"
  if [[ -s "$diff_file" ]]; then
    (cd "$snapshot" && git apply --binary --unsafe-paths "$diff_file")
  fi

  while IFS= read -r -d '' path; do
    if [[ "$path" == /* || "$path" == ".." || "$path" == ../* || "$path" == */../* ]]; then
      echo "Unsafe untracked release path: $path" >&2
      return 7
    fi
    if [[ ! -f "${ROOT}/${path}" && ! -L "${ROOT}/${path}" ]]; then
      echo "Untracked release input changed while snapshotting: $path" >&2
      return 7
    fi
    parent="$(dirname -- "$path")"
    mkdir -p -- "${snapshot}/${parent}"
    cp -a -- "${ROOT}/${path}" "${snapshot}/${path}"
  done < "$untracked_file"

  fingerprint="$({
    cat -- "$diff_file"
    while IFS= read -r -d '' path; do
      printf 'untracked\0%s\0' "$path"
      git -C "$snapshot" hash-object --no-filters -- "$path"
    done < "$untracked_file"
  } | sha256sum | awk '{print $1}')"
  if [[ ! "$fingerprint" =~ ^[0-9a-f]{64}$ || "$fingerprint" != "$expected_fingerprint" ]]; then
    echo "Release source changed while creating the immutable build snapshot" >&2
    return 7
  fi
}

create_build_snapshot() {
  local head="${RELEASE_SOURCE_ID%%:*}" source_state="${RELEASE_SOURCE_ID#*:}"
  local template="${TMPDIR:-/tmp}/ai-filter-build.XXXXXXXX"

  if [[ ! "$head" =~ ^[0-9a-f]{40,64}$ ]] || \
     [[ ! "$source_state" =~ ^(clean|[0-9a-f]{64})$ ]]; then
    echo "Release source fingerprint is unavailable" >&2
    return 7
  fi
  cleanup_build_snapshot
  umask 077
  BUILD_TEMP_DIR="$(mktemp -d "$template")"
  chmod 0700 "$BUILD_TEMP_DIR"
  BUILD_SNAPSHOT_DIR="${BUILD_TEMP_DIR}/context"
  mkdir -m 0700 "$BUILD_SNAPSHOT_DIR"

  git -C "$ROOT" archive --format=tar "$head" | tar -xf - -C "$BUILD_SNAPSHOT_DIR"
  if [[ "$source_state" != "clean" ]]; then
    capture_dirty_snapshot "$BUILD_SNAPSHOT_DIR" "$head" "$source_state"
  fi
  if [[ ! -f "${BUILD_SNAPSHOT_DIR}/Dockerfile" || \
        ! -f "${BUILD_SNAPSHOT_DIR}/config.yaml" ]]; then
    echo "Immutable build snapshot is missing Dockerfile or config.yaml" >&2
    return 7
  fi
}

build_contract_path() {
  local ref="$1" key
  key="$(printf '%s' "$ref" | sha256sum | awk '{print $1}')"
  if [[ ! "$key" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Could not derive build-contract key for $ref" >&2
    return 2
  fi
  printf '%s/%s.state\n' "$BUILD_CONTRACT_DIR" "$key"
}

write_build_contract() {
  local ref="$1" digest="$2" manifest="$3" config_hash="$4" source_id="$5"
  local target bundle
  target="$(build_contract_path "$ref")"
  bundle="${ref}"$'\n'"${digest}"$'\n'"${manifest}"$'\n'"${config_hash}"$'\n'"${source_id}"$'\n'"${EVENT_MAPPING_MIGRATION_GENERATION}"
  if [[ -n "$PRIVATE_CONTRACT" ]]; then
    bundle+=$'\n'"${PRIVATE_CONTRACT_SHA}"
  fi
  write_atomic "$target" "$bundle"
  chmod 0600 "$target"
}

read_build_contract() {
  local ref="$1" target value
  local -a values=()
  target="$(build_contract_path "$ref")"
  if [[ ! -f "$target" || ! -r "$target" || -L "$target" ]]; then
    echo "No trusted build contract is recorded for $ref; run release.sh build first" >&2
    return 3
  fi
  while IFS= read -r value; do
    values[${#values[@]}]="$value"
  done < "$target"
  if [[ "${#values[@]}" -ne 5 && "${#values[@]}" -ne 6 && "${#values[@]}" -ne 7 ]] || \
     [[ "${values[0]}" != "$ref" ]] || \
     [[ ! "${values[1]}" =~ ^sha256:[0-9a-f]{64}$ ]] || \
     [[ ! "${values[2]}" =~ ^[0-9a-f]{64}$ ]] || \
     [[ ! "${values[3]}" =~ ^[0-9a-f]{64}$ ]] || \
     [[ ! "${values[4]}" =~ ^[0-9a-f]{40,64}:(clean|[0-9a-f]{64})$ ]]; then
    echo "Invalid build contract for $ref" >&2
    return 3
  fi
  CONTRACT_REF="${values[0]}"
  CONTRACT_DIGEST="${values[1]}"
  CONTRACT_MANIFEST="${values[2]}"
  CONTRACT_CONFIG_HASH="${values[3]}"
  CONTRACT_SOURCE_ID="${values[4]}"
  CONTRACT_MAPPING_GENERATION="${values[5]:-}"
  CONTRACT_PRIVATE_SHA="${values[6]:-}"
  if [[ -n "$CONTRACT_PRIVATE_SHA" &&
        ( ! "$CONTRACT_PRIVATE_SHA" =~ ^[0-9a-f]{64}$ || "$CONTRACT_PRIVATE_SHA" != "$PRIVATE_CONTRACT_SHA" ) ]]; then
    echo "Private build contract is missing or mismatched" >&2
    return 3
  fi
  if [[ -n "$CONTRACT_MAPPING_GENERATION" && \
        "$CONTRACT_MAPPING_GENERATION" != "$EVENT_MAPPING_MIGRATION_GENERATION" ]]; then
    echo "Invalid event-mapping generation in build contract for $ref" >&2
    return 3
  fi
}

image_label_value() {
  local image="$1" key="$2"
  docker image inspect "$image" --format "{{ index .Config.Labels \"${key}\" }}"
}

validate_event_mapping_release_target() {
  local action="$1" ref="$2" digest="$3" manifest="$4"
  local current_ref="${5:-}" current_digest="${6:-}" current_manifest="${7:-}"
  local target_generation manifest_path manifest_config
  target_generation="$(
    image_label_value "$digest" "io.ai-filter.event-mapping-generation"
  )" || return 3
  if [[ "$target_generation" == "<no value>" ]]; then
    target_generation=""
  fi
  manifest_path="${MANIFEST_DIR}/${manifest}.compose.yml"
  if ! manifest_config="$(
    IMAGE_REF="$digest" release_compose \
      -f "$manifest_path" --profile cron config --format json
  )"; then
    echo "Release manifest cannot prove the event-mapping generation" >&2
    return 3
  fi
  EVENT_MAPPING_RELEASE_CONFIG_JSON="$manifest_config" python3 -I - \
    "$EVENT_MAPPING_MIGRATION_GATE_FILE" \
    "$EVENT_MAPPING_MIGRATION_STATE_FILE" \
    "$action" "$ref" "$digest" "$manifest" \
    "$target_generation" "$EVENT_MAPPING_MIGRATION_GENERATION" \
    "$current_ref" "$current_digest" "$current_manifest" <<'PY'
import json
import hashlib
import os
import re
import stat
import sys
from pathlib import Path

(
    gate_path,
    state_path,
    action,
    ref,
    digest,
    manifest_hash,
    image_generation,
    required,
    current_ref,
    current_digest,
    current_manifest,
) = sys.argv[1:]


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(3)


def secure_json(path_text, max_bytes):
    path = Path(path_text)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        fail("Event-mapping migration state is unreadable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > max_bytes
    ):
        fail("Event-mapping migration state is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            fail("Event-mapping migration state changed while opening")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            fail("Event-mapping migration state is unsafe")
        value = json.loads(raw)
    except (OSError, TypeError, ValueError):
        fail("Event-mapping migration state is malformed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        fail("Event-mapping migration state is malformed")
    return value, hashlib.sha256(raw).hexdigest()


try:
    manifest = json.loads(os.environ.pop("EVENT_MAPPING_RELEASE_CONFIG_JSON"))
    services = manifest["services"]
except (KeyError, TypeError, ValueError):
    fail("Release manifest cannot prove the event-mapping generation")

declarations = []
for name in ("ai-filter-api", "ai-filter-worker", "event-enrichment"):
    service = services.get(name)
    if not isinstance(service, dict):
        declarations.append(('', ''))
        continue
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        parsed = {}
        for item in environment:
            key, separator, value = str(item).partition("=")
            if separator:
                parsed[key] = value
        environment = parsed
    if not isinstance(environment, dict):
        fail("Release manifest has an invalid event-mapping declaration")
    declarations.append(
        (
            str(environment.get("AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE", "")),
            str(
                environment.get(
                    "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION", ""
                )
            ),
        )
    )

manifest_generation = (
    required if declarations == [("1", required)] * 3 else ""
)
partially_declared = any(enforce or generation for enforce, generation in declarations)
if partially_declared and not manifest_generation:
    fail("Release manifest has inconsistent event-mapping fences")
if image_generation and image_generation != required:
    fail("Image has an incompatible event-mapping generation")
if bool(image_generation) != bool(manifest_generation):
    fail("Image and release manifest disagree on the event-mapping generation")

gate_result = secure_json(gate_path, 4096)
state_result = secure_json(state_path, 1024 * 1024)
gate, gate_sha256 = gate_result if gate_result is not None else (None, "")
state, _state_sha256 = state_result if state_result is not None else (None, "")
state_status = ""
point_of_no_return = False
write_attempted = False
automatic_rollback_allowed = True
if state is not None:
    state_status = str(state.get("status") or "")
    boolean_values = (
        state.get("point_of_no_return"),
        state.get("write_attempted"),
        state.get("automatic_rollback_allowed"),
    )
    if (
        isinstance(state.get("version"), bool)
        or state.get("version") != 1
        or state.get("generation") != required
        or state_status not in {
            "preparing", "prepared", "running", "progress", "failed",
            "aborting", "aborted", "verified", "activation_pending",
            "restoring_writers", "complete",
        }
        or any(not isinstance(value, bool) for value in boolean_values)
        or not isinstance(state.get("target_image_ref"), str)
        or not state.get("target_image_ref")
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(state.get("target_digest") or "")
        ) is None
    ):
        fail("Event-mapping migration state contract is invalid")
    point_of_no_return, write_attempted, automatic_rollback_allowed = (
        boolean_values
    )
    if (
        write_attempted is not point_of_no_return
        or automatic_rollback_allowed is point_of_no_return
        or (
            state_status in {"preparing", "prepared", "aborting", "aborted"}
            and point_of_no_return
        )
        or (
            state_status
            in {"verified", "activation_pending", "restoring_writers", "complete"}
            and not point_of_no_return
        )
    ):
        fail("Event-mapping migration state boundary is inconsistent")

compatible_target = (
    image_generation == required and manifest_generation == required
)
if gate is None:
    if compatible_target:
        fail("Prepare the event-mapping migration before deploying this image")
    if state and not (state_status == "aborted" and not point_of_no_return):
        fail("Event-mapping migration state forbids a legacy image")
    raise SystemExit(0)

if (
    isinstance(gate.get("version"), bool)
    or gate.get("version") != 1
    or gate.get("kind") != "event_mapping_gate"
    or gate.get("generation") != required
):
    fail("Event-mapping migration gate is incompatible")

gate_status = gate.get("status")
if gate_status == "complete":
    gate_ref = str(gate.get("target_image_ref") or "")
    gate_digest = str(gate.get("target_image_digest") or "")
    gate_manifest = str(gate.get("manifest_hash") or "")
    if (
        not compatible_target
        or "token" in gate
        or not gate_ref
        or re.fullmatch(r"sha256:[0-9a-f]{64}", gate_digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", gate_manifest) is None
        or not str(gate.get("mapping_contract") or "").startswith("asset-map:")
        or re.fullmatch(
            r"[0-9a-f]{64}", str(gate.get("input_snapshot_sha256") or "")
        )
        is None
        or re.fullmatch(
            r"[0-9a-f]{64}", str(gate.get("verification_sha256") or "")
        )
        is None
        or isinstance(gate.get("completed_at"), bool)
        or not isinstance(gate.get("completed_at"), int)
        or gate.get("completed_at", 0) <= 0
    ):
        fail("Completed event-mapping migration forbids this image")
    migration_release = (state or {}).get("release")
    migration_state_valid = (
        isinstance(state, dict)
        and state.get("generation") == required
        and state.get("target_image_ref") == gate_ref
        and state.get("migration_complete") is True
        and state.get("point_of_no_return") is True
        and isinstance(migration_release, dict)
        and migration_release.get("image_ref") == gate_ref
        and migration_release.get("digest") == gate_digest
        and migration_release.get("manifest_hash") == gate_manifest
    )
    if not migration_state_valid:
        fail("Completed event-mapping gate has no matching migration state")
    if state_status != "complete":
        if (
            state_status
            not in {"verified", "activation_pending", "restoring_writers", "failed"}
            or state.get("phase") != "complete"
            or ref != gate_ref
            or digest != gate_digest
            or manifest_hash != gate_manifest
        ):
            fail("Event-mapping activation is bound to its verified image")
    raise SystemExit(0)

if (
    gate_status != "prepared"
    or not state
    or re.fullmatch(r"[0-9a-f]{64}", str(gate.get("token") or "")) is None
    or re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(gate.get("target_image_digest") or "")
    )
    is None
    or isinstance(gate.get("created_at"), bool)
    or not isinstance(gate.get("created_at"), int)
    or gate.get("created_at", 0) <= 0
):
    fail("Event-mapping migration gate is incomplete")
if (
    state.get("generation") != required
    or state.get("target_image_ref") != gate.get("target_image_ref")
    or state.get("target_digest") != gate.get("target_image_digest")
):
    fail("Event-mapping migration gate and state disagree")

if compatible_target:
    state_gate = state.get("gate")
    source = state.get("source_release")
    if (
        state_status != "prepared"
        or point_of_no_return
        or not isinstance(state_gate, dict)
        or state_gate.get("sha256") != gate_sha256
        or ref != gate.get("target_image_ref")
        or digest != gate.get("target_image_digest")
        or not isinstance(source, dict)
        or source.get("image_ref") != current_ref
        or source.get("digest") != current_digest
        or source.get("manifest_hash") != current_manifest
        or not current_ref
        or re.fullmatch(r"sha256:[0-9a-f]{64}", current_digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", current_manifest) is None
    ):
        fail("Prepared migration is not bound to a verified release pair")
    raise SystemExit(0)

source = state.get("source_release")
if (
    action not in {"rollback", "auto-rollback"}
    or point_of_no_return
    or not isinstance(source, dict)
    or ref != source.get("image_ref")
    or digest != source.get("digest")
    or manifest_hash != source.get("manifest_hash")
):
    fail("Forward-only event-mapping migration forbids this image")
PY
}

validate_image_contract() {
  local ref="$1" digest="$2" manifest="$3" config_hash="$4" source_id="$5"
  local mapping_generation="${6:-}"
  local git_head="${source_id%%:*}" key expected actual image_config_hash final_digest
  local -a keys=(
    "org.opencontainers.image.version"
    "org.opencontainers.image.revision"
    "io.ai-filter.compose-manifest.sha256"
    "io.ai-filter.config.sha256"
    "io.ai-filter.source.identity"
  )
  local -a expected_values=(
    "$ref" "$git_head" "$manifest" "$config_hash" "$source_id"
  )
  if [[ -n "$PRIVATE_CONTRACT" && "$manifest" == "$(sha256sum "$COMPOSE_FILE" | awk '{print $1}')" ]]; then
    private_contract check || return $?
    keys+=("io.ai-filter.private-release.sha256")
    keys+=("io.ai-filter.runtime-config.sha256")
    expected_values+=("$PRIVATE_CONTRACT_SHA" "$PRIVATE_RUNTIME_CONFIG_SHA")
  fi
  if [[ -n "$mapping_generation" ]]; then
    keys+=("io.ai-filter.event-mapping-generation")
    expected_values+=("$mapping_generation")
  fi

  validate_runtime_digest "$digest"
  if [[ ! "$manifest" =~ ^[0-9a-f]{64}$ || \
        ! "$config_hash" =~ ^[0-9a-f]{64}$ || \
        ! "$source_id" =~ ^[0-9a-f]{40,64}:(clean|[0-9a-f]{64})$ ]]; then
    echo "Invalid expected image contract for $ref" >&2
    return 3
  fi
  for index in "${!keys[@]}"; do
    key="${keys[$index]}"
    expected="${expected_values[$index]}"
    actual="$(image_label_value "$digest" "$key")" || return 3
    if [[ "$actual" != "$expected" ]]; then
      echo "Image contract label mismatch for $ref: $key" >&2
      return 3
    fi
  done

  image_config_hash="$(
    docker run --rm --network none --read-only --cap-drop ALL \
      --entrypoint python "$digest" -c \
      'import hashlib; print(hashlib.sha256(open("/app/config.yaml", "rb").read()).hexdigest())'
  )" || return 3
  if [[ "$image_config_hash" != "$config_hash" ]]; then
    echo "Image /app/config.yaml hash mismatch for $ref" >&2
    return 3
  fi
  final_digest="$(resolved_image_id "$ref")"
  if [[ "$final_digest" != "$digest" ]]; then
    echo "Image ref changed while validating build contract: $ref" >&2
    return 3
  fi
}

validate_recorded_image_contract_if_present() {
  local ref="$1" digest="$2" manifest="${3:-}" target
  target="$(build_contract_path "$ref")"
  [[ -e "$target" || -L "$target" ]] || return 0
  read_build_contract "$ref"
  if [[ "$CONTRACT_DIGEST" != "$digest" ]] || \
     [[ -n "$manifest" && "$CONTRACT_MANIFEST" != "$manifest" ]]; then
    echo "Recorded release does not match its build contract: $ref" >&2
    return 3
  fi
  validate_image_contract "$CONTRACT_REF" "$CONTRACT_DIGEST" \
    "$CONTRACT_MANIFEST" "$CONTRACT_CONFIG_HASH" "$CONTRACT_SOURCE_ID" \
    "$CONTRACT_MAPPING_GENERATION"
}

validate_build_contract_for_deploy() {
  local ref="$1" digest="$2" manifest="$3" config_hash source_id
  local reason actor audit_file
  if [[ "$REQUIRE_BUILD_CONTRACT" != "1" ]]; then
    reason="${RELEASE_BUILD_CONTRACT_OVERRIDE_REASON:-}"
    actor="${RELEASE_BUILD_CONTRACT_OVERRIDE_ACTOR:-${SUDO_USER:-$(id -un)}}"
    validate_dirty_override_field "RELEASE_BUILD_CONTRACT_OVERRIDE_REASON" "$reason" 512 || return $?
    validate_dirty_override_field "RELEASE_BUILD_CONTRACT_OVERRIDE_ACTOR" "$actor" 128 || return $?
    audit_file="${REGISTRY_DIR}/build-contract-overrides.tsv"
    touch "$audit_file"
    chmod 0600 "$audit_file"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ref" "$digest" "$manifest" \
      "$RELEASE_SOURCE_ID" "$actor" "$reason" >> "$audit_file"
    echo "WARNING: accepted audited deploy build-contract override" >&2
    return 0
  fi
  if [[ -n "$PRIVATE_CONTRACT" ]]; then
    private_contract check --fresh || return $?
  fi
  config_hash="$(release_config_hash)"
  source_id="$RELEASE_SOURCE_ID"
  if [[ ! "$source_id" =~ ^[0-9a-f]{40,64}:(clean|[0-9a-f]{64})$ ]]; then
    echo "Release source fingerprint is unavailable" >&2
    return 3
  fi
  read_build_contract "$ref" || return $?
  if [[ "$CONTRACT_DIGEST" != "$digest" ]] || \
     [[ "$CONTRACT_MANIFEST" != "$manifest" ]] || \
     [[ "$CONTRACT_CONFIG_HASH" != "$config_hash" ]] || \
     [[ "$CONTRACT_SOURCE_ID" != "$source_id" ]] || \
     [[ "$CONTRACT_PRIVATE_SHA" != "$PRIVATE_CONTRACT_SHA" ]]; then
    echo "Build/deploy contract mismatch for $ref (image, manifest, config, or source changed)" >&2
    return 3
  fi
  validate_image_contract "$CONTRACT_REF" "$CONTRACT_DIGEST" \
    "$CONTRACT_MANIFEST" "$CONTRACT_CONFIG_HASH" "$CONTRACT_SOURCE_ID" \
    "$CONTRACT_MAPPING_GENERATION"
}

validate_health_services() {
  local services="$1" service count=0
  for service in $services; do
    if [[ ! "$service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
      echo "Invalid health service name: $service" >&2
      return 2
    fi
    count=$((count + 1))
  done
  if [[ "$count" -eq 0 ]]; then
    echo "Health service list must not be empty" >&2
    return 2
  fi
}

normalize_health_services() {
  local services="$1" service normalized=""
  validate_health_services "$services"
  for service in $services; do
    if [[ -n "$normalized" ]]; then
      normalized+=" "
    fi
    normalized+="$service"
  done
  printf '%s\n' "$normalized"
}

archive_manifest() {
  local source="$1" hash target tmp
  if [[ ! -f "$source" || ! -r "$source" ]]; then
    echo "Compose manifest is not a readable file: $source" >&2
    return 2
  fi
  hash="$(sha256sum "$source" | awk '{print $1}')"
  if [[ ! "$hash" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Could not hash Compose manifest: $source" >&2
    return 2
  fi
  target="${MANIFEST_DIR}/${hash}.compose.yml"
  if [[ ! -f "$target" ]]; then
    tmp="${target}.tmp.$$"
    cp -- "$source" "$tmp"
    if [[ -n "$PRIVATE_CONTRACT" ]]; then
      chmod 0400 "$tmp"
    else
      chmod 0644 "$tmp"
    fi
    mv -f "$tmp" "$target"
  fi
  if [[ "$(sha256sum "$target" | awk '{print $1}')" != "$hash" ]]; then
    echo "Archived Compose manifest hash mismatch: $target" >&2
    return 3
  fi
  printf '%s\n' "$hash"
}

manifest_path() {
  local hash="$1" target actual
  if [[ ! "$hash" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Invalid Compose manifest hash: $hash" >&2
    return 2
  fi
  target="${MANIFEST_DIR}/${hash}.compose.yml"
  if [[ ! -f "$target" ]]; then
    echo "Archived Compose manifest is missing: $target" >&2
    return 2
  fi
  actual="$(sha256sum "$target" | awk '{print $1}')"
  if [[ "$actual" != "$hash" ]]; then
    echo "Archived Compose manifest was modified: $target" >&2
    return 3
  fi
  printf '%s\n' "$target"
}

archive_config_snapshot() {
  local source="$1" hash target tmp
  hash="$(release_config_hash "$source")"
  target="${CONFIG_SNAPSHOT_DIR}/${hash}.yaml"
  if [[ -L "$target" || ( -e "$target" && ! -f "$target" ) ]]; then
    echo "Invalid archived config snapshot path: $target" >&2
    return 3
  fi
  if [[ ! -e "$target" ]]; then
    tmp="${target}.tmp.$$"
    cp -- "$source" "$tmp"
    chmod 0600 "$tmp"
    if [[ "$(release_config_hash "$tmp")" != "$hash" ]]; then
      rm -f -- "$tmp"
      echo "Config changed while it was being archived: $source" >&2
      return 3
    fi
    mv -f "$tmp" "$target"
  fi
  if [[ "$(release_config_hash "$target")" != "$hash" ]]; then
    echo "Archived config snapshot hash mismatch: $target" >&2
    return 3
  fi
  chmod 0600 "$target"
  printf '%s\n' "$target"
}

rewrite_legacy_config_mounts() {
  local source="$1" target="$2" snapshot="$3"
  python3 -I - "$source" "$target" "$snapshot" <<'PY'
import re
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
snapshot = sys.argv[3]
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
count = 0

short = re.compile(
    r"^(?P<prefix>\s*-\s*)(?P<quote>['\"]?)\./config\.yaml:/app/config\.yaml"
    r"(?P<mode>:[^'\"\s#]+)?(?P=quote)(?P<trail>\s*(?:#.*)?)(?P<nl>\r?\n?)$"
)
for index, line in enumerate(lines):
    match = short.match(line)
    if not match:
        continue
    value = f"{snapshot}:/app/config.yaml{match.group('mode') or ''}"
    value = "'" + value.replace("'", "''") + "'"
    lines[index] = (
        match.group("prefix") + value + match.group("trail") + match.group("nl")
    )
    count += 1

source_line = re.compile(
    r"^(?P<indent>\s*)source:\s*(?P<quote>['\"]?)\./config\.yaml(?P=quote)"
    r"(?P<trail>\s*(?:#.*)?)(?P<nl>\r?\n?)$"
)
target_line = re.compile(
    r"^\s*target:\s*['\"]?/app/config\.yaml['\"]?\s*(?:#.*)?(?:\r?\n)?$"
)
list_item = re.compile(r"^(?P<indent>\s*)-\s*")
for index, line in enumerate(lines):
    match = source_line.match(line)
    if not match:
        continue
    item_start = None
    item_indent = None
    for cursor in range(index - 1, -1, -1):
        candidate = list_item.match(lines[cursor])
        if candidate and len(candidate.group("indent")) < len(match.group("indent")):
            item_start = cursor
            item_indent = len(candidate.group("indent"))
            break
    if item_start is None:
        continue
    item_end = len(lines)
    for cursor in range(item_start + 1, len(lines)):
        stripped = lines[cursor].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[cursor]) - len(lines[cursor].lstrip())
        if cursor > index and indent <= item_indent:
            item_end = cursor
            break
    if not any(target_line.match(candidate) for candidate in lines[item_start:item_end]):
        continue
    value = "'" + snapshot.replace("'", "''") + "'"
    lines[index] = (
        match.group("indent") + "source: " + value
        + match.group("trail") + match.group("nl")
    )
    count += 1

with target.open("w", encoding="utf-8", newline="") as handle:
    handle.write("".join(lines))
print(count)
PY
}

seal_legacy_config_manifest() {
  local manifest="$1" file config_hash snapshot_dir snapshot tmp replacements
  if [[ -n "$PRIVATE_CONTRACT" ]]; then
    file="$(manifest_path "$manifest")"
    if grep -Eq '(\./config\.yaml|@RUNTIME_CONFIG@)' "$file"; then
      echo "Private deployment requires the previous runtime config to be independently sealed before cutover" >&2
      return 3
    fi
    printf '%s\n' "$manifest"
    return 0
  fi
  local archived_snapshot sealed
  file="$(manifest_path "$manifest")"
  config_hash="$(release_config_hash "$CONFIG_FILE")"
  snapshot_dir="$(cd "$CONFIG_SNAPSHOT_DIR" && pwd -P)"
  snapshot="$snapshot_dir/$config_hash.yaml"
  tmp="$MANIFEST_DIR/legacy-config-seal.tmp.$$"
  replacements="$(rewrite_legacy_config_mounts "$file" "$tmp" "$snapshot")" || {
    rm -f -- "$tmp"
    return 3
  }
  if [[ ! "$replacements" =~ ^[0-9]+$ ]]; then
    rm -f -- "$tmp"
    echo "Could not verify legacy config mount rewrite for manifest $manifest" >&2
    return 3
  fi
  if [[ "$replacements" -eq 0 ]]; then
    rm -f -- "$tmp"
    printf '%s\n' "$manifest"
    return 0
  fi
  archived_snapshot="$(archive_config_snapshot "$CONFIG_FILE")" || {
    rm -f -- "$tmp"
    return 3
  }
  if [[ "$archived_snapshot" != "$snapshot" ]]; then
    rm -f -- "$tmp"
    echo "Config changed while sealing legacy rollback manifest $manifest" >&2
    return 3
  fi
  sealed="$(archive_manifest "$tmp")" || {
    rm -f -- "$tmp"
    return 3
  }
  rm -f -- "$tmp"
  echo "Sealed legacy host config for rollback in manifest $sealed" >&2
  printf '%s\n' "$sealed"
}

compose_validate() {
  local ref="$1" manifest="$2" file
  file="$(manifest_path "$manifest")"
  IMAGE_REF="$ref" release_compose -f "$file" \
    config --quiet || return $?

  # Compose may return success after warning about an interpolated Nginx
  # variable and silently render it as an empty string.  For manifests that
  # contain the versioned gateway, validate the *mounted* config with the
  # pinned Nginx image.  Older archived manifests do not have this config and
  # remain valid rollback targets.
  if grep -Fq 'ai-filter-gateway-nginx:' "$file"; then
    IMAGE_REF="$ref" release_compose -f "$file" \
      run --rm --no-deps ai-filter-web nginx -t || return $?
  fi
}

validate_runtime_secrets_for_manifest() {
  local manifest="$1" file secret_name secret_file mode
  file="$(manifest_path "$manifest")"
  if [[ -n "$PRIVATE_CONTRACT" ]]; then
    private_contract check
    return $?
  fi
  for secret_name in \
    dashboard_session_secret \
    ai_filter_integration_token \
    quant_market_context_token
  do
    if ! grep -Fq "${secret_name}:" "$file"; then
      continue
    fi
    secret_file="${ROOT}/secrets/${secret_name}"
    if [[ -L "$secret_file" || ! -f "$secret_file" || ! -r "$secret_file" ]]; then
      echo "Runtime secret must be a readable regular non-symlink file: $secret_file" >&2
      return 3
    fi
    mode="$(stat -c '%a' -- "$secret_file")" || return 3
    if [[ "$mode" != "400" && "$mode" != "600" ]]; then
      echo "Runtime secret must have mode 0400 or 0600: $secret_file" >&2
      return 3
    fi
    # Validate the opened file as raw bytes. Bash command substitution cannot
    # safely inspect secrets because it silently drops NUL bytes.
    if ! python3 -I "${ROOT}/scripts/validate_runtime_secret.py" "$secret_file"; then
      echo "Runtime secret must contain 32-4096 ASCII graphic bytes and at most one final LF: $secret_file" >&2
      return 3
    fi
  done
}

compose_config_services() {
  local ref="$1" manifest="$2" file
  validate_runtime_digest "$ref"
  file="$(manifest_path "$manifest")"
  IMAGE_REF="$ref" release_compose -f "$file" config --services
}

validate_manifest_health_services() {
  local ref="$1" manifest="$2" services="$3"
  local configured service
  validate_health_services "$services"
  configured="$(compose_config_services "$ref" "$manifest")"
  for service in $services; do
    if ! grep -Fqx -- "$service" <<< "$configured"; then
      echo "Health service is not defined by archived manifest: $service" >&2
      return 2
    fi
  done
}

compose_up() {
  local ref="$1" manifest="$2" file
  validate_runtime_digest "$ref"
  file="$(manifest_path "$manifest")"
  # Modern gateway manifests are converged in dependency order. Keeping the
  # old gateway alive while replacing a slow-starting API can make its proxied
  # health check turn unhealthy, which causes Compose to abandon the worker.
  # Each command has an explicit return because this function is called from
  # an `if ! compose_up` condition, where Bash suppresses errexit semantics.
  if grep -Fq 'ai-filter-gateway-nginx:' "$file"; then
    IMAGE_REF="$ref" release_compose -f "$file" \
      up -d --no-build --pull never redis rsshub || return $?
    wait_healthy "$ref" "$manifest" "redis rsshub" "infra" || return $?
    IMAGE_REF="$ref" release_compose -f "$file" \
      up -d --no-build --pull never --no-deps --force-recreate ai-filter-api || return $?
    wait_healthy "$ref" "$manifest" "ai-filter-api" "api" || return $?
    # Compose does not always recreate a container when only inline config
    # content changes under the same config name. Recreate the lightweight
    # gateway after the API is healthy so its end-to-end probe stays useful.
    IMAGE_REF="$ref" release_compose -f "$file" \
      up -d --no-build --pull never --no-deps --force-recreate ai-filter-web || return $?
    wait_healthy "$ref" "$manifest" "ai-filter-web" "web" || return $?
    IMAGE_REF="$ref" release_compose -f "$file" \
      up -d --no-build --pull never --no-deps --force-recreate ai-filter-worker || return $?
    wait_healthy "$ref" "$manifest" "ai-filter-worker" "worker" || return $?
  fi
  IMAGE_REF="$ref" release_compose -f "$file" \
    up -d --no-build --pull never --remove-orphans || return $?
}

service_state() {
  local ref="$1" manifest="$2" service="$3" file container_id
  validate_runtime_digest "$ref"
  file="$(manifest_path "$manifest")"
  container_id="$(IMAGE_REF="$ref" release_compose -f "$file" ps --all -q "$service")"
  if [[ -z "$container_id" ]]; then
    printf '%s\n' "missing"
    return
  fi
  IMAGE_REF="$ref" docker inspect "$container_id" \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}'
}

capture_health_timeout_diagnostics() {
  local ref="$1" manifest="$2" services="$3" failed_service="$4"
  local failed_state="$5" phase="$6" service helper
  local -a command
  [[ "$CAPTURE_HEALTH_DIAGNOSTICS" == "1" ]] || return 0

  helper="${ROOT}/scripts/capture_release_diagnostics.py"
  if [[ ! -f "$helper" || -L "$helper" ]]; then
    echo "Health diagnostics unavailable: helper is missing or unsafe" >&2
    return 0
  fi

  command=(
    timeout --signal=TERM --kill-after=2s 20s
    python3 -I "$helper"
    --registry-dir "$REGISTRY_DIR"
    --project-dir "$ROOT"
    --compose-file "$(manifest_path "$manifest")"
    --image-ref "$ref"
    --manifest-hash "$manifest"
    --phase "$phase"
    --failed-service "$failed_service"
    --failed-state "$failed_state"
  )
  for service in $services; do
    command+=(--service "$service")
  done

  # Diagnostics are deliberately best-effort. A collector problem must never
  # change the original health result or delay the existing rollback path.
  if ! IMAGE_REF="$ref" "${command[@]}" >&2; then
    echo "Health diagnostics capture failed; continuing original recovery path" >&2
  fi
  return 0
}

wait_healthy() {
  local ref="$1" manifest="$2" services="$3" phase="${4:-health}"
  local deadline state all_healthy service
  validate_runtime_digest "$ref"
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while true; do
    all_healthy=1
    for service in $services; do
      state="$(service_state "$ref" "$manifest" "$service" 2>/dev/null || true)"
      if [[ "$state" != "healthy" && "$state" != "running" ]]; then
        all_healthy=0
        break
      fi
    done
    if [[ "$all_healthy" -eq 1 ]]; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "Health timeout for $ref (last ${service:-service} state: ${state:-unknown})" >&2
      capture_health_timeout_diagnostics \
        "$ref" "$manifest" "$services" "${service:-service}" \
        "${state:-unknown}" "$phase"
      return 1
    fi
    sleep "$HEALTH_POLL"
  done
}

verify_runtime_source_immutable() {
  local ref="$1" manifest="$2" service container_id drift
  validate_runtime_digest "$ref"
  for service in ai-filter-api ai-filter-worker webhook-worker ai-filter-web; do
    container_id="$(IMAGE_REF="$ref" release_compose \
      -f "$(manifest_path "$manifest")" ps -q "$service" 2>/dev/null || true)"
    if [[ -z "$container_id" ]]; then
      continue
    fi
    drift="$(docker diff "$container_id" 2>/dev/null | \
      grep -E '^[ACD] /app/(.*\.py|config\.yaml)$' || true)"
    if [[ -n "$drift" ]]; then
      echo "Runtime source/config drift detected in $service:" >&2
      printf '%s\n' "$drift" >&2
      return 1
    fi
  done
}

state_file() {
  local prefix="$1" field="$2"
  printf '%s/%s.%s\n' "$REGISTRY_DIR" "$prefix" "$field"
}

state_bundle_file() {
  local prefix="$1"
  printf '%s/%s.state\n' "$REGISTRY_DIR" "$prefix"
}

state_any_present() {
  local prefix="$1" field
  [[ -e "$(state_bundle_file "$prefix")" ]] && return 0
  for field in ref digest manifest health-services; do
    [[ -e "$(state_file "$prefix" "$field")" ]] && return 0
  done
  return 1
}

state_complete() {
  local prefix="$1" field bundle line_count
  bundle="$(state_bundle_file "$prefix")"
  if [[ -e "$bundle" ]]; then
    [[ -s "$bundle" ]] || return 1
    line_count="$(awk 'END {print NR}' "$bundle")"
    [[ "$line_count" -eq 4 ]] || return 1
    while IFS= read -r field; do
      [[ -n "$field" ]] || return 1
    done < "$bundle"
    return 0
  fi
  for field in ref digest manifest health-services; do
    [[ -s "$(state_file "$prefix" "$field")" ]] || return 1
  done
}

read_state() {
  local prefix="$1" bundle value
  local -a values=()
  bundle="$(state_bundle_file "$prefix")"
  if [[ -e "$bundle" ]]; then
    while IFS= read -r value; do
      values[${#values[@]}]="$value"
    done < "$bundle"
    if [[ "${#values[@]}" -ne 4 ]]; then
      echo "Invalid $prefix release state bundle" >&2
      return 3
    fi
    STATE_REF="${values[0]}"
    STATE_DIGEST="${values[1]}"
    STATE_MANIFEST="${values[2]}"
    STATE_HEALTH_SERVICES="${values[3]}"
    return 0
  fi
  STATE_REF="$(<"$(state_file "$prefix" ref)")"
  STATE_DIGEST="$(<"$(state_file "$prefix" digest)")"
  STATE_MANIFEST="$(<"$(state_file "$prefix" manifest)")"
  STATE_HEALTH_SERVICES="$(<"$(state_file "$prefix" health-services)")"
}

write_state() {
  local prefix="$1" ref="$2" digest="$3" manifest="$4" services="$5"
  local bundle
  services="$(normalize_health_services "$services")"
  # Compatibility mirrors are staged first. The .state bundle is the atomic
  # commit point read by subsequent invocations while the release lock serializes
  # live readers. A crash before the commit leaves the prior complete bundle.
  write_atomic "$(state_file "$prefix" ref)" "$ref"
  write_atomic "$(state_file "$prefix" digest)" "$digest"
  write_atomic "$(state_file "$prefix" manifest)" "$manifest"
  write_atomic "$(state_file "$prefix" health-services)" "$services"
  bundle="${ref}"$'\n'"${digest}"$'\n'"${manifest}"$'\n'"${services}"
  write_atomic "$(state_bundle_file "$prefix")" "$bundle"
}

validate_recorded_state() {
  local ref="$1" digest="$2" manifest="$3" services="$4" actual
  validate_image_ref "$ref"
  if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "Invalid recorded image digest for $ref: $digest" >&2
    return 3
  fi
  manifest_path "$manifest" >/dev/null
  validate_health_services "$services"
  ensure_image "$ref"
  actual="$(resolved_image_id "$ref")"
  if [[ "$actual" != "$digest" ]]; then
    echo "Recorded release digest mismatch for $ref: expected $digest, resolved $actual" >&2
    return 3
  fi
  compose_validate "$digest" "$manifest"
  validate_runtime_secrets_for_manifest "$manifest"
  validate_manifest_health_services "$digest" "$manifest" "$services"
  validate_recorded_image_contract_if_present "$ref" "$digest" "$manifest"
}

bootstrap_or_validate_registry() {
  local ref digest manifest services previous_file previous_services
  if state_any_present current; then
    if ! state_complete current; then
      if [[ ! -s "$(state_file current ref)" ]]; then
        echo "Incomplete current release state (missing current.ref); refusing deployment" >&2
        return 3
      fi
      previous_file="${PREVIOUS_COMPOSE_FILE:-}"
      previous_services="${PREVIOUS_HEALTH_SERVICES:-}"
      if [[ -z "$previous_file" || -z "$previous_services" ]]; then
        echo "Existing current.ref has no complete rollback manifest. Set PREVIOUS_COMPOSE_FILE and PREVIOUS_HEALTH_SERVICES before deployment." >&2
        return 3
      fi
      ref="$(<"$(state_file current ref)")"
      validate_image_ref "$ref"
      previous_services="$(normalize_health_services "$previous_services")"
      ensure_image "$ref"
      digest="$(resolved_image_id "$ref")"
      if [[ -s "$(state_file current digest)" ]] && \
         [[ "$(<"$(state_file current digest)")" != "$digest" ]]; then
        echo "Existing current.digest does not match current.ref; refusing bootstrap" >&2
        return 3
      fi
      manifest="$(archive_manifest "$previous_file")"
      compose_validate "$digest" "$manifest"
      validate_manifest_health_services "$digest" "$manifest" "$previous_services"
      write_state current "$ref" "$digest" "$manifest" "$previous_services"
      echo "Bootstrapped rollback manifest $manifest for $ref" >&2
    fi
    read_state current
    validate_recorded_state "$STATE_REF" "$STATE_DIGEST" "$STATE_MANIFEST" "$STATE_HEALTH_SERVICES"
  fi

  if state_any_present previous; then
    if ! state_complete previous; then
      echo "Incomplete previous release state; refusing deployment" >&2
      return 3
    fi
    read_state previous
    validate_recorded_state "$STATE_REF" "$STATE_DIGEST" "$STATE_MANIFEST" "$STATE_HEALTH_SERVICES"
  fi
}

record_current() {
  local action="$1" ref="$2" digest="$3" manifest="$4" services="$5"
  local old_ref="${6:-}" old_digest="${7:-}" old_manifest="${8:-}" old_services="${9:-}"
  if [[ -n "$old_ref" ]] && \
     [[ "$old_ref|$old_digest|$old_manifest|$old_services" != "$ref|$digest|$manifest|$services" ]]; then
    write_state previous "$old_ref" "$old_digest" "$old_manifest" "$old_services"
  fi
  write_state current "$ref" "$digest" "$manifest" "$services"
  # A successful standard release must not retain a former forced deployment's
  # health-recovery override, even when it redeploys the same ref and digest.
  rm -f -- "${REGISTRY_DIR}/current.override" "${REGISTRY_DIR}/current.override-sha256"
  record_history "$action" "$ref" "$digest" "healthy" "$manifest" "$services"
}

restore_previous() {
  local ref="$1" digest="$2" manifest="$3" services="$4" actual
  if [[ -z "$ref" ]]; then
    echo "No previous release state is recorded" >&2
    return 1
  fi
  echo "Restoring previous release $ref with manifest $manifest" >&2
  validate_recorded_state "$ref" "$digest" "$manifest" "$services"
  validate_event_mapping_release_target \
    "auto-rollback" "$ref" "$digest" "$manifest" "" "" "" || return $?
  compose_up "$digest" "$manifest"
  if ! wait_healthy "$digest" "$manifest" "$services" "rollback-final"; then
    echo "Automatic rollback also failed health checks" >&2
    return 1
  fi
  actual="$(resolved_image_id "$ref")"
  record_history "auto-rollback" "$ref" "$actual" "healthy" "$manifest" "$services"
}

deploy_ref() {
  local action="$1" ref="$2" manifest="$3" services="$4"
  local previous_ref="" previous_digest="" previous_manifest="" previous_services=""
  local digest sealed_manifest
  validate_image_ref "$ref"
  services="$(normalize_health_services "$services")"
  bootstrap_or_validate_registry
  if state_complete current; then
    read_state current
    previous_ref="$STATE_REF"
    previous_digest="$STATE_DIGEST"
    previous_manifest="$STATE_MANIFEST"
    previous_services="$STATE_HEALTH_SERVICES"
    sealed_manifest="$(seal_legacy_config_manifest "$previous_manifest")"
    if [[ "$sealed_manifest" != "$previous_manifest" ]]; then
      previous_manifest="$sealed_manifest"
      validate_recorded_state "$previous_ref" "$previous_digest" \
        "$previous_manifest" "$previous_services"
      # Persist the safe rollback target before any new-release preflight or
      # container mutation. This fallback captures the config visible now; it
      # cannot prove the pre-checkout bytes, so operators must prefer the
      # explicit seal-current command before changing the worktree.
      write_state current "$previous_ref" "$previous_digest" \
        "$previous_manifest" "$previous_services"
      record_history "seal-current" "$previous_ref" "$previous_digest" \
        "fallback_sealed" "$previous_manifest" "$previous_services"
      echo "WARNING: fallback-sealed current config during deploy; pre-change provenance is not provable" >&2
    fi
  fi

  ensure_image "$ref"
  verify_requested_digest "$ref"
  digest="$(resolved_image_id "$ref")"
  assert_ref_consistency "$ref" "$digest"
  if ! validate_event_mapping_release_target \
    "$action" "$ref" "$digest" "$manifest" \
    "$previous_ref" "$previous_digest" "$previous_manifest"; then
    record_history "$action" "$ref" "$digest" \
      "migration_fence_failed" "$manifest" "$services"
    return 3
  fi

  if [[ "$action" == "deploy" ]] && \
     ! validate_build_contract_for_deploy "$ref" "$digest" "$manifest"; then
    record_history "$action" "$ref" "$digest" "preflight_failed" "$manifest" "$services"
    return 3
  fi

  if ! compose_validate "$digest" "$manifest" || \
     ! validate_runtime_secrets_for_manifest "$manifest" || \
     ! validate_manifest_health_services "$digest" "$manifest" "$services"; then
    record_history "$action" "$ref" "$digest" "preflight_failed" "$manifest" "$services"
    return 3
  fi

  if [[ -n "$PRIVATE_CONTRACT" ]]; then
    private_contract preflight --fresh || return 3
    private_contract image-probe --image "$digest" || return 3
  fi
  if ! compose_up "$digest" "$manifest"; then
    record_history "$action" "$ref" "$digest" "compose_failed" "$manifest" "$services"
    if [[ -n "$previous_ref" ]] && \
       [[ "$previous_ref|$previous_manifest" != "$ref|$manifest" ]]; then
      if [[ "$AUTO_ROLLBACK" == "1" ]]; then
        restore_previous "$previous_ref" "$previous_digest" "$previous_manifest" "$previous_services" || return 5
      else
        echo "Automatic image rollback is disabled; leaving recovery to the maintenance caller" >&2
      fi
    fi
    return 4
  fi
  if wait_healthy "$digest" "$manifest" "$services" "release-final" && \
     verify_runtime_source_immutable "$digest" "$manifest" && \
     verify_private_runtime "$digest"; then
    record_current "$action" "$ref" "$digest" "$manifest" "$services" \
      "$previous_ref" "$previous_digest" "$previous_manifest" "$previous_services"
    echo "${action}: ${ref} (${digest}), manifest ${manifest}"
    return 0
  fi

  record_history "$action" "$ref" "$digest" "health_failed" "$manifest" "$services"
  if [[ -n "$previous_ref" ]] && \
     [[ "$previous_ref|$previous_manifest" != "$ref|$manifest" ]]; then
    if [[ "$AUTO_ROLLBACK" == "1" ]]; then
      restore_previous "$previous_ref" "$previous_digest" "$previous_manifest" "$previous_services" || return 5
    else
      echo "Automatic image rollback is disabled; leaving recovery to the maintenance caller" >&2
    fi
  else
    echo "Release unhealthy and no distinct previous release state is recorded" >&2
  fi
  return 4
}

verify_private_runtime() {
  local digest="$1"
  [[ -n "$PRIVATE_CONTRACT" ]] || return 0
  private_contract verify-runtime --image "$digest"
}

recorded_target_for_rollback() {
  local wanted="$1" prefix
  for prefix in previous current; do
    if state_complete "$prefix"; then
      read_state "$prefix"
      if [[ "$STATE_REF" == "$wanted" ]]; then
        ROLLBACK_MANIFEST="$STATE_MANIFEST"
        ROLLBACK_HEALTH_SERVICES="$STATE_HEALTH_SERVICES"
        return 0
      fi
    fi
  done
  echo "No recorded manifest and health-service set for rollback ref: $wanted" >&2
  return 3
}

[[ -n "$cmd" && -n "$image_ref" ]] || usage

case "$cmd" in
  build)
    validate_image_ref "$image_ref"
    if [[ "$image_ref" == *@sha256:* || "$image_ref" == sha256:* ]]; then
      echo "build requires an immutable version tag, not a pre-existing digest" >&2
      exit 2
    fi
    assert_ref_unused_for_build "$image_ref"
    require_clean_release_tree "$cmd" "$image_ref"
    if [[ -z "$PRIVATE_CONTRACT" && "$COMPOSE_FILE" != "${ROOT}/docker-compose.yml" ]]; then
      echo "Refusing build with RELEASE_COMPOSE_FILE; the test attestation covers the committed Compose manifest only" >&2
      exit 8
    fi
    require_valid_release_test_attestation "$RELEASE_SOURCE_ID"
    if [[ -n "$PRIVATE_CONTRACT" ]]; then
      private_contract check --fresh
    fi
    if docker image inspect "$image_ref" >/dev/null 2>&1; then
      echo "Refusing to overwrite existing immutable version: $image_ref" >&2
      exit 3
    fi
    create_build_snapshot
    build_manifest_file="$COMPOSE_FILE"
    if [[ "$COMPOSE_FILE" == "${ROOT}/docker-compose.yml" ]]; then
      build_manifest_file="${BUILD_SNAPSHOT_DIR}/docker-compose.yml"
    fi
    manifest="$(archive_manifest "$build_manifest_file")"
    config_hash="$(release_config_hash "${BUILD_SNAPSHOT_DIR}/config.yaml")"
    git_head="${RELEASE_SOURCE_ID%%:*}"
    compose_validate "$image_ref" "$manifest"
    IMAGE_REF="$image_ref" docker build --tag "$image_ref" \
      --build-arg "AI_FILTER_BUILD_VERSION=$image_ref" \
      --build-arg "AI_FILTER_BUILD_GIT_SHA=$git_head" \
      --build-arg "AI_FILTER_BUILD_MANIFEST_SHA=$manifest" \
      --build-arg "AI_FILTER_BUILD_CONFIG_SHA=$config_hash" \
      --build-arg "AI_FILTER_BUILD_SOURCE_ID=$RELEASE_SOURCE_ID" \
      --build-arg "AI_FILTER_PRIVATE_RELEASE_SHA=${PRIVATE_CONTRACT_SHA:-none}" \
      --build-arg "AI_FILTER_RUNTIME_CONFIG_SHA=${PRIVATE_RUNTIME_CONFIG_SHA:-$config_hash}" \
      --build-arg "AI_FILTER_MAPPING_MIGRATION_GENERATION=$EVENT_MAPPING_MIGRATION_GENERATION" \
      "$BUILD_SNAPSHOT_DIR"
    ensure_image "$image_ref"
    digest="$(resolved_image_id "$image_ref")"
    assert_ref_consistency "$image_ref" "$digest"
    record_history "build" "$image_ref" "$digest" "image_built" "$manifest" "$HEALTH_SERVICES"
    if ! validate_image_contract "$image_ref" "$digest" "$manifest" \
      "$config_hash" "$RELEASE_SOURCE_ID" \
      "$EVENT_MAPPING_MIGRATION_GENERATION"; then
      record_history "build" "$image_ref" "$digest" "contract_failed" "$manifest" "$HEALTH_SERVICES"
      exit 3
    fi
    if [[ -n "$PRIVATE_CONTRACT" ]]; then
      private_contract image-probe --image "$digest"
    fi
    write_build_contract "$image_ref" "$digest" "$manifest" "$config_hash" "$RELEASE_SOURCE_ID"
    record_history "build" "$image_ref" "$digest" "built" "$manifest" "$HEALTH_SERVICES"
    cleanup_build_snapshot
    echo "Built ${image_ref} (${digest}) with manifest ${manifest}"
    ;;
  deploy)
    validate_image_ref "$image_ref"
    require_clean_release_tree "$cmd" "$image_ref"
    manifest="$(archive_manifest "$COMPOSE_FILE")"
    deploy_ref "deploy" "$image_ref" "$manifest" "$HEALTH_SERVICES"
    ;;
  rollback)
    bootstrap_or_validate_registry
    recorded_target_for_rollback "$image_ref"
    deploy_ref "rollback" "$image_ref" "$ROLLBACK_MANIFEST" "$ROLLBACK_HEALTH_SERVICES"
    ;;
  verify)
    validate_image_ref "$image_ref"
    ensure_image "$image_ref"
    verify_requested_digest "$image_ref"
    digest="$(resolved_image_id "$image_ref")"
    assert_ref_consistency "$image_ref" "$digest"
    validate_recorded_image_contract_if_present "$image_ref" "$digest"
    if [[ -n "$PRIVATE_CONTRACT" ]]; then
      read_build_contract "$image_ref"
      if [[ "$CONTRACT_PRIVATE_SHA" != "$PRIVATE_CONTRACT_SHA" ]]; then
        echo "Private verification requires the matching private build contract" >&2
        exit 3
      fi
      private_contract image-probe --image "$digest"
      verify_private_runtime "$digest"
    fi
    echo "Verified ${image_ref} (${digest})"
    ;;
  seal-current)
    validate_image_ref "$image_ref"
    if ! state_complete current; then
      echo "A complete current release state is required before seal-current" >&2
      exit 3
    fi
    read_state current
    if [[ "$STATE_REF" != "$image_ref" ]]; then
      echo "seal-current ref mismatch: registry has $STATE_REF, requested $image_ref" >&2
      exit 3
    fi
    validate_recorded_state "$STATE_REF" "$STATE_DIGEST" \
      "$STATE_MANIFEST" "$STATE_HEALTH_SERVICES"
    sealed_manifest="$(seal_legacy_config_manifest "$STATE_MANIFEST")"
    if [[ "$sealed_manifest" == "$STATE_MANIFEST" ]]; then
      echo "Current release has no legacy ./config.yaml bind mount to seal"
      exit 0
    fi
    validate_recorded_state "$STATE_REF" "$STATE_DIGEST" \
      "$sealed_manifest" "$STATE_HEALTH_SERVICES"
    write_state current "$STATE_REF" "$STATE_DIGEST" \
      "$sealed_manifest" "$STATE_HEALTH_SERVICES"
    record_history "seal-current" "$STATE_REF" "$STATE_DIGEST" \
      "sealed" "$sealed_manifest" "$STATE_HEALTH_SERVICES"
    echo "Sealed current release ${STATE_REF} with manifest ${sealed_manifest}"
    ;;
  *)
    usage
    ;;
esac
