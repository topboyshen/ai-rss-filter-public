#!/usr/bin/env bash
# Produce a short-lived, local release-test attestation for one clean commit.
#
# The runner deliberately has no argument passthrough: both the suite and its
# coverage threshold are part of the attested contract.  The attestation is a
# local release control, not a cryptographic proof against a privileged writer.

set -euo pipefail
umask 077

MAGIC="ai-filter-release-test-attestation-v1"
SUITE="pytest-full-cov75-v1"
ACTIVE_VAR="AI_FILTER_RELEASE_GATE_ACTIVE"

die() {
  echo "Release gate: $*" >&2
  exit 2
}

if (( $# != 0 )); then
  echo "Usage: scripts/run-release-gate.sh" >&2
  echo "This runner has a fixed suite and does not accept arguments." >&2
  exit 2
fi

if [[ "${ACTIVE:-0}" == "1" || "${AI_FILTER_RELEASE_GATE_ACTIVE:-0}" == "1" ]]; then
  die "recursive invocation is forbidden"
fi

canonical_path() {
  local path="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -- "${path}"
  elif command -v readlink >/dev/null 2>&1; then
    readlink -f -- "${path}"
  else
    return 1
  fi
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "${path}" | awk '{print $1}'
  else
    shasum -a 256 -- "${path}" | awk '{print $1}'
  fi
}

sha256_text() {
  local value="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "${value}" | sha256sum | awk '{print $1}'
  else
    printf '%s' "${value}" | shasum -a 256 | awk '{print $1}'
  fi
}

stat_uid() {
  local path="$1"
  if stat -c '%u' -- "${path}" >/dev/null 2>&1; then
    stat -c '%u' -- "${path}"
  else
    stat -f '%u' -- "${path}"
  fi
}

stat_mode() {
  local path="$1"
  if stat -c '%a' -- "${path}" >/dev/null 2>&1; then
    stat -c '%a' -- "${path}"
  else
    stat -f '%Lp' -- "${path}"
  fi
}

stat_links() {
  local path="$1"
  if stat -c '%h' -- "${path}" >/dev/null 2>&1; then
    stat -c '%h' -- "${path}"
  else
    stat -f '%l' -- "${path}"
  fi
}

validate_private_directory() {
  local path="$1" uid mode current_uid
  [[ -d "${path}" && ! -L "${path}" ]] || die "unsafe attestation directory: ${path}"
  uid="$(stat_uid "${path}")"
  mode="$(stat_mode "${path}")"
  current_uid="$(id -u)"
  [[ "${uid}" == "${current_uid}" ]] || die "attestation directory must be owned by the effective user"
  [[ "${mode}" == "700" || "${mode}" == "0700" ]] || die "attestation directory must have mode 0700"
}

validate_private_file() {
  local path="$1" uid mode links current_uid
  [[ -f "${path}" && ! -L "${path}" ]] || die "unsafe attestation file: ${path}"
  uid="$(stat_uid "${path}")"
  mode="$(stat_mode "${path}")"
  links="$(stat_links "${path}")"
  current_uid="$(id -u)"
  [[ "${uid}" == "${current_uid}" ]] || die "attestation file must be owned by the effective user"
  [[ "${mode}" == "600" || "${mode}" == "0600" ]] || die "attestation file must have mode 0600"
  [[ "${links}" == "1" ]] || die "attestation file must have exactly one hard link"
}

validate_python_file() {
  local path="$1" uid mode mode_value current_uid
  [[ -f "${path}" && ! -L "${path}" && -x "${path}" ]] || die "resolved Python must be an executable regular file"
  uid="$(stat_uid "${path}")"
  current_uid="$(id -u)"
  [[ "${uid}" == "0" || "${uid}" == "${current_uid}" ]] || die "resolved Python must be owned by root or the effective user"
  mode="$(stat_mode "${path}")"
  [[ "${mode}" =~ ^[0-7]{3,4}$ ]] || die "could not validate Python permissions"
  mode_value=$((8#${mode}))
  (( (mode_value & 0022) == 0 )) || die "resolved Python must not be group- or world-writable"
}

read_python_identity() {
  local path="$1"
  "${path}" -I -c 'import platform, sys; print(f"{platform.python_implementation()}|{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")'
}

SCRIPT_PATH="$(canonical_path "${BASH_SOURCE[0]}")" || die "could not resolve runner path"
SCRIPT_DIR="$(cd -- "$(dirname -- "${SCRIPT_PATH}")" && pwd -P)"
ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)" || die "runner is not inside a Git worktree"
ROOT="$(cd -- "${ROOT}" && pwd -P)"

[[ "${SCRIPT_PATH}" == "${ROOT}/scripts/run-release-gate.sh" ]] || die "runner must be the repository scripts/run-release-gate.sh"

SOURCE_STATUS="$(git -C "${ROOT}" status --porcelain=v1 --untracked-files=all)"
if [[ -n "${SOURCE_STATUS}" ]]; then
  echo "Refusing to attest a dirty source worktree:" >&2
  printf '%s\n' "${SOURCE_STATUS}" >&2
  exit 2
fi

HEAD_OID="$(git -C "${ROOT}" rev-parse --verify 'HEAD^{commit}')"
[[ "${HEAD_OID}" =~ ^[0-9a-f]{40,64}$ ]] || die "could not freeze a valid source commit"
SOURCE_ID="${HEAD_OID}:clean"

RUNNER_SHA256="$(sha256_file "${SCRIPT_PATH}")" || die "could not hash runner"
[[ "${RUNNER_SHA256}" =~ ^[0-9a-f]{64}$ ]] || die "invalid runner SHA-256"

PYTHON_INPUT="${AI_FILTER_TEST_PYTHON:-${ROOT}/.venv-release-gate/bin/python}"
[[ "${PYTHON_INPUT}" == /* ]] || die "AI_FILTER_TEST_PYTHON must be an absolute path"
PYTHON_DIRECTORY="$(cd -- "$(dirname -- "${PYTHON_INPUT}")" 2>/dev/null && pwd -P)" || die "could not resolve Python directory"
PYTHON_PATH="${PYTHON_DIRECTORY}/$(basename -- "${PYTHON_INPUT}")"
[[ -x "${PYTHON_PATH}" && "${PYTHON_PATH}" != *$'\n'* && "${PYTHON_PATH}" != *$'\r'* ]] || die "configured Python path is unsafe"
PYTHON_TARGET="$(canonical_path "${PYTHON_PATH}")" || die "could not resolve Python target"
[[ "${PYTHON_TARGET}" == /* && "${PYTHON_TARGET}" != *$'\n'* && "${PYTHON_TARGET}" != *$'\r'* ]] || die "resolved Python target is unsafe"
validate_python_file "${PYTHON_TARGET}"
PYTHON_SHA256="$(sha256_file "${PYTHON_TARGET}")" || die "could not hash Python"
[[ "${PYTHON_SHA256}" =~ ^[0-9a-f]{64}$ ]] || die "invalid Python SHA-256"
# Invoke through the configured venv path. Executing the final symlink target
# directly would discard pyvenv.cfg and silently test the system environment.
PYTHON_IDENTITY="$(read_python_identity "${PYTHON_PATH}")" || die "could not identify Python"
[[ "${PYTHON_IDENTITY}" =~ ^CPython\|3\.11\.[0-9]+$ ]] || die "release gate requires CPython 3.11.x"
PYTHON_IDENTITY_SHA256="$(sha256_text "${PYTHON_IDENTITY}")" || die "could not hash Python identity"
[[ "${PYTHON_IDENTITY_SHA256}" =~ ^[0-9a-f]{64}$ ]] || die "invalid Python identity SHA-256"

ATTESTATION_DIR="${RELEASE_TEST_ATTESTATION_DIR:-/run/ai-filter-release-gate/attestations}"
[[ "${ATTESTATION_DIR}" == /* && "${ATTESTATION_DIR}" != "/" && "${ATTESTATION_DIR}" != *$'\n'* && "${ATTESTATION_DIR}" != *$'\r'* ]] || die "RELEASE_TEST_ATTESTATION_DIR must be a safe absolute path"
ATTESTATION_PARENT="$(dirname -- "${ATTESTATION_DIR}")"
mkdir -p -- "${ATTESTATION_PARENT}"
if [[ ! -e "${ATTESTATION_DIR}" && ! -L "${ATTESTATION_DIR}" ]]; then
  mkdir -m 0700 -- "${ATTESTATION_DIR}"
fi
validate_private_directory "${ATTESTATION_DIR}"
[[ "$(canonical_path "${ATTESTATION_DIR}")" == "${ATTESTATION_DIR}" ]] || die "attestation directory must not traverse symlinked path components"

STATE_PATH="${ATTESTATION_DIR}/${HEAD_OID}.state"
LOG_PATH="${ATTESTATION_DIR}/${HEAD_OID}.log"
LOCK_PATH="${ATTESTATION_DIR}/attestation.lock"

if [[ ! -e "${LOCK_PATH}" && ! -L "${LOCK_PATH}" ]]; then
  ( umask 077; : > "${LOCK_PATH}" )
fi
validate_private_file "${LOCK_PATH}"
command -v flock >/dev/null 2>&1 || die "flock is required for the release/build lock protocol"
exec 9<>"${LOCK_PATH}"
if ! flock -n 9; then
  die "another release gate is already running"
fi
COMMITTED=0

# Invalidate an older result for this commit before doing any new work.  A
# failed or interrupted rerun must never leave a stale passing credential.
rm -f -- "${STATE_PATH}" "${LOG_PATH}"

TMP_BASE="${AI_FILTER_TEST_TMPDIR:-/var/tmp}"
[[ "${TMP_BASE}" == /* && "${TMP_BASE}" != "/" && -d "${TMP_BASE}" && -w "${TMP_BASE}" && -x "${TMP_BASE}" ]] || die "AI_FILTER_TEST_TMPDIR must be a safe absolute writable directory"
TMP_BASE="$(cd -- "${TMP_BASE}" && pwd -P)"
[[ "${TMP_BASE}" != "/" ]] || die "refusing to use / as the temporary directory"

WORK_ROOT="$(mktemp -d "${TMP_BASE}/ai-filter-release-gate.XXXXXX")"
WORKTREE="${WORK_ROOT}/worktree"
TEST_HOME="${WORK_ROOT}/home"
TEST_TMP="${WORK_ROOT}/tmp"
RUN_LOG="${WORK_ROOT}/release-gate.log"
mkdir -m 0700 -- "${TEST_HOME}" "${TEST_TMP}"

cleanup() {
  local original_rc=$? cleanup_rc=0
  trap - EXIT HUP INT TERM

  if [[ -n "${WORKTREE:-}" && -e "${WORKTREE}" ]]; then
    git -C "${ROOT}" worktree remove --force "${WORKTREE}" || cleanup_rc=70
  fi

  case "${WORK_ROOT:-}" in
    "${TMP_BASE}"/ai-filter-release-gate.*)
      if [[ -e "${WORK_ROOT}" ]]; then
        rm -rf -- "${WORK_ROOT}" || cleanup_rc=70
      fi
      ;;
    "") ;;
    *)
      echo "Refusing cleanup outside the controlled release-gate prefix: ${WORK_ROOT}" >&2
      cleanup_rc=70
      ;;
  esac

  # A PASS is valid only when both the main flow and all temporary cleanup
  # complete successfully. Keep the Linux flock FD held while revoking a
  # credential after any failure.
  if (( original_rc != 0 || cleanup_rc != 0 )) || [[ "${COMMITTED:-0}" != "1" ]]; then
    rm -f -- "${STATE_PATH}" "${LOG_PATH}" || cleanup_rc=70
  fi

  if (( cleanup_rc != 0 && original_rc == 0 )); then
    original_rc="${cleanup_rc}"
  fi
  exit "${original_rc}"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

git -C "${ROOT}" -c core.hooksPath=/dev/null worktree add --detach "${WORKTREE}" "${HEAD_OID}"

DETACHED_HEAD="$(git -C "${WORKTREE}" rev-parse --verify 'HEAD^{commit}')"
[[ "${DETACHED_HEAD}" == "${HEAD_OID}" ]] || die "detached worktree is not at the frozen commit"
DETACHED_STATUS="$(git -C "${WORKTREE}" status --porcelain=v1 --untracked-files=all)"
[[ -z "${DETACHED_STATUS}" ]] || die "detached worktree was not initially clean"

SAFE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
STARTED_EPOCH="$(date +%s)"
[[ "${STARTED_EPOCH}" =~ ^[0-9]+$ ]] || die "could not record start time"

set +e
(
  cd -- "${WORKTREE}"
  env -i \
    HOME="${TEST_HOME}" \
    TMPDIR="${TEST_TMP}" \
    PATH="${SAFE_PATH}" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    PYTHONNOUSERSITE=1 \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    ACTIVE=1 \
    "${ACTIVE_VAR}=1" \
      bash scripts/check-shell-syntax.sh
  static_rc=$?
  if (( static_rc != 0 )); then
    exit "${static_rc}"
  fi
  exec env -i \
    HOME="${TEST_HOME}" \
    TMPDIR="${TEST_TMP}" \
    PATH="${SAFE_PATH}" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    PYTHONNOUSERSITE=1 \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    ACTIVE=1 \
    "${ACTIVE_VAR}=1" \
    "${PYTHON_PATH}" -I -m pytest \
      --cov --cov-config=.coveragerc --cov-report=term --cov-fail-under=75
) 2>&1 | tee "${RUN_LOG}"
PIPE_STATUSES=("${PIPESTATUS[@]}")
set -e

if (( PIPE_STATUSES[0] != 0 )); then
  echo "Release gate: test suite failed with status ${PIPE_STATUSES[0]}" >&2
  exit "${PIPE_STATUSES[0]}"
fi
if (( PIPE_STATUSES[1] != 0 )); then
  die "could not capture the release-gate log"
fi

COMPLETED_EPOCH="$(date +%s)"
[[ "${COMPLETED_EPOCH}" =~ ^[0-9]+$ && "${COMPLETED_EPOCH}" -ge "${STARTED_EPOCH}" ]] || die "could not record completion time"

# Revalidate every mutable input after the suite succeeds and before publishing
# either artifact.
[[ "$(git -C "${ROOT}" rev-parse --verify 'HEAD^{commit}')" == "${HEAD_OID}" ]] || die "source HEAD changed during the release gate"
SOURCE_STATUS="$(git -C "${ROOT}" status --porcelain=v1 --untracked-files=all)"
[[ -z "${SOURCE_STATUS}" ]] || die "source worktree changed during the release gate"
[[ "$(git -C "${WORKTREE}" rev-parse --verify 'HEAD^{commit}')" == "${HEAD_OID}" ]] || die "detached worktree HEAD changed during the release gate"
DETACHED_STATUS="$(git -C "${WORKTREE}" status --porcelain=v1 --untracked-files=all)"
[[ -z "${DETACHED_STATUS}" ]] || die "detached worktree changed during the release gate"
[[ "$(sha256_file "${SCRIPT_PATH}")" == "${RUNNER_SHA256}" ]] || die "runner changed during the release gate"

PYTHON_DIRECTORY_AFTER="$(cd -- "$(dirname -- "${PYTHON_INPUT}")" 2>/dev/null && pwd -P)" || die "could not re-resolve Python directory"
PYTHON_PATH_AFTER="${PYTHON_DIRECTORY_AFTER}/$(basename -- "${PYTHON_INPUT}")"
[[ "${PYTHON_PATH_AFTER}" == "${PYTHON_PATH}" ]] || die "Python invocation path changed during the release gate"
PYTHON_TARGET_AFTER="$(canonical_path "${PYTHON_PATH_AFTER}")" || die "could not re-resolve Python target"
[[ "${PYTHON_TARGET_AFTER}" == "${PYTHON_TARGET}" ]] || die "Python target changed during the release gate"
validate_python_file "${PYTHON_TARGET_AFTER}"
[[ "$(sha256_file "${PYTHON_TARGET_AFTER}")" == "${PYTHON_SHA256}" ]] || die "Python binary changed during the release gate"
PYTHON_IDENTITY_AFTER="$(read_python_identity "${PYTHON_PATH_AFTER}")" || die "could not re-identify Python"
[[ "${PYTHON_IDENTITY_AFTER}" == "${PYTHON_IDENTITY}" ]] || die "Python identity changed during the release gate"
[[ "$(sha256_text "${PYTHON_IDENTITY_AFTER}")" == "${PYTHON_IDENTITY_SHA256}" ]] || die "Python identity hash changed during the release gate"

LOG_SHA256="$(sha256_file "${RUN_LOG}")" || die "could not hash release-gate log"
[[ "${LOG_SHA256}" =~ ^[0-9a-f]{64}$ ]] || die "invalid log SHA-256"

LOG_TEMP="$(mktemp "${ATTESTATION_DIR}/.${HEAD_OID}.log.XXXXXX")"
cp -- "${RUN_LOG}" "${LOG_TEMP}"
chmod 0600 "${LOG_TEMP}"
mv -f -- "${LOG_TEMP}" "${LOG_PATH}"
validate_private_file "${LOG_PATH}"
[[ "$(sha256_file "${LOG_PATH}")" == "${LOG_SHA256}" ]] || die "published log hash mismatch"

STATE_TEMP="$(mktemp "${ATTESTATION_DIR}/.${HEAD_OID}.state.XXXXXX")"
printf '%s\n' \
  "${MAGIC}" \
  "${SOURCE_ID}" \
  "${SUITE}" \
  "${RUNNER_SHA256}" \
  "${PYTHON_PATH}" \
  "${PYTHON_SHA256}" \
  "${PYTHON_IDENTITY_SHA256}" \
  "${STARTED_EPOCH}" \
  "${COMPLETED_EPOCH}" \
  "${LOG_SHA256}" \
  "passed" > "${STATE_TEMP}"
chmod 0600 "${STATE_TEMP}"
mv -f -- "${STATE_TEMP}" "${STATE_PATH}"
validate_private_file "${STATE_PATH}"

LINE_COUNT="$(wc -l < "${STATE_PATH}" | tr -d '[:space:]')"
[[ "${LINE_COUNT}" == "11" ]] || die "published credential does not have exactly 11 lines"

COMMITTED=1
echo "Release gate passed for ${SOURCE_ID}"
echo "Credential: ${STATE_PATH}"
