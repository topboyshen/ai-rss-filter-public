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

# Only the release controller's separately reviewed executable/SHA pair enables
# this mode. Ambient AI_FILTER_TEST_NODE, NODE_OPTIONS and caller PATH do not.
gate_node_guard() {
  "${PYTHON_PATH}" -I - "$1" "${GATE_NODE_INPUT}" "${GATE_NODE_EXPECTED}" "${WORK_ROOT}" "${ROOT}" <<'AI_FILTER_RELEASE_GATE_NODE_GUARD'
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

MAX_NODE = 128 * 1024**2
MAX_PROOF = 32768
GUARD_SECONDS = 5
IDENTITY = ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns')


class NodeGuardError(ValueError):
    pass


def need(value, reason):
    if not value:
        raise NodeGuardError(reason)


def identity(info):
    return {name: getattr(info, 'st_' + name) for name in IDENTITY}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def plain_path(raw):
    need(type(raw) is str and 1 < len(raw) <= 1024 and raw.isascii()
         and not any(ord(c) < 32 or ord(c) == 127 or c == ':' for c in raw), 'invalid_path')
    path = Path(raw)
    need(path.is_absolute() and str(path) == raw and '..' not in path.parts, 'invalid_path')
    return path


def chain(path, *, temporary=False):
    records = []
    for item in [*reversed(path.parents), path]:
        info = os.lstat(item)
        need(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0, 'unsafe_path_owner_or_link')
        sticky_root = temporary and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        need(not info.st_mode & 0o022 or sticky_root, 'writable_path_component')
        need(item == path or stat.S_ISDIR(info.st_mode), 'non_directory_parent')
        records.append({'path': str(item), 'identity': identity(info)})
    need(os.path.realpath(path, strict=True) == str(path), 'noncanonical_path')
    return records


def source_snapshot(raw, expected, source_root, work_root):
    path = plain_path(raw)
    need(path.name == 'node' and len(expected) == 64 and all(c in '0123456789abcdef' for c in expected),
         'invalid_node_or_sha256')
    need(not path.is_relative_to(source_root) and not path.is_relative_to(work_root), 'node_inside_source_or_work')
    rows = chain(path)
    info = os.lstat(path)
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_mode & 0o111
         and 0 < info.st_size <= MAX_NODE, 'invalid_node_file')
    return path, rows, identity(info)


def private_root(raw):
    path = plain_path(raw)
    chain(path, temporary=True)
    info = os.lstat(path)
    need(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700, 'unsafe_private_root')
    return path, {name: getattr(info, 'st_' + name) for name in ('dev', 'ino', 'uid', 'gid', 'mode')}


def checked_open(path, expected_identity, flags=os.O_RDONLY):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        need(identity(os.fstat(fd)) == expected_identity, 'opened_identity_changed')
    except BaseException:
        os.close(fd)
        raise
    return fd


def digest_fd(fd, *, destination=None):
    result, count = hashlib.sha256(), 0
    while True:
        block = os.read(fd, min(65536, MAX_NODE + 1 - count))
        if not block:
            break
        count += len(block)
        need(count <= MAX_NODE, 'node_size_limit')
        result.update(block)
        if destination is not None:
            view = memoryview(block)
            while view:
                written = os.write(destination, view)
                need(written > 0, 'short_private_write')
                view = view[written:]
    return result.hexdigest(), count


def validate_copy(path, expected):
    rows = chain(path, temporary=True)
    info = os.lstat(path)
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o500
         and 0 < info.st_size <= MAX_NODE, 'unsafe_private_node')
    fd = checked_open(path, identity(info))
    try:
        actual, size = digest_fd(fd)
        need(actual == expected and size == info.st_size and identity(os.fstat(fd)) == identity(info),
             'private_node_hash_or_identity_changed')
    finally:
        os.close(fd)
    need(chain(path, temporary=True) == rows, 'private_node_path_changed')
    return identity(info)


def stage(raw, expected, root_raw, source_raw):
    need(os.geteuid() == 0, 'root_required_for_explicit_node')
    work, work_identity = private_root(root_raw)
    source_root = plain_path(source_raw)
    path, before, source_identity = source_snapshot(raw, expected, source_root, work)
    fd = checked_open(path, source_identity)
    try:
        private_bin = work / 'node-bin'
        os.mkdir(private_bin, 0o700)
        private = private_bin / 'node'
        output = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o500)
        try:
            actual, size = digest_fd(fd, destination=output)
            need(actual == expected and size == source_identity['size'], 'node_sha256_mismatch')
            os.fsync(output)
        finally:
            os.close(output)
        need(identity(os.fstat(fd)) == source_identity and chain(path) == before, 'source_changed_during_copy')
    finally:
        os.close(fd)
    need(private_root(root_raw)[1] == work_identity, 'private_root_changed')
    copy_identity = validate_copy(private, expected)
    bin_identity = identity(os.lstat(private_bin))
    need(stat.S_IMODE(bin_identity['mode']) == 0o700, 'unsafe_private_bin')
    proof = {'schema': 1, 'source': raw, 'expected_sha256': expected, 'source_root': source_raw,
             'source_chain': before, 'source_identity': source_identity, 'work_identity': work_identity,
             'copy_identity': copy_identity, 'bin_identity': bin_identity}
    payload = encoded(proof)
    need(len(payload) <= MAX_PROOF, 'proof_size_limit')
    proof_fd = os.open(work / 'node-proof.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(proof_fd, view)
            need(count > 0, 'short_proof_write')
            view = view[count:]
        os.fsync(proof_fd)
    finally:
        os.close(proof_fd)
    return public_proof(payload, expected)


def read_proof(work):
    path = work / 'node-proof.json'
    info = os.lstat(path)
    need(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
         and stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= MAX_PROOF, 'unsafe_private_proof')
    fd = checked_open(path, identity(info))
    try:
        data = bytearray()
        while len(data) <= MAX_PROOF:
            block = os.read(fd, min(4096, MAX_PROOF + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        need(len(data) == info.st_size and identity(os.fstat(fd)) == identity(info)
             and identity(os.lstat(path)) == identity(info), 'private_proof_changed')
    finally:
        os.close(fd)
    return bytes(data)


def public_proof(payload, expected):
    result = {'node_sha256': expected, 'private_proof_sha256': hashlib.sha256(payload).hexdigest(),
              'metadata_proof': json.loads(payload)}
    need(len(encoded(result)) <= MAX_PROOF, 'audit_proof_size_limit')
    return result


def verify(raw, expected, root_raw, source_raw):
    need(os.geteuid() == 0, 'root_required_for_explicit_node')
    work, work_identity = private_root(root_raw)
    payload = read_proof(work)
    proof = json.loads(payload)
    keys = {'schema', 'source', 'expected_sha256', 'source_root', 'source_chain', 'source_identity',
            'work_identity', 'copy_identity', 'bin_identity'}
    need(type(proof) is dict and set(proof) == keys and proof['schema'] == 1
         and type(proof['schema']) is int and encoded(proof) == payload, 'invalid_private_proof')
    need(proof['source'] == raw and proof['expected_sha256'] == expected and proof['source_root'] == source_raw
         and proof['work_identity'] == work_identity, 'private_binding_changed')
    path, rows, info = source_snapshot(raw, expected, plain_path(source_raw), work)
    need(rows == proof['source_chain'] and info == proof['source_identity'], 'source_identity_changed')
    fd = checked_open(path, info)
    try:
        actual, size = digest_fd(fd)
        need(actual == expected and size == info['size'] and identity(os.fstat(fd)) == info,
             'source_hash_or_fd_changed')
    finally:
        os.close(fd)
    private_bin = work / 'node-bin'
    need(identity(os.lstat(private_bin)) == proof['bin_identity'], 'private_bin_changed')
    with os.scandir(private_bin) as entries:
        names = []
        for entry in entries:
            names.append(entry.name)
            need(len(names) <= 1, 'extra_private_executable')
    need(names == ['node'], 'private_node_missing')
    need(validate_copy(private_bin / 'node', expected) == proof['copy_identity'], 'private_copy_identity_changed')
    need(chain(path) == rows and private_root(root_raw)[1] == work_identity, 'final_path_changed')
    return public_proof(payload, expected)


def main(argv):
    # This child never extends an enclosing controller budget. The alarm covers
    # metadata, hashing, fsync and both outputs; kernel-uninterruptible I/O is not
    # claimed to have stopped merely because a parent reports a timeout.
    need(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'existing_guard_timer')
    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()

    def expired(_signum, _frame):
        os._exit(124)

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, GUARD_SECONDS)
    try:
        need(len(argv) == 5 and argv[0] in {'stage', 'verify'}, 'fixed_guard_operation_required')
        result = (stage if argv[0] == 'stage' else verify)(*argv[1:])
        need(time.monotonic() - started < GUARD_SECONDS, 'node_guard_deadline')
        print(json.dumps(result, sort_keys=True, separators=(',', ':')), flush=True)
        return 0
    except Exception as error:
        reason = str(error) if type(error) is NodeGuardError else 'node_guard_io_or_input_failure'
        print('Release gate Node guard: ' + reason, file=sys.stderr, flush=True)
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
AI_FILTER_RELEASE_GATE_NODE_GUARD
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

GATE_NODE_INPUT=""
GATE_NODE_EXPECTED=""
GATE_NODE_ENABLED=0
if [[ -n "${AI_FILTER_RELEASE_GATE_NODE+x}${AI_FILTER_RELEASE_GATE_NODE_SHA256+x}" ]]; then
  [[ -n "${AI_FILTER_RELEASE_GATE_NODE:-}" && -n "${AI_FILTER_RELEASE_GATE_NODE_SHA256:-}" ]] || \
    die "explicit release-gate Node requires both executable and reviewed SHA-256"
  GATE_NODE_INPUT="${AI_FILTER_RELEASE_GATE_NODE}"
  GATE_NODE_EXPECTED="${AI_FILTER_RELEASE_GATE_NODE_SHA256}"
  GATE_NODE_ENABLED=1
fi

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

GATE_NODE_LOG_PROOF=""
if [[ "${GATE_NODE_ENABLED}" == "1" ]]; then
  GATE_NODE_LOG_PROOF="$(gate_node_guard stage)" || die "explicit release-gate Node staging failed"
fi

git -C "${ROOT}" -c core.hooksPath=/dev/null worktree add --detach "${WORKTREE}" "${HEAD_OID}"

DETACHED_HEAD="$(git -C "${WORKTREE}" rev-parse --verify 'HEAD^{commit}')"
[[ "${DETACHED_HEAD}" == "${HEAD_OID}" ]] || die "detached worktree is not at the frozen commit"
DETACHED_STATUS="$(git -C "${WORKTREE}" status --porcelain=v1 --untracked-files=all)"
[[ -z "${DETACHED_STATUS}" ]] || die "detached worktree was not initially clean"

SAFE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [[ "${GATE_NODE_ENABLED}" == "1" ]]; then
  SAFE_PATH="${WORK_ROOT}/node-bin:${SAFE_PATH}"
fi
STARTED_EPOCH="$(date +%s)"
[[ "${STARTED_EPOCH}" =~ ^[0-9]+$ ]] || die "could not record start time"

set +e
(
  cd -- "${WORKTREE}"
  if [[ "${GATE_NODE_ENABLED}" == "1" ]]; then
    printf 'Release gate reviewed Node: %s\n' "${GATE_NODE_LOG_PROOF}"
  fi
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

if [[ "${GATE_NODE_ENABLED}" == "1" ]]; then
  GATE_NODE_FINAL_PROOF="$(gate_node_guard verify)" || die "explicit release-gate Node changed during tests"
  [[ "${GATE_NODE_FINAL_PROOF}" == "${GATE_NODE_LOG_PROOF}" ]] || die "release-gate Node proof changed"
fi

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
