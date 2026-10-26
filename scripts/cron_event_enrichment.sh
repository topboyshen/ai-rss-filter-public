#!/usr/bin/env bash
# 定时事件富化与合并：回放真实个股映射并合并跨源重复事件。
# 若不调度此任务，resolve_event_graph 永远不会运行，确认的跨源重复事件
# （规范化标题一致 + 独立来源 + 36 小时内）会一直以两条 active 记录并存。
set -euo pipefail
umask 077

ROOT="${AI_FILTER_ROOT:-/root/ai-rss-filter}"
REGISTRY_DIR="${RELEASE_REGISTRY_DIR:-${ROOT}/data/releases}"
CURRENT_RELEASE_FILE="${REGISTRY_DIR}/current.state"
MANIFEST_DIR="${REGISTRY_DIR}/manifests"
LOCK_FILE="${AI_FILTER_CRON_ENRICHMENT_LOCK:-${REGISTRY_DIR}/event-enrichment.lock}"
RELEASE_LOCK_FILE="${REGISTRY_DIR}/release.lock"
REFRESH_EXECUTION_LOCK_FILE="${ROOT}/data/.refresh-execution.lock"
STATE_FILE="${AI_FILTER_ENRICHMENT_STATE_FILE:-/var/lib/ai-filter-ops-check/event-enrichment.json}"
MAINTENANCE_FILE="${AI_FILTER_MAINTENANCE_FILE:-${HEALTH_MAINTENANCE_FILE:-/var/lib/ai-filter-health/maintenance}}"
RELEASE_LOCK_TIMEOUT="${ENRICHMENT_RELEASE_LOCK_TIMEOUT_SECONDS:-300}"
RUN_TIMEOUT="${ENRICHMENT_RUN_TIMEOUT_SECONDS:-540}"
ENRICH_DAYS="${AI_FILTER_ENRICH_DAYS:-7}"
ENRICH_LIMIT="${AI_FILTER_ENRICH_LIMIT:-5000}"
ENRICH_MAX_PAIRS="${AI_FILTER_ENRICH_MAX_PAIRS:-10000}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# 与 cron_refresh.sh 一致：维护哨兵在锁创建与任何数据库写入之前检查，
# 损坏的符号链接同样视为处于维护态（fail closed）。
if [[ "${MAINTENANCE_FILE}" != /* ]]; then
  log "error: maintenance sentinel path must be absolute"
  exit 78
fi
if [[ -e "${MAINTENANCE_FILE}" || -L "${MAINTENANCE_FILE}" ]]; then
  log "maintenance: event enrichment skipped (${MAINTENANCE_FILE})"
  exit 0
fi

for required_command in flock python3 docker timeout sha256sum; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    log "error: required command is unavailable: ${required_command}"
    exit 69
  fi
done
if [[ ! "$RELEASE_LOCK_TIMEOUT" =~ ^[0-9]+$ ]] || [[ ! "$RUN_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  log "error: invalid enrichment timeout configuration"
  exit 2
fi
if [[ ! -d "$REGISTRY_DIR" ]]; then
  log "error: release registry is missing (${REGISTRY_DIR})"
  exit 3
fi

write_enrichment_state() {
  local status=$1 started_at=$2 completed_at=$3 exit_code=$4 result=${5:-}
  AI_FILTER_STATE_RESULT="$result" python3 - "$STATE_FILE" "$status" "$started_at" "$completed_at" "$exit_code" <<'PY'
import json
import os
import stat
import sys
import tempfile

path, status, started_at, completed_at, exit_code = sys.argv[1:]
if not os.path.isabs(path):
    raise SystemExit("enrichment state path must be absolute")
parent = os.path.realpath(os.path.dirname(path))
os.makedirs(parent, mode=0o700, exist_ok=True)
parent_stat = os.stat(parent)
if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022:
    raise SystemExit("enrichment state directory ownership or mode is unsafe")
try:
    current = os.lstat(path)
except FileNotFoundError:
    pass
else:
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise SystemExit("enrichment state must be a regular non-symlink file")
    if current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) & 0o077:
        raise SystemExit("enrichment state ownership or mode is unsafe")
payload = {
    "version": 1,
    "status": status,
    "started_at": int(started_at),
    "completed_at": int(completed_at),
    "exit_code": int(exit_code),
}
raw = os.environ.pop("AI_FILTER_STATE_RESULT", "")
result = None
if raw:
    if len(raw) > 1024 * 1024:
        raise SystemExit("enrichment result is too large")
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        if status == "ok":
            raise SystemExit("enrichment result is malformed")
elif status == "ok":
    raise SystemExit("enrichment result is malformed")
if status == "ok" and not isinstance(result, dict):
    raise SystemExit("enrichment result is malformed")
if isinstance(result, dict):
    core_status = str(result.get("status") or "")
    if status != "ok" and core_status in {"degraded", "failed"}:
        payload["status"] = core_status
    mappings = result.get("mappings") if isinstance(result, dict) else None
    if not isinstance(mappings, dict):
        mappings = {}
    payload.update(
        mapping_deferred_groups=max(0, int(mappings.get("deferred_groups") or 0)),
        mapping_unreplayable_groups=max(
            0, int(mappings.get("unreplayable_groups") or 0)
        ),
        mapping_exhausted_groups=max(
            0, int(mappings.get("exhausted_groups") or 0)
        ),
        mapping_failed_evidence=max(
            0, int(mappings.get("failed_evidence") or 0)
        ),
    )
fd, temporary = tempfile.mkstemp(prefix=".event-enrichment-", dir=parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
print(payload["status"])
PY
}

# 锁路径校验与 cron_refresh.sh 相同：先验证父目录与已有 inode，
# 打开后再比对描述符与路径，防替换竞争。
if ! python3 - "${LOCK_FILE}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
euid = os.geteuid()

if not os.path.isabs(path):
    raise SystemExit("enrichment lock path must be absolute")

parent = os.path.realpath(os.path.dirname(path))
try:
    parent_stat = os.stat(parent)
except OSError as exc:
    raise SystemExit(f"cannot inspect enrichment lock directory: {exc}")
if not stat.S_ISDIR(parent_stat.st_mode):
    raise SystemExit("enrichment lock parent must be a directory")
if parent_stat.st_uid != euid or stat.S_IMODE(parent_stat.st_mode) & 0o022:
    raise SystemExit("enrichment lock directory must be owned by the effective user and not group/world-writable")

try:
    lock_stat = os.lstat(path)
except FileNotFoundError:
    pass
except OSError as exc:
    raise SystemExit(f"cannot inspect enrichment lock: {exc}")
else:
    if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
        raise SystemExit("enrichment lock must be a regular non-symlink file")
    if lock_stat.st_uid != euid or stat.S_IMODE(lock_stat.st_mode) & 0o077:
        raise SystemExit("enrichment lock must be owned by the effective user and owner-only")
PY
then
  log "error: refusing unsafe enrichment lock path (${LOCK_FILE})"
  exit 78
fi

if ! exec 9>>"${LOCK_FILE}"; then
  log "error: cannot open enrichment lock (${LOCK_FILE})"
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
    raise SystemExit(f"cannot verify opened enrichment lock: {exc}")

if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
    raise SystemExit("opened enrichment lock is not a regular non-symlink file")
if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
    raise SystemExit("enrichment lock changed while it was being opened")
if fd_stat.st_uid != os.geteuid() or stat.S_IMODE(fd_stat.st_mode) & 0o077:
    raise SystemExit("opened enrichment lock is not owner-only")
PY
then
  log "error: enrichment lock verification failed (${LOCK_FILE})"
  exit 78
fi
if ! flock -n 9; then
  log "duplicate: previous event enrichment is still running"
  exit 0
fi

if ! python3 - "$RELEASE_LOCK_FILE" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
if not os.path.isabs(path):
    raise SystemExit("release lock path must be absolute")
parent = os.path.realpath(os.path.dirname(path))
parent_stat = os.stat(parent)
if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022:
    raise SystemExit("release lock directory ownership or mode is unsafe")
try:
    value = os.lstat(path)
except FileNotFoundError:
    pass
else:
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise SystemExit("release lock must be a regular non-symlink file")
    if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & 0o022:
        raise SystemExit("release lock ownership or mode is unsafe")
PY
then
  log "error: refusing unsafe release lock path (${RELEASE_LOCK_FILE})"
  exit 78
fi
exec 8>>"${RELEASE_LOCK_FILE}"
if ! python3 - "$RELEASE_LOCK_FILE" <<'PY'
import os
import stat
import sys

path_stat = os.lstat(sys.argv[1])
fd_stat = os.fstat(8)
if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
    raise SystemExit("opened release lock is unsafe")
if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
    raise SystemExit("release lock changed while opening")
if fd_stat.st_uid != os.geteuid() or stat.S_IMODE(fd_stat.st_mode) & 0o022:
    raise SystemExit("opened release lock ownership or mode is unsafe")
PY
then
  log "error: release lock verification failed (${RELEASE_LOCK_FILE})"
  exit 78
fi
if ! flock -s -w "$RELEASE_LOCK_TIMEOUT" 8; then
  log "error: timed out waiting for release lock"
  exit 6
fi

# The worker holds the container peer of this file for the complete refresh
# sweep (`/app/data/.refresh-execution.lock`).  The host bind mount exposes the
# same inode below ROOT/data, so taking this lock serializes the two long-lived
# SQLite writer lanes across their separate containers.  A busy enrichment run
# is deliberately deferred to the next cron tick; leaving the last successful
# heartbeat untouched lets the ops check detect persistent starvation as stale.
if ! python3 - "$REFRESH_EXECUTION_LOCK_FILE" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
euid = os.geteuid()
if not os.path.isabs(path):
    raise SystemExit("refresh execution lock path must be absolute")

parent = os.path.realpath(os.path.dirname(path))
try:
    parent_stat = os.stat(parent)
except OSError as exc:
    raise SystemExit(f"cannot inspect refresh execution lock directory: {exc}")
if not stat.S_ISDIR(parent_stat.st_mode):
    raise SystemExit("refresh execution lock parent must be a directory")
if parent_stat.st_uid != euid or stat.S_IMODE(parent_stat.st_mode) & 0o022:
    raise SystemExit(
        "refresh execution lock directory must be owned by the effective user "
        "and not group/world-writable"
    )

try:
    lock_stat = os.lstat(path)
except FileNotFoundError:
    pass
except OSError as exc:
    raise SystemExit(f"cannot inspect refresh execution lock: {exc}")
else:
    if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
        raise SystemExit("refresh execution lock must be a regular non-symlink file")
    # Existing worker-created files may be 0644. Readability is harmless for an
    # empty advisory lock; ownership and write permissions are the integrity
    # boundary.
    if lock_stat.st_uid != euid or stat.S_IMODE(lock_stat.st_mode) & 0o022:
        raise SystemExit(
            "refresh execution lock must be owned by the effective user and "
            "not group/world-writable"
        )
PY
then
  log "error: refusing unsafe refresh execution lock path (${REFRESH_EXECUTION_LOCK_FILE})"
  exit 78
fi

if ! exec 7>>"${REFRESH_EXECUTION_LOCK_FILE}"; then
  log "error: cannot open refresh execution lock (${REFRESH_EXECUTION_LOCK_FILE})"
  exit 73
fi
if ! python3 - "$REFRESH_EXECUTION_LOCK_FILE" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    path_stat = os.lstat(path)
    fd_stat = os.fstat(7)
except OSError as exc:
    raise SystemExit(f"cannot verify opened refresh execution lock: {exc}")
if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
    raise SystemExit("opened refresh execution lock is not a regular non-symlink file")
if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
    raise SystemExit("refresh execution lock changed while it was being opened")
if fd_stat.st_uid != os.geteuid() or stat.S_IMODE(fd_stat.st_mode) & 0o022:
    raise SystemExit("opened refresh execution lock ownership or mode is unsafe")
PY
then
  log "error: refresh execution lock verification failed (${REFRESH_EXECUTION_LOCK_FILE})"
  exit 78
fi
if ! flock -n 7; then
  log "status=deferred reason=refresh_execution_lock_busy retry=next_schedule"
  exit 0
fi

started="$(date +%s)"
finalized=0
on_exit() {
  local rc=$?
  if (( rc != 0 && finalized == 0 )); then
    set +e
    write_enrichment_state failed "$started" "$(date +%s)" "$rc" "" >/dev/null
  fi
  trap - EXIT
  exit "$rc"
}
trap on_exit EXIT
write_enrichment_state running "$started" 0 0 "" >/dev/null

if [[ ! -s "$CURRENT_RELEASE_FILE" ]]; then
  log "error: current release state is missing (${CURRENT_RELEASE_FILE})"
  exit 3
fi
state=()
while IFS= read -r value || [[ -n "$value" ]]; do
  state+=("$value")
done < "$CURRENT_RELEASE_FILE"
if [[ "${#state[@]}" -ne 4 ]] || [[ -z "${state[0]}" || -z "${state[1]}" || -z "${state[2]}" || -z "${state[3]}" ]]; then
  log "error: invalid current release state"
  exit 3
fi
for value in "${state[@]}"; do
  if [[ "$value" == *$'\r'* ]]; then
    log "error: malformed current release state"
    exit 3
  fi
done
IMAGE_REF="${state[0]}"
RECORDED_DIGEST="${state[1]}"
MANIFEST_HASH="${state[2]}"
if [[ "$IMAGE_REF" == "latest" || "$IMAGE_REF" == *":latest" ]] || \
   { [[ ! "$IMAGE_REF" =~ ^sha256:[0-9a-f]{64}$ ]] && \
     [[ ! "$IMAGE_REF" =~ ^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$ ]] && \
     [[ ! "$IMAGE_REF" =~ ^[A-Za-z0-9._:/-]+:v?[0-9][A-Za-z0-9._-]*$ ]]; }; then
  log "error: invalid immutable image ref in current release state"
  exit 3
fi
if [[ ! "$RECORDED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || [[ ! "$MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]]; then
  log "error: invalid release digest or manifest hash"
  exit 3
fi
MANIFEST_FILE="${MANIFEST_DIR}/${MANIFEST_HASH}.compose.yml"
if [[ ! -f "$MANIFEST_FILE" || ! -r "$MANIFEST_FILE" ]]; then
  log "error: archived Compose manifest is missing"
  exit 3
fi
ACTUAL_MANIFEST_HASH="$(sha256sum -- "$MANIFEST_FILE" | awk '{print $1}')"
if [[ "$ACTUAL_MANIFEST_HASH" != "$MANIFEST_HASH" ]]; then
  log "error: archived Compose manifest hash mismatch"
  exit 3
fi
if ! docker image inspect "$IMAGE_REF" >/dev/null 2>&1; then
  log "error: recorded production image is not available locally"
  exit 3
fi
ACTUAL_DIGEST="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')"
if [[ "$ACTUAL_DIGEST" != "$RECORDED_DIGEST" ]]; then
  log "error: recorded production image digest mismatch"
  exit 3
fi
# Scheduled work must use the same explicitly recorded runtime configuration
# as the active release. Never carry a forced override into another release.
OVERRIDE_ARGS=()
OVERRIDE_FILE="$(python3 - "$REGISTRY_DIR" "$IMAGE_REF" "$RECORDED_DIGEST" "$MANIFEST_HASH" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

registry = Path(sys.argv[1])
path_file = registry / "current.override"
hash_file = registry / "current.override-sha256"

def read_regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("override metadata is not a regular file")
    return path.read_text(encoding="utf-8").strip()

try:
    if not any(p.exists() or p.is_symlink() for p in (path_file, hash_file)):
        raise SystemExit(0)
    path = read_regular(path_file)
    override = Path(path)
    expected_hash = read_regular(hash_file)
    if (not override.is_absolute() or override.parent != registry
            or not override.name.endswith(".compose.yml")
            or any(c in path for c in "\r\n\t")
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ValueError("invalid override path or hash")
    read_regular(override)
    if hashlib.sha256(override.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("override hash mismatch")
    receipt = override.with_name(override.name.removesuffix(".compose.yml") + ".receipt")
    fields = {}
    for line in read_regular(receipt).splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise ValueError("invalid forced release receipt")
        fields[key] = value
    expected = {
        "status": "forced_gate_bypass_healthy",
        "ref": sys.argv[2], "digest": sys.argv[3],
        "base_manifest_sha256": sys.argv[4],
        "override_path": path, "override_sha256": expected_hash,
        "migration_gate_enforced": "false",
        "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE": "0",
    }
    if any(fields.get(key) != value for key, value in expected.items()):
        raise ValueError("forced receipt does not match the active release")
    print(path)
except (OSError, UnicodeError, ValueError) as exc:
    print(f"event enrichment override invalid: {exc}", file=sys.stderr)
    raise SystemExit(3)
PY
)"
if [[ -n "$OVERRIDE_FILE" ]]; then
  OVERRIDE_ARGS=(-f "$OVERRIDE_FILE")
fi
CONFIGURED_SERVICES="$(
  IMAGE_REF="$RECORDED_DIGEST" docker compose --project-directory "$ROOT" \
    -f "$MANIFEST_FILE" ${OVERRIDE_ARGS[@]+"${OVERRIDE_ARGS[@]}"} --profile cron config --services
)"
if ! grep -Fqx -- "event-enrichment" <<< "$CONFIGURED_SERVICES"; then
  log "error: current release manifest does not define event-enrichment"
  exit 3
fi

log "release=${IMAGE_REF} digest=${RECORDED_DIGEST} manifest=${MANIFEST_HASH}"
set +e
result="$(
  IMAGE_REF="$RECORDED_DIGEST" timeout --signal=TERM --kill-after=60s \
    "${RUN_TIMEOUT}s" docker compose --project-directory "$ROOT" \
      -f "$MANIFEST_FILE" ${OVERRIDE_ARGS[@]+"${OVERRIDE_ARGS[@]}"} --profile cron run --pull never --rm --no-deps \
      event-enrichment python scripts/run_event_enrichment.py \
      --days "$ENRICH_DAYS" --limit "$ENRICH_LIMIT" --max-pairs "$ENRICH_MAX_PAIRS"
)"
rc=$?
set -e
if (( rc != 0 )); then
  completed="$(date +%s)"
  observed_status="$(write_enrichment_state failed "$started" "$completed" "$rc" "$result")"
  finalized=1
  elapsed="$(( completed - started ))"
  log "error: event enrichment failed (exit=${rc})"
  log "status=${observed_status} elapsed_seconds=${elapsed} result=${result}"
  exit "$rc"
fi

completed="$(date +%s)"
observed_status="$(write_enrichment_state ok "$started" "$completed" 0 "$result")"
finalized=1
elapsed="$(( completed - started ))"
log "status=${observed_status} elapsed_seconds=${elapsed} result=${result}"
