#!/usr/bin/env bash
# Run a fixed test target from a detached, clean worktree.
#
# Production secrets live below the main checkout.  The test suite creates fake
# secrets, so it must never run in that checkout or inherit its environment.

set -euo pipefail
umask 077

if (( $# != 1 )) || [[ "$1" != "focused" && "$1" != "full" ]]; then
  echo "Usage: scripts/run-release-tests.sh {focused|full}" >&2
  echo "This runner accepts a fixed mode only; pytest arguments are forbidden." >&2
  exit 2
fi
MODE="$1"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)"
ROOT="$(cd -- "${ROOT}" && pwd -P)"

if [[ "${SCRIPT_DIR}" != "${ROOT}/scripts" ]]; then
  echo "Runner must be executed from the repository scripts directory" >&2
  exit 2
fi

STATUS="$(git -C "${ROOT}" status --porcelain=v1 --untracked-files=all)"
if [[ -n "${STATUS}" ]]; then
  echo "Refusing to test a dirty source worktree:" >&2
  printf '%s\n' "${STATUS}" >&2
  exit 2
fi

COVERAGE_OUTPUT="${AI_FILTER_TEST_COVERAGE_OUTPUT:-}"
COVERAGE_DIR=""
COVERAGE_BASENAME=""
if [[ -n "${COVERAGE_OUTPUT}" ]]; then
  if [[ "${COVERAGE_OUTPUT}" != /* ]]; then
    echo "AI_FILTER_TEST_COVERAGE_OUTPUT must be an absolute path" >&2
    exit 2
  fi
  COVERAGE_DIR="$(dirname -- "${COVERAGE_OUTPUT}")"
  COVERAGE_BASENAME="$(basename -- "${COVERAGE_OUTPUT}")"
  if [[ ! -d "${COVERAGE_DIR}" || ! -w "${COVERAGE_DIR}" || \
        ! -x "${COVERAGE_DIR}" || "${COVERAGE_BASENAME}" == "." || \
        "${COVERAGE_BASENAME}" == ".." ]]; then
    echo "AI_FILTER_TEST_COVERAGE_OUTPUT must have a writable parent directory" >&2
    exit 2
  fi
  COVERAGE_DIR="$(cd -- "${COVERAGE_DIR}" && pwd -P)"
  COVERAGE_OUTPUT="${COVERAGE_DIR}/${COVERAGE_BASENAME}"
  case "${COVERAGE_OUTPUT}" in
    "${ROOT}"|"${ROOT}"/*)
      echo "Coverage output must be outside the source worktree" >&2
      exit 2
      ;;
  esac
  if [[ -L "${COVERAGE_OUTPUT}" ]] || \
     [[ -e "${COVERAGE_OUTPUT}" && ! -f "${COVERAGE_OUTPUT}" ]]; then
    echo "Refusing unsafe coverage output path: ${COVERAGE_OUTPUT}" >&2
    exit 2
  fi
fi

HEAD_OID="$(git -C "${ROOT}" rev-parse --verify 'HEAD^{commit}')"
if [[ ! "${HEAD_OID}" =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "Could not freeze a valid source commit" >&2
  exit 2
fi

# Match run-release-gate.sh so local isolated tests use the same dependencies.
PYTHON="${AI_FILTER_TEST_PYTHON:-${ROOT}/.venv-release-gate/bin/python}"
if [[ "${PYTHON}" != /* || ! -x "${PYTHON}" ]]; then
  echo "AI_FILTER_TEST_PYTHON must be an absolute executable path" >&2
  exit 2
fi

TMP_BASE="${AI_FILTER_TEST_TMPDIR:-/var/tmp}"
if [[ "${TMP_BASE}" != /* || ! -d "${TMP_BASE}" || ! -w "${TMP_BASE}" || ! -x "${TMP_BASE}" ]]; then
  echo "AI_FILTER_TEST_TMPDIR must be an absolute writable directory" >&2
  exit 2
fi
if [[ "${MODE}" == "full" && -n "${AI_FILTER_TEST_NODE:-}" ]]; then
  # The sentinel prevents command substitution from stripping real trailing LFs.
  # Remove only pwd's one output newline and the fixed sentinel, not path bytes.
  NODE_TMP_CAPTURE="$(cd -- "${TMP_BASE}" && pwd -P && printf '.')"
  TMP_BASE="${NODE_TMP_CAPTURE%$'\n.'}"
else
  TMP_BASE="$(cd -- "${TMP_BASE}" && pwd -P)"
fi
if [[ "${TMP_BASE}" == "/" ]]; then
  echo "Refusing to use / as the test temporary directory" >&2
  exit 2
fi

# Only this explicit synthetic-evidence destination crosses the clean env.
# It is never used as a cleanup target, test selector, or production path.
ISOLATED_EVIDENCE_ENV=()
CUTOVER_EVIDENCE_DIR="${AI_FILTER_CUTOVER_EVIDENCE_DIR:-}"
if [[ -n "${CUTOVER_EVIDENCE_DIR}" ]]; then
  if [[ "${CUTOVER_EVIDENCE_DIR}" != /* || -L "${CUTOVER_EVIDENCE_DIR}" || \
        ! -d "${CUTOVER_EVIDENCE_DIR}" || ! -O "${CUTOVER_EVIDENCE_DIR}" || \
        ! -w "${CUTOVER_EVIDENCE_DIR}" || ! -x "${CUTOVER_EVIDENCE_DIR}" ]]; then
    echo "AI_FILTER_CUTOVER_EVIDENCE_DIR must be an owned private absolute directory" >&2
    exit 2
  fi
  CUTOVER_EVIDENCE_DIR="$(cd -- "${CUTOVER_EVIDENCE_DIR}" && pwd -P)"
  case "${CUTOVER_EVIDENCE_DIR}" in
    /|"${ROOT}"|"${ROOT}"/*)
      echo "Synthetic evidence must be outside the source worktree" >&2
      exit 2
      ;;
  esac
  EVIDENCE_MODE="$(stat -c '%a' "${CUTOVER_EVIDENCE_DIR}" 2>/dev/null || stat -f '%Lp' "${CUTOVER_EVIDENCE_DIR}")"
  if [[ "${EVIDENCE_MODE}" != "700" ]]; then
    echo "Synthetic evidence directory must have mode 0700" >&2
    exit 2
  fi
  ISOLATED_EVIDENCE_ENV=("AI_FILTER_CUTOVER_EVIDENCE_DIR=${CUTOVER_EVIDENCE_DIR}")
fi

# A fixed Node supplied explicitly by CI is the only optional executable exposed
# to full tests. Do not inherit setup-node's tool directory or the caller's PATH.
TEST_NODE=""
if [[ "${MODE}" == "full" && -n "${AI_FILTER_TEST_NODE:-}" ]]; then
  # PATH uses ':' separators even when assignments are quoted. Check the input
  # as well as its canonical path (a directory alias can hide a separator).
  for NODE_TMP_BASE in "${AI_FILTER_TEST_TMPDIR:-/var/tmp}" "${TMP_BASE}"; do
    case "${NODE_TMP_BASE}" in
      *:*|*$'\n'*|*$'\r'*)
        echo "AI_FILTER_TEST_TMPDIR must not contain PATH separators or line breaks when Node is supplied" >&2
        exit 2
        ;;
    esac
  done
  TEST_NODE="${AI_FILTER_TEST_NODE}"
  if [[ "${TEST_NODE}" != /* || "${TEST_NODE}" == *$'\n'* || \
        "${TEST_NODE}" == *$'\r'* || -L "${TEST_NODE}" || \
        ! -f "${TEST_NODE}" || ! -x "${TEST_NODE}" ]]; then
    echo "AI_FILTER_TEST_NODE must be an absolute executable regular file" >&2
    exit 2
  fi
  NODE_DIR_CAPTURE="$(cd -- "$(dirname -- "${TEST_NODE}")" && pwd -P && printf '.')"
  NODE_DIR="${NODE_DIR_CAPTURE%$'\n.'}"
  if [[ "${NODE_DIR}" == *$'\n'* || "${NODE_DIR}" == *$'\r'* ]]; then
    echo "AI_FILTER_TEST_NODE resolved directory must not contain line breaks" >&2
    exit 2
  fi
  TEST_NODE="${NODE_DIR}/$(basename -- "${TEST_NODE}")"
  case "${TEST_NODE}" in
    "${ROOT}"|"${ROOT}"/*)
      echo "AI_FILTER_TEST_NODE must be outside the source worktree" >&2
      exit 2
      ;;
  esac
  NODE_MODE="$(stat -c '%a' "${TEST_NODE}" 2>/dev/null || stat -f '%Lp' "${TEST_NODE}")"
  if [[ -L "${TEST_NODE}" || ! -f "${TEST_NODE}" || ! -x "${TEST_NODE}" || \
        ! "${NODE_MODE}" =~ ^[0-7]{3,4}$ ]] || (( (8#${NODE_MODE} & 0022) != 0 )); then
    echo "AI_FILTER_TEST_NODE must not be a link or group/other writable" >&2
    exit 2
  fi
fi

WORK_ROOT="$(mktemp -d "${TMP_BASE}/ai-filter-release-tests.XXXXXX")"
WORKTREE="${WORK_ROOT}/worktree"
TEST_HOME="${WORK_ROOT}/home"
TEST_TMP="${WORK_ROOT}/tmp"
COVERAGE_TMP=""
mkdir -m 0700 "${TEST_HOME}" "${TEST_TMP}"

safe_remove_tree() {
  local target="$1"
  case "${target}" in
    "${TMP_BASE}"/ai-filter-release-tests.*) ;;
    *)
      echo "Refusing cleanup outside the controlled test prefix: ${target}" >&2
      return 70
      ;;
  esac

  # GNU rm's mount-boundary guard is used on Linux.  The fallback keeps the
  # runner usable on developer Macs while retaining the exact path guard above.
  if rm --help 2>&1 | grep -q -- '--one-file-system'; then
    rm -rf --one-file-system -- "${target}"
  else
    rm -rf -- "${target}"
  fi
}

cleanup() {
  local original_rc=$? cleanup_rc=0
  trap - EXIT HUP INT TERM

  if [[ -n "${COVERAGE_TMP}" ]]; then
    case "${COVERAGE_TMP}" in
      "${COVERAGE_DIR}"/."${COVERAGE_BASENAME}".release-tests.*)
        rm -f -- "${COVERAGE_TMP}" || cleanup_rc=70
        ;;
      *)
        echo "Refusing unexpected coverage temporary path: ${COVERAGE_TMP}" >&2
        cleanup_rc=70
        ;;
    esac
  fi

  if [[ -e "${WORKTREE}" ]]; then
    if ! git -C "${ROOT}" worktree remove --force "${WORKTREE}"; then
      echo "git worktree removal failed; attempting guarded temporary cleanup" >&2
      cleanup_rc=70
      safe_remove_tree "${WORK_ROOT}" || cleanup_rc=70
      git -C "${ROOT}" worktree prune --expire=now || cleanup_rc=70
    fi
  fi

  if [[ -e "${WORK_ROOT}" ]]; then
    safe_remove_tree "${WORK_ROOT}" || cleanup_rc=70
  fi

  if (( cleanup_rc != 0 )); then
    echo "Release-test temporary cleanup failed" >&2
    if (( original_rc == 0 )); then
      exit "${cleanup_rc}"
    fi
  fi
  exit "${original_rc}"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

git -C "${ROOT}" -c core.hooksPath=/dev/null \
  worktree add --detach "${WORKTREE}" "${HEAD_OID}"

SAFE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [[ -n "${TEST_NODE}" ]]; then
  NODE_BIN="${WORK_ROOT}/node-bin"
  mkdir -m 0700 "${NODE_BIN}"
  ln -s -- "${TEST_NODE}" "${NODE_BIN}/node"
  SAFE_PATH="${NODE_BIN}:${SAFE_PATH}"
fi
if [[ "${MODE}" == "focused" ]]; then
  PYTEST_ARGS=(
    -I -m pytest
    tests/test_release_script.py
  )
else
  PYTEST_ARGS=(
    -I -m pytest
    --cov
    --cov-config=.coveragerc
    --cov-report=xml
    --cov-report=term
    --cov-fail-under=75
  )
fi

(
  cd -- "${WORKTREE}"
  exec env -i \
    HOME="${TEST_HOME}" \
    TMPDIR="${TEST_TMP}" \
    PATH="${SAFE_PATH}" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    PYTHONNOUSERSITE=1 \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    ${ISOLATED_EVIDENCE_ENV[@]+"${ISOLATED_EVIDENCE_ENV[@]}"} \
    "${PYTHON}" "${PYTEST_ARGS[@]}"
)

if [[ "${MODE}" == "full" ]]; then
  if [[ ! -s "${WORKTREE}/coverage.xml" || -L "${WORKTREE}/coverage.xml" ]]; then
    echo "Full test mode did not produce a regular coverage.xml" >&2
    exit 1
  fi
  if [[ -n "${COVERAGE_OUTPUT}" ]]; then
    COVERAGE_TMP="$(mktemp \
      "${COVERAGE_DIR}/.${COVERAGE_BASENAME}.release-tests.XXXXXX")"
    install -m 0600 "${WORKTREE}/coverage.xml" "${COVERAGE_TMP}"
    mv -f -- "${COVERAGE_TMP}" "${COVERAGE_OUTPUT}"
    COVERAGE_TMP=""
  fi
fi
