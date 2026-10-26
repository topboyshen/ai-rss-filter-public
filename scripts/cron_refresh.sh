#!/usr/bin/env bash
# 定时全量 /refresh：密钥仅从 .env 读取，勿写进 crontab。
# 轮换密钥: scripts/rotate_filter_api_key.py → 改 .env → 重启容器；见 .env.example 说明。
set -euo pipefail
umask 077

ROOT="${AI_FILTER_ROOT:-/root/ai-rss-filter}"
LOCK_FILE="${AI_FILTER_CRON_REFRESH_LOCK:-/var/run/ai-filter-refresh.lock}"
ENDPOINT="${AI_FILTER_REFRESH_URL:-http://127.0.0.1:5000/refresh}"
MAINTENANCE_FILE="${AI_FILTER_MAINTENANCE_FILE:-${HEALTH_MAINTENANCE_FILE:-/var/lib/ai-filter-health/maintenance}}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# Maintenance is checked before command discovery, lock creation, and—most
# importantly—before sourcing the root-only .env that contains the API key.
# A broken symlink is also treated as an active sentinel so maintenance fails
# closed rather than unexpectedly issuing a refresh.
if [[ "${MAINTENANCE_FILE}" != /* ]]; then
  log "error: maintenance sentinel path must be absolute"
  exit 78
fi
if [[ -e "${MAINTENANCE_FILE}" || -L "${MAINTENANCE_FILE}" ]]; then
  log "maintenance: refresh trigger skipped (${MAINTENANCE_FILE})"
  exit 0
fi

for required_command in flock curl python3 mktemp; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    log "error: required command is unavailable: ${required_command}"
    exit 69
  fi
done

# The host trigger lock must not live in the container-writable data tree.
# Validate its parent and any existing inode before the shell opens the path;
# then compare the opened descriptor with the path to catch replacement races.
# Production runs as root, while the effective-UID check keeps this testable
# without weakening the root-owned production invariant.
if ! python3 - "${LOCK_FILE}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
euid = os.geteuid()

if not os.path.isabs(path):
    raise SystemExit("refresh lock path must be absolute")

parent = os.path.realpath(os.path.dirname(path))
try:
    parent_stat = os.stat(parent)
except OSError as exc:
    raise SystemExit(f"cannot inspect refresh lock directory: {exc}")
if not stat.S_ISDIR(parent_stat.st_mode):
    raise SystemExit("refresh lock parent must be a directory")
if parent_stat.st_uid != euid or stat.S_IMODE(parent_stat.st_mode) & 0o022:
    raise SystemExit("refresh lock directory must be owned by the effective user and not group/world-writable")

try:
    lock_stat = os.lstat(path)
except FileNotFoundError:
    pass
except OSError as exc:
    raise SystemExit(f"cannot inspect refresh lock: {exc}")
else:
    if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
        raise SystemExit("refresh lock must be a regular non-symlink file")
    if lock_stat.st_uid != euid or stat.S_IMODE(lock_stat.st_mode) & 0o077:
        raise SystemExit("refresh lock must be owned by the effective user and owner-only")
PY
then
  log "error: refusing unsafe refresh lock path (${LOCK_FILE})"
  exit 78
fi

if ! exec 9>>"${LOCK_FILE}"; then
  log "error: cannot open refresh lock (${LOCK_FILE})"
  exit 73
fi
if ! python3 - "${LOCK_FILE}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    path_stat = os.lstat(path)
    fd_stat = os.fstat(9)
except OSError as exc:
    raise SystemExit(f"cannot verify opened refresh lock: {exc}")

if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
    raise SystemExit("opened refresh lock is not a regular non-symlink file")
if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
    raise SystemExit("refresh lock changed while it was being opened")
if fd_stat.st_uid != os.geteuid() or stat.S_IMODE(fd_stat.st_mode) & 0o077:
    raise SystemExit("opened refresh lock is not owner-only")
PY
then
  log "error: refresh lock verification failed (${LOCK_FILE})"
  exit 78
fi
if ! flock -n 9; then
  log "duplicate: previous refresh trigger is still running"
  exit 0
fi

set -a
if [ -f "${ROOT}/.env" ]; then
  # shellcheck source=/dev/null
  . "${ROOT}/.env"
fi
set +a

if [ -z "${AI_FILTER_API_KEY:-}" ]; then
  log "error: AI_FILTER_API_KEY is unset"
  exit 2
fi

if [[ "${AI_FILTER_API_KEY}" == *$'\r'* || "${AI_FILTER_API_KEY}" == *$'\n'* ]]; then
  log "error: AI_FILTER_API_KEY must not contain CR or LF"
  exit 2
fi

# Keep credentials out of curl's process arguments. A private directory also
# prevents another user from replacing the header between creation and use.
request_dir="$(mktemp -d "${TMPDIR:-/tmp}/ai-filter-refresh.XXXXXX")"
response_file="${request_dir}/response"
header_file="${request_dir}/headers"
cleanup() {
  local status=$?
  rm -f -- "${response_file}" "${header_file}"
  rmdir -- "${request_dir}"
  return "${status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'X-API-Key: %s\n' "${AI_FILTER_API_KEY}" > "${header_file}"
chmod 600 "${header_file}"
# The sourced .env is exported; curl needs only the header file, not the key
# repeated in its inherited environment.
unset AI_FILTER_API_KEY

started="$(date +%s)"
http_code="$(
  curl --silent --show-error --noproxy '*' --request POST \
    --connect-timeout 5 --max-time 30 \
    --output "${response_file}" --write-out '%{http_code}' \
    --header "@${header_file}" \
    "${ENDPOINT}"
)" || {
  rc=$?
  log "error: refresh request failed (curl_exit=${rc})"
  exit "${rc}"
}

if [[ ! "${http_code}" =~ ^2[0-9][0-9]$ ]]; then
  log "error: refresh endpoint returned HTTP ${http_code}"
  exit 22
fi

status="$(python3 - "${response_file}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, ValueError, TypeError):
    raise SystemExit(1)

status = payload.get("status")
if status not in {"queued", "duplicate", "ok"}:
    raise SystemExit(1)
print(status)
PY
)" || {
  log "error: refresh endpoint returned an invalid success payload"
  exit 1
}

elapsed="$(( $(date +%s) - started ))"
log "status=${status} http=${http_code} elapsed_seconds=${elapsed}"
